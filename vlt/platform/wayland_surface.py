"""Wayland output geometry, discovery and layer-surface lifecycle."""
from __future__ import annotations
import ctypes
import os
from dataclasses import dataclass
from typing import Any
from . import wayland_protocol as wl

_LOG = "[desktop:wayland]"


@dataclass(frozen=True)
class OutputInfo:
    """一块输出的全局逻辑几何（坐标与 `wl_output.geometry` 同系）。"""

    x: int
    y: int
    width: int
    height: int
    scale: int
    name: str


def pick_output(pos: tuple[int, int], size: tuple[int, int],
                outputs: list[OutputInfo]) -> OutputInfo | None:
    """挑面板该落在哪块输出。

    1. 有相交：取**相交面积最大**的那块（面板整体落在某块里时就是它）；
    2. 都不相交：取面板中心**最近**的那块（负坐标副屏也按距离算）；
    3. 空列表：None（调用方先跳过这一帧，等 output 事件）。
    """
    if not outputs:
        return None
    x, y = int(pos[0]), int(pos[1])
    w, h = int(size[0]), int(size[1])
    cx, cy = x + w / 2.0, y + h / 2.0

    def area(o: OutputInfo) -> int:
        ix = max(0, min(x + w, o.x + o.width) - max(x, o.x))
        iy = max(0, min(y + h, o.y + o.height) - max(y, o.y))
        return ix * iy

    def center_dist(o: OutputInfo) -> float:
        return (cx - (o.x + o.width / 2.0)) ** 2 + (cy - (o.y + o.height / 2.0)) ** 2

    best = max(outputs, key=lambda o: (area(o), -center_dist(o)))
    if area(best) > 0:
        return best
    return min(outputs, key=center_dist)


def layer_margins(pos: tuple[int, int], output: OutputInfo) -> tuple[int, int]:
    """anchor=top|left 时 layer 面的 (top, left) 边距 = 全局坐标 − 输出原点。

    可以为负（面板有一半在输出外时）——协议允许负 margin，剪裁交给合成器。
    """
    return (int(pos[1]) - output.y, int(pos[0]) - output.x)


