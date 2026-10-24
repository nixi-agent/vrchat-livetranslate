"""GUI queue consumption and scheduled status updates."""
from __future__ import annotations

import queue
import time
from typing import Callable

import tkinter as tk

from .gui_chat_context import ChatCtx
from .i18n import t
from .session.chatgpt_output import allowed, scope


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
                approval = item[5] if len(item) > 5 else None
                if not allowed(approval):
                    continue
                with scope(approval):
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
                if ctx.push_desktop_fn:
                    ctx.push_desktop_fn()
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
        ctx.overlay_note_relay.relay(ov, set_status_fn)
    dov = ctx.desktop_out_fn() if ctx.desktop_out_fn else None
    if dov is not None:
        dov.tick()
        if getattr(dov, 'available', True) is False and ctx.push_desktop_fn:
            ctx.push_desktop_fn()

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
