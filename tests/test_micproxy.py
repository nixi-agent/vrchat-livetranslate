"""麦克风代理（MicProxy）纯逻辑测试：绝不碰真实音频设备。

## ⚠️ 打桩纪律（与 test_virtualmic.py 同一条）

`MicProxy.start()` 会**真的打开音频流**（虚拟声卡输出 + 麦克风采集）。本文件里
调用 `start()` 的只有下面三条，其余用例都只在**构造出来、没 start()** 的
MicProxy 上验证纯逻辑（重采样 / 环形缓冲 / 档位切换 / 译音垫片 / 缓冲参数）——
构造函数不开任何流，所以这些用例在没装虚拟声卡的机器上也照跑。

三条涉及 start() 的用例，各自都把该桩的东西桩住：

  · `test_start_close_stubbed`：假 `sounddevice` + 假采集后端 + 假设备枚举
    + **放行测试进程守卫**（守卫是最后一道保险，正常用例要显式放行才算「两侧都打桩」）；
  · `test_start_no_device_degrades_gracefully`：设备枚举返回 None（同样放行守卫）；
  · `test_start_refused_in_test_process`：★ **故意不放行守卫**，并且把设备枚举换成
    「一调就抛」的桩 —— 用来钉住「守卫必须在碰任何设备之前就拒绝」。

三者都在 finally 里还原，全程不触碰用户的音频图。
"""
from __future__ import annotations

import asyncio
import struct
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlt.output.micproxy import (          # noqa: E402
    MODE_PASSTHROUGH,
    MODE_TRANSLATED,
    MicProxy,
    TranslatedSink,
    _Ring,
    _TranslatedBuffer,
    resample_to_48k_stereo,
)

_BPF = 48000 * 2 * 2 / 1000        # 48kHz 立体声 s16le：每毫秒字节数 = 192


# ---------------------------------------------------------------- 重采样

def test_resample_16k_to_48k_dc_byte_exact():
    """16k 单声道 → 48k 立体声：长度 = 3× 采样点 × 2 声道；直流信号逐样本恒定、左右相同。"""
    n = 1600                                          # 100ms @16k
    pcm = struct.pack(f"<{n}h", *([500] * n))
    out = resample_to_48k_stereo(pcm, 16000, 1)
    assert len(out) == n * 3 * 2 * 2, f"期望 {n*3*2*2} 字节，实际 {len(out)}"
    samples = struct.unpack(f"<{len(out)//2}h", out)
    assert all(s == 500 for s in samples), "直流信号重采样后应逐样本恒为 500"
    assert samples[0::2] == samples[1::2], "单声道复制成立体声：左右声道必须一致"
    print(f"  16k→48k 立体声重采样 OK（{len(pcm)}B → {len(out)}B，直流逐样本恒定）")


def test_resample_48k_mono_to_stereo():
    """源已是 48k：不重采样，只做单声道 → 立体声复制（长度翻倍）。"""
    n = 100
    pcm = struct.pack(f"<{n}h", *([i for i in range(n)]))
    out = resample_to_48k_stereo(pcm, 48000, 1)
    assert len(out) == n * 2 * 2, f"期望 {n*4} 字节，实际 {len(out)}"
    samples = struct.unpack(f"<{len(out)//2}h", out)
    assert samples[0::2] == tuple(range(n)), "左声道应原样保留输入样本"
    assert samples[0::2] == samples[1::2], "右声道 = 左声道"
    print("  48k 单声道 → 立体声复制 OK（长度翻倍、左右一致）")


def test_resample_stereo_input_downmix():
    """多声道输入先下混成单声道（逐帧取均值）再升立体声。"""
    pcm = struct.pack("<4h", 100, 300, 100, 300)       # 2 帧立体声：L=100 R=300
    out = resample_to_48k_stereo(pcm, 48000, 2)
    samples = struct.unpack(f"<{len(out)//2}h", out)
    assert all(s == 200 for s in samples), f"下混均值应为 200，实际 {samples}"
    print("  立体声输入下混 OK（(100+300)/2 = 200）")


