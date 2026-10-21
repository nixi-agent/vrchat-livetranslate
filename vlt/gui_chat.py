"""轮询 / 气泡渲染 / 状态显示的 UI 构建与纯逻辑。

从 ``vlt.gui.TranslationGUI`` 的 M 区（约 L1329-L1443, L2367-L2639）提取而来。
所有 Tk 控件与可变状态通过 :class:`ChatCtx` 传入，函数本身不持有 ``self`` 引用。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import ttk

from .i18n import t
from .output.overlay import DegradedNoteRelay
from .ui_text import _Bubble
from .ui_theme import (
    BG,
    BORDER,
    COLOR_ERROR,
    COLOR_META,
    COLOR_MINE,
    COLOR_OK,
    COLOR_SRC_MINE,
    COLOR_SRC_THEIRS,
    COLOR_TEXT,
    COLOR_THEIRS,
    COLOR_WARN,
    MAX_BUBBLES,
    PANEL,
    TEXT_MUTED,
)
# ⚠️ 字体常量**必须运行时取** `ui_tk.FONT_*`，不能在 import 期捕获快照（issue #61）：
#    `apply_ui_font()` 是在建 root **之后**才把 ui_tk 里那份从占位字族
#    （"Microsoft YaHei UI"）改成解析后的字族。import 期捕获的话，Linux 上该字族不存在、
#    Tk 静默回落到另一个字体 —— 度量跟着飘，凡是按 `width=` 量出来的控件就会裁字
#    （实测：ru 的「发送」按钮少 1 个字符宽，`test_all_ui_languages_window_guard` 抓到的就是它）。
#    `vlt/gui_desktop.py` 里 `ui_tk.FONT_UI` 那种写法才是对的，照它来。
from . import ui_tk
from .ui_tk import _char_width_for, round_rect


# ================================================================ 上下文

@dataclass
class ChatCtx:
    """聊天 / 轮询 / 状态功能所需的全部 **控件引用** 与 **回调**。

    可变状态（``bubbles`` / ``current`` / ``canvas_w`` 等）由 gui.py 的薄壳
    方法管理，本模块的函数通过参数接收，不直接持有。
    """
    # ── 聊天区控件 ──
    canvas: Any = None                    # tk.Canvas
    vsb: Any = None                       # ttk.Scrollbar
    status_dot: Any = None                # tk.Label（彩色圆点）
    status_label: Any = None              # ttk.Label
    stats_label: Any = None               # ttk.Label

    # ── 输入行控件 ──
    text_var: Any = None                  # tk.StringVar
    text_entry: Any = None                # ttk.Entry
    send_btn: Any = None                  # ttk.Button

    # ── 聊天状态 ──
    bubbles: list = field(default_factory=list)    # list[_Bubble]
    current: dict = field(default_factory=dict)    # dict[str, _Bubble] 流式增量
    auto_scroll: bool = True
    canvas_w: int = 1
    relayout_job: Any = None              # root.after 句柄
    placeholder_drawn: bool = False       # 空聊天区占位提示是否已画（避免重复画）

    # ── 轮询队列 ──
    q: Any = None                         # queue.Queue
    poll_job: Any = None                  # root.after 句柄（poll 循环）

    # ── 引擎引用（poll 使用） ──
    engines_ref: list = field(default_factory=list)       # 可变 list 容器
    engine_dirs_ref: list = field(default_factory=list)   # 同上

    # ── 每跳现取的活引用（**不许缓存实例**） ──
    # 桌面字幕 / 手腕屏是「开始翻译」后才建的，而构建期就跑起了 poll：那时它们还是
    # None。早先拆分（6083052）把这里做成单元素 list 容器，却**从没写回** ——
    # poll 永远读到 None → `tick()` 一次都不跑 → 桌面字幕「收不到内容 / 拖不动 /
    # 配置热重载失效」（三条症状同源）。现在改成每跳现取，谁先谁后都对。
    overlay_out_fn: Optional[Callable] = None      # () -> Overlay | None
    desktop_out_fn: Optional[Callable] = None      # () -> DesktopOverlay | None
    pending_starts_fn: Optional[Callable] = None   # () -> int
    # 手腕屏**降级说明**的转发（判重与引擎 `_pump_loop` 那条路**共用同一个类**）：
    # 实例由本模块每跳现取 `overlay_out_fn()`，所以这里只放判重状态，不持有实例。
    overlay_note_relay: DegradedNoteRelay = field(default_factory=DegradedNoteRelay)

    # ── 状态栏 ──
    last_status_level: str = "info"
    #: 打字发送后的状态回报（`gui._set_status`）。拆分（6083052）后调用点漏传这个，
    #: 导致回车直接 `'NoneType' object is not callable` —— 见 issue #110。
    set_status_fn: Optional[Callable] = None

    # ── 房间轮询 ──
    room_status_next: float = 0.0

    # ── 门限轮询 ──
    gate_level_tick: int = 0

    # ── 回调 ──
    push_overlay_fn: Optional[Callable] = None     # () -> None
    push_desktop_fn: Optional[Callable] = None     # () -> None
    on_device_scan_result_fn: Optional[Callable] = None
    on_update_check_result_fn: Optional[Callable] = None
    on_download_progress_fn: Optional[Callable] = None
    on_download_done_fn: Optional[Callable] = None
    on_download_error_fn: Optional[Callable] = None
    on_voice_preview_done_fn: Optional[Callable] = None
    refresh_room_status_fn: Optional[Callable] = None
    sync_gate_level_fn: Optional[Callable] = None
    refresh_gate_level_fn: Optional[Callable] = None
    # 开始/停止单按钮控制：stop_done 事件 + 「引擎全部退出」兜底都要把按钮态刷回 idle
    power_state_fn: Optional[Callable] = None          # (state) -> None


# ================================================================ UI 构建

def build_chat(parent, ctx: ChatCtx, on_mousewheel_fn) -> Any:
    """构建聊天区 UI（Canvas + 滚动条），返回外层 ``ttk.Frame``。

    *on_mousewheel_fn* 是鼠标滚轮回调（``(event) -> None``），由 gui.py
    提供以便与其它滚轮逻辑保持一致。
    """
    chat_frame = ttk.Frame(parent)
    # 与头部两行同一个 14px 左边距：左右边界对齐才有「一栏到底」的秩序感
    chat_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

    # Treeview 不支持多行/换行，聊天气泡用 Canvas 手绘圆角矩形
    ctx.canvas = tk.Canvas(chat_frame, bg=BG, highlightthickness=1,
                           highlightbackground=BORDER, highlightcolor=BORDER)
    ctx.vsb = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL,
                            command=ctx.canvas.yview)
    ctx.canvas.configure(yscrollcommand=lambda f, l: on_canvas_scroll(ctx, f, l))
    ctx.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    ctx.vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

    ctx.canvas.bind("<Configure>",
                    lambda e: _on_canvas_configure(ctx, parent.winfo_toplevel(), e))
    ctx.canvas.bind("<MouseWheel>", on_mousewheel_fn)
    _draw_placeholder(ctx)
    return chat_frame


def _on_canvas_configure(ctx: ChatCtx, root: tk.Misc, event=None) -> None:
    """Configure 回调：先让占位提示跟上尺寸，再走原有的重排逻辑。

    ⚠️ 占位提示**只能**在这里补画：窗口首次布局时 canvas 尺寸才从 1 变成真值。
    绝不能用 `after_idle` 自旋等待尺寸 —— 窗口没被映射时它会无限重排（测试里
    就是这种场景），把事件循环占死。
    """
    if not ctx.bubbles and not ctx.placeholder_drawn:
        _draw_placeholder(ctx)
    on_canvas_configure(ctx, root, event)


def _draw_placeholder(ctx: ChatCtx) -> None:
    """空聊天区的居中占位提示：第一条消息到来时自动消失。

    纯 Canvas 图元（tag="placeholder"），零轮询、零定时器 —— 消失时机靠
    add_text 里的 _clear_placeholder 主动删除。
    """
    cv = ctx.canvas
    if cv is None or ctx.placeholder_drawn:
        return
    try:
        w, h = cv.winfo_width(), cv.winfo_height()
        if w <= 1 or h <= 1:
            return                     # 尺寸还没算出来：等下一次 Configure
        # 字体常量按上游口径**运行时取** ui_tk.FONT_*，不在 import 期捕获快照（issue #61）
        cv.create_text(w // 2, h // 2 - 10, text=t("译文会显示在这里"),
                       tag="placeholder", font=ui_tk.FONT, fill=TEXT_MUTED,
                       justify=tk.CENTER)
        cv.create_text(w // 2, h // 2 + 18, text=t("点「开始翻译」后开始说话"),
                       tag="placeholder", font=ui_tk.FONT_SMALL, fill=TEXT_MUTED,
                       justify=tk.CENTER)
        ctx.placeholder_drawn = True
    except Exception:  # noqa: BLE001 — 占位符画不出来绝不影响聊天区
        pass


def _clear_placeholder(ctx: ChatCtx) -> None:
    """删掉占位提示（幂等：删过一次后再删无副作用）。"""
    ctx.placeholder_drawn = False
    try:
        if ctx.canvas is not None:
            ctx.canvas.delete("placeholder")
    except Exception:  # noqa: BLE001
        pass


def build_input_row(parent, ctx: ChatCtx, cfg,
                    attach_edit_menu_fn) -> None:
    """打字输入行：不想开麦时用键盘替代麦克风，回车即发。

    它替代的是**麦克风**，所以只在方向含「我说」时可用（没会话时置灰，
    省得用户按了回车却没反应、以为坏了）。

    *attach_edit_menu_fn* 用于给 Entry 绑定右键编辑菜单（由 gui.py 提供）。
    """
    if not (cfg.text_input or {}).get("enabled", True):
        return                       # 配置里关掉了：整行不建
    row = ttk.Frame(parent, padding=(14, 8, 14, 6))
    row.pack(fill=tk.X)
    ttk.Label(row, text=t("打字:"), style="Dim.TLabel").pack(side=tk.LEFT)
    ctx.text_var = tk.StringVar()
    ctx.text_entry = ttk.Entry(row, textvariable=ctx.text_var, font=ui_tk.FONT_UI,
                               style="Key.TEntry")
    ctx.text_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
    ctx.text_entry.bind("<Return>",
                        lambda _e: _submit_typed(ctx))
    ctx.text_entry.bind("<Escape>",
                        lambda _e: ctx.text_var.set(""))
    attach_edit_menu_fn(ctx.text_entry)
    ctx.send_btn = ttk.Button(
        row, text=t("发送"),
        width=_char_width_for(t("发送"), ui_tk.FONT_UI, 8),
        command=lambda: _submit_typed(ctx))
    ctx.send_btn.pack(side=tk.LEFT)
    ttk.Label(row, text=t("回车发送 · Esc 清空"),
              style="Muted.TLabel").pack(side=tk.LEFT, padx=(8, 0))
    set_text_input_enabled(ctx, False)


def build_status(parent, ctx: ChatCtx) -> None:
    """构建状态栏 UI（圆点 + 状态文案 + 统计汇总）。"""
    bar = ttk.Frame(parent, padding=(14, 7))
    bar.pack(fill=tk.X)
    # 左侧：彩色圆点（连接状态）+ 最新一条状态消息；右侧放统计汇总——两者分开，
    # 否则"等待收尾"这类瞬时消息会把"已翻译 N 条 / 首增量 Xms"覆盖掉。
    ctx.status_dot = tk.Label(bar, text="●", bg=PANEL, fg=TEXT_MUTED,
                              font=ui_tk.FONT_STATUS, bd=0)
    ctx.status_dot.pack(side=tk.LEFT, padx=(0, 6))
    ctx.status_label = ttk.Label(bar, text=t("就绪"), style="Status.TLabel")
    ctx.status_label.pack(side=tk.LEFT)
    ctx.stats_label = ttk.Label(bar, text="", style="Muted.TLabel")
    ctx.stats_label.pack(side=tk.RIGHT)


# ================================================================ 输入行

def set_text_input_enabled(ctx: ChatCtx, on: bool) -> None:
    """没有「我说」方向的会话时置灰：打字替代的是麦克风，没腿就没输出面。"""
    if ctx.text_entry is None:
        return
    for w in (ctx.text_entry, ctx.send_btn):
        w.state(["!disabled"] if on else ["disabled"])


def _submit_typed(ctx: ChatCtx) -> str:
    """输入行的**唯一入口**：回车与「发送」按钮都走这里（issue #110）。

    拆分（6083052）后两个调用点各写一遍参数，结果都漏了 engines / engine_dirs /
    set_status_fn —— 回车抛 `'NoneType' object is not callable`、按钮抛
    `TypeError: missing 3 required positional arguments`，打字输入整整坏了三个版本。
    现在只保留一个入口：以后改参数，回车和按钮不会分叉。
    """
    return on_text_enter(ctx, list(ctx.engines_ref), list(ctx.engine_dirs_ref),
                         ctx.set_status_fn)


def on_text_enter(ctx: ChatCtx, engines: list = (), engine_dirs: list = (), set_status_fn=None) -> str:
    """Entry 的 ``<Return>`` 回调：发送并吃掉事件。"""
    send_typed(ctx, list(engines), list(engine_dirs), set_status_fn)
    return "break"                  # 吃掉回车：否则 Tk 会再响一声提示音


def _notify(set_status_fn, level: str, msg: str) -> None:
    """状态回报：headless / 测试里 `set_status_fn` 可能是 None（不许因此崩掉发送）。"""
    if set_status_fn is not None:
        set_status_fn(level, msg)


def send_typed(ctx: ChatCtx, engines: list, engine_dirs: list,
               set_status_fn) -> None:
    """把输入框里的文字交给「我说」那条腿翻译并送出（下游与说话完全一致）。"""
    if ctx.text_entry is None or ctx.text_var is None:
        return
    text = ctx.text_var.get().strip()
    if not text:
        return
    targets = [e for e, dirn in zip(engines, engine_dirs) if dirn == "mine"]
    if not targets:
        _notify(set_status_fn, "warn", t("打字替代的是麦克风 —— 先点「开始翻译」，"
                                        "且方向要含「我说」"))
        return
    sent = sum(1 for e in targets if e.send_text(text))
    if sent:
        ctx.text_var.set("")      # 清空：肉眼确认已发出
        _notify(set_status_fn, "info", t("打字已送出（{n} 字），翻译中…", n=len(text)))
    else:
        _notify(set_status_fn, "warn", t("引擎还没就绪，稍后重试"))


# ================================================================ API 密钥

def check_api_key(ctx: ChatCtx,
                  refresh_key_status_fn: Callable,
                  current_key_slot_fn: Callable,
                  set_status_fn) -> None:
    """启动前检查 API 密钥是否已配置。"""
    refresh_key_status_fn()
    try:
        from .config import load_api_key
        load_api_key(slot=current_key_slot_fn())
    except SystemExit as e:
        set_status_fn("error", str(e))


# ================================================================ Canvas 事件

def on_canvas_scroll(ctx: ChatCtx, first: str, last: str) -> None:
    """Canvas 滚动回调：同步滚动条并判断是否恢复自动跟随。"""
    ctx.vsb.set(first, last)
    # 滚到底部才恢复自动跟随；用户手动往上翻时不抢
    ctx.auto_scroll = float(last) >= 0.99


def on_mousewheel(ctx: ChatCtx, event) -> None:
    """鼠标滚轮：按 delta 滚动 Canvas。"""
    ctx.canvas.yview_scroll(int(-event.delta / 120), "units")


def on_canvas_configure(ctx: ChatCtx, root: tk.Misc, event=None) -> None:
    """Canvas 尺寸变化：合并到 120ms 后一次性重排（拖拽窗口会连发 Configure）。"""
    w = event.width if event is not None else ctx.canvas.winfo_width()
    if w <= 1 or abs(w - ctx.canvas_w) <= 2:
        return
    ctx.canvas_w = w
    if ctx.relayout_job is not None:
        try:
            root.after_cancel(ctx.relayout_job)
        except Exception:                       # noqa: BLE001
            pass
    ctx.relayout_job = root.after(120, lambda: redraw_all(ctx))


# ================================================================ 聊天气泡

def add_text(ctx: ChatCtx, source: str, text: str, is_final: bool,
             who: str = "mine", label: str = "") -> None:
    """添加/更新一条聊天气泡（流式增量就地重画，终版封口）。"""
    now_str = datetime.now().strftime("%H:%M:%S")
    cur = ctx.current.get(who)
    if cur is not None:
        # 流式增量：就地重画同一条气泡（终版只做"封口"，绝不新插第二条）
        cur.source = source
        cur.text = text
        if label:
            cur.label = label      # 远端昵称：partial 就地刷新时也带上
        if is_final:
            cur.final = True
            ctx.current.pop(who, None)
        if ctx.canvas is not None:
            redraw_current(ctx, cur)
        if ctx.auto_scroll and ctx.canvas is not None:
            ctx.canvas.yview_moveto(1.0)
        return
    # 没有正在刷新的气泡 → 新建
    b = _Bubble(who=who, source=source, text=text, ts=now_str,
                final=is_final, label=label)
    if not is_final:
        ctx.current[who] = b
    b.y = (ctx.bubbles[-1].y + ctx.bubbles[-1].h + 8) if ctx.bubbles else 8
    ctx.bubbles.append(b)
    if ctx.canvas is not None:
        _clear_placeholder(ctx)
        draw_bubble(ctx, b)
        trim(ctx)
        update_scrollregion(ctx)
        if ctx.auto_scroll:
            ctx.canvas.yview_moveto(1.0)


def draw_bubble(ctx: ChatCtx, b: _Bubble) -> int:
    """按已验证配方画一条气泡，返回下一条气泡的起始 y。

    每条气泡两行：**原文小字（上） + 译文大字（下）**——主次分明，
    你要读的永远是下面那行大字。服务端没返回原文（或原文与译文相同）时
    只画译文，不留空行。
    """
    cv = ctx.canvas
    w = ctx.canvas_w if ctx.canvas_w > 1 else 600
    maxw = int(w * 0.62)                       # width 参数自带自动换行

    # 1) 先量尺寸：两张文字都先画在 (0,0)，量完再挪进气泡
    tid_big = cv.create_text(0, 0, text=b.text, width=maxw,
                             anchor="nw", font=ui_tk.FONT, fill=COLOR_TEXT)
    bx1, by1, bx2, by2 = cv.bbox(tid_big)
    big_w, big_h = bx2 - bx1, by2 - by1

    tid_small = None
    small_w = small_h = 0
    # 小字行：远端成员优先显示**昵称**（谁在说），本机气泡仍是原文。
    # 为空、或与大字译文相同时不画（省一行空白）。
    small_raw = b.label if (b.label or "").strip() else b.source
    small_key = (small_raw or "").strip()
    if small_key and small_key != (b.text or "").strip():
        tid_small = cv.create_text(
            0, 0, text=small_raw, width=maxw, anchor="nw",
            font=ui_tk.FONT_SMALL,
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
            font=ui_tk.FONT_META, fill=COLOR_META))
        y += 14
    bx = w - 18 - bw if b.who == "mine" else 18  # 右 / 左
    fill = COLOR_MINE if b.who == "mine" else COLOR_THEIRS
    rid = round_rect(cv, bx, y, bx + bw, y + bh, 12, fill=fill, outline="")
    # ⚠️ 必须压到最底层（tag_lower 不带第二参数）。
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


def redraw_current(ctx: ChatCtx, b: _Bubble) -> None:
    """流式增量：删掉旧图元，在同一个起始 y 位置重画同一条气泡。"""
    for iid in b.items:
        ctx.canvas.delete(iid)
    old_h = b.h
    draw_bubble(ctx, b)
    delta = b.h - old_h
    if delta:
        # 气泡长高时把下面的气泡整体下移，避免重叠
        idx = ctx.bubbles.index(b)
        # ⚠️ Canvas.move(tagOrId, x, y) 只接受**一个** tagOrId。
        for other in ctx.bubbles[idx + 1:]:
            other.y += delta
            for iid in other.items:
                ctx.canvas.move(iid, 0, delta)
        update_scrollregion(ctx)


def trim(ctx: ChatCtx) -> None:
    """气泡超过上限时删最旧的若干条，剩余图元整体上移。"""
    removed = 0
    while len(ctx.bubbles) > MAX_BUBBLES:
        old = ctx.bubbles.pop(0)
        if ctx.current.get(old.who) is old:
            ctx.current.pop(old.who, None)
        removed += old.h + 8
        for iid in old.items:
            ctx.canvas.delete(iid)
    if removed:
        ctx.canvas.move("all", 0, -removed)
        for b in ctx.bubbles:
            b.y -= removed


def update_scrollregion(ctx: ChatCtx) -> None:
    """根据气泡列表更新 Canvas 的可滚动区域。"""
    if ctx.canvas is None:
        return
    content = 8
    if ctx.bubbles:
        last = ctx.bubbles[-1]
        content = last.y + last.h + 8
    h = ctx.canvas.winfo_height()
    ctx.canvas.configure(
        scrollregion=(0, 0, max(ctx.canvas_w, 1), max(content, h)))


def redraw_all(ctx: ChatCtx) -> None:
    """窗口 resize 后按新宽度全部重画（消息多时也比逐条挪快）。"""
    ctx.relayout_job = None
    if ctx.canvas is None:
        return
    ctx.canvas.delete("all")
    ctx.placeholder_drawn = False          # delete("all") 连占位提示一起删了
    if not ctx.bubbles:
        _draw_placeholder(ctx)             # 空聊天区：按新尺寸重新居中
    y = 8
    for b in ctx.bubbles:
        b.y = y
        y = draw_bubble(ctx, b)
    update_scrollregion(ctx)
    if ctx.auto_scroll:
        ctx.canvas.yview_moveto(1.0)


def cancel_poll(ctx: ChatCtx, root: tk.Misc) -> None:
    """取消 poll 循环的 ``root.after`` 调度，防止窗口销毁后回调仍在事件队列里。"""
    if ctx.poll_job is not None:
        try:
            root.after_cancel(ctx.poll_job)
        except Exception:                       # noqa: BLE001
            pass
        ctx.poll_job = None


# ================================================================ 状态栏

def set_status(ctx: ChatCtx, level: str, msg: str) -> None:
    """设置状态栏消息与颜色级别（info / warn / error）。"""
    ctx.last_status_level = level
    if ctx.status_label is None:
        return
    # 状态色走圆点，文字保持中性色——更现代，也不会整行刺眼
    colors = {"info": COLOR_OK, "warn": COLOR_WARN, "error": COLOR_ERROR}
    ctx.status_dot.configure(fg=colors.get(level, TEXT_MUTED))
    ctx.status_label.configure(text=t("状态：{msg}", msg=msg))


def refresh_status(ctx: ChatCtx, engines: list, direction_var,
                   stats: dict) -> None:
    """刷新统计栏（运行中 / 已翻译 N 条 / 首增量 Xms）。"""
    if ctx.stats_label is None:
        return
    parts: list[str] = []
    if any(e.running for e in engines):
        parts.append(t("运行中"))
        # 常驻提示：状态栏正文会被引擎消息覆盖，这里不会。
        if direction_var is not None and direction_var.get() == "mine":
            parts.append(t("仅翻译你说的话"))
    if any(e.chatbox is not None for e in engines):
        sent = sum(e.chatbox.sent_ok
                   for e in engines if e.chatbox is not None)
        parts.append(t("已翻译 {n} 条", n=sent))
    ms = stats.get("first_delta_ms") or stats.get("connect_ms")
    if ms is not None:
        parts.append(t("首增量 {ms}ms", ms=f"{ms:.0f}"))
    ctx.stats_label.configure(text=" · ".join(parts))


# ================================================================ 主轮询


def setup_poll_ctx(ctx: ChatCtx, gui) -> None:
    """设置 ChatCtx 上的回调引用（从 gui 实例读取）。"""
    c = ctx; c.q = gui._q; c.engines_ref = gui._engines; c.engine_dirs_ref = gui._engine_dirs
    c.set_status_fn = gui._set_status          # 打字发送的状态回报（issue #110）
    c.room_status_next = gui._room_status_next; c.gate_level_tick = gui._gate_level_tick
    # 活引用：闭包捕获的是 gui 实例，取值发生在**每跳 poll 里**，所以
    # 桌面字幕/手腕屏晚于构建期创建也能被 tick（见 ChatCtx 里那段说明）
    c.overlay_out_fn = lambda: gui._overlay_out
    c.desktop_out_fn = lambda: gui._desktop_out
    c.pending_starts_fn = lambda: gui._pending_starts
    c.push_overlay_fn = gui._push_overlay; c.push_desktop_fn = gui._push_desktop
    c.on_device_scan_result_fn = gui._on_device_scan_result; c.on_update_check_result_fn = gui._on_update_check_result
    c.on_download_progress_fn = gui._on_download_progress; c.on_download_done_fn = gui._on_download_done
    c.on_download_error_fn = gui._on_download_error; c.on_voice_preview_done_fn = gui._on_voice_preview_done
    c.refresh_room_status_fn = gui._refresh_room_status_label; c.sync_gate_level_fn = gui._sync_gate_level_probe
    c.refresh_gate_level_fn = gui._refresh_gate_level
    c.power_state_fn = gui._set_power_state


def poll(ctx: ChatCtx, root: tk.Misc,
         set_status_fn: Callable,
         add_text_fn: Callable,
         refresh_status_fn: Callable,
         stats: dict,
         ) -> None:
    """主轮询：从队列读取消息并分发到各处理器（每 50ms 一次）。

    手腕屏 / 桌面字幕实例**不通过参数传进来** —— 它们是「开始翻译」后才创建的，
    而本循环在构建期就起跑了；一律走 ``ctx.*_out_fn()`` 每跳现取（见 ChatCtx）。
    """
    try:
        while True:
            item = ctx.q.get_nowait()
            kind = item[0]
            if kind == "text":
                add_text_fn(item[2], item[3], item[4], who=item[1])
                if ctx.push_overlay_fn:
                    ctx.push_overlay_fn()
                if ctx.push_desktop_fn:
                    ctx.push_desktop_fn()
            elif kind == "status":
                set_status_fn(item[1], item[2])
            elif kind == "stats":
                stats.update(item[1])
                refresh_status_fn()
            elif kind == "devices":
                if ctx.on_device_scan_result_fn:
                    ctx.on_device_scan_result_fn(item[1], item[2], item[3])
            elif kind == "devices_error":
                set_status_fn("warn",
                              t("设备扫描失败：{msg}", msg=item[1]))
                # gui.py 薄壳负责把 _device_scan_pending 置 False
            elif kind == "update_check":
                if ctx.on_update_check_result_fn:
                    ctx.on_update_check_result_fn(
                        item[1], item[2], item[3], item[4])
            elif kind == "update_progress":
                if ctx.on_download_progress_fn:
                    ctx.on_download_progress_fn(item[1], item[2])
            elif kind == "update_download_done":
                if ctx.on_download_done_fn:
                    ctx.on_download_done_fn(item[1])
            elif kind == "update_download_error":
                if ctx.on_download_error_fn:
                    ctx.on_download_error_fn(item[1])
            elif kind == "room":
                # 远端成员的一句话：进聊天气泡
                add_text_fn("", item[3], item[4],
                            who=f"peer:{item[2]}", label=item[1])
                if ctx.push_overlay_fn:
                    ctx.push_overlay_fn()
            elif kind == "room_status":
                # 房间链路状态：按行首符号定状态栏颜色级别
                txt = str(item[1])
                level = ("error" if txt.startswith("❌")
                         else "warn" if txt.startswith("⚠️")
                         else "info")
                set_status_fn(level, txt)
            elif kind == "voice_preview":
                if ctx.on_voice_preview_done_fn:
                    ctx.on_voice_preview_done_fn(
                        item[1], item[2], item[3])
            elif kind == "stop_done":
                # 引擎收尾完成 → 单按钮刷回 idle（超时也要放开，宁可让用户再点一次也不能把界面锁死）
                _, elapsed, n_engines, incomplete = item
                if ctx.power_state_fn:
                    ctx.power_state_fn("idle")
                print(f"[gui] 停止收尾完成：{n_engines} 个引擎，"
                      f"用时 {elapsed:.2f}s"
                      + ("（有引擎超时未退出，仍在关设备）"
                         if incomplete else ""),
                      flush=True)
                if ctx.last_status_level != "error":
                    if incomplete:
                        set_status_fn("warn",
                                      t("已停止（上一次会话仍在收尾）"))
                    else:
                        set_status_fn("info", t("已停止"))
    except queue.Empty:
        pass

    # 引擎全部退出 → 恢复按钮
    engines = ctx.engines_ref
    pending_starts = ctx.pending_starts_fn() if ctx.pending_starts_fn else 0
    if (pending_starts == 0 and engines
            and all(not e.running for e in engines)):
        if ctx.power_state_fn:
            ctx.power_state_fn("idle")
        if ctx.last_status_level != "error":
            set_status_fn("info", t("已停止"))
        # gui.py 薄壳负责清空 engines / engine_dirs

    # 手腕屏 / 桌面字幕的热重载 / 淡出。
    # ⚠️ 必须**每跳现取**：两个实例都是「开始翻译」后才创建的，构建期这里还是 None；
    #    早先缓存进 list 容器却从不写回 → tick() 永不执行（桌面字幕内容/拖动/热重载全废）。
    ov = ctx.overlay_out_fn() if ctx.overlay_out_fn else None
    if ov is not None:
        ov.tick()
        # 手腕屏的**降级说明**也要推给状态栏 —— GUI 下 `Engine._overlay` 恒为 None（实例由界面
        # 持有），引擎 `_pump_loop` 里那条转发在这里根本跑不到：只打日志的话用户永远看不到
        # 「面板为什么偶尔不刷新」（#98 合并后复审发现的缺口，见 issue #99）。
        # 判重与那条路共用 `DegradedNoteRelay`，只在变化时推（降级 warn / 恢复 info）。
        ctx.overlay_note_relay.relay(ov, set_status_fn)
    dov = ctx.desktop_out_fn() if ctx.desktop_out_fn else None
    if dov is not None:
        dov.tick()

    # 房间行状态每 0.5s 刷一次
    _now = time.monotonic()
    if _now >= ctx.room_status_next:
        ctx.room_status_next = _now + 0.5
        if ctx.refresh_room_status_fn:
            ctx.refresh_room_status_fn()

    # 输入门限的实时电平条：每 100ms 刷一次（poll 本身 50ms 一跳）
    ctx.gate_level_tick += 1
    if ctx.gate_level_tick % 2 == 0:
        if ctx.sync_gate_level_fn:
            ctx.sync_gate_level_fn()
        if ctx.canvas is not None and ctx.refresh_gate_level_fn:
            ctx.refresh_gate_level_fn()

    ctx.poll_job = root.after(50, lambda: poll(
        ctx, root, set_status_fn, add_text_fn, refresh_status_fn, stats))
