"""图片生成接口：REST 控制 + WebSocket 状态推送。

挂在 ``/api/image-editor``（由卡片的 ``register_api`` 决定前缀）。

* ``GET  /api/image-editor/``            — 当前快照
* ``POST /api/image-editor/generate``     — 发起一次出图（**立即返回**）
* ``POST /api/image-editor/stop``         — 请求取消当前这次
* ``GET  /api/image-editor/history``      — 生成记录
* ``GET  /api/image-editor/image/{id}``   — 取某次生成的 PNG
* ``WS   /api/image-editor/ws``           — 状态变化时推送快照

⚠️ 出图是**重**操作（8 步 ≈ 35 s，20 步 ≈ 70 s，28 步 ≈ 95 s，40 步 ≈ 130 s，首次还要等
worker 把模型 load 进来 ≈ 90 s）⇒ ``generate`` 绝不能同步等图，接口只负责
「收下请求 + 起线程 + 立刻回话」，结果靠 WS / 轮询拿。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from wdnapps_media_generator.cards.image_editor.service import (
    DEFAULT_STEPS,
    DEFAULT_STRENGTH,
    VALID_STEPS,
    ImageRequest,
    _first_png,
    find_run_dir,
    get_session,
    run_dir,
)

LOG = logging.getLogger("apps.image_editor")

#: 单次 WS 推送的超时（秒）。超过就认为这条连接已经死了 —— 见
#: ``image_editor_ws`` 里那段关于 CLOSE-WAIT 的说明。
_WS_SEND_TIMEOUT = 5.0

#: 心跳：每 N 秒**无条件**推一次（即使状态没变）。
#: 作用是「让死掉的连接有机会被发现」—— 只在状态变化时推的话，
#: 一条已经死掉但没关闭的连接永远等不到 send ⇒ 永远发现不了它死了。
_WS_HEARTBEAT = 10

router = APIRouter(tags=["image-editor"])


@router.get("/")
async def image_editor_state() -> dict:
    """当前状态快照（前端轮询 / WS 首帧都用它）。"""
    return get_session().snapshot()


@router.post("/generate")
async def image_editor_generate(payload: Optional[dict] = None) -> dict:
    """发起一次**图生图**。

    入参::

        {"prompt": "...", "steps": 8|20|28|40, "fixed_noise": false,
         "image": "<base64>", "strength": 0.75}

    * ``image`` —— 输入图的 base64（可带 ``data:image/png;base64,`` 前缀）。
      **必填** —— 没有它就退化成文生图了，那是隔壁「图片生成」卡的事。
    * ``strength`` —— 保留原图的程度（0~1，默认 0.75）。1.0 ⇒ 几乎不听
      原图；越小越贴近原图。
    * ``fixed_noise=True`` 用**固定初始噪声**（cshim 现场生成，与历史基线
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
    try:
        strength = float(payload.get("strength", DEFAULT_STRENGTH))
    except (TypeError, ValueError):
        strength = DEFAULT_STRENGTH
    if not (0.05 <= strength <= 1.0):
        raise HTTPException(status_code=400, detail="strength 必须在 0.05~1.0 之间")
    req = ImageRequest(prompt, steps=steps,
                       fixed_noise=bool(payload.get("fixed_noise")),
                       image_b64=str(payload.get("image") or ""),
                       strength=strength)
    result = get_session().start(req)
    if not result.get("ok"):
        raise HTTPException(status_code=409,
                            detail=result.get("message") or "无法启动生成")
    return result


@router.post("/stop")
async def image_editor_stop() -> dict:
    """请求取消当前这次生成。

    ⚠️ 正在跑的那一次**打不断**（NPU 调用不可中断）—— 它的结果会被丢弃。
    """
    return get_session().stop()


@router.get("/history")
async def image_editor_history() -> dict:
    """最近生成过的图（新的在前）。"""
    return {"items": get_session().history()}


@router.get("/image/{run_id}")
async def image_editor_image(run_id: str):
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
async def image_editor_ws(ws: WebSocket) -> None:
    """状态变化时推送快照（前端不用一直轮询）。

    ⚠️⚠️ **必须同时「读」一次**，否则 webui 关不干净（2026-11-04 实测）：

    uvicorn 关停时对 WebSocket 只做 ``close(1001)``，**不会**让「只写不读」
    的协程看到 disconnect —— 它不写就不报错 ⇒ 这个 ASGI 任务永远不结束
    ⇒ ``Server.shutdown()`` 的 ``gather(server_state.tasks)`` 一直等
    ⇒ **webui 热重载 / 关停卡死**（表现：日志停在 "Waiting for background
    tasks to complete"，端口 8000 起不来）。

    ⇒ 这里起一个「只读」的 watcher 协程：客户端断开时它抛
    ``WebSocketDisconnect`` ⇒ 用 ``FIRST_COMPLETED`` 把它和推送协程赛跑，
    谁先结束都收尾。
    """
    await ws.accept()
    sess = get_session()
    last_key = None

    async def _push_loop() -> None:
        nonlocal last_key
        tick = 0
        while True:
            snap = sess.snapshot()
            prog = snap.get("progress") or {}
            key = (
                snap["state"],
                snap.get("error") or "",
                snap.get("capability_ready"),
                tuple(snap.get("capability_loading") or ()),
                snap.get("history_count"),
                (snap.get("last") or {}).get("run_id"),
                # 进度：阶段 + 步序变了就推（前端据此画进度条）
                prog.get("stage"),
                prog.get("step"),
            )
            tick += 1
            # 每 _WS_HEARTBEAT 秒**无条件**推一次（心跳）：
            #   只在「状态变化时」推的话，一条已经死掉但没关闭的连接
            #   永远等不到一次 send ⇒ 也就永远发现不了它死了 ⇒ 关停挂死。
            if key != last_key or tick % _WS_HEARTBEAT == 0:
                last_key = key
                payload = dict(snap)
                payload["type"] = "status"
                # ⚠️⚠️ **必须给 send 加超时**（2026-11-04 实测踩到）：
                #   对端「消失但不干净关闭」时（浏览器标签被杀 / 网断 /
                #   进程被 kill），socket 会停在 CLOSE-WAIT 且**还有数据没发出去**
                #   ⇒ uvicorn 的 ``await self.send(data)`` 一直等写缓冲排空
                #   ⇒ 这个协程**永远不返回** ⇒ uvicorn 关停时
                #   ``server_state.tasks`` 一直非空 ⇒ 整个 webui 的
                #   热重载 / 关停**卡死**（日志停在 "Waiting for background
                #   tasks to complete"，端口起不来）。
                #   ⇒ 超时即视为「连接已死」⇒ 主动收尾。
                try:
                    await asyncio.wait_for(
                        ws.send_json(payload), timeout=_WS_SEND_TIMEOUT)
                except (asyncio.TimeoutError, WebSocketDisconnect):
                    return
                except Exception:  # noqa: BLE001
                    return
            await asyncio.sleep(1.0)

    async def _watch_disconnect() -> None:
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            return

    push = asyncio.create_task(_push_loop())
    watch = asyncio.create_task(_watch_disconnect())
    try:
        await asyncio.wait({push, watch},
                           return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        LOG.debug("image_editor ws closed: %s", Path(__file__).name)
        return
    finally:
        for t in (push, watch):
            t.cancel()
        await asyncio.gather(push, watch, return_exceptions=True)
