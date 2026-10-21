"""译音输出设备热切换验收（issue #111）。

## 真实问题

设置 → 音频里换「译音输出」（`output.audio.device_name`）后，**正在跑的麦克风代理
不会改用新设备**：配置落盘了、界面显示正常，但声音还在启动时选的那个端点，
只能重启程序 —— 而且界面上没有任何提示。

根因：代理的输出设备只在 `start()` 时由 `_pick_output_device()` 解析一次；
设置页 `save_device_config()` 只写配置，不碰代理。麦克风那条路是对称做过的
（`_apply_mic_change → reopen_mic`），输出侧漏了。

## 这里钉住什么

1. `MicProxy.reopen_output("新设备")` 真的用**新索引**重开输出流，并关掉旧流；
2. 新设备打不开 → **换回旧端点**、返回 False（调用方据此状态栏 + 日志留痕）；
3. 同一台设备 → 不重开（避免无谓静音）；
4. 代理没启动 → 只更新配置、返回 False（下次 start 生效）；
5. `gui._on_device_change` 在 `device_name` 变化时**必须**调到 `_apply_out_change`
   —— 这是本次漏掉的那条接线，缺了它前 4 条全绿也没用。

故障注入：删掉 `_on_device_change` 里的 out 分支 → 第 5 条必须变红。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output import micproxy as mp  # noqa: E402


class _Stream:
    def __init__(self, **kw) -> None:
        self.kw = kw
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _SD:
    """假 sounddevice：按 device 索引决定打不打得开。"""

    fail_on: set[int] = set()
    opened: list[_Stream] = []

    @classmethod
    def reset(cls) -> None:
        cls.fail_on = set()
        cls.opened = []

    @classmethod
    def query_devices(cls, idx=None):
        return {"name": f"Card#{idx}", "default_samplerate": 48000}

    @classmethod
    def RawOutputStream(cls, **kw):
        dev = kw.get("device")
        if dev in cls.fail_on:
            raise OSError(f"device {dev} busy")
        s = _Stream(**kw)
        cls.opened.append(s)
        return s


def _proxy(cfg_device: str = "Old") -> "mp.MicProxy":
    p = mp.MicProxy(audio_cfg={"device_name": cfg_device, "sample_rate": 48000,
                               "buffer_ms": 300, "proxy": {}})
    p._opened = True
    p._out_device = (0, cfg_device)
    p._out_stream = _Stream(device=0)
    return p


def test_reopen_output_switches_to_new_index() -> None:
    _SD.reset()
    old_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = _SD
    p = _proxy("Old")
    old_stream = p._out_stream
    try:
        # 名字 → 索引：新设备 = 1
        saved = (mp.resolve_device_name, mp.platform.output_device_fallbacks)
        mp.resolve_device_name = lambda name, kind: 1 if name == "New" else 0
        mp.platform.output_device_fallbacks = lambda *a, **k: []
        try:
            ok = p.reopen_output("New")
        finally:
            mp.resolve_device_name, mp.platform.output_device_fallbacks = saved
        assert ok is True, "reopen_output 应返回 True"
        assert old_stream.closed, "旧输出流必须被关掉（否则两路同时出声）"
        assert p._out_device == (1, "Card#1"), p._out_device
        assert _SD.opened and _SD.opened[-1].kw.get("device") == 1, _SD.opened[-1].kw
        assert p._audio_cfg["device_name"] == "New"
        print("  切到新设备：用新索引重开、关旧流 OK")
    finally:
        if old_sd is not None:
            sys.modules["sounddevice"] = old_sd
        else:
            sys.modules.pop("sounddevice", None)


def test_reopen_output_falls_back_to_old_stream() -> None:
    _SD.reset()
    _SD.fail_on = {1}
    old_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = _SD
    p = _proxy("Old")
    try:
        saved = (mp.resolve_device_name, mp.platform.output_device_fallbacks)
        mp.resolve_device_name = lambda name, kind: 1
        mp.platform.output_device_fallbacks = lambda *a, **k: []
        try:
            ok = p.reopen_output("New")
        finally:
            mp.resolve_device_name, mp.platform.output_device_fallbacks = saved
        assert ok is False, "新设备打不开必须返回 False"
        assert p._out_device == (0, "Old"), f"必须换回旧端点，实际 {p._out_device}"
        assert any(s.kw.get("device") == 0 for s in _SD.opened), "旧端点要重新打开"
        print("  新设备失败 → 换回旧端点、返回 False OK")
    finally:
        if old_sd is not None:
            sys.modules["sounddevice"] = old_sd
        else:
            sys.modules.pop("sounddevice", None)


def test_same_device_does_not_reopen() -> None:
    _SD.reset()
    old_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = _SD
    p = _proxy("Same")
    old_stream = p._out_stream
    try:
        saved = (mp.resolve_device_name, mp.platform.output_device_fallbacks)
        mp.resolve_device_name = lambda name, kind: 0
        mp.platform.output_device_fallbacks = lambda *a, **k: []
        try:
            assert p.reopen_output("Same") is True
        finally:
            mp.resolve_device_name, mp.platform.output_device_fallbacks = saved
        assert not old_stream.closed, "同一台设备不该重开（会白静音一下）"
        assert _SD.opened == []
        print("  同一台设备 → 不重开 OK")
    finally:
        if old_sd is not None:
            sys.modules["sounddevice"] = old_sd
        else:
            sys.modules.pop("sounddevice", None)


def test_not_opened_only_updates_config() -> None:
    p = mp.MicProxy(audio_cfg={"device_name": "Old", "sample_rate": 48000,
                               "buffer_ms": 300, "proxy": {}})
    assert p.reopen_output("New") is False, "没启动时应返回 False（下次 start 生效）"
    assert p._audio_cfg["device_name"] == "New"
    print("  未启动 → 只更新配置 OK")


def test_gui_device_change_wires_out_branch() -> None:
    """`_on_device_change` 必须把 out 变化接到 `_apply_out_change`（本次漏的接线）。"""
    import vlt.gui as gui_mod

    class _FakeGui:
        def __init__(self) -> None:
            self._cfg = SimpleNamespace(output={"audio": {"device_name": "Old"},
                                                "capture": {"mic_device": ""}})
            self._audio_ctx = object()
            self._names_holder = {}
            self.calls: list[tuple[str, str]] = []

        def _sync_audio_ctx(self) -> None:
            pass

        def _current_mic_device(self) -> str:
            return str((self._cfg.output.get("capture") or {}).get("mic_device") or "")

        def _current_out_device(self) -> str:
            return str((self._cfg.output.get("audio") or {}).get("device_name") or "")

        def _apply_mic_change(self, name: str) -> None:
            self.calls.append(("mic", name))

        def _apply_out_change(self, name: str) -> None:
            self.calls.append(("out", name))

    g = _FakeGui()
    orig = gui_mod.gui_audio.on_device_change
    gui_mod.gui_audio.on_device_change = (
        lambda ctx, cfg, holder: cfg.output["audio"].update({"device_name": "New"}))
    try:
        gui_mod.TranslationGUI._on_device_change(g)
    finally:
        gui_mod.gui_audio.on_device_change = orig
    assert ("out", "New") in g.calls, f"device_name 变了却没走 _apply_out_change：{g.calls}"
    print("  _on_device_change → _apply_out_change 接线 OK")


if __name__ == "__main__":
    print("test_output_device_switch:")
    test_reopen_output_switches_to_new_index()
    test_reopen_output_falls_back_to_old_stream()
    test_same_device_does_not_reopen()
    test_not_opened_only_updates_config()
    test_gui_device_change_wires_out_branch()
    print("ALL PASSED")
