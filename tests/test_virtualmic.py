"""VirtualMic 纯函数/纯逻辑测试：不依赖真实音频设备。"""
from __future__ import annotations

import logging
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlt.output.virtualmic import (
    VirtualMic,
    pick_output_device,
    resample_24k_mono_to_48k_stereo,
)


def test_resample_byte_count():
    """输出样本数 == 输入 × 2；字节数 == 输出样本数 × 2 声道 × 2 字节。"""
    n_in = 1000
    pcm = struct.pack(f"<{n_in}h", *([1000] * n_in))
    out = resample_24k_mono_to_48k_stereo(pcm)
    out_samples_per_ch = n_in * 2
    expected_bytes = out_samples_per_ch * 2 * 2
    assert len(out) == expected_bytes, f"期望 {expected_bytes} 字节，实际 {len(out)}"
    assert len(out) == len(pcm) * 4
    print(f"  resample byte count OK (in={len(pcm)} → out={len(out)})")


def test_resample_fidelity():
    """喂正弦波，输出峰值接近输入峰值。"""
    n = 4800
    freq = 440
    samples = [int(20000 * math.sin(2 * math.pi * freq * i / 24000)) for i in range(n)]
    pcm = struct.pack(f"<{n}h", *samples)
    out = resample_24k_mono_to_48k_stereo(pcm)
    out_n = len(out) // 2
    out_samples = struct.unpack(f"<{out_n}h", out)
    peak_in = max(abs(s) for s in samples)
    peak_out = max(abs(s) for s in out_samples)
    ratio = peak_out / peak_in if peak_in else 0
    assert 0.95 <= ratio <= 1.05, f"峰值比 {ratio:.3f} 偏离过大（in={peak_in}, out={peak_out}）"
    print(f"  resample fidelity OK (peak_in={peak_in}, peak_out={peak_out}, ratio={ratio:.3f})")


def test_pick_output_device_match():
    """按回退链命中第一个匹配项。"""
    fake_devices = [
        {"name": "Speakers (Realtek)", "max_output_channels": 2, "default_samplerate": 48000},
        {"name": "VoiceMeeter Input (VB-Audio)", "max_output_channels": 2, "default_samplerate": 48000},
        {"name": "VoiceMeeter Aux Input", "max_output_channels": 2, "default_samplerate": 44100},
    ]
    result = pick_output_device(
        patterns=["voicemeeter input", "voicemeeter aux input"],
        devices=fake_devices,
    )
    assert result is not None
    idx, name, rate = result
    assert idx == 1
    assert "voicemeeter input" in name.lower()
    print(f"  pick device match OK (#{idx} {name})")


def test_pick_output_device_no_match():
    """匹配不到时返回 None。"""
    fake_devices = [
        {"name": "Speakers (Realtek)", "max_output_channels": 2, "default_samplerate": 48000},
    ]
    result = pick_output_device(patterns=["voicemeeter input"], devices=fake_devices)
    assert result is None
    print("  pick device no-match OK (None)")


def test_buffer_overflow_no_marker_bounded_by_hard_ceiling():
    """没有句尾标记时（异常情况）按硬上限兜底，绝不无限增长。

    正常路径见 test_overflow_drops_whole_sentences_only：有标记就只丢整句。
    这里模拟"句尾标记一直没来"，缓冲最多涨到 4× max_buffer_ms 就必须开始丢。
    """
    vm = VirtualMic(
        device_index=0, device_name="fake",
        sample_rate=48000, buffer_ms=10, max_buffer_ms=50,
    )
    bytes_per_ms = 48000 * 2 * 2 / 1000
    chunk = b"\x01" * int(30 * bytes_per_ms)      # 每段 30ms

    for _ in range(20):                            # 推 600ms（上限 50ms、硬上限 200ms）
        vm.push(chunk)

    with vm._lock:
        total = vm._buf_bytes
        hard = int(50 * bytes_per_ms) * 4
        assert total <= hard, f"缓冲 {total} 超过硬上限 {hard}（会无限增长）"
        assert len(vm._buf) >= 1
    print(f"  无句尾标记时按硬上限兜底 OK（buf={total/(48000*4):.0f}ms ≤ {hard/(48000*4):.0f}ms）")


