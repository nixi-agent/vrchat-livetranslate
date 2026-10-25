#!/usr/bin/env python
"""桌面字幕上的「原声/译音」胶囊：配置 / 定位 / 真窗性质 / 界面接线。

用户反馈：现在要切原声/译音，得「闭麦 → 切到 VLT 主窗口 → 点按钮 → 听 → 切回 VRChat
→ 开麦」。胶囊就是为了省掉这一串 —— 字幕旁边点一下就行，**焦点不能动**。

本文件钉住四件事：

1. **独立小窗，不是画在面板里**：字幕窗默认鼠标穿透（`WS_EX_TRANSPARENT`），面板里的
   按钮永远收不到点击；胶囊必须是不穿透的独立 Toplevel，但带
   `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`（点了不抢焦点）—— 这两条在真窗上逐一断言；
2. **定位**：贴面板右上角**外侧**（不挡字幕）；顶部放不下才退回面板内部右上角；
3. **dry-run / 关开关绝不建窗**：现有离线用例依赖 dry-run 不建任何窗口；
4. **接线只有一条路径**：胶囊回调与主界面按钮是同一个 `gui_voice.toggle_voice_mode`，
   档位翻转与状态栏文案必须逐字一致（把 `_refresh_mode_pill` 从
   `_refresh_voice_mode_btn` 摘掉时，本文件必有用例变红）。

只读写 out/ 下的沙箱配置；代理/引擎/桌面字幕全是假对象，不开音频设备、不联网。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _cfgbox import sandbox_config  # noqa: E402

CONFIG = sandbox_config(reset=True)

import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"
_i18n.set_language("zh")

from vlt import gui_voice, platform  # noqa: E402
from vlt.output.desktop_overlay import (  # noqa: E402
    PILL_GAP, DesktopOverlay, DesktopOverlayConfig, pill_position,
)
from vlt.output.micproxy import MODE_PASSTHROUGH, MODE_TRANSLATED  # noqa: E402

PILL_TRANSLATED = "🗣 译音"
PILL_SOURCE = "🎙 原声"


# ---------------------------------------------------------------- ① 配置
def test_config_parses_mode_pill() -> None:
    """`desktop_overlay.mode_pill` 默认开；显式 false 要认；示例配置里要有这一行。"""
    assert DesktopOverlayConfig().mode_pill is True, "默认应该带胶囊"
    assert DesktopOverlayConfig.from_dict({}).mode_pill is True
    assert DesktopOverlayConfig.from_dict({"mode_pill": False}).mode_pill is False
    assert DesktopOverlayConfig.from_dict({"mode_pill": "false"}).mode_pill is True, \
        "字符串 \"false\" 在 YAML 里不该出现；本用例只钉「非空字符串按 true 处理」的现状"

    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    section = text.split("desktop_overlay:", 1)[1].split("\noutput:", 1)[0]
    assert "mode_pill:" in section, "config.example.yaml 的 desktop_overlay 段要写出 mode_pill"
    print("  配置：默认 true、可关、示例段已登记 OK")


# ---------------------------------------------------------------- ② dry-run / 关开关
def test_dry_run_records_text_without_building_window() -> None:
    """dry-run 只记文字，绝不建窗（现有离线用例的硬约束）。"""
    ov = DesktopOverlay(DesktopOverlayConfig(enabled=True), dry_run=True)
    assert ov.start() is True
    try:
        fired: list[int] = []
        ov.set_mode_pill(PILL_TRANSLATED, cb=lambda: fired.append(1))
        assert ov._pill is None, f"dry-run 不许建窗：{ov._pill!r}"
        assert ov._pill_text == PILL_TRANSLATED, ov._pill_text
        assert ov._pill_cb is not None, "回调要记住（dry-run 也得能验接线）"

        ov.set_mode_pill(None)
        assert ov._pill is None
        assert ov._pill_text == "", f"None 要隐藏（清空文字）：{ov._pill_text!r}"
        assert ov._pill_cb is None, "隐藏后回调也要清掉"
        print("  dry-run：记文字 / None 隐藏 / 不建窗 OK")
    finally:
        ov.close()


def test_mode_pill_disabled_never_builds() -> None:
    """`mode_pill: false` → 即使给了文字也不建胶囊（连文字都不留）。"""
    cfg = DesktopOverlayConfig.from_dict({"mode_pill": False})
    ov = DesktopOverlay(cfg, dry_run=True)
    assert ov.start() is True
    try:
        ov.set_mode_pill(PILL_TRANSLATED, cb=lambda: None)
        assert ov._pill is None
        assert ov._pill_text == "", f"关掉开关时不该记文字：{ov._pill_text!r}"
        print("  mode_pill=false：不建、不记 OK")
    finally:
        ov.close()


# ---------------------------------------------------------------- ③ 定位（纯函数）
def test_pill_position_above_panel_then_inside_at_screen_top() -> None:
    """右上角外侧；顶部没空间 → 面板内部右上角（内缩一个 gap）。"""
    area = (0, 0, 1920, 1080)
    panel, pill = (400, 200), (120, 40)

    # 面板在屏幕中间：贴在面板上沿之上，右边缘与面板右边缘对齐
    x, y = pill_position((100, 300), panel, pill, area)
    assert (x, y) == (100 + 400 - 120, 300 - 40 - PILL_GAP), (x, y, PILL_GAP)

    # 面板几乎贴着屏幕顶（py=20 → 外侧 y=-28 会跑到屏幕外）→ 挪进面板内部右上角
    x, y = pill_position((100, 20), panel, pill, area)
    assert (x, y) == (100 + 400 - 120 - PILL_GAP, 20 + PILL_GAP), (x, y)

    # 副屏在主屏左侧（x 为负）也不许被夹回主屏
    x, y = pill_position((-1900, 300), panel, pill, (-1920, 0, 0, 1080))
    assert x == -1900 + 400 - 120, x
    print(f"  定位：外侧 gap={PILL_GAP}px / 顶部放不下转内置 / 负坐标不跑偏 OK")


# ---------------------------------------------------------------- ④ 真窗（Tk 冒烟）
def _try_tk():
    """建一个 withdraw 的 Tk root；建不了返回 (None, None, 原因)。"""
    try:
        import tkinter as tk
    except Exception as exc:  # noqa: BLE001
        return None, None, f"没有 tkinter：{type(exc).__name__}: {exc}"
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 — TclError：无显示器 / 无 X
        return None, tk, f"{type(exc).__name__}: {exc}"
    root.withdraw()
    return root, tk, None


def _ex_style(hwnd: int) -> int:
    """Win32 扩展样式（只在 Windows 上被调用）。"""
    import ctypes

    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return int(u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20)) & 0xFFFFFFFF


def test_real_pill_window_is_clickable_and_does_not_steal_focus() -> None:
    """真建一次胶囊：可见、贴在面板右上、**不穿透**、点了不抢焦点、点得到、随窗一起收。

    ⚠️ 这一条不能省：面板本身是穿透的，胶囊反过来必须**不**穿透；而「不抢焦点」是
    本功能的全部意义（焦点被抢走 = 用户在 VRChat 里动不了）。
    """
    root, _tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    cfg = DesktopOverlayConfig(enabled=True, attach_to_game=False, backend="tk",
                               size_px=(320, 120), pos=(200, 200))
    ov = DesktopOverlay(cfg, root=root)
    try:
        assert ov.start() is True
        root.update()
        assert ov._pill is None, "没给文字前不许建胶囊（惰性建窗）"

        clicked: list[int] = []
        ov.set_mode_pill(PILL_SOURCE, cb=lambda: clicked.append(1))
        root.update()
        pill = ov._pill
        assert pill is not None, "给了文字就该建出胶囊"
        assert str(ov._pill_label.cget("text")) == PILL_SOURCE, ov._pill_label.cget("text")
        assert pill.winfo_viewable(), "胶囊必须真的显示出来"
        assert ov._pill_hwnd and platform.is_window(ov._pill_hwnd), ov._pill_hwnd

        # 位置：右边缘对齐面板右边缘，且在面板上沿**之上**（不挡字幕）
        px, py = ov.position
        pw = int(cfg.size_px[0])
        assert pill.winfo_x() + pill.winfo_reqwidth() == px + pw, \
            (pill.winfo_x(), pill.winfo_reqwidth(), px, pw)
        assert pill.winfo_y() + pill.winfo_reqheight() == py - PILL_GAP, \
            (pill.winfo_y(), pill.winfo_reqheight(), py, PILL_GAP)

        if platform.IS_WINDOWS:
            from vlt.platform import win as w

            ex = _ex_style(ov._pill_hwnd)
            assert ex & w._WS_EX_NOACTIVATE, f"胶囊必须不抢焦点：0x{ex:08X}"
            assert ex & w._WS_EX_TOOLWINDOW, f"胶囊不该进 alt-tab：0x{ex:08X}"
            assert not (ex & w._WS_EX_TRANSPARENT), \
                f"胶囊**不许**鼠标穿透（否则点不着）：0x{ex:08X}"
            panel_ex = _ex_style(ov._hwnd)
            assert panel_ex & w._WS_EX_TRANSPARENT, \
                f"前提：字幕面板本身是穿透的（胶囊的存在理由）：0x{panel_ex:08X}"
            print(f"  面板 ex=0x{panel_ex:08X}（穿透） / 胶囊 ex=0x{ex:08X}"
                  f"（NOACTIVATE|TOOLWINDOW）OK")

        # 点一下：绑定真的走到回调（不是画了个死按钮）
        ov._pill_label.event_generate("<Button-1>", x=4, y=4)
        root.update()
        assert clicked == [1], f"点胶囊必须触发回调：{clicked}"

        # 换字：复用同一个窗（不重建、不闪）
        ov.set_mode_pill(PILL_TRANSLATED, cb=lambda: clicked.append(2))
        root.update()
        assert ov._pill is pill, "换文字不该重建窗口"
        assert str(ov._pill_label.cget("text")) == PILL_TRANSLATED

        # 隐藏 → 再显示
        ov.set_mode_pill(None)
        root.update()
        assert not pill.winfo_viewable(), "None 必须把胶囊收起来"
        ov.set_mode_pill(PILL_SOURCE, cb=lambda: clicked.append(3))
        root.update()
        assert pill.winfo_viewable(), "再给文字要重新显示"

        # 关字幕：胶囊一起收掉（否则桌面上会留一个孤儿小块）
        hwnd = ov._pill_hwnd
        ov.close()
        root.update()
        assert ov._pill is None and ov._pill_hwnd == 0
        assert not platform.is_window(hwnd), "胶囊窗口必须随字幕一起销毁"
        print("  真窗：可见 / 右上对齐 / 点得到 / 换字复用 / 关窗一起收 OK")
    finally:
        ov.close()
        root.destroy()


# ---------------------------------------------------------------- ⑤ 界面接线
class _FakeDesktop:
    """假桌面字幕：只记 set_mode_pill 调用（不建任何窗口）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_mode_pill(self, text, cb=None) -> None:      # noqa: ANN001
        self.calls.append((text, cb))

    @property
    def last(self) -> tuple:
        return self.calls[-1] if self.calls else (None, None)


