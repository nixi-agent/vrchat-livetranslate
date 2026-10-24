"""房间连接 / 发布 / 状态的 UI 构建与纯逻辑。

从 ``vlt.gui.TranslationGUI`` 的 B 区（约 L634-L844）提取而来。
所有 Tk 控件与可变状态通过 :class:`RoomCtx` 传入，函数本身不持有 ``self`` 引用。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import ttk

from .i18n import t
from .room.client import RoomClient
from .room.model import ConnectionState, RoomConfig, RoomMessage
from .room.protocol import new_room_code
from .room.publisher import SourcePublisher, should_publish
from .session.chatgpt_output import allowed, current
from .ui_state import room_status_text


# ================================================================ 上下文

@dataclass
class RoomCtx:
    """房间功能所需的全部**控件引用**与**回调**。

    可变状态（``room`` / ``room_cfg``）由 gui.py 的薄壳方法管理，
    本模块的函数通过参数接收，不直接持有。
    """
    # ── 控件 ──
    room_btn: Any = None              # ttk.Button
    room_status: Any = None           # ttk.Label
    room_var: Any = None              # tk.BooleanVar（连接意图）
    room_code_var: Any = None         # tk.StringVar（设置页里的房间码）
    room_nick_var: Any = None         # tk.StringVar（设置页里的昵称）

    # ── 回调 ──
    set_status_fn: Optional[Callable] = None     # (level, msg) -> None
    open_settings_fn: Optional[Callable] = None  # (page?) -> None
    refresh_status_label_fn: Optional[Callable] = None  # () -> None
    q: Any = None                     # queue.Queue（塞事件给 _poll）


# ================================================================ 状态查询

def _get_state(room: Any):
    """安全取连接态快照：返回 ``ConnectionState`` 或 ``None``。"""
    if room is None:
        return None
    try:
        return room.state()
    except Exception:                               # noqa: BLE001
        return None


# ================================================================ UI 构建

def build_room_row(parent, ctx: RoomCtx, room_cfg: RoomConfig) -> Any:
    """构建房间行 UI（一行：``[连接房间] 状态：未连接 · 0 人``）。

    返回创建的 ``ttk.Frame``。控件引用同时写回 *ctx*。
    """
    row = ttk.Frame(parent, padding=(14, 0, 14, 10))
    row.pack(fill=tk.X)

    ctx.room_var = tk.BooleanVar(value=bool(room_cfg.enabled))
    ctx.room_btn = ttk.Button(
        row, text=t("连接房间"), style="Accent.TButton",
    )                                   # command 由 gui.py 薄壳绑定（需 self）
    ctx.room_btn.pack(side=tk.LEFT)
    ctx.room_status = ttk.Label(
        row,
        text=room_status_text(None),    # 初始态：未连接
        style="Muted.TLabel",
    )
    ctx.room_status.pack(side=tk.LEFT, padx=(14, 0))
    return row


# ================================================================ 按钮 / 状态刷新

def refresh_btn(ctx: RoomCtx, room: Any) -> None:
    """按连接态刷新按钮文案 / 可用态 / 样式。

    未连接 → 「连接房间」（蓝色）；连接中 → 「连接中」（禁用）；
    在线 / 重连 / 错误 → 「断开连接」（红棕 Danger）。
    """
    btn = ctx.room_btn
    if btn is None:
        return
    st = _get_state(room)
    if st is None:
        text, state, style = t("连接房间"), tk.NORMAL, "Accent.TButton"
    elif st.conn is ConnectionState.CONNECTING:
        text, state, style = t("连接中"), tk.DISABLED, "Accent.TButton"
    else:
        text, state, style = t("断开连接"), tk.NORMAL, "Danger.TButton"
    try:
        btn.configure(text=text, state=state, style=style)
    except Exception:                   # noqa: BLE001
        pass


def refresh_status_label(ctx: RoomCtx, room: Any) -> None:
    """刷新房间行状态文案 + 按钮（**只在主线程调用**）。"""
    if ctx.room_status is None:
        return
    try:
        ctx.room_status.configure(text=room_status_text(room))
    except Exception:                   # noqa: BLE001
        pass
    refresh_btn(ctx, room)


# ================================================================ 设置页字段变更

def on_room_generate(gui_or_ctx, field_change_fn: Callable) -> None:
    """生成一个合法房间码填进输入框，并走与手输完全相同的落盘 / 重连路径。"""
    code = new_room_code()
    # 控件变量可能在 self 或 ctx 上
    code_var = getattr(gui_or_ctx, '_room_code_var', None)
    if code_var is None:
        code_var = getattr(gui_or_ctx, 'room_code_var', None)
    code_var.set(code)
    field_change_fn()
    print(f"[gui] 已生成随机房间码：{code}", flush=True)


# ================================================================ 配置同步 / 保存

def sync_room_cfg_from_fields(gui_or_ctx, room_cfg: RoomConfig) -> RoomConfig:
    """把界面上的勾选 / 房间码 / 昵称同步进 *room_cfg*（只改内存，不落盘）。

    *gui_or_ctx* 可以是 ``TranslationGUI`` 实例（薄壳委托时传 self）或
    ``RoomCtx``（纯函数调用时传 ctx）。按属性名探测，两种入参走同一套取值逻辑。

    返回（可能被刷新过的）RoomConfig。
    """
    code = room_cfg.room_code
    nick = room_cfg.nickname
    enabled = room_cfg.enabled
    # 控件变量可能尚未创建（设置页懒加载）→ hasattr 探测
    _code_var = getattr(gui_or_ctx, '_room_code_var', None)
    _nick_var = getattr(gui_or_ctx, '_room_nick_var', None)
    _room_var = getattr(gui_or_ctx, '_room_var', None)
    # 也尝试 ctx 上的别名（纯函数路径）
    if _code_var is None:
        _code_var = getattr(gui_or_ctx, 'room_code_var', None)
    if _nick_var is None:
        _nick_var = getattr(gui_or_ctx, 'room_nick_var', None)
    if _room_var is None:
        _room_var = getattr(gui_or_ctx, 'room_var', None)
    if _code_var is not None:
        code = (_code_var.get() or "").strip()
    if _nick_var is not None:
        nick = (_nick_var.get() or "").strip()
    if _room_var is not None:
        enabled = bool(_room_var.get())
    return room_cfg.with_overrides(enabled=enabled, room_code=code, nickname=nick)


# ================================================================ 启动 / 停止

def start_room(room_cfg: RoomConfig,
               publisher: SourcePublisher,
               on_message_fn: Callable,
               on_status_fn: Callable,
               set_status: Callable) -> Optional[RoomClient]:
    """建 RoomClient 并启动（**幂等**）。成功返回 client，失败返回 None。"""
    try:
        publisher.reset()
        client = RoomClient(room_cfg,
                            on_message=on_message_fn,
                            on_status=on_status_fn)
        client.start()
        return client
    except Exception as exc:            # noqa: BLE001
        print(f"[room] ⚠️ 启动失败（翻译不受影响）：{type(exc).__name__}: {exc}",
              flush=True)
        set_status("warn", t("房间出错（翻译不受影响）：{msg}",
                              msg=f"{type(exc).__name__}: {exc}"))
        return None


def stop_room(room: Any, publisher: SourcePublisher) -> None:
    """停掉 RoomClient（**幂等、≤5s**）。任何异常只留痕。"""
    if room is None:
        return
    try:
        room.stop(timeout=5.0)
    except Exception as exc:            # noqa: BLE001
        print(f"[room] ⚠️ 停止时出错（忽略）：{type(exc).__name__}: {exc}",
              flush=True)
    finally:
        publisher.reset()


# ================================================================ 回调（房间线程 → 队列）

def on_room_message(msg: RoomMessage, room_cfg: RoomConfig, q: Any) -> None:
    """（**房间线程**）收到远端成员的一句话 → 只塞队列，绝不碰 Tk。"""
    try:
        if not room_cfg.show_remote:
            return
        q.put(("room", msg.nick, msg.peer_id, msg.text, msg.is_final))
    except Exception as exc:            # noqa: BLE001
        print(f"[room] ⚠️ 入队远端消息失败（忽略）：{type(exc).__name__}: {exc}",
              flush=True)


def on_room_status(text: str, q: Any) -> None:
    """（**房间线程**）房间链路状态 → 只塞队列，由 _poll 在主线程落到状态栏。"""
    try:
        q.put(("room_status", str(text)))
    except Exception as exc:            # noqa: BLE001
        print(f"[room] ⚠️ 入队房间状态失败（忽略）：{type(exc).__name__}: {exc}",
              flush=True)


# ================================================================ 上行发布

def publish_to_room(room: Any,
                    room_cfg: RoomConfig,
                    publisher: SourcePublisher,
                    source_id: str,
                    src_text: str,
                    is_final: bool) -> None:
    """把「我自己说的话」的源文切成房间上行帧发出去（loopback 那条腿永不发）。

    铁律：房间链路的任何异常都不能拖垮翻译 / chatbox / 手腕屏 —— 全程包 try，
    出错只在日志留一行。只有 ``should_publish`` 放行才动发布器。
    """
    if room is None:
        return
    try:
        if not should_publish(source_id, room_cfg.enabled,
                              room_cfg.broadcast_source):
            return
        approval = current()
        if not allowed(approval, 'mine') or (approval is not None and not is_final):
            return
        if approval is not None:
            # 核准段已完整封句；不继承旧串流的 partial 或重复 done 判据。
            publisher.reset()
        for item in publisher.feed(src_text, is_final):
            room.publish(item.utt, item.rev, item.text, item.is_final)
    except Exception as exc:            # noqa: BLE001
        print(f"[room] ⚠️ 上行发布失败（翻译不受影响）：{type(exc).__name__}: {exc}",
              flush=True)
