"""主窗右侧「房间玩家」栏：勾选要翻译谁，列表按距离排序。

分工：本模块只管**画**和**勾选** —— 读 `PlayerWarmup.status()["player_rows"]`，
勾选后写 config（`players.select_users` / `players.select_mode`）并通知正在跑的
`PlayerLeg` 重算名单。抓包、解码、限流全在 `vlt/players/` 里，这里一行都不碰。

为什么名单存 `user_id` 不存 actor：actor 只是同房间内的临时编号，换世界/重进会
重排复用；`user_id` 才是账号身份（见 `vlt/players/names.py:PlayerRecord`）。

设计取舍（暗色主题）：选中行不用浅色底（`#F0F7FF` 在 `PANEL=#1b1e26` 上会变成
一条白带），改用「深色微蓝底 + 名字加粗 + 左侧 2px 主色竖条」；状态字形也不用
emoji —— Tk 8.6 对 BMP 外字符（🔊 U+1F50A 等）只能显示成方框，改用 BMP 内的
`●`（在说，绿）/ `○`（已连接，蓝）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import ui_tk
from .i18n import t
from .players_settings import MODE_DECRYPT, parse_players
from .ui_theme import ACCENT, BORDER, PANEL, SURFACE, TEXT, TEXT_DIM, TEXT_MUTED

#: 栏宽（未按 DPI 缩放的基准像素）
PANEL_W = 226
#: 刷新周期：和设置页的预热状态一致（1 秒）
TICK_MS = 1000
#: 名字列最多显示几个字（超出截断加省略号）
NAME_MAX = 14
#: 名单模式下拉：(config 值, 界面文案)。文案走 `t()` 但 key 是变量，所以不参与
#: `tests/test_i18n.py` 的字面量扫描；四套词表里仍要各有一条。
SELECT_LABELS = (("all", "全都翻译"), ("only", "只翻勾选的"), ("except", "除了勾选的"))
#: 正在说话的字形颜色（绿色，主题里没有对应常量）
SPEAK_COLOR = "#2e9e5b"
#: 选中行的深色微蓝底
ROW_BG_SEL = "#232c3d"


# ── 对外入口 ──
def build_panel(gui) -> None:
    """在窗口右侧建栏（必须在 `_build_chat()` 之前调用：pack 先到先得右侧条带）。"""
    scale = float(getattr(gui, "_dpi_scale", 1.0) or 1.0)
    frame = ttk.Frame(gui._root, padding=(10, 12, 10, 12))
    frame.configure(width=int(PANEL_W * scale))
    frame.pack_propagate(False)
    gui._players_panel = frame
    gui._ppl_packed = False
    gui._ppl_vars: dict[str, tk.BooleanVar] = {}
    # 行控件缓存：key → 行控件包。距离每秒都在变，只能改文本不能重建控件，
    # 否则整栏会一直闪（Tk 销毁重建的每一帧都是可见的重绘）。
    gui._ppl_rows: dict[str, dict] = {}
    gui._ppl_order: list[str] = []

    title = tk.Label(frame, text="", bg=PANEL, fg=TEXT, font=ui_tk.FONT_BOLD_SM, anchor="w")
    title.pack(fill=tk.X)
    gui._ppl_title = title

    gui._ppl_mode_var = tk.StringVar(value="")
    combo = ttk.Combobox(frame, textvariable=gui._ppl_mode_var, state="readonly",
                         width=12, values=[label for _key, label in _mode_choices()])
    combo.pack(fill=tk.X, pady=(6, 8))
    gui._ppl_mode = combo
    combo.bind("<<ComboboxSelected>>", lambda _e: _on_mode(gui))

    body = tk.Frame(frame, bg=PANEL)
    body.pack(fill=tk.BOTH, expand=True)
    canvas = tk.Canvas(body, bg=PANEL, highlightthickness=0, bd=0, takefocus=0)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    inner = tk.Frame(canvas, bg=PANEL)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
    for widget in (canvas, inner):
        widget.bind("<MouseWheel>", lambda e: _on_wheel(canvas, e))
    gui._ppl_canvas = canvas
    gui._ppl_inner = inner

    footer = tk.Label(frame, text="", bg=PANEL, fg=TEXT_MUTED, font=ui_tk.FONT_META,
                      anchor="w", justify=tk.LEFT)
    footer.pack(fill=tk.X, pady=(8, 4))
    gui._ppl_footer = footer

    bar = ttk.Frame(frame)
    bar.pack(fill=tk.X)
    ttk.Button(bar, text=t("全选"), command=lambda: _select_all(gui)).pack(side=tk.LEFT)
    ttk.Button(bar, text=t("全不选"), command=lambda: _select_none(gui)).pack(side=tk.LEFT, padx=(4, 0))

    _set_visible(gui, _settings(gui).mode == MODE_DECRYPT)
    refresh(gui)


def refresh(gui) -> None:
    """每秒刷一次：先同步可见性，再增量更新行（只改文本，不重建控件）。"""
    frame = getattr(gui, "_players_panel", None)
    if frame is None:
        return
    try:
        alive = frame.winfo_exists()
    except Exception:                        # noqa: BLE001 - 窗口正在销毁
        return
    if not alive:
        return
    try:
        _sync(gui)
    except Exception as exc:                 # noqa: BLE001 - 刷新不该炸掉主循环
        print(f"[players-panel] 刷新失败：{type(exc).__name__}: {exc}")
    try:
        gui._root.after(TICK_MS, lambda: refresh(gui))
    except Exception:                        # noqa: BLE001
        pass


# ── 内部：数据 ──
def _settings(gui):
    return parse_players(getattr(gui._cfg, "players", None) or {})


def _rows(gui) -> list[dict]:
    """玩家行：自己恒在首位，其余来自预热器（按距离排序）。"""
    from .gui_settings import _players_warmup
    warmup = _players_warmup(gui)
    rows: list[dict] = []
    if warmup is not None:
        try:
            status = warmup.status() or {}
        except Exception:                    # noqa: BLE001
            status = {}
        rows = [dict(r) for r in (status.get("player_rows") or []) if isinstance(r, dict)]
    me = {"actor": 0, "user_id": "", "name": t("我"), "distance": None,
          "self": True, "speaking": False, "translating": False}
    return [me] + rows


def _sort_key(row: dict):
    if row.get("self"):
        return (0, 0.0, "")
    distance = row.get("distance")
    if not isinstance(distance, (int, float)):
        distance = 1e9                       # 没位置的垫底
    return (1, float(distance), str(row.get("name") or ""))


def _mode_choices() -> list[tuple[str, str]]:
    return [(key, t(label)) for key, label in SELECT_LABELS]


def _row_key(row: dict) -> str:
    """行的稳定标识：自己恒为同一行，别人按 user_id；还没学到 id 时退回落 actor。"""
    if row.get("self"):
        return "\x00self"
    user_id = str(row.get("user_id") or "")
    return user_id or f"\x00actor{row.get('actor')}"


def _set_text(label, text: str) -> None:
    """文本没变就别 configure：Tk 每次 configure 都要重排，白闪一下。"""
    if str(label.cget("text")) != text:
        label.configure(text=text)


# ── 内部：渲染 ──
def _sync(gui) -> None:
    ps = _settings(gui)
    visible = ps.mode == MODE_DECRYPT
    _set_visible(gui, visible)
    if not visible:
        return
    rows = _rows(gui)
    chosen = set(ps.select_users)
    _set_text(gui._ppl_title, f"{t('房间玩家')} ({len(rows) - 1})")
    live = sum(1 for r in rows if r.get("translating"))
    _set_text(gui._ppl_footer, t("已选 {n} / 同时翻译 {m}", n=len(chosen), m=live))
    label = dict(_mode_choices()).get(ps.select_mode)
    if label and gui._ppl_mode_var.get() != label:
        gui._ppl_mode_var.set(label)
    _render(gui, rows, chosen)


def _render(gui, rows: list[dict], chosen: set[str]) -> None:
    """增量重画：只有「玩家增删」才建/毁控件，距离与状态只改文本。

    以前每 tick 都比一次签名、签名不等就销毁重建整块列表；而签名里带实时距离，
    距离每秒都在变，于是右栏控件被反复销毁重建 —— 视觉上就是一直在闪。
    """
    inner = gui._ppl_inner
    ordered = sorted(rows, key=_sort_key)
    keys = [_row_key(r) for r in ordered]
    cache: dict[str, dict] = gui._ppl_rows
    for key in [k for k in cache if k not in keys]:
        cache.pop(key)["frame"].destroy()
        gui._ppl_vars.pop(key, None)
    for row, key in zip(ordered, keys):
        entry = cache.get(key)
        if entry is None:
            cache[key] = _render_row(gui, inner, row, chosen)
        else:
            _update_row(gui, entry, row, chosen)
    if keys != gui._ppl_order:
        # 只调整次序（距离排序会换位），控件本身不销毁
        for key in keys:
            cache[key]["frame"].pack_forget()
        for key in keys:
            cache[key]["frame"].pack(fill=tk.X, pady=1)
        gui._ppl_order = keys


def _render_row(gui, parent, row: dict, chosen: set[str]) -> dict:
    user_id = str(row.get("user_id") or "")
    selected = bool(user_id) and user_id in chosen
    bg = ROW_BG_SEL if selected else PANEL
    line = tk.Frame(parent, bg=bg)
    line.pack(fill=tk.X, pady=1)
    # 左侧 2px 竖条：只有选中行才亮（暗色下比整行浅底更克制）
    bar = tk.Frame(line, bg=ACCENT if selected else bg, width=2)
    bar.pack(side=tk.LEFT, fill=tk.Y)
    bg_widgets = [line, bar]

    is_self = bool(row.get("self"))
    var = tk.BooleanVar(value=selected)
    check = None
    if is_self or not user_id:
        # 自己和还没学到 user_id 的人都不能勾（名单按 user_id 存，没 id 无从持久化）
        pad = tk.Label(line, text="  ", bg=bg, width=2)
        pad.pack(side=tk.LEFT)
        bg_widgets.append(pad)
    else:
        check = tk.Checkbutton(line, variable=var, bg=bg, activebackground=bg,
                               selectcolor=SURFACE, highlightthickness=0, bd=0,
                               takefocus=0, width=2, padx=0,
                               command=lambda: _on_toggle(gui, user_id, var))
        check.pack(side=tk.LEFT)
        gui._ppl_vars[user_id] = var
        bg_widgets.append(check)

    glyph, color = _glyph(row)
    glyph_label = tk.Label(line, text=glyph, bg=bg, fg=color, font=ui_tk.FONT_SMALL,
                           width=2)
    glyph_label.pack(side=tk.RIGHT)
    dist_text = _distance_text(row)
    dist_label = tk.Label(line, text=dist_text, bg=bg, fg=TEXT_MUTED,
                          font=ui_tk.FONT_META, width=5, anchor="e")
    dist_label.pack(side=tk.RIGHT)
    bg_widgets.extend((glyph_label, dist_label))

    name = row.get("name") or (t("我") if is_self else "")
    name_text = _clip(str(name))
    name_label = tk.Label(line, text=name_text, bg=bg,
                          fg=TEXT if selected else TEXT_DIM,
                          font=ui_tk.FONT_BOLD_SM if selected else ui_tk.FONT_SMALL,
                          anchor="w")
    name_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
    bg_widgets.append(name_label)
    for widget in (line, name_label):
        widget.bind("<MouseWheel>", lambda e: _on_wheel(gui._ppl_canvas, e))
    return {"frame": line, "bar": bar, "bg": bg, "bg_widgets": bg_widgets,
            "var": var, "check": check, "glyph": glyph_label,
            "glyph_text": glyph, "glyph_color": color,
            "dist": dist_label, "dist_text": dist_text,
            "name": name_label, "name_text": name_text}


def _update_row(gui, entry: dict, row: dict, chosen: set[str]) -> None:
    """就地更新一行：只在值真的变了才 configure，避免无谓重排。"""
    user_id = str(row.get("user_id") or "")
    selected = bool(user_id) and user_id in chosen
    if entry["check"] is not None and bool(entry["var"].get()) != selected:
        entry["var"].set(selected)           # set() 不会触发 command，安全
    bg = ROW_BG_SEL if selected else PANEL
    if entry["bg"] != bg:
        entry["bg"] = bg
        for widget in entry["bg_widgets"]:
            widget.configure(bg=bg)
        entry["bar"].configure(bg=ACCENT if selected else bg)
        if entry["check"] is not None:
            entry["check"].configure(activebackground=bg)
        entry["name"].configure(fg=TEXT if selected else TEXT_DIM,
                                font=ui_tk.FONT_BOLD_SM if selected else ui_tk.FONT_SMALL)
    glyph, color = _glyph(row)
    if entry["glyph_text"] != glyph:
        entry["glyph_text"] = glyph
        entry["glyph"].configure(text=glyph)
    if entry["glyph_color"] != color:
        entry["glyph_color"] = color
        entry["glyph"].configure(fg=color)
    dist_text = _distance_text(row)
    if entry["dist_text"] != dist_text:
        entry["dist_text"] = dist_text
        entry["dist"].configure(text=dist_text)
    name = row.get("name") or (t("我") if row.get("self") else "")
    name_text = _clip(str(name))
    if entry["name_text"] != name_text:
        entry["name_text"] = name_text
        entry["name"].configure(text=name_text)


def _clip(name: str) -> str:
    return name if len(name) <= NAME_MAX else name[:NAME_MAX - 1] + "…"


def _distance_text(row: dict) -> str:
    from .players.positions import format_distance
    distance = row.get("distance")
    if not isinstance(distance, (int, float)):
        return ""
    return format_distance(float(distance))


def _glyph(row: dict) -> tuple[str, str]:
    """(字形, 颜色)：`●` 在说 / `○` 已连接在翻译 / 空 = 没参与。"""
    if row.get("speaking"):
        return "●", SPEAK_COLOR
    if row.get("translating"):
        return "○", ACCENT
    return "", TEXT_MUTED


def _on_wheel(canvas, event) -> None:
    try:
        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
    except Exception:                        # noqa: BLE001
        pass


def _set_visible(gui, visible: bool) -> None:
    frame = gui._players_panel
    if bool(getattr(gui, "_ppl_packed", False)) == visible:
        return
    gui._ppl_packed = visible
    if not visible:
        frame.pack_forget()
        return
    chat = getattr(gui, "_chat_frame", None)
    kwargs = dict(side=tk.RIGHT, fill=tk.Y, padx=(0, 10))
    if chat is not None:
        frame.pack(before=chat, **kwargs)    # 建栏晚于聊天区时也要回到它左边
    else:
        frame.pack(**kwargs)


# ── 内部：交互 ──
def _on_mode(gui) -> None:
    picked = gui._ppl_mode_var.get()
    key = next((k for k, label in _mode_choices() if label == picked), "")
    if not key or key == _settings(gui).select_mode:
        return
    _save(gui, "select_mode", key)


def _on_toggle(gui, user_id: str, var) -> None:
    ps = _settings(gui)
    users = list(ps.select_users)
    if bool(var.get()):
        if user_id not in users:
            users.append(user_id)
    else:
        users = [u for u in users if u != user_id]
    _save(gui, "select_users", users)
    if ps.select_mode == "all":
        # `all` 下勾人等于「我要只翻这几个」，顺手切到 only，否则勾了没反应
        _save(gui, "select_mode", "only")


def _select_all(gui) -> None:
    users = [str(r.get("user_id")) for r in _rows(gui)
             if r.get("user_id") and not r.get("self")]
    _save(gui, "select_users", users)
    if _settings(gui).select_mode == "except":
        _save(gui, "select_mode", "only")


def _select_none(gui) -> None:
    _save(gui, "select_users", [])
    if _settings(gui).select_mode == "only":
        # only + 空名单 = 谁都不翻，太反直觉；清空名单就回落「全都翻」
        _save(gui, "select_mode", "all")


def _save(gui, key: str, value) -> None:
    """写 config + 通知运行中的腿（写盘复用设置页那套，含状态提示）。"""
    from .gui_settings import save_players_setting
    try:
        save_players_setting(gui, key, value)
    except Exception as exc:                 # noqa: BLE001
        gui._set_status("error", t("❌ 没保存：{msg}", msg=exc))
        return
    _push(gui)


def _push(gui) -> None:
    """把新名单推给正在跑的 `PlayerLeg`（跨线程安全，见 `PlayerLeg.set_selection`）。"""
    ctx = getattr(gui, "_engine_ctx", None)
    leg = getattr(ctx, "players_leg", None) if ctx is not None else None
    tell = getattr(leg, "set_selection", None)
    if tell is None:
        return
    ps = _settings(gui)
    try:
        tell(ps.select_mode, ps.select_users)
    except Exception as exc:                 # noqa: BLE001
        print(f"[players-panel] 推送名单失败：{type(exc).__name__}: {exc}")
