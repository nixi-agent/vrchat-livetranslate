"""引擎 GUI 的既有共享狀態。"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass
class EngineCtx:
    """引擎生命周期所需的全部 **控件引用**、**可变状态** 与 **回调**。

    可变状态由 gui.py 的薄壳方法管理，本模块的函数通过参数接收，不直接持有。
    """
    # ── 核心可变状态 ──
    engines: list = field(default_factory=list)
    engine_dirs: list = field(default_factory=list)
    specs: list = field(default_factory=list)
    sinks: set = field(default_factory=set)
    pending_starts: int = 0
    current: dict = field(default_factory=dict)
    auto_scroll: bool = True
    closing: bool = False

    # ── 覆盖层输出 ──
    overlay_out: Any = None          # WristOverlay 实例
    desktop_out: Any = None          # DesktopOverlay 实例
    desktop_dragging: bool = False

    # ── 控件引用 ──
    power_btn: Any = None            # ttk.Button（单按钮开关；headless 下为 None）

    # ── Tk 变量 ──
    direction_var: Any = None        # tk.StringVar（翻译方向）
    chatbox_var: Any = None          # tk.BooleanVar
    overlay_var: Any = None          # tk.BooleanVar
    desktop_var: Any = None          # tk.BooleanVar
    vmic_var: Any = None             # tk.BooleanVar（译音输出）
    lang_pair: dict = field(default_factory=dict)

    # ── 桌面字幕控件变量 ──
    desktop_alpha_var: Any = None    # tk.DoubleVar
    desktop_alpha_lbl: Any = None    # ttk.Label
    desktop_font_var: Any = None     # tk.StringVar / tk.IntVar
    desktop_srcfont_var: Any = None  # tk.StringVar / tk.IntVar
    desktop_w_var: Any = None        # tk.DoubleVar
    desktop_h_var: Any = None        # tk.DoubleVar
    desktop_drag_btn: Any = None     # ttk.Button

    # ── 桌面字幕内部状态 ──
    desktop_save_job: Any = None     # root.after 的 job id
    desktop_alpha_touched: bool = False
    desktop_tuned: set = field(default_factory=set)

    # ── 气泡（用于推送给覆盖层）──
    bubbles: list = field(default_factory=list)

    # ── 基础设施 ──
    q: Any = None                    # queue.Queue
    root: Any = None                 # tk.Tk / tk.Misc
    cfg: Any = None                  # AppConfig
    start_job: Any = None            # root.after 的 job id（错开启动）
    stop_done_evt: Any = None        # threading.Event

    # ── 回调 ──
    set_status_fn: Optional[Callable] = None           # (level, msg) -> None
    on_engine_text_fn: Optional[Callable] = None       # (who, srcid, src, txt, final) -> None
    set_text_input_enabled_fn: Optional[Callable] = None  # (bool) -> None
    refresh_api_key_fn: Optional[Callable] = None      # () -> None
    start_room_fn: Optional[Callable] = None           # () -> None
    stop_room_fn: Optional[Callable] = None            # () -> None
    refresh_room_status_fn: Optional[Callable] = None  # () -> None
    sync_gate_probe_fn: Optional[Callable] = None      # () -> None
    stop_gate_probe_fn: Optional[Callable] = None      # () -> None
    maybe_replace_on_exit_fn: Optional[Callable] = None  # () -> None
    destroy_root_fn: Optional[Callable] = None         # () -> None
    save_ui_state_fn: Optional[Callable] = None        # () -> None
    cancel_poll_fn: Optional[Callable] = None          # () -> None（取消 poll 循环）
    proxy_fn: Optional[Callable] = None                # () -> MicProxy | None（每次现取：代理会被重开/关闭）
    power_state_fn: Optional[Callable] = None          # ("idle"|"running"|"stopping") -> None（刷开始/停止单按钮）
