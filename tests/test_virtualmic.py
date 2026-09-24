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


def test_buffer_overflow_drops_oldest():
    """超过 max_buffer_ms 时丢最旧的，保留最新。"""
    statuses: list[tuple[str, str]] = []
    vm = VirtualMic(
        device_index=0, device_name="fake",
        sample_rate=48000, buffer_ms=10, max_buffer_ms=50,
        on_status=lambda lvl, msg: statuses.append((lvl, msg)),
    )
    bytes_per_ms = 48000 * 2 * 2 / 1000
    chunk_ms = 30
    chunk_bytes = int(chunk_ms * bytes_per_ms)
    chunk = b"\x01" * chunk_bytes

    vm.push(chunk)
    vm.push(chunk)
    vm.push(chunk)

    with vm._lock:
        total_bytes = vm._buf_bytes
        max_allowed = int(50 * bytes_per_ms)
        assert total_bytes <= max_allowed, f"缓冲 {total_bytes} 超过上限 {max_allowed}"
        assert len(vm._buf) >= 1
    print(f"  buffer overflow OK (buf_chunks={len(vm._buf)}, bytes={total_bytes}, max={max_allowed})")


def test_buffer_keeps_newest():
    """确认丢的是最旧的块，保留的是最新的。"""
    vm = VirtualMic(
        device_index=0, device_name="fake",
        sample_rate=48000, buffer_ms=10, max_buffer_ms=100,
    )
    bytes_per_ms = 48000 * 2 * 2 / 1000
    chunk_bytes = int(60 * bytes_per_ms)

    old_chunk = b"\xAA" * chunk_bytes
    new_chunk = b"\xBB" * chunk_bytes
    vm.push(old_chunk)
    vm.push(new_chunk)

    with vm._lock:
        first = vm._buf[0]
        last = vm._buf[-1]
    assert first[0:1] == b"\xBB", "最旧的应该被丢弃，第一个应该是新数据"
    print("  buffer keeps newest OK")


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


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print("test_virtualmic:")
    test_resample_byte_count()
    test_resample_fidelity()
    test_pick_output_device_match()
    test_pick_output_device_no_match()
    test_buffer_overflow_drops_oldest()
    test_buffer_keeps_newest()
    test_no_device_no_crash()
    test_prime_timeout_short_audio()
    test_resample_edge_cases()
    print("ALL PASSED")
