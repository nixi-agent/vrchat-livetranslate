"""Wayland event pumping and pointer dragging."""
from __future__ import annotations
import ctypes
import os
import select
import time
from typing import Any, Callable
from . import wayland_protocol as wl
from .wayland_surface import drag_target

_LOG = "[desktop:wayland]"

class _WaylandEvents:
    def _pump_events(self, timeout_ms: int = 0) -> bool:
        """非阻塞泵事件。返回 False = 连接坏了（面板停更，不崩进程）。

        ⚠️ 规范流程：prepare_read → flush → poll → read_events | cancel_read；
        **只要进过 prepare_read 就必须以 read/cancel 收尾**（用 finally 兜）。
        """
        if self._dead or self._display is None:
            return False
        prepared = False
        try:
            if self._lib.wl_display_prepare_read(wl._ptr(self._display)) != 0:
                # 队列里还有没派发的事件：先派发；下一跳再 prepare
                if self._lib.wl_display_dispatch_pending(wl._ptr(self._display)) == -1:
                    return self._mark_dead("dispatch_pending 失败")
                return True
            prepared = True
            self._lib.wl_display_flush(wl._ptr(self._display))
            fd = self._lib.wl_display_get_fd(wl._ptr(self._display))
            r, _, _ = select.select([fd], [], [], max(0, timeout_ms) / 1000.0)
            if r:
                if self._lib.wl_display_read_events(wl._ptr(self._display)) == -1:
                    return self._mark_dead("read_events 失败")
            else:
                # 没数据：按规范先 cancel 收尾，再派发已排队的（dispatch 不再夹在 prepare 中间）
                self._lib.wl_display_cancel_read(wl._ptr(self._display))
            prepared = False
        finally:
            if prepared:
                self._lib.wl_display_cancel_read(wl._ptr(self._display))
        if self._lib.wl_display_dispatch_pending(wl._ptr(self._display)) == -1:
            return self._mark_dead("dispatch_pending 失败")
        return True

    def _pump_until(self, pred: Callable[[], bool], timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if pred():
                return True
            if not self._pump_events(timeout_ms=5):
                return False
            time.sleep(wl._PUMP_SLEEP_S)
        return pred()

    def _flush(self) -> None:
        try:
            self._lib.wl_display_flush(wl._ptr(self._display))
        except Exception:  # noqa: BLE001
            pass

    def _mark_dead(self, why: str) -> bool:
        if not self._dead:
            self._dead = True
            self.available = False
            self._panel = self._panel_approval = None
            self._dirty = False
            print(f"{_LOG} ⚠️ Wayland 连接断开（面板停更）：{why}", flush=True)
        return False

    def _make_pointer(self, seat: Any) -> None:
        pointer = self._req(seat, wl._WL_SEAT_GET_POINTER, [("n", None)],
                            interface=wl._core_iface(self._lib, "wl_pointer_interface"),
                            version=int(self._lib.wl_proxy_get_version(wl._ptr(seat))))
        if not pointer:
            return
        self._pointer = pointer

        def on_motion(_d: Any, _p: Any, _t: int, fx: int, fy: int) -> None:
            """本地坐标法（第二拖动源）：niri 系合成器只发 motion、不发 relative。

            实测（niri 26.04 + 本机 `WAYLAND_DEBUG`）：按下拖动期间 `wl_pointer.motion`
            持续送达（含拖出面板后的坐标），而 `relative_motion` **全程 0 条**。
            niri 的 click-grab 又把焦点坐标冻结在按下那一刻，所以本地坐标差 = 真实位移。
            """
            if not self._drag_active or self._drag_mode == "relative":
                return
            local = (wl._fixed_to_float(fx), wl._fixed_to_float(fy))
            if self._drag_mode == "":
                if time.monotonic() - self._drag_started < wl._RELATIVE_GRACE_S:
                    return
                self._drag_mode = "local"
                self._drag_anchor_pos = self._pos
                self._drag_anchor_local = local
                return
            if self._drag_anchor_local is None:
                return
            self.move(*drag_target(self._drag_anchor_pos, self._drag_anchor_local, local))

        def on_button(_d: Any, _p: Any, _serial: int, _t: int, button: int,
                      state: int) -> None:
            self._buttons_seen += 1
            if button != wl._BTN_LEFT:
                return
            if int(state) == wl._WL_POINTER_STATE_PRESSED:
                if self._draggable:
                    self._drag_active = True
                    self._drag_mode = ""
                    self._drag_anchor_pos = self._pos
                    self._drag_anchor_local = None
                    self._drag_started = time.monotonic()
                    print(f"{_LOG} 已抓住面板（拖动中，放开结束）", flush=True)
            else:
                if self._drag_active:
                    self._drag_active = False
                    self._drag_mode = ""
                    self._drag_anchor_local = None
                    # 跨屏落位：拖动中面板被夹在本输出边缘（中途换面会打断指针 grab）——
                    # 松开后补一次 move()：此时不在拖动中，会走安全的重建 layer 面路径，
                    # 把面换到指针松手时所在的那块屏上（落点即指针位置）。
                    self.move(*self._pos)
                    if self._on_drag_end is not None:
                        try:
                            self._on_drag_end(self._pos[0], self._pos[1])
                        except Exception as exc:  # noqa: BLE001
                            print(f"{_LOG} ⚠️ 拖动回调出错（已忽略）："
                                  f"{type(exc).__name__}: {exc}", flush=True)
                    print(f"{_LOG} 拖动结束，落点 {self._pos}", flush=True)

        listener = wl._PointerListener(
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_int32, ctypes.c_int32)(lambda *a: None),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_void_p)(lambda *a: None),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_int32, ctypes.c_int32)(on_motion),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_uint32, ctypes.c_uint32)(on_button),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_int32)(lambda *a: None),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p)(lambda *a: None))
        self._listeners.append(listener)
        self._attach_listener(pointer, listener)

        # 测试钩子：跳过 relative-pointer 对象，强制走「本地坐标法」
        # （用于在嵌套合成器里模拟 niri 那类不发 relative_motion 的会话）。
        if self._rel_manager is not None and not os.environ.get("VLT_WAYLAND_NO_RELATIVE"):
            rel = self._req(self._rel_manager, wl._REL_MANAGER_GET,
                            [("n", None), ("o", pointer)],
                            interface=ctypes.addressof(wl.iface_rel_pointer), version=1)
            if not rel:
                return
            self._rel_pointer = rel

            def on_rel(_d: Any, _p: Any, _hi: int, _lo: int, dx: int, dy: int,
                       _dxu: int, _dyu: int) -> None:
                if not self._drag_active or self._drag_mode == "local":
                    return                       # 已切本地坐标法：别两个源都算
                self._drag_mode = "relative"
                nx = self._pos[0] + int(round(wl._fixed_to_float(dx)))
                ny = self._pos[1] + int(round(wl._fixed_to_float(dy)))
                self.move(nx, ny)

            listener2 = wl._RelPointerListener(
                wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                    ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
                    ctypes.c_int32)(on_rel))
            self._listeners.append(listener2)
            self._attach_listener(rel, listener2)
