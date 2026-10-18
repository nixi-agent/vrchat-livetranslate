"""耳返开关的**位置**与接线：开关在主界面「输出」行，音量/设备仍在设置页。

维护者口径（2026-10-18）：耳返是日常会开关的东西，藏进「设置 → 音频」太深；
音量/输出设备是「设一次就不动」的参数，留在设置页。

本文件钉住：
1. 位置：主界面输出行 = chatbox / 手腕屏 / 译音输出 / **耳返** / 桌面字幕（耳返紧挨译音输出）；
2. 唯一变量：`gui._monitor_var` 只有主界面那一份，设置页**不再**建第二个（旧实现两处各一个）；
3. 接线：勾选 → 写 `output.audio.monitor.enabled` + `proxy.restart_monitor(...)`；
4. 设置页只剩音量/设备，且它们的可用态跟着主界面开关走；
5. 「译音输出」没勾时勾耳返 → 照开流（配置一致）但状态栏说明为什么没声；
6. Linux（无实现）→ 主界面不建这个控件。

只读写 out/ 下的沙箱配置；代理是假对象，不开任何真实音频设备。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _cfgbox import sandbox_config  # noqa: E402

CONFIG = sandbox_config(reset=True)

# 干净环境没有 key；沙箱里先把耳返打开，用来验「主界面初值来自配置」。
_text = CONFIG.read_text(encoding="utf-8")
_text = _text.replace("    monitor:\n      enabled: false",
                      "    monitor:\n      enabled: true", 1)
CONFIG.write_text(_text, encoding="utf-8")

import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

from vlt import gui_layout  # noqa: E402
from vlt import platform as _plat  # noqa: E402

#: Linux 没有耳返实现（主界面不建控件、设置页也没有音量/设备）→ Windows 专用用例需明确跳过，
#: 不能判红（CI 的 linux-tests 腿就是这么挂的）。
_SKIP_WIN = "  ⚠️ Linux：耳返无实现（控件不建）→ 跳过这条 Windows 专用用例"
from vlt.gui import TranslationGUI  # noqa: E402


class _FakeProxy:
    """只记调用，不开设备。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.volumes: list[float] = []

    def restart_monitor(self, **kw):                     # noqa: ANN003
        self.calls.append(kw)
        return True

    def set_monitor_volume(self, v: float) -> None:
        self.volumes.append(float(v))


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _texts(widget) -> list[str]:
    out = []
    for w in _walk(widget):
        try:
            txt = w.cget("text")
        except Exception:                                # noqa: BLE001
            continue
        if txt:
            out.append(str(txt))
    return out


def _build() -> TranslationGUI:
    # 非 headless：控件要真建出来（headless 只跳过 UI 构建）。测试进程里
    # `_test_process_guard()` 会挡住麦克风代理，不会真开音频设备。
    gui = TranslationGUI()
    if gui._update_check_job is not None:                # noqa: SLF001
        gui._root.after_cancel(gui._update_check_job)    # noqa: SLF001
        gui._update_check_job = None                     # noqa: SLF001
    gui._root.update()                                   # noqa: SLF001
    return gui


# ---------------------------------------------------------------- ① 位置

def test_toggle_lives_in_output_row() -> None:
    if _plat.IS_LINUX:
        print(_SKIP_WIN)
        return
    gui = _build()
    try:
        assert gui._monitor_check is not None, "Windows 主界面必须有耳返开关"
        assert str(gui._monitor_check.cget("text")) == "耳返", gui._monitor_check.cget("text")
        row = gui._monitor_check.master
        labels = [t for t in _texts(row) if t in ("chatbox", "手腕屏", "译音输出", "耳返", "桌面字幕")]
        assert labels == ["chatbox", "手腕屏", "译音输出", "耳返", "桌面字幕"], labels
        print(f"  主界面输出行顺序：{' / '.join(labels)} OK（耳返紧挨译音输出）")

        # 初值来自配置（沙箱里 enabled: true）
        assert bool(gui._monitor_var.get()) is True, "主界面初值必须来自 output.audio.monitor.enabled"
        print("  主界面初值来自 config（enabled: true）OK")
    finally:
        gui._root.destroy()                              # noqa: SLF001


# ---------------------------------------------------------------- ② 接线

def test_toggle_writes_config_and_restarts_monitor() -> None:
    if _plat.IS_LINUX:
        print(_SKIP_WIN)
        return
    gui = _build()
    try:
        gui._proxy = _FakeProxy()                        # noqa: SLF001
        gui._vmic_var.set(True)                          # noqa: SLF001 — 译音输出开着
        gui._monitor_var.set(False)                      # noqa: SLF001
        gui._on_monitor_change()                         # noqa: SLF001
        assert gui._proxy.calls[-1] == {"enabled": False, "device_name": "", "volume": 70.0}, \
            gui._proxy.calls[-1]
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        assert raw["output"]["audio"]["monitor"]["enabled"] is False, raw["output"]["audio"]["monitor"]

        gui._monitor_var.set(True)                       # noqa: SLF001
        gui._on_monitor_change()                         # noqa: SLF001
        assert gui._proxy.calls[-1] == {"enabled": True, "device_name": "", "volume": 70.0}, \
            gui._proxy.calls[-1]
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        assert raw["output"]["audio"]["monitor"]["enabled"] is True, raw["output"]["audio"]["monitor"]
        print("  勾选/取消 → 落盘 + restart_monitor(enabled, device, volume) OK")
    finally:
        gui._root.destroy()                              # noqa: SLF001


