"""图片生成卡片：进度解析与进度条折算的单元测试。

不碰 NPU、不碰 webui —— 纯逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wdnapps_media_generator.cards.media_generator.service import (  # noqa: E402
    ProgressTracker,
    parse_progress,
)


def test_parse_progress_reads_all_fields():
    ev = parse_progress("qi|stage=flow|step=3|total=8|elapsed_ms=2601.5")
    assert ev == {"stage": "flow", "step": 3, "total": 8,
                  "elapsed_ms": 2601.5}


def test_parse_progress_ignores_human_messages():
    # 后端也会发人类可读的行（"[Qwen-Image-2.1] 生成中 …"）⇒ 必须忽略
    assert parse_progress("[Qwen-Image-2.1] 生成中 …") is None
    assert parse_progress("") is None
    assert parse_progress(None) is None


def test_parse_progress_tolerates_garbage():
    assert parse_progress("qi|stage=flow") is None          # 缺 total
    assert parse_progress("qi|stage=flow|step=x|total=8") is None  # step 不是数字


def test_tracker_percent_and_eta():
    t = ProgressTracker()
    t.update({"stage": "flow", "step": 3, "total": 8, "elapsed_ms": 2600.0})
    snap = t.snapshot()
    assert snap["stage"] == "flow"
    assert snap["stage_label"] == "生成图像"
    assert snap["step"] == 3 and snap["total"] == 8
    # flow 权重 0.80，3/8 ⇒ 30%
    assert snap["percent"] == pytest.approx(30.0, abs=0.1)
    # 这一步 2.6 s；平均也是 2.6 s
    assert snap["step_ms"] == pytest.approx(2600.0, abs=0.1)
    assert snap["step_avg_ms"] == pytest.approx(2600.0, abs=0.1)
    # 还剩 5 步 × 2.6 s + 后面 vae/save 的预估 ⇒ 明显大于 5×2.6
    assert snap["eta"] > 5 * 2.6
    assert "3/8" in snap["detail"]


def test_tracker_eta_shrinks_as_steps_advance():
    t = ProgressTracker()
    t.update({"stage": "te", "step": 1, "total": 1, "elapsed_ms": 200.0})
    t.update({"stage": "flow", "step": 1, "total": 8, "elapsed_ms": 2600.0})
    eta1 = t.snapshot()["eta"]
    for step in range(2, 9):
        t.update({"stage": "flow", "step": step, "total": 8,
                  "elapsed_ms": 2600.0})
    snap = t.snapshot()
    assert snap["eta"] < eta1
    # te(2%) + flow 8/8(80%) ⇒ 82%
    assert snap["percent"] == pytest.approx(82.0, abs=0.1)


def test_tracker_resets_between_runs():
    t = ProgressTracker()
    t.update({"stage": "flow", "step": 8, "total": 8, "elapsed_ms": 2600.0})
    assert t.snapshot()["percent"] >= 80.0
    t.reset()
    assert t.snapshot() is None


def test_tracker_switching_stage_keeps_step_counts_apart():
    t = ProgressTracker()
    t.update({"stage": "flow", "step": 8, "total": 8, "elapsed_ms": 2600.0})
    t.update({"stage": "vae", "step": 1, "total": 4, "elapsed_ms": 5000.0})
    snap = t.snapshot()
    assert snap["stage"] == "vae"
    assert snap["step"] == 1 and snap["total"] == 4
    # vae 的平均只算 vae 自己的那一步，不该混进 flow 的 8 步
    assert snap["step_avg_ms"] == pytest.approx(5000.0, abs=0.1)
    assert "VAE" in snap["detail"]
