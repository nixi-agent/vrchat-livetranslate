"""VirtualMic 纯函数/纯逻辑测试：不依赖真实音频设备。

## ⚠️ 打桩纪律（踩过 4 次，每次都动了用户的音频图）

任何会让引擎走到 `_setup_virtualmic` 的测试，都**必须同时桩住两个平台**：

    Windows 侧：`vlt.engine.pick_output_device` / `vlt.engine.VirtualMic`
    Linux  侧：`vlt.platform.open_audio_out`

只桩 Windows 侧时，Linux 分支会**绕过打桩**、真的拉起 `pw-loopback`
去创建虚拟声卡 —— 这会往用户的 PipeWire 图里塞节点、还可能污染
`default.configured.*` 元数据。统一用下面的 `_stub_audio_out()` 就不会漏。
（`vlt/platform/linux.py` 里还有一道「测试进程拒绝声明虚拟声卡」的防呆兜底。）
"""
from __future__ import annotations

import contextlib
import logging
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlt.output.virtualmic import (
    PwCatVirtualMic,
    VirtualMic,
    pick_output_device,
    resample_24k_mono_to_48k_stereo,
)


@contextlib.contextmanager
def _stub_audio_out(*, win_vm=None, win_pick=None, linux_factory=None):
    """一次性把两个平台的译音输出入口都换成假的。

    `linux_factory(cfg, on_status)` 要返回一个**未打开**的对象
    （引擎随后会自己调 `.open()`），返回 None 表示这条腿不可用。
    """
    import vlt.engine as E
    from vlt import platform

    orig = (E.pick_output_device, E.VirtualMic, platform.open_audio_out)
    if win_pick is not None:
        E.pick_output_device = win_pick
    if win_vm is not None:
        E.VirtualMic = win_vm
    if linux_factory is not None:
        platform.open_audio_out = linux_factory            # type: ignore[assignment]
    else:
        platform.open_audio_out = lambda _c, _s: None      # type: ignore[assignment]
    try:
        yield
    finally:
        E.pick_output_device, E.VirtualMic, platform.open_audio_out = orig


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
        chunks = [c for c, _e, _approval in vm._buf]
    first_chunk = chunks[0]
    assert first_chunk[0:1] == b"\xBB" or first_chunk[0:1] == b"\xCC", \
        f"最旧的整句应被丢弃，实际第一个是 {first_chunk[0:1]!r}"
    assert chunks[-1][0:1] == b"\xCC", "最新的数据必须保留"
    print("  buffer keeps newest OK（整句丢弃，最新保留）")