def test_toggle_saves_config_without_settings_controls() -> None:
    """有开关、但音量/设备控件还没建（懒加载 / headless）时，配置仍要保存。

    审阅 #3（EMeowAGENT）：早先 `_on_monitor_change` 在「没有音量控件」时整体早退，
    会把用户刚点的开关静默丢掉；现在只跳过 UI 同步，配置照常落盘。
    """
    if _plat.IS_LINUX:
        print(_SKIP_WIN)
        return
    gui = _build()
    try:
        gui._proxy = None                                # noqa: SLF001
        gui._monitor_vol_var = None                      # noqa: SLF001 — 模拟控件还没建
        gui._monitor_combo = None                        # noqa: SLF001
        gui._monitor_var.set(False)                      # noqa: SLF001
        gui._on_monitor_change()                         # noqa: SLF001
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        assert raw["output"]["audio"]["monitor"]["enabled"] is False, \
            f"没有音量控件时也必须保存开关：{raw['output']['audio']['monitor']}"
        gui._monitor_var.set(True)                       # noqa: SLF001
        gui._on_monitor_change()                         # noqa: SLF001
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        assert raw["output"]["audio"]["monitor"]["enabled"] is True
        assert raw["output"]["audio"]["monitor"]["volume"] == 70.0, "沿用配置里的音量"
        print("  无音量控件时开关照样落盘（沿用配置音量/设备）OK")
    finally:
        gui._root.destroy()                              # noqa: SLF001


def test_settings_page_keeps_only_volume_and_device() -> None:
    if _plat.IS_LINUX:
        print(_SKIP_WIN)
        return
    gui = _build()
    try:
        win = gui._settings_win                             # noqa: SLF001
        assert win is not None, "设置弹窗启动时就该建好"
        texts = _texts(win)
        assert "启用耳返（自己听译音）" not in texts, "开关已搬到主界面，设置页不许再有一个"
        assert "耳返音量:" in texts, texts
        assert "耳返输出:" in texts, texts
        assert gui._monitor_vol is not None and gui._monitor_combo is not None

        # 可用态跟主界面开关走
        gui._monitor_var.set(False)                       # noqa: SLF001
        from vlt import gui_settings
        gui_settings.sync_monitor_controls(gui)
        assert str(gui._monitor_vol.cget("state")) == "disabled", gui._monitor_vol.cget("state")
        assert str(gui._monitor_combo.cget("state")) == "disabled"
        gui._monitor_var.set(True)                       # noqa: SLF001
        gui_settings.sync_monitor_controls(gui)
        assert str(gui._monitor_vol.cget("state")) == "normal", gui._monitor_vol.cget("state")
        assert str(gui._monitor_combo.cget("state")) == "readonly"
        print("  设置页：只有音量/输出设备，且可用态跟随主界面开关 OK")
    finally:
        gui._root.destroy()                              # noqa: SLF001


def test_sidetone_without_voice_output_says_why() -> None:
    if _plat.IS_LINUX:
        print(_SKIP_WIN)
        return
    gui = _build()
    try:
        gui._proxy = _FakeProxy()                        # noqa: SLF001
        gui._vmic_var.set(False)                         # noqa: SLF001 — 译音输出没开
        gui._monitor_var.set(True)                       # noqa: SLF001
        gui._on_monitor_change()                         # noqa: SLF001
        # 仍然开流（配置/运行态一致，之后勾译音输出立刻有声）
        assert gui._proxy.calls[-1]["enabled"] is True, gui._proxy.calls[-1]
        gui._root.update_idletasks()                     # noqa: SLF001
        status = str(gui._status_label.cget("text"))     # noqa: SLF001
        assert "译音输出" in status, f"状态栏要说清为什么没声：{status}"
        print(f"  译音输出没勾时勾耳返 → 状态栏提示「{status}」OK")
    finally:
        gui._root.destroy()                              # noqa: SLF001


def test_toggle_without_proxy_says_why() -> None:
    """没有麦克风代理（代理关掉 / 测试进程）时勾耳返：配置照常存，但要说清为什么没声。"""
    if _plat.IS_LINUX:
        print(_SKIP_WIN)
        return
    gui = _build()
    try:
        gui._proxy = None                                # noqa: SLF001
        gui._vmic_var.set(True)                          # noqa: SLF001
        gui._monitor_var.set(True)                       # noqa: SLF001
        gui._on_monitor_change()                         # noqa: SLF001
        gui._root.update_idletasks()                     # noqa: SLF001
        status = str(gui._status_label.cget("text"))     # noqa: SLF001
        assert "麦克风代理" in status, f"没有代理时必须说明，不能静默：{status}"
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        assert raw["output"]["audio"]["monitor"]["enabled"] is True, "用户的选择仍要落盘"
        print(f"  无代理时勾耳返 → 状态栏提示「{status}」OK")
    finally:
        gui._root.destroy()                              # noqa: SLF001


# ---------------------------------------------------------------- ③ Linux 不建

def test_no_toggle_on_linux() -> None:
    old = gui_layout.IS_LINUX
    gui_layout.IS_LINUX = True
    try:
        gui = _build()
        try:
            assert gui._monitor_check is None, "Linux 无耳返实现，不该建一个点了没反应的开关"
            assert gui._monitor_var is None
            print("  Linux：主界面不建耳返开关 OK")
        finally:
            gui._root.destroy()                          # noqa: SLF001
    finally:
        gui_layout.IS_LINUX = old


if __name__ == "__main__":
    print("test_monitor_main_toggle:")
    test_toggle_lives_in_output_row()
    test_toggle_writes_config_and_restarts_monitor()
    test_toggle_saves_config_without_settings_controls()
    test_settings_page_keeps_only_volume_and_device()
    test_sidetone_without_voice_output_says_why()
    test_toggle_without_proxy_says_why()
    test_no_toggle_on_linux()
    print("ALL PASSED")
