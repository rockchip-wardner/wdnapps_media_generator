"""文生图接口：REST 控制 + WebSocket 状态推送。

挂在 ``/api/media-generator``（由卡片的 ``register_api`` 决定前缀）。

* ``GET  /api/media-generator/``            — 当前快照
* ``POST /api/media-generator/generate``     — 发起一次出图（**立即返回**）
* ``POST /api/media-generator/stop``         — 请求取消当前这次
* ``GET  /api/media-generator/history``      — 生成记录
* ``GET  /api/media-generator/image/{id}``   — 取某次生成的 PNG
* ``WS   /api/media-generator/ws``           — 状态变化时推送快照

⚠️ 出图是**重**操作（8 步 ≈ 34 s，20 步 ≈ 55 s，首次还要等 worker 把
模型 load 进来 ≈ 90 s）⇒ ``generate`` 绝不能同步等图，接口只负责
「收下请求 + 起线程 + 立刻回话」，结果靠 WS / 轮询拿。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from wdnapps_media_generator.cards.media_generator.service import (
    DEFAULT_STEPS,
    VALID_STEPS,
    ImageRequest,
    _first_png,
    find_run_dir,
    get_session,
    run_dir,
)

LOG = logging.getLogger("apps.media_generator")

router = APIRouter(tags=["media-generator"])


@router.get("/")
async def media_generator_state() -> dict:
    """当前状态快照（前端轮询 / WS 首帧都用它）。"""
    return get_session().snapshot()


@router.post("/generate")
async def media_generator_generate(payload: Optional[dict] = None) -> dict:
    """发起一次出图。

    入参：``{"prompt": "...", "steps": 8|20|28, "fixed_noise": false}``

    ``fixed_noise=True`` 用**固定初始噪声**（cshim 现场生成，与历史基线
    逐字节一致）⇒ 同一个提示词每次出**同一张图**，方便对拍与演示。
    """
    payload = payload or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="提示词不能为空")
    try:
        steps = int(payload.get("steps") or DEFAULT_STEPS)
    except (TypeError, ValueError):
        steps = DEFAULT_STEPS
    if steps not in VALID_STEPS:
        raise HTTPException(
            status_code=400,
            detail="steps 只能是 {} 之一".format(list(VALID_STEPS)),
        )
    req = ImageRequest(prompt, steps=steps,
                       fixed_noise=bool(payload.get("fixed_noise")))
    result = get_session().start(req)
    if not result.get("ok"):
        raise HTTPException(status_code=409,
                            detail=result.get("message") or "无法启动生成")
    return result


@router.post("/stop")
async def media_generator_stop() -> dict:
    """请求取消当前这次生成。

    ⚠️ 正在跑的那一次**打不断**（NPU 调用不可中断）—— 它的结果会被丢弃。
    """
    return get_session().stop()


@router.get("/history")
async def media_generator_history() -> dict:
    """最近生成过的图（新的在前）。"""
    return {"items": get_session().history()}


@router.get("/image/{run_id}")
async def media_generator_image(run_id: str):
    """取某次生成的 PNG。

    走 ``run_id`` 而不是绝对路径 ⇒ 没有路径穿越面。
    """
    d = find_run_dir(run_id)
    png = _first_png(d)
    if png is None:
        raise HTTPException(status_code=404, detail="该次生成没有图片")
    return FileResponse(str(png), media_type="image/png")


# --------------------------------------------------------------------------- #
# WebSocket 状态流
# --------------------------------------------------------------------------- #
@router.websocket("/ws")
async def media_generator_ws(ws: WebSocket) -> None:
    """状态变化时推送快照（前端不用一直轮询）。"""
    await ws.accept()
    sess = get_session()
    last_key = None
    try:
        while True:
            snap = sess.snapshot()
            key = (
                snap["state"],
                snap.get("error") or "",
                snap.get("capability_ready"),
                tuple(snap.get("capability_loading") or ()),
                snap.get("history_count"),
                (snap.get("last") or {}).get("run_id"),
            )
            if key != last_key:
                last_key = key
                payload = dict(snap)
                payload["type"] = "status"
                await ws.send_json(payload)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        LOG.debug("media_generator ws closed: %s", Path(__file__).name)
        return
