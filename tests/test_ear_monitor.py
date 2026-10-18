#!/usr/bin/env python
"""耳返（sidetone）离线验收。

## 要钉住的行为

1. **音量**：0% 全静音、50% 幅度减半、100% 原样；缓冲里已有的旧音频也按新音量放。
2. **缓冲**：推进来的 PCM 超过上限时**丢最旧的**；欠载补零（不拖引擎线程）。
3. **失败口径**：设备打不开 / 平台不支持 → 只禁用耳返，返回 False，不抛、不影响代理。
4. **接线**：耳返必须从**播放侧** tap（`MicProxy._drain_translated()`）—— 它拿到的是实际
   发给虚拟声卡的那批字节、同一条时间轴；`TranslatedSink.push()` **不许**喂耳返
   （那里是 TTS 突发入口，一个 delta 常有几百 ms，会被 120ms 监听缓冲截断）。
5. **热重开**：界面改开关/设备 → `restart_monitor()` 只重开这一路，不动虚拟声卡。
6. **回路策略**：耳返与采集端点同设备 + 端点模式 → 自动改 process；
   process 不可用 → 关掉耳返并 warn（绝不静默留一个会自我回译的回路）。

全程离线：假 sounddevice / 假代理 / 假设备表，不打开任何真实音频设备。

跑法：.venv/Scripts/python.exe tests/test_ear_monitor.py
"""
from __future__ import annotations

import queue
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.earmonitor import BLOCK_FRAMES, EarMonitor  # noqa: E402


def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, orig)


