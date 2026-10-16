"""Wayland 原生桌面叠加窗（Linux 独占）：ctypes 直调 libwayland-client 的 layer-shell 窗。

## 为什么需要它（Tk 那条腿在 Wayland 上为什么不行）

桌面字幕窗原先用 Tk（`vlt/output/desktop_overlay.py`）。Windows 上 Tk 的
`-transparentcolor` 能把整窗底色抠成透明；但 **X11 的 Tk 根本没有这个属性**，
而 Wayland 会话里 Tk 走 XWayland，经 `xwayland-satellite` 时更糟：

* 合成缓冲固定 `Xrgb8888`（**没有 alpha 通道**，源码 `src/server/decoration.rs`）；
* `_NET_WM_WINDOW_OPACITY` 没人处理（全仓库无此属性）；
* X Shape 蒙版也没有处理 → 鼠标穿透/圆角抠形全部无效。

于是「色键透明」与「整窗透明度」双双失效 —— 字幕在 niri 上就是一块不透明矩形，
还会被按平铺窗口管理。要让像素**真的**透过去，唯一的路是让字幕窗自己成为原生
Wayland 客户端，用 `wl_shm`（ARGB8888，预乘 alpha）出图。

## 为什么是 layer-shell

普通 `xdg_toplevel` 由合成器决定位置与堆叠，Wayland 协议层不允许客户端自己定位。
`zwlr_layer_shell_v1`（overlay 层）一次给齐三件事：

1. **置顶**：overlay 层在所有窗口之上（含全屏窗口）；
2. **定位**：`anchor=top|left` + `set_margin(top, 0, 0, left)` 把面放到输出上的任意坐标；
3. **输入语义**：`wl_surface.set_input_region(空)` = 协议级鼠标穿透，不赌合成器是否实现 EWMH。

## 覆盖范围（运行时探测，不写白名单）

niri / sway / Hyprland / KWin 6.x / labwc / Cage / gamescope 等实现 layer-shell 的合成器
走本后端；GNOME(Mutter) / Weston 这类没实现的由 `vlt/platform/linux.py` 探测后自动
回落 Tk 路径（并打日志说明）。

## 协议绑定为什么是手写的

libwayland-client 只导出**通用**入口（`wl_proxy_marshal_array_flags` 等）和核心协议的
**接口数据**（`wl_surface_interface` 一类是导出的 data 符号）；请求帮助函数
（`wl_surface_attach` 一类）都是头文件里的 `static inline`，编译进各个客户端，不在 .so 里。
非核心协议（layer-shell / relative-pointer / xdg-output）更是没有任何现成绑定。纯 ctypes
下唯一的办法就是按 XML 手写 `wl_interface` / `wl_message` 结构 —— 下面的
`_build_layer_shell()` / `_build_relative_pointer()` / `_build_xdg_output()` 与
wayland-protocols / wlr-protocols 的 XML 一一对应（改的时候对着 XML 核签名；
`tests/test_wayland_window.py` 有签名断言守着）。

⚠️ **多屏位置必须读 xdg-output**：wlroots 系合成器（sway 等）的 `wl_output.geometry`
x/y **恒为 (0,0)**（实测：双屏 3280 宽布局里两块都报 (0,0)）—— 拿它挑输出/算边距会认错屏；
`zxdg_output_v1.logical_position/logical_size` 才是真实位置。niri/smithay 两者都对，
但统一走 xdg-output、geometry 只作兜底。

⚠️ **数组式 marshalling（`wl_proxy_marshal_array_flags`）的槽位数必须与 XML 一字不差**。
踩过：`wl_shm.create_pool` 签名是 `(new_id, fd, size)`，少写 1 个 new_id 槽 → libwayland
把 `size` 当 fd 去 `dup`，报 `dup failed: Bad file descriptor`（strace 看到 `fcntl(4096,…)`）。
new_id 槽的值填空（NULL），对象归 libwayland 创建。

## 线程约定

与 Tk 一致：**只有 GUI 主线程**碰这个对象（`DesktopOverlay.tick()` 在 Tk 主循环里），
事件泵用的是非阻塞的 `wl_display_prepare_read/read_events/cancel_read`，绝不阻塞界面。
"""
from __future__ import annotations

