"""Tkinter 图形界面：聊天气泡视图 + 开关 + 语言镜像 + 双向同时。

用法：
  python -m vlt.gui                    # 启动界面
  python -m vlt.gui --self-test        # 自动化验收（单方向，不起窗口）
  python -m vlt.gui --self-test-dual   # 双向同时验收（两个 PCM 驱动两个引擎）
"""
from __future__ import annotations

import argparse
import queue
import re
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
from .output.overlay import OverlayConfig, WristOverlay
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


def _yaml_set_in_text(text: str, path: list[str], value: str) -> str:
    """在 YAML 文本里**就地**改一个叶子值，保留注释、空行与键的顺序。

    为什么不用 `yaml.safe_load` + `yaml.dump` 整文件重写：那会抹平所有注释和顺序
    （实测把一份带完整中文说明的 config.yaml 变成一坨没有注释的键值对，键还被按字母重排）。
    配置文件是给人读的，程序存个设置不该毁掉它的可读性。
    找不到路径就返回原文——宁可这次没生效，也不退化成整文件重写。
    """
    lines = text.split("\n")

    def _span(key: str, indent: int, lo: int, hi: int):
        head = re.compile(rf"^(\s*){re.escape(key)}:\s*$")
        for i in range(lo, hi):
            m = head.match(lines[i])
            if m is None or len(m.group(1)) != indent:
                continue
            sub_hi = hi
            for j in range(i + 1, hi):
                if lines[j].strip() and not lines[j].startswith(" " * (indent + 1)):
                    sub_hi = j
                    break
            return i, sub_hi
        return None

    lo, hi, indent = 0, len(lines), 0
    for key in path[:-1]:
        got = _span(key, indent, lo, hi)
        if got is None:
            return text
        lo, hi = got[0] + 1, got[1]
        indent += 2

    leaf = path[-1]
    pat = re.compile(rf"^(\s*){re.escape(leaf)}:(\s*)([^#\n]*)(\s*#.*)?$")
    for i in range(lo, hi):
        m = pat.match(lines[i])
        if m and len(m.group(1)) == indent:
            comment = (m.group(4) or "").strip()
            new_line = f"{m.group(1)}{leaf}: {value}" + (f"   {comment}" if comment else "")
            # ⚠️ 关键：如果这一项的旧值是**多行块**（块序列 / 嵌套映射），必须把子行一并删掉，
            # 否则会留下孤立的 `- 0.0` 之类 → 整个文件变成非法 YAML。
            # 用户实测踩过：旧版整文件 yaml.dump 会把 `pos: [0.0, 0.06, 0.02]` 写成
            #    pos:
            #    - 0.0
            #  而本函数当时只换了 `pos:` 那一行，热重载就报
            #  `expected <block end>, but found '-'`，界面上拖滑块完全没效果。
            j = i + 1
            while j < hi and lines[j].strip():
                stripped = lines[j].lstrip()
                ind_j = len(lines[j]) - len(stripped)
                # 更深的缩进 = 属于本键的块；同级但以 "- " 开头 = 块序列（PyYAML 默认就不缩进）
                if ind_j > indent or (ind_j == indent and stripped.startswith("- ")):
                    j += 1
                    continue
                break
            del lines[i + 1:j]
            lines[i] = new_line
            return "\n".join(lines)
    lines.insert(hi, f"{' ' * indent}{leaf}: {value}")
    return "\n".join(lines)


def _write_config_text(path: Path, text: str) -> None:
    """写回配置前先验证仍是合法 YAML。

    宁可这次改动不生效（调用方会 catch 并打印），也**绝不能把用户的配置写坏** ——
    配置坏了影响的是启动，比一个滑块没生效严重得多。
    """
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"生成的新配置不是合法 YAML，已放弃写入：{exc}") from exc
    path.write_text(text, encoding="utf-8")


def _fmt_scalar(x) -> str:  # noqa: ANN001
    """None → null；float → 紧凑写法（0.24 而不是 0.24000000000000002）。"""
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        return f"{x:g}"
    return str(x)


