"""Tkinter 图形界面：聊天气泡视图 + 开关 + 语言镜像 + 双向同时。

用法：
  python -m vlt.gui                    # 启动界面
  python -m vlt.gui --self-test        # 自动化验收（单方向，不起窗口）
  python -m vlt.gui --self-test-dual   # 双向同时验收（两个 PCM 驱动两个引擎）
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import yaml

from .config import Direction, DEFAULT_CONFIG, load_config
from . import crashlog
from .devices import (
    DeviceInfo,
    enumerate_audio_out_devices,
    enumerate_loopback_devices,
    enumerate_mic_devices,
    format_device_display,
)
from .engine import Engine, EngineEvents

ROOT = Path(__file__).resolve().parent.parent

FONT = ("Microsoft YaHei UI", 11)          # 译文（主）
FONT_SMALL = ("Microsoft YaHei UI", 9)     # 原文（辅，小一号）
FONT_META = ("Microsoft YaHei UI", 8)
FONT_UI = ("Microsoft YaHei UI", 9)        # 控件文字
FONT_STATUS = ("Microsoft YaHei UI", 8)    # 状态栏
MAX_BUBBLES = 500

# ---- 统一配色：深灰 + 蓝（明度阶梯：聊天区最暗 → 面板次之 → 控件最亮） ----
BG            = "#14161c"   # 聊天区背景（最暗）
PANEL         = "#1b1e26"   # 顶栏 / 状态栏 / 窗口底色
SURFACE       = "#262a33"   # 按钮 / 下拉框 / 指示器底色（最亮一档）
SURFACE_HOVER = "#303541"   # 悬停
BORDER        = "#2e333d"   # 边框 / 分割线
ACCENT        = "#2f6fd0"   # 主色蓝（与"我说的"气泡同色）
ACCENT_HOVER  = "#3a7de0"
ACCENT_ACTIVE = "#2559a8"   # 按下
TEXT          = "#e8eaee"   # 主文字
TEXT_DIM      = "#9aa1ad"   # 次要文字
TEXT_MUTED    = "#6f7480"   # 时间戳 / 占位
COLOR_MINE    = ACCENT      # 气泡：我说的
COLOR_THEIRS  = "#33363f"   # 气泡：别人说的
COLOR_TEXT    = "#ffffff"
COLOR_META    = TEXT_MUTED
COLOR_OK      = "#4a90d9"   # 状态栏 info
COLOR_WARN    = "#d9904a"
COLOR_ERROR   = "#e05a5a"
# 原文小字的颜色：比译文暗一档但仍清晰可读（按气泡底色分别取，保证对比度）
COLOR_SRC_MINE = "#c3d4ee"
COLOR_SRC_THEIRS = TEXT_DIM

SOURCE_LANGS = {
    "自动检测": None,
    "中文": "zh",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
}

TARGET_LANGS = {
    "中文": "zh",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
}


def _source_name(code: str | None) -> str:
    return next((k for k, v in SOURCE_LANGS.items() if v == code), "自动检测")


def _target_name(code: str) -> str:
    return next((k for k, v in TARGET_LANGS.items() if v == code), "英语")


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """圆角矩形：polygon + smooth=True 才有圆角。"""
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
           x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


@dataclass
class _Bubble:
    """一条聊天气泡。items = 这条气泡占用的 Canvas 图元（就地重画时整体删掉）。"""
    who: str
    source: str
    text: str
    ts: str
    final: bool = False
    y: int = 0
    h: int = 0
    items: list = field(default_factory=list)


class TranslationGUI:
    """主界面。headless=True 时不创建 Tk 窗口（给 --self-test / --self-test-dual 用）。"""

    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._q: queue.Queue = queue.Queue()
        self._engines: list[Engine] = []
        self._engine_dirs: list[str] = []      # 与 _engines 一一对应
        self._specs: list[tuple] = []
        self._sinks: set[str] = set()
        self._pending_starts = 0
        self._start_job: str | None = None
        self._bubbles: list[_Bubble] = []
        self._current: dict[str, _Bubble] = {}  # 每个方向一条正在流式刷新的气泡
        self._auto_scroll = True
        self._stats: dict = {}
        self._canvas_w = 600
        self._relayout_job: str | None = None
        self._last_status_level = ""

        # 设备选择
        self._mic_names: list[str] = []
        self._loopback_names: list[str] = []
        self._audio_out_names: list[str] = []
        self._device_scan_pending = False

        self._cfg = load_config()
        mine = self._cfg.directions.get("mine")
        # 语言对：我的语言 A ↔ 对方语言 B。别人说方向自动镜像（B → A）。
        self._lang_pair = {
            "source": mine.source_lang if mine else "zh",
            "target": (mine.target_lang if mine else "en") or "en",
        }

        if not headless:
            self._build_ui()

    # ================================================================ UI 构建

    def _build_ui(self) -> None:
        self._root = tk.Tk()
        self._root.title("VRChat 实时同传")
        self._root.geometry("920x620")
        self._root.minsize(860, 460)      # 下限要保证设备行三个下拉 + 刷新按钮都放得下
        self._root.configure(bg=PANEL)

        self._apply_theme()          # 必须先于任何控件创建
        self._build_controls()
        self._build_device_row()
        self._divider()
        self._build_chat()
        self._divider()
        self._build_status()
        self._apply_dark_titlebar()

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_direction_langs()
        self._check_api_key()
        self._poll()
        self._start_device_scan()

    # ================================================================ 主题

    def _apply_theme(self) -> None:
        """统一深色主题：深灰 + 蓝。

        ⚠️ Windows 上 ttk 默认主题（vista/xpnative）由系统绘制，
        style.configure(background=...) 会被**静默忽略**——必须切到 clam。
        """
        root = self._root
        style = ttk.Style(root)
        style.theme_use("clam")

        style.configure(".", font=FONT_UI, background=PANEL, foreground=TEXT,
                        bordercolor=BORDER, focuscolor=PANEL)
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=TEXT)
        style.configure("Dim.TLabel", foreground=TEXT_DIM)
        style.configure("Muted.TLabel", foreground=TEXT_DIM, font=FONT_STATUS)
        style.configure("Status.TLabel", font=FONT_STATUS)

        # 按钮：扁平、无边框（clam 的按钮边框会带亮色 bevel，直接不要边框），
        # 悬停/按下有反馈；focuscolor 设成与背景同色，去掉点状焦点框
        style.configure("TButton", background=SURFACE, foreground=TEXT,
                        borderwidth=0, focusthickness=0, focuscolor=PANEL,
                        padding=(12, 7))
        style.map("TButton",
                  background=[("pressed", SURFACE_HOVER), ("active", SURFACE_HOVER),
                              ("disabled", "#20242d")],
                  foreground=[("disabled", TEXT_MUTED)])
        # 主按钮（开始翻译）：蓝色强调
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        borderwidth=0, focusthickness=0, focuscolor=ACCENT,
                        padding=(14, 6))
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_HOVER),
                              ("disabled", "#22374f")],
                  foreground=[("disabled", "#6b87ab")])

        # 下拉框：字段、箭头、边框都变深；readonly 下保持深色
        style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=TEXT_DIM, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, insertcolor=TEXT,
                        padding=(8, 4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", SURFACE)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", SURFACE)],   # 去掉选中文字的高亮白块
                  selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  arrowcolor=[("active", TEXT)])
        # 下拉弹出的列表是独立 Listbox，必须单独配色（否则弹出来是白的）
        root.option_add("*TCombobox*Listbox.background", SURFACE)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        root.option_add("*TCombobox*Listbox.font", FONT_UI)

        # 滚动条：细、暗、无箭头，跟聊天区融合
        style.layout("Vertical.TScrollbar",
                     [("Vertical.Scrollbar.trough",
                       {"children": [("Vertical.Scrollbar.thumb",
                                      {"expand": "1", "sticky": "nswe"})],
                        "sticky": "ns"})])
        style.configure("Vertical.TScrollbar", background=SURFACE, troughcolor=BG,
                        bordercolor=BG, darkcolor=BG, lightcolor=BG,
                        arrowcolor=TEXT_DIM, gripcount=0)
        style.map("Vertical.TScrollbar",
                  background=[("pressed", ACCENT_ACTIVE), ("active", SURFACE_HOVER)])

    def _apply_dark_titlebar(self) -> None:
        """Windows 标题栏变深色；老系统不支持就静默跳过（不能因此崩掉）。"""
        try:
            import ctypes
            self._root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):          # 20 = Win10 20H1+，19 = 更早版本
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    def _divider(self) -> None:
        """1px 深色分割线：用明度差 + 分隔线表达层次，不用 3D 边框。"""
        tk.Frame(self._root, bg=BORDER, height=1, bd=0,
                 highlightthickness=0).pack(fill=tk.X)

    def _build_controls(self) -> None:
        ctrl = ttk.Frame(self._root, padding=(14, 10))
        ctrl.pack(fill=tk.X)

        self._start_btn = ttk.Button(ctrl, text="开始翻译", style="Accent.TButton",
                                     command=self._start)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._stop_btn = ttk.Button(ctrl, text="停止翻译", command=self._stop,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 20))

        ttk.Label(ctrl, text="方向:").pack(side=tk.LEFT)
        dir_frame = ttk.Frame(ctrl)
        dir_frame.pack(side=tk.LEFT, padx=(0, 20))
        self._direction_var = tk.StringVar(value="mine")
        # 单选/复选框用经典 tk 控件：ttk 的指示器在 clam 下也吃不准颜色，
        # 经典控件的 bg/fg/selectcolor 一定可控，扁平且与面板融为一体。
        radio_kw = dict(variable=self._direction_var, command=self._on_direction_change,
                        **self._indicator_kw())
        tk.Radiobutton(dir_frame, text="我说", value="mine",
                       **radio_kw).pack(side=tk.LEFT, padx=(4, 0))
        tk.Radiobutton(dir_frame, text="别人说", value="theirs",
                       **radio_kw).pack(side=tk.LEFT, padx=(10, 0))
        tk.Radiobutton(dir_frame, text="双向同时", value="dual",
                       **radio_kw).pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(ctrl, text="源:").pack(side=tk.LEFT)
        self._source_combo = ttk.Combobox(ctrl, values=list(SOURCE_LANGS.keys()),
                                           state="readonly", width=10)
        self._source_combo.pack(side=tk.LEFT, padx=(4, 12))
        self._source_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

        ttk.Label(ctrl, text="目标:").pack(side=tk.LEFT)
        self._target_combo = ttk.Combobox(ctrl, values=list(TARGET_LANGS.keys()),
                                           state="readonly", width=10)
        self._target_combo.pack(side=tk.LEFT, padx=(4, 20))
        self._target_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

        # 输出勾选框**单独一行**：和方向/语言挤在同一行时整行需要 1062px，
        # 而窗口默认只有 920px —— 超出部分会被 Tk 直接裁掉，
        # 表现就是「某个选项莫名消失」（用户实测看不到「手腕屏」勾选框）。
        out_frame = ttk.Frame(self._root, padding=(14, 0))
        out_frame.pack(fill=tk.X)
        ttk.Label(out_frame, text="输出:").pack(side=tk.LEFT)
        self._chatbox_var = tk.BooleanVar(value=True)
        self._overlay_var = tk.BooleanVar(value=False)
        # 译音输出：把「我说的话」的译音回灌进虚拟声卡，VRChat 里的对方就能听见外语 TTS。
        # 这是**总开关**；config 里 directions.<X>.output_audio 是方向级开关，两者是「与」关系。
        _audio_cfg = (self._cfg.output.get("audio") or {}) if isinstance(self._cfg.output, dict) else {}
        self._vmic_var = tk.BooleanVar(value=bool(_audio_cfg.get("enabled", False)))
        tk.Checkbutton(out_frame, text="chatbox", variable=self._chatbox_var,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(6, 0))
        tk.Checkbutton(out_frame, text="手腕屏", variable=self._overlay_var,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(10, 0))
        tk.Checkbutton(out_frame, text="译音输出", variable=self._vmic_var,
                       command=self._save_audio_flag,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(10, 0))

    @staticmethod
    def _indicator_kw() -> dict:
        """经典 tk 单选/复选框的统一配色：选中时指示器变蓝，其余深灰。"""
        return dict(bg=PANEL, fg=TEXT, activebackground=PANEL,
                    activeforeground="#ffffff", selectcolor=SURFACE,
                    highlightthickness=0, bd=0, font=FONT_UI)

    def _build_device_row(self) -> None:
        row = ttk.Frame(self._root, padding=(14, 6))
        row.pack(fill=tk.X)

        auto = "自动检测"

        # ⚠️ 「刷新」按钮必须先打包（side=RIGHT）：Tk 的 pack 空间不足时**先挤压
        # 最后打包的控件**，按钮若最后打包会被挤成 1px（实测窗口 860 宽时按钮消失）。
        # 先占住右侧，让下拉框去吸收压缩。
        self._refresh_btn = ttk.Button(row, text="刷新", width=5,
                                       command=self._on_refresh_devices)
        self._refresh_btn.pack(side=tk.RIGHT, padx=(8, 0))

        ttk.Label(row, text="麦克风:", style="Dim.TLabel").pack(side=tk.LEFT)
        self._mic_combo = ttk.Combobox(row, values=[auto], state="readonly", width=24)
        # fill+expand：随窗口伸缩。设备名普遍 30~50 字符，固定宽度要么截断要么把
        # "刷新"按钮挤出窗口（实测固定 30 时设备行需要 1046px，窗口只有 920px）
        self._mic_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 12))
        self._mic_combo.set(auto)
        self._mic_combo.bind("<<ComboboxSelected>>", self._on_device_change)

        ttk.Label(row, text="VRChat 音频:", style="Dim.TLabel").pack(side=tk.LEFT)
        self._loopback_combo = ttk.Combobox(row, values=[auto], state="readonly", width=24)
        self._loopback_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 12))
        self._loopback_combo.set(auto)
        self._loopback_combo.bind("<<ComboboxSelected>>", self._on_device_change)

        ttk.Label(row, text="译音输出:", style="Dim.TLabel").pack(side=tk.LEFT)
        self._audio_out_combo = ttk.Combobox(row, values=[auto], state="readonly", width=24)
        self._audio_out_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 12))
        self._audio_out_combo.set(auto)
        self._audio_out_combo.bind("<<ComboboxSelected>>", self._on_device_change)

        # 从配置恢复选择
        capture_cfg = (self._cfg.output or {}).get("capture") or {}
        audio_cfg = (self._cfg.output or {}).get("audio") or {}
        self._mic_combo.set(capture_cfg.get("mic_device") or auto)
        self._loopback_combo.set(capture_cfg.get("loopback_device") or auto)
        self._audio_out_combo.set(audio_cfg.get("device_name") or auto)

    def _build_chat(self) -> None:
        chat_frame = ttk.Frame(self._root)
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # Treeview 不支持多行/换行，聊天气泡用 Canvas 手绘圆角矩形
        self._canvas = tk.Canvas(chat_frame, bg=BG, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=BORDER)
        self._vsb = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_canvas_scroll)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)

    def _build_status(self) -> None:
        bar = ttk.Frame(self._root, padding=(14, 7))
        bar.pack(fill=tk.X)
        # 左侧：彩色圆点（连接状态）+ 最新一条状态消息；右侧放统计汇总——两者分开，
        # 否则"等待收尾"这类瞬时消息会把"已翻译 N 条 / 首增量 Xms"覆盖掉。
        self._status_dot = tk.Label(bar, text="●", bg=PANEL, fg=TEXT_MUTED,
                                    font=FONT_STATUS, bd=0)
        self._status_dot.pack(side=tk.LEFT, padx=(0, 6))
        self._status_label = ttk.Label(bar, text="就绪", style="Status.TLabel")
        self._status_label.pack(side=tk.LEFT)
        self._stats_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self._stats_label.pack(side=tk.RIGHT)

    # ================================================================ 事件处理

    def _check_api_key(self) -> None:
        try:
            from .config import load_api_key
            load_api_key()
        except SystemExit as e:
            self._set_status("error", str(e))

    def _on_canvas_scroll(self, first: str, last: str) -> None:
        self._vsb.set(first, last)
        # 滚到底部才恢复自动跟随；用户手动往上翻时不抢
        self._auto_scroll = float(last) >= 0.99

    def _on_mousewheel(self, event) -> None:
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_canvas_configure(self, event=None) -> None:
        w = event.width if event is not None else self._canvas.winfo_width()
        if w <= 1 or abs(w - self._canvas_w) <= 2:
            return
        self._canvas_w = w
        # 拖拽窗口会连发 Configure：合并到 120ms 后一次性重排
        if self._relayout_job is not None:
            try:
                self._root.after_cancel(self._relayout_job)
            except Exception:
                pass
        self._relayout_job = self._root.after(120, self._redraw_all)

    def _on_direction_change(self) -> None:
        self._update_direction_langs()

    def _update_direction_langs(self) -> None:
        d = self._direction_var.get()
        a, b = self._lang_pair["source"], self._lang_pair["target"]
        if d == "theirs":
            # 别人说：源=对方语言（=我的目标），目标=我的语言（=我的源）
            src_code, tgt_code = b, a or "zh"
            self._source_combo.configure(values=list(TARGET_LANGS.keys()))
            self._target_combo.configure(values=list(TARGET_LANGS.keys()))
        else:
            src_code, tgt_code = a, b
            self._source_combo.configure(values=list(SOURCE_LANGS.keys()))
            self._target_combo.configure(values=list(TARGET_LANGS.keys()))
        self._source_combo.set(_source_name(src_code))
        self._target_combo.set(_target_name(tgt_code))

    def _on_lang_change(self, _event=None) -> None:
        d = self._direction_var.get()
        if d == "theirs":
            self._lang_pair["target"] = TARGET_LANGS.get(self._source_combo.get(),
                                                         self._lang_pair["target"])
            self._lang_pair["source"] = TARGET_LANGS.get(self._target_combo.get())
        else:
            self._lang_pair["source"] = SOURCE_LANGS.get(self._source_combo.get())
            self._lang_pair["target"] = TARGET_LANGS.get(self._target_combo.get(),
                                                         self._lang_pair["target"])
        if self._lang_pair["source"] is None:
            # 自动检测没有对应目标：别人说方向的目标回落到中文
            self._set_status("info", f"已切换为{_target_name(self._lang_pair['target'])} → 中文")
        self._save_lang_config()
        self._push_lang_to_engines()
        self._update_direction_langs()

    def _save_lang_config(self) -> None:
        """把互为镜像的两组方向写回 config.yaml（mine: A→B / theirs: B→A）。"""
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        pairs = {
            "mine": {"source_lang": a, "target_lang": b},
            "theirs": {"source_lang": b, "target_lang": a or "zh"},
        }
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            dirs = dict(raw.get("directions") or {})
            for name, langs in pairs.items():
                d = dict(dirs.get(name) or {})
                d["source_lang"] = langs["source_lang"]
                d["target_lang"] = langs["target_lang"]
                dirs[name] = d
            raw["directions"] = dirs
            with p.open("w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)
        except Exception as exc:
            print(f"[gui] 保存配置失败：{exc}")

    def _save_audio_flag(self) -> None:
        """把译音输出总开关写回 config.yaml（output.audio.enabled），勾了就一直记住。"""
        want = bool(self._vmic_var.get())
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            out = dict(raw.get("output") or {})
            audio = dict(out.get("audio") or {})
            audio["enabled"] = want
            out["audio"] = audio
            raw["output"] = out
            with p.open("w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)
            print(f"[gui] 译音输出总开关 → {'开' if want else '关'}（已写入 config.yaml）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存译音开关失败：{exc}", flush=True)

    def _push_lang_to_engines(self) -> None:
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        for name, (src, tgt) in (("mine", (a, b)), ("theirs", (b, a or "zh"))):
            d = self._cfg.directions.setdefault(name, Direction())
            d.source_lang = src
            d.target_lang = tgt
        for eng, direction in zip(self._engines, self._engine_dirs):
            if not eng.running:
                continue
            src, tgt = (a, b) if direction == "mine" else (b, a or "zh")
            eng.set_languages(src, tgt)

    # ================================================================ 引擎控制

    def _start(self) -> None:
        if any(e.running for e in self._engines):
            return
        d = self._direction_var.get()

        sinks: set[str] = set()
        if self._chatbox_var.get():
            sinks.add("chatbox")
        if self._overlay_var.get():
            sinks.add("overlay")
        if not sinks:
            self._set_status("warn", "请至少选择一个输出")
            return

        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        specs: list[tuple] = []
        if d in ("mine", "dual"):
            specs.append(("mine", "mine", "mic", a, b))
        if d in ("theirs", "dual"):
            specs.append(("theirs", "theirs", "loopback", b, a or "zh"))

        # 启动时把「方向 + 每条腿的来源/语言 + 输出面」写进日志。
        # 没有这行的话，事后只能从有没有 [loopback] 打印去反推方向——
        # 用户报「英文没翻译」时就是这样，明明是没起 loopback 腿，却看着像翻译坏了。
        _label = {"mine": "我说的话", "theirs": "别人说", "dual": "双向同时"}.get(d, d)
        print(f"[gui] 启动：方向={_label}({d}) | 输出={','.join(sorted(sinks)) or '无'} | "
              f"语言对={a}→{b}", flush=True)
        for _w, _dir, _src, _sl, _tl in specs:
            print(f"[gui]   腿 {_dir}：来源={'麦克风' if _src == 'mic' else '游戏音频(loopback)'}"
                  f" | {_sl}→{_tl}", flush=True)

        # 译音输出：勾选框（总开关）+ 方向级开关，两者是「与」关系，都开才出声。
        # 译音回灌只对「我说的话」有意义（对方要听的是我说的话的译文）；
        # 「别人说」的译文是我自己的母语，回灌进虚拟麦毫无意义 → 明确告知，不静默忽略。
        want_audio = bool(self._vmic_var.get())
        audio_warn = ""
        if want_audio and not any(s[1] == "mine" for s in specs):
            audio_warn = "译音输出只对「我说的话」方向有效（当前方向不含它）→ 本次已忽略"
            print(f"[gui] ⚠️ {audio_warn}", flush=True)
            want_audio = False
        if isinstance(self._cfg.output, dict):
            self._cfg.output.setdefault("audio", {})["enabled"] = want_audio
            _dev = (self._cfg.output.get("audio") or {}).get("device_name") or "自动回退链"
        else:
            _dev = "自动回退链"
        print(f"[gui]   译音输出={'开' if want_audio else '关'}（虚拟声卡：{_dev}）", flush=True)

        for _who, direction, _src, src_lang, tgt_lang in specs:
            dd = self._cfg.directions.setdefault(direction, Direction())
            dd.source_lang = src_lang
            dd.target_lang = tgt_lang
            dd.output_audio = want_audio and direction == "mine"

        self._current = {}
        self._auto_scroll = True
        self._engines = []
        self._engine_dirs = []
        self._specs = specs
        self._sinks = sinks
        self._pending_starts = len(specs)
        self._start_engine(0)
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        if audio_warn:
            self._set_status("warn", audio_warn)
        elif d == "mine":
            # 只翻自己的话时明确提示一句：用户放英文视频却没选对方向，
            # 表现就是「翻译坏了」，而实际是根本没采集对方/视频的声音。
            self._set_status("info", "正在启动…（只翻译你说的话；要翻译对方/视频请选「双向同时」）")
        else:
            self._set_status("info", "正在启动（双向）…" if len(specs) == 2 else "正在启动…")

    def _start_engine(self, index: int) -> None:
        specs = self._specs
        if index >= len(specs):
            return
        who, direction, source, src_lang, tgt_lang = specs[index]
        events = EngineEvents(
            on_text=lambda src, txt, final, who=who: self._q.put(("text", who, src, txt, final)),
            on_status=lambda lvl, msg, who=who: self._on_engine_status(lvl, msg, who),
            on_stats=lambda s: self._q.put(("stats", s)),
        )
        eng = Engine(
            cfg=self._cfg,
            direction=direction,
            source=source,
            sinks=self._sinks,
            events=events,
            config_path=DEFAULT_CONFIG,
        )
        self._engines.append(eng)
        self._engine_dirs.append(direction)
        self._pending_starts -= 1
        eng.start()
        if self._pending_starts > 0:
            # 两个引擎错开 300ms 启动，避免同时抢占音频设备
            self._start_job = self._root.after(300, self._start_engine, index + 1)

    def _on_engine_status(self, lvl: str, msg: str, who: str) -> None:
        """引擎状态既要进状态栏，也要进日志（stdout）。

        状态栏文字不落盘 —— 少了这一行，「译音输出为什么没出声」「设备为什么没匹配上」
        这类提示在用户发来的日志里完全看不到，只能靠猜。
        """
        print(f"[{who}][{lvl}] {msg}", flush=True)
        self._q.put(("status", lvl, msg))

    def _stop(self) -> None:
        self._pending_starts = 0
        self._specs = []
        if self._start_job is not None:
            try:
                self._root.after_cancel(self._start_job)
            except Exception:
                pass
            self._start_job = None
        for eng in self._engines:
            eng.stop()
        self._engines = []
        self._engine_dirs = []
        self._start_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)
        self._set_status("info", "已停止")

    def _on_close(self) -> None:
        """关窗口：先停引擎（会在超时内等采集线程真正退出），再销毁窗口。

        顺序很重要——如果先销毁窗口再去等引擎，主线程会阻塞在一个已经失效的
        Tk 事件循环上，界面看起来就是"卡死后闪退"。
        """
        try:
            self._stop()
        except Exception as exc:
            print(f"[gui] 停止引擎时出错（继续关闭）：{exc}", file=sys.stderr)
        try:
            self._root.destroy()
        except Exception:
            pass
        crashlog.close()

    # ================================================================ 设备选择

    def _start_device_scan(self) -> None:
        """扫描设备（**主线程同步执行**）。

        为什么不用后台线程：PortAudio 的初始化/销毁是**线程绑定**的（WASAPI 用 COM
        单元）。sounddevice 在首次调用的那个线程里 Pa_Initialize，而它的 atexit 钩子
        在主线程 Pa_Terminate —— 跨线程销毁会抛
        `Tcl_AsyncDelete: async handler deleted by the wrong thread`（实测退出码 3，
        更早的版本还直接段错误）。实测枚举冷启动 348ms、预热后只要 23ms，
        同步做完全可接受，换来的是确定性。
        """
        if self._headless:
            return                        # 无界面的自检模式不需要设备列表
        self._device_scan_pending = True
        try:
            self._root.update_idletasks()     # 先把"正在扫描…"画出来再阻塞
            mics = enumerate_mic_devices()
            loops = enumerate_loopback_devices()
            outs = enumerate_audio_out_devices()
            self._on_device_scan_result(mics, loops, outs)
        except Exception as exc:
            self._device_scan_pending = False
            self._set_status("warn", f"设备扫描失败：{exc}")

    def _on_refresh_devices(self) -> None:
        if any(e.running for e in self._engines):
            self._set_status("warn", "建议停止翻译后再刷新设备列表")
        self._start_device_scan()
        self._set_status("info", "正在扫描设备…")

    def _on_device_scan_result(self, mics, loops, outs) -> None:
        auto = "自动检测"
        self._mic_names = []
        mic_display = [auto]
        for info in mics:
            self._mic_names.append(info.name)
            mic_display.append(format_device_display(info))

        self._loopback_names = []
        loop_display = [auto]
        for info in loops:
            self._loopback_names.append(info.name)
            loop_display.append(format_device_display(info))

        self._audio_out_names = []
        out_display = [auto]
        for info in outs:
            self._audio_out_names.append(info.name)
            out_display.append(format_device_display(info))

        self._mic_combo.configure(values=mic_display)
        self._loopback_combo.configure(values=loop_display)
        self._audio_out_combo.configure(values=out_display)

        # 恢复配置里的选择（如果设备在列表里）
        capture_cfg = (self._cfg.output or {}).get("capture") or {}
        audio_cfg = (self._cfg.output or {}).get("audio") or {}
        mic_name = capture_cfg.get("mic_device") or ""
        loop_name = capture_cfg.get("loopback_device") or ""
        out_name = audio_cfg.get("device_name") or ""

        if mic_name and mic_name in self._mic_names:
            self._mic_combo.set(mic_display[self._mic_names.index(mic_name) + 1])
        else:
            self._mic_combo.set(auto)

        if loop_name and loop_name in self._loopback_names:
            self._loopback_combo.set(loop_display[self._loopback_names.index(loop_name) + 1])
        else:
            self._loopback_combo.set(auto)

        if out_name and out_name in self._audio_out_names:
            self._audio_out_combo.set(out_display[self._audio_out_names.index(out_name) + 1])
        else:
            self._audio_out_combo.set(auto)

        if not mics and not loops and not outs:
            self._set_status("warn", "未扫描到设备（远程会话下枚举为空是正常的）")
        else:
            self._set_status("info",
                f"已扫描到 {len(mics)} 个麦克风 / {len(loops)} 个 loopback / {len(outs)} 个输出")
        self._device_scan_pending = False

    def _on_device_change(self, _event=None) -> None:
        auto = "自动检测"
        mic_text = self._mic_combo.get()
        loop_text = self._loopback_combo.get()
        out_text = self._audio_out_combo.get()

        mic_name = ""
        if mic_text != auto and mic_text:
            display_list = list(self._mic_combo.cget("values"))
            idx = display_list.index(mic_text) if mic_text in display_list else -1
            if idx > 0 and idx - 1 < len(self._mic_names):
                mic_name = self._mic_names[idx - 1]

        loop_name = ""
        if loop_text != auto and loop_text:
            display_list = list(self._loopback_combo.cget("values"))
            idx = display_list.index(loop_text) if loop_text in display_list else -1
            if idx > 0 and idx - 1 < len(self._loopback_names):
                loop_name = self._loopback_names[idx - 1]

        out_name = ""
        if out_text != auto and out_text:
            display_list = list(self._audio_out_combo.cget("values"))
            idx = display_list.index(out_text) if out_text in display_list else -1
            if idx > 0 and idx - 1 < len(self._audio_out_names):
                out_name = self._audio_out_names[idx - 1]

        self._save_device_config(mic_name, loop_name, out_name)
        # 回显完整设备名：下拉框宽度有限（长设备名会被截断），
        # 状态栏给一次完整确认，免得用户不知道自己到底选了哪个。
        picked = [t for t in (mic_name, loop_name, out_name) if t]
        if picked:
            self._set_status("info", "已选设备：" + " | ".join(picked))
        else:
            self._set_status("info", "设备：全部自动检测")

    def _save_device_config(self, mic_name: str, loop_name: str, out_name: str) -> None:
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            capture = dict(raw.get("capture") or {})
            capture["mic_device"] = mic_name
            capture["loopback_device"] = loop_name
            raw["capture"] = capture
            output = dict(raw.get("output") or {})
            audio = dict(output.get("audio") or {})
            audio["device_name"] = out_name
            output["audio"] = audio
            raw["output"] = output
            with p.open("w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)
        except Exception as exc:
            print(f"[gui] 保存设备配置失败：{exc}")
            return
        # 同步内存里的配置，引擎启动时会读
        self._cfg.output.setdefault("capture", {})["mic_device"] = mic_name
        self._cfg.output["capture"]["loopback_device"] = loop_name
        self._cfg.output.setdefault("audio", {})["device_name"] = out_name

    # ================================================================ 队列轮询

    def _poll(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "text":
                    self._add_text(item[2], item[3], item[4], who=item[1])
                elif kind == "status":
                    self._set_status(item[1], item[2])
                elif kind == "stats":
                    self._stats.update(item[1])
                    self._refresh_status()
                elif kind == "devices":
                    self._on_device_scan_result(item[1], item[2], item[3])
                elif kind == "devices_error":
                    self._set_status("warn", f"设备扫描失败：{item[1]}")
                    self._device_scan_pending = False
        except queue.Empty:
            pass
        if (self._pending_starts == 0 and self._engines
                and all(not e.running for e in self._engines)):
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            # 引擎失败时状态栏已有 error 消息，别用"已停止"盖掉
            if self._last_status_level != "error":
                self._set_status("info", "已停止")
            self._engines = []
            self._engine_dirs = []
        self._root.after(50, self._poll)

    # ================================================================ 聊天气泡

    def _add_text(self, source: str, text: str, is_final: bool, who: str = "mine") -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        cur = self._current.get(who)
        if cur is not None:
            # 流式增量：就地重画同一条气泡（终版只做"封口"，绝不新插第二条）
            cur.source = source
            cur.text = text
            if is_final:
                cur.final = True
                self._current.pop(who, None)
            if hasattr(self, "_canvas"):
                self._redraw_current(cur)
            if self._auto_scroll and hasattr(self, "_canvas"):
                self._canvas.yview_moveto(1.0)
            return
        # 没有正在刷新的气泡 → 新建（一句话的第一条就是终版时走这里）
        b = _Bubble(who=who, source=source, text=text, ts=now_str, final=is_final)
        if not is_final:
            self._current[who] = b
        b.y = (self._bubbles[-1].y + self._bubbles[-1].h + 8) if self._bubbles else 8
        self._bubbles.append(b)
        if hasattr(self, "_canvas"):
            self._draw_bubble(b)
            self._trim()
            self._update_scrollregion()
            if self._auto_scroll:
                self._canvas.yview_moveto(1.0)

    def _draw_bubble(self, b: _Bubble) -> int:
        """按已验证配方画一条气泡，返回下一条气泡的起始 y。

        每条气泡两行：**原文小字（上） + 译文大字（下）**——主次分明，
        你要读的永远是下面那行大字。服务端没返回原文（或原文与译文相同）时
        只画译文，不留空行。
        """
        cv = self._canvas
        w = self._canvas_w if self._canvas_w > 1 else 600
        maxw = int(w * 0.62)                       # width 参数自带自动换行

        # 1) 先量尺寸：两张文字都先画在 (0,0)，量完再挪进气泡
        tid_big = cv.create_text(0, 0, text=b.text, width=maxw,
                                 anchor="nw", font=FONT, fill=COLOR_TEXT)
        bx1, by1, bx2, by2 = cv.bbox(tid_big)
        big_w, big_h = bx2 - bx1, by2 - by1

        tid_small = None
        small_w = small_h = 0
        src = (b.source or "").strip()
        if src and src != (b.text or "").strip():   # 原文为空或与译文相同时不画小字行
            tid_small = cv.create_text(0, 0, text=b.source, width=maxw, anchor="nw",
                                       font=FONT_SMALL,
                                       fill=COLOR_SRC_MINE if b.who == "mine" else COLOR_SRC_THEIRS)
            sx1, sy1, sx2, sy2 = cv.bbox(tid_small)
            small_w, small_h = sx2 - sx1, sy2 - sy1

        gap = 6 if tid_small is not None else 0
        pad_x, pad_y = 12, 9
        bw = max(big_w, small_w) + pad_x * 2       # 气泡贴合内容自适应
        bh = small_h + gap + big_h + pad_y * 2
        y = b.y
        items = [tid_big]
        if tid_small is not None:
            items.append(tid_small)
        if b.ts:
            items.append(cv.create_text(
                w - 18 if b.who == "mine" else 18, y, text=b.ts,
                anchor="ne" if b.who == "mine" else "nw",
                font=FONT_META, fill=COLOR_META))
            y += 14
        bx = w - 18 - bw if b.who == "mine" else 18  # 右 / 左
        fill = COLOR_MINE if b.who == "mine" else COLOR_THEIRS
        rid = round_rect(cv, bx, y, bx + bw, y + bh, 12, fill=fill, outline="")
        # ⚠️ 必须压到最底层（tag_lower 不带第二参数）。若写成
        # `for t in items: cv.tag_lower(rid, t)`，第二次迭代会把矩形挪到
        # 大号文字之上，译文会被气泡背景整条遮住——原型阶段实测踩过这个坑。
        cv.tag_lower(rid)

        ty = y + pad_y
        if tid_small is not None:
            cv.coords(tid_small, bx + pad_x, ty)
            ty += small_h + gap
        cv.coords(tid_big, bx + pad_x, ty)         # 译文大字在下方
        items.append(rid)
        b.items = items
        b.h = y + bh - b.y
        return y + bh + 8

    def _redraw_current(self, b: _Bubble) -> None:
        """流式增量：删掉旧图元，在同一个起始 y 位置重画同一条气泡。"""
        for iid in b.items:
            self._canvas.delete(iid)
        old_h = b.h
        self._draw_bubble(b)
        delta = b.h - old_h
        if delta:
            # 气泡长高时把下面的气泡整体下移，避免重叠（双向同时时会用到）
            idx = self._bubbles.index(b)
            for other in self._bubbles[idx + 1:]:
                other.y += delta
                if other.items:
                    self._canvas.move(*other.items, 0, delta)
            self._update_scrollregion()

    def _trim(self) -> None:
        """气泡超过上限时删最旧的若干条，剩余图元整体上移。"""
        removed = 0
        while len(self._bubbles) > MAX_BUBBLES:
            old = self._bubbles.pop(0)
            if self._current.get(old.who) is old:
                self._current.pop(old.who, None)
            removed += old.h + 8
            for iid in old.items:
                self._canvas.delete(iid)
        if removed:
            self._canvas.move("all", 0, -removed)
            for b in self._bubbles:
                b.y -= removed

    def _update_scrollregion(self) -> None:
        if not hasattr(self, "_canvas"):
            return
        content = 8
        if self._bubbles:
            last = self._bubbles[-1]
            content = last.y + last.h + 8
        h = self._canvas.winfo_height()
        self._canvas.configure(scrollregion=(0, 0, max(self._canvas_w, 1), max(content, h)))

    def _redraw_all(self) -> None:
        """窗口 resize 后按新宽度全部重画（消息多时也比逐条挪快）。"""
        self._relayout_job = None
        if not hasattr(self, "_canvas"):
            return
        self._canvas.delete("all")
        y = 8
        for b in self._bubbles:
            b.y = y
            y = self._draw_bubble(b)
        self._update_scrollregion()
        if self._auto_scroll:
            self._canvas.yview_moveto(1.0)

    # ================================================================ 状态栏

    def _set_status(self, level: str, msg: str) -> None:
        self._last_status_level = level
        if not hasattr(self, "_status_label"):
            return
        # 状态色走圆点，文字保持中性色——更现代，也不会整行刺眼
        colors = {"info": COLOR_OK, "warn": COLOR_WARN, "error": COLOR_ERROR}
        self._status_dot.configure(fg=colors.get(level, TEXT_MUTED))
        self._status_label.configure(text=f"状态：{msg}")

    def _refresh_status(self) -> None:
        if not hasattr(self, "_stats_label"):
            return
        parts: list[str] = []
        if any(e.running for e in self._engines):
            parts.append("运行中")
            # 常驻提示：状态栏正文会被引擎消息覆盖，这里不会。
            # 用户放英文视频却没选对方向时，症状看着就是「翻译坏了」。
            if self._direction_var.get() == "mine":
                parts.append("仅翻译你说的话")
        if any(e.chatbox is not None for e in self._engines):
            sent = sum(e.chatbox.sent_ok for e in self._engines if e.chatbox is not None)
            parts.append(f"已翻译 {sent} 条")
        ms = self._stats.get("first_delta_ms") or self._stats.get("connect_ms")
        if ms is not None:
            parts.append(f"首增量 {ms:.0f}ms")
        self._stats_label.configure(text=" · ".join(parts))

    # ================================================================ 自检

    def run_self_test(self) -> int:
        pcm_path = ROOT / "testdata" / "zh_test_16k.pcm"
        if not pcm_path.exists():
            print(f"GUI_SELFTEST_FAIL: 测试音频不存在 {pcm_path}", file=sys.stderr)
            return 1

        results: list[tuple] = []

        def on_text(src, txt, final):
            results.append((src, txt, final))

        def on_status(level, msg):
            print(f"[selftest][{level}] {msg}")

        cfg = load_config()
        cfg.directions["mine"].source_lang = "zh"
        cfg.directions["mine"].target_lang = "en"

        events = EngineEvents(on_text=on_text, on_status=on_status)
        engine = Engine(
            cfg=cfg, direction="mine", source=f"pcm:{pcm_path}",
            sinks={"chatbox"}, events=events, dry_run=True,
        )
        engine.start()
        engine.join(timeout=60)
        engine.stop(timeout=5)

        has_source = any(r[0].strip() for r in results)
        has_target = any(r[1].strip() for r in results)
        if has_source and has_target:
            print("GUI_SELFTEST_OK")
            return 0
        print(f"GUI_SELFTEST_FAIL: source={has_source} target={has_target} rows={len(results)}",
              file=sys.stderr)
        return 1

    def run_self_test_dual(self) -> int:
        """双向同时验收：两个测试 PCM 同时驱动两个引擎，左右两侧都必须出气泡。"""
        zh = ROOT / "testdata" / "zh_test_16k.pcm"
        en = ROOT / "testdata" / "en_test_16k.pcm"
        for p in (zh, en):
            if not p.exists():
                print(f"GUI_SELFTEST_DUAL_FAIL: 测试音频不存在 {p}", file=sys.stderr)
                return 1

        cfg = load_config()
        cfg.directions.setdefault("mine", Direction())
        cfg.directions.setdefault("theirs", Direction())
        cfg.directions["mine"].source_lang = "zh"
        cfg.directions["mine"].target_lang = "en"
        cfg.directions["theirs"].source_lang = "en"
        cfg.directions["theirs"].target_lang = "zh"

        engines: list[Engine] = []
        for who, direction, pcm_path in (("mine", "mine", zh), ("theirs", "theirs", en)):
            def on_text(src, txt, final, who=who):
                self._add_text(src, txt, final, who=who)

            events = EngineEvents(
                on_text=on_text,
                on_status=lambda lvl, msg, who=who: print(f"[dualtest][{who}][{lvl}] {msg}"),
            )
            eng = Engine(cfg=cfg, direction=direction, source=f"pcm:{pcm_path}",
                         sinks={"chatbox"}, events=events, dry_run=True)
            engines.append(eng)
            eng.start()
            time.sleep(0.3)      # 与 GUI 一致：错开 300ms，避免同时抢占音频设备
        for eng in engines:
            eng.join(timeout=90)
        for eng in engines:
            eng.stop(timeout=5)

        mine = [b for b in self._bubbles if b.who == "mine"]
        theirs = [b for b in self._bubbles if b.who == "theirs"]
        if mine and theirs:
            print(f"GUI_SELFTEST_DUAL_OK mine={len(mine)} theirs={len(theirs)}")
            return 0
        print(f"GUI_SELFTEST_DUAL_FAIL: mine={len(mine)} theirs={len(theirs)}",
              file=sys.stderr)
        return 1


# ================================================================ 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 - 图形界面")
    ap.add_argument("--self-test", action="store_true", help="自动化验收模式（单方向）")
    ap.add_argument("--self-test-dual", action="store_true",
                    help="双向同时验收：两个测试 PCM 同时驱动两个引擎")
    args = ap.parse_args()

    # 崩溃日志：闪退时窗口一关什么都没了，必须落盘。
    # 在创建界面之前装上，连启动阶段的崩溃也能抓到。
    crashlog.install(ROOT / "logs", "gui")
    crashlog.log_startup_info(f"gui {'--self-test-dual' if args.self_test_dual else args.self_test and '--self-test' or ''}")

    if args.self_test_dual:
        gui = TranslationGUI(headless=True)
        return gui.run_self_test_dual()
    if args.self_test:
        gui = TranslationGUI(headless=True)
        return gui.run_self_test()

    gui = TranslationGUI()
    crashlog.install_tk(gui._root)      # 接管 Tk 回调异常（默认只打印不退出）
    try:
        gui._root.mainloop()
    finally:
        crashlog.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
