"""手腕屏微调 / 桌面字幕调整：都在**设置弹窗**里，且两页各管各的（issue #42 返工）。

## 为什么单独一条用例

维护者口径（2026-10-04）：
  ① 桌面面板与手腕屏区别太大，**配置不许共享**、控件也不许混在一起
     —— 第一版把桌面控件塞进手腕屏那个面板，被打回；
  ② 这两块都是「设一次就不再动」的参数，不该占主界面 → 收进「⚙ 设置」。

本文件钉住：两块都在设置弹窗的分页里（主窗口上一个控件都没有）、两页互不串门、
桌面那四个滑块的初值/落盘/内存同步/热重载真的变（只验"写了个数"是空过）。

沙箱里手腕屏段（`overlay:`）的字号/尺寸**故意与桌面段不同**：谁把两段接回一起就现形。
全程只读写 `out/` 下的沙箱配置，用户的 `config.yaml` 一概不碰。
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 干净环境（CI）没有 API key，而 load_config 默认 require_key=True 会 SystemExit。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "deskpanel0123456789abcdef")

GUI_SANDBOX_DIR = ROOT / "out" / "desktop_panel_cfg"
GUI_SANDBOX = GUI_SANDBOX_DIR / "config.yaml"
FAKE_MISSING_TITLE = "vlt-no-such-window-panel"

# 沙箱值：桌面段（本段的）与手腕屏段（overlay:）**故意不同**
DESK_FONT, DESK_SRC_FONT = 40, 22
DESK_W, DESK_H = 900, 300
DESK_ALPHA = 0.70
WRIST_FONT = 55            # overlay.font_size —— 桌面字幕绝不该读它
WRIST_SIZE = [1024, 440]   # overlay.size_px

# 用户拖动后的值
NEW_FONT, NEW_SRC_FONT = 62, 34
NEW_W, NEW_H = 1280, 420
NEW_WRIST_FONT = 58        # 手腕屏段拖动后的字号


def _make_gui_sandbox() -> None:
    """从模板生成沙箱配置：桌面段一套值、手腕屏段另一套值、目标窗口不存在。

    只在 `desktop_overlay:` **之后**做替换（`overlay:` 段在前面，且两段有同名键）——
    用 partition 切开再改尾巴，避免误伤手腕屏段。
    """
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    head, sep, tail = text.partition("desktop_overlay:")
    assert sep, "模板里没有 desktop_overlay: 段"

    def sub(pattern: str, repl: str, what: str) -> None:
        nonlocal tail
        tail, n = re.subn(pattern, repl, tail, count=1)
        assert n == 1, f"沙箱替换失败：{what}（模板改了？）"

    sub(r"(?m)^  font_size: 36.*$", f"  font_size: {DESK_FONT}", "桌面段 font_size")
    sub(r"(?m)^  source_font_size: 29.*$", f"  source_font_size: {DESK_SRC_FONT}",
        "桌面段 source_font_size")
    sub(r"(?m)^  size_px: \[1024, 360\].*$", f"  size_px: [{DESK_W}, {DESK_H}]",
        "桌面段 size_px")
    sub(r"(?m)^  alpha: 0\.90.*$", f"  alpha: {DESK_ALPHA}", "桌面段 alpha")
    sub(r"(?m)^(  game_title:\s*)\S+", rf"\g<1>{FAKE_MISSING_TITLE}", "game_title")

    # 手腕屏段的字号/尺寸改成与桌面段不同的值（证明两段独立）
    head, n = re.subn(r"(?m)^  font_size: 36.*$", f"  font_size: {WRIST_FONT}", head, count=1)
    assert n == 1, "沙箱替换失败：overlay 段 font_size"
    head, n = re.subn(r"(?m)^  size_px: \[1024, 440\].*$",
                      f"  size_px: [{WRIST_SIZE[0]}, {WRIST_SIZE[1]}]", head, count=1)
    assert n == 1, "沙箱替换失败：overlay 段 size_px"

    GUI_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    # .gitattributes 规定源码 LF：显式传 newline，别让 Windows 把整份配置写成 CRLF
    GUI_SANDBOX.write_text(head + sep + tail, encoding="utf-8", newline="\n")

    data = _read()
    assert data["desktop_overlay"]["font_size"] == DESK_FONT, data["desktop_overlay"]
    assert data["desktop_overlay"]["size_px"] == [DESK_W, DESK_H], data["desktop_overlay"]
    assert data["overlay"]["font_size"] == WRIST_FONT, data["overlay"]


def _read() -> dict:
    return yaml.safe_load(GUI_SANDBOX.read_text(encoding="utf-8"))


def _texts(win, skip: set | None = None) -> set[str]:
    """递归收集一棵控件树里所有 text 属性（判断面板归属用）。

    `skip` = 整棵跳过的子树（如设置弹窗 —— Tk 里 Toplevel 挂在 root 的
    `winfo_children()` 下，不跳过的话「主窗口里没有这些控件」根本验不出来）。
    """
    skip = skip or set()
    out: set[str] = set()
    for w in win.winfo_children():
        if w in skip:
            continue
        try:
            keys = w.keys()
        except Exception:  # noqa: BLE001
            continue
        if "text" in keys:
            out.add(str(w.cget("text")))
        out |= _texts(w, skip)
    return out


def test_adjust_panels_live_in_settings_pages() -> None:
    """★ 手腕屏微调 / 桌面字幕调整都在**设置弹窗**里，且两页各管各的。

    维护者口径（2026-10-04）：① 桌面面板与手腕屏**配置不共享**、控件也不能混在一起；
    ② 这两块都是「设一次就不再动」的参数，不该占据主界面 → 收进「⚙ 设置」。
    """
    import vlt.config as _cfg_mod
    import vlt.i18n as _i18n
    import vlt.gui as _gui_mod

    _make_gui_sandbox()
    saved = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG, _i18n.detect_system_language)
    _cfg_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _gui_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _i18n.detect_system_language = lambda: "zh"

    from vlt.gui import TranslationGUI
    from vlt.i18n import t

    gui = None
    try:
        gui = TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        # ① 设置弹窗在启动时就建好（控件可用）但**不显示**
        assert gui._settings_win is not None and not gui._settings_win.winfo_ismapped(), \
            "设置弹窗不该在启动时就显示出来"

        # ② 两页都在设置弹窗里，且各挂各的分页
        for page, title in ((gui._desktop_page, "桌面字幕"), (gui._wrist_page, "手腕屏")):
            assert str(page.winfo_toplevel()) == str(gui._settings_win), \
                f"「{title}」页不在设置弹窗里"
            assert t(title) in gui._settings_tabs, f"设置里没有「{title}」这一页"

        # ③ 两页控件互不串门（字号两边都有，属正常；尺寸/拖动/手腕屏专属参数各归各的）
        desk_txts = _texts(gui._desktop_page)
        wrist_txts = _texts(gui._wrist_page)
        for label in ("译文字号", "原文字号", "面板宽度", "面板高度", "透明度", "解锁拖动"):
            assert t(label) in desk_txts, f"桌面字幕页里没有「{label}」"
        for label in ("面板宽度", "面板高度", "解锁拖动"):
            assert t(label) not in wrist_txts, f"「{label}」跑进手腕屏页了（两页必须各管各的）"
        for label in ("弯曲", "俯仰X", "底板不透明度"):
            assert t(label) not in desk_txts, f"手腕屏专属的「{label}」跑进桌面字幕页了"

        # ④ 主窗口上不该再有这些控件（搬家的核心；跳过设置弹窗那棵树才验得准）
        main_txts = _texts(gui._root, skip={gui._settings_win})
        for label in ("面板宽度", "面板高度", "俯仰X", "弯曲", "底板不透明度"):
            assert t(label) not in main_txts, f"「{label}」还留在主窗口上（应该只在设置弹窗里）"

        # ⑤ 滑块初值 == 桌面段自己的值（**不是**手腕屏段的）
        assert int(float(gui._desktop_font_var.get())) == DESK_FONT
        assert int(float(gui._desktop_srcfont_var.get())) == DESK_SRC_FONT
        assert (int(float(gui._desktop_w_var.get())),
                int(float(gui._desktop_h_var.get()))) == (DESK_W, DESK_H)
        assert int(float(gui._desktop_font_var.get())) != WRIST_FONT, \
            "读到了手腕屏 overlay.font_size"
        assert gui._desktop_font_lbl.cget("text") == str(DESK_FONT), \
            gui._desktop_font_lbl.cget("text")
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG,
         _i18n.detect_system_language) = saved
    print("  两块调整都在设置弹窗里（手腕屏页 / 桌面字幕页各自独立）+ 主窗口已清空 OK")


def test_desktop_sliders_save_own_keys_only_when_touched() -> None:
    """四个滑块：没拖过不写盘；拖过的写 `desktop_overlay:` 自己的键，且热重载真的变。"""
    import vlt.config as _cfg_mod
    import vlt.i18n as _i18n
    import vlt.gui as _gui_mod

    _make_gui_sandbox()
    before = GUI_SANDBOX.read_text(encoding="utf-8")
    saved = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG, _i18n.detect_system_language)
    _cfg_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _gui_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _i18n.detect_system_language = lambda: "zh"

    from vlt.gui import TranslationGUI
    from vlt.output.desktop_overlay import DesktopOverlayConfig

    gui = None
    try:
        gui = TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        # ① 没拖过 → 一个字节都不许改（`_save_desktop_cfg` 也被拖动落盘调到）
        assert gui._desktop_out is None, "本用例不该起字幕窗（只验滑块 → 配置这条路）"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._save_desktop_cfg()
        assert GUI_SANDBOX.read_text(encoding="utf-8") == before, "没拖过滑块却改写了配置"
        # ⚠️ 「文件没变」还不够（写进去的值恰好等于原值也看不出）：日志里也不许有落盘记录
        assert "已写入 config.yaml" not in buf.getvalue(), \
            f"没拖过滑块却触发了落盘：{buf.getvalue()!r}"

        # ② 拖四个滑块 → 值标签跟着变 + 写盘 + 内存同步
        gui._desktop_font_var.set(NEW_FONT)
        gui._on_desktop_font()
        gui._desktop_srcfont_var.set(NEW_SRC_FONT)
        gui._on_desktop_srcfont()
        gui._desktop_w_var.set(NEW_W)
        gui._on_desktop_width()
        gui._desktop_h_var.set(NEW_H)
        gui._on_desktop_height()
        gui._save_desktop_cfg()                  # 防抖那 300ms 直接跑同一个函数

        data = _read()
        d = data["desktop_overlay"]
        assert int(d["font_size"]) == NEW_FONT, d.get("font_size")
        assert int(d["source_font_size"]) == NEW_SRC_FONT, d.get("source_font_size")
        assert [int(v) for v in d["size_px"]] == [NEW_W, NEW_H], d.get("size_px")
        # 手腕屏段一个字节都不许被碰
        assert data["overlay"]["font_size"] == WRIST_FONT, data["overlay"]
        assert data["overlay"]["size_px"] == WRIST_SIZE, data["overlay"]
        # 内存同步（否则「关掉桌面字幕再勾上」会用旧值重建窗口）
        for key, want in (("font_size", NEW_FONT), ("source_font_size", NEW_SRC_FONT),
                          ("size_px", [NEW_W, NEW_H])):
            assert gui._cfg.desktop_overlay.get(key) == want, \
                f"内存没同步 {key}：{gui._cfg.desktop_overlay.get(key)}"

        # ③ 关键一条：字幕窗热重载时解析出来**真的变了**（只验"写了个数"是空过）
        hot = DesktopOverlayConfig.from_dict(d)
        assert hot.font_size == NEW_FONT and hot.source_font_size == NEW_SRC_FONT
        assert tuple(hot.size_px) == (NEW_W, NEW_H), hot.size_px
        v = hot.visual_config()
        assert v.font_size == NEW_FONT and v.size_px == (NEW_W, NEW_H), (v.font_size, v.size_px)

        # ④ 值标签也跟着更新了（界面上看得见的反馈）
        assert gui._desktop_font_lbl.cget("text") == str(NEW_FONT)
        assert gui._desktop_w_lbl.cget("text") == str(NEW_W)
    finally:
        GUI_SANDBOX.write_text(before, encoding="utf-8")
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG,
         _i18n.detect_system_language) = saved
    print(f"  四个滑块：没拖不写盘 / 拖过写 desktop_overlay 自己的键 "
          f"(字号 {NEW_FONT}/{NEW_SRC_FONT}、面板 {NEW_W}x{NEW_H}) / 热重载真的变 OK")


def test_settings_sliders_really_schedule_save() -> None:
    """★ 回归 6083052：设置页的滑块必须真的把「落盘」排上 —— 只改内存/只改标签不算。

    症状（用户实测）：拖手腕屏或桌面字幕的滑块，config.yaml 一个字节都不变 →
    后端没得热重载，看着就是「设置不生效」。根因：拆分时 gui_desktop 里另留了一份
    schedule_*_save，Tk root 从没传进去（`if root is not None` 直接空转）→ 全是死路。

    ⚠️ 本用例**不绕过防抖**（同文件 `test_desktop_sliders_save_own_keys_only_when_touched`
    是直接调 `_save_*_cfg()`，所以抓不到这个）：调真实滑块处理器，再推 Tk 事件循环让
    `after(300)` 到期，最后验配置文件真的变了。
    """
    import time as _time

    import vlt.config as _cfg_mod
    import vlt.gui as _gui_mod
    import vlt.i18n as _i18n
    from vlt import gui_desktop

    _make_gui_sandbox()
    before = GUI_SANDBOX.read_text(encoding="utf-8")
    saved = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG, _i18n.detect_system_language)
    _cfg_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _gui_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _i18n.detect_system_language = lambda: "zh"

    from vlt.gui import TranslationGUI

    def _pump(root, seconds: float) -> None:      # noqa: ANN001
        end = _time.monotonic() + seconds
        while _time.monotonic() < end:
            root.update()
            _time.sleep(0.01)

    gui = None
    try:
        gui = TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        ctx = gui._desktop_ctx
        assert ctx.schedule_desktop_save_fn is not None and ctx.schedule_overlay_save_fn is not None, \
            "设置页没拿到落盘回调 → 滑块永远不落盘（本 bug 的直接判据）"

        # ① 桌面字幕：动一次字号 → 等 after(300) 到期 → 配置必须变
        gui._desktop_font_var.set(NEW_FONT)
        gui._on_desktop_font()
        _pump(gui._root, 0.6)
        assert int(_read()["desktop_overlay"]["font_size"]) == NEW_FONT, \
            f"桌面字幕滑块没落盘：{_read()['desktop_overlay'].get('font_size')}"
        assert _read()["overlay"]["font_size"] == WRIST_FONT, "桌面滑块串改了手腕屏段"

        # ② 手腕屏：动一次字号（走设置页真实用的那个 handler）→ 同样必须落盘
        ctx.tune_values["font_size"] = float(NEW_WRIST_FONT)
        ctx.tune_vars["font_size"].set(NEW_WRIST_FONT)
        gui_desktop.make_tune_handler(
            "font_size", ctx.tune_vars["font_size"], ctx.tune_lbls["font_size"], "",
            ctx)(str(NEW_WRIST_FONT))
        _pump(gui._root, 0.6)
        assert int(_read()["overlay"]["font_size"]) == NEW_WRIST_FONT, \
            f"手腕屏滑块没落盘：{_read()['overlay'].get('font_size')}"
        assert int(_read()["desktop_overlay"]["font_size"]) == NEW_FONT, "手腕屏滑块串改了桌面段"
    finally:
        GUI_SANDBOX.write_text(before, encoding="utf-8")
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG,
         _i18n.detect_system_language) = saved
    print("  设置页滑块走真实防抖链路：桌面 + 手腕屏都真的写进了 config.yaml OK")


def test_wrist_page_falls_back_to_fewer_columns_when_narrow() -> None:
    """★ 设置内容区放不下时，手腕屏页必须**降列**而不是硬撑被裁（列的判据是实测）。

    这一页是从主界面搬进设置弹窗的：主界面俄语下宽 1273px，设置窗固定 760px ——
    首轮就踩了三列滑块顶破内容区（实测 768 > 756，最右边那列的值标签被裁）。
    做法：把设置窗宽压到 420（比任何语言都要窄得多）再建界面，
    列数必须自动降下来，网格需求宽度不许超过同一套公式算出的内容区。
    ⚠️ 宽度现在是「基准值 × DPI 缩放」的运行时有效值：这里的基准取自
    `ui_theme.SETTINGS_WIDTH`，同时把缩放钉成 1.0，免得宿主 DPI（≈100dpi → ×1.04）
    把 420 再放大、把这条例子的前提冲掉。

    ⚠️ 只测「网格需求宽度」这个**未映射时也可靠**的量：真显示设置窗再量控件在
    Windows CI 上会把 Tcl 搞崩（`Tcl_AsyncDelete: async handler deleted by the wrong thread`
    —— 设置窗会拉起电平探针线程，反复开关窗时踩到），别那么测。
    """
    import vlt.config as _cfg_mod
    import vlt.i18n as _i18n
    import vlt.gui as _gui_mod
    import vlt.ui_theme as _ui_theme_mod

    _make_gui_sandbox()
    sandbox = yaml.safe_load(GUI_SANDBOX.read_text(encoding="utf-8"))
    sandbox.setdefault("ui", {})["scale"] = 1.0  # 字号与几何一起固定，不能只 mock 几何。
    GUI_SANDBOX.write_text(yaml.safe_dump(sandbox, allow_unicode=True), encoding="utf-8")
    saved = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG, _i18n.detect_system_language,
             _ui_theme_mod.SETTINGS_WIDTH)
    _cfg_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _gui_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _ui_theme_mod.SETTINGS_WIDTH = 420       # 比五种语言里最窄的还窄
    _i18n.detect_system_language = lambda: "zh"

    from vlt.gui import TAB_INSET_X, TranslationGUI

    gui = None
    try:
        gui = TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update_idletasks()
        assert abs(float(gui._root.tk.call("tk", "scaling")) - 96 / 72) < .02
        budget = 420 - 2 * TAB_INSET_X - 24       # 与 vlt/gui.py 里同一套算法
        got = int(gui._tune_grid.winfo_reqwidth())
        cols = len({int(c.grid_info()["column"]) for c in gui._tune_grid.winfo_children()})
        assert got <= budget, (f"内容区只有 {budget}px，滑块网格却要 {got}px（列数没降下来）")
        assert cols == 1, f"窄到 420px 时该降到 1 列，实际 {cols} 列"
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG, _i18n.detect_system_language,
         _ui_theme_mod.SETTINGS_WIDTH) = saved
    print("  内容区变窄时手腕屏页自动降列（420px → 1 列，不越界）OK")


if __name__ == "__main__":
    print("test_desktop_panel:")
    test_adjust_panels_live_in_settings_pages()
    test_wrist_page_falls_back_to_fewer_columns_when_narrow()
    test_desktop_sliders_save_own_keys_only_when_touched()
    test_settings_sliders_really_schedule_save()
    print("ALL PASSED")