def test_no_device_no_crash():
    """没有可用输出设备时走 on_status('error', ...)、virtualmic 留 None、不抛异常。

    ⚠️ 两个平台要分别打桩，**绝不能让测试真的去建虚拟声卡**（那会动用户的音频图）：
      * Windows：桩在 `E.pick_output_device`（按设备名找不到 → None）
      * Linux  ：桩在 `platform.open_audio_out`（声明失败 → None）
    早先只桩了 Windows 那侧，加上 Linux 分支后测试会真的拉起 pw-loopback —— 已修。
    """
    statuses: list[tuple[str, str]] = []
    from vlt.config import AppConfig, Direction
    from vlt import platform
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

    def _fake_open(_audio_cfg, on_status):
        on_status("error", "没找到匹配的输出设备（回退链：nonexistent_device_xyz）"
                           "。虚拟声卡装好了吗？其余功能不受影响。")
        return None

    orig = platform.open_audio_out
    platform.open_audio_out = _fake_open            # type: ignore[assignment]
    try:
        engine = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(), events=events)
        engine._setup_virtualmic(cfg.output["audio"])
    finally:
        platform.open_audio_out = orig              # type: ignore[assignment]

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
    """★ 回归：译音输出设备**打开成功**那条分支必须跑通，且后续调用不因属性名写错而炸。

    真实事故（用户机器上有 VoiceMeeter 才会走到这条分支）：
    `_setup_virtualmic` 成功分支里多打了一行日志，用了不存在的属性
    `self._virtualmic.sample_rate`（真实属性叫 `_sample_rate`）→ AttributeError
    → 引擎报「运行错误」，**整条翻译腿直接死掉**（用户日志里连报三次）。

    我本机没装虚拟声卡，永远走不到这条分支，所以本地测试全绿、一到用户那儿就炸 ——
    因此必须用**假设备**把成功分支覆盖掉。

    ⚠️ 两个平台都要桩住（Windows 桩 `E.pick_output_device`/`E.VirtualMic`，
    Linux 桩 `platform.open_audio_out`）。**只桩一侧会让另一侧真去建虚拟声卡、
    动到用户的音频图** —— 这个坑踩过一次（测试真的拉起了 pw-loopback）。
    而且必须用**真实类的子类**、只换掉 `open()`：这样后续对它的每一处属性访问都走真对象，
    「属性名写错」这类 bug 才能立刻暴露（纯假对象会漏掉）。
    """
    from vlt.config import AppConfig, Direction
    from vlt.engine import Engine, EngineEvents
    import vlt.engine as E
    from vlt import platform

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

    # ★ 用**真实的类**做子类，只把 open() 换掉不真的开音频流
    from vlt.output.virtualmic import PwCatVirtualMic
    from vlt.output.virtualmic import VirtualMic as RealVirtualMic

    class PatchedVirtualMic(RealVirtualMic):
        def open(self) -> bool:
            self.opened = True
            return True

    class PatchedPwCat(PwCatVirtualMic):
        def open(self) -> bool:
            self.opened = True
            return True

    def _fake_open(_audio_cfg, _on_status):
        # 注意：PwCatVirtualMic 的构造**不启动任何进程**，只有 open() 才会 ——
        # 所以这里返回真对象也不会碰到用户的音频图。
        return PatchedPwCat("vlt_mic_sink", sample_rate=48000, buffer_ms=300,
                            max_buffer_ms=2000)

    orig_pick, orig_vm = E.pick_output_device, E.VirtualMic
    orig_open = platform.open_audio_out
    E.pick_output_device = lambda patterns=None: (  # noqa: ARG005
        11, "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)", 48000)
    E.VirtualMic = PatchedVirtualMic
    platform.open_audio_out = _fake_open            # type: ignore[assignment]
    try:
        engine = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                        events=EngineEvents(on_status=lambda lvl, msg: statuses.append((lvl, msg))))
        engine._setup_virtualmic(cfg.output["audio"])       # ← 不许抛异常
        vm = engine.virtualmic
        assert vm is not None, "设备可用时应该留下译音输出实例"
        assert getattr(vm, "opened", False), "没有调用 open()"
        # ★ 后续调用也要走一遍：属性名/接口写错在这里立刻炸（这正是当年那个 bug）
        vm.push(bytes(48000 * 4 // 10))                     # 100ms 立体声 s16
        vm.end_sentence()
        vm.close()
    finally:
        E.pick_output_device, E.VirtualMic = orig_pick, orig_vm
        platform.open_audio_out = orig_open         # type: ignore[assignment]

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
    from vlt import platform

    def _fake_open(_audio_cfg, on_status):
        # Linux 侧对应「声明虚拟声卡失败 / 打开写入端失败」——一样要优雅降级。
        on_status("error", "虚拟声卡声明失败 → 译音输出已禁用（其余功能不受影响）")
        return None

    orig_open = platform.open_audio_out
    platform.open_audio_out = _fake_open            # type: ignore[assignment]
    try:
        engine = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                        events=EngineEvents(on_status=lambda lvl, msg: None))
        engine._setup_virtualmic(cfg.output["audio"])
    finally:
        E.pick_output_device, E.VirtualMic = orig_pick, orig_vm
        platform.open_audio_out = orig_open         # type: ignore[assignment]
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

    joined = b"".join(c for c, _e, _approval in vm._buf)
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

    joined = b"".join(c for c, _e, _approval in vm._buf)
    assert joined.count(b"\x01") > 0, "正在播放的第一句被丢掉了 —— 会听到半句话"
    print(f"  正在播的那句没被丢 OK（第 1 句剩 {joined.count(bytes([1])) / (48000*4):.2f}s）")


def test_engine_marks_sentence_boundaries():
    """引擎必须在句子边界调用 end_sentence()（虚拟麦整句丢弃的依据）。"""
    from vlt.config import AppConfig, Direction
    from vlt.engine import Engine, EngineEvents
    from vlt.session.base import TextDelta
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

    class RecordingPwCat(PwCatVirtualMic):
        """Linux 侧的等价录制桩（构造不启动进程，只有 open() 才启动）。"""

        def __init__(self) -> None:
            super().__init__("vlt_mic_sink")
            self.marks = 0
            self.pushed = 0
            self.opens = 0

        def open(self) -> bool:
            self.opens += 1
            return True

        def end_sentence(self) -> None:
            self.marks += 1

        def push(self, pcm: bytes) -> None:  # noqa: ARG002
            self.pushed += 1

    with _stub_audio_out(win_vm=RecordingVM,
                         win_pick=lambda patterns=None: (11, "Fake", 48000),
                         linux_factory=lambda _c, _s: RecordingPwCat()):
        eng = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                     events=EngineEvents(on_status=lambda lvl, msg: None))
        eng._setup_virtualmic(cfg.output["audio"])
        vm = eng.virtualmic
        for _ in range(3):                       # 第一句：3 段音频 + 终版文本
            eng._on_audio(b"\x00\x01" * 240)
        eng._on_text(TextDelta(confirmed="你好", pending="", is_final=True, source=""))
        for _ in range(3):                       # 第二句：3 段音频
            eng._on_audio(b"\x00\x02" * 240)

    assert vm.pushed == 6, f"音频段数不对：{vm.pushed}"
    assert vm.marks == 1, f"句尾标记数不对：{vm.marks}（应在第二句起始处封一次）"
    print(f"  引擎在句子边界封句 OK（推入 {vm.pushed} 段，封句 {vm.marks} 次）")