def test_resample_edge_cases():
    """空输入、奇数字节、单样本都不能崩，且输出永远是 4 字节对齐（立体声帧）。"""
    assert resample_to_48k_stereo(b"", 16000, 1) == b""
    odd = resample_to_48k_stereo(b"\x01\x00\x02", 16000, 1)   # 末尾半个样本被丢弃
    assert len(odd) % 4 == 0, f"输出应 4 字节对齐，实际 {len(odd)}"
    one = resample_to_48k_stereo(struct.pack("<h", 7), 16000, 1)
    assert len(one) % 4 == 0 and len(one) > 0
    print(f"  重采样边界 OK（空 / 奇数={len(odd)}B / 单样本={len(one)}B）")


# ---------------------------------------------------------------- 直通环形缓冲

def test_ring_drops_oldest_latency_bounded():
    """环形缓冲超限丢**最旧**：延迟不累积，最新数据一定在。"""
    r = _Ring(cap_bytes=100)
    for i in range(1, 11):                              # 每块 40B，帽 100B（值 1..10，避开静音 0）
        r.push(bytes([i]) * 40)
    retained = r._n
    assert retained <= r._cap, f"滞留 {retained}B 超过帽 {r._cap}B —— 延迟会累积"
    data, _under = r.drain(1000)
    assert bytes([10]) * 40 in data, "最新那块必须还在"
    assert bytes([1]) not in data, "最旧那块应已被丢弃"
    assert bytes([8]) not in data, "超出容量的旧数据都应被丢弃"
    print(f"  环形缓冲丢最旧 OK（滞留 {retained}B ≤ 帽 {r._cap}B，最新保留、最旧丢弃）")


def test_ring_drain_pads_silence_and_flags_underrun():
    """drain 不足时补静音并置欠载标志；够时精确排空、不欠载。"""
    r = _Ring(cap_bytes=1000)
    r.push(b"X" * 10)
    data, under = r.drain(20)
    assert data == b"X" * 10 + b"\x00" * 10, f"应补静音到 20B，实际 {data!r}"
    assert under is True, "数据不足应报欠载"

    r2 = _Ring(cap_bytes=1000)
    r2.push(b"Y" * 30)
    d2, u2 = r2.drain(20)
    assert d2 == b"Y" * 20 and u2 is False, "够取时不应欠载"
    assert r2._n == 10, f"应剩 10B，实际 {r2._n}"
    print("  环形缓冲补静音 / 欠载标志 OK")


def test_ring_set_cap_trims():
    """set_cap 缩小容量后立即裁到帽内（只留最新的）。"""
    r = _Ring(cap_bytes=1000)
    for i in range(1, 6):
        r.push(bytes([i]) * 100)                        # n=500（值 1..5，避开静音 0）
    r.set_cap(150)
    assert r._n <= 150, f"裁剪后应 ≤ 150B，实际 {r._n}"
    data, _ = r.drain(1000)
    assert bytes([5]) in data and bytes([1]) not in data, "应只留最新的块"
    print(f"  环形缓冲 set_cap 裁剪 OK（→ {r._n}B）")


# ---------------------------------------------------------------- 译音抖动缓冲（借用 VirtualMic）

def test_translated_buffer_is_buffer_only():
    """_TranslatedBuffer.open() 返回 True 但**绝不开真实音频流**（流由 MicProxy 独占）。"""
    tb = _TranslatedBuffer(device_index=0, device_name="x", sample_rate=48000,
                           buffer_ms=100, max_buffer_ms=2000)
    assert tb.open() is True
    assert tb._stream is None, "buffer-only：不该开任何 PortAudio 流"
    print("  译音缓冲 buffer-only OK（open() 不开流）")