class _FakeProxy:
    """假麦克风代理：记档位与 set_mode 调用，不开设备。"""

    def __init__(self, mode: str = MODE_PASSTHROUGH) -> None:
        self.mode = mode
        self.set_modes: list[str] = []

    def set_mode(self, mode: str) -> bool:
        self.set_modes.append(mode)
        self.mode = mode
        return True


class _FakeEngine:
    def __init__(self, running: bool = True) -> None:
        self.running = running


def _build_gui():
    """非 headless 建界面（要能点主界面那颗按钮）；挪掉更新检查，不联网。"""
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    if gui._update_check_job is not None:                # noqa: SLF001
        gui._root.after_cancel(gui._update_check_job)    # noqa: SLF001
        gui._update_check_job = None                     # noqa: SLF001
    gui._root.update()                                   # noqa: SLF001
    return gui


def test_gui_pill_mirrors_button() -> None:
    """翻译跑起来 → 胶囊显示当前档位；档位一变（主界面或胶囊）文字跟着变；停下 → 隐藏。"""
    gui = _build_gui()
    try:
        fake = _FakeDesktop()
        gui._desktop_out = fake                           # noqa: SLF001
        gui._proxy = _FakeProxy(MODE_PASSTHROUGH)          # noqa: SLF001
        gui._engines = [_FakeEngine()]                    # noqa: SLF001
        gui._vmic_var.set(True)                           # noqa: SLF001
        gui._refresh_voice_mode_btn()                     # noqa: SLF001

        assert fake.last[0] == PILL_SOURCE, fake.calls
        assert fake.last[1] is not None, "有代理 + 翻译在跑 → 必须给可点的回调"
        assert str(gui._voice_mode_btn.cget("text")) == PILL_SOURCE

        gui._voice_mode_btn.invoke()                      # 点主界面按钮
        assert gui._proxy.mode == MODE_TRANSLATED
        assert fake.last[0] == PILL_TRANSLATED, "胶囊文字必须跟着档位变"
        assert str(gui._voice_mode_btn.cget("text")) == PILL_TRANSLATED, \
            "胶囊与主界面按钮不许各说各话"

        fake.last[1]()                                    # 点胶囊
        assert gui._proxy.mode == MODE_PASSTHROUGH
        assert fake.last[0] == PILL_SOURCE
        assert str(gui._voice_mode_btn.cget("text")) == PILL_SOURCE

        gui._engines = []                                 # 翻译停了
        gui._refresh_voice_mode_btn()                     # noqa: SLF001
        assert fake.last == (None, None), f"停止翻译后胶囊必须隐藏：{fake.last}"
        print("  接线：文案随档位双向翻转、停止后隐藏 OK")
    finally:
        gui._root.destroy()                               # noqa: SLF001


