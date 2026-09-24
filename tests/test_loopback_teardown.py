"""回归测试：loopback 采集收尾时的**关闭顺序**——读线程必须先退出，才能关流。

## 真实事故（用户实测闪退，faulthandler 抓到现场）

    [loopback] 采集结束，共 681600 bytes ≈ 21s
    Windows fatal exception: access violation

    Current thread 0x0000c570 (most recent call first):
      File "...\\pyaudiowpatch\\__init__.py", line 640 in read     ← 崩在阻塞读里
      File "vlt\\engine.py", line 590 in reader                    ← 我们的读线程

    Thread 0x00007f8c (most recent call first):
      File "vlt\\engine.py", line 109 in stop                      ← 同时收尾线程在跑
      File "vlt\\gui.py", line 565 in _stop                        ← 用户点的「停止翻译」

根因：`reader()` 是 `while True` **死循环没有退出条件**，而主流程 finally 里直接
`stream.stop_stream() / close() / p.terminate()` —— 一个线程卡在阻塞的 `read()` 里，
另一个线程把流和 PortAudio 销毁了 → 访问违规。

（麦克风那条腿用 sounddevice 回调 API，由 PortAudio 自己管线程，所以只有 loopback 会崩；
而且要在**双向同时**下才会走到，正好是用户的用法。）

## 本测试怎么保证不再犯

用假的 pyaudiowpatch 记录**调用顺序**，断言：一旦关了流，就不能再出现 `read()`。
另外断言读线程真的退出了（不是靠 daemon 被进程结束带走）。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EVENTS: list[str] = []
# 关闭流的那一刻，如果读线程还活着，就把它标记出来 ——
# 这是本测试的核心不变式，且与调度时序无关（确定性抓到）。
READER_NAME = "vlt-loopback-reader"


def _reader_alive() -> bool:
    return any(t.name == READER_NAME for t in threading.enumerate())


class _FakeStream:
    """模拟真实 loopback 端点：先有数据，然后**变安静**（get_read_available 返回 0）。

    变安静这一步是关键——真实端点在没音频播放时就是这个状态，
    而阻塞式 read() 会在这里永久卡住（这正是崩溃的条件）。
    """

    def __init__(self, with_audio_calls: int = 8) -> None:
        self._polls = 0
        self._with_audio_calls = with_audio_calls

    def get_read_available(self) -> int:
        self._polls += 1
        return 4800 if self._polls <= self._with_audio_calls else 0

    def read(self, n, exception_on_overflow=False):   # noqa: ANN001
        EVENTS.append("read")
        time.sleep(0.02)                              # 模拟一次读
        return b"\x00" * (n * 2 * 2)                  # 立体声 16bit 静音

    def stop_stream(self) -> None:
        EVENTS.append("stop_stream_WITH_READER_ALIVE" if _reader_alive() else "stop_stream")

    def close(self) -> None:
        EVENTS.append("close_WITH_READER_ALIVE" if _reader_alive() else "close")


class _FakePyAudio:
    paInt16 = 8

    def open(self, **kw):  # noqa: ANN003, ANN201
        return _FakeStream()

    def terminate(self) -> None:
        EVENTS.append("terminate_WITH_READER_ALIVE" if _reader_alive() else "terminate")


class _FakeSession:
    def __init__(self) -> None:
        self.sent = 0

    async def send_audio(self, pcm: bytes) -> None:
        self.sent += len(pcm)


def test_reader_joins_before_stream_close() -> None:
    import vlt.engine as E

    EVENTS.clear()
    fake_p = _FakePyAudio()
    orig_pick = E.pick_loopback_device
    E.pick_loopback_device = lambda patterns=None: ((63, "Fake Loopback", 48000, 2), fake_p)
    try:
        session = _FakeSession()
        asyncio.run(E.run_loopback(session, None, seconds=0.4))
    finally:
        E.pick_loopback_device = orig_pick

    assert "stop_stream" in EVENTS or "stop_stream_WITH_READER_ALIVE" in EVENTS, \
        f"没有关闭流，采集可能没跑起来：{EVENTS}"

    # 核心不变式：关流/释放 PortAudio 时，读线程必须已经退出
    bad = [e for e in EVENTS if e.endswith("_WITH_READER_ALIVE")]
    assert not bad, (
        f"关闭流/释放 PortAudio 时读线程还活着（{bad}）—— 这正是访问违规的原因："
        f"一个线程卡在阻塞 read() 里，另一个线程把流销毁了。调用序列：{EVENTS}"
    )
    print(f"  teardown order OK（{len(EVENTS)} 次调用，关流前读线程已退出）")


def test_reader_thread_exits() -> None:
    import vlt.engine as E

    EVENTS.clear()
    fake_p = _FakePyAudio()
    orig_pick = E.pick_loopback_device
    E.pick_loopback_device = lambda patterns=None: ((63, "Fake Loopback", 48000, 2), fake_p)
    try:
        asyncio.run(E.run_loopback(_FakeSession(), None, seconds=0.3))
    finally:
        E.pick_loopback_device = orig_pick

    alive = [t.name for t in threading.enumerate() if t.name == READER_NAME]
    assert not alive, (
        "采集返回后读线程还活着 —— 它必须在关闭流之前被 join 掉，"
        "否则线程会在别人销毁流的同时继续 read()"
    )
    print("  reader thread joined OK（采集返回时读线程已退出）")


if __name__ == "__main__":
    print("test_loopback_teardown:")
    test_reader_joins_before_stream_close()
    test_reader_thread_exits()
    print("ALL PASSED")
