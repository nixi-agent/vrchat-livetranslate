"""Engine 无头测试：不依赖 API key / 网络，验证生命周期与语言切换。"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlt.config import AppConfig, Direction, load_config
from vlt.engine import Engine, EngineEvents


def _make_cfg() -> AppConfig:
    cfg = load_config()
    return cfg


def test_engine_construction():
    cfg = _make_cfg()
    events = EngineEvents()
    engine = Engine(cfg=cfg, direction="mine", source="mic", sinks={"chatbox"}, events=events)
    assert not engine.running
    print("  construction OK")


def test_engine_stop_idempotent():
    cfg = _make_cfg()
    events = EngineEvents()
    engine = Engine(cfg=cfg, direction="mine", source="mic", sinks={"chatbox"}, events=events)
    engine.stop()
    engine.stop()
    assert not engine.running
    print("  stop idempotent OK")


def test_set_languages_no_session():
    cfg = _make_cfg()
    events = EngineEvents()
    engine = Engine(cfg=cfg, direction="mine", source="mic", sinks={"chatbox"}, events=events)
    ok = engine.set_languages("zh", "ja")
    assert ok
    assert cfg.directions["mine"].source_lang == "zh"
    assert cfg.directions["mine"].target_lang == "ja"
    print("  set_languages (no session) OK")


def test_set_languages_budget_warn():
    """预算检查只在需要重建会话时生效；无会话时只改配置，不撞预算。"""
    cfg = _make_cfg()
    cfg.session_base["max_new_sessions_per_minute"] = 2
    events = EngineEvents()
    engine = Engine(cfg=cfg, direction="mine", source="mic", sinks={"chatbox"}, events=events)
    engine._connect_ts = [time.monotonic(), time.monotonic()]
    ok = engine.set_languages("zh", "en")
    assert ok
    assert cfg.directions["mine"].source_lang == "zh"
    assert cfg.directions["mine"].target_lang == "en"
    print("  set_languages budget (no session) OK")


def test_engine_pcm_dry_run():
    """用 PCM 文件跑引擎（dry-run），验证生命周期完整。"""
    pcm = Path(__file__).resolve().parent.parent / "testdata" / "zh_test_16k.pcm"
    if not pcm.exists():
        print("  pcm dry-run SKIP (no test data)")
        return
    cfg = _make_cfg()
    texts: list[tuple] = []
    statuses: list[tuple] = []
    events = EngineEvents(
        on_text=lambda src, txt, final: texts.append((src, txt, final)),
        on_status=lambda level, msg: statuses.append((level, msg)),
    )
    engine = Engine(
        cfg=cfg, direction="mine", source=f"pcm:{pcm}",
        sinks={"chatbox"}, events=events, dry_run=True, settle_s=2.0,
    )
    engine.start()
    engine.join(timeout=30)
    engine.stop(timeout=5)
    assert not engine.running
    assert engine.chatbox is not None
    print(f"  pcm dry-run OK (texts={len(texts)}, chatbox_sent={engine.chatbox.sent_ok})")


if __name__ == "__main__":
    print("test_engine:")
    test_engine_construction()
    test_engine_stop_idempotent()
    test_set_languages_no_session()
    test_set_languages_budget_warn()
    test_engine_pcm_dry_run()
    print("ALL PASSED")