import ctypes
import mmap
import os
from typing import Any, Callable
from PIL import Image
from .overlay_pixels import clamp01, premultiplied_bgra
from ..session.chatgpt_output import allowed, current
from . import wayland_protocol as wl
from .wayland_surface import (  # noqa: F401 — preserve public imports
    _WaylandSurface, OutputInfo, pick_output, layer_margins, logical_size, buffer_dims, drag_target,
)
from .wayland_events import _WaylandEvents

_LOG = "[desktop:wayland]"


def __getattr__(name):
    return getattr(wl, name)


class LayerShellWindow(_WaylandSurface, _WaylandEvents):
    """一块 layer-shell overlay 面的最小封装（对共享模块遵守
    `vlt/platform/base.py:DesktopWindow` 的窗口契约）。

    生命周期：`__init__` 连显示 + 绑全局 + 建面（等 configure，有界）；
    之后 `tick()` 泵事件、`set_panel()` 出新图、`move()`/`set_*()` 改状态；`close()` 幂等。
    """

    #: 建不起来时保持 False（调用方据此回落 Tk）
    available = False

    def __init__(self, size: tuple[int, int], alpha: float = 1.0,
                 click_through: bool = True,
                 on_drag_end: Callable[[int, int], None] | None = None,
                 _lib: Any = None) -> None:
        self._size = (max(1, int(size[0])), max(1, int(size[1])))
        self._alpha = clamp01(alpha)
        self._click_through = bool(click_through)
        self._draggable = False
        self._on_drag_end = on_drag_end
        self._pos = (0, 0)
        self._panel: Image.Image | None = None
        self._panel_approval: Any = None
        self._surface_approval: Any = None
        self._dirty = False
        self._dead = False

        self._lib: Any = None
        self._display: Any = None
        self._registry: Any = None
        self._compositor: Any = None
        self._shm: Any = None
        self._layer_shell: Any = None
        self._rel_manager: Any = None
        self._xdg_manager: Any = None
        self._seat: Any = None
        self._pointer: Any = None
        self._rel_pointer: Any = None
        self._surface: Any = None
        self._layer_surface: Any = None

        self._globals: dict[str, tuple[int, int]] = {}   # 接口名 → (global 名, 版本)
        self._shm_formats: list[int] = []
        self._outputs: dict[int, dict] = {}              # global 名 → 状态
        self._output_proxies: dict[int, int] = {}        # 代理地址 → global 名
        self._output: OutputInfo | None = None
        self._output_global = 0
        self._configured = False

        self._shm_fd: int | None = None
        self._shm_map: Any = None
        self._pool: Any = None
        self._buffers: list[dict] = []                   # {proxy, addr, busy}
        self._next_buf = 0
        self._stride = 0
        self._buf_dims = (0, 0)

        self._drag_active = False
        # 拖动源状态机："" = 观察中（等 relative-pointer）/ "relative" = 用相对增量 /
        # "local" = 用 wl_pointer.motion 的本地坐标差（niri 系没有 relative 事件）
        self._drag_mode = ""
        self._drag_anchor_pos: tuple[int, int] = (0, 0)
        self._drag_anchor_local: tuple[float, float] | None = None
        self._drag_started = 0.0
        self._buttons_seen = 0        # 收到的指针按键总数（穿透验证 / 排障用）

        self._listeners: list[Any] = []                  # listener 结构 + 回调的引用池
        self._buffer_listeners: list[Any] = []           # shm 缓冲的 release 监听（重建时整批换掉）

        try:
            self._setup(_lib)
            self.available = True
        except Exception as exc:  # noqa: BLE001 — 建不起来就保持 available=False
            print(f"{_LOG} ⚠️ 原生 Wayland 窗建不起来（回落 Tk）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            self._teardown()

    def _rebuild_buffers(self) -> None:
        """按当前尺寸/scale 重造 shm 池与 2 块轮换缓冲。"""
        if self._shm is None:
            raise RuntimeError("wl_shm 还没绑上")
        scale = self._output.scale if self._output is not None else 1
        bw, bh = buffer_dims(self._size, scale)
        if (bw, bh) == self._buf_dims and self._buffers:
            return
        self._release_buffers()
        stride = bw * 4
        total = stride * bh * 2
        self._shm_fd = fd = os.memfd_create("vlt-overlay", 0)
        try:
            os.ftruncate(fd, total)
            self._shm_map = mmap.mmap(fd, total)
            self._pool = pool = self._req(self._shm, wl._WL_SHM_CREATE_POOL,
                             [("n", None), ("h", fd), ("i", total)],
                             interface=wl._core_iface(self._lib, "wl_shm_pool_interface"),
                             version=1)
            if not pool:
                raise RuntimeError("create_pool 失败")
            for i in range(2):
                buf = self._req(pool, wl._WL_SHM_POOL_CREATE_BUFFER,
                                [("n", None), ("i", i * stride * bh), ("i", bw), ("i", bh),
                                 ("i", stride), ("u", wl._WL_SHM_FORMAT_ARGB8888)],
                                interface=wl._core_iface(self._lib, "wl_buffer_interface"),
                                version=1)
                if not buf:
                    raise RuntimeError("create_buffer 失败")
                st = {"proxy": buf, "addr": wl._addr(buf), "busy": False}
                self._buffers.append(st)
                listener = wl._BufferListener(
                    wl._cb(ctypes.c_void_p, ctypes.c_void_p)(
                        (lambda _i: (lambda _d, _b: self._on_buffer_release(_i)))(i)))
                self._buffer_listeners.append(listener)
                self._attach_listener(buf, listener)
        except BaseException:
            self._release_buffers()
            raise
        self._stride = stride
        self._buf_dims = (bw, bh)
        self._next_buf = 0
        self._dirty = True

    def _release_buffers(self) -> None:
        for b in self._buffers:
            try:
                self._req(b["proxy"], wl._WL_BUFFER_DESTROY, [])
            except Exception:  # noqa: BLE001
                pass
            try:
                self._lib.wl_proxy_destroy(wl._ptr(b["proxy"]))
            except Exception:  # noqa: BLE001
                pass
        self._buffers = []
        self._buffer_listeners.clear()          # 旧缓冲的监听引用整批释放，别随重建越积越多
        if self._pool is not None:
            try:
                self._req(self._pool, wl._WL_SHM_POOL_DESTROY, [])
            except Exception:  # noqa: BLE001
                pass
            try:
                self._lib.wl_proxy_destroy(wl._ptr(self._pool))
            except Exception:  # noqa: BLE001
                pass
            self._pool = None
        if self._shm_map is not None:
            try:
                self._shm_map.close()
            except Exception:  # noqa: BLE001
                pass
            self._shm_map = None
        if self._shm_fd is not None:
            try:
                os.close(self._shm_fd)
            except Exception:  # noqa: BLE001
                pass
            self._shm_fd = None
        self._buf_dims = (0, 0)

    def _on_buffer_release(self, idx: int) -> None:
        if 0 <= idx < len(self._buffers):
            self._buffers[idx]["busy"] = False

    def _draw(self) -> None:
        if self._dead or not self._configured or self._surface is None or self._panel is None:
            return
        panel, approval, alpha = self._panel, self._panel_approval, self._alpha
        if not allowed(approval):
            if alpha == 0:
                self._req(self._surface, wl._WL_SURFACE_ATTACH, [("o", None)])
                self._req(self._surface, wl._WL_SURFACE_COMMIT, [])
                self._flush()
            return
        if not self._buffers:
            try:
                self._rebuild_buffers()
            except Exception as exc:  # noqa: BLE001
                print(f"{_LOG} ⚠️ 建 shm 缓冲失败：{type(exc).__name__}: {exc}", flush=True)
                return
        idx = self._next_buf
        if self._buffers[idx]["busy"]:
            idx = 1 - idx
            if self._buffers[idx]["busy"]:
                return                        # 两块都被合成器占着：下一跳再试，内容不会丢
        bw, bh = self._buf_dims
        buffers, shared, surface = self._buffers, self._shm_map, self._surface
        def valid():
            return (not self._dead and self._panel is panel and self._buffers is buffers
                    and self._shm_map is shared and self._surface is surface
                    and self._alpha == alpha and allowed(approval))
        img = panel
        if img.size != (bw, bh):
            padded = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            padded.paste(img, (0, 0))
            img = padded
        data = premultiplied_bgra(img, alpha)
        if not valid():
            return
        offset = self._stride * bh * idx
        self._shm_map.seek(offset)
        self._shm_map.write(data)
        if not valid():
            return
        buf = self._buffers[idx]["proxy"]
        ver = int(self._lib.wl_proxy_get_version(wl._ptr(self._surface)))
        if not valid():
            return
        self._surface_approval = approval
        self._req(surface, wl._WL_SURFACE_ATTACH, [("o", buf)])
        if not valid():
            return
        if ver >= 4:
            self._req(self._surface, wl._WL_SURFACE_DAMAGE_BUFFER,
                      [("i", 0), ("i", 0), ("i", bw), ("i", bh)])
        else:  # pragma: no cover —— 老合成器兜底
            self._req(self._surface, wl._WL_SURFACE_DAMAGE,
                      [("i", 0), ("i", 0), ("i", bw), ("i", bh)])
        if self._output is not None and self._output.scale > 1 and ver >= 3:
            self._req(self._surface, wl._WL_SURFACE_SET_BUFFER_SCALE,
                      [("i", self._output.scale)])
        committed = self._req(surface, wl._WL_SURFACE_COMMIT, [])
        if committed is wl._NOT_SENT or self._surface_approval is not approval:
            return
        buffers[idx]["busy"] = True
        if self._buffers is not buffers or self._surface is not surface:
            return
        self._next_buf = 1 - idx
        self._dirty = False
        self._flush()

    def set_panel(self, image: Image.Image, alpha: float | None = None) -> None:
        """贴一帧面板；`alpha=None` 保持当前整层乘子（窗口契约，见 base.DesktopWindow）。"""
        approval = current()
        if self._dead or not allowed(approval):
            return
        panel = image.copy()
        if self._dead or not allowed(approval):
            return
        self._panel, self._panel_approval = panel, approval
        if alpha is not None:
            self._alpha = clamp01(alpha)
        self._dirty = True
        self._draw()

    def move(self, x: int, y: int) -> None:
        """移到全局坐标；自动挑输出。拖动过程中**不换面**（换面会打断指针 grab）。"""
        if self._dead:
            return
        self._pos = (int(x), int(y))
        outs = self._output_infos()
        if not outs:
            return
        out = pick_output(self._pos, self._size, outs)
        if out is None:
            return
        gname = self._global_for_output(out)
        if not self._drag_active and (self._surface is None or self._output_global != gname):
            try:
                self._ensure_surface(out, wait_configure=False)
            except Exception as exc:  # noqa: BLE001 — 建面失败也停更而不是崩
                self._mark_dead(f"建面失败 {type(exc).__name__}: {exc}")
                return
        elif self._output is None:
            self._output = out
        self._apply_layer_state(commit=True)

    def set_size(self, size: tuple[int, int]) -> None:
        size = (max(1, int(size[0])), max(1, int(size[1])))
        if size == self._size:
            return
        self._size = size
        try:
            self._rebuild_buffers()
        except Exception as exc:  # noqa: BLE001
            self._mark_dead(f"改尺寸失败 {type(exc).__name__}: {exc}")
            return
        self._apply_layer_state(commit=True)
        self._dirty = True

    def set_alpha(self, alpha: float) -> None:
        a = clamp01(alpha)
        if abs(a - self._alpha) < 1e-6:
            return
        self._alpha = a
        self._dirty = True
        self._draw()

    def set_click_through(self, on: bool) -> None:
        on = bool(on)
        if on == self._click_through:
            return
        self._click_through = on
        self._apply_input_region(commit=True)

    def set_draggable(self, on: bool) -> None:
        on = bool(on)
        if on == self._draggable:
            return
        self._draggable = on
        if not on:
            self._drag_active = False
            self._drag_mode = ""
            self._drag_anchor_local = None
        self._apply_input_region(commit=True)

    def tick(self) -> None:
        if self._dead:
            return
        if not self._pump_events():
            return
        if self._dirty and self._configured:
            self._draw()

    @property
    def position(self) -> tuple[int, int]:
        return self._pos

    def close(self) -> None:
        self._panel = self._panel_approval = self._surface_approval = None
        self._dirty = False
        self._dead = True
        if self._display is None and not self._buffers and self._surface is None:
            self.available = False
            return
        self._teardown()
        self.available = False

    def _req(self, proxy: Any, opcode: int, args: list[tuple[str, Any]],
             interface: int | None = None, version: int = 0,
             flags: int = 0) -> Any:
        """转调模块级 `marshal_request`（文档见那里）。"""
        guard, detach = None, False
        if proxy == self._surface and opcode in (wl._WL_SURFACE_ATTACH, wl._WL_SURFACE_COMMIT):
            approval = self._surface_approval
            if opcode == wl._WL_SURFACE_ATTACH and args == [("o", None)]:
                detach, approval = True, None
            elif opcode == wl._WL_SURFACE_COMMIT and not allowed(approval):
                # A config commit must not publish an abandoned pending attachment.
                if self._req(proxy, wl._WL_SURFACE_ATTACH, [("o", None)]) is wl._NOT_SENT:
                    return wl._NOT_SENT
                approval = None
            guard = lambda: not self._dead and proxy == self._surface and allowed(approval)
        try:
            result = wl.marshal_request(self._lib, proxy, opcode, args, interface, version, flags,
                                        before_send=guard)
        except Exception:
            if guard is not None and opcode == wl._WL_SURFACE_ATTACH and not detach:
                self._surface_approval = lambda: False  # Unknown attachment cannot authorize pixels.
            raise
        if detach and result is not wl._NOT_SENT and proxy == self._surface:
            self._surface_approval = None
        return result

    def _teardown(self) -> None:
        if self._lib is None:
            self._dead = True
            return
        try:
            self._drag_active = False
            self._release_buffers()
            self._destroy_surface()
            for proxy in (self._pointer, self._rel_pointer, self._rel_manager,
                          self._xdg_manager, self._seat, self._layer_shell, self._shm,
                          self._compositor, self._registry):
                if proxy:
                    try:
                        self._lib.wl_proxy_destroy(wl._ptr(proxy))
                    except Exception:  # noqa: BLE001
                        pass
            self._pointer = self._rel_pointer = self._rel_manager = self._seat = None
            self._xdg_manager = None
            self._layer_shell = self._shm = self._compositor = self._registry = None
            for st in self._outputs.values():
                xa = st.get("xdg_addr")
                if xa:
                    try:
                        self._lib.wl_proxy_destroy(ctypes.c_void_p(int(xa)))
                    except Exception:  # noqa: BLE001
                        pass
            for addr in self._output_proxies:
                try:
                    self._lib.wl_proxy_destroy(ctypes.c_void_p(int(addr)))
                except Exception:  # noqa: BLE001
                    pass
            self._output_proxies.clear()
            self._outputs.clear()
            if self._display:
                try:
                    self._lib.wl_display_flush(wl._ptr(self._display))
                except Exception:  # noqa: BLE001
                    pass
                self._lib.wl_display_disconnect(wl._ptr(self._display))
        except Exception as exc:  # noqa: BLE001
            print(f"{_LOG} ⚠️ 关闭原生 Wayland 窗时报错（已忽略）："
                  f"{type(exc).__name__}: {exc}", flush=True)
        finally:
            self._display = None
            self._listeners.clear()
            self._dead = True
            self.available = False
