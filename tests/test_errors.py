"""图片生成卡片：错误可读化 + 就绪拦截的单元测试。

不碰 NPU、不碰 webui —— 纯逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wdnapps_media_generator.cards.media_generator.service import (  # noqa: E402
    ImageRequest,
    MediaGenSession,
    friendly_error,
)


# --------------------------------------------------------------------------- #
# friendly_error
# --------------------------------------------------------------------------- #
def test_friendly_error_translates_infer_timeout():
    """worker 侧的 60 s 超时原文要被翻成人话，但**原文必须保留**。"""
    exc = RuntimeError(
        "LLMRPCSession[wdndds/rpc/qwenimage21/worker-70476] worker error: "
        "TimeoutError: inference exceeded 60.0s")
    msg = friendly_error(exc)
    assert "推理超时" in msg
    # 原文照附 ⇒ 排查时还能看到真正的错误
    assert "inference exceeded 60.0s" in msg


def test_friendly_error_translates_not_loaded():
    exc = RuntimeError("session not loaded; send action='load' first")
    msg = friendly_error(exc)
    assert "还没加载完" in msg
    assert "session not loaded" in msg


def test_friendly_error_passes_unknown_through():
    exc = ValueError("something else entirely")
    assert friendly_error(exc) == "ValueError: something else entirely"


# --------------------------------------------------------------------------- #
# start() 的就绪拦截
# --------------------------------------------------------------------------- #
@pytest.fixture
def not_ready(monkeypatch):
    """把「模型没就绪」这件事固定下来（不连 registry）。"""
    monkeypatch.setattr(MediaGenSession, "_probe_ready",
                        staticmethod(lambda: False))
    monkeypatch.setattr(MediaGenSession, "_probe_loading",
                        staticmethod(lambda: []))


@pytest.fixture
def loading(monkeypatch):
    monkeypatch.setattr(MediaGenSession, "_probe_ready",
                        staticmethod(lambda: False))
    monkeypatch.setattr(MediaGenSession, "_probe_loading",
                        staticmethod(lambda: ["qwenimage21"]))


def test_start_refuses_when_model_not_enabled(not_ready):
    sess = MediaGenSession()
    out = sess.start(ImageRequest("a cat"))
    assert out["ok"] is False
    assert out["reason"] == "not_ready"
    assert "算力配置" in out["message"]


def test_start_refuses_while_loading(loading):
    sess = MediaGenSession()
    out = sess.start(ImageRequest("a cat"))
    assert out["ok"] is False
    assert out["reason"] == "loading"
    assert "qwenimage21" in out["message"]


def test_start_refuses_empty_prompt(monkeypatch):
    """提示词为空要在**就绪检查之前**被拒 —— 否则用户会看到
    「模型未启用」这种跟他毫无关系的提示。"""
    monkeypatch.setattr(MediaGenSession, "_probe_ready",
                        staticmethod(lambda: False))
    monkeypatch.setattr(MediaGenSession, "_probe_loading",
                        staticmethod(lambda: []))
    sess = MediaGenSession()
    out = sess.start(ImageRequest("   "))
    assert out["ok"] is False
    assert out["reason"] == "empty_prompt"
