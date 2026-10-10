"""设置对话框 UI 构建与交互逻辑。

从 ``vlt.gui.TranslationGUI`` 的 D 区（约 L1114-L2144）提取而来。
所有 Tk 控件与可变状态通过 ``gui`` 参数（TranslationGUI 实例）传入，不使用 self。

设置对话框是一个分页弹窗（Notebook），包含 7 个页面：
  常规 / 音频 / 手腕屏 / 桌面字幕 / 词库 / 房间 / 关于

为什么单独拆出：
  · gui.py 已经超过 4000 行，设置对话框的 7 个页面 + 辅助方法占了约 1000 行；
  · 设置弹窗是「打开 → 改 → 关」的独立交互单元，与主界面生命周期无关。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from tkinter import font as tkfont
from typing import Any

import tkinter as tk
from tkinter import ttk

from . import endpoints, i18n, platform
from .version import display_version
from . import config as _config_mod
from . import ui_theme as _ui_theme
from .config_io import (
    _write_config_text,
    _yaml_set_in_text,
    _yaml_set_or_create,
)
from .engine import (
    INPUT_GATE_MAX_DB,
    INPUT_GATE_MIN_DB,
    input_gate_settings,
)
from .i18n import t
from .paths import APP_DIR
from .ui_state import current_key_slot, provider
from .ui_text import _persist_provider, _provider_choices
from .ui_theme import (
    ACCENT,
    BORDER,
    COLOR_SRC_MINE,
    PANEL,
    SETTINGS_CHROME_H,
    SETTINGS_MAX_H,
    SETTINGS_MIN_H,
    SETTINGS_WIDTH,
    SETTINGS_WRAP,
    SPONSORS,
    SURFACE,
    TAB_INSET_X,
    TEXT,
)
from . import ui_tk as _ui_tk
from .ui_tk import _char_width_for
# 字体常量运行时取 `_ui_tk.FONT_*`（import 期快照在 Linux 上会静默回落，见 issue #61）

ROOT = APP_DIR


def _indicator_kw(gui) -> dict:
    """经典 tk 单选/复选框的统一配色：选中时指示器变蓝，其余深灰。"""
    return dict(bg=PANEL, fg=TEXT, activebackground=PANEL,
                activeforeground="#ffffff", selectcolor=SURFACE,
                highlightthickness=0, bd=0, font=_ui_tk.FONT_UI)


# ================================================================ 上下文

@dataclass
class SettingsCtx:
    """设置弹窗的**结构状态**（窗口 / Notebook / 页面列表 / tab 映射）。

    控件引用仍挂在 ``gui`` 实例上（``gui._key_entry`` / ``gui._mic_combo`` 等），
    保持与 gui.py 中事件处理器 / 回调的向后兼容。
    本 dataclass 只收纳「弹窗自身」的结构数据，方便测试与未来解耦。
    """
    win: Any = None                       # tk.Toplevel
    nb: Any = None                        # ttk.Notebook
    pages: list = field(default_factory=list)   # [(canvas, inner, sb), ...]
    tabs: dict = field(default_factory=dict)    # {title_text: tab_frame}
    size: tuple = (SETTINGS_WIDTH, SETTINGS_MIN_H)


# ================================================================ 弹窗生命周期

def apply_settings_metrics(gui) -> None:
    """按 `gui._dpi_scale` 把设置弹窗的**有效尺寸**重绑到本模块全局。

    口径与字体一致（issue #61）：`gui_settings` 里所有 `wraplength=SETTINGS_WRAP` /
    `SETTINGS_WIDTH` 取的都是这些**模块级名字**，在函数体内读取 —— 重绑即生效，
    不是 import 期快照。基准值定义在 `vlt/ui_theme.py`，这里只存「当前档」的有效值。

    必须在 `build_settings_dialog()` 建页之前调用（那时才知道弹窗要开多大）。
    同时把这份有效值挂到 `gui._settings_metrics`，供 gui.py 构「手腕屏」页时取用。

    ⚠️ **屏幕夹取必须在建页之前做**：窄屏上放大后的宽度装不下时，换行宽也得同步收窄，
    否则长说明会按超宽的 `wraplength` 排版、建完就横向溢出被裁（换行宽是建页时定死的，
    事后改不回来）。
    """
    global SETTINGS_WIDTH, SETTINGS_WRAP, SETTINGS_MIN_H, SETTINGS_MAX_H, SETTINGS_CHROME_H
    s = max(1.0, float(getattr(gui, "_dpi_scale", 1.0)))
    m = _ui_theme.settings_metrics(s)
    try:
        screen_w = int(gui._root.winfo_screenwidth() or 0)
    except Exception:  # noqa: BLE001
        screen_w = 0
    if screen_w and m.width > screen_w - 16:
        w = max(320, screen_w - 16)
        m = replace(m, width=w, wrap=max(160, m.wrap - (m.width - w)))
    SETTINGS_WIDTH, SETTINGS_WRAP = m.width, m.wrap
    SETTINGS_MIN_H, SETTINGS_MAX_H, SETTINGS_CHROME_H = m.min_h, m.max_h, m.chrome_h
    gui._settings_metrics = m


def build_settings_dialog(gui) -> None:
    """低频设置收进弹窗：**分页**（常规 / 音频 / 词库 / 房间 / 关于）+ 固定尺寸 + 每页可滚。

    弹窗**先建好再 withdraw**，且各页的控件**一次性全建齐**（不做「切到那页才建」的
    懒加载）：控件属性必须在弹窗不可见时也随即可用。
    """
    apply_settings_metrics(gui)          # 先按 DPI 定有效尺寸，再建页（wrap 等在函数内读取）
    win = tk.Toplevel(gui._root)
    win.title(t("设置"))
    win.configure(bg=PANEL)
    win.transient(gui._root)
    win.resizable(False, False)
    win.withdraw()
    win.protocol("WM_DELETE_WINDOW", lambda: close_settings(gui))
    win.bind("<Escape>", lambda _e: close_settings(gui))
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        win.bind(seq, lambda e: on_settings_wheel(gui, e))
    gui._settings_win = win
    gui._apply_dark_titlebar(win)

    nb = ttk.Notebook(win, style="TNotebook")
    nb.pack(fill=tk.BOTH, expand=True)
    gui._settings_nb = nb

    gui._settings_tabs = {}

    ctx = SettingsCtx(win=win, nb=nb, tabs=gui._settings_tabs)

    build_settings_general(gui, settings_page(ctx, gui, t("常规")))
    build_settings_audio(gui, settings_page(ctx, gui, t("音频")))
    build_settings_wrist(gui, settings_page(ctx, gui, t("手腕屏")))
    build_settings_desktop(gui, settings_page(ctx, gui, t("桌面字幕")))
    build_settings_glossary(gui, settings_page(ctx, gui, t("词库")))
    build_settings_room(gui, settings_page(ctx, gui, t("房间")))
    build_settings_about(gui, settings_page(ctx, gui, t("关于")))

    gui._settings_pages = ctx.pages
    size_settings_window(gui, ctx)
    gui._settings_size = ctx.size
    gui._settings_ctx = ctx


def settings_page(ctx: SettingsCtx, gui, title: str) -> ttk.Frame:
    """给 Notebook 加一页，返回该页**放控件的内容 frame**（外面套一层可滚动画布）。"""
    nb = ctx.nb
    tab = ttk.Frame(nb)
    nb.add(tab, text=title)
    ctx.tabs[title] = tab
    canvas = tk.Canvas(tab, bg=PANEL, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(tab, orient=tk.VERTICAL, style="Vertical.TScrollbar",
                       command=canvas.yview)
    inner = ttk.Frame(canvas, padding=(TAB_INSET_X, 16, TAB_INSET_X, 16))
    canvas.configure(yscrollcommand=sb.set)
    slot = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    ctx.pages.append((canvas, inner, sb))

    def _on_inner(_e=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all") or (0, 0, 1, 1))
        sync_page_scrollbar(canvas, inner, sb)

    def _on_canvas(e) -> None:
        canvas.itemconfigure(slot, width=e.width)
        sync_page_scrollbar(canvas, inner, sb)

    inner.bind("<Configure>", _on_inner)
    canvas.bind("<Configure>", _on_canvas)
    return inner


def sync_page_scrollbar(canvas: tk.Canvas, inner: ttk.Frame,
                         sb: ttk.Scrollbar) -> None:
    """内容比可视高度高才显示滚动条；装得下就收起来。"""
    have = canvas.winfo_height()
    if have <= 1:
        return
    if inner.winfo_reqheight() > have + 1:
        if sb.winfo_manager() != "pack":
            sb.pack(side=tk.RIGHT, fill=tk.Y, before=canvas)
    elif sb.winfo_manager() == "pack":
        sb.pack_forget()


def sync_settings_pages(gui, ctx: SettingsCtx | None = None) -> None:
    """弹窗显示出来后补一次滚动条判定。"""
    if ctx is None:
        ctx = SettingsCtx(win=gui._settings_win, pages=gui._settings_pages)
    try:
        ctx.win.update_idletasks()
        for canvas, inner, sb in ctx.pages:
            sync_page_scrollbar(canvas, inner, sb)
    except Exception as exc:  # noqa: BLE001
        print(f"[ui] ⚠️ 设置弹窗滚动条同步失败（不影响使用）："
              f"{type(exc).__name__}: {exc}", flush=True)


def on_settings_wheel(gui, event) -> None:
    """滚轮滚**当前那一页**；指针停在自带滚动的控件上时不抢。"""
    try:
        w = getattr(event, "widget", None)
        if w is not None and w.winfo_class() in ("Text", "Listbox"):
            return
        nb = gui._settings_nb
        canvas = gui._settings_pages[nb.index(nb.select())][0]
    except Exception:  # noqa: BLE001
        return
    delta, num = getattr(event, "delta", 0), getattr(event, "num", 0)
    if delta:
        step = -1 if delta > 0 else 1
    elif num == 4:
        step = -1
    elif num == 5:
        step = 1
    else:
        return
    canvas.yview_scroll(step * 3, "units")


def size_settings_window(gui, ctx: SettingsCtx | None = None) -> None:
    """按当前语言实测各页需求高度，定弹窗的固定尺寸。"""
    if ctx is None:
        ctx = SettingsCtx(win=gui._settings_win, pages=gui._settings_pages)
    win = ctx.win
    pages = ctx.pages
    try:
        win.update_idletasks()
        need = max((inner.winfo_reqheight() for _c, inner, _s in pages),
                   default=0)
        screen_h = int(win.winfo_screenheight() or 0)
        screen_w = int(win.winfo_screenwidth() or 0)
        cap = min(SETTINGS_MAX_H, screen_h - 90) if screen_h else SETTINGS_MAX_H
        h = max(SETTINGS_MIN_H, min(need + SETTINGS_CHROME_H, cap))
        # ⚠️ min_h 也随 DPI 放大（360×s），小屏+高 DPI 下会超过屏幕 —— 再夹一道，
        #    否则弹窗比屏幕还高，底部内容连滚动条都够不到。
        if screen_h:
            h = min(h, max(240, screen_h - 90))
        # 宽度随 DPI 放大后也要夹到屏幕内（基准 760 时一般不需要，HiDPI 下会）
        w = SETTINGS_WIDTH
        if screen_w:
            w = max(320, min(w, screen_w - 16))
        ctx.size = (w, h)
        win.geometry(f"{w}x{h}")
        print(f"[ui] 设置弹窗 {w}x{h}"
              f"（最高一页需 {need}px，屏高 {screen_h}px）", flush=True)
        # ⚠️ 极窄屏 + 超高档（宽度被屏幕夹住后）可能装不下某些定宽控件
        #    （`width=34` 的 Entry、按字符宽算的下拉——它们随字号一起变大）。页面只能纵向
        #    滚动，横向装不下就会**被裁且够不到**，所以给一行可见告警，让用户去调 `ui.scale`。
        need_w = max((inner.winfo_reqwidth() for _c, inner, _s in pages), default=0)
        avail_w = w - 16                    # 弹窗边框/滚动条占掉的横向余量（约）
        if need_w > avail_w:
            print(f"[ui] ⚠️ 设置弹窗宽 {w}px 装不下最宽的一页（需 {need_w}px，"
                  f"可用 {avail_w}px）—— 屏幕 {screen_w}px 配当前缩放偏小，"
                  f"可在 config.yaml 调低 ui.scale（当前档 {getattr(gui, '_dpi_scale', 1.0):.2f}）",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        ctx.size = (SETTINGS_WIDTH, SETTINGS_MAX_H)
        win.geometry(f"{SETTINGS_WIDTH}x{SETTINGS_MAX_H}")
        print(f"[ui] ⚠️ 设置弹窗尺寸自适应失败，按上限值开窗："
              f"{type(exc).__name__}: {exc}", flush=True)


# ================================================================ 常规页

def build_settings_general(gui, body: ttk.Frame) -> None:
    """「常规」页：API key → 服务线路 → 界面语言 → VRChat OSC 端口。"""
    # ---- API Key ----
    ttk.Label(body, text="API KEY", style="Section.TLabel").pack(anchor=tk.W)
    key_row = ttk.Frame(body)
    key_row.pack(fill=tk.X, pady=(8, 4))
    gui._key_clear_btn = ttk.Button(key_row, text=t("清除"),
                                    command=lambda: gui._on_clear_key())
    gui._key_clear_btn.pack(side=tk.RIGHT, padx=(6, 0))
    gui._key_save_btn = ttk.Button(key_row, text=t("保存"),
                                   command=lambda: gui._on_save_key())
    gui._key_save_btn.pack(side=tk.RIGHT)
    gui._key_var = tk.StringVar()
    gui._key_entry = ttk.Entry(key_row, textvariable=gui._key_var, show="●",
                               width=34, style="Key.TEntry")
    gui._key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
    gui._key_entry.bind("<Return>", lambda _e: gui._on_save_key())
    gui._attach_edit_menu(gui._key_entry)
    gui._key_status = ttk.Label(body, text="", style="Dim.TLabel",
                                justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._key_status.pack(anchor=tk.W)
    gui._refresh_key_status()

    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)

    # ---- 服务线路 ----
    build_provider_section(gui, body)

    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)

    # ---- 界面语言 ----
    ttk.Label(body, text=t("界面语言"), style="Section.TLabel").pack(anchor=tk.W)
    lang_row = ttk.Frame(body)
    lang_row.pack(fill=tk.X, pady=(8, 4))
    gui._ui_lang_names = dict(i18n.available_languages())
    gui._ui_lang_var = tk.StringVar(
        value=gui._ui_lang_names.get(i18n.current_language(), "简体中文"))
    gui._ui_lang_combo = ttk.Combobox(
        lang_row, values=list(gui._ui_lang_names.values()),
        state="readonly", width=14, textvariable=gui._ui_lang_var)
    gui._ui_lang_combo.pack(side=tk.LEFT)
    gui._ui_lang_combo.bind("<<ComboboxSelected>>",
                            lambda e: on_ui_lang_change(gui, e))
    gui._ui_lang_note = ttk.Label(body, text=t("界面语言在重启程序后生效"),
                                  style="Muted.TLabel", justify=tk.LEFT,
                                  wraplength=SETTINGS_WRAP)
    gui._ui_lang_note.pack(anchor=tk.W)

    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)

    # ---- VRChat OSC 端口 ----
    ttk.Label(body, text="VRChat OSC", style="Section.TLabel").pack(anchor=tk.W)
    osc_row = ttk.Frame(body)
    osc_row.pack(fill=tk.X, pady=(8, 4))
    gui._osc_save_btn = ttk.Button(osc_row, text=t("保存端口"),
                                   command=lambda: on_save_osc_port(gui))
    gui._osc_save_btn.pack(side=tk.RIGHT, padx=(6, 0))
    ttk.Label(osc_row, text=t("端口:"), style="Dim.TLabel").pack(side=tk.LEFT)
    gui._osc_port_var = tk.StringVar(value=str(chatbox_port(gui)))
    gui._osc_port_entry = ttk.Entry(osc_row, textvariable=gui._osc_port_var,
                                    width=8, style="Key.TEntry")
    gui._osc_port_entry.pack(side=tk.LEFT, padx=(8, 0))
    gui._osc_port_entry.bind("<Return>", lambda _e: on_save_osc_port(gui))
    gui._attach_edit_menu(gui._osc_port_entry)
    ttk.Label(body,
              text=t("VRChat 默认收 9000 端口；只有你在 VRChat 里改过 OSC 端口"
                     "（或中间挂了转发工具）时才需要动这里"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W)
    gui._osc_err = ttk.Label(body, text="", style="Error.TLabel",
                             justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._osc_err.pack(anchor=tk.W, pady=(6, 0))


def chatbox_port(gui) -> int:
    """当前 chatbox.port（脏值一律回落到 9000）。"""
    try:
        return int((gui._cfg.chatbox or {}).get("port", 9000))
    except Exception:  # noqa: BLE001
        return 9000


def on_save_osc_port(gui) -> None:
    """把 VRChat 的 OSC 接收端口就地写回 config.yaml 的 `chatbox.port`。"""
    raw = (gui._osc_port_var.get() or "").strip()
    port = int(raw) if raw.isdigit() else None
    if port is None or not 1 <= port <= 65535:
        err = t("端口必须是 1–65535 之间的整数")
        gui._osc_err.configure(text="❌ " + err)
        gui._set_status("error", t("❌ 没保存：{msg}", msg=err))
        print(f"[gui] ❌ OSC 端口没保存：{raw!r}（{err}）", flush=True)
        return
    p = _config_mod.DEFAULT_CONFIG
    if not p.exists():
        msg = f"找不到 {p.name}"
        gui._osc_err.configure(text=t("❌ 没保存：{msg}", msg=msg))
        gui._set_status("error", t("❌ 没保存：{msg}", msg=msg))
        print(f"[gui] ❌ OSC 端口没保存：{msg}", flush=True)
        return
    try:
        text = p.read_text(encoding="utf-8")
        text = _yaml_set_or_create(text, ["chatbox", "port"], str(port))
        _write_config_text(p, text)
    except Exception as exc:
        gui._osc_err.configure(text=t("❌ 没保存：{msg}", msg=exc))
        gui._set_status("error", t("❌ 没保存：{msg}", msg=exc))
        print(f"[gui] ❌ 保存 OSC 端口失败（配置未改动）：{exc}", flush=True)
        return
    gui._osc_err.configure(text="")
    if isinstance(gui._cfg.chatbox, dict):
        gui._cfg.chatbox["port"] = port
    if any(e.running for e in gui._engines):
        gui._set_status("warn", t("OSC 端口已保存：{port}（正在翻译，重开翻译后生效）",
                                  port=port))
        print(f"[gui] ⚠️ OSC 端口已保存：{port}，但翻译正在进行中 —— "
              f"需先停止再重新开始才生效", flush=True)
    else:
        gui._set_status("ok", t("OSC 端口已保存：{port}", port=port))
        print(f"[gui] OSC 端口已保存：{port}", flush=True)


# ================================================================ 服务线路

def build_provider_section(gui, body: ttk.Frame) -> None:
    """「服务线路」区：千问云 / 千问云·海外版（互斥）。"""
    ttk.Label(body, text=t("服务线路"), style="Section.TLabel").pack(anchor=tk.W)

    gui._provider_name_to_id = dict(_provider_choices())
    gui._provider_id_to_name = {pid: name
                                for name, pid in gui._provider_name_to_id.items()}
    cur = get_provider(gui)
    gui._provider_var = tk.StringVar(value=gui._provider_id_to_name[cur])
    gui._provider_combo = ttk.Combobox(body, values=list(gui._provider_name_to_id),
                                       state="readonly",
                                       textvariable=gui._provider_var)
    gui._provider_combo.pack(fill=tk.X, pady=(8, 4))
    gui._provider_combo.bind("<<ComboboxSelected>>",
                             lambda e: on_provider_change(gui, e))

    ttk.Label(body,
              text=t("国内用「千问云」；海外用「千问云·海外版」（qwencloud.com）。"
                     "两版的 API key 不互通，各存各的，来回切线路不用重填"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

    save_row = ttk.Frame(body)
    save_row.pack(fill=tk.X, pady=(10, 0))
    gui._provider_save_btn = ttk.Button(save_row, text=t("保存线路设置"),
                                        command=lambda: on_save_provider(gui))
    gui._provider_save_btn.pack(side=tk.RIGHT)
    gui._provider_err = ttk.Label(body, text="", style="Error.TLabel",
                                  justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._provider_err.pack(anchor=tk.W, pady=(6, 0))


def on_provider_change(gui, _event=None) -> None:
    """下拉选完线路：只清掉就地红字，不写盘。"""
    gui._provider_err.configure(text="")


def selected_provider(gui) -> str:
    """下拉当前选中的线路 id。"""
    return gui._provider_name_to_id.get(gui._provider_var.get(),
                                        endpoints.PROVIDER_QIANWEN)


def on_save_provider(gui) -> None:
    """把线路两项（provider / base_url）就地写回 config.yaml。"""
    prov = selected_provider(gui)
    base_url = endpoints.default_base_url(prov)
    p = _config_mod.DEFAULT_CONFIG
    if not p.exists():
        print(f"[gui] ❌ 线路没保存：找不到 {p.name}（配置尚未生成）", flush=True)
        gui._provider_err.configure(text=t("❌ 没保存：{msg}", msg=p.name))
        gui._set_status("error", t("❌ 没保存：{msg}", msg=p.name))
        return
    try:
        _persist_provider(p, prov, base_url)
    except Exception as exc:
        print(f"[gui] ❌ 保存服务线路失败（配置未改动）：{exc}", flush=True)
        gui._provider_err.configure(text=t("❌ 没保存：{msg}", msg=exc))
        gui._set_status("error", t("❌ 没保存：{msg}", msg=exc))
        return
    gui._provider_err.configure(text="")
    gui._cfg.session_base["provider"] = prov
    gui._cfg.session_base["base_url"] = base_url
    gui._refresh_key_status()
    gui._refresh_api_key_in_cfg()
    if any(e.running for e in gui._engines):
        gui._set_status("warn", t("当前线路需要先停止翻译，改完再重新开始"))
        print("[gui] ⚠️ 服务线路已保存，但翻译正在进行中 —— 需先停止再重新开始才生效",
              flush=True)
    else:
        gui._set_status("ok", t("已切换到 {line}（重启翻译后生效）",
                                line=provider_label(gui)))
    print(f"[gui] 服务线路已保存：provider={prov} base_url={base_url}", flush=True)


# ---------------------------------------------------------------- 线路取值助手

def get_provider(gui) -> str:
    """当前线路 id。"""
    return provider(gui._cfg)


def provider_label(gui) -> str:
    """当前线路的界面显示名。"""
    for name, pid in _provider_choices():
        if pid == get_provider(gui):
            return name
    return t("千问云")


def get_current_key_slot(gui) -> str:
    """当前线路的密钥槽名。"""
    return current_key_slot(gui._cfg)


def signup_url(gui) -> str:
    """当前线路的开通页地址。"""
    return endpoints.signup_url(get_provider(gui))


# ================================================================ 音频页

def build_settings_audio(gui, body: ttk.Frame) -> None:
    """「音频」页：设备选择 → 输入门限 → 译音音色。"""
    from .voices import REALTIME_VOICES, TTS_VOICES, voice_choices

    # ---- 音频设备 ----
    dev_head = ttk.Frame(body)
    dev_head.pack(fill=tk.X)
    gui._refresh_btn = ttk.Button(dev_head, text=t("刷新"),
                                  command=lambda: gui._on_refresh_devices())
    gui._refresh_btn.pack(side=tk.RIGHT)
    ttk.Label(dev_head, text=t("音频设备"), style="Section.TLabel").pack(side=tk.LEFT)

    grid = ttk.Frame(body)
    grid.pack(fill=tk.X, pady=(8, 2))
    grid.columnconfigure(1, weight=1)
    auto = t("自动检测")
    gui._linux_fixed_audio = platform.IS_LINUX
    gui._mic_combo = ttk.Combobox(grid, values=[auto], state="readonly")
    gui._loopback_combo = None
    gui._audio_out_combo = None
    rows = [(t("麦克风:"), gui._mic_combo)]
    if not gui._linux_fixed_audio:
        gui._loopback_combo = ttk.Combobox(grid, values=[auto], state="readonly")
        gui._audio_out_combo = ttk.Combobox(grid, values=[auto], state="readonly")
        rows += [(t("VRChat 音频:"), gui._loopback_combo),
                 (t("译音输出:"), gui._audio_out_combo)]
    for i, (label, combo) in enumerate(rows):
        ttk.Label(grid, text=label, style="Dim.TLabel").grid(
            row=i, column=0, sticky="w", pady=3)
        combo.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=3)
        combo.set(auto)
        combo.bind("<<ComboboxSelected>>", lambda e: gui._on_device_change())

    capture_cfg = (gui._cfg.output or {}).get("capture") or {}
    audio_cfg = (gui._cfg.output or {}).get("audio") or {}
    gui._mic_combo.set(capture_cfg.get("mic_device") or auto)
    if not gui._linux_fixed_audio:
        gui._loopback_combo.set(capture_cfg.get("loopback_device") or auto)
        gui._audio_out_combo.set(audio_cfg.get("device_name") or auto)

    if gui._linux_fixed_audio:
        ttk.Label(body, text=t("Linux：VRChat 音频与译音输出已自动处理"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))
    else:
        ttk.Label(body, text=t("设备选择自动保存到 config.yaml"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

    # ---- 麦克风代理（**两端都建**：Windows 走 PortAudio 输出流，Linux 走 pw-cat 管道 +
    #      运行时声明的虚拟麦。语义完全一致，见 vlt/output/micproxy*.py 的模块头）----
    gui._proxy_check = None
    gui._passthrough_spin = None
    gui._translated_spin = None
    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
    ttk.Label(body, text=t("麦克风代理"), style="Section.TLabel").pack(anchor=tk.W)
    _proxy_cfg = audio_cfg.get("proxy") or {}
    gui._proxy_enabled_var = tk.BooleanVar(value=bool(_proxy_cfg.get("enabled", True)))
    gui._proxy_check = tk.Checkbutton(
        body, variable=gui._proxy_enabled_var,
        text=t("启用 —— VRChat 麦克风固定选虚拟声卡，原声/译音在主界面一键切"),
        command=lambda: gui._on_proxy_toggle(), wraplength=SETTINGS_WRAP,
        justify=tk.LEFT, anchor="w", **_indicator_kw(gui))
    gui._proxy_check.pack(anchor=tk.W, pady=(8, 4))

    pgrid = ttk.Frame(body)
    pgrid.pack(fill=tk.X)
    pgrid.columnconfigure(1, weight=1)
    gui._passthrough_var = tk.IntVar(
        value=int(_proxy_cfg.get("passthrough_buffer_ms", 150)))
    ttk.Label(pgrid, text=t("直通缓冲(ms):"), style="Dim.TLabel").grid(
        row=0, column=0, sticky="w", pady=3)
    gui._passthrough_spin = ttk.Spinbox(
        pgrid, from_=60, to=500, increment=10, width=8,
        textvariable=gui._passthrough_var,
        command=lambda: gui._on_proxy_buffer_change())
    gui._passthrough_spin.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=3)
    gui._passthrough_spin.bind("<FocusOut>",
                               lambda _e: gui._on_proxy_buffer_change())
    gui._passthrough_spin.bind("<Return>",
                               lambda _e: gui._on_proxy_buffer_change())

    gui._translated_buf_var = tk.IntVar(
        value=int(audio_cfg.get("buffer_ms", 300)))
    ttk.Label(pgrid, text=t("译音缓冲(ms):"), style="Dim.TLabel").grid(
        row=1, column=0, sticky="w", pady=3)
    gui._translated_spin = ttk.Spinbox(
        pgrid, from_=50, to=2000, increment=50, width=8,
        textvariable=gui._translated_buf_var,
        command=lambda: gui._on_proxy_buffer_change())
    gui._translated_spin.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)
    gui._translated_spin.bind("<FocusOut>",
                              lambda _e: gui._on_proxy_buffer_change())
    gui._translated_spin.bind("<Return>",
                              lambda _e: gui._on_proxy_buffer_change())

    gui._proxy_hint = ttk.Label(body, text="", style="Muted.TLabel",
                               justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._proxy_hint.pack(anchor=tk.W, pady=(6, 0))
    gui._sync_proxy_controls_state()

    # ---- 译音音量（固定增益 / 跟随麦克风）----
    #      原声直通与译音共用**同一条**虚拟声卡输出流，VRChat 的麦克风音量只能整体调，
    #      两条腿之间的相对失配修不了 → 只能在程序内修。参数落盘与热更新见
    #      vlt/gui_proxy.py 的 `_on_level_change`（与上面那两个缓冲 spin 同一套路）。
    gui._level_mode_combo = None
    gui._level_fixed_spin = None
    gui._level_offset_spin = None
    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
    ttk.Label(body, text=t("译音音量"), style="Section.TLabel").pack(anchor=tk.W)
    ttk.Label(body,
              text=t("原声与译音共用一条虚拟声卡输出流，VRChat 只能整体调麦克风音量"
                     "——译音比原声响/轻要在这里修"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(4, 0))

    _level_cfg = audio_cfg.get("level") or {}
    _level_names = gui._level_mode_names()
    _level_mode = str(_level_cfg.get("mode") or "off")
    if _level_mode not in _level_names:
        _level_mode = "off"
    gui._level_mode_var = tk.StringVar(value=_level_names[_level_mode])

    lgrid = ttk.Frame(body)
    lgrid.pack(fill=tk.X, pady=(8, 2))
    lgrid.columnconfigure(1, weight=1)

    ttk.Label(lgrid, text=t("模式:"), style="Dim.TLabel").grid(
        row=0, column=0, sticky="w", pady=3)
    gui._level_mode_combo = ttk.Combobox(
        lgrid, textvariable=gui._level_mode_var, state="readonly", width=18,
        values=[_level_names[k] for k in ("off", "fixed", "follow_mic")])
    gui._level_mode_combo.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=3)
    gui._level_mode_combo.bind("<<ComboboxSelected>>",
                               lambda _e: gui._on_level_change())

    gui._level_fixed_var = tk.DoubleVar(
        value=float(_level_cfg.get("fixed_gain_db", 0.0)))
    ttk.Label(lgrid, text=t("固定增益(dB):"), style="Dim.TLabel").grid(
        row=1, column=0, sticky="w", pady=3)
    gui._level_fixed_spin = ttk.Spinbox(
        lgrid, from_=-24.0, to=6.0, increment=0.5, width=8,
        textvariable=gui._level_fixed_var,
        command=lambda: gui._on_level_change())
    gui._level_fixed_spin.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)
    gui._level_fixed_spin.bind("<FocusOut>", lambda _e: gui._on_level_change())
    gui._level_fixed_spin.bind("<Return>", lambda _e: gui._on_level_change())

    gui._level_offset_var = tk.DoubleVar(
        value=float(_level_cfg.get("offset_db", 0.0)))
    ttk.Label(lgrid, text=t("相对麦克风(dB):"), style="Dim.TLabel").grid(
        row=2, column=0, sticky="w", pady=3)
    gui._level_offset_spin = ttk.Spinbox(
        lgrid, from_=-12.0, to=12.0, increment=0.5, width=8,
        textvariable=gui._level_offset_var,
        command=lambda: gui._on_level_change())
    gui._level_offset_spin.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)
    gui._level_offset_spin.bind("<FocusOut>", lambda _e: gui._on_level_change())
    gui._level_offset_spin.bind("<Return>", lambda _e: gui._on_level_change())

    # 实时读数：读的是**正在跑**的代理/引擎，绝不为它新开采集设备（tick 见
    # gui_proxy._refresh_level_readout，只在设置窗可见时跑，窗口一关就停）。
    ttk.Label(lgrid, text=t("当前增益:"), style="Dim.TLabel").grid(
        row=3, column=0, sticky="w", pady=3)
    gui._level_gain_lbl = ttk.Label(lgrid, text="—", style="Dim.TLabel", width=9)
    gui._level_gain_lbl.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=3)

    gui._level_readout_lbl = ttk.Label(body, text="—", style="Dim.TLabel",
                                       justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._level_readout_lbl.pack(anchor=tk.W, pady=(2, 0))
    gui._level_hint = ttk.Label(body, text="", style="Muted.TLabel",
                                justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._level_hint.pack(anchor=tk.W, pady=(6, 0))
    ttk.Label(body, text=t("改完立即生效（逐句重算，不给译音加缓冲）"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(2, 0))
    gui._sync_level_controls_state()

    # ---- 输入门限 ----
    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
    ttk.Label(body, text=t("输入门限"), style="Section.TLabel").pack(anchor=tk.W)
    _gate_en, _gate_db, gui._gate_hold_ms, gui._gate_preroll_ms = \
        input_gate_settings(capture_cfg)
    gui._gate_enabled_var = tk.BooleanVar(value=_gate_en)
    gui._gate_check = tk.Checkbutton(
        body, variable=gui._gate_enabled_var,
        text=t("启用 —— 低于门限的声音不翻译（滤掉远处说话小声的玩家）"),
        command=lambda: gui._on_gate_change(), wraplength=SETTINGS_WRAP,
        justify=tk.LEFT, anchor="w", **_indicator_kw(gui))
    gui._gate_check.pack(anchor=tk.W, pady=(8, 4))

    ggrid = ttk.Frame(body)
    ggrid.pack(fill=tk.X)
    ggrid.columnconfigure(1, weight=1)
    gui._gate_level_canvas = tk.Canvas(ggrid, width=320, height=15,
                                       bg=SURFACE, highlightthickness=1,
                                       highlightbackground=BORDER, bd=0)
    ttk.Label(ggrid, text=t("当前电平:"), style="Dim.TLabel").grid(
        row=0, column=0, sticky="w", pady=3)
    gui._gate_level_canvas.grid(row=0, column=1, sticky="w", padx=(8, 6), pady=3)
    gui._gate_level_lbl = ttk.Label(ggrid, text="—", style="Dim.TLabel", width=9)
    gui._gate_level_lbl.grid(row=0, column=2, sticky="w")

    gui._gate_var = tk.DoubleVar(value=_gate_db)
    ttk.Label(ggrid, text=t("门限:"), style="Dim.TLabel").grid(
        row=1, column=0, sticky="w", pady=3)
    gui._gate_scale = tk.Scale(
        ggrid, from_=INPUT_GATE_MIN_DB, to=INPUT_GATE_MAX_DB, resolution=1,
        orient=tk.HORIZONTAL, variable=gui._gate_var, showvalue=False, length=320,
        bg=PANEL, fg=TEXT, troughcolor=SURFACE, activebackground=ACCENT,
        highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
        command=lambda _v: gui._on_gate_change())
    gui._gate_scale.grid(row=1, column=1, sticky="w", padx=(8, 6), pady=3)
    gui._gate_val_lbl = ttk.Label(ggrid, text=f"{_gate_db:g} dB",
                                  style="Dim.TLabel", width=9)
    gui._gate_val_lbl.grid(row=1, column=2, sticky="w")
    ttk.Label(body,
              text=t("只有响度超过门限的声音才会被翻译；改完立刻生效"
                     "（勾选「启用」后这里显示实时电平）"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

    # ---- 音色 ----
    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
    ttk.Label(body, text=t("音色"), style="Section.TLabel").pack(anchor=tk.W)
    vgrid = ttk.Frame(body)
    vgrid.pack(fill=tk.X, pady=(8, 2))
    vgrid.columnconfigure(1, weight=1)
    gui._voice_ctx.speech_voice_var = tk.StringVar()
    gui._speech_voice_var = gui._voice_ctx.speech_voice_var  # 测试兼容别名
    gui._voice_ctx.tts_voice_var = tk.StringVar()
    gui._tts_voice_var = gui._voice_ctx.tts_voice_var  # 测试兼容别名
    gui._voice_ctx.speech_voice_combo = ttk.Combobox(
        vgrid, textvariable=gui._voice_ctx.speech_voice_var, state="normal", width=18)
    gui._speech_voice_combo = gui._voice_ctx.speech_voice_combo  # 测试兼容别名
    gui._voice_ctx.tts_voice_combo = ttk.Combobox(
        vgrid, textvariable=gui._voice_ctx.tts_voice_var, state="normal", width=18)
    gui._tts_voice_combo = gui._voice_ctx.tts_voice_combo  # 测试兼容别名
    _preview_w = _char_width_for(t("试听中…"), _ui_tk.FONT_UI, 6)
    for i, (label, combo, values, kind, cb) in enumerate((
            (t("说话译音:"), gui._voice_ctx.speech_voice_combo,
             voice_choices(gui._effective_speech_voice(), REALTIME_VOICES),
             "speech", lambda: gui._on_preview_speech_voice()),
            (t("打字译音:"), gui._voice_ctx.tts_voice_combo,
             voice_choices(str((gui._cfg.text_input.get("tts") or {}).get("voice") or ""),
                           TTS_VOICES),
             "tts", lambda: gui._on_preview_tts_voice()))):
        ttk.Label(vgrid, text=label, style="Dim.TLabel").grid(
            row=i, column=0, sticky="w", pady=3)
        combo.configure(values=values)
        combo.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=3)
        btn = ttk.Button(vgrid, text=t("试听"), width=_preview_w, command=cb)
        btn.grid(row=i, column=2, sticky="w", padx=(8, 0), pady=3)
        if kind == "speech":
            gui._voice_ctx.speech_preview_btn = btn
            gui._speech_preview_btn = btn  # 测试兼容别名
        else:
            gui._voice_ctx.tts_preview_btn = btn
            gui._tts_preview_btn = btn  # 测试兼容别名
    gui._voice_ctx.speech_voice_var.set(gui._effective_speech_voice())
    gui._voice_ctx.tts_voice_var.set(
        str((gui._cfg.text_input.get("tts") or {}).get("voice") or "Cherry"))
    gui._voice_ctx.speech_voice_combo.bind("<<ComboboxSelected>>",
                                 lambda e: gui._on_speech_voice_change())
    gui._voice_ctx.speech_voice_combo.bind("<Return>",
                                 lambda e: gui._on_speech_voice_change())
    gui._voice_ctx.tts_voice_combo.bind("<<ComboboxSelected>>",
                              lambda e: gui._on_tts_voice_change())
    gui._voice_ctx.tts_voice_combo.bind("<Return>",
                              lambda e: gui._on_tts_voice_change())
    gui._voice_note = ttk.Label(
        body, text=t("说话译音跟随「译音输出」开关"
                     "（改完下次开始翻译生效）；打字译音立刻生效"),
        style="Muted.TLabel", justify=tk.LEFT,
        wraplength=SETTINGS_WRAP)
    gui._voice_note.pack(anchor=tk.W, pady=(6, 0))


# ================================================================ 手腕屏页

def build_settings_wrist(gui, body: ttk.Frame) -> None:
    """「设置 → 手腕屏」：头显里那块手腕屏微调。"""
    ttk.Label(body, text=t("头显里那块手腕屏的锚点、位置 / 旋转 / 字号等参数。"),
              style="Dim.TLabel").pack(anchor="w", pady=(0, 6))
    gui._build_tune_page(body)
    gui._wrist_page = body


# ================================================================ 桌面字幕页

def build_settings_desktop(gui, body: ttk.Frame) -> None:
    """「设置 → 桌面字幕」：桌面字幕自己的一套参数。"""
    ttk.Label(body, text=t("贴在 VRChat 窗口上的那块字幕窗：尺寸 / 字号 / 透明度。"),
              style="Dim.TLabel").pack(anchor="w", pady=(0, 6))
    gui._build_desktop_tune_page(body)
    gui._desktop_page = body


# ================================================================ 词库页

def build_settings_glossary(gui, body: ttk.Frame) -> None:
    """「词库」页：专有名词怎么译。"""
    ttk.Label(body, text=t("专有词库"), style="Section.TLabel").pack(anchor=tk.W)
    gui._voice_ctx.glossary_scope_names = {
        "global": t("全局"),
        "mine": t("我说"),
        "theirs": t("别人说"),
    }
    gui._glossary_scope_names = gui._voice_ctx.glossary_scope_names  # 测试兼容别名
    scope_row = ttk.Frame(body)
    scope_row.pack(fill=tk.X, pady=(8, 0))
    ttk.Label(scope_row, text=t("作用方向:"), style="Dim.TLabel").pack(side=tk.LEFT)
    gui._voice_ctx.glossary_scope_var = tk.StringVar(
        value=gui._voice_ctx.glossary_scope_names["global"])
    gui._glossary_scope_var = gui._voice_ctx.glossary_scope_var  # 测试兼容别名
    gui._glossary_scope_combo = ttk.Combobox(
        scope_row, values=list(gui._voice_ctx.glossary_scope_names.values()),
        state="readonly", width=10, textvariable=gui._voice_ctx.glossary_scope_var)
    gui._glossary_scope_combo.pack(side=tk.LEFT, padx=(8, 0))
    gui._glossary_scope_combo.bind("<<ComboboxSelected>>",
                                   lambda e: gui._on_glossary_scope_change())
    gloss_box = ttk.Frame(body)
    gloss_box.pack(fill=tk.X, pady=(8, 2))
    gui._voice_ctx.glossary_text = tk.Text(
        gloss_box, height=9, width=44, wrap=tk.NONE, undo=True,
        bg=SURFACE, fg=TEXT, insertbackground=TEXT, selectbackground=ACCENT,
        selectforeground="#ffffff", relief=tk.FLAT, highlightthickness=1,
        highlightbackground=BORDER, highlightcolor=ACCENT, font=_ui_tk.FONT_UI)
    gui._glossary_text = gui._voice_ctx.glossary_text  # 测试兼容别名
    gui._attach_edit_menu(gui._voice_ctx.glossary_text)
    gloss_sb = ttk.Scrollbar(gloss_box, orient=tk.VERTICAL,
                             style="Vertical.TScrollbar",
                             command=gui._voice_ctx.glossary_text.yview)
    gloss_sb.pack(side=tk.RIGHT, fill=tk.Y)
    gui._voice_ctx.glossary_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
    gui._voice_ctx.glossary_text.configure(yscrollcommand=gloss_sb.set)
    gui._voice_ctx.glossary_hint = ttk.Label(
        body, text="", style="Muted.TLabel", justify=tk.LEFT,
        wraplength=SETTINGS_WRAP)
    gui._glossary_hint = gui._voice_ctx.glossary_hint  # 测试兼容别名
    gui._voice_ctx.glossary_hint.pack(anchor=tk.W, pady=(4, 4))
    gloss_row = ttk.Frame(body)
    gloss_row.pack(fill=tk.X)
    gui._glossary_save_btn = ttk.Button(gloss_row, text=t("保存词库"),
                                       command=lambda: gui._on_save_glossary())
    gui._glossary_save_btn.pack(side=tk.RIGHT)
    gui._voice_ctx.glossary_status = ttk.Label(gloss_row, text="", style="Muted.TLabel")
    gui._glossary_status = gui._voice_ctx.glossary_status  # 测试兼容别名
    gui._voice_ctx.glossary_status.pack(side=tk.LEFT)
    gui._refresh_glossary_box()


# ================================================================ 房间页

def build_settings_room(gui, body: ttk.Frame) -> None:
    """「房间」页：房间码 / 昵称。"""
    ttk.Label(body, text=t("房间"), style="Section.TLabel").pack(anchor=tk.W)

    form = ttk.Frame(body)
    form.pack(fill=tk.X, pady=(8, 0))
    ttk.Label(form, text=t("房间码:"), style="Dim.TLabel").grid(
        row=0, column=0, sticky="w")
    gui._room_code_var = tk.StringVar(value=gui._room_cfg.room_code)
    code_entry = ttk.Entry(form, textvariable=gui._room_code_var, width=12,
                           style="Key.TEntry", font=_ui_tk.FONT_UI)
    gui._room_code_entry = code_entry
    code_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))
    code_entry.bind("<Return>", lambda _e: gui._on_room_field_change())
    code_entry.bind("<FocusOut>", lambda _e: gui._on_room_field_change())
    gui._attach_edit_menu(code_entry)
    gui._room_gen_btn = ttk.Button(form, text=t("随机生成"),
                                   command=lambda: gui._on_room_generate())
    gui._room_gen_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))

    ttk.Label(form, text=t("昵称:"), style="Dim.TLabel").grid(
        row=1, column=0, sticky="w", pady=(6, 0))
    gui._room_nick_var = tk.StringVar(value=gui._room_cfg.nickname)
    nick_entry = ttk.Entry(form, textvariable=gui._room_nick_var, width=12,
                           style="Key.TEntry", font=_ui_tk.FONT_UI)
    gui._room_nick_entry = nick_entry
    nick_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
    nick_entry.bind("<Return>", lambda _e: gui._on_room_field_change())
    nick_entry.bind("<FocusOut>", lambda _e: gui._on_room_field_change())
    gui._attach_edit_menu(nick_entry)

    ttk.Label(body,
              text=t("和填了同一个房间码的人互相看到对方说的话；"
                     "只有你自己说的话会被发出去。"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(10, 0))
    ttk.Label(body, text=t("改动会即时保存；已连接时按新设置重连。"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(4, 0))


# ================================================================ 关于页

def build_settings_about(gui, body: ttk.Frame) -> None:
    """「关于」页：软件更新 + 日志 + 开发者署名 + 赞助者。"""
    # ---- 软件更新 ----
    upd_head = ttk.Frame(body)
    upd_head.pack(fill=tk.X)
    gui._update_check_btn = ttk.Button(
        upd_head, text=t("检查更新"),
        command=lambda: gui._schedule_update_check(manual=True))
    gui._update_check_btn.pack(side=tk.RIGHT)
    ttk.Label(upd_head, text=t("软件更新"), style="Section.TLabel").pack(side=tk.LEFT)
    gui._update_info = ttk.Label(
        body, text=t("当前版本 v{ver} · 启动时会自动检查一次", ver=display_version()),
        style="Muted.TLabel", justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._update_info.pack(anchor=tk.W, pady=(6, 0))

    # ---- 日志 ----
    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
    log_head = ttk.Frame(body)
    log_head.pack(fill=tk.X)
    gui._log_export_btn = ttk.Button(log_head, text=t("导出日志压缩包…"),
                                     command=lambda: on_export_logs(gui))
    gui._log_export_btn.pack(side=tk.RIGHT)
    gui._log_open_btn = ttk.Button(log_head, text=t("打开日志文件夹"),
                                   command=lambda: on_open_log_folder(gui))
    gui._log_open_btn.pack(side=tk.RIGHT, padx=(0, 6))
    ttk.Label(log_head, text=t("日志"), style="Section.TLabel").pack(side=tk.LEFT)
    gui._log_info = ttk.Label(body, text="", style="Muted.TLabel",
                              justify=tk.LEFT, wraplength=SETTINGS_WRAP)
    gui._log_info.pack(anchor=tk.W, pady=(6, 0))
    refresh_log_info(gui)

    # ---- 开发者 ----
    ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
    ttk.Label(body, text=t("开发者"), style="Section.TLabel").pack(anchor=tk.W)
    ttk.Label(body, text=t("由可爱的赛博巫师和他的朋友们 开发"),
              style="Muted.TLabel", justify=tk.LEFT,
              wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

    # ---- 赞助者 ----
    gui._sponsor_names_widgets = []
    if SPONSORS:
        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
        ttk.Label(body, text=f"❤ {t('赞助者')}",
                  style="Section.TLabel").pack(anchor=tk.W)
        flow = ttk.Frame(body)
        flow.pack(fill=tk.X, pady=(8, 0))
        build_sponsor_list(flow, gui)


def build_sponsor_list(flow: ttk.Frame, gui) -> None:
    """把赞助者名字排成自动换行的亮字名单。"""
    font = tkfont.Font(font=_ui_tk.FONT_BOLD_MD)
    gap_x, gap_y = 16, 6
    row = ttk.Frame(flow)
    row.pack(anchor=tk.W)
    used = 0
    for name in SPONSORS:
        need = font.measure(name)
        if used and used + gap_x + need > SETTINGS_WRAP:
            row = ttk.Frame(flow)
            row.pack(anchor=tk.W, pady=(gap_y, 0))
            used = 0
        item = tk.Label(row, text=name, font=_ui_tk.FONT_BOLD_MD,
                        fg=COLOR_SRC_MINE, bg=PANEL)
        item.pack(side=tk.LEFT, padx=(0 if used == 0 else gap_x, 0))
        used += (0 if used == 0 else gap_x) + need
        gui._sponsor_names_widgets.append(item)


# ================================================================ 日志目录 / 导出

def log_dir(gui) -> Path:
    """日志目录路径。"""
    from .crashlog import _LOG_PATH
    if _LOG_PATH is not None:
        return Path(_LOG_PATH).parent
    return ROOT / "logs"


def refresh_log_info(gui) -> None:
    """把日志目录/体积/上限显示出来。"""
    from .crashlog import MAX_LOG_TOTAL_BYTES

    d = log_dir(gui)
    try:
        files = [p for p in d.glob("*.log*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
    except OSError:
        files, total = [], 0
    cap_mb = MAX_LOG_TOTAL_BYTES // 1024 // 1024
    gui._log_info.config(
        text=t("共 {n} 个文件，{size} MB（超过 {cap} MB 自动删最旧的）"
               "\n{path}\n出问题时导出压缩包发给维护者即可"
               "（自动脱敏，不含密钥）",
               n=len(files), size=f"{total / 1024 / 1024:.1f}",
               cap=cap_mb, path=d))


def on_export_logs(gui) -> None:
    """把日志打成 zip 到用户指定位置。"""
    import datetime as _dt
    from tkinter import filedialog, messagebox
    from .crashlog import export_logs

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    # Tk 的文件列表（::tk::IconList）canvas 底色写死在 Tk 里，样式 / option database 都够不着
    # → 只能等它建好再补色。纯配色，失败静默降级（详见 vlt/ui_tk.py 里那段说明）。
    _ui_tk.watch_file_dialog(gui._root)
    try:
        dest = filedialog.asksaveasfilename(
            parent=gui._settings_win, title=t("导出日志压缩包"),
            initialfile=f"vrchat-livetranslate-logs-{stamp}.zip",
            defaultextension=".zip",
            filetypes=[(t("ZIP 压缩包"), "*.zip"), (t("所有文件"), "*.*")])
    except Exception as exc:
        gui._set_status("error", t("打不开保存对话框：{msg}", msg=exc))
        return
    if not dest:
        print("[gui] 导出日志：用户取消", flush=True)
        return
    try:
        path, n, size, redacted = export_logs(Path(dest), log_dir=log_dir(gui))
    except Exception as exc:
        gui._set_status("error", t("导出日志失败：{msg}", msg=exc))
        print(f"[gui] ❌ 导出日志失败：{type(exc).__name__}: {exc}", flush=True)
        return
    msg_log = f"日志已导出：{path}（{n} 个文件，{size / 1024:.0f} KB）"
    msg_ui = t("日志已导出：{path}（{n} 个文件，{size} KB）",
               path=path, n=n, size=f"{size / 1024:.0f}")
    if redacted:
        msg_log += f"｜已脱敏 {len(redacted)} 个文件"
        msg_ui += t("｜已脱敏 {n} 个文件", n=len(redacted))
    gui._set_status("ok", msg_ui)
    print(f"[gui] ✅ {msg_log}", flush=True)
    refresh_log_info(gui)
    try:
        messagebox.showinfo(t("导出完成"),
                            f"{msg_ui}\n\n{t('把这个压缩包发给维护者即可。')}",
                            parent=gui._settings_win)
    except Exception:
        pass


def on_open_log_folder(gui) -> None:
    """打开日志文件夹。"""
    d = log_dir(gui)
    try:
        d.mkdir(parents=True, exist_ok=True)
        platform.open_path(str(d))
    except Exception as exc:
        print(f"[gui] ⚠️ 打不开日志文件夹（{d}）：{type(exc).__name__}: {exc}",
              flush=True)
        gui._set_status("error", t("打不开日志文件夹：{msg}", msg=exc))
        return
    print(f"[gui] 已打开日志文件夹：{d}", flush=True)


# ================================================================ 页面选择 / 打开 / 关闭

_SETTINGS_PAGE_TAB = {
    "room": "房间", "general": "常规",
    "wrist": "手腕屏", "desktop": "桌面字幕",
}


def select_settings_page(gui, page: str) -> None:
    """把设置弹窗切到某一页。"""
    title = _SETTINGS_PAGE_TAB.get(page)
    if not title:
        return
    try:
        tab = gui._settings_tabs.get(t(title))
        if tab is not None and gui._settings_nb is not None:
            gui._settings_nb.select(tab)
    except Exception as exc:  # noqa: BLE001
        print(f"[ui] ⚠️ 设置弹窗切到「{title}」页失败（不影响使用）："
              f"{type(exc).__name__}: {exc}", flush=True)


def open_settings(gui, page: str | None = None) -> None:
    """打开设置弹窗。"""
    win = gui._settings_win
    gui._refresh_key_status()
    gui._refresh_glossary_box()
    win.update_idletasks()
    ww, wh = gui._settings_size
    rx, ry = gui._root.winfo_x(), gui._root.winfo_y()
    rw = gui._root.winfo_width()
    x, y = rx + max((rw - ww) // 2, 20), ry + 48
    screen_w = int(win.winfo_screenwidth() or 0)
    screen_h = int(win.winfo_screenheight() or 0)
    if screen_w:
        x = max(8, min(x, screen_w - ww - 8))
    if screen_h:
        y = max(8, min(y, screen_h - wh - 48))
    win.geometry(f"{ww}x{wh}+{x}+{y}")
    win.deiconify()
    win.lift()
    win.focus_set()
    if page:
        select_settings_page(gui, page)
    gui._apply_dark_titlebar(win)
    sync_settings_pages(gui)
    gui._sync_gate_level_probe()
    gui._refresh_level_readout()      # 起「译音音量」实时读数的 500ms tick（可见时才续期）


def close_settings(gui) -> None:
    """关闭设置弹窗。"""
    gui._settings_win.withdraw()
    gui._sync_gate_level_probe()
    gui._refresh_level_readout()      # 窗口已不可见 → 取消挂起的 tick 且不再续期


# ================================================================ 界面语言

def on_ui_lang_change(gui, _event=None) -> None:
    """选完立即写 ui.lang。"""
    name = gui._ui_lang_var.get()
    code = next((c for c, n in gui._ui_lang_names.items() if n == name), "zh")
    save_ui_language(gui, code)
    lang_name = gui._ui_lang_names.get(code, name)
    gui._ui_lang_note.configure(
        text=t("已保存：重启程序后界面将切换为 {lang}", lang=lang_name))
    gui._set_status("info",
                    t("界面语言已保存：{lang}（重启程序后生效）", lang=lang_name))
    print(f"[gui] 界面语言已选择：{code}（{lang_name}），"
          f"已写入 ui.lang，重启后生效", flush=True)


def save_ui_language(gui, code: str) -> None:
    """把界面语言写进 config.yaml 的 ui.lang。"""
    p = _config_mod.DEFAULT_CONFIG
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
        if not re.search(r"^ui:", text, re.M):
            text = text.rstrip("\n") + "\n\n# 界面上次的选择（启动时自动恢复，不用手改）\nui:\n"
        text = _yaml_set_in_text(text, ["ui", "lang"], code)
        _write_config_text(p, text)
        if isinstance(gui._cfg.ui, dict):
            gui._cfg.ui["lang"] = code
    except Exception as exc:
        print(f"[gui] 保存界面语言失败：{exc}", flush=True)
