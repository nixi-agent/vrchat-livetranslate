"""回归测试：点「停止翻译」必须真的把采集停掉。

## 为什么要有这个测试（真实事故）

用户实测点击「停止翻译」报错：

    Exception in callback Engine._schedule_stop()
    AttributeError: 'Engine' object has no attribute '_async_stop'

`_schedule_stop()` 调了一个**从未实现**的 `self._async_stop()`。异常发生在
asyncio 的回调里，被默认异常处理器吞掉、只打一行——**功能坏了但测试全绿**。

## 为什么既有的测试抓不住

1. 异常不进 `on_status`，测试断言的是状态消息，看不见。
2. 断言 `engine.running` 也没用：`stop()` 无论如何都会把 `self._thread` 置 None。
3. 用 `pcm:` 音源测更没用：`_feed_pcm` 自己检查 `_stopping`，根本不走 `_async_stop`。
   真正会挂住的是 **mic / loopback 这种无限循环音源**。

所以本测试：**假造一个"无限循环、只认 stop_event"的采集器**（复刻真实采集器的契约），
然后抓住线程对象断言它**真的死了**。

## 离线可跑

同时替换 create_session 与 run_mic，不连网络、不需要音频设备。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vlt.engine as E            # noqa: E402
from vlt.config import AppConfig, Direction   # noqa: E402


class _FakeSession:
    """够用的假会话：只需要不报错地被启停。"""

    def __init__(self, cfg=None) -> None:
        self.closed = False

    async def start(self, on_text=None, on_audio=None, on_usage=None):
        self.on_text = on_text
        return None

    async def send_audio(self, pcm: bytes) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def tick(self) -> None:
        return None


async def _fake_mic(session, tele, seconds: float = 0.0, device_pattern=None,
                    device_name=None, stop_event=None) -> None:
    """假采集器：**复刻真实 mic 的契约**——无限循环，直到 stop_event 置位。

    ⚠️ 这行断言是测试的核心：引擎必须把 stop_event 传下来，否则采集永远停不下来。
    """
    import asyncio

    assert stop_event is not None, "引擎没把 stop_event 传给采集器 —— 停止路径必然失效"
    while not stop_event.is_set():
        await asyncio.sleep(0.05)


def _cfg() -> AppConfig:
    return AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en")},
        chatbox={}, merger={}, overlay={}, output={},
    )


def test_stop_actually_kills_capture_thread() -> None:
    original_session = E.create_session
    original_mic = E.run_mic
    E.create_session = lambda scfg: _FakeSession(scfg)
    E.run_mic = _fake_mic
    try:
        statuses: list[tuple[str, str]] = []
        events = E.EngineEvents(on_status=lambda lvl, msg: statuses.append((lvl, msg)))
        engine = E.Engine(cfg=_cfg(), direction="mine", source="mic",
                          sinks=set(), events=events, settle_s=30.0, dry_run=True)
        engine.start()
        time.sleep(1.2)                      # 让采集循环跑起来
        assert engine.running, "引擎没能启动"
        thread = engine._thread              # 抓住线程对象（stop() 之后引用会被清空）
        assert thread is not None and thread.is_alive()

        t0 = time.perf_counter()
        engine.stop(timeout=8.0)
        elapsed = time.perf_counter() - t0

        errors = [m for lvl, m in statuses if lvl == "error"]
        assert not thread.is_alive(), (
            "stop() 之后采集线程仍在运行 —— 停止路径失效"
            "（历史事故：_async_stop 未实现，AttributeError 被 asyncio 吞掉）"
            f" 状态消息={statuses}"
        )
        assert elapsed < 5.0, f"停止耗时 {elapsed:.1f}s，太慢（应收敛在 1~2 秒内）"
        assert not errors, f"停止过程中有错误：{errors}"
        print(f"  stop kills capture thread OK（耗时 {elapsed:.2f}s，线程已退出）")
    finally:
        E.create_session = original_session
        E.run_mic = original_mic


def test_stop_is_idempotent() -> None:
    """重复点停止不能抛异常。"""
    original_session = E.create_session
    original_mic = E.run_mic
    E.create_session = lambda scfg: _FakeSession(scfg)
    E.run_mic = _fake_mic
    try:
        engine = E.Engine(cfg=_cfg(), direction="mine", source="mic",
                          sinks=set(), events=E.EngineEvents(), settle_s=30.0, dry_run=True)
        engine.start()
        time.sleep(0.8)
        engine.stop(timeout=6.0)
        engine.stop(timeout=6.0)             # 第二次应当直接返回
        engine.stop(timeout=6.0)
        assert not engine.running
        print("  stop idempotent OK（连续调用三次无异常）")
    finally:
        E.create_session = original_session
        E.run_mic = original_mic


if __name__ == "__main__":
    print("test_engine_stop:")
    test_stop_actually_kills_capture_thread()
    test_stop_is_idempotent()
    print("ALL PASSED")
