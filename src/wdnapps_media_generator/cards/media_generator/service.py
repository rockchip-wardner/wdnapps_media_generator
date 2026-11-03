"""文生图（Qwen-Image-2.1）会话服务 —— 卡片后端的业务逻辑。

模型**不在本进程跑**：它在**算力注册中心**启用的 ``qwenimage21`` worker
进程里（``compute_worker.py --model qwenimage21``）。本模块只做四件事：

1. 找到那个实例的 RPC session（``app.core.compute.registry``）；
2. 把提示词送过去，等回一张 BGR ndarray；
3. 落盘成 PNG（**worker 自己写的那份**，逐字节等于运行时基线）；
4. 记历史 + 广播状态给前端。

为什么要走 worker 而不是本进程直接建 session：NPU 资源由算力注册中心
统一分配，worker 进程崩了不影响 Web UI；而且一个 worker 可以同时服务
多个消费者（卡片 / LAN 对端）。这与 TTS 卡是同一套机制。

⚠️ 耗时（板端实测，2×RK1828）：worker 首次 load 要 ~90 s（TE + DiT
常驻），之后**每张图 ≈ 34 s**（8 步）。所以本模块的所有接口都是
**异步**的：``generate`` 立刻返回，前端用 WebSocket 收状态。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import struct
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("apps.media_generator")

#: 文生图能力词（``wdnai.SoftwareCapability.TEXT_TO_IMAGE``）。
#: 刻意写成字面量而不是 import wdnai：``wdnai`` 只在**真正出图**时才需要，
#: 而 Web UI 的开机路径不应该因为模型 SDK 缺失就起不来（卡片仍能被
#: 发现，只是显示「功能未启用」）。
CAPABILITY = "swcap_text_to_image"

#: 允许的 flow-match 步数。步数是纯运行时参数（模型不用重建），
#: 但太小的步数出图糊、太大的步数慢 —— 8 / 20 / 28 覆盖「快 / 推荐 / 精细」。
VALID_STEPS = (8, 20, 28)
DEFAULT_STEPS = 8

#: 历史里最多保留多少张图（更早的连同落盘文件一起删）。
MAX_HISTORY = 50


# --------------------------------------------------------------------------- #
# 输出目录
# --------------------------------------------------------------------------- #
def output_root() -> Path:
    """本卡的产物根目录：``<resource_dir>/media_generator``。

    **不**用 ``app.core.uploads.get_card_uploads_root``：那落在 emmc
    分区（宿主仓库所在），而 emmc 的写入寿命是真实顾虑 —— 一张 512×512
    PNG 约 300 KB，攒几十张就把 emmc 写坏了。这里读
    ``config['base']['resource_dir']``（默认 ``/home/rockchip/assets``，
    在 NVMe 上），与 TTS 卡的 ``get_tts_output_root`` 同一口径。
    """
    from app.core.config import default_resource_root, get_config

    base = get_config().get_section("base", {}) or {}
    rd = base.get("resource_dir")
    root = Path(rd.strip()) if isinstance(rd, str) and rd.strip() \
        else default_resource_root()
    out = root / "media_generator"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 只读 fs / 权限不足 —— 让第一次真正的写盘暴露真正的原因。
        pass
    return out


def run_dir(run_id: str) -> Path:
    """某一次生成的落盘目录：``<output_root>/runs/<run_id>``。

    每次生成一个独立目录 ⇒ worker 写出来的 ``img01.png`` 不会被下一次
    覆盖，历史里每张图都有自己的一份。
    """
    d = output_root() / "runs" / _safe_id(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_run_dir(run_id: str) -> Path:
    """**只读**地定位某次生成的目录（不存在就返回一个不存在的路径）。

    与 :func:`run_dir` 的区别：这里**不 mkdir** ⇒ 查一张不存在的图不会
    在磁盘上留下空目录。
    """
    return output_root() / "runs" / _safe_id(run_id)


def _safe_id(run_id: str) -> str:
    """把 run_id 收拾成能当目录名的 slug（防路径穿越）。"""
    keep = [c for c in str(run_id or "") if c.isalnum() or c in "-_"]
    out = "".join(keep)[:64]
    return out or "run"


# --------------------------------------------------------------------------- #
# PNG 编码（回退路径专用）
# --------------------------------------------------------------------------- #
def _write_png(path: Path, bgr: Any) -> bool:
    """把 BGR ndarray 写成 8-bit RGB PNG —— 纯 stdlib（zlib），无 PIL/cv2。

    参数与 ``wdnai.backends.image._write_png`` **完全一致**（filter-0
    scanline + zlib level 6）⇒ 同一张图两条路径出来的字节相同。

    这条路径只在「worker 没有把 PNG 落盘」时用（正常情况下 worker 自己
    写的那份就在 run 目录里，见 :meth:`MediaGenSession._run`）。
    """
    try:
        import numpy as np
    except ImportError:
        LOG.warning("回退编码 PNG 需要 numpy，未安装 —— 该图无法落盘")
        return False
    arr = np.ascontiguousarray(bgr, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        LOG.warning("回退编码 PNG：期望 HxWx3，得到 %s", arr.shape)
        return False
    rgb = np.ascontiguousarray(arr[:, :, ::-1])
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    Path(path).write_bytes(png)
    return True


# --------------------------------------------------------------------------- #
# 请求对象
# --------------------------------------------------------------------------- #
class ImageRequest:
    """一次出图请求。"""

    __slots__ = ("run_id", "prompt", "steps", "fixed_noise", "created_at")

    def __init__(self, prompt: str, steps: int = DEFAULT_STEPS,
                 fixed_noise: bool = False):
        self.run_id = uuid.uuid4().hex[:12]
        self.prompt = (prompt or "").strip()
        self.steps = int(steps) if int(steps) in VALID_STEPS else DEFAULT_STEPS
        self.fixed_noise = bool(fixed_noise)
        self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "run_id":    self.run_id,
            "prompt":    self.prompt,
            "steps":     self.steps,
            "fixed_noise": self.fixed_noise,
            "created_at": self.created_at,
        }


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
class MediaGenSession:
    """文生图会话状态机（单例）。

    状态：``idle`` → ``running`` → ``idle`` / ``error``。

    生成在**独立线程**里跑（``asyncio.run`` 包一层）：FastAPI 的事件
    循环不能被一次 34 s（8 步）/ 55 s（20 步）的 NPU 调用堵住。
    """

    STATE_IDLE = "idle"
    STATE_RUNNING = "running"
    STATE_ERROR = "error"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = self.STATE_IDLE
        self._error = ""
        self._request: Optional[ImageRequest] = None
        self._elapsed = 0.0
        self._last_run: Optional[dict] = None
        self._history: List[dict] = []
        self._cancel = False
        self._thread: Optional[threading.Thread] = None
        self._broadcaster: Optional[Callable[[dict], None]] = None

    # -- 能力探测 ------------------------------------------------------- #
    @staticmethod
    def _probe_ready() -> bool:
        """有没有**正在运行**的文生图实例（含 LAN peer）。

        用 ``list_sessions`` 而不是 ``get_session``：后者每次都打一条
        INFO，而 snapshot 由前端轮询 / WS 推进调用，不能污染日志。
        """
        try:
            from app.core.compute.registry import get_registry
            return bool(get_registry().list_sessions(
                CAPABILITY, include_remote=True))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _probe_loading() -> List[str]:
        """本机持有文生图能力但**还在加载**的 model id 列表。

        与 ``_probe_ready`` 是同一件事的两面：ready=False 时，只有这里
        非空才能区分「模型正在加载」（等一等就好）和「压根没启用」
        （要去算力配置页开）。只看本机 —— peer 上的加载进度本机看不到。
        """
        try:
            from app.core.compute.registry import get_registry
            return list(get_registry().pending_models_for_capability(CAPABILITY))
        except Exception:  # noqa: BLE001
            return []

    # -- 快照 ----------------------------------------------------------- #
    def snapshot(self) -> dict:
        ready = self._probe_ready()
        with self._lock:
            return {
                "state":       self._state,
                "running":     self._state == self.STATE_RUNNING,
                "busy":        self._state == self.STATE_RUNNING,
                "available":   True,
                "capability_ready": ready,
                "capability_loading": self._probe_loading(),
                "error":       self._error,
                "elapsed":     round(self._elapsed, 2),
                "request":     self._request.to_dict() if self._request else None,
                "last":        self._last_run,
                "history_count": len(self._history),
                "output_root": str(output_root()),
            }

    def history(self) -> List[dict]:
        with self._lock:
            return list(self._history)

    # -- 广播 ----------------------------------------------------------- #
    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._broadcaster = fn

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            if self._broadcaster is fn:
                self._broadcaster = None

    def _emit(self) -> None:
        with self._lock:
            fn = self._broadcaster
        if fn is None:
            return
        try:
            fn(self.snapshot())
        except Exception:  # noqa: BLE001 — 一个坏订阅者不能带崩生成
            pass

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error
        self._emit()

    # -- 启停 ------------------------------------------------------------ #
    def start(self, req: ImageRequest) -> dict:
        """启动一次生成（**立即返回**，不等图出来）。

        已经在跑 ⇒ 拒绝（文生图是**重**能力：34~55 s 一张，排队没有意义，
        用户自己再点一次更清楚）。
        """
        with self._lock:
            if self._state == self.STATE_RUNNING:
                return {"ok": False, "reason": "busy",
                        "message": "上一张还在生成中，请等它完成"}
            if not req.prompt:
                return {"ok": False, "reason": "empty_prompt",
                        "message": "提示词不能为空"}
            self._request = req
            self._cancel = False
            self._elapsed = 0.0
            self._error = ""
            self._state = self.STATE_RUNNING
        self._emit()

        t = threading.Thread(target=self._run, args=(req,),
                             name="media-generator", daemon=True)
        self._thread = t
        t.start()
        return {"ok": True, "run_id": req.run_id}

    def stop(self) -> dict:
        """请求取消当前这次生成。

        ⚠️ **不能真的打断**：文生图是一次 NPU 调用（cshim 的 C 侧调用
        不可中断，``sink.stop()`` 只是一个标志位）。这里能做的是
        「这一张的结果不要了」—— 生成结束后不写进历史、不展示。
        """
        with self._lock:
            running = self._state == self.STATE_RUNNING
            self._cancel = True
        return {"ok": True, "running": running,
                "message": "已请求取消（正在跑的那一次无法中途打断）"}

    # -- 生成主循环 ------------------------------------------------------ #
    def _run(self, req: ImageRequest) -> None:
        t0 = time.time()
        try:
            png = self._generate(req)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("media_generator: 生成失败")
            with self._lock:
                self._elapsed = time.time() - t0
            self._set_state(self.STATE_ERROR, f"{type(exc).__name__}: {exc}")
            return

        with self._lock:
            self._elapsed = time.time() - t0
            cancelled = self._cancel
        if cancelled:
            LOG.info("media_generator: run %s 已被取消（结果丢弃）", req.run_id)
            self._set_state(self.STATE_IDLE)
            return

        entry = {
            "run_id":   req.run_id,
            "prompt":   req.prompt,
            "steps":    req.steps,
            "fixed_noise": req.fixed_noise,
            "png":      str(png),
            "url":      image_url(req.run_id),
            "created_at": req.created_at,
            "elapsed":  round(self._elapsed, 2),
        }
        with self._lock:
            self._history.insert(0, entry)
            self._last_run = entry
            over = self._history[MAX_HISTORY:]
            del self._history[MAX_HISTORY:]
        self._prune(over)
        self._set_state(self.STATE_IDLE)

    def _prune(self, stale: List[dict]) -> None:
        """删掉超出历史上限的落盘文件。"""
        for item in stale:
            try:
                d = run_dir(item.get("run_id", ""))
                shutil.rmtree(d, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

    def _generate(self, req: ImageRequest) -> Path:
        """把提示词送到 worker，等回一张图，落盘。

        两条取图路径（**优先第一条**）：

        1. **worker 自己写的 PNG**：把 ``output_dir`` 传给后端 ⇒ 后端在
           自己的临时目录里写好 PNG 后**原样复制**到该目录 ⇒ 字节与
           运行时基线**逐字节一致**（不重新编码）。
        2. **回退**：worker 没落盘（老后端）⇒ 用回程的 BGR ndarray 自己
           编码一份（参数与基线一致，同样逐字节一致）。
        """
        from app.core.compute.registry import get_registry

        sess = get_registry().get_session(CAPABILITY)
        target = run_dir(req.run_id)

        text, attachments = asyncio.run(sess.run_prompt(
            req.prompt,
            output_dir=str(target),
            steps=req.steps,
            noise_fresh=(not req.fixed_noise),
            noise_seed=0,
        ))

        png = _first_png(target)
        if png is not None:
            return png

        # 回退：worker 没落盘 ⇒ 自己从附件编码
        for att in attachments or []:
            images = getattr(att, "images", None)
            if not images:
                continue
            out = target / "img01.png"
            if _write_png(out, images[0]):
                return out
        raise RuntimeError(
            "worker 没有返回图片（既没落盘，附件里也没有图像）")

    def close(self) -> None:
        """释放资源（webui 关闭时调用）。"""
        with self._lock:
            self._cancel = True


def _first_png(d: Path) -> Optional[Path]:
    try:
        files = sorted(d.glob("*.png"))
    except OSError:
        return None
    return files[0] if files else None


def image_url(run_id: str) -> str:
    """某次生成的图片 URL（浏览器可直接用）。

    走 run_id 而不是绝对路径 ⇒ **没有路径穿越面**（不用自己拼
    ``?path=`` 再做白名单校验）。
    """
    return "/api/media-generator/image/" + _safe_id(run_id)


_SESSION: Optional[MediaGenSession] = None
_SESSION_LOCK = threading.Lock()


def get_session() -> MediaGenSession:
    """返回进程内唯一的 :class:`MediaGenSession`。"""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = MediaGenSession()
        return _SESSION