def test_buffer_keeps_newest():
    """有句尾标记时：丢的是最旧的**整句**，最新的那句一定留着。"""
    vm = VirtualMic(
        device_index=0, device_name="fake",
        sample_rate=48000, buffer_ms=10, max_buffer_ms=100,
    )
    bytes_per_ms = 48000 * 2 * 2 / 1000
    chunk_bytes = int(60 * bytes_per_ms)

    vm.push(b"\xAA" * chunk_bytes)
    vm.end_sentence()                              # 第一句（60ms，整句）
    vm.push(b"\xBB" * chunk_bytes)
    vm.end_sentence()                              # 第二句（60ms，整句）
    vm.push(b"\xCC" * chunk_bytes)                 # 第三句（未封口）

    with vm._lock:
        chunks = [c for c, _e in vm._buf]
        bounds = None
    first_chunk = chunks[0]
    assert first_chunk[0:1] == b"\xBB" or first_chunk[0:1] == b"\xCC", \
        f"最旧的整句应被丢弃，实际第一个是 {first_chunk[0:1]!r}"
    assert chunks[-1][0:1] == b"\xCC", "最新的数据必须保留"
    print("  buffer keeps newest OK（整句丢弃，最新保留）")


def test_no_device_no_crash():
    """没有设备时走 on_status('error', ...) 且进程正常退出。"""
    statuses: list[tuple[str, str]] = []
    from vlt.config import AppConfig, Direction
    cfg = AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                       "workspace_id": "", "reconnect_backoff": [1], "max_new_sessions_per_minute": 10,
                       "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=True)},
        chatbox={}, merger={}, overlay={},
        output={"audio": {"enabled": True, "device": ["nonexistent_device_xyz"],
                          "sample_rate": 48000, "buffer_ms": 300, "max_buffer_ms": 2000}},
    )
    from vlt.engine import Engine, EngineEvents
    events = EngineEvents(
        on_status=lambda lvl, msg: statuses.append((lvl, msg)),
    )
    engine = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(), events=events)
    engine._setup_virtualmic(cfg.output["audio"])
    assert engine.virtualmic is None
    errors = [msg for lvl, msg in statuses if lvl == "error"]
    assert len(errors) >= 1, f"应该有 error 状态，实际：{statuses}"
    assert "nonexistent_device_xyz" in errors[0] or "没找到" in errors[0]
    print(f"  no-device graceful OK (error: {errors[0][:80]})")


def test_prime_timeout_short_audio():
    """短译音兜底：整段音频短于 buffer_ms 时，停更超时也必须起播，否则永远不出声。

    回归自真实缺口：原实现只在"攒够 buffer_ms"时置 _primed，
    若一段译音总量不足 buffer_ms（例如很短的一句），回调会永远输出静音。
    """
    from vlt.output.virtualmic import PRIME_TIMEOUT_S
    vm = VirtualMic(device_index=0, device_name="fake",
                    sample_rate=48000, buffer_ms=300, max_buffer_ms=2000)
    bytes_per_ms = 48000 * 2 * 2 / 1000
    short = b"\x11\x22" * int(100 * bytes_per_ms / 2)     # 只有 100ms < buffer_ms
    vm.push(short)
    assert not vm._primed, "只推了 100ms，还没到 300ms 起播线，此时不该起播"

    # 模拟"数据已经停更"（把上次推送时间往前拨）
    with vm._lock:
        vm._last_push_ts -= (PRIME_TIMEOUT_S + 0.2)

    out = bytearray(3840)
    vm._audio_callback(out, 960, None, None)              # 960 帧 @48k = 20ms
    assert vm._primed, "停更超时后必须起播，否则短译音永远卡在缓冲里"
    assert out[:4] == b"\x11\x22\x11\x22", f"应该听到真实数据而不是静音，实际 {out[:4]!r}"
    print("  prime on idle timeout OK（短译音不会卡死）")


def test_resample_edge_cases():
    """边界：单样本、奇数长度不能崩，字节比仍为 4x。"""
    from vlt.output.virtualmic import resample_24k_mono_to_48k_stereo
    one = resample_24k_mono_to_48k_stereo(b"\x01\x00")
    assert len(one) == 8, f"单样本应输出 8 字节（2 倍 × 双声道 × 2 字节），实际 {len(one)}"
    assert one[:4] == one[4:], "左右声道应相同"
    odd = resample_24k_mono_to_48k_stereo(b"\x01\x00\x02")     # 奇数长度，末尾半个样本应被丢弃
    assert len(odd) == len(one), f"奇数长度应丢弃半个样本，实际 {len(odd)}"
    print("  resample edge cases OK（单样本/奇数长度）")