class TranslationGUI:
    """主界面。headless=True 时不创建 Tk 窗口（给 --self-test / --self-test-dual 用）。"""

    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._q: queue.Queue = queue.Queue()
        self._engines: list[Engine] = []
        self._engine_dirs: list[str] = []      # 与 _engines 一一对应
        # 手腕屏由**界面**持有（不是某个引擎）：手腕上只该有一块屏，内容镜像聊天区，
        # 而聊天区本来就在界面这一层（两个方向的文字都汇到这里）。
        self._overlay_out: WristOverlay | None = None
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
        self._root.geometry("920x600")
        self._root.minsize(860, 460)      # 下限保证第一行（开/停+方向+语言对+设置）不裁切
        self._root.configure(bg=PANEL)

        self._apply_theme()          # 必须先于任何控件创建
        # 信息架构：主界面只留**高频**操作（开/停、方向、语言、输出、看译文），
        # 低频设置（API key、音频设备）收进「⚙ 设置」弹窗 —— 见 _build_settings_dialog。
        self._build_controls()       # 第一行：开/停 + 方向 + 语言对（会话控制）
        self._build_output_row()     # 第二行：输出勾选 + 手腕屏微调 + key 状态入口
        self._divider()
        self._build_chat()
        self._divider()
        self._build_status()
        self._build_settings_dialog()  # 先建好再隐藏：控件属性必须随即可用（测试直接访问）
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
        # 分区小标题（设置弹窗里的「API KEY / 音频设备」）：小一号、暗色、加粗
        style.configure("Section.TLabel", foreground=TEXT_DIM,
                        font=("Microsoft YaHei UI", 8, "bold"))
        # key 状态入口（第二行右侧的小按钮）：比常规按钮矮半档，不抢视觉
        style.configure("Chip.TButton", font=FONT_STATUS, padding=(8, 3))
        # 分割线/分组竖线：用 1px 明度差表达层次，不用 3D 边框
        style.configure("TSeparator", background=BORDER)

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

        # API key 输入行：不加这条会沿用 clam 的浅色默认底 —— 深色界面里出现一块白，很扎眼
        # （截图复核时发现的）。字段底/文字/插入符/边框全部对齐 SURFACE/TEXT/BORDER 体系。
        style.configure("Key.TEntry", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, padding=(8, 4))
        style.map("Key.TEntry",
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  fieldbackground=[("disabled", BG), ("readonly", SURFACE)],
                  foreground=[("disabled", TEXT_DIM)])

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

    def _apply_dark_titlebar(self, win=None) -> None:
        """Windows 标题栏变深色；老系统不支持就静默跳过（不能因此崩掉）。"""
        try:
            import ctypes
            w = win if win is not None else self._root
            w.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(w.winfo_id())
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

    @staticmethod
    def _vsep(parent) -> None:
        """组间竖向分隔线：比留白更明确地表达「这里换了一组」。"""
        ttk.Separator(parent, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y,
                                                       padx=12, pady=5)

    def _build_controls(self) -> None:
        """第一行 = 会话控制：开/停 | 方向 | 语言对。VR 里最高频的操作全在这行。"""
        ctrl = ttk.Frame(self._root, padding=(14, 12, 14, 8))
        ctrl.pack(fill=tk.X)

        # 「⚙ 设置」先打包（side=RIGHT）：窗口变窄时 Tk 先挤压后打包的控件，
        # 先占住右侧入口，压缩只会发生在左侧分组之间的留白上。
        ttk.Button(ctrl, text="⚙ 设置", width=8,
                   command=self._open_settings).pack(side=tk.RIGHT)

        self._start_btn = ttk.Button(ctrl, text="开始翻译", style="Accent.TButton",
                                     command=self._start)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._stop_btn = ttk.Button(ctrl, text="停止翻译", command=self._stop,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT)

        self._vsep(ctrl)
        ttk.Label(ctrl, text="方向:").pack(side=tk.LEFT)
        dir_frame = ttk.Frame(ctrl)
        dir_frame.pack(side=tk.LEFT)
        self._direction_var = tk.StringVar(value=str((self._cfg.ui or {}).get("direction", "mine")))
        if self._direction_var.get() not in ("mine", "theirs", "dual"):
            self._direction_var.set("mine")
        # 单选/复选框用经典 tk 控件：ttk 的指示器在 clam 下也吃不准颜色，
        # 经典控件的 bg/fg/selectcolor 一定可控，扁平且与面板融为一体。
        radio_kw = dict(variable=self._direction_var, command=self._on_direction_change,
                        **self._indicator_kw())
        tk.Radiobutton(dir_frame, text="我说", value="mine",
                       **radio_kw).pack(side=tk.LEFT, padx=(8, 0))
        tk.Radiobutton(dir_frame, text="别人说", value="theirs",
                       **radio_kw).pack(side=tk.LEFT, padx=(10, 0))
        tk.Radiobutton(dir_frame, text="双向同时", value="dual",
                       **radio_kw).pack(side=tk.LEFT, padx=(10, 0))

        self._vsep(ctrl)
        # 语言对写成「A → B」：翻译方向一目了然，比「源:/目标:」两个标签省地方
        self._source_combo = ttk.Combobox(ctrl, values=list(SOURCE_LANGS.keys()),
                                           state="readonly", width=9)
        self._source_combo.pack(side=tk.LEFT)
        self._source_combo.bind("<<ComboboxSelected>>", self._on_lang_change)
        ttk.Label(ctrl, text="→", style="Dim.TLabel").pack(side=tk.LEFT, padx=6)
        self._target_combo = ttk.Combobox(ctrl, values=list(TARGET_LANGS.keys()),
                                           state="readonly", width=9)
        self._target_combo.pack(side=tk.LEFT)
        self._target_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

    def _build_output_row(self) -> None:
        """第二行 = 输出面：译文发到哪。勾选框**单独一行**：和方向/语言挤在同一行时

        整行需要 1062px，而窗口默认只有 920px —— 超出部分会被 Tk 直接裁掉，
        表现就是「某个选项莫名消失」（用户实测看不到「手腕屏」勾选框）。
        """
        out_frame = ttk.Frame(self._root, padding=(14, 0, 14, 10))
        out_frame.pack(fill=tk.X)

        # 右侧：API key 状态入口（先打包占住右侧，理由同「⚙ 设置」）。
        # 未配置时是一直可见的提醒，点它直接进设置弹窗。
        self._key_chip = ttk.Button(out_frame, text="", style="Chip.TButton",
                                    command=self._open_settings)
        self._key_chip.pack(side=tk.RIGHT)

        ttk.Label(out_frame, text="输出:", style="Dim.TLabel").pack(side=tk.LEFT)
        self._chatbox_var = tk.BooleanVar(value=bool((self._cfg.ui or {}).get("chatbox", True)))
        self._overlay_var = tk.BooleanVar(value=bool((self._cfg.ui or {}).get("overlay", False)))
        # 译音输出：把「我说的话」的译音回灌进虚拟声卡，VRChat 里的对方就能听见外语 TTS。
        # 这是**总开关**；config 里 directions.<X>.output_audio 是方向级开关，两者是「与」关系。
        _audio_cfg = (self._cfg.output.get("audio") or {}) if isinstance(self._cfg.output, dict) else {}
        self._vmic_var = tk.BooleanVar(value=bool(_audio_cfg.get("enabled", False)))
        tk.Checkbutton(out_frame, text="chatbox", variable=self._chatbox_var,
                       command=self._save_ui_state,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(6, 0))
        tk.Checkbutton(out_frame, text="手腕屏", variable=self._overlay_var,
                       command=self._save_ui_state,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(10, 0))
        # 微调按钮紧跟「手腕屏」勾选：它是手腕屏的从属工具，放远了看不出归属
        self._tune_btn = ttk.Button(out_frame, text="微调 ▸", width=7,
                                    command=self._toggle_tune_panel)
        self._tune_btn.pack(side=tk.LEFT, padx=(4, 0))
        tk.Checkbutton(out_frame, text="译音输出", variable=self._vmic_var,
                       command=self._save_audio_flag,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(10, 0))

        # 微调面板：外层常驻（保证位置固定），只切换内层 body 的显隐——
        # 若整块 pack_forget 再 pack，会被排到窗口最底部去。
        self._tune_frame = ttk.Frame(self._root, padding=(14, 0))
        self._tune_frame.pack(fill=tk.X)
        self._tune_body = ttk.Frame(self._tune_frame)
        self._build_tune_body()

    @staticmethod
    def _indicator_kw() -> dict:
        """经典 tk 单选/复选框的统一配色：选中时指示器变蓝，其余深灰。"""
        return dict(bg=PANEL, fg=TEXT, activebackground=PANEL,
                    activeforeground="#ffffff", selectcolor=SURFACE,
                    highlightthickness=0, bd=0, font=FONT_UI)

    # ---------------------------------------------------------------- 手腕屏微调
    def _toggle_tune_panel(self) -> None:
        """展开/收起微调面板（只切内层 body，外层常驻以固定位置）。

        展开时同步把窗口加高：否则聊天区被面板挤扁（实测 434px → 329px），
        而调参时正需要一边看译文一边拖。
        """
        win_h = self._root.winfo_height()
        win_w = self._root.winfo_width()
        if self._tune_body.winfo_ismapped():
            self._tune_body.pack_forget()
            # ⚠️ 只 pack_forget 不够：Tk 不会重算空 frame 的高度（实测 reqheight 仍停在 106），
            # 那 106px 会一直占着把聊天区压扁。必须显式关掉传播并把高度压到 0。
            self._tune_frame.pack_propagate(False)
            self._tune_frame.configure(height=1)
            self._tune_btn.configure(text="微调 ▸")
            new_h = max(self._root.minsize()[1], win_h - getattr(self, "_tune_added_h", 0))
        else:
            self._tune_frame.pack_propagate(True)
            self._tune_body.pack(fill=tk.X)
            self._tune_btn.configure(text="微调 ▾")
            self._root.update_idletasks()
            self._tune_added_h = self._tune_body.winfo_reqheight() + 8
            new_h = win_h + self._tune_added_h
        self._root.geometry(f"{win_w}x{new_h}")

    def _build_tune_body(self) -> None:
        """手腕屏微调：滑块改动 → 写回 config.yaml → overlay 配置热重载（无需重启）。

        面板做成**可收起**的（默认收起）：它是调试期工具，平时不该占屏幕，
        但参数全都要能拖——位置/旋转/大小/弯曲/透明度 + 锚点。
        """
        ov = self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}
        off = ov.get("offset") or {}
        pos = list(off.get("pos") or [0.0, 0.06, 0.02])
        rot = list(off.get("rot") or [0, 0, 0])
        _sz = list(ov.get("size_px") or [1024, 320])
        self._tune_panel_w = int(_sz[0])
        self._tune_values: dict[str, float] = {
            "pos_x": float(pos[0]), "pos_y": float(pos[1]), "pos_z": float(pos[2]),
            "rot_x": float(rot[0]), "rot_y": float(rot[1]), "rot_z": float(rot[2]),
            "width_m": float(off.get("width_m", 0.24)),
            "curvature": float(off.get("curvature", 0.0)),
            "alpha": float(off.get("alpha", 0.9)),
            # 字号 / 面板高度决定「一块屏能显示多少字」——用户明确要能自己调
            "font_size": float(ov.get("font_size", 42)),
            "source_font_size": float(ov.get("source_font_size", 30)),
            "panel_h": float(_sz[1]),
        }
        self._ov_save_job: str | None = None
        self._anchor_label_to_key = {"右手": "right_hand", "左手": "left_hand",
                                     "前臂 tracker": "tracker", "头显前固定": "hmd"}
        _key_to_label = {v: k for k, v in self._anchor_label_to_key.items()}

        row = ttk.Frame(self._tune_body)
        row.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(row, text="锚点:", font=FONT_UI).pack(side=tk.LEFT)
        self._anchor_combo = ttk.Combobox(row, values=list(self._anchor_label_to_key),
                                          state="readonly", width=12, font=FONT_UI)
        self._anchor_combo.set(_key_to_label.get(str(ov.get("anchor", "right_hand")), "右手"))
        self._anchor_combo.pack(side=tk.LEFT, padx=(4, 14))
        self._anchor_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_anchor_change())
        ttk.Label(row, text="tracker 序号:", font=FONT_UI).pack(side=tk.LEFT)
        self._tracker_var = tk.StringVar(value=str(ov.get("tracker_index", 0)))
        ttk.Spinbox(row, from_=0, to=3, width=3, font=FONT_UI, textvariable=self._tracker_var,
                    command=self._save_overlay_cfg).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(row, text="（仅锚点=前臂 tracker 时有效）", font=FONT_STATUS,
                  foreground=TEXT_MUTED).pack(side=tk.LEFT, padx=(10, 0))

        grid = ttk.Frame(self._tune_body)
        grid.pack(fill=tk.X, pady=(2, 2))
        specs = [
            ("pos_x", "位置X", -0.30, 0.30, 0.005, "m"),
            ("pos_y", "位置Y", -0.30, 0.30, 0.005, "m"),
            ("pos_z", "位置Z", -0.30, 0.30, 0.005, "m"),
            ("rot_x", "俯仰X", -90.0, 90.0, 1.0, "°"),
            ("rot_y", "偏航Y", -90.0, 90.0, 1.0, "°"),
            ("rot_z", "翻滚Z", -90.0, 90.0, 1.0, "°"),
            ("width_m", "大小", 0.05, 0.80, 0.01, "m"),
            ("curvature", "弯曲", 0.0, 0.50, 0.01, ""),
            ("alpha", "透明度", 0.10, 1.00, 0.05, ""),
            # 显示多少字由这三个决定：字号调小 → 同样高度塞更多字；面板调高 → 多一轮对话
            ("font_size", "译文字号", 20, 64, 1, ""),
            ("source_font_size", "原文字号", 14, 48, 1, ""),
            ("panel_h", "面板高", 240, 560, 10, "px"),
        ]
        for i, (key, label, lo, hi, res, unit) in enumerate(specs):
            row_i, col_i = divmod(i, 3)
            cell = ttk.Frame(grid)
            cell.grid(row=row_i, column=col_i, sticky="w", padx=(0, 18), pady=1)
            ttk.Label(cell, text=label, font=FONT_UI, width=6).pack(side=tk.LEFT)
            var = tk.DoubleVar(value=self._tune_values[key])
            val_lbl = ttk.Label(cell, text=f"{self._tune_values[key]:g}{unit}",
                                font=FONT_STATUS, foreground=TEXT_DIM, width=7)
            tk.Scale(cell, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                     variable=var, showvalue=False, length=104, width=10,
                     bg=PANEL, fg=TEXT, troughcolor=SURFACE, activebackground=ACCENT,
                     highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
                     command=self._make_tune_handler(key, var, val_lbl, unit)).pack(side=tk.LEFT, padx=(4, 6))
            val_lbl.pack(side=tk.LEFT)

    def _make_tune_handler(self, key: str, var, lbl, unit: str):  # noqa: ANN001
        def _on_move(_v: str) -> None:
            self._tune_values[key] = round(float(var.get()), 4)
            lbl.configure(text=f"{self._tune_values[key]:g}{unit}")
            self._schedule_overlay_save()
        return _on_move

    def _on_anchor_change(self) -> None:
        self._save_overlay_cfg()

    def _schedule_overlay_save(self) -> None:
        """拖动时不要每像素写盘：延后 200ms，停手才落盘。"""
        if self._ov_save_job is not None:
            try:
                self._root.after_cancel(self._ov_save_job)
            except Exception:
                pass
        self._ov_save_job = self._root.after(200, self._save_overlay_cfg)

    def _save_overlay_cfg(self) -> None:
        """把微调面板的值写回 config.yaml；overlay 侧有热重载，改完立刻生效。

        用就地改文本的方式（`_yaml_set_in_text`），**不整文件重写**，
        否则拖动一次滑块就会把配置里的注释和键顺序全抹掉。
        """
        self._ov_save_job = None
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            v = self._tune_values
            try:
                tracker = int(self._tracker_var.get())
            except (TypeError, ValueError):
                tracker = 0
            updates: list[tuple[list[str], str]] = [
                (["overlay", "anchor"],
                 self._anchor_label_to_key.get(self._anchor_combo.get(), "right_hand")),
                (["overlay", "tracker_index"], str(tracker)),
                (["overlay", "offset", "pos"],
                 f"[{_fmt_scalar(v['pos_x'])}, {_fmt_scalar(v['pos_y'])}, {_fmt_scalar(v['pos_z'])}]"),
                (["overlay", "offset", "rot"],
                 f"[{_fmt_scalar(v['rot_x'])}, {_fmt_scalar(v['rot_y'])}, {_fmt_scalar(v['rot_z'])}]"),
                (["overlay", "offset", "width_m"], _fmt_scalar(v["width_m"])),
                (["overlay", "offset", "curvature"], _fmt_scalar(v["curvature"])),
                (["overlay", "offset", "alpha"], _fmt_scalar(v["alpha"])),
                # 字号 / 面板像素高度（改这些会触发 overlay 重新渲染一帧）
                (["overlay", "font_size"], _fmt_scalar(v["font_size"])),
                (["overlay", "source_font_size"], _fmt_scalar(v["source_font_size"])),
                (["overlay", "size_px"], f"[{self._tune_panel_w}, {_fmt_scalar(v['panel_h'])}]"),
            ]
            for key_path, val in updates:
                text = _yaml_set_in_text(text, key_path, val)
            _write_config_text(p, text)
            print(f"[gui] 手腕屏参数已写入 config.yaml：anchor={updates[0][1]} "
                  f"pos={updates[2][1]} rot={updates[3][1]} width={updates[4][1]}m "
                  f"curvature={updates[5][1]} alpha={updates[6][1]} "
                  f"字号={updates[7][1]}/{updates[8][1]} 面板={updates[9][1]}"
                  f"（overlay 会热重载，无需重启）",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存手腕屏参数失败：{exc}", flush=True)

    # ---------------------------------------------------------------- 设置弹窗（低频设置）
    def _build_settings_dialog(self) -> None:
        """低频设置收进弹窗：API key + 音频设备。

        为什么不在主界面：这两组是「装好一次、几乎不动」的设置，常驻只会让
        主界面变成 4 行控件堆叠（改造前的样子）。弹窗**先建好再 withdraw**——
        控件属性（_key_entry / _mic_combo 等）必须在弹窗不可见时也随即可用，
        设备扫描和自动化测试都直接访问它们。
        """
        win = tk.Toplevel(self._root)
        win.title("设置")
        win.configure(bg=PANEL)
        win.transient(self._root)
        win.resizable(False, False)
        win.withdraw()
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        win.bind("<Escape>", lambda _e: self._close_settings())
        self._settings_win = win
        self._apply_dark_titlebar(win)

        body = ttk.Frame(win, padding=(18, 16, 18, 14))
        body.pack(fill=tk.BOTH, expand=True)

        # ---- API Key ----
        # 安全约束（与 vlt/credentials.py 一致）：
        # - 输入框用 ● 掩码；保存成功后**立刻清空输入框**，明文不留在界面上；
        # - 状态只出现打码形式（sk-xx-****yyyy）；明文不进日志、不进 config.yaml。
        ttk.Label(body, text="API KEY", style="Section.TLabel").pack(anchor=tk.W)
        key_row = ttk.Frame(body)
        key_row.pack(fill=tk.X, pady=(8, 4))
        # 先占右侧，空间不足时才不会把按钮挤没
        self._key_clear_btn = ttk.Button(key_row, text="清除", width=5,
                                         command=self._on_clear_key)
        self._key_clear_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self._key_save_btn = ttk.Button(key_row, text="保存", width=6,
                                        command=self._on_save_key)
        self._key_save_btn.pack(side=tk.RIGHT)
        self._key_var = tk.StringVar()
        self._key_entry = ttk.Entry(key_row, textvariable=self._key_var, show="●",
                                    width=34, style="Key.TEntry")
        self._key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        self._key_entry.bind("<Return>", lambda _e: self._on_save_key())
        self._key_status = ttk.Label(body, text="", style="Dim.TLabel")
        self._key_status.pack(anchor=tk.W)
        self._refresh_key_status()

        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)

        # ---- 音频设备 ----
        dev_head = ttk.Frame(body)
        dev_head.pack(fill=tk.X)
        ttk.Label(dev_head, text="音频设备", style="Section.TLabel").pack(side=tk.LEFT)
        self._refresh_btn = ttk.Button(dev_head, text="刷新", width=5,
                                       command=self._on_refresh_devices)
        self._refresh_btn.pack(side=tk.RIGHT)

        grid = ttk.Frame(body)
        grid.pack(fill=tk.X, pady=(8, 2))
        grid.columnconfigure(1, weight=1)
        auto = "自动检测"
        # ⚠️ 控件名不能改：设备扫描结果直接往这三个下拉里写值
        self._mic_combo = ttk.Combobox(grid, values=[auto], state="readonly")
        self._loopback_combo = ttk.Combobox(grid, values=[auto], state="readonly")
        self._audio_out_combo = ttk.Combobox(grid, values=[auto], state="readonly")
        for i, (label, combo) in enumerate((
                ("麦克风:", self._mic_combo),
                ("VRChat 音频:", self._loopback_combo),
                ("译音输出:", self._audio_out_combo))):
            ttk.Label(grid, text=label, style="Dim.TLabel").grid(
                row=i, column=0, sticky="w", pady=3)
            combo.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=3)
            combo.set(auto)
            combo.bind("<<ComboboxSelected>>", self._on_device_change)

        # 从配置恢复选择
        capture_cfg = (self._cfg.output or {}).get("capture") or {}
        audio_cfg = (self._cfg.output or {}).get("audio") or {}
        self._mic_combo.set(capture_cfg.get("mic_device") or auto)
        self._loopback_combo.set(capture_cfg.get("loopback_device") or auto)
        self._audio_out_combo.set(audio_cfg.get("device_name") or auto)

        ttk.Label(body, text="设备选择自动保存到 config.yaml",
                  style="Muted.TLabel").pack(anchor=tk.W, pady=(6, 0))

    def _open_settings(self) -> None:
        """打开设置弹窗（已建好，只是显示出来），定位到主窗口附近。"""
        win = self._settings_win
        self._refresh_key_status()          # 每次打开都刷新来源/打码显示
        win.update_idletasks()
        rx, ry = self._root.winfo_x(), self._root.winfo_y()
        rw = self._root.winfo_width()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{rx + max((rw - ww) // 2, 20)}+{ry + 48}")
        win.deiconify()
        win.lift()
        win.focus_set()
        # 建窗时它处于 withdraw 状态，那时调 DWM 拿不到有效 hwnd、会静默失败
        # （实测弹窗标题栏仍是浅色、跟主窗口不一致）。显示出来之后再设一次。
        self._apply_dark_titlebar(win)

    def _close_settings(self) -> None:
        self._settings_win.withdraw()

    def _refresh_key_status(self) -> None:
        """只显示来源 + 打码值，绝不显示明文。

        写两处：设置弹窗里的完整状态（_key_status）+ 主界面第二行右侧的
        入口按钮（_key_chip）——key 没配时用户不看弹窗也能一眼看到提醒。
        """
        try:
            from .credentials import key_source

            src, masked = key_source()
        except Exception as exc:  # noqa: BLE001
            self._key_status.config(text=f"⚠️ 读取 key 状态失败：{exc}")
            return
        if masked:
            self._key_status.config(text=f"当前：{src} {masked}")
            chip = "API key ✓"
        else:
            self._key_status.config(text="⚠️ 未配置 API key —— 在上面粘贴后点「保存」")
            chip = "⚠ 未配置 API key"
        if hasattr(self, "_key_chip"):
            self._key_chip.configure(text=chip)

    def _on_save_key(self) -> None:
        from .credentials import load_saved_key, mask_key, save_api_key

        raw = self._key_var.get()
        try:
            path = save_api_key(raw)
        except ValueError as exc:
            self._key_var.set("")                      # 明文不留在界面上
            self._key_status.config(text=f"❌ 没保存：{exc}")
            self._set_status("error", f"API key 保存失败：{exc}")
            print(f"[gui] ❌ API key 保存失败：{exc}", flush=True)
            return
        self._key_var.set("")
        shown = mask_key(load_saved_key() or "")
        self._refresh_key_status()
        self._set_status("ok", f"API key 已保存（{shown}）")
        print(f"[gui] ✅ API key 已保存（{shown}）→ {path}", flush=True)
        self._check_api_key()

    def _on_clear_key(self) -> None:
        from .credentials import clear_saved_key

        removed = clear_saved_key()
        self._refresh_key_status()
        msg = "已清除保存的 API key" if removed else "本来就没有保存过 API key"
        self._set_status("info", msg)
        print(f"[gui] {msg}", flush=True)
        self._check_api_key()

    def _build_chat(self) -> None:
        chat_frame = ttk.Frame(self._root)
        # 与头部两行同一个 14px 左边距：左右边界对齐才有「一栏到底」的秩序感
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

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
        self._refresh_key_status()
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
        self._save_ui_state()      # 方向要记下来，下次启动恢复（用户明确要求）

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
        """把互为镜像的两组方向写回 config.yaml（mine: A→B / theirs: B→A）。

        就地改文本，不整文件重写（原因见 `_yaml_set_in_text`）。
        """
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        pairs = {
            "mine": {"source_lang": a, "target_lang": b},
            "theirs": {"source_lang": b, "target_lang": a or "zh"},
        }
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            for name, langs in pairs.items():
                for key, val in langs.items():
                    text = _yaml_set_in_text(text, ["directions", name, key], _fmt_scalar(val))
            _write_config_text(p, text)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存配置失败：{exc}", flush=True)

    def _save_ui_state(self) -> None:
        """把界面上的选择（方向 / 输出勾选）写回 config.yaml 的 `ui:` 段，下次启动恢复。

        用户明确要求「方向这个东西是需要保存的」——之前每次启动都会重置成「我说的话」。
        段不存在时补建：老 config.yaml（从旧模板生成）里没有 ui 段。
        """
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            if not re.search(r"^ui:", text, re.M):
                text = text.rstrip("\n") + "\n\n# 界面上次的选择（启动时自动恢复，不用手改）\nui:\n"
            updates = [
                (["ui", "direction"], self._direction_var.get()),
                (["ui", "chatbox"], _fmt_scalar(bool(self._chatbox_var.get()))),
                (["ui", "overlay"], _fmt_scalar(bool(self._overlay_var.get()))),
            ]
            for key_path, val in updates:
                text = _yaml_set_in_text(text, key_path, val)
            _write_config_text(p, text)
            print(f"[gui] 界面选择已保存：direction={updates[0][1]} chatbox={updates[1][1]} "
                  f"overlay={updates[2][1]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存界面选择失败：{exc}", flush=True)

    def _save_audio_flag(self) -> None:
        """把译音输出总开关写回 config.yaml（output.audio.enabled），勾了就一直记住。

        就地改一行（`_yaml_set_in_text`），不整文件重写 —— 保住注释与键顺序。
        """
        want = bool(self._vmic_var.get())
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            text = _yaml_set_in_text(text, ["output", "audio", "enabled"], _fmt_scalar(want))
            _write_config_text(p, text)
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
        # 手腕屏由界面持有，内容镜像聊天区（两个方向都进同一块屏）
        self._start_overlay()
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
        # 引擎不碰手腕屏：它由界面持有（一块屏显示两个方向的对话）。
        # 若交给两个引擎各自创建，会撞 `OverlayError_KeyInUse`（用户实测）。
        own_sinks = {s for s in self._sinks if s != "overlay"}
        events = EngineEvents(
            on_text=lambda src, txt, final, who=who: self._q.put(("text", who, src, txt, final)),
            on_status=lambda lvl, msg, who=who: self._on_engine_status(lvl, msg, who),
            on_stats=lambda s: self._q.put(("stats", s)),
        )
        eng = Engine(
            cfg=self._cfg,
            direction=direction,
            source=source,
            sinks=own_sinks,
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

    # ---------------------------------------------------------------- 手腕屏（界面持有）
    def _start_overlay(self) -> None:
        """按勾选状态接管 SteamVR 的手腕屏。失败只禁用这一项，绝不影响翻译。

        手腕屏由**界面**持有而不是某个引擎：手腕上只该有一块屏，内容是最近几句对话
        （镜像聊天区）。交给两个引擎各自创建会撞 `OverlayError_KeyInUse`（用户实测）。
        """
        if "overlay" not in self._sinks:
            return
        try:
            self._overlay_out = WristOverlay(
                OverlayConfig.from_dict(self._cfg.overlay), config_path=DEFAULT_CONFIG)
            if not self._overlay_out.start():
                self._overlay_out = None      # start() 内部已打印原因
                return
            self._push_overlay(force=True)
        except Exception as exc:  # noqa: BLE001
            self._overlay_out = None
            print(f"[gui] ⚠️ 手腕屏初始化异常，已禁用（翻译不受影响）："
                  f"{type(exc).__name__}: {exc}", flush=True)

    def _stop_overlay(self) -> None:
        if self._overlay_out is None:
            return
        try:
            self._overlay_out.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 关闭手腕屏时出错（忽略）：{exc}", flush=True)
        self._overlay_out = None

    def _push_overlay(self, force: bool = False) -> None:
        """把聊天区最近几条推给手腕屏（同一块屏显示两个方向的对话）。"""
        if self._overlay_out is None:
            return
        try:
            entries = [(b.who, b.source, b.text) for b in self._bubbles[-8:]]
            self._overlay_out.update_entries(entries, force=force)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 手腕屏刷新失败：{type(exc).__name__}: {exc}", flush=True)

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
        self._stop_overlay()
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
                    self._push_overlay()   # 手腕屏镜像聊天区（同一块屏，两个方向都上）
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
        if self._overlay_out is not None:
            self._overlay_out.tick()      # 手腕屏的热重载 / 淡出
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
            # ⚠️ Canvas.move(tagOrId, x, y) 只接受**一个** tagOrId。
            # 写成 `move(*other.items, 0, delta)` 会在 items 有 ≥2 个图元时抛
            # `TclError: wrong # args`（双行气泡必然有 ≥2 个图元）→ 下面的气泡不移位 → 重叠。
            # 用户实测日志里就抓到了这个 TclError。
            for other in self._bubbles[idx + 1:]:
                other.y += delta
                for iid in other.items:
                    self._canvas.move(iid, 0, delta)
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