def test_translated_buffer_prime_drain_reset():
    """攒够起播线才出数据；不足则返回 None；reset 清空并复位起播状态。"""
    tb = _TranslatedBuffer(device_index=0, device_name="x", sample_rate=48000,
                           buffer_ms=100, max_buffer_ms=2000)
    tb.push(b"\x02" * 1000)                             # < 100ms 起播线（19200B）
    assert tb.drain_block(3840) is None, "还没攒够起播线，应返回 None（补静音）"

    tb.push(b"\x01" * 20000)                            # 攒够 → 起播
    data = tb.drain_block(3840)
    assert data is not None and len(data) == 3840, "起播后应排空出一整块"

    tb.reset()
    assert tb._buf_bytes == 0 and not tb._primed and not tb._head_started, \
        "reset 应清空缓冲并复位起播状态"
    assert tb.drain_block(3840) is None, "reset 后应回到未起播"
    print("  译音缓冲起播 / 排空 / reset OK")


def test_translated_buffer_set_buffer_ms():
    """set_buffer_ms 改起播线（译音缓冲 Spinbox 的落点）。"""
    tb = _TranslatedBuffer(device_index=0, device_name="x", sample_rate=48000,
                           buffer_ms=300, max_buffer_ms=2000)
    tb.set_buffer_ms(500)
    assert tb._buffer_ms == 500
    print("  译音缓冲 set_buffer_ms OK")


# ---------------------------------------------------------------- TranslatedSink 垫片

def _make_proxy_with_buffer(**cfg_kw) -> MicProxy:
    """构造一个**未 start()** 的 MicProxy，并手动挂上译音缓冲（start() 才会建的东西）。"""
    audio_cfg = {"sample_rate": 48000, "buffer_ms": 100, "max_buffer_ms": 2000,
                 "proxy": {"enabled": True, "passthrough_buffer_ms": 150}}
    audio_cfg.update(cfg_kw)
    p = MicProxy(audio_cfg=audio_cfg)
    p._translated = _TranslatedBuffer(device_index=11, device_name="FakeCard",
                                      sample_rate=48000, buffer_ms=100, max_buffer_ms=2000)
    p._out_device = (11, "FakeCard")
    return p


def test_translated_sink_push_and_end_sentence():
    """引擎只认这个鸭子类型：push/end_sentence 落进代理的译音缓冲；close() 是空操作。"""
    p = _make_proxy_with_buffer()
    sink = p.translated_sink
    assert isinstance(sink, TranslatedSink)
    assert sink.device_name == "FakeCard"
    assert isinstance(sink.opened, bool)

    sink.push(b"\x03" * 20000)
    assert p._translated._buf_bytes == 20000, "push 应落进译音缓冲"
    sink.end_sentence()
    assert p._translated._buf[-1][1] is True, "end_sentence 应给最后一块打句尾标记"

    sink.close()                                        # 归代理管：空操作
    assert p._translated._buf_bytes == 20000, "sink.close() 绝不能清掉代理的缓冲"
    print("  TranslatedSink 垫片 push/end_sentence/close OK")


# ---------------------------------------------------------------- 档位切换

def test_set_mode_rejects_translated_when_inactive():
    """翻译没运行时切译音档：拒绝、留痕、保持原声档。"""
    statuses: list[tuple[str, str]] = []
    p = MicProxy(audio_cfg={}, on_status=lambda lvl, msg, **kw: statuses.append((lvl, msg)))
    p._translated = _TranslatedBuffer(device_index=0, device_name="x", sample_rate=48000,
                                      buffer_ms=100, max_buffer_ms=2000)
    assert p.set_mode(MODE_TRANSLATED) is False, "未激活翻译时不该允许切译音档"
    assert p.mode == MODE_PASSTHROUGH, "应保持原声档"
    assert any(lvl == "warn" for lvl, _ in statuses), "拒绝切换应经 on_status 留痕"
    print("  译音档在未翻译时被拒绝 OK（保持原声 + 留痕）")