def test_engine_uses_injected_audio_sink():
    """★ 注入 audio_sink（麦克风代理的 TranslatedSink）时：引擎**不自建** VirtualMic、
    **不 pick 设备**；停翻译（_cleanup）时**不 close** 注入的 sink（它归代理管）。

    这是「原声/译音一键切换」的地基：虚拟声卡输出流由代理独占持有，引擎只把译音 PCM
    灌进代理那条常驻流。若引擎误自建 / 误关，切换时就会出现「两条流写同一设备」或
    「停翻译把代理的输出流也一起关了」的事故 —— 用会炸的桩把这两条路都堵死来证明没走。
    """
    import asyncio
    from vlt.config import AppConfig, Direction
    from vlt.engine import Engine, EngineEvents

    class RecordingSink:
        def __init__(self) -> None:
            self.closed = 0
            self.pushed = 0
            self.device_name = "proxy-translated-sink"

        def open(self) -> bool:
            raise AssertionError("引擎不该 open 注入的 sink（它归代理管）")

        def push(self, pcm: bytes) -> None:  # noqa: ARG002
            self.pushed += 1

        def end_sentence(self) -> None:
            pass

        def close(self) -> None:
            self.closed += 1

    class BoomVM:
        def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
            raise AssertionError("注入 sink 时引擎不该自建 VirtualMic")

    def _boom_pick(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("注入 sink 时引擎不该 pick 输出设备")

    sink = RecordingSink()
    cfg = AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=True)},
        chatbox={}, merger={}, overlay={},
        output={"audio": {"enabled": True, "device": ["voicemeeter input"], "sample_rate": 48000}},
    )
    with _stub_audio_out(win_vm=BoomVM, win_pick=_boom_pick,
                         linux_factory=lambda _c, _s: None):
        eng = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                     events=EngineEvents(on_status=lambda lvl, msg: None),
                     audio_sink=sink)
        eng._setup_virtualmic(cfg.output["audio"])       # 注入路径：直接返回，不碰桩
        assert eng.virtualmic is sink, "应直接采用注入的 sink，不自建 VirtualMic"
        assert eng._owns_virtualmic is False, "注入的 sink 不归引擎所有"
        asyncio.run(eng._cleanup())                      # 停翻译收尾
        assert sink.closed == 0, "共享 sink 归代理管，引擎停翻译时绝不能 close 它"
        assert eng.virtualmic is None and eng._owns_virtualmic is False
    print("  注入 audio_sink：不自建 VirtualMic、不 pick 设备、停翻译不 close OK")


