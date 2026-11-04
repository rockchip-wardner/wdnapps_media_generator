"""图片生成（Qwen-Image-2.1）会话服务 —— 卡片后端的业务逻辑。

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
常驻），之后**每张图 33~125 s**（8 / 20 / 28 步）。所以本模块的所有接口都是
**异步**的：``generate`` 立刻返回，前端用 WebSocket 收状态。

⚠️ **超时**：worker 侧的推理预算是**每模型**的（写在 worker manifest 的
``infer_timeout_s`` 里，文生图是 300 s）—— 全局默认 60 s 会把 20/28 步
的出图砍掉（``inference exceeded 60.0s``）。
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
# 错误可读化
# --------------------------------------------------------------------------- #
#: 把 worker / 传输层的原始异常翻成一句人话。
#:
#: 原始错误形如
#: ``RuntimeError: LLMRPCSession[wdndds/rpc/qwenimage21/worker-70476] worker
#: error: TimeoutError: inference exceeded 60.0s`` —— 对普通用户毫无意义。
#: 这里只做**翻译**，不改变错误类型，也不吞掉原始信息（原文附在后面）。
_ERROR_HINTS = (
    ("inference exceeded",
     "推理超时：worker 侧的推理预算用完了（NPU 可能卡住了）。"
     "请到「算力配置」页停掉再启用这个实例。"),
    ("session not loaded",
     "模型还没加载完（首次 load 约 90 秒），请等加载完成再试。"),
    ("no data",
     "worker 没有回话（链路断了），请到「算力配置」页重启这个实例。"),
)


def friendly_error(exc: BaseException) -> str:
    """把异常变成一句中文说明（原文照附在后面）。

    用来把 ``RuntimeError: ... worker error: TimeoutError: inference
    exceeded 60.0s`` 这种传输层原文变成「推理超时：…」——
    **不改变语义**，原文仍然保留在 ``（原文：…）`` 里，方便排查。
    """
    raw = f"{type(exc).__name__}: {exc}"
    low = raw.lower()
    for needle, hint in _ERROR_HINTS:
        if needle in low:
            return "{}（原文：{}）".format(hint, raw)
    return raw


# --------------------------------------------------------------------------- #
# 进度
# --------------------------------------------------------------------------- #
#: worker 通过 DDS delta 通道推过来的**结构化**进度消息前缀
#: （wdnai 的 image backend 把 cshim 的回调转成这种格式，见
#: ``wdnai/backends/image.py::_on_cshim_progress``）。
PROGRESS_PREFIX = "qi|"

#: 阶段 → 中文标签（前端直接显示）
STAGE_LABELS = {
    "init":     "加载模型",
    "te":       "编码提示词",
    "flow":     "生成图像",
    "vae_init": "加载解码器",
    "vae":      "解码图像",
    "release":  "释放采样器",
    "save":     "保存图片",
    "done":     "完成",
}

#: 各阶段在**整条流水线**里的大致占比（用来算总进度百分比）。
#: 实测（8 步）：te 0.2 s / flow 21 s / vae 10 s ⇒ flow 占大头。
STAGE_WEIGHTS = {
    "te": 0.02, "flow": 0.80, "vae": 0.15,
    "vae_init": 0.01, "release": 0.01, "save": 0.01,
}

#: 流水线顺序（用于「还剩哪些阶段」）
STAGE_ORDER = ("te", "flow", "vae", "save")

#: 还没进入的阶段的**固定预估秒数**（板端实测值）。
#: ETA = 当前阶段的剩余时间 + 后面这些阶段的预估值。
STAGE_ETA_SECONDS = {
    "te": 0.3, "flow": 0.0, "vae": 10.0, "save": 0.3,
    "vae_init": 2.0, "release": 0.5,
}


def parse_progress(msg: str) -> Optional[dict]:
    """解析 worker 推来的进度消息；不是进度消息就返回 None。

    消息格式（见 :data:`PROGRESS_PREFIX`）::

        qi|stage=flow|step=3|total=8|elapsed_ms=2601.5
    """
    if not msg or not msg.startswith(PROGRESS_PREFIX):
        return None
    out: Dict[str, Any] = {}
    for part in msg[len(PROGRESS_PREFIX):].split("|"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    stage = out.get("stage")
    # ⚠️ 「step」必须**存在**（不是「能解析成数字」）—— 缺了说明这条消息
    # 不是一个完整的进度事件（多半是别的东西误打进来的）⇒ 丢掉。
    if not stage or "step" not in out:
        return None
    try:
        step = int(out.get("step", "0"))
        total = int(out.get("total", "0"))
        elapsed_ms = float(out.get("elapsed_ms", "0"))
    except (TypeError, ValueError):
        return None
    return {"stage": stage, "step": step, "total": total,
            "elapsed_ms": elapsed_ms}


class ProgressTracker:
    """把一串进度事件折成「进度条 + 每步耗时 + 还需多久」。

    输入是 worker 的结构化事件；输出是给前端的一个 dict：

    ==================  ====================================================
    字段                 含义
    ==================  ====================================================
    ``stage``           当前阶段（init/te/flow/vae/…）
    ``stage_label``     阶段中文名
    ``step`` / ``total`` 该阶段内的步序 / 总步数
    ``percent``         整条流水线的百分比（0~100）
    ``elapsed``         本次生成已用（秒）
    ``step_ms``         **刚这一步**用了多久（毫秒）
    ``step_avg_ms``     本阶段的平均每步（毫秒）
    ``eta``             **预计还需**多久（秒）
    ``detail``          一句话人话（「flow-match 第 3/8 步」）
    ==================  ====================================================
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._lock = threading.Lock()
        self.stage = ""
        self.step = 0
        self.total = 0
        self.step_ms = 0.0
        self._durations: List[float] = []
        self._stage_done: Dict[str, float] = {}   # 已完成阶段的耗时
        self._t0 = time.perf_counter()
        self._last: Optional[dict] = None

    def update(self, ev: dict) -> None:
        """吃进一个进度事件。"""
        with self._lock:
            stage = ev["stage"]
            if stage != self.stage:
                # 换阶段了 ⇒ 上一个阶段收尾，把它的耗时记下来
                if self.stage:
                    self._stage_done[self.stage] = sum(self._durations)
                self.stage = stage
                self._durations = []
                self.step = 0
                self.total = ev.get("total", 0)
            self.step = ev.get("step", 0)
            self.total = ev.get("total", self.total)
            ms = float(ev.get("elapsed_ms", 0.0))
            self.step_ms = ms
            if ms > 0:
                self._durations.append(ms)
            self._last = ev

    # -- 输出 ------------------------------------------------------------ #
    def snapshot(self) -> Optional[dict]:
        with self._lock:
            if not self.stage:
                return None
            stage = self.stage
            step = self.step
            total = self.total
            step_ms = self.step_ms
            durations = list(self._durations)
            done = dict(self._stage_done)
            t0 = self._t0

        elapsed = time.perf_counter() - t0
        avg = (sum(durations) / len(durations)) if durations else 0.0

        # ---- 百分比 ----
        percent = 0.0
        for st, w in STAGE_WEIGHTS.items():
            if st in done:
                percent += w
            elif st == stage:
                frac = (step / total) if total else 0.0
                percent += w * max(0.0, min(1.0, frac))
        percent = max(0.0, min(100.0, percent * 100.0))

        # ---- 还需多久 ----
        eta = 0.0
        if stage in ("flow", "vae") and total and avg > 0:
            eta += avg * max(0, total - step) / 1000.0
        elif stage == "flow" and total:
            # 一个步长都还没完成 ⇒ 用板端实测的均值兜底
            eta += 2.6 * max(0, total - step)
        # 后面还没开始的阶段
        try:
            idx = STAGE_ORDER.index(stage)
            rest = STAGE_ORDER[idx + 1:]
        except ValueError:
            rest = ()
        for st in rest:
            eta += STAGE_ETA_SECONDS.get(st, 0.0)
        if stage in ("vae_init", "release"):
            eta += STAGE_ETA_SECONDS.get(stage, 0.0)

        # ---- 人话 ----
        label = STAGE_LABELS.get(stage, stage)
        if stage == "flow":
            detail = "flow-match 第 {}/{} 步".format(step, total)
        elif stage == "vae":
            detail = "VAE 第 {}/{} 段".format(step, total)
        elif stage == "te":
            detail = "文本编码 第 {}/{} 张".format(step, total)
        elif total > 1:
            detail = "{} {}/{}".format(label, step, total)
        else:
            detail = label

        return {
            "stage": stage,
            "stage_label": label,
            "step": step,
            "total": total,
            "percent": round(percent, 1),
            "elapsed": round(elapsed, 2),
            "step_ms": round(step_ms, 1),
            "step_avg_ms": round(avg, 1),
            "eta": round(max(0.0, eta), 1),
            "detail": detail,
        }


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
    """图片生成会话状态机（单例）。

    状态：``idle`` → ``running`` → ``idle`` / ``error``。

    生成在**独立线程**里跑（``asyncio.run`` 包一层）：FastAPI 的事件
    循环不能被一次 35 s（8 步）/ 100 s（20 步）/ 125 s（28 步）的 NPU
    调用堵住。
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
        self._progress = ProgressTracker()

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
                "progress":    self._progress.snapshot(),
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

        已经在跑 ⇒ 拒绝（文生图是**重**能力：33~125 s 一张，排队没有意义，
        用户自己再点一次更清楚）。

        模型**还没加载完** ⇒ 也拒绝，但要说清楚是「在加载」还是「压根没
        启用」—— 前者等 90 秒就好，后者要去算力配置页开。前端已经按
        ``capability_ready`` 禁用了按钮，这里是同一道闸的**后端**版本：
        防的是「快照与点击之间 worker 掉了」这种竞态。
        """
        # ① 入参校验 —— 纯本地，先做。用户提示词没填就没必要去问
        #    registry「模型在不在」，回一句「提示词不能为空」最直接。
        if not req.prompt:
            return {"ok": False, "reason": "empty_prompt",
                    "message": "提示词不能为空"}
        # ② 就绪探测 —— **锁外**做（它要走 registry / DDS，是 I/O；
        #    ``snapshot()`` 也要做同样的探测，在锁里做会把它一起堵住）。
        if not self._probe_ready():
            loading = self._probe_loading()
            if loading:
                return {"ok": False, "reason": "loading",
                        "message": "模型正在加载（{}），首次约需 90 秒，"
                                   "加载完成后再试".format(", ".join(loading))}
            return {"ok": False, "reason": "not_ready",
                    "message": "图片生成模型未启用，请到「算力配置」页启用 "
                               "qwenimage21 实例（需要两张 NPU 卡）"}
        # ③ 状态迁移 —— 锁内一次做完：busy 检查与「置成 running」在同一个
        #    临界区里 ⇒ 两个并发的 start() 只有一个能进去（另一个看到
        #    STATE_RUNNING 就返回 busy），不会同时起两条生成线程。
        with self._lock:
            if self._state == self.STATE_RUNNING:
                return {"ok": False, "reason": "busy",
                        "message": "上一张还在生成中，请等它完成"}
            self._request = req
            self._cancel = False
            self._elapsed = 0.0
            self._error = ""
            self._state = self.STATE_RUNNING
            self._progress.reset()
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
            self._set_state(self.STATE_ERROR, friendly_error(exc))
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

    def _on_delta(self, msg: str) -> None:
        """worker 推来的增量消息（DDS delta 通道）。

        ⚠️ 这个回调跑在 **DDS 的读线程**上，不是事件循环线程 ⇒
        只做「改状态」这一件事：**不**在这里调 ``_emit()``（那会从
        非事件循环线程去碰 registry）。前端靠 WS 的 1 秒节拍把进度取走。

        消息里只有 ``qi|`` 开头的才是进度消息，其余（人类可读的日志行）忽略。
        """
        ev = parse_progress(msg)
        if ev is None:
            return
        self._progress.update(ev)

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
            on_delta=self._on_delta,
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