def test_set_mode_clears_both_buffers():
    """任何方向切换都清空**两侧**缓冲：立刻生效、不念旧账。"""
    p = _make_proxy_with_buffer()
    p.set_translation_active(True)

    p._ring.push(b"R" * 1000)
    p._translated.push(b"\x04" * 20000)
    p._translated.end_sentence()
    assert p.set_mode(MODE_TRANSLATED) is True
    assert p.mode == MODE_TRANSLATED
    assert p._ring._n == 0, "切到译音档应清空直通缓冲（防切回时爆陈旧原声）"
    assert p._translated._buf_bytes == 0 and not p._translated._primed, \
        "切到译音档应清空译音缓冲并复位起播（听到的是新内容）"

    p._ring.push(b"R" * 500)
    p._translated.push(b"\x05" * 20000)
    assert p.set_mode(MODE_PASSTHROUGH) is True
    assert p.mode == MODE_PASSTHROUGH
    assert p._translated._buf_bytes == 0, "切回原声档应丢掉未说完的半句译音"
    print("  档位切换清空两侧缓冲 OK")


def test_set_translation_active_false_forces_passthrough():
    """翻译停止（set_translation_active(False)）→ 强制回落原声档。"""
    p = _make_proxy_with_buffer()
    p.set_translation_active(True)
    assert p.set_mode(MODE_TRANSLATED) is True
    assert p.mode == MODE_TRANSLATED
    p.set_translation_active(False)
    assert p.mode == MODE_PASSTHROUGH, "翻译停止后必须弹回原声档"
    print("  翻译停止强制回落原声档 OK")


def test_set_mode_rejects_unknown():
    """未知档位名：拒绝、不改当前档。"""
    p = MicProxy(audio_cfg={})
    assert p.set_mode("bogus") is False
    assert p.mode == MODE_PASSTHROUGH
    print("  未知档位名被拒绝 OK")


# ---------------------------------------------------------------- 输出回调

def test_out_callback_passthrough_and_underrun():
    """原声档：从环形缓冲排空；空了补静音并累计欠载。

    ⚠️ 收到第一块麦克风数据**之前**不计欠载（启动/换麦瞬态，见 `_got_mic_data`）——
    先钉「无数据不计数」，置位后再钉「空了计数」。
    """
    p = MicProxy(audio_cfg={})
    p._mode = MODE_PASSTHROUGH

    out0 = bytearray(960 * 2 * 2)
    p._out_callback(out0, 960, None, None)              # 还没有任何麦克风数据
    assert p.underrun_count == 0, "收到第一块之前不该计欠载"

    frame = struct.pack("<h", 500) * 2                  # 一帧立体声 DC=500
    p._ring.push(frame * 960)                           # 960 帧 = 一个输出块
    p._got_mic_data = True                              # 模拟已收到第一块麦克风数据
    out = bytearray(960 * 2 * 2)
    p._out_callback(out, 960, None, None)
    assert bytes(out) == frame * 960, "应原样排出直通数据"
    assert p.underrun_count == 0

    out2 = bytearray(960 * 2 * 2)
    p._out_callback(out2, 960, None, None)              # 缓冲已空
    assert bytes(out2) == b"\x00" * (960 * 2 * 2), "空了应补静音"
    assert p.underrun_count == 1, "欠载应计数"
    print("  输出回调（原声档）排空 / 补静音 / 欠载计数 OK")


def test_out_callback_translated():
    """译音档：从译音抖动缓冲排空。"""
    p = _make_proxy_with_buffer()
    p._mode = MODE_TRANSLATED
    p._translated.push(b"\x05" * 20000)                 # 越过 100ms 起播线（19200B）才会起播
    p._translated.end_sentence()
    out = bytearray(960 * 2 * 2)
    p._out_callback(out, 960, None, None)
    assert out[:10] == b"\x05" * 10, "译音档应排出译音数据"
    print("  输出回调（译音档）排空 OK")


# ---------------------------------------------------------------- 缓冲参数

