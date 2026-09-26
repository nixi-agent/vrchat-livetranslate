"""chatbox 方向收敛回归测试。

验证 chatbox 只对 direction="mine" 生效：
  1) direction="theirs" + sinks={"chatbox"} → 不创建 chatbox、不往 chatbox 发任何东西
  2) direction="mine" + sinks={"chatbox"} → 照常创建并使用 chatbox（防修过头）
  3) mine 方向的 send_text() 仍能把译文送进 chatbox
  4) _on_text 在 theirs 方向不往 merger 推送

全程离线：不连服务端、不碰 Tk。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vlt.engine as engine_mod
from vlt.config import AppConfig, Direction
from vlt.engine import Engine, EngineEvents
from vlt.output.merger import Merger
from vlt.session.base import TextDelta


# ---------------------------------------------------------------- 测试替身


class FakeChatbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    def send(self, text: str, is_final: bool = False) -> bool:
        self.sent.append((text, is_final))
        return True


def _mk_engine(direction: str = "mine", sinks: set[str] | None = None) -> Engine:
    cfg = AppConfig(
        session_base={"api_key": "sk-test", "model": "qwen3.8-livetranslate-flash-realtime"},
        directions={
            "mine": Direction(source_lang="zh", target_lang="en"),
            "theirs": Direction(source_lang="en", target_lang="zh"),
        },
        chatbox={"max_chars": 144},
        merger={},
        text_input={"model": "qwen-mt-flash", "timeout_s": 5},
    )
    return Engine(
        cfg=cfg, direction=direction,
        source="mic" if direction == "mine" else "loopback",
        sinks=sinks or {"chatbox"},
        events=EngineEvents(),
        dry_run=True,
    )


# ---------------------------------------------------------------- 1) theirs 不创建 chatbox


def test_theirs_no_chatbox() -> bool:
    ok = True

    eng = _mk_engine("theirs", {"chatbox"})
    cond = eng._chatbox_wanted is False
    print(f"  theirs + chatbox in sinks → _chatbox_wanted={eng._chatbox_wanted}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    cond = eng.chatbox is None
    print(f"  theirs → engine.chatbox is None (未启动) = {eng.chatbox is None}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    # 模拟 _build_and_run 已跑过但没创建 chatbox 的状态：手动调 _on_text
    fake_cb = FakeChatbox()
    eng._chatbox = None
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final) if eng._chatbox else None,
                         interval_s=2.0)
    d = TextDelta(confirmed="Hello", is_final=True, source="Hello")
    eng._on_text(d)
    cond = fake_cb.sent == []
    print(f"  theirs _on_text → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    return ok


# ---------------------------------------------------------------- 2) mine 照常使用 chatbox


def test_mine_still_uses_chatbox() -> bool:
    ok = True

    eng = _mk_engine("mine", {"chatbox"})
    cond = eng._chatbox_wanted is True
    print(f"  mine + chatbox in sinks → _chatbox_wanted={eng._chatbox_wanted}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final),
                         interval_s=2.0)
    d = TextDelta(confirmed="Hello", is_final=True, source="你好")
    eng._on_text(d)
    cond = len(fake_cb.sent) > 0
    print(f"  mine _on_text → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    return ok


# ---------------------------------------------------------------- 3) mine send_text 仍送 chatbox


def test_mine_send_text_to_chatbox() -> bool:
    ok = True
    real = engine_mod.translate_text

    eng = _mk_engine("mine")
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._loop = asyncio.new_event_loop()
    eng._thread = type("T", (), {"is_alive": lambda self: True})()
    engine_mod.translate_text = lambda text, **kw: "Hello from typing"
    try:
        asyncio.run(eng._async_send_text("你好"))
    finally:
        engine_mod.translate_text = real
        eng._loop.close()

    cond = len(fake_cb.sent) > 0 and all(t for t, _ in fake_cb.sent)
    print(f"  mine send_text → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    return ok


# ---------------------------------------------------------------- 4) theirs send_text 被拒（方向不对）


def test_theirs_send_text_rejected() -> bool:
    eng = _mk_engine("theirs")
    eng._loop = asyncio.new_event_loop()
    eng._thread = type("T", (), {"is_alive": lambda self: True})()
    result = eng.send_text("Hello")
    eng._loop.close()
    cond = result is False
    print(f"  theirs send_text → 返回 {result}（应为 False）  "
          f"{'OK' if cond else '✗'}")
    return cond


# ---------------------------------------------------------------- 5) theirs 方向即使手动塞了 chatbox 也不走 merger


def test_theirs_merger_not_pushed() -> bool:
    eng = _mk_engine("theirs", {"chatbox"})
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final),
                         interval_s=2.0)

    d1 = TextDelta(confirmed="Hi", is_final=False, source="Hi")
    d2 = TextDelta(confirmed="Hello world", is_final=True, source="Hello world")
    eng._on_text(d1)
    eng._on_text(d2)

    cond = fake_cb.sent == []
    print(f"  theirs 即使有 chatbox 实例，merger 也不推送 → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    return cond


# ---------------------------------------------------------------- 主入口


def main() -> int:
    print("test_chatbox_direction:")
    results = [
        ("theirs 不创建 chatbox", test_theirs_no_chatbox()),
        ("mine 照常使用 chatbox", test_mine_still_uses_chatbox()),
        ("mine send_text 送 chatbox", test_mine_send_text_to_chatbox()),
        ("theirs send_text 被拒", test_theirs_send_text_rejected()),
        ("theirs merger 不推送", test_theirs_merger_not_pushed()),
    ]
    bad = [name for name, ok in results if not ok]
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
