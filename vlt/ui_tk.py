"""需要 tkinter 的界面底层工具：字体族解析、字符宽度换算、控件读取、圆角矩形。

为什么单列一个模块（且是全仓库**唯一**给界面工具 import tkinter 的新模块）：
  · 字体族解析 / `width=` 字符宽度换算都要真起 Tk 才能 measure；
  · `combo_values` / `round_rect` 直接操作控件/画布。
  它们都不属于「纯数据」（那些在 vlt/ui_theme.py）或「纯字符串」（vlt/ui_text.py），
  所以按依赖边界单独成模块 —— 纯模块绝不 import tkinter，本模块才 import。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from .ui_theme import (
    ACCENT,
    ACCENT_ACTIVE,
    ACCENT_HOVER,
    BG,
    BORDER,
    COLOR_ERROR,
    COLOR_WARN,
    DANGER,
    DANGER_ACTIVE,
    DANGER_HOVER,
    PANEL,
    SURFACE,
    SURFACE_HOVER,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
)
from .ui_theme import _FONT_CANDIDATES

# 下面这组是**占位**默认值，`apply_ui_font()` 会在建 Tk root 之后按平台重绑。
FONT = ("Microsoft YaHei UI", 11)          # 译文（主）
FONT_SMALL = ("Microsoft YaHei UI", 9)     # 原文（辅，小一号）
FONT_META = ("Microsoft YaHei UI", 8)
FONT_UI = ("Microsoft YaHei UI", 9)        # 控件文字
FONT_STATUS = ("Microsoft YaHei UI", 8)    # 状态栏
FONT_BOLD_SM = ("Microsoft YaHei UI", 8, "bold")    # 分区小标题
FONT_BOLD_MD = ("Microsoft YaHei UI", 12, "bold")   # 弹窗小标题
FONT_BOLD_LG = ("Microsoft YaHei UI", 13, "bold")   # 弹窗大标题（赞助）
# 房间玩家那一行：VR 里用激光笔点名字，8/9 号太小点不中，单独给一档 11。
FONT_ROW = ("Microsoft YaHei UI", 11)
FONT_ROW_BOLD = ("Microsoft YaHei UI", 11, "bold")

# 解析出的字体族缓存（resolve_ui_family 首次调用后钉住；None = 还没解析过）。
_ui_family: str | None = None


def resolve_ui_family(root) -> str:
    """挑一个这台机器上**真实存在**的界面字体族。

    先按候选表找；都没有就退回 Tk 自己的默认字体族（`TkDefaultFont` 的 actual family），
    保证至少是一个有字形的真字体，而不是 `fixed`。
    """
    global _ui_family
    if _ui_family:
        return _ui_family
    try:
        available = {str(f).strip().lower() for f in tkfont.families(root)}
    except Exception:  # noqa: BLE001 — 拿不到列表就退回默认
        available = set()
    for cand in _FONT_CANDIDATES:
        if cand.lower() in available:
            _ui_family = cand
            return cand
    try:
        _ui_family = str(tkfont.nametofont("TkDefaultFont").actual("family"))
    except Exception:  # noqa: BLE001
        _ui_family = "sans-serif"
    return _ui_family


def apply_ui_font(root) -> None:
    """按当前平台重绑界面字体常量（在建 Tk root 之后、建任何控件之前调用）。"""
    global FONT, FONT_SMALL, FONT_META, FONT_UI, FONT_STATUS
    global FONT_BOLD_SM, FONT_BOLD_MD, FONT_BOLD_LG
    global FONT_ROW, FONT_ROW_BOLD
    fam = resolve_ui_family(root)
    FONT = (fam, 11)
    FONT_SMALL = (fam, 9)
    FONT_META = (fam, 8)
    FONT_UI = (fam, 9)
    FONT_STATUS = (fam, 8)
    FONT_BOLD_SM = (fam, 8, "bold")
    FONT_BOLD_MD = (fam, 12, "bold")
    FONT_BOLD_LG = (fam, 13, "bold")
    FONT_ROW = (fam, 11)
    FONT_ROW_BOLD = (fam, 11, "bold")


_char_width_cache: dict[tuple, int] = {}


def _font_spec_key(font_spec) -> object:
    """把字体规格压成可哈希的缓存 key（跨平台容错）。

    `font_spec` 可能是 tuple、字符串（字体名）、`tkfont.Font`，或 —— 在 Linux 上 ——
    `widget.cget("font")` 返回的 `_tkinter.Tcl_Obj`（不可迭代，直接 `tuple()`
    会抛 `TypeError: '_tkinter.Tcl_Obj' object is not iterable`）。
    """
    if isinstance(font_spec, str):
        return font_spec
    try:
        return tuple(font_spec)
    except TypeError:
        return str(font_spec)


def _int_fmt(v: float) -> str:
    """桌面字幕滑块值标签的统一格式（字号 / 像素尺寸都是整数）。"""
    return f"{int(round(v))}"


def _char_width_for(text: str, font_spec, minimum: int = 0) -> int:
    """把「这段文字需要多宽」换算成 Tk 的**字符宽度单位**（给 width= 用）。

    ⚠️ 坑（实测）：Tk 的 `width=N` 单位是**字体平均字符宽**（本机 ≈7px），而一个汉字/假名
    约等于 2 倍宽 ⇒ `width=len(text)` 对中日韩文字**必然裁字**：
    「訳文の文字サイズ」自然宽 100px，按 8 个字符只申请到 67px，屏幕上只剩「訳文の文字」。
    中文同样中招（「译文字号」需 52px、按 6 字符只给 46px），只是裁得少不容易看出来。
    这里用字体的真实 measure 换算，并留 1 个字符余量。

    结果**带缓存**：切界面语言会把整套控件重建一遍，同一个词条会被反复测量；
    而一次 `measure()` 在字体需要 fontconfig 回落的机器上要几百毫秒
    （见上方 _FONT_CANDIDATES 的说明），不缓存会明显卡顿。
    """
    key = (text, _font_spec_key(font_spec), minimum)
    hit = _char_width_cache.get(key)
    if hit is not None:
        return hit
    try:
        f = tkfont.Font(font=font_spec)
        avg = max(1, f.measure("0"))
        need = -(-f.measure(text) // avg) + 1     # 向上取整 + 1 字符余量
    except Exception:  # noqa: BLE001 — 量不出来就退回字符数（至少不比改动前差）
        need = len(text) + 1
    out = max(minimum, need)
    _char_width_cache[key] = out
    return out


def _combo_width(names, minimum: int = 9, font_spec=None) -> int:
    """下拉框宽度（字符单位）：按当前语言里最长的名字算，避免被截断。

    同样受「平均字符宽 ≠ 汉字宽」影响，所以走 `_char_width_for` 换算，不直接数字符。
    """
    f = font_spec or FONT_UI
    return max(minimum, max((_char_width_for(str(n), f) for n in names), default=0))


def slider_kwargs(*, width: int = 10) -> dict:
    """深色主题的 `tk.Scale` 统一外观（全仓滑条只该有这一套）。

    为什么不用 `ttk.Scale`：它跟 ttk 主题（`TScale`）走，槽/滑块/按下态是另一套配色，
    放在本仓库的深色界面里会明显不一致（耳返音量曾经就是唯一一个 `ttk.Scale`）。
    几何相关的 `from_` / `to` / `length` / `variable` / `command` 由调用点各自给，
    这里只固化「看起来是什么样」。
    """
    return dict(showvalue=False, width=width, bg=PANEL, fg=TEXT,
                troughcolor=SURFACE, activebackground=ACCENT,
                highlightthickness=0, bd=0, sliderrelief=tk.FLAT)


def combo_values(combo) -> list[str]:
    """读回 ttk.Combobox 的候选值（跨平台安全）。

    ⚠️ 坑（实测）：`combo.cget("values")` 的**返回类型依平台而变** ——
    Windows 上 Tk 返回 tuple（可直接 `list()`），Linux 上返回
    `_tkinter.Tcl_Obj`（不可迭代，`list()` 直接抛
    `TypeError: '_tkinter.Tcl_Obj' object is not iterable`）。
    这在设备下拉里是**真会走到**的路径（`_on_device_change` 要按下标取回原始设备名），
    不是只影响测试。

    用 Tk 自己的 `splitlist` 归一化：tuple / 列表 / 空格分隔的字符串 / Tcl_Obj 都能吃。
    """
    try:
        return [str(v) for v in combo.tk.splitlist(combo.cget("values"))]
    except Exception:  # noqa: BLE001 — 读不到就当空，别让「保存设备选择」这一步炸掉
        return []


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """圆角矩形：polygon + smooth=True 才有圆角。"""
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
           x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def apply_theme(root) -> None:
        """统一深色主题：深灰 + 蓝。

        ⚠️ Windows 上 ttk 默认主题（vista/xpnative）由系统绘制，
        style.configure(background=...) 会被**静默忽略**——必须切到 clam。
        """
        style = ttk.Style(root)
        style.theme_use("clam")

        # ⚠️ 这里**故意不设 foreground**（前景色一律逐控件显式配，见下）。
        # 原因：Tk 自带的文件对话框会从**根样式**读前景 —— Tcl 侧 `ttk::style lookup . -foreground`
        # （updir 箭头图标），而它的文件列表（`::tk::IconList`）把每条文字的 item `-fill`
        # 也取成这个值，可那块 canvas 的底色是**硬编码的 #ffffff**、没有任何选项能改。
        # 实测：全局设了近白前景之后，文件列表的 `-fill` 恰好等于 TEXT(#e8eaee) → 白底白字，
        # 且**改不掉**（建窗时读取，导航一次就重画一次）。所以宁可让未配色的 ttk 控件回落
        # clam 的默认深色，也不要为了自己方便把全局前景染白。
        # 代价已核查：本模块下面用到的每个类都显式配了前景（TLabel/各具名 Label/TButton/
        # 具名 Button/TCombobox/TEntry/TSpinbox/TMenubutton/TCheckbutton/TNotebook.Tab），
        # 因此我们自己的界面不受影响（有截图逐像素比对 + tests/test_ui_theme_contrast.py 钉住）。
        style.configure(".", font=FONT_UI, background=PANEL,
                        bordercolor=BORDER, focuscolor=PANEL)
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=TEXT)
        style.configure("Dim.TLabel", foreground=TEXT_DIM)
        style.configure("Muted.TLabel", foreground=TEXT_DIM, font=FONT_STATUS)
        style.configure("Status.TLabel", font=FONT_STATUS)
        # 分区小标题（设置弹窗里的「API KEY / 音频设备」）：小一号、暗色、加粗
        style.configure("Section.TLabel", foreground=TEXT_DIM,
                        font=FONT_BOLD_SM)
        # API key 状态槽位里的两个控件：**已配置 → 纯展示标签**（「⚙ 设置」是改 key 的入口，
        # 标签不可点）；**未配置 → 可点按钮**，点击用默认浏览器打开千问云开通页（QIANWEN_SIGNUP_URL）。
        style.configure("Chip.TLabel", font=FONT_STATUS, foreground=TEXT_DIM)
        style.configure("ChipWarn.TLabel", font=FONT_STATUS, foreground=COLOR_WARN)
        # 设置弹窗里「保存失败」这类就地提示：警示色，但只是文字（不抢按钮的视觉重量）
        style.configure("Warn.TLabel", font=FONT_STATUS, foreground=COLOR_WARN)
        # 「保存被拒 · 什么都没写」的就地红字：警示橙只表达"注意一下"，而这种情况下
        # 配置**一点没变**（用户以为切了线路、其实还在老线路上）—— 必须比橙更重一档。
        style.configure("Error.TLabel", font=FONT_STATUS, foreground=COLOR_ERROR)
        # 未配置按钮：暗橙底 + 警示橙字，悬停/按下亮一档 —— 警示色系但不刺眼。
        style.configure("ChipWarn.TButton", font=FONT_STATUS, foreground=COLOR_WARN,
                        background="#33291c", borderwidth=0, focusthickness=0,
                        focuscolor="#33291c", padding=(8, 2))
        style.map("ChipWarn.TButton",
                  background=[("pressed", "#453723"), ("active", "#453723")],
                  foreground=[("active", "#e8a85c")])
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
        # 主操作按钮（主窗「开始翻译」）：在 Accent 之上再重一档 —— 加粗 + 更大内边距。
        # 同一行里「开始翻译」是当前唯一该被点的按钮、「停止翻译」是普通灰按钮，
        # 两者尺寸一致时主次分不出来（用户反馈层级不够）。只在主窗用，弹窗继续用
        # Accent.TButton，免得每个确认框的主按钮都变粗体大字。
        style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff",
                        font=FONT_BOLD_MD, borderwidth=0, focusthickness=0,
                        focuscolor=ACCENT, padding=(20, 9))
        style.map("Primary.TButton",
                  background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_HOVER),
                              ("disabled", "#22374f")],
                  foreground=[("disabled", "#6b87ab")])
        # 反向动作按钮（房间的「断开连接」）：与「连接房间」同形状、**不同颜色** ——
        # 同一个位置在不同连接态下写着相反的动作，只靠文字区分容易点错，
        # 颜色是比文字快得多的提示（用户明确要求「断开用个别的颜色」）。
        style.configure("Danger.TButton", background=DANGER, foreground="#ffffff",
                        borderwidth=0, focusthickness=0, focuscolor=DANGER,
                        padding=(14, 6))
        style.map("Danger.TButton",
                  background=[("pressed", DANGER_ACTIVE), ("active", DANGER_HOVER),
                              ("disabled", "#3a2726")],
                  foreground=[("disabled", "#9c7a78")])

        # 单按钮开关（主窗第一行「开始翻译 ⇄ 停止翻译」）：**同一个控件**在两种状态间换外观。
        # ⚠️ 两个样式必须逐字同尺寸 —— 字体与内边距都取下面这两份常量。
        # 曾经的写法是蓝底用 Primary.TButton（粗体 + padding (20,9)）、红底用 Danger.TButton
        # （常规字体 + padding (14,6)），于是点一下按钮就**变大 / 变小**（用户实测反馈
        # 「按一下变大、按一下变小」）。改样式名的人请注意：这两个值的唯一真源在这里。
        _POWER_FONT = FONT_BOLD_MD
        _POWER_PAD = (20, 9)
        style.configure("Power.TButton", background=ACCENT, foreground="#ffffff",
                        font=_POWER_FONT, borderwidth=0, focusthickness=0,
                        focuscolor=ACCENT, padding=_POWER_PAD)
        style.map("Power.TButton",
                  background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_HOVER),
                              ("disabled", "#22374f")],
                  foreground=[("disabled", "#6b87ab")])
        style.configure("PowerDanger.TButton", background=DANGER, foreground="#ffffff",
                        font=_POWER_FONT, borderwidth=0, focusthickness=0,
                        focuscolor=DANGER, padding=_POWER_PAD)
        style.map("PowerDanger.TButton",
                  background=[("pressed", DANGER_ACTIVE), ("active", DANGER_HOVER),
                              ("disabled", "#3a2726")],
                  foreground=[("disabled", "#9c7a78")])

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

        # ---- 通用输入区（Entry / Spinbox）----
        # ⚠️ 上面 style.configure(".", foreground=TEXT) 是**全局**的：凡是没有显式配
        #    `fieldbackground` 的 ttk 输入类，输入区会回落 clam 的**白色**默认底，
        #    配上这份近白前景就是「白底白字」，内容完全看不见。
        #    实测（ttk::style lookup）：修这条之前 TEntry/TSpinbox 的 fieldbackground 都是
        #    空串，而 Key.TEntry/TCombobox 因为显式配过所以正常 —— 这正是「API key、OSC
        #    端口没事，麦克风代理的两个缓冲框看不见」的原因（那两个是 ttk.Spinbox，
        #    全仓库唯一没写 style= 的输入控件）。
        #    所以：TEntry / TSpinbox 必须**作为基类**配深色，而不是只补一个具名样式 ——
        #    别名样式（Key.TEntry）会覆盖这里的每一项，外观逐像素不变。
        style.configure("TEntry", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE,
                        selectbackground=ACCENT, selectforeground="#ffffff",
                        padding=(8, 4))
        style.map("TEntry",
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  fieldbackground=[("disabled", BG), ("readonly", SURFACE)],
                  foreground=[("disabled", TEXT_DIM), ("readonly", TEXT)])
        # Spinbox 另有上下两个箭头元素（Spinbox.uparrow/downarrow），它们认 -background
        # 与 -arrowcolor —— 只配 fieldbackground 的话箭头仍是 clam 的浅色小块。
        style.configure("TSpinbox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, insertcolor=TEXT, arrowcolor=TEXT_DIM,
                        bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                        selectbackground=ACCENT, selectforeground="#ffffff",
                        padding=(8, 4))
        style.map("TSpinbox",
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  fieldbackground=[("disabled", BG), ("readonly", SURFACE)],
                  foreground=[("disabled", TEXT_DIM), ("readonly", TEXT)],
                  arrowcolor=[("disabled", TEXT_MUTED), ("active", TEXT)])

        # 菜单按钮（TMenubutton）：我们自己的界面一处都没用，但 **Tk 自带的文件对话框**
        # 用 ttk::menubutton 画「Directory:」那一行 —— 不配就是 clam 浅底 + 我们的近白字，
        # 整行看不清（用户截图里的实况）。配齐后它和 TCombobox 一个观感。
        style.configure("TMenubutton", background=SURFACE, foreground=TEXT,
                        arrowcolor=TEXT_DIM, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, padding=(8, 4))
        style.map("TMenubutton",
                  background=[("pressed", SURFACE_HOVER), ("active", SURFACE_HOVER),
                              ("disabled", "#20242d")],
                  foreground=[("disabled", TEXT_MUTED)],
                  arrowcolor=[("active", TEXT)],
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)])

        # 勾选框（TCheckbutton）：同理，我们自己的勾选框全是经典 tk.Checkbutton
        # （见 gui_layout._indicator_kw），这条只为 Tk 文件对话框的「显示隐藏文件」而配。
        # 指示器是独立元素，认 -indicatorbackground/-indicatorforeground/上下边框色 ——
        # 只配 background 的话那个小方块仍是白的。
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT,
                        focuscolor=PANEL, indicatorbackground=SURFACE,
                        indicatorforeground=TEXT, upperbordercolor=BORDER,
                        lowerbordercolor=BORDER)
        style.map("TCheckbutton",
                  background=[("active", PANEL)],
                  foreground=[("disabled", TEXT_MUTED)],
                  indicatorbackground=[("disabled", BG), ("selected", ACCENT)],
                  indicatorforeground=[("disabled", TEXT_MUTED)])

        # ---- 经典 Tk 控件（option database）----
        # 同样是「只为 Tk 自带的文件对话框」而配：它的 Directory / Files of type 下拉是
        # 经典 Menu、边缘是经典 Scrollbar、外框是经典 Toplevel —— 这些不吃 ttk 样式，
        # 只能走 option database。
        # 影响面已核查：本仓库代码里一处都没用经典 Scrollbar，唯一的 tk.Menu 是右键编辑
        # 菜单（显式配色，显式配的选项优先于 option database，不受影响）。
        # 更具体的 *TCombobox*Listbox.* 仍优先 → 下拉框的配色不动。
        root.option_add("*Listbox.background", SURFACE)
        root.option_add("*Listbox.foreground", TEXT)
        root.option_add("*Listbox.selectBackground", ACCENT)
        root.option_add("*Listbox.selectForeground", "#ffffff")
        root.option_add("*Menu.background", SURFACE)
        root.option_add("*Menu.foreground", TEXT)
        root.option_add("*Menu.activeBackground", ACCENT)
        root.option_add("*Menu.activeForeground", "#ffffff")
        root.option_add("*Menu.borderWidth", 0)
        root.option_add("*Scrollbar.background", SURFACE)
        root.option_add("*Scrollbar.troughColor", BG)
        root.option_add("*Scrollbar.activeBackground", SURFACE_HOVER)

        # 滚动条：细、暗、无箭头，跟聊天区融合。
        # 两个方向都配：垂直的是我们自己在用（聊天区 / 设置页 / 词库），
        # 水平的是 **Tk 文件对话框** 的文件列表在用（我们代码里用不到，
        # 但它是灰白的一整条）。同一份取值写两遍容易漂移，所以用循环。
        for _orient, _sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
            style.layout(f"{_orient}.TScrollbar",
                         [(f"{_orient}.Scrollbar.trough",
                           {"children": [(f"{_orient}.Scrollbar.thumb",
                                          {"expand": "1", "sticky": "nswe"})],
                            "sticky": _sticky})])
            style.configure(f"{_orient}.TScrollbar", background=SURFACE, troughcolor=BG,
                            bordercolor=BG, darkcolor=BG, lightcolor=BG,
                            arrowcolor=TEXT_DIM, gripcount=0)
            style.map(f"{_orient}.TScrollbar",
                      background=[("pressed", ACCENT_ACTIVE), ("active", SURFACE_HOVER)])

        # 设置弹窗的分页标签（Notebook）：clam 的默认 tab 是浅灰渐变，
        # 深色界面里就是一块亮斑（和「ttk.Entry 默认白底」同一类坑），必须逐状态配色。
        # tabmargins 左侧必须是 **0**：标签条的基准是 Notebook **外框**左边 ——
        # 也就是内容区那条左边框竖线（贯穿整窗、也是用户会拿来对的那条线）。
        # ⚠️ 别把它对齐到「页内分隔线的左端」：那条线本身被页面 20px 内边距缩进过，
        #    拿它当基准会让整排标签比内容区左边框右缩 22px（截图实测过，正是用户说的「没对齐」）。
        style.configure("TNotebook", background=PANEL, bordercolor=BORDER,
                        darkcolor=PANEL, lightcolor=PANEL, tabmargins=(0, 6, 10, 0))
        tab_pad = (16, 7)
        style.configure("TNotebook.Tab", font=FONT_UI, padding=tab_pad,
                        background=PANEL, foreground=TEXT_DIM, bordercolor=BORDER,
                        lightcolor=PANEL, darkcolor=PANEL, focuscolor=PANEL)
        # padding 必须逐状态映射成同一个值：只写 configure 的默认值时，selected/active
        # 会回落成 clam 自己的 tab 布局尺寸，选中标签的外框就比未选中的矮一截。
        # lightcolor/darkcolor/bordercolor 同理——任一状态回落成空值都会画出亮边。
        # 于是选中态**只靠背景色 + 前景色**区分，三态尺寸完全一致。
        # 注意两点实测坑：① ttk 取「第一个匹配的状态规格」，所以默认态必须排在最后；
        # ② 默认态**不能**写成 ("", …)——Tk 8.6 里空规格匹配任意状态，会把 selected 盖掉。
        style.map("TNotebook.Tab",
                  padding=[("selected", tab_pad), ("active", tab_pad),
                           ("!selected !active", tab_pad)],
                  background=[("selected", SURFACE), ("active", SURFACE_HOVER),
                              ("!selected !active", PANEL)],
                  foreground=[("selected", TEXT), ("active", TEXT),
                              ("!selected !active", TEXT_DIM)],
                  lightcolor=[("selected", SURFACE), ("active", SURFACE_HOVER),
                              ("!selected !active", PANEL)],
                  darkcolor=[("selected", SURFACE), ("active", SURFACE_HOVER),
                             ("!selected !active", PANEL)],
                  bordercolor=[("selected", BORDER), ("active", BORDER),
                               ("!selected !active", BORDER)])


# ================================================================ Tk 自带文件对话框

#: Tk 的文件列表是 C 实现的 `::tk::IconList`：它把每条文件名的 canvas item `-fill`
#: 取成**根 ttk 样式的前景**（所以上面 `.` 才刻意不设前景，否则是白底白字），
#: 但那块 canvas 的底色是**建窗时写死的 `#ffffff`** —— 没有对应选项、也吃不到
#: option database（实测 `*Canvas.background` 对新建 canvas 有效、对它无效），
#: `::tk::IconList` 自身只暴露 `-font`。
#: 于是「整窗深色」只剩一条路：等它建好之后把这块 canvas 改色。对话框是模态的，
#: 但 Tk 的 after 在它的嵌套事件循环里照常触发，所以用一条轮询兜住（导航一次会重画
#: 条目，所以要反复补，不是补一次就完）。
#:
#: ⚠️ **对话框挂在哪，取决于 `-parent`**（tkfbox.tcl 110-118）：
#:     `-parent .`          → `.__tk_filedialog`
#:     `-parent .toplevel`  → `.toplevel.__tk_filedialog`
#: 我们自己的调用点是「设置弹窗 → 导出日志压缩包」，传的 `parent` 是**设置弹窗**，
#: 所以它挂在设置窗下面。第一版这里写死了 `winfo children "."` + `.__tk_filedialog`，
#: 结果一个都找不到、列表保持浅色（真机复现过）—— 所以下面一律**从根递归搜**，
#: 不假设它在谁下面。
_DIALOG_LEAF = "__tk_filedialog"                # Tk 给文件名对话框起的窗口名（8.6 / 9.x 一致）
_CANVAS_LEAF = "cHull.canvas"                   # 图标列表那块 canvas 的固定末段
_SEARCH_BUDGET = 5000                           # 搜索上限（控件数），防止异常结构下遍历过久


def _find_file_dialog_roots(window) -> list[str]:
    """整棵树里所有 Tk 文件名对话框的窗口路径（`-parent` 决定它挂在哪一层）。"""
    found: list[str] = []
    stack, seen = ["."], 0
    while stack and seen < _SEARCH_BUDGET:
        parent = stack.pop()
        try:
            children = window.tk.call("winfo", "children", parent)
        except Exception:                       # noqa: BLE001  控件已销毁
            continue
        for child in children:
            child = str(child)
            seen += 1
            stack.append(child)
            if child.rsplit(".", 1)[-1] == _DIALOG_LEAF:
                found.append(child)
    return found


def _find_icon_canvas(window, dialog: str) -> str | None:
    """对话框里那块图标列表 canvas 的路径（按名字找，不写死整条路径）。"""
    stack, seen = [dialog], 0
    while stack and seen < _SEARCH_BUDGET:
        parent = stack.pop()
        try:
            children = window.tk.call("winfo", "children", parent)
        except Exception:                       # noqa: BLE001
            continue
        for child in children:
            child = str(child)
            seen += 1
            if child.endswith(_CANVAS_LEAF):
                return child
            stack.append(child)
    return None


def darken_file_dialog(window) -> int:
    """把 Tk 文件对话框里那块写死白底的图标列表改成深色。返回改到的 canvas 数。

    纯配色，**任何一步失败都静默跳过**（宁可是白的，也不能因为改色把对话框弄坏）。
    独立成函数是为了能脱离真对话框单测（tests/test_ui_theme_contrast.py 造同形状的树）。
    """
    changed = 0
    for dialog in _find_file_dialog_roots(window):
        canvas = _find_icon_canvas(window, dialog)
        if canvas is None:
            continue
        try:
            window.tk.call(canvas, "configure", "-background", SURFACE,
                           "-selectbackground", ACCENT, "-selectforeground", "#ffffff")
        except Exception:                       # noqa: BLE001  不是这个版本的结构
            continue
        if str(window.tk.call(canvas, "cget", "-background")) != SURFACE:
            continue                            # 没吃进去（Tk 改了实现）→ 不谎报
        changed += 1
        try:
            for item in window.tk.splitlist(window.tk.call(canvas, "find", "all")):
                if window.tk.call(canvas, "type", item) == "text":
                    window.tk.call(canvas, "itemconfigure", item, "-fill", TEXT)
        except Exception:                       # noqa: BLE001  条目列表取不到就算了
            pass
    return changed


def watch_file_dialog(root, interval_ms: int = 150, give_up_after: int = 80) -> None:
    """在打开文件对话框**之前**调用：轮询到它出现就补色，它一关就自动停。

    `give_up_after` = 对话框始终没出现时最多轮询多少拍（默认 ~12s），到点自己退出，
    绝不留下一条永不结束的 after 链。
    """
    try:
        # Windows / macOS 用的是**系统原生**对话框（tk_getSaveFile 在那里是内建命令，
        # tkfbox.tcl 那套 Tcl 对话框根本不会被创建）→ 没有可补色的树，直接别挂轮询。
        if str(root.tk.call("tk", "windowingsystem")) != "x11":
            return
    except Exception:                           # noqa: BLE001  解释器没了
        return

    state = {"seen": False, "ticks": 0}

    def tick() -> None:
        state["ticks"] += 1
        try:
            alive = bool(_find_file_dialog_roots(root))     # 用同一个搜索：它不一定挂在根下
        except Exception:                       # noqa: BLE001
            return
        if alive:
            state["seen"] = True
            darken_file_dialog(root)
            root.after(interval_ms, tick)
            return
        # 还没出现 → 继续等；见过又没了（= 用户关了对话框）或等太久 → 收工
        if not state["seen"] and state["ticks"] < give_up_after:
            root.after(interval_ms, tick)

    root.after(interval_ms, tick)