def test_reopen_with_updates_params_idempotent():
    """reopen_with 只改软件参数（环形缓冲帽 / 译音起播线），幂等，不重开流。"""
    p = _make_proxy_with_buffer()
    assert p._passthrough_ms == 150
    assert p._ring._cap == int(150 * _BPF)

    p.reopen_with(passthrough_ms=200, translated_buffer_ms=500)
    assert p._passthrough_ms == 200
    assert p._ring._cap == int(200 * _BPF), "环形缓冲帽应随直通缓冲更新"
    assert p._translated._buffer_ms == 500, "译音起播线应更新"

    p.reopen_with(passthrough_ms=200, translated_buffer_ms=500)   # 幂等
    assert p._ring._cap == int(200 * _BPF) and p._translated._buffer_ms == 500

    p.reopen_with()                                     # 不给参数 → 不变
    assert p._passthrough_ms == 200 and p._translated._buffer_ms == 500
    print("  reopen_with 更新缓冲参数（幂等 / 无需重开流）OK")


# ---------------------------------------------------------------- 启停（打桩，绝不碰真设备）

class _FakeOutStream:
    instances: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.started = self.stopped = self.closed = False
        _FakeOutStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakeSource:
    """假麦克风采集源：先吐给定的几块，之后返回 None（模拟「还活着但没数据」）。"""

    def __init__(self, chunks, *, rate=16000, channels=1) -> None:
        self.rate = rate
        self.channels = channels
        self._chunks = list(chunks)
        self.closed = False

    async def read(self, timeout: float = 1.0):
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(0.01)
        return None

    def close(self) -> None:
        self.closed = True


class _FakeCaptureBackend:
    def __init__(self, source) -> None:
        self._src = source
        self.last_rate = None

    def open_mic(self, name, *, rate, channels, blocksize):   # noqa: ARG002
        self.last_rate = rate                    # 记录代理请求的采集率（回归用）
        return self._src


def test_start_close_stubbed():
    """★ 集成：start() 打开虚拟声卡输出流 + 起麦克风直通线程；数据流进环形缓冲；close() 幂等收尾。

    全程打桩（假 sounddevice / 假采集后端 / 假设备枚举），不触碰用户音频图。
    """
    from vlt.output import micproxy as MP
    from vlt import platform

    _FakeOutStream.instances.clear()
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.RawOutputStream = lambda **kw: _FakeOutStream(**kw)      # type: ignore[attr-defined]
    fake_sd.query_devices = lambda *a, **k: {                        # type: ignore[attr-defined]
        "name": "FakeCard", "default_samplerate": 48000}
    orig_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake_sd

    dc = struct.pack("<h", 500) * 1600               # 100ms DC=500 @16k 单声道
    src = _FakeSource([dc], rate=16000, channels=1)
    orig_cb = platform.capture_backend
    orig_pick = MP.pick_output_device
    # ★ 必须**两侧都打桩**：`start()` 第一件事就是测试进程守卫（见 test_micproxy.py 头部的
    #   打桩纪律与 `test_start_refused_in_test_process`）。这里放行它，好走完整条打桩链路。
    orig_guard = MP._test_process_guard
    MP._test_process_guard = lambda *a, **k: None        # noqa: ARG005
    backend = _FakeCaptureBackend(src)
    platform.capture_backend = lambda: backend           # type: ignore[assignment]
    MP.pick_output_device = lambda patterns=None: (11, "FakeCard", 48000)  # noqa: ARG005

    p = MicProxy(audio_cfg={"sample_rate": 48000, "buffer_ms": 100, "max_buffer_ms": 2000,
                            "device": ["voicemeeter input"],
                            "proxy": {"enabled": True, "passthrough_buffer_ms": 150}})
    try:
        assert p.start() is True, "打桩环境下 start() 应成功"
        assert p.opened is True
        assert p.start() is True, "重复 start() 应幂等返回已开状态"
        assert _FakeOutStream.instances, "输出流没被打开"
        assert _FakeOutStream.instances[0].started, "输出流没 start()"
        # ★ 代理原声直通必须以**虚拟声卡输出率**（48k）采集，不能降到 16k（否则丢 8kHz 以上）
        assert backend.last_rate == 48000, f"代理请求了 {backend.last_rate}，应为 48000（全带宽）"

        # 轮询等麦克风线程把直通数据推进环形缓冲（避免固定 sleep 在慢机上翻车）
        frame48 = struct.pack("<h", 500) * 2
        got = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            out = bytearray(960 * 2 * 2)
            p._out_callback(out, 960, None, None)
            if bytes(out[:4]) == frame48:
                got = out
                break
            time.sleep(0.02)
        assert got is not None, "麦克风直通数据没进环形缓冲（重采样/推流链路断了）"
    finally:
        p.close()
        p.close()                                     # 幂等
        platform.capture_backend = orig_cb            # type: ignore[assignment]
        MP.pick_output_device = orig_pick
        MP._test_process_guard = orig_guard
        if orig_sd is not None:
            sys.modules["sounddevice"] = orig_sd
        else:
            sys.modules.pop("sounddevice", None)

    assert src.closed, "麦克风源没被关闭（收尾顺序不对）"
    assert p.opened is False
    assert _FakeOutStream.instances[0].closed, "输出流没被关闭"
    print("  start()/close() 打桩集成 OK（输出流开、直通数据进缓冲、幂等收尾）")


