"""Chat controls and mutable references; re-exported by gui_chat."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from .output.overlay import DegradedNoteRelay


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
    overlay_note_relay: DegradedNoteRelay = field(default_factory=DegradedNoteRelay)

    # ── 状态栏 ──
    last_status_level: str = "info"

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