class _FakeStream:
    def __init__(self, on_start=None):     # noqa: ANN001
        self.stopped = False
        self.closed = False
        self._on_start = on_start

    def start(self):
        if self._on_start:
            self._on_start()

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeMon:
    """替身：只记录 push / 音量 / 开关，不开设备。"""

    def __init__(self, **kw):             # noqa: ANN003
        self.kw = kw
        self.got: list[bytes] = []
        self.volumes: list[float] = []
        self.closed = False

    def open(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def push(self, pcm: bytes) -> None:
        self.got.append(pcm)

    def set_volume(self, v: float) -> None:
        self.volumes.append(float(v))


# ---------------------------------------------------------------- ① 音量 / 缓冲

def test_volume_math() -> None:
    def render(volume: float, samples: int = BLOCK_FRAMES, fill: int = 1000):
        m = EarMonitor(volume=volume)
        m._opened = True                                   # noqa: SLF001 — 跳过设备
        frame = int(fill).to_bytes(2, "little", signed=True) * 2
        m._buf.extend(frame * samples)                     # noqa: SLF001
        out = np.zeros((samples, 2), dtype=np.int16)
        m._callback(out, samples, None, None)               # noqa: SLF001
        return out

    assert int(render(100).max()) == 1000, "100% 必须原样"
    assert int(render(50).max()) == 500, "50% 幅度减半"
    assert int(render(0).max()) == 0, "0% 必须全静音"
    assert int(render(0).min()) == 0
    print("  音量：0% 静音 / 50% 减半 / 100% 原样 OK")


def test_ring_drops_oldest_and_pads_silence() -> None:
    m = EarMonitor(volume=100)
    m._opened = True                                       # noqa: SLF001
    cap = m._cap_bytes                                     # noqa: SLF001
    loud = (1234).to_bytes(2, "little", signed=True) * 2
    m.push(loud * (cap // len(loud) + 1000))
    assert len(m._buf) == cap, f"缓冲必须被裁到上限：{len(m._buf)} != {cap}"   # noqa: SLF001

    out = np.full((BLOCK_FRAMES, 2), 7, dtype=np.int16)    # 故意预先填非零
    m._buf.clear()                                         # noqa: SLF001
    m._callback(out, BLOCK_FRAMES, None, None)             # noqa: SLF001
    assert not out.any(), "欠载必须补零（不能留上一帧的残音）"
    print("  缓冲：超限丢最旧 + 欠载补零 OK")


# ---------------------------------------------------------------- ② 失败口径

def test_open_failure_is_contained() -> None:
    warnings: list = []
    m = EarMonitor(device_name="Nope", on_status=lambda lvl, msg: warnings.append((lvl, msg)))
    fake_sd = types.SimpleNamespace(
        OutputStream=lambda **kw: (_ for _ in ()).throw(RuntimeError("-9999 boom")),  # noqa: ARG005
        query_devices=lambda *a, **k: {"name": "x"})
    old_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake_sd
    from vlt import platform
    r1 = _patch(platform, "IS_WINDOWS", True)
    try:
        assert m.open() is False, "设备打不开必须返回 False"
    finally:
        r1()
        if old_sd is None:
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = old_sd
    assert warnings and warnings[0][0] == "warn", warnings
    assert m.opened is False
    print("  设备打不开：返回 False + warn，不抛 OK")


def test_non_windows_is_disabled() -> None:
    from vlt import platform
    m = EarMonitor(on_status=lambda *a: None)
    r = _patch(platform, "IS_WINDOWS", False)
    try:
        assert m.open() is False
    finally:
        r()
    print("  非 Windows：明确禁用（不是静默无声）OK")


# ---------------------------------------------------------------- ③ 接线 / 热重开

class _FakeBuf:
    """译音播放缓冲替身：按剧本吐块；`None` = 未起播/不足（生产里补静音）。"""

    def __init__(self, blocks: list[bytes]) -> None:
        self.blocks = list(blocks)
        self.pushed: list[bytes] = []

    def push(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    def drain_block(self, need: int) -> bytes | None:
        if not self.blocks:
            return None
        d = self.blocks.pop(0)
        assert len(d) == need, f"剧本块长不对：{len(d)} != {need}"
        return d


def test_push_does_not_feed_monitor() -> None:
    """push 只负责「进缓冲」；在那里 tap 耳返会被 120ms 容量截断（真机症状的根因）。"""
    from vlt.output.micproxy import MicProxy

    proxy = MicProxy(audio_cfg={"monitor": {"enabled": False}})
    buf = _FakeBuf([])
    mon = _FakeMon()
    proxy._translated = buf                             # noqa: SLF001
    proxy._monitor = mon                                # noqa: SLF001
    burst = b"\x01\x00\x02\x00" * 72000                 # 1.5s 一次性到达（真实 TTS delta 形态）
    proxy.translated_sink.push(burst)
    assert buf.pushed == [burst], "push 仍必须把数据推进译音缓冲"
    assert mon.got == [], f"push 处不许喂耳返（会被截断）：{mon.got!r}"
    print("  TranslatedSink.push 不再 tap 耳返（只进缓冲）OK")


def test_playout_tap_feeds_monitor_same_bytes() -> None:
    """播放侧 tap：交出去的字节 == 耳返收到的字节（同一批、同一时刻）。"""
    from vlt.output.micproxy import MicProxy

    need = 3840                                          # 20ms / 48k / 2ch / int16
    chunk = b"\x33\x00" * (need // 2)
    proxy = MicProxy(audio_cfg={"monitor": {"enabled": False}})
    proxy._translated = _FakeBuf([chunk])               # noqa: SLF001
    mon = _FakeMon()
    proxy._monitor = mon                                # noqa: SLF001

    got = proxy._drain_translated(need)                 # noqa: SLF001
    assert got == chunk, "交出去的必须就是缓冲里那一块"
    assert mon.got == [chunk], f"耳返必须收到同一块：{mon.got!r}"
    print("  播放侧 tap：耳返字节 == 实际播出字节 OK")


def test_silence_is_not_pushed_to_monitor() -> None:
    """未起播/不足时补静音，但**不喂**耳返（别把静音堆进监听缓冲、凭空加延迟）。"""
    from vlt.output.micproxy import MicProxy

    need = 3840
    proxy = MicProxy(audio_cfg={"monitor": {"enabled": False}})
    proxy._translated = _FakeBuf([])                    # noqa: SLF001
    mon = _FakeMon()
    proxy._monitor = mon                                # noqa: SLF001
    assert proxy._drain_translated(need) == b"\x00" * need   # noqa: SLF001
    assert mon.got == [], mon.got
    print("  补静音不进耳返 OK")


def test_burst_arrival_never_truncates_monitor() -> None:
    """回归（真机症状「上一句没播完就跳到下一句」）：TTS 整两句突发到达 + 实时排空，
    耳返收到的块必须与播出去的逐块一致 —— 不许丢正在播的那一句。"""
    from vlt.output.micproxy import MicProxy

    need = 3840
    sent_a = [b"\x11\x00" * (need // 2) for _ in range(75)]     # 1.5s = 75 × 20ms
    sent_b = [b"\x22\x00" * (need // 2) for _ in range(75)]
    proxy = MicProxy(audio_cfg={"monitor": {"enabled": False}})
    proxy._translated = _FakeBuf(sent_a + sent_b)              # noqa: SLF001
    mon = _FakeMon()
    proxy._monitor = mon                                       # noqa: SLF001

    # 突发：两句一次性推进来（push 不再触达耳返）
    proxy.translated_sink.push(b"".join(sent_a + sent_b))
    assert mon.got == [], "突发入队本身不该产生监听音频"

    played = [proxy._drain_translated(need) for _ in range(150)]   # noqa: SLF001
    assert played == sent_a + sent_b, "播放内容必须完整、有序"
    assert mon.got == played, ("耳返必须逐块等于播出去的数据（旧实现在这里会丢句子）："
                              f"{len(mon.got)} vs {len(played)} 块")
    print("  突发到达 + 实时排空：耳返与游戏听到的逐块一致 OK")


def test_proxy_restart_monitor() -> None:
    import vlt.output.micproxy as mp

    proxy = mp.MicProxy(audio_cfg={"monitor": {"enabled": False}})
    proxy._opened = True                               # noqa: SLF001 — 模拟代理已启动
    created: list[_FakeMon] = []

    def factory(**kw):                                 # noqa: ANN003
        m = _FakeMon(**kw)
        created.append(m)
        return m

    r = _patch(mp, "EarMonitor", factory)
    try:
        assert proxy.restart_monitor(enabled=True, device_name="Headset", volume=40) is True
        assert created and created[0].kw["device_name"] == "Headset"
        assert created[0].kw["volume"] == 40
        proxy.set_monitor_volume(55)
        assert created[0].volumes[-1] == 55
        assert proxy.restart_monitor(enabled=False, device_name="", volume=40) is False
        assert created[0].closed is True, "关掉时必须 close 耳返"
    finally:
        r()
    print("  restart_monitor：热重开 / 关掉都只动耳返这一路 OK")


# ---------------------------------------------------------------- ④ 回路策略

def test_conflict_detection() -> None:
    import vlt.engine as E

    r1 = _patch(E, "_default_output_name", lambda: "Speakers")
    r2 = _patch(E, "resolve_device_name", lambda name, kind: 5 if name == "Speakers" else None)
    r3 = _patch(E, "_output_name_of", lambda idx: "Speakers")
    r4 = _patch(E, "pick_loopback_target",
                lambda name=None: E.LoopbackTarget(id="9", name="Speakers [Loopback]",
                                                   sample_rate=48000, channels=2))
    try:
        assert E.monitor_capture_conflict({"enabled": True, "device_name": ""},
                                          {"loopback_device": ""}) is True, "都自动 = 同一默认输出"
        assert E.monitor_capture_conflict({"enabled": True, "device_name": "Speakers"},
                                          {"loopback_device": "Speakers"}) is True
    finally:
        r4()
        r3()
        r2()
        r1()
    print("  同设备判定：自动/自动、同名（带 [Loopback] 后缀）都判为冲突 OK")


def test_effective_mode_upgrades_to_process() -> None:
    import vlt.engine as E
    from vlt import platform

    cap = {"loopback_mode": "endpoint"}
    mon = {"enabled": True, "device_name": ""}
    r1 = _patch(E, "monitor_capture_conflict", lambda m, c: True)
    r2 = _patch(platform, "process_loopback_available", lambda: (True, "build 26200"))
    try:
        assert E.effective_loopback_mode(cap, mon) == "process"
    finally:
        r2()
        r1()
    r1 = _patch(E, "monitor_capture_conflict", lambda m, c: True)
    r2 = _patch(platform, "process_loopback_available", lambda: (False, "build 19041"))
    try:
        assert E.effective_loopback_mode(cap, mon) == "endpoint", "切不了就保持原样"
        assert E.effective_loopback_mode(cap, {"enabled": False}) == "endpoint"
        assert E.effective_loopback_mode({"loopback_mode": "process"}, mon) == "process"
    finally:
        r2()
        r1()
    print("  策略：同设备 + 可用 → process；不可用/未启用 → 保持原模式 OK")


def test_policy_disables_monitor_when_unavailable() -> None:
    import vlt.gui_engine as GE

    q: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(cfg=SimpleNamespace(output={
        "capture": {"loopback_mode": "endpoint"},
        "audio": {"monitor": {"enabled": True, "device_name": "", "volume": 60}},
    }), q=q, proxy_fn=None)
    calls: list[dict] = []

    class FakeProxy:
        def restart_monitor(self, **kw):                # noqa: ANN003
            calls.append(kw)
            return False

    ctx.proxy_fn = lambda: FakeProxy()
    r1 = _patch(GE, "monitor_capture_conflict", lambda m, c: True)
    r2 = _patch(GE, "effective_loopback_mode", lambda c, m: "endpoint")
    try:
        GE.apply_ear_monitor_policy(ctx)
    finally:
        r2()
        r1()
    mon = ctx.cfg.output["audio"]["monitor"]
    assert mon["enabled"] is False, "切不了 process 时必须关掉耳返"
    assert calls and calls[0]["enabled"] is False, calls
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert any(m[0] == "status" and m[1] == "warn" for m in msgs), msgs
    print("  process 不可用 → 关耳返 + warn OK")


def test_policy_switches_capture_mode() -> None:
    import vlt.gui_engine as GE

    q: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(cfg=SimpleNamespace(output={
        "capture": {"loopback_mode": "endpoint"},
        "audio": {"monitor": {"enabled": True, "device_name": "", "volume": 60}},
    }), q=q, proxy_fn=None)
    r1 = _patch(GE, "monitor_capture_conflict", lambda m, c: True)
    r2 = _patch(GE, "effective_loopback_mode", lambda c, m: "process")
    try:
        GE.apply_ear_monitor_policy(ctx)
    finally:
        r2()
        r1()
    assert ctx.cfg.output["capture"]["loopback_mode"] == "process"
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    assert any(m[0] == "status" and m[1] == "info" for m in msgs), msgs
    print("  同设备 + 可用 → 本次采集自动改 process + info 留痕 OK")


# ---------------------------------------------------------------- ⑤ 落盘

def test_save_monitor_config() -> None:
    from vlt import config as C
    from vlt import gui_audio as GA

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        p.write_text("output:\n  audio:\n    monitor:\n      enabled: false\n"
                     "      device_name: \"\"\n      volume: 70\n", encoding="utf-8")
        orig = C.DEFAULT_CONFIG
        C.DEFAULT_CONFIG = p
        try:
            ctx = GA.AudioCtx()
            GA.save_monitor_config(ctx, None, True, "Headset", 55.0)
        finally:
            C.DEFAULT_CONFIG = orig
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        m = raw["output"]["audio"]["monitor"]
        assert m == {"enabled": True, "device_name": "Headset", "volume": 55.0}, m
    print("  save_monitor_config 真落盘三项 OK")


if __name__ == "__main__":
    print("test_ear_monitor:")
    test_volume_math()
    test_ring_drops_oldest_and_pads_silence()
    test_open_failure_is_contained()
    test_non_windows_is_disabled()
    test_push_does_not_feed_monitor()
    test_playout_tap_feeds_monitor_same_bytes()
    test_silence_is_not_pushed_to_monitor()
    test_burst_arrival_never_truncates_monitor()
    test_proxy_restart_monitor()
    test_conflict_detection()
    test_effective_mode_upgrades_to_process()
    test_policy_disables_monitor_when_unavailable()
    test_policy_switches_capture_mode()
    test_save_monitor_config()
    print("ALL PASSED")