def test_start_no_device_degrades_gracefully():
    """没有可用虚拟声卡：start() 返回 False、留痕、不抛异常、不开流。"""
    from vlt.output import micproxy as MP

    statuses: list[tuple[str, str]] = []
    orig_pick = MP.pick_output_device
    orig_guard = MP._test_process_guard
    MP._test_process_guard = lambda *a, **k: None        # noqa: ARG005
    MP.pick_output_device = lambda patterns=None: None       # noqa: ARG005
    p = MicProxy(audio_cfg={"device": ["nonexistent_xyz"]},
                 on_status=lambda lvl, msg, **kw: statuses.append((lvl, msg)))
    try:
        assert p.start() is False, "没设备时 start() 应返回 False"
    finally:
        MP.pick_output_device = orig_pick
        MP._test_process_guard = orig_guard
    assert p.opened is False
    assert any(lvl == "error" for lvl, _ in statuses), "应经 on_status 报 error"
    print("  无虚拟声卡时优雅降级 OK（返回 False + 留痕，不抛异常）")


def test_start_refused_in_test_process():
    """★ 测试进程守卫：`start()` 必须**在碰任何设备之前**就拒绝。

    本仓库实测踩过四次「单元测试绕过打桩、真的拉起了设备」；Windows 上尤其危险 ——
    用户机器上通常真的装着 VoiceMeeter / VB-Cable，一跑测试就会打开他们的虚拟声卡、
    实时把麦克风直通出去。所以这条守卫是**最后一道保险**，必须有用例钉住它。
    """
    from vlt.output import micproxy as MP

    statuses: list[tuple[str, str]] = []
    orig_pick = MP.pick_output_device

    def _boom(patterns=None):                              # noqa: ARG001
        raise AssertionError("守卫没拦住：竟然去枚举设备了")

    MP.pick_output_device = _boom
    p = MicProxy(audio_cfg={"device": ["voicemeeter input"]},
                 on_status=lambda lvl, msg, **kw: statuses.append((lvl, msg)))
    try:
        assert p.start() is False, "测试进程里 start() 必须被拒"
    finally:
        MP.pick_output_device = orig_pick
    assert p.opened is False, "被拒后不许标记为已开"
    assert p._out_stream is None, "被拒后不许留下输出流句柄"
    assert p._thread is None, "被拒后不许起麦克风线程"
    assert any("测试进程" in msg for _, msg in statuses), "被拒要留痕（说清原因）"
    print("  测试进程守卫：start() 在任何设备操作之前被拒 + 留痕 OK")