def test_setup_virtualmic_success_path():
    """★ 回归：译音输出设备**打开成功**那条分支必须跑通。

    真实事故（用户机器上有 VoiceMeeter 才会走到这条分支）：
    `_setup_virtualmic` 成功分支里多打了一行日志，用了不存在的属性
    `self._virtualmic.sample_rate`（真实属性叫 `_sample_rate`）→ AttributeError
    → 引擎报「运行错误」，**整条翻译腿直接死掉**（用户日志里连报三次）。

    我本机没装虚拟声卡，永远走不到这条分支，所以本地测试全绿、一到用户那儿就炸 ——
    因此必须用**假设备**把成功分支覆盖掉。
    """
    from vlt.config import AppConfig, Direction
    from vlt.engine import Engine, EngineEvents
    import vlt.engine as E

    statuses: list[tuple[str, str]] = []
    cfg = AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=True)},
        chatbox={}, merger={}, overlay={},
        output={"audio": {"enabled": True, "device": ["voicemeeter input"],
                          "device_name": "", "sample_rate": 48000,
                          "buffer_ms": 300, "max_buffer_ms": 2000}},
    )

    # ★ 用**真实的 VirtualMic 子类**，只把 open() 换掉不真的开音频流：
    # 这样引擎里对它的每一处属性访问都会走真对象，属性名写错立刻暴露。
    from vlt.output.virtualmic import VirtualMic as RealVirtualMic

    class PatchedVirtualMic(RealVirtualMic):
        def open(self) -> bool:
            self.opened = True
            return True

    orig_pick, orig_vm = E.pick_output_device, E.VirtualMic
    E.pick_output_device = lambda patterns=None: (  # noqa: ARG005
        11, "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)", 48000)
    E.VirtualMic = PatchedVirtualMic
    try:
        engine = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                        events=EngineEvents(on_status=lambda lvl, msg: statuses.append((lvl, msg))))
        engine._setup_virtualmic(cfg.output["audio"])       # ← 不许抛异常
    finally:
        E.pick_output_device, E.VirtualMic = orig_pick, orig_vm

    assert engine.virtualmic is not None, "设备可用时应该留下 VirtualMic 实例"
    assert getattr(engine.virtualmic, "opened", False), "没有调用 open()"
    assert isinstance(engine.virtualmic, RealVirtualMic)
    print(f"  虚拟声卡打开成功分支不抛异常 OK（状态：{[m[:40] for _, m in statuses]}）")


def test_setup_virtualmic_open_failure():
    """打开失败要优雅降级：留 None + 打印原因，不许抛异常拖垮引擎。"""
    from vlt.config import AppConfig, Direction
    from vlt.engine import Engine, EngineEvents
    import vlt.engine as E
    from vlt.output.virtualmic import VirtualMic as RealVirtualMic

    cfg = AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=True)},
        chatbox={}, merger={}, overlay={},
        output={"audio": {"enabled": True, "device": ["voicemeeter input"], "sample_rate": 48000}},
    )

    class FailVirtualMic(RealVirtualMic):
        def open(self) -> bool:
            return False

    orig_pick, orig_vm = E.pick_output_device, E.VirtualMic
    E.pick_output_device = lambda patterns=None: (11, "Fake Virtual Card", 48000)  # noqa: ARG005
    E.VirtualMic = FailVirtualMic
    try:
        engine = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                        events=EngineEvents(on_status=lambda lvl, msg: None))
        engine._setup_virtualmic(cfg.output["audio"])
    finally:
        E.pick_output_device, E.VirtualMic = orig_pick, orig_vm
    assert engine.virtualmic is None, "打开失败应清成 None"
    print("  虚拟声卡打开失败 → 优雅降级 OK")


def test_overflow_drops_whole_sentences_only():
    """★ 回归：缓冲超限时只丢**整句**，绝不从句子中间切断。

    用户实测：连续说多段话时「上一句 TTS 还没说完就切到了下一句」——
    原因是原来 push() 从队首一个个 chunk 丢。
    """
    from vlt.output.virtualmic import VirtualMic

    vm = VirtualMic(device_index=0, device_name="x", sample_rate=48000,
                    buffer_ms=100, max_buffer_ms=500)
    SENT = int(48000 * 2 * 2 * 1.0)          # 每句 1 秒
    for tag in (1, 2, 3, 4, 5):
        vm.push(bytes([tag]) * SENT)
        vm.end_sentence()

    joined = b"".join(c for c, _e in vm._buf)
    assert joined, "全被丢光了（应该还留着最新的）"
    for tag in (1, 2, 3, 4, 5):
        n = joined.count(bytes([tag]))
        assert n in (0, SENT), f"第 {tag} 句被切成了 {n}/{SENT} 字节 —— 绝不能从句中切断"
    kept = [t for t in (1, 2, 3, 4, 5) if joined.count(bytes([t])) == SENT]
    assert kept[-1] == 5, f"最新的那句必须留着，实际留下 {kept}"
    print(f"  超限只丢整句 OK（留下 {kept}，共 {len(joined) / (48000 * 4):.1f}s）")