def test_fallback_computed_in_auto_chain_branch():
    """★ 「自动回退链」档（device_name 为空）挑中的虚拟声卡，也必须带同名回落候选。

    真机事故（2026-10-06 测试者）：配置里 `device_name` 为空（界面下拉停在「自动检测」）时，
    `_make_audio_out` 走 `pick_output_device(patterns)` 那条分支，而 `fallbacks` 一直是空列表
    → 虚拟声卡的 WASAPI 端点打不开（真机必现 `-9999 … WdmSyncIoctl … GLE = 0x490`）时
    **一次候选都不试**，直接把「译音输出」整条腿判死；指定设备名时反而正常回落。

    判据：回退链分支也要算候选（桩成同名设备 #31），且首选 #40 打不开时能真的回到 #31、
    译音输出仍然活着。
    """
    from vlt.config import AppConfig, Direction
    from vlt.engine import Engine, EngineEvents
    import vlt.engine as E
    from vlt.output.virtualmic import VirtualMic as RealVirtualMic
    from vlt import platform

    if not platform.IS_WINDOWS:
        # `Engine._make_audio_out` 在 Linux 上是**早返回**（走 `platform.open_audio_out`
        # 运行时声明 PipeWire 节点），而「同名回落候选」是 **Windows 独占**分支的补丁 ——
        # 只有 WASAPI 端点才会 `-9999` 打不开、需要按 host API 回落。
        # 所以这里照仓库口径**显式跳过、不静默**（上游这条用例没有平台守卫，
        # 在 Linux CI 上会假红；已在本机复现 upstream/main 原样即失败）。
        print("  [skip] 非 Windows：同名回落候选是 Windows 独占分支"
              "（Linux 走运行时声明节点，没有 host API 回落这回事）")
        return

    cfg = AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=True)},
        chatbox={}, merger={}, overlay={},
        # ⚠️ 故意不写 device_name —— 复刻真机那份配置（只有回退链）
        output={"audio": {"enabled": True, "device": ["voicemeeter input"],
                          "sample_rate": 48000}},
    )

    statuses: list[tuple[str, str]] = []
    asked: list[tuple[str, object]] = []

    class RecordingVirtualMic(RealVirtualMic):
        def open(self) -> bool:
            # 复刻真机：首选（WASAPI 那条）打不开，同名回落能开
            for i, dev in enumerate([self._device_index, *self._device_fallbacks]):
                if i == 0:
                    self._on_status("warn", f"虚拟声卡 #{dev} 打不开：-9999")
                    continue
                self._open_device_index = dev
                self._on_status("warn", f"虚拟声卡 #{self._device_index} 打不开，"
                                        f"已回落到同名设备 #{dev}")
                return True
            self._on_status("error", f"打开虚拟声卡失败（#{self._device_index}）")
            return False

    orig_pick, orig_vm = E.pick_output_device, E.VirtualMic
    orig_fb = platform.output_device_fallbacks

    def _fb(name, exclude=None):
        asked.append((name, exclude))
        return [31]                       # 同名输出设备在别的 host API 下的条目

    E.pick_output_device = lambda patterns=None: (  # noqa: ARG005
        40, "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)", 48000)
    E.VirtualMic = RecordingVirtualMic
    platform.output_device_fallbacks = _fb           # type: ignore[assignment]
    try:
        engine = Engine(cfg=cfg, direction="mine", source="mic", sinks=set(),
                        events=EngineEvents(on_status=lambda lvl, msg: statuses.append((lvl, msg))))
        engine._setup_virtualmic(cfg.output["audio"])
    finally:
        E.pick_output_device, E.VirtualMic = orig_pick, orig_vm
        platform.output_device_fallbacks = orig_fb   # type: ignore[assignment]

    assert asked, "回退链分支没有去算同名回落候选（这就是那个 bug）"
    assert asked[0][1] == 40, f"算候选时应排除首选设备本身：{asked[0]!r}"
    vm = engine.virtualmic
    assert vm is not None, "首选打不开但同名回落可用时，译音输出不该被禁用"
    assert vm._open_device_index == 31, f"没有回落到 #31：{vm._open_device_index}"
    assert any("已回落到同名设备 #31" in m for _, m in statuses), statuses
    print("  回退链档也算同名回落候选（#40 打不开 → 回落 #31，译音输出仍可用）OK")


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
    test_engine_uses_injected_audio_sink()
    test_fallback_computed_in_auto_chain_branch()
    print("ALL PASSED")