def test_gui_pill_hidden_without_proxy_or_overlay() -> None:
    """没代理 / 没字幕窗 → 隐藏（不给死按钮）或不动作；老实例没有该方法也不许崩。"""
    gui = _build_gui()
    try:
        fake = _FakeDesktop()
        gui._desktop_out = fake                           # noqa: SLF001
        gui._engines = [_FakeEngine()]                    # noqa: SLF001
        gui._proxy = None                                 # noqa: SLF001
        gui._refresh_voice_mode_btn()                     # noqa: SLF001
        assert fake.last == (None, None), f"没代理 → 隐藏：{fake.last}"

        gui._proxy = _FakeProxy(MODE_TRANSLATED)           # noqa: SLF001
        gui._desktop_out = None                           # noqa: SLF001
        before = len(fake.calls)
        gui._refresh_voice_mode_btn()                     # noqa: SLF001
        assert len(fake.calls) == before, "没有字幕窗时不该往它身上调"

        gui._desktop_out = object()                       # 老/异形实例：没有 set_mode_pill
        gui._refresh_mode_pill()                          # noqa: SLF001 — 不许抛
        print("  接线：无代理隐藏 / 无字幕窗不动作 / 无异形实例不崩 OK")
    finally:
        gui._root.destroy()                               # noqa: SLF001


