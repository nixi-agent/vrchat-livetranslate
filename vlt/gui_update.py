"""更新检查 / 下载 / 赞助对话框 + API key 管理。

从 ``vlt.gui.TranslationGUI`` 拆出来的独立函数集合。所有需要 Tk 控件和状态的对象
通过 ``gui`` 参数（TranslationGUI 实例）传入，不使用 self。

为什么放在这里（而不是留在 gui.py）：
  · gui.py 已经超过 5000 行，把「更新 + 赞助 + key 管理」这三块相对独立的逻辑
    抽出来后便于阅读与维护；
  · 后续再拆 gui_settings / gui_audio / gui_voice 时，本模块就是参考模板。

⚠️ 本模块 **不许** import vlt.gui（防循环引用）。
测试中对 ``gui_mod.webbrowser`` / ``gui_mod.subprocess`` / ``gui_mod.APP_DIR``
等名字的打桩通过 ``_m(gui)`` 访问 gui 模块命名空间，确保猴子补丁生效。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__, endpoints, update_check
from .config import DEFAULT_CONFIG
from .i18n import t
from .ui_text import (
    _DownloadCancelled,
    _dir_writable,
)
from .ui_theme import (
    ACCENT_HOVER,
    BORDER,
    PANEL,
    SPONSOR_QR_SIZE,
)
from . import ui_tk
from .ui_tk import _char_width_for
# 字体常量运行时取 `ui_tk.FONT_*`（import 期快照在 Linux 上会静默回落，见 issue #61）


def _m(gui):
    """返回 ``vlt.gui`` 模块引用 —— 测试对 ``gui_mod.webbrowser`` / ``gui_mod.subprocess``
    / ``gui_mod.APP_DIR`` / ``gui_mod._sponsor_qr_specs`` / ``gui_mod.updater_env``
    的打桩必须通过模块命名空间生效，不能走本模块的直接 import。"""
    return sys.modules[gui.__class__.__module__]


# ================================================================ 常量
# gui.py 同步重导出的名字（测试 ``from vlt.gui import SPONSOR_URL`` 等仍有效）。
# 这里定义一份，gui.py 通过 ``from .gui_update import SPONSOR_URL as SPONSOR_URL`` 重导出。

# ================================================================ 赞助对话框

def open_kofi(gui) -> None:
    """打开 Ko-fi 赞助页面（独立成小函数：测试打桩它，绝不真开浏览器）。"""
    try:
        _m(gui).webbrowser.open(_m(gui).SPONSOR_URL)
        print(f"[gui] 已打开赞助页面 {_m(gui).SPONSOR_URL}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] ⚠️ 打不开浏览器：{type(exc).__name__}: {exc}", flush=True)
        gui._set_status("warn", t("打不开浏览器，请手动访问 {url}", url=_m(gui).SPONSOR_URL))


def open_sponsor(gui) -> None:
    """打开赞助弹窗；已经开着时只聚焦/置顶已有窗口，绝不 new 第二个。"""
    if gui._sponsor_win is not None:
        try:
            if gui._sponsor_win.winfo_exists():
                gui._sponsor_win.deiconify()
                gui._sponsor_win.lift()
                gui._sponsor_win.focus_set()
                return
        except Exception:  # noqa: BLE001
            pass
        gui._sponsor_win = None
    try:
        build_sponsor_dialog(gui)
    except Exception as exc:  # noqa: BLE001
        # 辅助功能挂掉绝不能拖垮主窗口：留痕 + 状态栏提示，不往上抛
        gui._sponsor_win = None
        print(f"[gui] ⚠️ 赞助弹窗创建失败（不影响主功能）："
              f"{type(exc).__name__}: {exc}", flush=True)
        gui._set_status("warn", t("赞助弹窗打不开：{msg}", msg=exc))


def build_sponsor_dialog(gui) -> None:
    """赞助弹窗：Ko-fi 按钮 + 可复制地址 + 两张收款码（微信/支付宝）。

    深色主题完全复用主窗口的颜色常量与 ttk style，不自创颜色。
    收款码必须**等比**缩放（LANCZOS，目标边长 240px）——拉变形就扫不出来。
    """
    win = tk.Toplevel(gui._root)
    win.title(t("赞助"))
    win.configure(bg=PANEL)
    win.transient(gui._root)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: close_sponsor(gui))
    win.bind("<Escape>", lambda _e: close_sponsor(gui))
    gui._sponsor_win = win
    gui._sponsor_imgs = []
    gui._sponsor_qr_labels = []

    body = ttk.Frame(win, padding=(20, 16, 20, 14))
    body.pack(fill=tk.BOTH, expand=True)

    ttk.Label(body, text=t("☕ 请我喝一杯"),
              font=ui_tk.FONT_BOLD_LG).pack(anchor=tk.CENTER)

    ttk.Button(body, text=t("打开 Ko-fi 赞助页面"), style="Accent.TButton",
               command=lambda: open_kofi(gui)).pack(anchor=tk.CENTER, pady=(12, 14))

    # 弹窗里**不放** Ko-fi 地址：蓝按钮点一下就直接开浏览器了，再摆一行地址纯属多余
    # （用户口径：2026-09-25）。地址并没有丢：open_kofi 打不开浏览器时会把它写进状态栏。

    # 两张收款码并排，各带文字标签，码和码之间留间距
    qr_row = ttk.Frame(body)
    qr_row.pack(anchor=tk.CENTER)
    for label, path in _m(gui)._sponsor_qr_specs():
        cell = ttk.Frame(qr_row)
        cell.pack(side=tk.LEFT, padx=14)
        _load_qr(gui, cell, path).pack(anchor=tk.CENTER)
        ttk.Label(cell, text=label, style="Dim.TLabel").pack(anchor=tk.CENTER,
                                                             pady=(6, 0))

    ttk.Label(body, text=t("扫码支持 · 你给的钱会变成 API token，然后被我烧掉"),
              style="Dim.TLabel").pack(anchor=tk.CENTER, pady=(14, 4))
    from .ui_tk import FONT_UI as _FONT_UI
    ttk.Button(body, text=t("关闭"), width=_char_width_for(t("关闭"), _FONT_UI, 8),
               command=lambda: close_sponsor(gui)).pack(anchor=tk.CENTER, pady=(8, 0))

    # 定位到主窗口附近 + 深色标题栏（与设置弹窗同一套做法）
    win.update_idletasks()
    rx, ry = gui._root.winfo_x(), gui._root.winfo_y()
    rw = gui._root.winfo_width()
    ww = win.winfo_reqwidth()
    win.geometry(f"+{rx + max((rw - ww) // 2, 20)}+{ry + 48}")
    gui._apply_dark_titlebar(win)


def _load_qr(gui, parent, path: Path):  # noqa: ANN001
    """等比缩放加载一张收款码；图片缺失/加载失败时降级成一行文字 + WARN 日志，
    绝不抛异常 —— 辅助功能挂掉不能拖垮主窗口。"""
    try:
        from PIL import Image, ImageTk

        if not path.exists():
            raise FileNotFoundError(path)
        im = Image.open(path)
        im.thumbnail((SPONSOR_QR_SIZE, SPONSOR_QR_SIZE), Image.LANCZOS)  # 等比，绝不拉伸
        photo = ImageTk.PhotoImage(im)
        gui._sponsor_imgs.append(photo)      # 留引用防 GC
        lbl = tk.Label(parent, image=photo, bg=PANEL, bd=0,
                       highlightthickness=1, highlightbackground=BORDER)
        gui._sponsor_qr_labels.append(lbl)
        return lbl
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] ⚠️ 收款码加载失败（已降级为文字提示）：{path} "
              f"→ {type(exc).__name__}: {exc}", flush=True)
        return ttk.Label(parent, text=t("二维码图片缺失"), style="Dim.TLabel")


def close_sponsor(gui) -> None:
    win, gui._sponsor_win = gui._sponsor_win, None
    gui._sponsor_imgs = []
    gui._sponsor_qr_labels = []
    if win is not None:
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass


# ================================================================ 更新检查

def schedule_update_check(gui, manual: bool = False) -> None:
    """后台线程检查更新。manual=False=启动自动检查（一次会话一次）；
    manual=True=设置弹窗里点的（不受一次限制，但防连点：进行中再点直接返回）。
    headless 自检模式直接跳过（不起网络线程）。"""
    if gui._headless or gui._update_check_running:
        return
    if not manual:
        if gui._update_check_done:
            return
        gui._update_check_done = True
    gui._update_check_running = True
    if manual and hasattr(gui, "_update_check_btn"):
        gui._update_check_btn.state(["disabled"])
        gui._update_check_btn.configure(text=t("检查中…"))
        gui._update_info.configure(text=t("正在检查更新…"))
    threading.Thread(target=lambda: run_update_check(gui, manual), daemon=True).start()


def run_update_check(gui, manual: bool) -> None:
    """工作线程体：调 check_for_updates，结果塞 gui._q 回主线程。
    顶层 except 吞掉一切并留 [update] 日志 —— 检查更新绝不允许影响翻译主流程。"""
    err = None
    try:
        status, info = gui._update_checker(__version__, DEFAULT_CONFIG)
    except Exception as exc:  # noqa: BLE001 — 失败只留痕，绝不甩给启动/翻译流程
        status, info = "error", None
        err = f"{type(exc).__name__}: {exc}"
        print(f"[update] 检查失败：{err}", flush=True)
    gui._q.put(("update_check", status, info, manual, err))


def on_update_check_result(gui, status: str, info, manual: bool, err: str | None) -> None:
    """主线程（_poll 分支）：有更新 → 弹三按钮窗；其余只留痕/更新设置区标签，不弹窗。"""
    gui._update_check_running = False
    if hasattr(gui, "_update_check_btn"):
        gui._update_check_btn.state(["!disabled"])
        gui._update_check_btn.configure(text=t("检查更新"))
    if status == "update" and info is not None:
        # GUI 入口复核忽略列表（双保险：check_for_updates 判过一次，但测试/手动路径
        # 可能绕过它直接给结果 —— 点过「不再提示这个版本」的，说什么也不再弹）
        if info.version in update_check.load_ignored_versions(DEFAULT_CONFIG):
            print(f"[update] v{info.version} 在忽略列表，跳过（不弹窗）", flush=True)
            if manual:
                gui._update_info.configure(
                    text=t("v{ver} 已设为「不再提示这个版本」", ver=info.version))
            return
        if gui._update_snoozed and not manual:
            print("[update] 本会话已选「下次再说」，自动提示不再弹（设置里可手动检查）",
                  flush=True)
            return
        show_update_dialog(gui, info)
        print("[update] 已弹出「发现新版本」提示窗", flush=True)
        if manual:
            gui._update_info.configure(
                text=t("发现新版本 v{new}（当前 v{cur}）",
                       new=info.version, cur=__version__))
    elif status == "latest" and info is not None:
        if manual:
            gui._update_info.configure(text=t("已是最新 v{ver} ✅", ver=info.version))
    elif status == "ignored" and info is not None:
        if manual:
            gui._update_info.configure(
                text=t("v{ver} 已设为「不再提示这个版本」", ver=info.version))
    else:  # error：自动检查对用户完全无感（只留日志）；手动检查把原因显示在设置区
        if manual:
            reason = err or t("原因见日志")
            gui._update_info.configure(
                text=t("检查失败：{reason}。可以点「检查更新」重试。", reason=reason))


# ---------------------------------------------------------------- 三按钮弹窗（发现新版本）

def show_update_dialog(gui, info) -> None:
    """懒建 Toplevel（非模态：transient + lift，不 grab_set —— 翻译不能被卡住）。
    文案逐字照抄计划「文案 checklist ①」。已存在弹窗时先销毁再建（防重复）。"""
    close_update_dialog(gui)
    win = tk.Toplevel(gui._root)
    win.title(t("发现新版本"))
    win.configure(bg=PANEL)
    win.transient(gui._root)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: on_update_later(gui))
    win.bind("<Escape>", lambda _e: on_update_later(gui))
    gui._update_win = win

    from .ui_tk import FONT_UI as _FONT_UI
    from .ui_tk import FONT_BOLD_MD

    body = ttk.Frame(win, padding=(20, 16, 20, 14))
    body.pack(fill=tk.BOTH, expand=True)
    ttk.Label(body, text=t("发现新版本"),
              font=FONT_BOLD_MD).pack(anchor=tk.W)
    ttk.Label(body,
              text=t("VRChat Live Translate 有新版本了。"
                     "现在更新只要一两分钟，不影响你正在进行的翻译。"),
              wraplength=380, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))
    link = tk.Label(body, text=t("看看这次更新了什么"), fg=ACCENT_HOVER, bg=PANEL,
                    cursor="hand2", font=_FONT_UI)
    link.pack(anchor=tk.W, pady=(8, 0))
    link.bind("<Button-1>", lambda _e: open_release_page(gui, info.html_url))

    btns = ttk.Frame(body)
    btns.pack(fill=tk.X, pady=(16, 0))
    now = ttk.Button(btns, text=t("立即更新"), style="Accent.TButton",
                     command=lambda: on_update_now(gui, info))
    now.pack(side=tk.LEFT)
    ttk.Button(btns, text=t("下次再说"),
               command=lambda: on_update_later(gui)).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(btns, text=t("不再提示这个版本"),
               command=lambda: on_update_ignore(gui, info)).pack(side=tk.LEFT,
                                                                   padx=(8, 0))
    now.focus_set()                       # 默认按钮：回车/焦点都落在「立即更新」

    win.update_idletasks()
    rx, ry = gui._root.winfo_x(), gui._root.winfo_y()
    rw = gui._root.winfo_width()
    win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 60}")
    gui._apply_dark_titlebar(win)
    win.lift()


def close_update_dialog(gui) -> None:
    win, gui._update_win = gui._update_win, None
    if win is not None:
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass


def open_release_page(gui, url: str) -> None:
    """用默认浏览器打开 Release 页（「看看这次更新了什么」）；失败只留痕 + 状态栏提示。"""
    try:
        _m(gui).webbrowser.open(url)
        print(f"[update] 已打开 Release 页面 {url}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[update] ⚠️ 打不开浏览器：{type(exc).__name__}: {exc}", flush=True)
        gui._set_status("warn", t("打不开浏览器，请手动访问 {url}", url=url))


def on_update_ignore(gui, info) -> None:
    """「不再提示这个版本」→ 写 config.yaml 的 ui.update_ignored，关窗，该版本永不再提。"""
    try:
        update_check.add_ignored_version(DEFAULT_CONFIG, info.version)
        print(f"[update] v{info.version} 已写入忽略列表（config.yaml ui.update_ignored），"
              f"这个版本不再提示", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[update] ⚠️ 写入忽略列表失败：{type(exc).__name__}: {exc}", flush=True)
        gui._set_status("warn", t("「不再提示」没存下来：{msg}", msg=exc))
    close_update_dialog(gui)


def on_update_later(gui) -> None:
    """「下次再说」→ 本会话不再自动弹（不落盘），下次启动照常检查。"""
    gui._update_snoozed = True
    print("[update] 用户选择「下次再说」：本会话不再自动弹（不落盘，下次启动照查）",
          flush=True)
    close_update_dialog(gui)


def on_update_now(gui, info) -> None:
    """「立即更新」：源码运行给指引（不自更新）；打包 exe / AppImage 走两段式 —— 先开下载进度窗。"""
    if not update_check.can_self_update():
        open_page = messagebox.askokcancel(
            t("如何更新"),
            t("你现在运行的是源码版，不能自动更新。\n\n"
              "· 会用 git：在仓库目录跑 git pull 就是最新版；\n"
              "· 或者点「确定」打开新版本下载页，下载安装包。\n\n"
              "点「取消」先不更新。"),
            parent=gui._update_win)
        if open_page:
            open_release_page(gui, info.html_url)
        print("[update] 源码运行：已给出更新指引（git pull / 下载页），不做自更新",
              flush=True)
        return
    target = update_check.update_target_path()
    if target is None or not _dir_writable(target.parent):
        open_page = messagebox.askokcancel(
            t("无法自动更新"),
            t("程序所在的位置不允许写入（比如放在 Program Files）。\n\n"
              "点「确定」打开下载页，自己下载新版本；点「取消」先不更新。"),
            parent=gui._update_win)
        if open_page:
            open_release_page(gui, info.html_url)
        print(f"[update] 安装目录不可写（{target.parent if target else '?'}），"
              f"转为手动下载指引", flush=True)
        return
    # 这个版本之前已经下载好（点过「稍后更新」/上次没换完就被关掉）→
    # 复验通过直接给更新入口，30MB+ 不白下（硬要求：用残留前必须重新校验）
    try:
        hit = update_check.check_pending_download(target, __version__)
    except Exception as exc:  # noqa: BLE001
        hit = None
        print(f"[update] ⚠️ 待更新文件复核失败：{type(exc).__name__}: {exc}"
              f"（按重新下载处理）", flush=True)
    if hit is not None and hit[0] == info.version:
        close_update_dialog(gui)
        show_download_window(gui, info)
        enter_download_done_state(gui, update_check.pending_new_asset(target))
        return
    close_update_dialog(gui)
    show_download_window(gui, info)
    start_download(gui, info)


# ---------------------------------------------------------------- 下载进度窗

def show_download_window(gui, info) -> None:
    """懒建懒销毁 Toplevel（transient + lift，非模态 —— 下载期间翻译照跑）。
    文案逐字照抄「文案 checklist ②」；完成后切「完成态」（checklist ③）。
    总量优先级：Content-Length（回调带）> ReleaseInfo.asset_size > indeterminate 只显示已下载量。"""
    close_download_window(gui)
    win = tk.Toplevel(gui._root)
    win.title(t("正在下载新版本"))
    win.configure(bg=PANEL)
    win.transient(gui._root)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: on_download_window_close(gui))
    gui._dl_win = win
    gui._dl_info = info
    gui._dl_new_exe = None

    body = ttk.Frame(win, padding=(20, 16, 20, 14))
    body.pack(fill=tk.BOTH, expand=True)
    gui._dl_bar = ttk.Progressbar(body, mode="determinate", length=380,
                                  maximum=100.0, value=0.0)
    gui._dl_bar.pack(fill=tk.X)
    if info.asset_size:
        text = t("已下载 {done} / 约 {total} MB，一般 1–3 分钟就好。下载期间可以正常翻译。",
                 done="0.0", total=f"{info.asset_size / 1048576:.1f}")
    else:
        gui._dl_bar.configure(mode="indeterminate")
        gui._dl_bar.start(14)
        text = t("已下载 {done} MB，请稍等。", done="0.0")
    gui._dl_text = ttk.Label(body, text=text, wraplength=380, justify=tk.LEFT)
    gui._dl_text.pack(anchor=tk.W, pady=(10, 0))
    gui._dl_note = ttk.Label(body, text=t("点右上角关闭会取消下载，下次可以再下。"),
                             style="Muted.TLabel")
    gui._dl_note.pack(anchor=tk.W, pady=(10, 0))
    # 「打开下载页自己下」：下载失败/完成态都能点到的第三条出路（网络实在不稳时自己下）
    from .ui_tk import FONT_UI as _FONT_UI
    gui._dl_link = tk.Label(body, text=t("打开下载页自己下"), fg=ACCENT_HOVER, bg=PANEL,
                            cursor="hand2", font=_FONT_UI)
    gui._dl_link.pack(anchor=tk.W, pady=(6, 0))
    gui._dl_link.bind("<Button-1>", lambda _e: open_download_page(gui))
    gui._dl_btn_frame = ttk.Frame(body)   # 完成态才放按钮（checklist ③）
    gui._dl_btn_frame.pack(fill=tk.X, pady=(14, 0))

    win.update_idletasks()
    rx, ry = gui._root.winfo_x(), gui._root.winfo_y()
    rw = gui._root.winfo_width()
    win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 80}")
    gui._apply_dark_titlebar(win)
    win.lift()


def close_download_window(gui) -> None:
    win, gui._dl_win = gui._dl_win, None
    gui._dl_bar = gui._dl_text = gui._dl_note = gui._dl_btn_frame = None
    gui._dl_reload_btn = gui._dl_postpone_btn = gui._dl_link = None
    if win is not None:
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass


def open_download_page(gui) -> None:
    """下载窗里的「打开下载页自己下」（与三按钮弹窗的链接同一写法）。"""
    url = gui._dl_info.html_url if gui._dl_info else ""
    if url:
        open_release_page(gui, url)


def on_download_window_close(gui) -> None:
    """下载中关窗 = 取消下载并清理残留（下载线程在下一个 chunk 边界自己中断）。"""
    if gui._dl_downloading and gui._dl_cancel is not None:
        gui._dl_cancel.set()
        print("[update] 用户关闭下载窗：取消下载", flush=True)
    elif gui._dl_new_exe is not None:
        print("[update] 下载窗已关闭：下载好的新版本文件保留，下次可继续用", flush=True)
    close_download_window(gui)


def start_download(gui, info) -> None:
    """守护线程跑 download_and_verify。线程纪律（硬约束）：progress 回调绝不直接
    碰 Tk 控件，只节流后（每 ~100ms 至多一条，允许丢旧留新）塞 gui._q，
    由 _poll() 在主线程更新控件 —— 与设备扫描同一模式。"""
    gui._dl_cancel = threading.Event()
    gui._dl_last_push = 0.0
    gui._dl_downloading = True
    # 安装文件（exe / AppImage）同目录：同卷 rename 才近原子。拿不到目标（不该发生：
    # 能走到这儿说明 can_self_update() 为真）就退回 APP_DIR，绝不让它崩在 None 上。
    target = update_check.update_target_path() or (_m(gui).APP_DIR / update_check.asset_name_for())
    dest_dir = target.parent

    def _progress(done: int, total: int | None) -> None:
        if gui._dl_cancel is not None and gui._dl_cancel.is_set():
            raise _DownloadCancelled()
        now = time.monotonic()
        is_final = bool(total) and done >= total
        if not is_final and now - gui._dl_last_push < gui._dl_throttle_s:
            return
        gui._dl_last_push = now
        gui._q.put(("update_progress", done, total))

    def _work() -> None:
        try:
            # 第二次机会（评审 §3）：主源上 37 MB 产物下不动时，用**同一版本**的 GitHub 附件
            # 再试一次 —— 大陆最常见的失败形态就是「清单拿得到、产物断在半路」。
            # 决定「要不要回退」在这里传下去；版本一致性、留痕、残留清理都在 update_check 那边。
            new_exe = gui._update_downloader(
                info, dest_dir, progress=_progress,
                fallback=lambda: update_check.github_fallback(info))
        except _DownloadCancelled:
            print("[update] 下载已取消（残留已清理）", flush=True)
            return
        except Exception as exc:  # noqa: BLE001 — 失败走统一错误分支，绝不静默
            gui._q.put(("update_download_error", str(exc)))
            return
        gui._q.put(("update_download_done", new_exe))

    threading.Thread(target=_work, daemon=True).start()


def on_download_progress(gui, done: int, total: int | None) -> None:
    """主线程：更新进度条与文本；total=None 且 asset_size 也没有 → indeterminate。"""
    if gui._dl_win is None or gui._dl_bar is None or gui._dl_text is None:
        return
    try:
        if not gui._dl_win.winfo_exists():
            return
    except Exception:  # noqa: BLE001
        return
    total = total or (gui._dl_info.asset_size if gui._dl_info else None)
    if total:
        if str(gui._dl_bar.cget("mode")) != "determinate":
            gui._dl_bar.stop()
            gui._dl_bar.configure(mode="determinate")
        gui._dl_bar.configure(maximum=float(total), value=float(done))
        gui._dl_text.configure(
            text=t("已下载 {done} / 约 {total} MB，一般 1–3 分钟就好。下载期间可以正常翻译。",
                   done=f"{done / 1048576:.1f}", total=f"{total / 1048576:.1f}"))
    else:
        if str(gui._dl_bar.cget("mode")) != "indeterminate":
            gui._dl_bar.configure(mode="indeterminate")
            gui._dl_bar.start(14)
        gui._dl_text.configure(
            text=t("已下载 {done} MB，请稍等。", done=f"{done / 1048576:.1f}"))


def on_download_done(gui, new_exe) -> None:
    """主线程：下载+校验完成 → 切「完成态」（文案 checklist ③）。"""
    gui._dl_downloading = False
    if gui._dl_win is None:
        # 用户在最后一刻关了窗：按取消处理，不留来路不明的下载物（json 一并清）
        try:
            Path(new_exe).unlink(missing_ok=True)
            (Path(new_exe).parent / update_check.PENDING_JSON).unlink(missing_ok=True)
        except OSError:
            pass
        print("[update] 下载完成时窗口已关闭：按取消处理，下载物已删除", flush=True)
        return
    enter_download_done_state(gui, Path(new_exe))


def enter_download_done_state(gui, new_exe: Path) -> None:
    """下载窗切「完成态」（文案 checklist ③）：进度条 100%，按钮区出
    [立即重启并更新] [稍后更新]。下载完成与残留恢复（不重复下载）共用。"""
    gui._dl_new_exe = Path(new_exe)
    gui._dl_bar.stop()
    gui._dl_bar.configure(mode="determinate", maximum=1.0, value=1.0)
    gui._dl_win.title(t("下载完成"))
    gui._dl_text.configure(
        text=t("新版本已经准备好了。\n"
               "点「立即重启并更新」：关闭当前窗口、自动换上新版本并重新打开。\n"
               "点「稍后更新」：继续用现在的版本；等你关闭程序时会自动换好，下次打开就是新版。"))
    gui._dl_note.pack_forget()            # 已完成：「关窗会取消」的小字不再适用
    for child in gui._dl_btn_frame.winfo_children():   # 防重复进完成态时叠按钮
        child.destroy()
    gui._dl_reload_btn = ttk.Button(gui._dl_btn_frame, text=t("立即重启并更新"),
                                    style="Accent.TButton",
                                    command=lambda: on_reload_clicked(gui))
    gui._dl_reload_btn.pack(side=tk.LEFT)
    gui._dl_postpone_btn = ttk.Button(gui._dl_btn_frame, text=t("稍后更新"),
                                      command=lambda: on_postpone_clicked(gui))
    gui._dl_postpone_btn.pack(side=tk.LEFT, padx=(8, 0))
    gui._dl_reload_btn.focus_set()
    print(f"[update] 新版本已下载好（{new_exe}），等待用户选择何时更新", flush=True)


def on_download_error(gui, msg: str) -> None:
    """主线程：清理残留 → 留痕 → 错误提示带可点下一步（重试=默认 / 取消=暂时跳过）。"""
    gui._dl_downloading = False
    print(f"[update] 下载失败：{msg}", flush=True)
    if gui._dl_win is None:
        return                               # 用户已关窗取消，错误不必再烦他
    # 残留双保险（download_and_verify 失败时已清过一遍）
    target = update_check.update_target_path()
    if target is None:
        target = _m(gui).APP_DIR / update_check.asset_name_for()
    try:
        update_check.pending_new_asset(target).unlink(missing_ok=True)
    except OSError:
        pass
    retry = messagebox.askretrycancel(
        t("下载没有成功"),
        t("下载没有成功（网络可能不太稳定）。\n\n"
          "点「重试」再下载一次；点「取消」暂时跳过。\n"
          "（之后也可以到「设置 → 软件更新」再检查）"),
        parent=gui._dl_win)
    if retry and gui._dl_win is not None and gui._dl_info is not None:
        gui._dl_bar.configure(mode="determinate", maximum=100.0, value=0.0)
        gui._dl_text.configure(text=t("已下载 {done} MB，请稍等。", done="0.0"))
        print("[update] 用户选择重试下载", flush=True)
        start_download(gui, gui._dl_info)
    else:
        print("[update] 用户选择暂时跳过本次下载", flush=True)
        close_download_window(gui)


# ---------------------------------------------------------------- 阶段二：重载 / 稍后 / 退出时替换

def on_reload_clicked(gui) -> None:
    """「立即重启并更新」= 立刻替换 + 自动拉起新版（当前会话结束）。

    Windows：生成 bat（relaunch=True）→ 分离启动 → 走正常退出流程。
    AppImage：**直接换**（`os.replace`，运行中的旧文件是旧 inode，不受影响）→ 拉起新文件。
    任一步失败：留痕 + 恢复按钮 + 错误提示带可点下一步，绝不静默。"""
    if gui._reload_started:
        return                              # 防连点：已经安排上了
    new_exe, info = gui._dl_new_exe, gui._dl_info
    if new_exe is None or info is None:
        print("[update] ⚠️ 「立即重启并更新」在非完成态被触发，已忽略", flush=True)
        return
    gui._reload_started = True
    for btn in (gui._dl_reload_btn, gui._dl_postpone_btn):
        if btn is not None:
            try:
                btn.state(["disabled"])
            except Exception:  # noqa: BLE001
                pass
    if gui._dl_reload_btn is not None:
        gui._dl_reload_btn.configure(text=t("正在重启…"))
    try:
        if update_check.update_mode() == "appimage":
            target = update_check.update_target_path()
            if target is None:
                raise update_check.UpdateCheckError(
                    "找不到正在运行的 AppImage 文件（$APPIMAGE 没了？）")
            update_check.install_appimage(new_exe, target)
            relaunch_appimage(gui, target)
        else:
            exe = Path(sys.executable).resolve()
            bat = update_check.build_updater_bat(pid=os.getpid(), current_exe=exe,
                                                 new_exe=new_exe, relaunch=True)
            launch_updater_bat(gui, bat)
    except Exception as exc:  # noqa: BLE001 — 失败必须被用户看到，不许静默
        print(f"[update] ⚠️ 启动更新器失败：{type(exc).__name__}: {exc}"
              f"（下载好的新版本保留着，可以再点）", flush=True)
        gui._reload_started = False
        for btn in (gui._dl_reload_btn, gui._dl_postpone_btn):
            if btn is not None:
                try:
                    btn.state(["!disabled"])
                except Exception:  # noqa: BLE001
                    pass
        if gui._dl_reload_btn is not None:
            gui._dl_reload_btn.configure(text=t("立即重启并更新"))
        retry = messagebox.askretrycancel(
            t("更新没有成功"),
            t("更新没有成功，现在的版本不受影响，可以继续用。\n\n"
              "点「重试」再试一次；点「取消」先继续用现在的版本"
              "（窗口里也可以「打开下载页自己下」）。"),
            parent=gui._dl_win)
        if retry:
            print("[update] 用户选择重试「立即重启并更新」", flush=True)
            on_reload_clicked(gui)
        else:
            print("[update] 用户选择先继续用现在的版本（新版本已下载好，保留着）",
                  flush=True)
        return
    print(f"[update] 已安排替换并拉起新版，程序即将退出（重载路径，v{__version__} → "
          f"v{info.version}）", flush=True)
    gui._on_close()


def on_postpone_clicked(gui) -> None:
    """「稍后更新」= 继续用旧版、关下载窗；已下载且校验通过的新版本保留着（不删），
    正常退出时自动换好（绝不自动拉起）、下次打开就是新版。"""
    info = gui._dl_info
    if info is None:
        return
    mark_update_pending(gui, info)
    print(f"[update] 已下载 v{info.version}，将在退出时完成更新（不自动打开）", flush=True)
    close_download_window(gui)


def mark_update_pending(gui, info) -> None:
    """记下「正常退出时要换成新版本」（稍后更新 / 启动残留命中共用）。"""
    gui._update_pending_exit = True
    gui._update_pending_info = info


def launch_updater_bat(gui, bat_text: str) -> Path:
    """把更新器脚本写到程序同目录并分离启动（不等它跑完；它自己会等本程序退出再动手）。
    失败（权限/磁盘/没有 cmd）抛异常给调用方 —— 由调用方负责让用户看见。"""
    exe = Path(sys.executable).resolve()
    bat_path = exe.parent / "_update.bat"
    # bat 正文已是 CRLF：newline="" 关掉写入时的换行翻译，否则 \r\n 会变 \r\r\n
    bat_path.write_text(bat_text, encoding="ascii", newline="")
    flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "DETACHED_PROCESS", 0))
    _m(gui).subprocess.Popen(["cmd", "/c", "start", "", "/min", str(bat_path)],
                         creationflags=flags, env=_m(gui).updater_env())
    return bat_path


def relaunch_appimage(gui, appimage: Path) -> None:
    """分离启动刚换上的 AppImage（当前会话随后正常退出，不等待它结束）。

    ⚠️ 必须用 `updater_env()`（它会剥掉 APPIMAGE / APPDIR / OWD / ARGV0）：AppImage
    运行时看到 `APPDIR` 已设就**不会重新挂载**，新进程会去用父进程那个马上要消失的
    挂载点 —— 与 Windows 侧漏清 `_MEI*` 是同一类「更新完没再打开」。
    cwd 落在 AppImage 自己所在目录：绝不能是旧挂载点里的路径（那会随进程一起消失）。
    """
    _m(gui).subprocess.Popen([str(appimage)], env=_m(gui).updater_env(), start_new_session=True,
                         cwd=str(Path(appimage).parent))


def maybe_replace_on_exit(gui) -> None:
    """正常退出时替换（【稍后】路径的另一半）：复验 → 替换 → 退出（绝不拉起新进程，下次
    用户自己打开就是新版）。Windows 走 bat（relaunch=False）；AppImage 直接 rename 顶替。"""
    if gui._reload_started:
        return                    # 重载路径已安排了带拉起的替换，别重复安排
    if not gui._update_pending_exit:
        return
    if not update_check.can_self_update():
        return                    # 源码运行不做自更新（正常也走不到这）
    target = update_check.update_target_path()
    if target is None:
        return
    try:
        # 硬要求：退出前再复验一次（防下载后文件被改坏/杀软动过），不通过就不换
        hit = update_check.check_pending_download(target, __version__)
    except Exception as exc:  # noqa: BLE001
        print(f"[update] 跳过退出时替换（复核异常：{type(exc).__name__}: {exc}）",
              flush=True)
        return
    if hit is None:
        print("[update] 跳过退出时替换（待更新文件复核未通过，残留已按规则清理）",
              flush=True)
        return
    version = hit[0]
    try:
        if update_check.update_mode() == "appimage":
            update_check.install_appimage(update_check.pending_new_asset(target), target)
        else:
            bat = update_check.build_updater_bat(
                pid=os.getpid(), current_exe=target,
                new_exe=update_check.pending_new_asset(target), relaunch=False)
            launch_updater_bat(gui, bat)
    except Exception as exc:  # noqa: BLE001 — 失败要被用户看到，不能静默退出
        print(f"[update] ⚠️ 退出时替换安排失败：{type(exc).__name__}: {exc}"
              f"（新版本已下载好并保留，下次启动会再给更新入口）", flush=True)
        offer_manual_download(gui,
            t("更新没有成功，现在的版本不受影响，下次打开还是它。\n\n"
              "点「确定」打开下载页自己下；点「取消」直接退出。"))
        return
    done = ("已就地替换" if update_check.update_mode() == "appimage" else "已安排替换")
    print(f"[update] 退出时{done}（不自动拉起），下次打开就是 v{version}", flush=True)


def offer_manual_download(gui, body: str) -> None:
    """自动替换没成功时的统一退路：问一句 →「确定」就打开下载页自己下。

    `body` 是给用户看的话（各调用点自己写，措辞必须讲清「现在还能继续用」）。
    """
    info = gui._update_pending_info
    open_page = messagebox.askokcancel(t("更新没有成功"), body, parent=gui._root)
    if open_page and info is not None and info.html_url:
        open_release_page(gui, info.html_url)


# ---------------------------------------------------------------- 启动兜底与一次性提示

def check_pending_update_at_startup(gui) -> None:
    """启动兜底：上次下载好了新版本但没来得及换（被强杀/直接关机）→
    重新复验残留，完好就直接出「下载完成」窗口给更新入口（不重复下载），
    并记下退出时替换；损坏/半截 → check_pending_download 内部已清理 + 留痕。"""
    if gui._headless or not update_check.can_self_update():
        return
    target = update_check.update_target_path()
    if target is None:
        return
    try:
        hit = update_check.check_pending_download(target, __version__)
    except Exception as exc:  # noqa: BLE001
        print(f"[update] ⚠️ 待更新文件复核失败：{type(exc).__name__}: {exc}", flush=True)
        return
    if hit is None:
        return
    version = hit[0]
    info = update_check.ReleaseInfo(
        tag=f"v{version}", version=version,
        html_url=f"{update_check.RELEASES_HTML}/tag/v{version}",
        asset_url="", asset_name=update_check.asset_name_for(),
        sums_url="", asset_size=None)
    mark_update_pending(gui, info)
    show_download_window(gui, info)
    enter_download_done_state(gui, update_check.pending_new_asset(target))


def schedule_version_changed_hint(gui) -> None:
    """启动版本提示：上次运行版本 ≠ 本次（刚完成过替换/升级）→ ~1.5 秒后弹一次性
    「已更新」小提示；首次运行只悄悄记下版本；版本没变 → 什么都不做。"""
    gui._updated_hint_job = None
    if gui._headless or not update_check.can_self_update():
        return
    last = update_check.load_last_seen_version(_m(gui).APP_DIR)
    if last is None:
        # 首次运行（或状态文件损坏）：只记录，不打扰
        update_check.save_last_seen_version(_m(gui).APP_DIR, __version__)
        return
    if last == __version__:
        return
    print(f"[update] 版本已从 v{last} 变为 v{__version__}：准备弹一次性「已更新」提示",
          flush=True)
    gui._updated_hint_job = gui._root.after(1500, lambda: show_version_changed_hint(gui))


def show_version_changed_hint(gui) -> None:
    """一次性、非模态、可秒关的「已更新到最新版本」小提示（逐字文案 checklist ④）。
    同一版本只弹一次：关闭时写回当前版本。版本号只进日志，不丢给普通用户。"""
    gui._updated_hint_job = None
    if gui._updated_hint_win is not None:
        return
    try:
        if not gui._root.winfo_exists():
            return
    except Exception:  # noqa: BLE001
        return
    win = tk.Toplevel(gui._root)
    win.title(t("已更新到最新版本"))
    win.configure(bg=PANEL)
    win.transient(gui._root)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: close_updated_hint(gui))
    win.bind("<Escape>", lambda _e: close_updated_hint(gui))
    gui._updated_hint_win = win

    from .ui_tk import FONT_UI as _FONT_UI
    from .ui_tk import FONT_BOLD_MD

    body = ttk.Frame(win, padding=(20, 16, 20, 14))
    body.pack(fill=tk.BOTH, expand=True)
    ttk.Label(body, text=t("已更新到最新版本"),
              font=FONT_BOLD_MD).pack(anchor=tk.W)
    ttk.Label(body, text=t("VRChat Live Translate 已更新到最新版本，一切照常使用。"),
              wraplength=360, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))
    link = tk.Label(body, text=t("看看这次更新了什么"), fg=ACCENT_HOVER, bg=PANEL,
                    cursor="hand2", font=_FONT_UI)
    link.pack(anchor=tk.W, pady=(8, 0))
    link.bind("<Button-1>", lambda _e: open_release_page(gui,
        f"{update_check.RELEASES_HTML}/tag/v{__version__}"))
    ok = ttk.Button(body, text=t("知道了"), style="Accent.TButton",
                    command=lambda: close_updated_hint(gui))
    ok.pack(anchor=tk.E, pady=(16, 0))
    ok.focus_set()

    win.update_idletasks()
    rx, ry = gui._root.winfo_x(), gui._root.winfo_y()
    rw = gui._root.winfo_width()
    win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 60}")
    gui._apply_dark_titlebar(win)
    win.lift()
    print("[update] 已弹出一次性「已更新到最新版本」提示", flush=True)


def close_updated_hint(gui) -> None:
    """关闭「已更新」提示（右上角 X / 知道了 / Esc 共用）：写回当前版本，同一版本不再弹。"""
    win, gui._updated_hint_win = gui._updated_hint_win, None
    if win is None:
        return                              # 没弹过就什么都不做（别误写状态文件）
    try:
        win.destroy()
    except Exception:  # noqa: BLE001
        pass
    update_check.save_last_seen_version(_m(gui).APP_DIR, __version__)


# ================================================================ API key 管理

def refresh_key_status(gui) -> None:
    """只显示来源 + 打码值，绝不显示明文。

    写两处：设置弹窗里的完整状态（_key_status）+ 主界面第二行右侧的状态槽位
    ——已配置时显示纯展示标签（_key_chip），未配置时换成可点按钮（_key_btn，
    点击打开**当前线路**的开通页）；保存/清除 key 后本函数会被再次调用，界面立刻切换。

    ⚠️ key 按线路分槽（`_current_key_slot()`）：国内版与海外版各存一份、互不通用。所以这里
    读的是**当前线路那一槽**，切完线路再调一次，显示的就是另一把 key 的状态。
    """
    slot = gui._current_key_slot()
    try:
        from .credentials import key_source

        src, masked = key_source(slot=slot)
    except Exception as exc:  # noqa: BLE001
        gui._key_status.config(text=t("⚠️ 读取 key 状态失败：{msg}", msg=exc))
        return
    if masked:
        # 带上线路名：两条线路各有一把 key，不写清是哪条的话"我明明填了 key"
        # 就会变成"界面说没填" —— 那其实是另一条线路的槽空着。
        gui._key_status.config(text=t("当前（{line}）：{src} {masked}",
                                      line=gui._provider_label(), src=src, masked=masked))
    else:
        gui._key_status.config(text=t("⚠️ 未配置 API key —— 在上面粘贴后点「保存」"))
    if hasattr(gui, "_key_chip"):
        if masked:
            # 已配置：恢复纯展示标签（按钮收起，不残留）
            gui._key_chip.configure(text=t("API key 已配置"), style="Chip.TLabel")
            if gui._key_btn.winfo_manager():
                gui._key_btn.pack_forget()
            if not gui._key_chip.winfo_manager():
                gui._key_chip.pack()
        else:
            # 未配置：换成可点按钮（跳转当前线路的开通页）
            # 文案档位：最小宽度 928 下能完整显示（实测见改动报告）。
            # 海外版那条**刻意更短**（"开通海外版" 而不是 "开通千问云·海外版"）：
            # 主界面这一行右侧还挤着四个输出勾选，写长了英文/俄语文案就会被裁。
            gui._key_btn.configure(
                text=(t("⚠ 未配置 API key · 点此开通千问云 ▸")
                      if gui._provider() == endpoints.PROVIDER_QIANWEN
                      else t("⚠ 未配置 API key · 点此开通海外版 ▸")))
            if gui._key_chip.winfo_manager():
                gui._key_chip.pack_forget()
            if not gui._key_btn.winfo_manager():
                gui._key_btn.pack()


def open_qianwen_signup(gui) -> None:
    """「未配置」状态按钮：用默认浏览器打开**当前线路**的开通页。

    函数名保留 `_open_qianwen_signup`（主界面按钮的 command 与既有测试都按它绑定），
    但地址已改为按线路取：千问云 → QIANWEN_SIGNUP_URL，千问云·海外版 → Qwen Cloud 首页。

    绝不抛异常、绝不影响主功能：失败时把链接写进状态栏让用户手动复制；
    成功/失败都打一行日志（**写清是哪条线路**，否则"打开了个不相干的页面"无从查起）。
    链接原文使用，不做任何解码/重组。
    """
    url = gui._signup_url()
    line = endpoints.provider_name(gui._provider())      # 日志恒用中文名
    try:
        ok = bool(_m(gui).webbrowser.open(url))
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[gui] ⚠ 打开浏览器失败（{exc}），请手动访问{line}开通页：{url}",
              flush=True)
    else:
        if ok:
            print(f"[gui] 已在默认浏览器打开{line}开通页：{url}", flush=True)
        else:
            print(f"[gui] ⚠ webbrowser.open 返回 False，请手动访问{line}开通页：{url}",
                  flush=True)
    if not ok:
        gui._set_status("warn", t("打不开浏览器，请手动复制访问：{url}", url=url))


def refresh_api_key_in_cfg(gui) -> None:
    """按既有优先级链重解 API key 并写回内存（实现见 `vlt.ui_state.refresh_api_key_in_cfg`）。"""
    from .ui_state import refresh_api_key_in_cfg as _refresh
    _refresh(gui._cfg)


def on_save_key(gui) -> None:
    from .credentials import load_saved_key, mask_key, save_api_key

    slot = gui._current_key_slot()       # 存进**当前线路那一槽**：两条线路的 key 不通用
    raw = gui._key_var.get()
    try:
        path = save_api_key(raw, slot=slot)
    except ValueError as exc:
        gui._key_var.set("")                      # 明文不留在界面上
        gui._key_status.config(text=t("❌ 没保存：{msg}", msg=exc))
        gui._set_status("error", t("API key 保存失败：{msg}", msg=exc))
        print(f"[gui] ❌ API key 保存失败：{exc}", flush=True)
        return
    gui._key_var.set("")
    shown = mask_key(load_saved_key(slot) or "")
    refresh_key_status(gui)
    refresh_api_key_in_cfg(gui)      # 关键：写回内存快照，否则开始翻译还读启动时的旧值
    gui._set_status("ok", t("API key 已保存（{shown}）", shown=shown))
    print(f"[gui] ✅ API key 已保存（线路={endpoints.provider_name(gui._provider())}，"
          f"{shown}）→ {path}", flush=True)
    gui._check_api_key()


def on_clear_key(gui) -> None:
    from .credentials import clear_saved_key

    # 只清**当前线路那一槽**：另一条线路的 key 原样留着（切回去还能用）
    removed = clear_saved_key(slot=gui._current_key_slot())
    refresh_key_status(gui)
    refresh_api_key_in_cfg(gui)      # 清除后可能回退到别的来源、也可能变空 —— 内存同步
    msg = t("已清除保存的 API key") if removed else t("本来就没有保存过 API key")
    gui._set_status("info", msg)
    # 日志保持中文（诊断用），界面已走 i18n
    print(f"[gui] {'已清除保存的 API key' if removed else '本来就没有保存过 API key'}"
          f"（线路={endpoints.provider_name(gui._provider())}）", flush=True)
    gui._check_api_key()