def test_playing_sentence_not_dropped():
    """正在播放的那一句绝不能被丢（否则就是「说到一半被切」）。"""
    from vlt.output.virtualmic import VirtualMic

    vm = VirtualMic(device_index=0, device_name="x", sample_rate=48000,
                    buffer_ms=100, max_buffer_ms=500)
    SENT = int(48000 * 2 * 2 * 1.0)
    vm.push(bytes([1]) * SENT)
    vm.end_sentence()

    chunk = bytearray(4 * 2 * 2)             # 模拟播放回调取走 4 帧
    vm._audio_callback(chunk, 4, None, None)
    assert vm._head_started, "没标记成正在播放"

    for tag in (2, 3, 4, 5):                 # 再灌 4 秒，逼出丢弃
        vm.push(bytes([tag]) * SENT)
        vm.end_sentence()

    joined = b"".join(c for c, _e in vm._buf)
    assert joined.count(b"\x01") > 0, "正在播放的第一句被丢掉了 —— 会听到半句话"
    print(f"  正在播的那句没被丢 OK（第 1 句剩 {joined.count(bytes([1])) / (48000*4):.2f}s）")


def test_engine_marks_sentence_boundaries():
    """引擎必须在句子边界调用 end_sentence()（虚拟麦整句丢弃的依据）。"""
    from vlt.config import AppConfig, Direction
    from vlt.engine import Engine, EngineEvents
    from vlt.session.base import TextDelta
    import vlt.engine as E
    from vlt.output.virtualmic import VirtualMic as RealVirtualMic

    class RecordingVM(RealVirtualMic):
        def __init__(self, **kw):  # noqa: ANN003
            super().__init__(**kw)
            self.marks = 0
            self.pushed = 0
            self.opens = 0

        def open(self) -> bool:
            self.opens += 1
            return True

        def end_sentence(self) -> None:
            self.marks += 1

        def push(self, pcm: bytes) -> None:
            self.pushed += 1

    cfg = AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=True)},
        chatbox={}, merger={}, overlay={},
        output={"audio": {"enabled": True, "device": ["voicemeeter input"], "sample_rate": 48000}},
    )
    orig_pick, orig_vm = E.pick_output_device, E.VirtualMic
    E.pick_output_device = lambda patterns=None: (11, "Fake", 48000)  # noqa: ARG005
    E.VirtualMic = RecordingVM
    try:
        eng = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                     events=EngineEvents(on_status=lambda lvl, msg: None))
        eng._setup_virtualmic(cfg.output["audio"])
        vm = eng.virtualmic
        for _ in range(3):                       # 第一句：3 段音频 + 终版文本
            eng._on_audio(b"\x00\x01" * 240)
        eng._on_text(TextDelta(confirmed="你好", pending="", is_final=True, source=""))
        for _ in range(3):                       # 第二句：3 段音频
            eng._on_audio(b"\x00\x02" * 240)
    finally:
        E.pick_output_device, E.VirtualMic = orig_pick, orig_vm

    assert vm.pushed == 6, f"音频段数不对：{vm.pushed}"
    assert vm.marks == 1, f"句尾标记数不对：{vm.marks}（应在第二句起始处封一次）"
    print(f"  引擎在句子边界封句 OK（推入 {vm.pushed} 段，封句 {vm.marks} 次）")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print("test_virtualmic:")
    test_resample_byte_count()
    test_resample_fidelity()
    test_pick_output_device_match()
    test_pick_output_device_no_match()
    test_buffer_overflow_no_marker_bounded_by_hard_ceiling()
    test_buffer_keeps_newest()
    test_no_device_no_crash()
    test_prime_timeout_short_audio()
    test_resample_edge_cases()
    test_setup_virtualmic_success_path()
    test_setup_virtualmic_open_failure()
    test_overflow_drops_whole_sentences_only()
    test_playing_sentence_not_dropped()
    test_engine_marks_sentence_boundaries()
    print("ALL PASSED")