def test_gui_pill_click_is_same_path_as_main_button() -> None:
    """点胶囊与点主界面按钮**同一条** `toggle_voice_mode`：档位翻转 + 状态栏文案逐字一致。"""
    gui = _build_gui()
    calls: list[dict] = []
    real = gui_voice.toggle_voice_mode

    def spy(ctx, **kw):                                   # noqa: ANN001, ANN003
        calls.append({"proxy": ctx.proxy, "vmic": kw["vmic_get"](),
                      "set_status": kw["set_status"], "refresh_btn": kw["refresh_btn"]})
        return real(ctx, **kw)

    gui_voice.toggle_voice_mode = spy
    try:
        fake = _FakeDesktop()
        gui._desktop_out = fake                           # noqa: SLF001
        proxy = _FakeProxy(MODE_PASSTHROUGH)
        gui._proxy = proxy                                # noqa: SLF001
        gui._engines = [_FakeEngine()]                    # noqa: SLF001
        gui._vmic_var.set(False)                          # noqa: SLF001 — 译音档会哑：两条路都要同一句提示
        gui._refresh_voice_mode_btn()                     # noqa: SLF001

        # ① 主界面按钮
        gui._voice_mode_btn.invoke()
        main_modes = list(proxy.set_modes)
        main_status = str(gui._chat_ctx.status_label.cget("text"))
        assert main_modes == [MODE_TRANSLATED], main_modes
        assert "未勾选「译音输出」" in main_status, main_status

        # ② 复位后点胶囊
        proxy.mode = MODE_PASSTHROUGH
        gui._refresh_voice_mode_btn()                      # noqa: SLF001
        gui._chat_ctx.status_label.configure(text="")      # noqa: SLF001 — 清空，看胶囊这条路自己写了什么
        pill_cb = fake.last[1]
        assert pill_cb is not None
        pill_cb()
        pill_modes = proxy.set_modes[len(main_modes):]
        pill_status = str(gui._chat_ctx.status_label.cget("text"))

        assert pill_modes == main_modes, (pill_modes, main_modes)
        assert pill_status == main_status, (pill_status, main_status)
    finally:
        gui_voice.toggle_voice_mode = real
        gui._root.destroy()                                # noqa: SLF001

    assert len(calls) == 2, f"两条路都应走 toggle_voice_mode：{len(calls)}"
    main_call, pill_call = calls
    for key in ("proxy", "set_status", "refresh_btn"):
        assert main_call[key] == pill_call[key], f"两条路的 {key} 不是同一个：{key}"
    assert main_call["vmic"] == pill_call["vmic"] is False
    print("  接线：胶囊与主界面按钮同一条 toggle_voice_mode（档位/状态栏/协同者一致）OK")


def main() -> None:
    test_config_parses_mode_pill()
    test_dry_run_records_text_without_building_window()
    test_mode_pill_disabled_never_builds()
    test_pill_position_above_panel_then_inside_at_screen_top()
    test_real_pill_window_is_clickable_and_does_not_steal_focus()
    test_gui_pill_mirrors_button()
    test_gui_pill_hidden_without_proxy_or_overlay()
    test_gui_pill_click_is_same_path_as_main_button()
    print("ALL PASSED")


if __name__ == "__main__":
    main()
