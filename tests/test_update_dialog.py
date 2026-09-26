"""GUI 更新弹窗 / 下载进度窗 / 设置「软件更新」区的接线验收。

跑法：.venv/Scripts/python.exe tests/test_update_dialog.py

全程离线：检查器（`gui._update_checker`）与下载器（`gui._update_downloader`）一律
注入假的，绝不真连 GitHub —— CI 机器不一定能连外网。需要 Tk（Windows 标准库自带）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

# 与 tests/test_config_save.py 同理：干净环境（CI）没有 API key，给个拼接出来的假 key，
# 本文件只验更新接线，跟 key 的真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "updguitest0123456789abcdef")

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

from vlt import __version__                          # noqa: E402
from vlt import update_check as uc                   # noqa: E402
from vlt.config import DEFAULT_CONFIG                # noqa: E402

MB = 1024 * 1024
OUT = ROOT / "out" / "update_dialog"
EXE_NEW = "VRChatLiveTranslate.exe.new"

INFO = uc.ReleaseInfo(tag="v9.9.9", version="9.9.9",
                      html_url="https://example.test/r",
                      exe_url="https://example.test/e",
                      sums_url="https://example.test/s",
                      exe_size=10 * MB)
INFO_NOSIZE = uc.ReleaseInfo(tag="v9.9.9", version="9.9.9",
                             html_url="https://example.test/r",
                             exe_url="https://example.test/e",
                             sums_url="https://example.test/s",
                             exe_size=None)
# 另一个版本号：ignore 用例会把 9.9.9 写进忽略列表（被测的正确行为），
# 之后还要弹窗的用例换用 9.9.8，避免用例间通过 config.yaml 互相干扰。
INFO2 = uc.ReleaseInfo(tag="v9.9.8", version="9.9.8",
                       html_url="https://example.test/r2",
                       exe_url="https://example.test/e2",
                       sums_url="https://example.test/s2",
                       exe_size=10 * MB)

# UI 禁词（计划「面向普通用户的交互/文案 checklist」硬要求）：技术细节只进日志，不进界面
FORBIDDEN_WORDS = ("exe", "pid", "sha256", "bat", "进程", "校验", "asset")


# ---------------------------------------------------------------- 小工具


def _make_gui():
    """真窗口（CI windows 有显示环境）。取消启动 3 秒的自动检查调度 —— 测试绝不真连网络。"""
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    if gui._update_check_job is not None:
        gui._root.after_cancel(gui._update_check_job)
        gui._update_check_job = None
    gui._root.update()
    return gui


def _destroy(gui) -> None:
    for closer in (gui._close_update_dialog, gui._close_download_window):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
    try:
        gui._root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _pump_until(gui, pred, timeout: float = 5.0) -> bool:
    """主线程泵队列（_poll 是唯一消费者），直到条件满足或超时。"""
    end = time.time() + timeout
    while time.time() < end:
        gui._poll()
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001 — 条件读取期间窗口可能刚销毁，下一轮再看
            pass
        time.sleep(0.01)
    return False


def _walk(win):
    for c in win.winfo_children():
        yield c
        yield from _walk(c)


def _walk_texts(win) -> list[str]:
    out = []
    for c in _walk(win):
        if "text" in c.keys():
            out.append(str(c.cget("text")))
    return out


def _all_buttons(win) -> list:
    return [c for c in _walk(win) if c.winfo_class() == "TButton"]


def _btn(win, text: str):
    for b in _all_buttons(win):
        if str(b.cget("text")) == text:
            return b
    raise AssertionError(f"按钮不存在：{text}（现有：{[str(b.cget('text')) for b in _all_buttons(win)]}）")


def _wrap_recorder(widget, method: str, touched: list) -> None:
    """包一层控件方法记录调用线程 id：下载线程若直接碰 Tk 控件，测试立刻红。"""
    orig = getattr(widget, method)

    def wrapped(*a, **k):
        touched.append(threading.get_ident())
        return orig(*a, **k)

    setattr(widget, method, wrapped)


def _fake_latest(ver, cfg):  # noqa: ANN001, ANN201
    """假检查器：已是最新。留痕与 check_for_updates 同款（它本来就负责打印结论行）。"""
    print("[update] 已是最新 v9.9.9", flush=True)
    return "latest", INFO


# ---------------------------------------------------------------- 用例


def test_update_dialog_and_ignore() -> None:
    """有更新 → 弹窗出现、三个按钮文字正确、非模态；「不再提示这个版本」写盘后不再弹。"""
    gui = _make_gui()
    try:
        gui._update_checker = lambda ver, cfg: ("update", INFO)
        gui._run_update_check(manual=False)
        gui._poll()
        win = gui._update_win
        assert win is not None and win.winfo_exists(), "有更新却没弹窗"
        assert win.title() == "发现新版本", f"标题不对：{win.title()!r}"
        texts = [str(b.cget("text")) for b in _all_buttons(win)]
        assert texts == ["立即更新", "下次再说", "不再提示这个版本"], f"按钮不对：{texts}"
        joined = "\n".join(_walk_texts(win))
        assert ("VRChat Live Translate 有新版本了。"
                "现在更新只要一两分钟，不影响你正在进行的翻译。") in joined, f"正文不对：{joined!r}"
        assert "看看这次更新了什么" in joined, "缺 Release 页链接"
        assert win.grab_current() is None, "弹窗不许 grab_set（非模态硬约束，不能卡翻译）"

        # 重复触发 → 旧窗销毁，永远只有一窗
        gui._show_update_dialog(INFO)
        win2 = gui._update_win
        assert win2 is not win and win2.winfo_exists(), "重建弹窗失败"
        assert not win.winfo_exists(), "旧弹窗没销毁"
        tops = [w for w in gui._root.winfo_children()
                if isinstance(w, tk.Toplevel) and w is not gui._settings_win]
        assert len(tops) == 1, f"弹窗数量不对：{len(tops)}"

        # 点「不再提示这个版本」→ 写 config.yaml + 关窗；同版本再来 → GUI 复核忽略列表拦住
        _btn(win2, "不再提示这个版本").invoke()
        assert gui._update_win is None, "点按钮后弹窗没关"
        assert "9.9.9" in uc.load_ignored_versions(DEFAULT_CONFIG), "忽略版本没写进 config.yaml"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._run_update_check(manual=False)
            gui._poll()
        assert gui._update_win is None, "忽略后还弹窗"
        assert "忽略列表" in buf.getvalue(), f"缺 [update] 留痕：{buf.getvalue()!r}"
        print("  ✓ 三按钮弹窗：文案/非模态/单窗；「不再提示这个版本」写 config 后不再弹")
    finally:
        _destroy(gui)


def test_later_snooze_session_only() -> None:
    """「下次再说」→ 本会话不再自动弹、磁盘里没记录；手动检查照弹。"""
    gui = _make_gui()
    try:
        gui._update_checker = lambda ver, cfg: ("update", INFO2)
        before = DEFAULT_CONFIG.read_text(encoding="utf-8") if DEFAULT_CONFIG.exists() else ""
        gui._run_update_check(manual=False)
        gui._poll()
        win = gui._update_win
        assert win is not None, "前提：弹窗要出现"
        _btn(win, "下次再说").invoke()
        assert gui._update_win is None, "点「下次再说」后弹窗没关"
        assert gui._update_snoozed is True, "没记会话级 snooze"
        after = DEFAULT_CONFIG.read_text(encoding="utf-8") if DEFAULT_CONFIG.exists() else ""
        # 「下次再说」不落盘 = 本用例前后 config.yaml 字节完全一致
        # （不能断言「没有 update_ignored」：上一个用例合法写入的 9.9.9 还在里面）
        assert after == before, "「下次再说」不该改 config.yaml"
        assert "9.9.8" not in after, "「下次再说」把版本号写进盘了"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._run_update_check(manual=False)
            gui._poll()
        assert gui._update_win is None, "snooze 后自动结果还弹窗"
        assert "下次再说" in buf.getvalue(), f"snooze 抑制没留痕：{buf.getvalue()!r}"

        gui._run_update_check(manual=True)          # 手动检查不受 snooze 影响（用户明确要看）
        gui._poll()
        assert gui._update_win is not None, "snooze 后手动检查也不弹了"
        print("  ✓ 下次再说：本会话不再自动弹、不落盘；手动检查照弹")
    finally:
        _destroy(gui)


def test_update_now_source_mode_shows_guidance() -> None:
    """源码运行点「立即更新」→ 弹指引（git pull / 下载页），绝不自更新、不开下载窗。"""
    from tkinter import messagebox as mb

    gui = _make_gui()
    calls: list = []
    orig = mb.askokcancel
    mb.askokcancel = lambda *a, **k: calls.append(a) or False
    try:
        assert uc.update_mode() == "source", "测试环境应是源码运行（这个分支走不到 frozen）"
        gui._on_update_check_result("update", INFO2, False, None)
        win = gui._update_win
        assert win is not None, "前提：弹窗要出现"
        _btn(win, "立即更新").invoke()
        assert len(calls) == 1, "源码运行点「立即更新」没给指引"
        assert "源码" in str(calls[0][1]) and "git pull" in str(calls[0][1]), \
            f"指引文案不对：{calls[0]!r}"
        assert gui._dl_win is None, "源码运行不该开下载窗"
        print("  ✓ 源码运行点「立即更新」→ 指引（git pull / 下载页），不做自更新")
    finally:
        mb.askokcancel = orig
        _destroy(gui)


def test_download_window_progress_and_done() -> None:
    """进度窗：假下载器发 progress → 进度条推进；完成 → 两个按钮；全程不在下载线程碰控件。"""
    from tkinter import messagebox as mb

    old_mode, old_exe = uc.update_mode, sys.executable
    uc.update_mode = lambda: "frozen"                    # 模拟打包 exe 运行形态
    fake_dir = OUT / "frozen_app"
    fake_dir.mkdir(parents=True, exist_ok=True)
    sys.executable = str(fake_dir / "VRChatLiveTranslate.exe")

    gate = threading.Event()
    seen: list[tuple] = []

    def fake_dl(info, dest_dir, timeout=None, progress=None):  # noqa: ANN001, ANN201
        progress(2 * MB, 10 * MB)
        seen.append(("progress", 2 * MB, 10 * MB, threading.get_ident()))
        assert gate.wait(5), "测试门闩超时"
        progress(10 * MB, 10 * MB)
        return Path(dest_dir) / EXE_NEW

    gui = _make_gui()
    try:
        gui._update_downloader = fake_dl
        gui._on_update_now(INFO)                         # frozen → 开进度窗 + 起下载线程
        win = gui._dl_win
        assert win is not None and win.winfo_exists(), "frozen 下没开下载进度窗"
        assert win.title() == "正在下载新版本", f"下载中标题不对：{win.title()!r}"
        assert float(gui._dl_bar.cget("value")) == 0.0, "进度条初始不是 0"
        assert "点右上角关闭会取消下载，下次可以再下。" in _walk_texts(win), "缺底部小字"

        # 线程纪律记录器：下载期间谁碰了这两个控件、在哪个线程，全部记下
        touched: list[int] = []
        _wrap_recorder(gui._dl_bar, "configure", touched)
        _wrap_recorder(gui._dl_text, "configure", touched)

        ok = _pump_until(gui, lambda: float(gui._dl_bar.cget("value")) == float(2 * MB))
        assert ok, "进度条没推进到 2MB"
        txt = str(gui._dl_text.cget("text"))
        assert "已下载 2.0 / 约 10.0 MB" in txt and "下载期间可以正常翻译" in txt, \
            f"进度文本不对：{txt!r}"

        gate.set()                                       # 放行假下载器跑完
        ok = _pump_until(gui, lambda: gui._dl_new_exe is not None)
        assert ok, "下载完成消息没回主线程"
        assert win.title() == "下载完成", f"完成态标题不对：{win.title()!r}"
        assert float(gui._dl_bar.cget("value")) == float(gui._dl_bar.cget("maximum")), \
            "完成时进度条不是 100%"
        texts = [str(b.cget("text")) for b in _all_buttons(win)]
        assert texts == ["立即重启并更新", "稍后更新"], f"完成态按钮不对：{texts}"
        body = str(gui._dl_text.cget("text"))
        assert "新版本已经准备好了。" in body and "等你关闭程序时会自动换好" in body, \
            f"完成态正文不对：{body!r}"
        assert not gui._dl_note.winfo_manager(), "完成态还显示「关窗取消」小字"

        main_id = threading.main_thread().ident
        bad = [t for t in touched if t != main_id]
        assert not bad, f"下载线程直接碰了 Tk 控件（线程 {bad}）"
        assert touched, "记录器一次都没被调到，说明进度根本没画上去"

        # 未接线的两个按钮：点了必须给明确提示，不许静默
        mb_calls: list = []
        orig_si = mb.showinfo
        mb.showinfo = lambda *a, **k: mb_calls.append(a) or True
        try:
            _btn(win, "立即重启并更新").invoke()
            _btn(win, "稍后更新").invoke()
        finally:
            mb.showinfo = orig_si
        assert len(mb_calls) == 2 and all("下一个版本" in str(c[1]) for c in mb_calls), \
            f"未接线按钮没给明确提示：{mb_calls!r}"
        print("  ✓ 下载进度窗：进度推进 → 完成态两按钮；回调没在非主线程碰控件；"
              "未接线按钮有明确提示")
    finally:
        uc.update_mode = old_mode
        sys.executable = old_exe
        _destroy(gui)


def test_download_total_fallback_and_indeterminate() -> None:
    """总量三档：Content-Length 优先 → exe_size 兜底 → 都没有时 indeterminate 只显示已下载量。"""
    gui = _make_gui()
    try:
        gui._show_download_window(INFO)                  # exe_size=10MB
        gui._on_download_progress(5 * MB, None)          # Content-Length 缺失 → exe_size 兜底
        assert str(gui._dl_bar.cget("mode")) == "determinate"
        assert float(gui._dl_bar.cget("maximum")) == float(10 * MB), "没用 exe_size 兜底"
        assert float(gui._dl_bar.cget("value")) == float(5 * MB)
        assert "已下载 5.0 / 约 10.0 MB" in str(gui._dl_text.cget("text"))

        gui._show_download_window(INFO_NOSIZE)           # 两边都没有 → indeterminate
        assert str(gui._dl_bar.cget("mode")) == "indeterminate", "初始就该自转"
        gui._on_download_progress(3 * MB, None)
        assert str(gui._dl_bar.cget("mode")) == "indeterminate"
        txt = str(gui._dl_text.cget("text"))
        assert txt == "已下载 3.0 MB，请稍等。", f"无总量文案不对：{txt!r}"
        print("  ✓ 进度总量三档：Content-Length 优先 / exe_size 兜底 / indeterminate 不崩")
    finally:
        _destroy(gui)


def test_download_error_then_retry_and_skip() -> None:
    """下载失败 → 提示带「重试」+ 残留清理；点重试自动再下成功；点取消=暂时跳过并关窗。"""
    from tkinter import messagebox as mb

    old_mode, old_exe = uc.update_mode, sys.executable
    uc.update_mode = lambda: "frozen"
    fake_dir = OUT / "frozen_err"
    fake_dir.mkdir(parents=True, exist_ok=True)
    sys.executable = str(fake_dir / "VRChatLiveTranslate.exe")
    residue = fake_dir / EXE_NEW

    state = {"fail": True}

    def fake_dl(info, dest_dir, timeout=None, progress=None):  # noqa: ANN001, ANN201
        if state["fail"]:
            state["fail"] = False
            Path(dest_dir, EXE_NEW).write_bytes(b"partial")     # 模拟半截残留
            raise uc.UpdateCheckError("网络不可达：timed out")
        return Path(dest_dir) / EXE_NEW

    rc_calls: list = []
    orig_rc = mb.askretrycancel
    gui = _make_gui()
    try:
        gui._update_downloader = fake_dl
        mb.askretrycancel = lambda *a, **k: rc_calls.append(a) or True   # 用户点「重试」
        gui._on_update_now(INFO)
        ok = _pump_until(gui, lambda: len(rc_calls) == 1)
        assert ok, "下载失败后没弹带「重试」的提示"
        args = rc_calls[0]
        assert args[0] == "下载没有成功" and "重试" in str(args[1]), f"错误提示不对：{args!r}"
        assert not residue.exists(), "失败残留 .new 没被清理"
        ok = _pump_until(gui, lambda: gui._dl_new_exe is not None)
        assert ok, "重试后没走到完成态"
        assert gui._dl_win.title() == "下载完成"

        # 再失败一次，这次用户点「取消」= 暂时跳过 → 进度窗关闭
        state["fail"] = True
        mb.askretrycancel = lambda *a, **k: rc_calls.append(a) or False
        gui._on_update_now(INFO)
        ok = _pump_until(gui, lambda: len(rc_calls) == 2)
        assert ok, "第二次失败没弹提示"
        assert gui._dl_win is None, "点「取消」后进度窗没关"
        assert not residue.exists(), "第二次失败残留没清理"
        print("  ✓ 下载失败：提示带「重试」、残留清理；重试成功到完成态；取消=暂时跳过关窗")
    finally:
        mb.askretrycancel = orig_rc
        uc.update_mode = old_mode
        sys.executable = old_exe
        residue.unlink(missing_ok=True)
        _destroy(gui)


def test_no_dialog_on_latest_ignored_error() -> None:
    """无更新/被忽略/检查失败 → 不弹窗；日志里有对应 [update] 行（禁静默降级）。"""
    gui = _make_gui()
    try:
        gui._update_checker = _fake_latest
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._run_update_check(manual=False)
            gui._poll()
        assert gui._update_win is None, "已是最新还弹窗"
        assert "[update] 已是最新" in buf.getvalue(), f"缺留痕：{buf.getvalue()!r}"

        gui._update_checker = lambda ver, cfg: ("ignored", INFO)
        gui._run_update_check(manual=False)
        gui._poll()
        assert gui._update_win is None, "被忽略的版本还弹窗"

        def fake_err(ver, cfg):  # noqa: ANN001, ANN201
            raise uc.UpdateCheckError("网络不可达：timed out")

        gui._update_checker = fake_err
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._run_update_check(manual=False)
            gui._poll()
        assert gui._update_win is None, "检查失败还弹窗"
        assert "[update] 检查失败" in buf.getvalue() and "网络不可达" in buf.getvalue(), \
            f"失败没留痕：{buf.getvalue()!r}"
        print("  ✓ 无更新/被忽略/检查失败 → 不弹窗，[update] 日志逐分支留痕")
    finally:
        _destroy(gui)


def test_settings_manual_check_button() -> None:
    """设置弹窗「软件更新」区：手动按钮有反应（检查中/已是最新/失败原因 + 防连点）。"""
    gui = _make_gui()
    try:
        btn = gui._update_check_btn
        assert str(btn.cget("text")) == "检查更新", "设置区缺「检查更新」按钮"
        assert f"当前版本 v{__version__}" in str(gui._update_info.cget("text")), \
            "结果标签初始文本不对"

        gui._update_checker = _fake_latest
        btn.invoke()
        ok = _pump_until(gui, lambda: "已是最新" in str(gui._update_info.cget("text")))
        assert ok, "手动检查后结果标签没更新"
        assert "9.9.9" in str(gui._update_info.cget("text"))
        assert str(btn.cget("text")) == "检查更新", "结果回来后按钮没恢复"
        assert gui._update_win is None, "已是最新还弹窗"

        # 防连点 + 检查中状态
        gate = threading.Event()
        calls = {"n": 0}

        def fake_slow(ver, cfg):  # noqa: ANN001, ANN201
            calls["n"] += 1
            gate.wait(5)
            return "latest", INFO

        gui._update_checker = fake_slow
        btn.invoke()
        ok = _pump_until(gui, lambda: str(btn.cget("text")) == "检查中…", timeout=2)
        assert ok and btn.instate(["disabled"]), "检查中按钮没置灰"
        assert "正在检查更新" in str(gui._update_info.cget("text"))
        btn.invoke()      # 置灰下 invoke 不触发；就算触发了，_update_check_running 也挡着
        assert calls["n"] == 1, "连点起了第二个检查线程"
        gate.set()
        ok = _pump_until(gui, lambda: str(btn.cget("text")) == "检查更新"
                         and not gui._update_check_running)
        assert ok, "检查完成后按钮没恢复"
        assert calls["n"] == 1

        def fake_err(ver, cfg):  # noqa: ANN001, ANN201
            raise uc.UpdateCheckError("GitHub 限流（未认证 60 次/小时），稍后再试")

        gui._update_checker = fake_err
        btn.invoke()
        ok = _pump_until(gui, lambda: "检查失败" in str(gui._update_info.cget("text")))
        assert ok, "失败原因没显示在结果标签"
        assert "限流" in str(gui._update_info.cget("text"))
        assert gui._update_win is None, "检查失败还弹窗"
        print("  ✓ 设置弹窗手动「检查更新」：检查中/已是最新/失败原因 + 防连点")
    finally:
        _destroy(gui)


def test_auto_check_once_per_session() -> None:
    """启动自动检查一次会话只查一次；手动检查不受一次限制。"""
    gui = _make_gui()
    try:
        calls = {"n": 0}

        def fake(ver, cfg):  # noqa: ANN001, ANN201
            calls["n"] += 1
            return "latest", INFO

        gui._update_checker = fake
        gui._schedule_update_check()                     # 自动：第一次放行
        ok = _pump_until(gui, lambda: calls["n"] >= 1 and not gui._update_check_running)
        assert ok, "第一次自动检查没跑"
        gui._schedule_update_check()                     # 自动：第二次被「一次会话一次」挡下
        gui._poll()
        time.sleep(0.1)
        gui._poll()
        assert calls["n"] == 1, "一次会话自动查了两次"
        gui._schedule_update_check(manual=True)          # 手动不受限
        ok = _pump_until(gui, lambda: calls["n"] >= 2 and not gui._update_check_running)
        assert ok, "手动检查没跑"
        assert calls["n"] == 2
        print("  ✓ 启动自动检查一次会话只查一次；手动检查不受限")
    finally:
        _destroy(gui)


def test_ui_texts_have_no_forbidden_words() -> None:
    """UI 禁词守卫：弹窗/进度窗/完成态/错误提示/设置区文案，一律不许出现技术词。"""
    from tkinter import messagebox as mb

    recorded: list[str] = []
    orig_si, orig_rc, orig_oc = mb.showinfo, mb.askretrycancel, mb.askokcancel
    mb.showinfo = lambda *a, **k: recorded.extend(str(x) for x in a) or True
    mb.askretrycancel = lambda *a, **k: recorded.extend(str(x) for x in a) or False
    mb.askokcancel = lambda *a, **k: recorded.extend(str(x) for x in a) or False
    old_exe = sys.executable
    fake_dir = OUT / "frozen_words"
    fake_dir.mkdir(parents=True, exist_ok=True)
    sys.executable = str(fake_dir / "VRChatLiveTranslate.exe")
    gui = _make_gui()
    try:
        texts: list[str] = []
        gui._show_update_dialog(INFO)                    # ① 三按钮弹窗
        texts += _walk_texts(gui._update_win) + [gui._update_win.title()]
        gui._on_update_now(INFO)                         # 源码指引 messagebox（记录）
        gui._show_download_window(INFO)                  # ② 下载中
        texts += _walk_texts(gui._dl_win) + [gui._dl_win.title()]
        gui._on_download_progress(5 * MB, None)
        texts.append(str(gui._dl_text.cget("text")))
        gui._on_download_done(OUT / EXE_NEW)             # ③ 完成态
        texts += _walk_texts(gui._dl_win) + [gui._dl_win.title()]
        _btn(gui._dl_win, "立即重启并更新").invoke()     # 未接线提示（记录）
        _btn(gui._dl_win, "稍后更新").invoke()
        gui._on_download_error("网络不可达：测试")        # 错误提示（记录）→ 取消 → 关窗
        texts.append(str(gui._update_info.cget("text")))  # 设置区
        texts.append(str(gui._update_check_btn.cget("text")))
        texts += recorded
        assert len(texts) > 10, f"采集到的文案太少，守卫形同虚设：{len(texts)}"
        for t in texts:
            low = t.lower()
            for w in FORBIDDEN_WORDS:
                assert w not in low, f"UI 文案出现禁词 {w!r}：{t!r}"
        print(f"  ✓ UI 禁词守卫：{len(texts)} 条文案均无 exe/PID/SHA256/bat/进程/校验/asset")
    finally:
        mb.showinfo, mb.askretrycancel, mb.askokcancel = orig_si, orig_rc, orig_oc
        sys.executable = old_exe
        _destroy(gui)


def main() -> int:
    # config.yaml 备份/还原（照抄 tests/test_config_save.py 的模式）：
    # 「不再提示这个版本」会真写它，测完必须原样放回；CI 上没有它就先按模板生成、测完删掉。
    cfg_existed = DEFAULT_CONFIG.exists()
    cfg_before = DEFAULT_CONFIG.read_text(encoding="utf-8") if cfg_existed else None
    if not cfg_existed:
        DEFAULT_CONFIG.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                                  encoding="utf-8")
        print("config.yaml 不存在 → 已从 config.example.yaml 生成（测试结束会删掉）")

    tests = [
        test_update_dialog_and_ignore,
        test_later_snooze_session_only,
        test_update_now_source_mode_shows_guidance,
        test_download_window_progress_and_done,
        test_download_total_fallback_and_indeterminate,
        test_download_error_then_retry_and_skip,
        test_no_dialog_on_latest_ignored_error,
        test_settings_manual_check_button,
        test_auto_check_once_per_session,
        test_ui_texts_have_no_forbidden_words,
    ]
    print("更新弹窗/下载窗/设置区接线验收：")
    failed = 0
    try:
        for t in tests:
            try:
                t()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                import traceback
                print(f"  ❌ {t.__name__}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
    finally:
        if cfg_before is not None:
            DEFAULT_CONFIG.write_text(cfg_before, encoding="utf-8")
        else:
            DEFAULT_CONFIG.unlink(missing_ok=True)
        print("已还原 config.yaml")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