def test_pick_output_device_computes_fallbacks_in_auto_chain():
    """★ 代理侧同一条纪律：回退链档（无 device_name）挑中的设备也要算同名回落候选。

    与 `engine._make_audio_out` 同因（2026-10-06 真机：档位落到回退链后首选端点打不开，
    没有候选就只试一次、代理这条腿直接不可用）。
    """
    from vlt.output import micproxy as MP
    from vlt import platform

    asked: list[tuple[str, object]] = []
    orig_pick, orig_fb = MP.pick_output_device, platform.output_device_fallbacks

    def _fb(name, exclude=None):
        asked.append((name, exclude))
        return [31]

    MP.pick_output_device = lambda patterns=None: (40, "FakeCard", 48000)  # noqa: ARG005
    platform.output_device_fallbacks = _fb            # type: ignore[assignment]
    try:
        p = MicProxy(audio_cfg={"device": ["voicemeeter input"]})
        got = p._pick_output_device()
    finally:
        MP.pick_output_device = orig_pick
        platform.output_device_fallbacks = orig_fb    # type: ignore[assignment]

    assert got is not None, "有设备时应返回四元组"
    idx, name, rate, fallbacks = got
    assert (idx, name) == (40, "FakeCard"), got
    assert fallbacks == [31], f"回退链档没算同名回落候选：{fallbacks!r}"
    assert asked and asked[0][1] == 40, f"算候选时应排除首选本身：{asked!r}"
    print("  代理侧回退链档也算同名回落候选 OK")


