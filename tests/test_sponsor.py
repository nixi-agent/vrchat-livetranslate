"""赞助弹窗验收：按钮存在、弹窗内容齐全、Ko-fi 打桩、缺图降级、不重复开窗。

规则（来自 .kimi-brief-sponsor-gui.md 的硬要求）：
- 顶栏要有「☕ 赞助」按钮（TButton，与「⚙ 设置」同侧）；
- 弹窗里两张收款码 = 两个 Label，PhotoImage 非空、等比缩到约 240px（严禁拉伸）；
- 「打开 Ko-fi 赞助页面」必须以正确 URL 调 webbrowser.open（测试里打桩，绝不真开浏览器）；
- 图片路径不存在时不抛异常，降级成文字提示；
- 重复点击不产生第二个弹窗。

需要 Tk（Windows 上标准库自带）。无显示环境跑不了 GUI 的部分。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 这些用例断言的是中文界面文案（「☕ 赞助」「打开 Ko-fi 赞助页面」「二维码图片缺失」等）。
# 界面语言会跟随系统语言（CI 与外国机器是英文系统）→ 必须钉死，
# 否则同一份代码在不同机器上结果不同。产品代码不依赖这个补丁。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"


def _make_gui():
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    gui._root.update()
    return gui


def _destroy(gui) -> None:
    try:
        gui._close_sponsor()
    except Exception:
        pass
    try:
        gui._root.destroy()
    except Exception:
        pass


def test_sponsor_button_exists() -> None:
    """① 顶栏按钮存在且是 TButton。"""
    import tkinter.ttk as ttk

    gui = _make_gui()
    try:
        btn = gui._sponsor_btn
        assert isinstance(btn, ttk.Button), f"不是 TButton：{type(btn)}"
        assert btn.cget("text") == "☕ 赞助", f"按钮文字不对：{btn.cget('text')!r}"
        assert str(btn.cget("command")) != "", "按钮没有绑定命令"
        # 与「⚙ 设置」同侧（同一个顶栏 frame 里、side=RIGHT）
        assert btn.master is gui._settings_btn.master, "赞助按钮不在顶栏"
        info_sponsor = btn.pack_info()
        info_settings = gui._settings_btn.pack_info()
        assert info_sponsor["side"] == "right" and info_settings["side"] == "right", \
            f"两个按钮都应 side=right：赞助={info_sponsor['side']} 设置={info_settings['side']}"
        print("  ✓ 顶栏「☕ 赞助」按钮存在（TButton，与「⚙ 设置」同侧 right）")
    finally:
        _destroy(gui)


def test_popup_contents() -> None:
    """② 弹窗真的建起来：两张码 = 两个 Label、PhotoImage 非空、约 240px 且等比。"""
    from vlt import gui as gui_mod

    gui = _make_gui()
    try:
        gui._open_sponsor()
        win = gui._sponsor_win
        assert win is not None and win.winfo_exists(), "弹窗没建起来"
        gui._root.update()

        # 标题 / 主按钮 / 说明 / 关闭 都在
        texts = []
        buttons = []

        def walk(w):
            for c in w.winfo_children():
                texts.append(str(c.cget("text")) if "text" in c.keys() else "")
                if c.winfo_class() == "TButton":
                    buttons.append(str(c.cget("text")))
                walk(c)

        walk(win)
        joined = "\n".join(texts)
        assert "☕ 请我喝一杯" in joined, "缺标题"
        assert "打开 Ko-fi 赞助页面" in buttons, f"缺主按钮：{buttons}"
        assert "扫码支持 · 你给的钱会变成 API token，然后被我烧掉" in joined, "缺说明文字"
        assert "关闭" in buttons, "缺关闭按钮"
        assert "微信" in joined and "支付宝" in joined, "缺码的标签"
        # 用户口径（2026-09-25）：「点那个蓝按钮就直接开浏览器了，地址没必要放」→ 弹窗里不再展示 URL。
        # 地址也没丢：_open_kofi 打不开浏览器时会把 SPONSOR_URL 写进状态栏（用例 ③ 覆盖）。
        assert gui_mod.SPONSOR_URL not in joined, f"弹窗里不该再出现 Ko-fi 地址：{joined!r}"
        assert not hasattr(gui, "_sponsor_url_var"), "地址输入框应已移除，不该留残留属性"

        # 两张码：两个带图 Label，PhotoImage 非空、尺寸约 240 且等比（原图 ≈ 方形）
        assert len(gui._sponsor_qr_labels) == 2, \
            f"应有 2 个码 Label，实际 {len(gui._sponsor_qr_labels)}"
        assert len(gui._sponsor_imgs) == 2, "PhotoImage 引用丢了"
        sizes = [(im.width(), im.height()) for im in gui._sponsor_imgs]
        for w, h in sizes:
            assert 200 <= w <= gui_mod.SPONSOR_QR_SIZE, f"码宽度异常：{w}"
            assert 200 <= h <= gui_mod.SPONSOR_QR_SIZE, f"码高度异常：{h}"
            assert abs(w - h) <= 2, f"码被拉伸变形了：{w}x{h}"
        print(f"  ✓ 弹窗内容齐全；两张码尺寸 {sizes}（等比，上限 {gui_mod.SPONSOR_QR_SIZE}）")
    finally:
        _destroy(gui)


def test_kofi_button_calls_webbrowser() -> None:
    """③ 点主按钮 → webbrowser.open 以正确 URL 被调用（打桩，绝不真开浏览器）。"""
    import vlt.gui as gui_mod

    calls: list[str] = []
    orig = gui_mod.webbrowser.open
    gui_mod.webbrowser.open = lambda url, *a, **kw: calls.append(url) or True
    gui = _make_gui()
    try:
        gui._open_sponsor()
        gui._root.update()
        gui._open_kofi()          # 与主按钮 command 是同一个方法
        assert calls == [gui_mod.SPONSOR_URL], f"webbrowser.open 调用不对：{calls}"
        assert gui_mod.SPONSOR_URL == "https://ko-fi.com/kcmnixi", \
            f"URL 变了：{gui_mod.SPONSOR_URL}"
        print(f"  ✓ webbrowser.open 被以 {calls[0]} 调用（打桩，未真开浏览器）")
    finally:
        gui_mod.webbrowser.open = orig
        _destroy(gui)


def test_missing_qr_images_degrade() -> None:
    """④ 图片路径不存在时不抛异常：降级文字提示 + WARN 日志，主窗口不受影响。"""
    import contextlib
    import io

    import vlt.gui as gui_mod

    orig = gui_mod._sponsor_qr_specs
    missing = ROOT / "assets" / "__definitely_missing__.png"
    gui_mod._sponsor_qr_specs = lambda: [("微信", missing), ("支付宝", missing)]
    buf = io.StringIO()
    gui = _make_gui()
    try:
        with contextlib.redirect_stdout(buf):
            gui._open_sponsor()   # 绝不许抛异常
        gui._root.update()
        win = gui._sponsor_win
        assert win is not None and win.winfo_exists(), "缺图时弹窗也应该能开"
        assert not gui._sponsor_qr_labels, "缺图时不该有码 Label"

        texts = []

        def walk(w):
            for c in w.winfo_children():
                if "text" in c.keys():
                    texts.append(str(c.cget("text")))
                walk(c)

        walk(win)
        assert texts.count("二维码图片缺失") == 2, f"降级文字不对：{texts}"
        out = buf.getvalue()
        assert "[gui] ⚠️ 收款码加载失败" in out, f"缺 WARN 日志：{out!r}"
        print("  ✓ 图片缺失时降级为「二维码图片缺失」+ WARN 日志，无异常")
    finally:
        gui_mod._sponsor_qr_specs = orig
        _destroy(gui)


def test_no_duplicate_popup() -> None:
    """⑤ 重复点击不产生第二个弹窗（已有窗口只聚焦/置顶）。"""
    import tkinter as tk

    gui = _make_gui()
    try:
        gui._open_sponsor()
        first = gui._sponsor_win
        gui._open_sponsor()
        gui._open_sponsor()
        gui._root.update()
        assert gui._sponsor_win is first, "重复点击 new 了第二个弹窗"
        popups = [w for w in gui._root.winfo_children()
                  if isinstance(w, tk.Toplevel) and w is not gui._settings_win]
        assert len(popups) == 1, f"弹窗数量不对：{len(popups)}"
        # ESC / 关闭后应能重新打开
        gui._close_sponsor()
        assert gui._sponsor_win is None, "关闭后引用没清掉"
        gui._open_sponsor()
        assert gui._sponsor_win is not None and gui._sponsor_win.winfo_exists(), \
            "关闭后重新打开失败"
        print("  ✓ 重复点击只聚焦已有弹窗；关闭后可重新打开")
    finally:
        _destroy(gui)


def main() -> int:
    tests = [
        test_sponsor_button_exists,
        test_popup_contents,
        test_kofi_button_calls_webbrowser,
        test_missing_qr_images_degrade,
        test_no_duplicate_popup,
    ]
    print("赞助弹窗验收：")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {t.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
