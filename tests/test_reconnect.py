"""断线自愈验收：服务器掐断连接后必须**自动重连**，而不是让那条腿永久死掉。

## 真实事故（用户实测日志）

    [session] ⚠️ 服务端 error: {"code": "COMMON_ERROR", "message": "model repeat output happened"}
    ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。
    [session] 事件循环中断：ConnectionClosedError: received 1011 (internal error) ...
    [mine][error] 运行错误：received 1011 (internal error) model repeat output happened

服务端因为模型输出进入重复状态而 1011 掐断连接，**当时完全没有重连逻辑** ——
那条翻译腿就此结束，用户只能重开界面。

## 两件事一起验

1. `_watchdog()` 发现会话不健康就安排重连（带退避），重连成功后日志有明确结论；
2. 采集循环手里拿的是 `_SessionProxy` —— 重连换新会话后，音频必须发到**新**连接上
   （否则音频还在往死连接上灌）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _cfg():
    from vlt.config import AppConfig, Direction

    return AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [0.05],
                      "max_new_sessions_per_minute": 100, "final_silence_s": 0.1},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=False)},
        chatbox={}, merger={}, overlay={}, output={},
    )


class _FakeSession:
    """可控的假会话：能标记为"死了"，并记录收到的音频。"""

    def __init__(self, alive: bool = True, reason: str = "") -> None:
        self.alive = alive
        self.reason = reason
        self.received = 0
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return self.alive

    @property
    def fail_reason(self) -> str:
        return self.reason

    async def send_audio(self, pcm: bytes) -> None:
        self.received += len(pcm)

    async def close(self) -> None:
        self.closed = True

    def tick(self) -> None:
        pass


def test_proxy_forwards_to_current_session() -> None:
    """★ 重连换会话后，音频必须发到**新**会话（代理动态取）。"""
    from vlt.engine import Engine, EngineEvents

    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents())

    async def run() -> tuple[int, int]:
        s1 = _FakeSession()
        eng._session = s1
        await eng._proxy.send_audio(b"a" * 100)
        s2 = _FakeSession()                      # 模拟重连换了会话
        eng._session = s2
        await eng._proxy.send_audio(b"b" * 50)
        return s1.received, s2.received

    old, new = asyncio.run(run())
    assert old == 100, f"旧会话应只收到重连前的 100 字节，实际 {old}"
    assert new == 50, f"新会话应收到重连后的 50 字节，实际 {new}"
    print(f"  代理转发到当前会话 OK（旧 {old}B / 新 {new}B）")


def test_watchdog_reconnects_dead_session() -> None:
    """会话不健康时，看门狗必须安排重连并成功恢复。"""
    from vlt.engine import Engine, EngineEvents

    statuses: list[tuple[str, str]] = []
    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents(on_status=lambda lvl, msg: statuses.append((lvl, msg))))

    dead = _FakeSession(alive=False, reason="ConnectionClosedError: received 1011 "
                                            "(internal error) model repeat output happened")
    fresh = _FakeSession(alive=True)
    eng._session = dead

    calls = {"n": 0}

    async def fake_create(scfg):                  # noqa: ANN001, ANN202
        calls["n"] += 1
        eng._session = fresh
        eng._connect_ts.append(0.0)
        eng._events.on_status("info", "会话已建立（1ms）")

    eng._create_session = fake_create             # type: ignore[assignment]

    async def run() -> None:
        await eng._watchdog()
        assert eng._reconnect_task is not None, "没有安排重连"
        await asyncio.wait_for(eng._reconnect_task, timeout=5)

    asyncio.run(run())
    assert calls["n"] == 1, f"应恰好重建 1 次，实际 {calls['n']}"
    assert dead.closed, "旧会话没有被关掉"
    assert eng._session is fresh, "重连后会话没换新"
    assert fresh.is_alive
    warned = [m for lvl, m in statuses if lvl == "warn"]
    assert warned and "重连" in warned[0], f"应给用户明确提示，实际 {statuses}"
    assert any("已重连" in m for _lvl, m in statuses), f"缺少重连成功提示：{statuses}"
    print(f"  看门狗自动重连 OK（提示：{warned[0][:60]}…）")


def test_watchdog_ignores_healthy_session() -> None:
    """会话健康时不该误重连。"""
    from vlt.engine import Engine, EngineEvents

    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents())
    eng._session = _FakeSession(alive=True)
    asyncio.run(eng._watchdog())
    assert eng._reconnect_task is None, "会话是活的，不该触发重连"
    print("  健康会话不误重连 OK")


def test_black_box_dump_on_disconnect() -> None:
    """★ 断线时必须打「黑匣子」：静音占比 + 最近译文本 + 服务端事件序列。

    用户的要求是「先加日志把问题钉死，再决定要不要改行为」——
    服务端 `model repeat output happened` 掐断时，日志要能回答：
    ① 模型是不是在重复输出同一句？② 输入音频是不是长期静音？
    """
    import contextlib
    import io
    import time

    from vlt.engine import Engine, EngineEvents
    from vlt.session.base import TextDelta

    eng = Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                 events=EngineEvents())
    silent = b"\x00\x00" * 1600
    loud = b"\x00\x20" * 1600

    async def feed() -> None:
        for _ in range(30):
            await eng._proxy.send_audio(silent)
        for _ in range(2):
            await eng._proxy.send_audio(loud)
        for i in range(5):                      # 模拟模型重复吐同一句
            eng._on_text(TextDelta(confirmed="我在测试翻译功能", pending="",
                                   is_final=(i == 4), source="我在测试"))
        eng._on_audio(b"\x00\x01" * 240)

    asyncio.run(feed())
    eng._session_started_at = time.monotonic() - 147.0

    class DeadWithEvents(_FakeSession):
        def recent_events(self):                 # noqa: ANN201
            return [("response.audio_transcript.delta", 3.2), ("response.done", 1.1)]

    eng._session = DeadWithEvents(alive=False,
                                  reason="ConnectionClosedError: 1011 model repeat output happened")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(eng._watchdog())
        if eng._reconnect_task is not None:
            eng._reconnect_task.cancel()
    out = buf.getvalue()

    checks = [
        ("会话存活时间", "会话存活 147s" in out),
        ("静音占比", "静音 30 块" in out and "94%" in out),
        ("最近有效声音", "最近一次检测到有效声音" in out),
        ("输入音频总时长", "发送输入音频" in out),
        ("最近译文本(重复可辨)", out.count("我在测试翻译功能") >= 5),
        ("服务端事件序列", "response.done" in out),
    ]
    bad = [name for name, ok in checks if not ok]
    print(f"  黑匣子字段：{ {n: o for n, o in checks} }")
    assert not bad, f"黑匣子缺字段：{bad}\n实际输出：\n{out}"
    print("  断线黑匣子内容完整 OK")


if __name__ == "__main__":
    print("test_reconnect:")
    test_proxy_forwards_to_current_session()
    test_watchdog_reconnects_dead_session()
    test_watchdog_ignores_healthy_session()
    test_black_box_dump_on_disconnect()
    print("ALL PASSED")