def test_reopen_mic_swaps_device_without_touching_output():
    """★ reopen_mic：换直通麦克风**只重启采集线程** —— 虚拟声卡输出流与 translated_sink 不变。

    这是「改麦克风下拉后直通即时生效」的底座：不关虚拟声卡 → 不打扰 VRChat 那侧、不影响
    运行中引擎手里的桥接。全程打桩，不碰真实音频。覆盖：首次按名开、换名重开、同名幂等、
    回落到默认（None）、输出流一个都没重开/没关。
    """
    from vlt.output import micproxy as MP
    from vlt import platform

    class _RecBackend:
        def __init__(self) -> None:
            self.names: list = []
            self.sources: list = []

        def open_mic(self, name, *, rate, channels, blocksize):   # noqa: ARG002
            self.names.append(name)
            src = _FakeSource([], rate=16000, channels=1)
            self.sources.append(src)
            return src

    _FakeOutStream.instances.clear()
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.RawOutputStream = lambda **kw: _FakeOutStream(**kw)      # type: ignore[attr-defined]
    fake_sd.query_devices = lambda *a, **k: {                        # type: ignore[attr-defined]
        "name": "FakeCard", "default_samplerate": 48000}
    orig_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake_sd

    rec = _RecBackend()
    orig_cb = platform.capture_backend
    orig_pick = MP.pick_output_device
    orig_guard = MP._test_process_guard
    MP._test_process_guard = lambda *a, **k: None                    # noqa: ARG005
    platform.capture_backend = lambda: rec                           # type: ignore[assignment]
    MP.pick_output_device = lambda patterns=None: (11, "FakeCard", 48000)  # noqa: ARG005

    p = MicProxy(audio_cfg={"sample_rate": 48000, "buffer_ms": 100, "max_buffer_ms": 2000,
                            "device": ["voicemeeter input"],
                            "proxy": {"enabled": True, "passthrough_buffer_ms": 150}},
                 mic_name="Mic X")

    def _wait_for(pred, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    try:
        assert p.start() is True, "打桩环境下 start() 应成功"
        assert _wait_for(lambda: rec.names[:1] == ["Mic X"]), \
            f"首次没按 Mic X 打开：{rec.names}"
        n_out = len(_FakeOutStream.instances)
        sink_before = p.translated_sink

        p.reopen_mic("Mic Y")
        assert _wait_for(lambda: rec.names[-1:] == ["Mic Y"]), \
            f"reopen 后没按 Mic Y 打开：{rec.names}"
        assert p.opened is True
        assert p.translated_sink is sink_before, "换麦不该换 translated_sink（引擎桥接必须不变）"
        assert len(_FakeOutStream.instances) == n_out, "换麦不该重开虚拟声卡输出流"
        assert not _FakeOutStream.instances[0].closed, "虚拟声卡输出流被误关"

        # 同名幂等：不该再开一次设备
        n_before = len(rec.names)
        p.reopen_mic("Mic Y")
        time.sleep(0.08)
        assert len(rec.names) == n_before, f"同名 reopen_mic 不该重新打开：{rec.names}"

        # 回落到系统默认
        p.reopen_mic(None)
        assert _wait_for(lambda: rec.names[-1:] == [None]), \
            f"reopen_mic(None) 没回到默认设备：{rec.names}"
    finally:
        p.close()
        platform.capture_backend = orig_cb                           # type: ignore[assignment]
        MP.pick_output_device = orig_pick
        MP._test_process_guard = orig_guard
        if orig_sd is not None:
            sys.modules["sounddevice"] = orig_sd
        else:
            sys.modules.pop("sounddevice", None)
    print("  reopen_mic：换采集设备、输出流与 translated_sink 不变、同名幂等、可回默认 OK")


def test_mic_reference_db_none_when_stale() -> None:
    """`mic_window_s` 必须**接到运行路径**：参考过期后 `mic_reference_db` 取数返回 None。

    为什么单列一条：`MicReference.valid()` 自己的语义有意义（`test_level_match.py` 覆盖了），
    但「代理把过期参考当新鲜用」是**接线**问题 —— 实测把 `valid(now)` 去掉（直接 `db()`）
    时，`tests/test_micproxy.py` 与 `tests/test_level_match.py` 都仍全绿，只有这条会红。
    """
    p = _make_proxy_with_buffer(level={"mode": "follow_mic", "mic_min_blocks": 3,
                                      "mic_window_s": 5.0})
    pcm = struct.pack("<2400h", *([3000] * 2400))        # 16kHz 单声道 150ms 说话块
    t0 = time.monotonic() - 60.0                        # 「60 秒前说过话」
    for _ in range(4):
        p._mic_ref.feed(pcm, 16000, 1, now=t0)
    assert p._mic_ref.db() is not None, "前提：样本够 → db() 必须给值"
    assert p._mic_ref.valid(t0 + 1.0) is True, "前提：刚说完 → 有效"
    assert p.mic_reference_db is None, "参考过期后必须返回 None（不许拿旧值当新鲜）"

    p._mic_ref.feed(pcm, 16000, 1, now=time.monotonic())  # 重新说话
    assert p.mic_reference_db is not None, "重新有话后应立刻恢复可用"
    print("  mic_reference_db：有效期内有值 / 过期返回 None / 重新说话恢复 OK")


if __name__ == "__main__":
    print("test_micproxy:")
    test_resample_16k_to_48k_dc_byte_exact()
    test_resample_48k_mono_to_stereo()
    test_resample_stereo_input_downmix()
    test_resample_edge_cases()
    test_ring_drops_oldest_latency_bounded()
    test_ring_drain_pads_silence_and_flags_underrun()
    test_ring_set_cap_trims()
    test_translated_buffer_is_buffer_only()
    test_translated_buffer_prime_drain_reset()
    test_translated_buffer_set_buffer_ms()
    test_translated_sink_push_and_end_sentence()
    test_set_mode_rejects_translated_when_inactive()
    test_set_mode_clears_both_buffers()
    test_set_translation_active_false_forces_passthrough()
    test_set_mode_rejects_unknown()
    test_out_callback_passthrough_and_underrun()
    test_out_callback_translated()
    test_reopen_with_updates_params_idempotent()
    test_start_close_stubbed()
    test_start_no_device_degrades_gracefully()
    test_start_refused_in_test_process()
    test_pick_output_device_computes_fallbacks_in_auto_chain()
    test_reopen_mic_swaps_device_without_touching_output()
    test_mic_reference_db_none_when_stale()
    print("ALL PASSED")
