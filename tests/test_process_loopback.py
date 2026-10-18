#!/usr/bin/env python
"""Windows「进程模式」采集腿的离线验收（process loopback）。

## 要钉住的行为

1. `capture.loopback_mode` 只认 endpoint / process，**默认 endpoint**（旧行为不变）；
   非法值留痕并回落 endpoint。
2. `run_loopback(loopback_mode="process")` 在 Windows 上走 `_run_loopback_process`；
   平台不支持 → **回落端点模式 + 状态栏 warn**（禁静默换路）。
3. 进程模式的状态机与 Linux 同形：**等 → 采 → 进程没了 → 再等**；
   打开失败 → 回落端点模式（同样留痕）。
4. `_pump_capture(keep_alive=...)`：进程没了要**结束这一段**（否则哑流一直"在采集"）。
5. int16 契约：`_float32_to_s16` 必须 clamp 到 ±32767 且长度不变
   （下游 `to_16k_mono` 只认 np.int16，喂 float32 会静默变噪声）。

Windows 专属用例（`_float32_to_s16` / `ProcessLoopbackSource` 契约）在没有
`ctypes.WinDLL` 的机器上**明确跳过**（不是判红）——CI 的 linux-tests 腿要能跑。

跑法：.venv/Scripts/python.exe tests/test_process_loopback.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

IS_WINDOWS = sys.platform == "win32"
SKIPPED: list[str] = []


def _patch(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, orig)


class _FakeSession:
    def __init__(self) -> None:
        self.pcm = b""

    async def send_audio(self, pcm: bytes) -> None:      # noqa: ANN001
        self.pcm += pcm


class _FakeGate:
    """只满足 `_gate_tail()` 读的三个属性；身份用来断言「一路带下去」。"""

    enabled = False
    dropped_chunks = 0
    opened = 0


class _FakeSource:
    def __init__(self, chunk: bytes = b"\x01\x00" * 1600, *, alive: bool = True) -> None:
        self.rate = 16000
        self.channels = 1
        self._chunk = chunk
        self._alive = alive
        self.reads = 0
        self.closed = False

    async def read(self, timeout: float = 1.0) -> bytes | None:   # noqa: ARG002
        # 必须真的让出事件循环：否则 keep_alive 失效时整条循环同步空转，
        # `asyncio.wait_for` 的取消永远插不进去（注入验证时实测挂到 120s）。
        await asyncio.sleep(0)
        self.reads += 1
        return self._chunk

    def close(self) -> None:
        self.closed = True

    def process_gone(self) -> bool:
        return not self._alive


# ---------------------------------------------------------------- ① 配置

def test_config_loopback_mode() -> None:
    from vlt import config

    def load(capture_yaml: str):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("session:\n  api_key: \"sk-test-000\"\n"
                         f"capture:\n{capture_yaml}"
                         "directions:\n  mine: {source_lang: zh, target_lang: en}\n",
                         encoding="utf-8")
            return config.load_config(p, require_key=False)

    cfg = load("  mic_device: \"\"\n")
    assert cfg.output["capture"]["loopback_mode"] == "endpoint", \
        f"缺省必须是 endpoint（旧行为不变）：{cfg.output['capture']}"

    cfg = load("  loopback_mode: process\n")
    assert cfg.output["capture"]["loopback_mode"] == "process"

    cfg = load("  loopback_mode: nonsense\n")
    assert cfg.output["capture"]["loopback_mode"] == "endpoint", "非法值必须回落 endpoint"
    print("  loopback_mode：默认 endpoint / 接受 process / 非法值回落 OK")


# ---------------------------------------------------------------- ② 分派

def test_run_loopback_dispatches_process_mode() -> None:
    import vlt.engine as E
    from vlt import platform

    seen: dict = {}

    async def fake_process(session, tele, **kw):        # noqa: ANN001, ANN003, ARG001
        seen.update(kw)

    async def fake_endpoint(*a, **kw):                  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("进程模式可用时不该走端点")

    r1 = _patch(platform, "IS_LINUX", False)
    r2 = _patch(platform, "process_loopback_available", lambda: (True, "build 26200"))
    r3 = _patch(E, "_run_loopback_process", fake_process)
    r4 = _patch(E, "_run_loopback_endpoint", fake_endpoint)
    gate = _FakeGate()
    try:
        asyncio.run(E.run_loopback(_FakeSession(), None, gate=gate,
                                   loopback_mode="process"))
    finally:
        r4()
        r3()
        r2()
        r1()
    assert seen.get("gate") is gate, f"门限没带下去：{seen}"
    print("  process 模式 → _run_loopback_process（gate 一路带下）OK")


def test_run_loopback_falls_back_when_unavailable() -> None:
    import vlt.engine as E
    from vlt import platform

    seen: dict = {"warn": [], "endpoint": 0}

    async def fake_endpoint(*a, **kw):                  # noqa: ANN002, ANN003, ARG001
        seen["endpoint"] += 1

    r1 = _patch(platform, "IS_LINUX", False)
    r2 = _patch(platform, "process_loopback_available",
                lambda: (False, "系统 build 19041 < 19044"))
    r3 = _patch(E, "_run_loopback_endpoint", fake_endpoint)
    try:
        asyncio.run(E.run_loopback(_FakeSession(), None, loopback_mode="process",
                                   on_status=lambda k, m: seen["warn"].append((k, m))))
    finally:
        r3()
        r2()
        r1()
    assert seen["endpoint"] == 1, "不可用时必须回落端点模式"
    assert any(k == "warn" and "进程模式不可用" in m for k, m in seen["warn"]), \
        f"回落必须在状态栏留痕：{seen['warn']}"
    print("  不可用 → 回落端点 + warn 留痕 OK")


# ---------------------------------------------------------------- ③ 状态机

def test_process_mode_waits_captures_and_rewait() -> None:
    import vlt.engine as E
    from vlt import platform

    waits = {"n": 0}
    pumps: list[dict] = []
    statuses: list[tuple[str, str]] = []
    stop = threading.Event()

    async def fake_wait(stop_event, on_status=None, *, poll_s=2.0):   # noqa: ANN001, ARG001, ANN202
        waits["n"] += 1
        if waits["n"] == 1:
            return 4321
        stop_event.set()                      # 第二次等：用户点了停止
        return None

    src = _FakeSource()

    def fake_open(pid):                       # noqa: ANN001, ANN202
        assert pid == 4321, pid
        return src

    async def fake_pump(session, tele, seconds, stop_event, label, opener,   # noqa: ANN001, ANN202, ARG001
                        gate=None, keep_alive=None):
        pumps.append({"gate": gate, "keep_alive": keep_alive, "source": opener()})
        assert keep_alive() is True, "进程还在时必须继续"
        src._alive = False                    # 模拟 VRChat 退出
        assert keep_alive() is False, "进程没了必须让采集这一段结束"
        return 4242

    gate = _FakeGate()
    r1 = _patch(platform, "find_process_pid", lambda name="VRChat.exe": 4321)  # noqa: ARG005
    r2 = _patch(platform, "open_process_loopback", fake_open)
    r3 = _patch(E, "_wait_for_vrchat_process", fake_wait)
    r4 = _patch(E, "_pump_capture", fake_pump)
    try:
        asyncio.run(E._run_loopback_process(_FakeSession(), None, stop_event=stop,
                                            on_status=lambda k, m: statuses.append((k, m)),
                                            gate=gate))
    finally:
        r4()
        r3()
        r2()
        r1()

    assert waits["n"] == 2, f"进程退出后必须回到等待：{waits}"
    assert len(pumps) == 1 and pumps[0]["gate"] is gate
    assert any("只采集 VRChat 音频（进程 4321）" in m for _, m in statuses), statuses
    assert any(k == "warn" and "等待重新启动" in m for k, m in statuses), statuses
    print("  等 → 采（带 gate）→ 进程退出 → 再等 OK")


def test_process_mode_open_failure_falls_back() -> None:
    import vlt.engine as E
    from vlt import platform

    seen = {"endpoint": 0, "warn": []}

    async def fake_wait(stop_event, on_status=None, *, poll_s=2.0):   # noqa: ANN001, ARG001, ANN202
        return 777

    def boom(pid):                             # noqa: ANN001, ARG001
        raise RuntimeError("激活进程采集失败：E_ACCESSDENIED")

    async def fake_endpoint(*a, **kw):         # noqa: ANN002, ANN003, ARG001
        seen["endpoint"] += 1

    r1 = _patch(E, "_wait_for_vrchat_process", fake_wait)
    r2 = _patch(platform, "open_process_loopback", boom)
    r3 = _patch(E, "_run_loopback_endpoint", fake_endpoint)
    try:
        asyncio.run(E._run_loopback_process(
            _FakeSession(), None, stop_event=threading.Event(),
            on_status=lambda k, m: seen["warn"].append((k, m))))
    finally:
        r3()
        r2()
        r1()
    assert seen["endpoint"] == 1, "打开失败必须回落端点模式"
    assert any(k == "warn" and "回落系统输出端点" in m for k, m in seen["warn"]), seen["warn"]
    print("  打开失败 → 回落端点 + warn 留痕 OK")


# ---------------------------------------------------------------- ④ 采集泵

def test_pump_capture_stops_when_keep_alive_false() -> None:
    import vlt.engine as E

    src = _FakeSource()
    calls = {"n": 0}

    def alive() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2

    async def run() -> int:
        # ⚠️ 必须带超时：keep_alive 失效时 `_pump_capture` 会永远读下去 —— 用例要
        #    **快速变红**，而不是把 CI 挂死（注入验证时实测会 hang 到 120s）。
        return await asyncio.wait_for(
            E._pump_capture(_FakeSession(), None, 0.0, threading.Event(),
                            "loopback", lambda: src, keep_alive=alive),
            timeout=2.0)

    sent = asyncio.run(run())
    assert sent == 6400, f"只该送前两块（各 3200 字节）：{sent}"
    assert src.closed, "结束这一段时必须关流"
    print("  keep_alive=False → 结束本段并关流 OK")


# ---------------------------------------------------------------- ⑤ int16 契约（Windows 专属）

def test_float32_to_s16_contract() -> None:
    if not IS_WINDOWS:
        SKIPPED.append("test_float32_to_s16_contract（非 Windows）")
        return
    import numpy as np
    from vlt.platform import win

    src = np.array([0.0, 0.5, -0.5, 1.5, -1.5, 1.0], dtype=np.float32)
    out = np.frombuffer(win._float32_to_s16(src.tobytes()), dtype=np.int16)  # noqa: SLF001
    assert out.dtype == np.int16 and out.size == src.size, out
    assert int(out[0]) == 0 and int(out[1]) == 16383 and int(out[2]) == -16383, out
    assert int(out[3]) == 32767 and int(out[4]) == -32767, f"必须 clamp，不能回绕：{out}"
    assert int(out[5]) == 32767, out
    print("  float32→int16：clamp ±32767 / 长度与字节序 OK")


def test_source_declares_pcm_contract() -> None:
    if not IS_WINDOWS:
        SKIPPED.append("test_source_declares_pcm_contract（非 Windows）")
        return
    from vlt.platform import win

    loop = asyncio.new_event_loop()
    try:
        src = win.ProcessLoopbackSource(loop, pid=os.getpid())
        assert (src.rate, src.channels) == (48000, 2), (src.rate, src.channels)
        assert src.label == "loopback"
    finally:
        loop.close()
    print("  采集源声明 48kHz ×2ch（下游按这个降混到 16k 单声道）OK")


def test_windows_probe_reports_reason() -> None:
    if not IS_WINDOWS:
        SKIPPED.append("test_windows_probe_reports_reason（非 Windows）")
        return
    from vlt.platform import win

    ok, why = win.process_loopback_available()
    assert isinstance(ok, bool) and isinstance(why, str) and why, (ok, why)
    if ok:
        assert win._windows_build() >= win.PROCESS_LOOPBACK_MIN_BUILD, why   # noqa: SLF001
    print(f"  探测可判定：available={ok}（{why}）OK")


# ---------------------------------------------------------------- ⑥ 落盘

def test_save_device_config_writes_mode() -> None:
    """界面保存：`capture.loopback_mode` 必须真的写进 YAML（不是只改内存）。"""
    import yaml
    from vlt import config as C
    from vlt import gui_audio as GA

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        p.write_text("capture:\n  mic_device: \"\"\n  loopback_device: \"\"\n"
                     "  loopback_mode: endpoint\n", encoding="utf-8")
        orig = C.DEFAULT_CONFIG
        C.DEFAULT_CONFIG = p
        try:
            ctx = GA.AudioCtx()
            ctx.linux_fixed_audio = False
            GA.save_device_config(ctx, None, "Mic A", "Cable In", "Speakers", "process")
        finally:
            C.DEFAULT_CONFIG = orig
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert raw["capture"]["loopback_mode"] == "process", raw["capture"]
        assert raw["capture"]["mic_device"] == "Mic A", raw["capture"]
    print("  save_device_config 真落盘 loopback_mode=process OK")


if __name__ == "__main__":
    print("test_process_loopback:")
    test_config_loopback_mode()
    test_run_loopback_dispatches_process_mode()
    test_run_loopback_falls_back_when_unavailable()
    test_process_mode_waits_captures_and_rewait()
    test_process_mode_open_failure_falls_back()
    test_pump_capture_stops_when_keep_alive_false()
    test_float32_to_s16_contract()
    test_source_declares_pcm_contract()
    test_windows_probe_reports_reason()
    test_save_device_config_writes_mode()
    for s in SKIPPED:
        print(f"  SKIP {s}")
    print("ALL PASSED")
