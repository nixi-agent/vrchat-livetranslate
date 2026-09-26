"""本地 repeat 抑制验收（改动③）。

## 治什么

服务端会因 `model repeat output happened` 掐线（已有①的主动重建兜底）；但在掐线之前，
模型重复吐的「对对对对对」已经一路刷到 chatbox / 手腕屏，**对方看得见**。现在：
连续 N 条最终译文高度雷同 → 判为 repeat → 不再刷出（留痕）→ 主动重建会话。

## 反例必须不判（误杀的代价是一次重连 + 丢一句真译文）

- 「好」→「好的」：相似度 0.667 < 0.9；
- 「嗯。」「好。」这类短应答连撞：短于 4 字的终版不参与判定；
- 正常连续三句不同的译文：相似度远低于 0.9。

## 离线可跑

判定是纯函数；引擎集成用假会话 + 直接喂 TextDelta，不连网络、不需要音频设备。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _cfg(**over):
    from vlt.config import AppConfig, Direction

    base = {"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
            "workspace_id": "", "reconnect_backoff": [0.05],
            "max_new_sessions_per_minute": 100, "final_silence_s": 0.1}
    base.update(over)
    return AppConfig(
        session_base=base,
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=False)},
        chatbox={}, merger={}, overlay={}, output={},
    )


# ---------------------------------------------------------------- 纯函数判定

def test_is_repeat_streak_positive_cases() -> None:
    from vlt.engine import is_repeat_streak

    assert is_repeat_streak(["对对对对对，就是这样", "对对对对对，就是这样", "对对对对对，就是这样"])
    assert is_repeat_streak(["hello there", "hello there"], hits=2)
    # 高度雷同（只差一个标点）也算
    assert is_repeat_streak(["我马上就到你家门口了。", "我马上就到你家门口了", "我马上就到你家门口了。"])
    print("  真 repeat 判得出 OK")


def test_is_repeat_streak_counter_examples() -> None:
    """★ 反例：这些**不得**被误判。"""
    from vlt.engine import is_repeat_streak

    # 「好」→「好的」：正常相似短句（相似度 0.667 < 0.9）
    assert not is_repeat_streak(["好", "好的", "好的"]), "「好→好的」被误判"
    # 短应答连撞：默认 min_len 由引擎传 4；纯函数层验证 min_len 生效
    assert not is_repeat_streak(["嗯。", "嗯。", "嗯。"], min_len=4), "短叹词连撞被误判"
    # 正常连续三句（内容不同）
    assert not is_repeat_streak(["我马上就到家了", "好的那你快点", "路上小心一点"])
    # 差一个字但相似度 < 0.9（8 字差 1 字 = 0.875）
    assert not is_repeat_streak(["abcdefgh", "abcdefgx", "abcdefgh"])
    # 空串 / 条数不足
    assert not is_repeat_streak(["", "", ""])
    assert not is_repeat_streak(["对对对对对", "对对对对对"])          # hits=3 条数不够
    assert not is_repeat_streak(["aaaaaaaa", "aaaaaaaa"], hits=1)      # hits<2 不判
    print("  反例不误判 OK（好→好的 / 短叹词 / 正常三句 / 边界相似度 / 空串）")


def test_repeat_guard_settings_validation() -> None:
    from vlt.engine import repeat_guard_settings

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        en, hits, ratio = repeat_guard_settings({
            "repeat_guard_enabled": 1,                 # 类型不对
            "repeat_guard_hits": 1,                    # <2
            "repeat_guard_ratio": 1.5,                 # 超出 (0,1]
        })
    assert (en, hits, ratio) == (True, 3, 0.9), (en, hits, ratio)
    out = buf.getvalue()
    for key in ("repeat_guard_enabled", "repeat_guard_hits", "repeat_guard_ratio"):
        assert key in out, f"{key} 非法却没留痕：{out!r}"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert repeat_guard_settings({}) == (True, 3, 0.9)
        assert repeat_guard_settings({"repeat_guard_hits": 4, "repeat_guard_ratio": 0.8}) \
            == (True, 4, 0.8)
    assert buf.getvalue() == "", "缺省/全合法时不该打印任何东西"
    print("  repeat_guard 配置校验留痕 + 回落默认 OK")


# ---------------------------------------------------------------- 引擎集成

class _FakeSession:
    """带「主动放弃」接口的假会话（复刻真实会话的 _abort_on_fatal 契约）。"""

    def __init__(self) -> None:
        self.alive = True
        self.fatal = ""
        self.closed = False
        self.on_text = None

    @property
    def is_alive(self) -> bool:
        return self.alive

    @property
    def fail_reason(self) -> str:
        return self.fatal

    def _abort_on_fatal(self, reason: str) -> None:
        if not self.fatal:
            self.fatal = reason
        self.alive = False

    async def start(self, on_text=None, on_audio=None, on_usage=None):
        self.on_text = on_text

    async def close(self) -> None:
        self.closed = True

    def tick(self) -> None:
        pass


def _final(text: str):
    from vlt.session.base import TextDelta

    return TextDelta(confirmed=text, pending="", is_final=True, source="源文")


def test_repeat_streak_suppressed_and_triggers_rebuild() -> None:
    """★ 连续 3 条雷同终版 → 第 3 条起不刷出 + 留痕 + 主动放弃会话 → 看门狗重连 → 恢复。"""
    import vlt.engine as E
    from vlt.engine import Engine, EngineEvents

    shown: list[str] = []
    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents(on_text=lambda _s, t, f: shown.append(t) if f else None))
    dead = _FakeSession()
    eng._session = dead
    fresh = _FakeSession()
    original_factory = E.create_session
    E.create_session = lambda scfg: fresh
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(2):                       # 前 2 条照常刷出
                eng._on_text(_final("对对对对对，就是这样"))
            assert shown == ["对对对对对，就是这样"] * 2, f"未达阈值不该抑制：{shown}"
            eng._on_text(_final("对对对对对，就是这样"))   # 第 3 条 → 触发
            assert shown == ["对对对对对，就是这样"] * 2, "触发的那一条也不该刷出"
            assert dead.fatal, "触发后必须主动放弃会话（走①的同一条路径）"
            assert "repeat" in dead.fatal
            # 抑制中：再来重复内容（含非 final 增量）全吞
            from vlt.session.base import TextDelta
            eng._on_text(TextDelta(confirmed="对对对对对", pending="", is_final=False))
            eng._on_text(_final("对对对对对，就是这样"))
            assert shown == ["对对对对对，就是这样"] * 2, f"抑制期仍在刷：{shown}"

            # 看门狗接手 → 重连成功（create_session 已换成 fresh）
            async def run() -> None:
                await eng._watchdog()
                assert eng._reconnect_task is not None, "会话被标死后看门狗没安排重连"
                await asyncio.wait_for(eng._reconnect_task, timeout=5)

            asyncio.run(run())
        out = buf.getvalue()
        assert "判为模型 repeat" in out, f"触发必须留痕：{out!r}"
        assert eng._session is fresh, "重连后会话没换新"
        assert dead.closed, "旧会话没有被关掉"
        assert not eng._repeat_suppressed, "新会话必须解除抑制（_create_session 清零）"
        eng._on_text(_final("How are you doing today"))
        assert shown[-1] == "How are you doing today", "重建后新译文必须照常刷出"
    finally:
        E.create_session = original_factory
    print("  repeat 触发 → 抑制刷出 + 留痕 + 主动重建 → 恢复 OK")


def test_repeat_suppression_lifts_on_diverse_text() -> None:
    """抑制中出现明显不同的终版（模型自己恢复了）→ 解除抑制，照常刷出。"""
    from vlt.engine import Engine, EngineEvents

    shown: list[str] = []
    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents(on_text=lambda _s, t, f: shown.append(t) if f else None))
    eng._session = _FakeSession()
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(3):
            eng._on_text(_final("对对对对对，就是这样"))
        assert eng._repeat_suppressed
        eng._on_text(_final("这句话跟前面完全不一样"))
    assert not eng._repeat_suppressed, "不同终版出现后必须解除抑制"
    assert shown[-1] == "这句话跟前面完全不一样"
    print("  译文恢复多样 → 自动解除抑制 OK")


def test_normal_short_replies_never_suppressed() -> None:
    """★ 反例（引擎层）：「好」「好的」「好的」连发不得触发抑制。"""
    from vlt.engine import Engine, EngineEvents

    shown: list[str] = []
    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents(on_text=lambda _s, t, f: shown.append(t) if f else None))
    fake = _FakeSession()
    eng._session = fake
    for t in ("好", "好的", "好的", "嗯", "嗯。"):
        eng._on_text(_final(t))
    assert shown == ["好", "好的", "好的", "嗯", "嗯。"], f"正常短应答被吞：{shown}"
    assert not fake.fatal, "短应答连撞竟然触发了重建"
    assert not eng._repeat_suppressed
    print("  短应答反例不触发 OK")


def test_guard_disabled_passes_everything() -> None:
    """repeat_guard_enabled: false → 重复内容照刷（用户可彻底关掉）。"""
    from vlt.engine import Engine, EngineEvents

    shown: list[str] = []
    eng = Engine(cfg=_cfg(repeat_guard_enabled=False), direction="mine", source="mic",
                 sinks=set(), events=EngineEvents(on_text=lambda _s, t, f: shown.append(t) if f else None))
    fake = _FakeSession()
    eng._session = fake
    for _ in range(5):
        eng._on_text(_final("对对对对对，就是这样"))
    assert len(shown) == 5 and not fake.fatal and not eng._repeat_suppressed
    print("  抑制禁用后全部照刷 OK")


if __name__ == "__main__":
    print("test_repeat_guard:")
    test_is_repeat_streak_positive_cases()
    test_is_repeat_streak_counter_examples()
    test_repeat_guard_settings_validation()
    test_repeat_streak_suppressed_and_triggers_rebuild()
    test_repeat_suppression_lifts_on_diverse_text()
    test_normal_short_replies_never_suppressed()
    test_guard_disabled_passes_everything()
    print("ALL PASSED")