def logical_size(physical: tuple[int, int], scale: int) -> tuple[int, int]:
    """物理像素 → 逻辑尺寸（向上取整；`set_size` 收的是逻辑尺寸）。"""
    s = max(1, int(scale))
    return (max(1, -(-int(physical[0]) // s)), max(1, -(-int(physical[1]) // s)))


def buffer_dims(physical: tuple[int, int], scale: int) -> tuple[int, int]:
    """按 scale 补齐到整数倍（协议要求 buffer 尺寸是 buffer_scale 的整数倍）。

    差出来的 1~scale-1 像素在右侧/下侧留空（面板贴 0,0，多出来的是透明边），
    比直接发奇数尺寸撞 `invalid_size` 协议错误安全。
    """
    s = max(1, int(scale))
    return (-(-int(physical[0]) // s) * s, -(-int(physical[1]) // s) * s)


def drag_target(anchor_pos: tuple[int, int],
                anchor_local: tuple[float, float],
                local: tuple[float, float]) -> tuple[int, int]:
    """「本地坐标法」的拖动目标位置 = 锚点位置 + 本地坐标差。

    **为什么本地坐标差能当位移用**：niri 系的 click-grab 在按下时**冻结焦点坐标**
    （niri 的 `ClickGrab` 有意不更新 grab location，见其源码注释），所以
    `wl_pointer.motion` 的 surface-local 坐标始终是「全局指针 − 按下时的面原点」，
    两次相减就是真实位移。相对指针（sway/wlroots 系）则直接给位移增量 ——
    两条源由 `_make_pointer` 里的状态机选择，这里只做数学。

    坐标用 float（协议里是 wl_fixed，1/256 像素），结果四舍五入到整数像素。
    """
    return (int(round(anchor_pos[0] + local[0] - anchor_local[0])),
            int(round(anchor_pos[1] + local[1] - anchor_local[1])))


class _WaylandSurface:
    def _setup(self, lib: Any) -> None:
        self._lib = lib if lib is not None else wl._load_lib()
        if not os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError("没有 WAYLAND_DISPLAY（X11 会话？）")
        self._display = self._lib.wl_display_connect(None)
        if not self._display:
            raise RuntimeError("wl_display_connect 失败")

        # 1) 先拿 registry + 全局对象表（输出在 on_global 里顺手绑定）
        reg_listener = self._registry_listener()
        self._registry = self._req(
            self._display, wl._WL_DISPLAY_GET_REGISTRY, [("n", None)],
            interface=wl._core_iface(self._lib, "wl_registry_interface"), version=1)
        if not self._registry:
            raise RuntimeError("get_registry 失败")
        self._attach_listener(self._registry, reg_listener)
        if self._lib.wl_display_roundtrip(wl._ptr(self._display)) == -1:
            raise RuntimeError("roundtrip 失败（取全局对象列表）")

        for need in ("wl_compositor", "wl_shm", "zwlr_layer_shell_v1"):
            if need not in self._globals:
                raise RuntimeError(f"合成器缺 {need}")
        self._compositor = self._bind_global("wl_compositor", "wl_compositor_interface", cap=6)
        self._shm = self._bind_global("wl_shm", "wl_shm_interface", cap=1)
        self._layer_shell = self._bind_global("zwlr_layer_shell_v1", None,
                                              cap=wl._LAYER_SHELL_VERSION, iface=wl.iface_layer_shell)
        if "zwp_relative_pointer_manager_v1" in self._globals:
            self._rel_manager = self._bind_global("zwp_relative_pointer_manager_v1", None,
                                                  cap=1, iface=wl.iface_rel_manager)
        if "zxdg_output_manager_v1" in self._globals:
            self._xdg_manager = self._bind_global("zxdg_output_manager_v1", None,
                                                  cap=3, iface=wl.iface_xdg_manager)
            # registry 顺序不保证 manager 先到：给已绑上的输出补挂 xdg_output
            for gname, st in self._outputs.items():
                if "xdg_addr" not in st:
                    self._make_xdg_output(int(gname), st["addr"], st)
        if "wl_seat" in self._globals:
            self._seat = self._bind_global("wl_seat", "wl_seat_interface", cap=1)
            self._attach_listener(self._seat, self._seat_listener())
        self._attach_listener(self._shm, self._shm_listener())

        # 2) 再一轮：把输出的 geometry/mode/scale 收齐
        if self._lib.wl_display_roundtrip(wl._ptr(self._display)) == -1:
            raise RuntimeError("roundtrip 失败（收输出几何）")
        if wl._WL_SHM_FORMAT_ARGB8888 not in self._shm_formats:
            raise RuntimeError(f"wl_shm 不支持 ARGB8888（支持：{self._shm_formats}）")

        out = pick_output(self._pos, self._size, self._output_infos())
        if out is not None:
            self._ensure_surface(out, wait_configure=True)

    def _bind_global(self, iface_name: str, core_sym: str | None,
                     cap: int, iface: Any = None) -> Any:
        name, version = self._globals[iface_name]
        ver = max(1, min(int(version), int(cap)))
        target = (ctypes.addressof(iface) if iface is not None
                  else wl._core_iface(self._lib, core_sym or ""))
        return self._req(self._registry, wl._WL_REGISTRY_BIND,
                         [("u", name), ("s", iface_name), ("u", ver), ("n", None)],
                         interface=target, version=ver)

    def _registry_listener(self) -> Any:
        def on_global(_data: Any, _reg: Any, name: int, iface: bytes, version: int) -> None:
            iface_s = iface.decode() if isinstance(iface, bytes) else str(iface)
            if iface_s == "wl_output":
                self._bind_output(name, version)
            else:
                self._globals[iface_s] = (int(name), int(version))

        def on_global_remove(_data: Any, _reg: Any, name: int) -> None:
            st = self._outputs.pop(int(name), None)
            if st is not None:
                xa = st.get("xdg_addr")
                if xa:
                    try:
                        self._lib.wl_proxy_destroy(ctypes.c_void_p(int(xa)))
                    except Exception:  # noqa: BLE001
                        pass
                addr = st["addr"]
                self._output_proxies.pop(addr, None)
                if addr:
                    try:
                        self._lib.wl_proxy_destroy(ctypes.c_void_p(addr))
                    except Exception:  # noqa: BLE001
                        pass

        listener = wl._RegistryListener(
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                ctypes.c_uint32)(on_global),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(on_global_remove))
        self._listeners.append(listener)
        return listener

    def _shm_listener(self) -> Any:
        def on_format(_data: Any, _shm: Any, fmt: int) -> None:
            self._shm_formats.append(int(fmt))

        listener = wl._ShmListener(wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(on_format))
        self._listeners.append(listener)
        return listener

    def _seat_listener(self) -> Any:
        def on_caps(_data: Any, seat: Any, caps: int) -> None:
            if (int(caps) & wl._WL_SEAT_CAPABILITY_POINTER) and self._pointer is None:
                self._make_pointer(seat)

        listener = wl._SeatListener(wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(on_caps))
        self._listeners.append(listener)
        return listener

    def _attach_listener(self, proxy: Any, listener: Any) -> None:
        self._lib.wl_proxy_add_listener(
            wl._ptr(proxy), ctypes.cast(ctypes.pointer(listener), ctypes.c_void_p), None)

    def _bind_output(self, name: int, version: int) -> None:
        # 输出不走 self._globals（on_global 直接调进来），这里单独 bind
        ver = max(1, min(int(version), 2))
        proxy = self._req(self._registry, wl._WL_REGISTRY_BIND,
                          [("u", name), ("s", "wl_output"), ("u", ver), ("n", None)],
                          interface=wl._core_iface(self._lib, "wl_output_interface"),
                          version=ver)
        st = {"x": 0, "y": 0, "width": 0, "height": 0, "scale": 1, "done": False,
              "addr": wl._addr(proxy),
              # xdg-output 的真实逻辑位置/尺寸（优先；wlroots 的 geometry x/y 恒为 0）
              "lx": None, "ly": None, "lw": None, "lh": None, "name": None}
        self._outputs[int(name)] = st
        self._output_proxies[st["addr"]] = int(name)

        def find(addr: int) -> dict | None:
            g = self._output_proxies.get(addr)
            return self._outputs.get(g) if g is not None else None

        def on_geometry(_d: Any, out: Any, x: int, y: int, _pw: int, _ph: int, _sub: int,
                        _mk: bytes, _md: bytes, _tr: int) -> None:
            st_ = find(wl._addr(out))
            if st_ is not None:
                st_["x"], st_["y"] = int(x), int(y)

        def on_mode(_d: Any, out: Any, _flags: int, w: int, h: int) -> None:
            st_ = find(wl._addr(out))
            if st_ is not None:
                st_["width"], st_["height"] = int(w), int(h)

        def on_done(_d: Any, out: Any) -> None:
            st_ = find(wl._addr(out))
            if st_ is not None:
                st_["done"] = True

        def on_scale(_d: Any, out: Any, factor: int) -> None:
            st_ = find(wl._addr(out))
            if st_ is not None:
                st_["scale"] = max(1, int(factor))

        listener = wl._OutputListener(
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_char_p,
                ctypes.c_char_p, ctypes.c_int32)(on_geometry),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32,
                ctypes.c_int32)(on_mode),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p)(on_done),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)(on_scale))
        self._listeners.append(listener)
        self._attach_listener(proxy, listener)

        if self._xdg_manager is not None:
            self._make_xdg_output(int(name), proxy, st)

    def _make_xdg_output(self, gname: int, wl_output: Any, st: dict) -> None:
        """给一块输出挂 xdg-output：**多屏真实位置/尺寸**。

        wlroots 系（sway/Hyprland）的 `wl_output.geometry` x/y 恒为 (0,0)（实测），
        `zxdg_output_v1.logical_position/logical_size` 才是能区分多屏的坐标。
        """
        if self._xdg_manager is None or wl.iface_xdg_output is None:
            return
        ver = min(3, int(self._lib.wl_proxy_get_version(wl._ptr(self._xdg_manager))))
        xo = self._req(self._xdg_manager, wl._XDG_MANAGER_GET,
                       [("n", None), ("o", wl_output)],
                       interface=ctypes.addressof(wl.iface_xdg_output), version=ver)
        if not xo:
            return
        st["xdg_addr"] = wl._addr(xo)

        def on_pos(_d: Any, _o: Any, x: int, y: int) -> None:
            st["lx"], st["ly"] = int(x), int(y)

        def on_size(_d: Any, _o: Any, w: int, h: int) -> None:
            st["lw"], st["lh"] = int(w), int(h)

        def on_name(_d: Any, _o: Any, s: bytes) -> None:
            if s:
                st["name"] = s.decode() if isinstance(s, (bytes, bytearray)) else str(s)

        listener = wl._XdgOutputListener(
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32)(on_pos),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32)(on_size),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p)(lambda *a: None),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(on_name),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(lambda *a: None))
        self._listeners.append(listener)
        self._attach_listener(xo, listener)

    @staticmethod
    def _effective_geom(st: dict) -> tuple[int, int, int, int] | None:
        """输出的**有效**全局逻辑几何：xdg-output 优先，geometry/mode 兜底。

        ⚠️ wlroots 系（sway/Hyprland）的 `wl_output.geometry` x/y **恒为 (0,0)** ——
        只有 xdg-output 的 `logical_position/logical_size` 能区分多屏（实测：双屏
        3280 宽布局里两块都报 (0,0)，拿它挑输出会认错屏）。
        """
        scale = max(1, int(st.get("scale", 1)))
        x = st.get("lx")
        y = st.get("ly")
        w = st.get("lw")
        h = st.get("lh")
        if x is None:
            x = int(st.get("x", 0))
        if y is None:
            y = int(st.get("y", 0))
        if not w:
            w = int(st.get("width", 0)) // scale
        if not h:
            h = int(st.get("height", 0)) // scale
        x, y, w, h = int(x), int(y), int(w), int(h)
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    def _output_infos(self) -> list[OutputInfo]:
        out: list[OutputInfo] = []
        for gname, st in self._outputs.items():
            geom = self._effective_geom(st)
            if geom is None:
                continue
            x, y, w, h = geom
            out.append(OutputInfo(x=x, y=y, width=w, height=h,
                                  scale=max(1, int(st.get("scale", 1))),
                                  name=str(st.get("name") or gname)))
        out.sort(key=lambda o: (o.y, o.x))
        return out

    def _global_for_output(self, output: OutputInfo) -> int:
        for gname, st in self._outputs.items():
            if self._effective_geom(st) == (output.x, output.y,
                                            output.width, output.height):
                return int(gname)
        return 0

    def _ensure_surface(self, output: OutputInfo, wait_configure: bool) -> None:
        """（重）建 layer 面并绑到指定输出。换输出必须重建 —— output 在 role 创建时绑死。"""
        self._destroy_surface()
        self._output = output
        gname = self._global_for_output(output)
        self._output_global = gname
        out_proxy = 0
        st = self._outputs.get(gname)
        if st is not None:
            out_proxy = st["addr"]

        self._surface = self._req(
            self._compositor, wl._WL_COMPOSITOR_CREATE_SURFACE, [("n", None)],
            interface=wl._core_iface(self._lib, "wl_surface_interface"),
            version=int(self._lib.wl_proxy_get_version(wl._ptr(self._compositor))))
        ls_ver = min(wl._LAYER_SHELL_VERSION,
                     int(self._lib.wl_proxy_get_version(wl._ptr(self._layer_shell))))
        self._layer_surface = self._req(
            self._layer_shell, wl._ZLSS_GET_LAYER_SURFACE,
            [("n", None), ("o", self._surface), ("o", out_proxy or None),
             ("u", wl._LAYER_OVERLAY), ("s", b"vlt-subtitle")],
            interface=ctypes.addressof(wl.iface_layer_surface), version=ls_ver)
        if not self._surface or not self._layer_surface:
            raise RuntimeError("建 layer 面失败（create_surface/get_layer_surface 返回空）")

        def on_configure(_d: Any, _ls: Any, serial: int, width: int, height: int) -> None:
            self._configured = True
            try:
                self._req(self._layer_surface, wl._ZLSS_ACK_CONFIGURE, [("u", int(serial))])
            except Exception:  # noqa: BLE001
                pass
            self._dirty = True
            if width and height:
                self._log_once_configured(width, height)
            self._flush()

        def on_closed(_d: Any, _ls: Any) -> None:
            print(f"{_LOG} ⚠️ 合成器关闭了 layer 面（输出被拔 / 被移除）→ 面板停更；"
                  f"重新勾选桌面字幕后可重建", flush=True)
            self._destroy_surface()
            self._mark_dead("layer surface closed")

        listener = wl._LayerSurfaceListener(
            wl._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_uint32)(on_configure),
            wl._cb(ctypes.c_void_p, ctypes.c_void_p)(on_closed))
        self._listeners.append(listener)
        self._attach_listener(self._layer_surface, listener)

        self._configured = False
        self._apply_layer_state(commit=False)
        self._apply_input_region(commit=False)
        self._req(self._surface, wl._WL_SURFACE_COMMIT, [])
        self._flush()
        if wait_configure:
            self._pump_until(lambda: self._configured, wl._CONFIGURE_TIMEOUT_S)

    def _log_once_configured(self, width: int, height: int) -> None:
        if getattr(self, "_configured_logged", False):
            return
        self._configured_logged = True
        print(f"{_LOG} ✅ layer 面已 configure（{width}x{height} 逻辑像素）", flush=True)

    def _destroy_surface(self) -> None:
        if self._layer_surface is not None:
            try:
                self._req(self._layer_surface, wl._ZLSS_DESTROY, [])
                self._lib.wl_proxy_destroy(wl._ptr(self._layer_surface))
            except Exception:  # noqa: BLE001
                pass
            self._layer_surface = None
        if self._surface is not None:
            try:
                self._req(self._surface, wl._WL_SURFACE_DESTROY, [])
                self._lib.wl_proxy_destroy(wl._ptr(self._surface))
            except Exception:  # noqa: BLE001
                pass
            self._surface = None
        self._output_global = 0
        self._configured = False
        self._dirty = True

    def _apply_layer_state(self, *, commit: bool) -> None:
        if self._layer_surface is None or self._output is None:
            return
        lw, lh = logical_size(self._size, self._output.scale)
        top, left = layer_margins(self._pos, self._output)
        self._req(self._layer_surface, wl._ZLSS_SET_SIZE, [("u", lw), ("u", lh)])
        self._req(self._layer_surface, wl._ZLSS_SET_ANCHOR, [("u", wl._ANCHOR_TOP | wl._ANCHOR_LEFT)])
        self._req(self._layer_surface, wl._ZLSS_SET_EXCLUSIVE_ZONE, [("i", -1)])
        self._req(self._layer_surface, wl._ZLSS_SET_MARGIN,
                  [("i", top), ("i", 0), ("i", 0), ("i", left)])
        self._req(self._layer_surface, wl._ZLSS_SET_KEYBOARD_INTERACTIVITY,
                  [("u", wl._KEYBOARD_INTERACTIVITY_NONE)])
        if commit and self._surface is not None:
            self._req(self._surface, wl._WL_SURFACE_COMMIT, [])
            self._flush()

    def _apply_input_region(self, *, commit: bool) -> None:
        """穿透 = 空输入区；解锁拖动 = 恢复默认（NULL = 整个面）。"""
        if self._surface is None:
            return
        if self._draggable or not self._click_through:
            self._req(self._surface, wl._WL_SURFACE_SET_INPUT_REGION, [("o", None)])
        else:
            region = self._req(self._compositor, wl._WL_COMPOSITOR_CREATE_REGION, [("n", None)],
                               interface=wl._core_iface(self._lib, "wl_region_interface"),
                               version=1)
            self._req(self._surface, wl._WL_SURFACE_SET_INPUT_REGION, [("o", region)])
            self._req(region, wl._WL_REGION_DESTROY, [])
            self._lib.wl_proxy_destroy(wl._ptr(region))
        if commit:
            self._req(self._surface, wl._WL_SURFACE_COMMIT, [])
            self._flush()
