"""Wayland ABI, protocol constants and lazy client-library binding."""
import ctypes
from typing import Any, Callable

_NOT_SENT = object()

# ================================================================ 协议常量（对照 XML）

# --- 核心协议（wayland.xml / wayland-client-protocol.h）
_WL_DISPLAY_GET_REGISTRY = 1
_WL_REGISTRY_BIND = 0
_WL_COMPOSITOR_CREATE_SURFACE = 0
_WL_COMPOSITOR_CREATE_REGION = 1
_WL_SURFACE_DESTROY = 0
_WL_SURFACE_ATTACH = 1
_WL_SURFACE_DAMAGE = 2
_WL_SURFACE_SET_INPUT_REGION = 5
_WL_SURFACE_COMMIT = 6
_WL_SURFACE_SET_BUFFER_SCALE = 8
_WL_SURFACE_DAMAGE_BUFFER = 9
_WL_SHM_CREATE_POOL = 0
_WL_SHM_POOL_CREATE_BUFFER = 0
_WL_SHM_POOL_DESTROY = 1
_WL_BUFFER_DESTROY = 0
_WL_REGION_DESTROY = 0
_WL_SEAT_GET_POINTER = 0

_WL_SHM_FORMAT_ARGB8888 = 0

# --- wlr-layer-shell-unstable-v1（对照 wlr-protocols 的 XML）
_ZLSS_GET_LAYER_SURFACE = 0
_ZLSS_SET_SIZE = 0
_ZLSS_SET_ANCHOR = 1
_ZLSS_SET_EXCLUSIVE_ZONE = 2
_ZLSS_SET_MARGIN = 3
_ZLSS_SET_KEYBOARD_INTERACTIVITY = 4
_ZLSS_ACK_CONFIGURE = 6
_ZLSS_DESTROY = 7

_LAYER_OVERLAY = 3
_ANCHOR_TOP = 1
_ANCHOR_LEFT = 4
_KEYBOARD_INTERACTIVITY_NONE = 0

# --- relative-pointer-unstable-v1（⚠️ XML 里 destroy 在前：opcode 0=destroy、1=get）
_REL_MANAGER_GET = 1
_REL_MANAGER_DESTROY = 0
_REL_POINTER_DESTROY = 0

# --- xdg-output-unstable-v1（多屏真实位置；wlroots 的 wl_output.geometry x/y 恒为 0）
# ⚠️ XML 里同样是 destroy 在前：opcode 0=destroy、1=get_xdg_output
_XDG_MANAGER_GET = 1

_BTN_LEFT = 0x110          # linux/input-event-codes.h
_WL_POINTER_STATE_PRESSED = 1
_WL_SEAT_CAPABILITY_POINTER = 1

#: 我们实现/绑定的 layer-shell 版本（XML 是 4；v5 只多了 exclusive_edge，用不到）
_LAYER_SHELL_VERSION = 4
#: 建窗时等 configure 的上限（正常合成器毫秒级）
_CONFIGURE_TIMEOUT_S = 1.0
_PUMP_SLEEP_S = 0.002
#: 拖动源选择的观察窗：按下后给它一点时间等 relative-pointer 事件（sway/wlroots 系第一帧就有）。
#: 等不到就切「本地坐标法」——niri 系只发 wl_pointer.motion（实测：全程 0 条 relative_motion），
#: 而它的 click-grab 冻结了焦点坐标，本地差 = 真实位移（见 `drag_target`）。
_RELATIVE_GRACE_S = 0.08


# ================================================================ ctypes 基础设施


class _WlArgument(ctypes.Union):
    """`union wl_argument`：按类型装参数槽（值/指针共 8 字节）。"""

    _fields_ = [
        ("i", ctypes.c_int32),
        ("u", ctypes.c_uint32),
        ("f", ctypes.c_int32),
        ("s", ctypes.c_char_p),
        ("o", ctypes.c_void_p),
        ("n", ctypes.c_uint32),
        ("a", ctypes.c_void_p),
        ("h", ctypes.c_int32),
    ]


class _WlMessage(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("signature", ctypes.c_char_p),
        ("types", ctypes.POINTER(ctypes.c_void_p)),
    ]


class _WlInterface(ctypes.Structure):
    pass


_WlInterface._fields_ = [
    ("name", ctypes.c_char_p),
    ("version", ctypes.c_int),
    ("method_count", ctypes.c_int),
    ("methods", ctypes.POINTER(_WlMessage)),
    ("event_count", ctypes.c_int),
    ("events", ctypes.POINTER(_WlMessage)),
]

#: 手写的 wl_interface/wl_message 结构必须活到进程结束（GC 掉 = 野指针）
_KEEP: list[Any] = []

iface_layer_shell: _WlInterface | None = None
iface_layer_surface: _WlInterface | None = None
iface_rel_manager: _WlInterface | None = None
iface_rel_pointer: _WlInterface | None = None
iface_xdg_manager: _WlInterface | None = None
iface_xdg_output: _WlInterface | None = None
iface_xdg_popup: _WlInterface | None = None      # 仅 types 表占位，从不实例化


def _mk_msg(name: str, signature: str, types: list[int] | None = None) -> _WlMessage:
    """造一条 `wl_message`；`types` 给接口地址（非接口参数填 0 占位）。"""
    msg = _WlMessage()
    msg.name = name.encode()
    msg.signature = signature.encode()
    if types is None:
        msg.types = None
    else:
        arr = (ctypes.c_void_p * len(types))(*[int(t or 0) for t in types])
        msg.types = ctypes.cast(arr, ctypes.POINTER(ctypes.c_void_p))
        _KEEP.append(arr)
    return msg


def _empty_iface(name: str) -> _WlInterface:
    iface = _WlInterface()
    iface.name = name.encode()
    iface.version = 1
    iface.method_count = 0
    iface.methods = None
    iface.event_count = 0
    iface.events = None
    return iface


def _core_iface(lib: Any, name: str) -> int:
    """取核心协议接口 data 符号的**地址**（in_dll 拿到的就是符号所在内存）。"""
    return ctypes.addressof(ctypes.c_char.in_dll(lib, name))


def _build_layer_shell(lib: Any) -> None:
    global iface_layer_shell, iface_layer_surface, iface_xdg_popup
    surface_iface = _core_iface(lib, "wl_surface_interface")
    output_iface = _core_iface(lib, "wl_output_interface")

    iface_xdg_popup = _empty_iface("xdg_popup")
    _KEEP.append(iface_xdg_popup)

    iface_layer_surface = _WlInterface()
    iface_layer_surface.name = b"zwlr_layer_surface_v1"
    iface_layer_surface.version = _LAYER_SHELL_VERSION
    ls_methods = (_WlMessage * 9)(
        _mk_msg("set_size", "uu", [0, 0]),
        _mk_msg("set_anchor", "u", [0]),
        _mk_msg("set_exclusive_zone", "i", [0]),
        _mk_msg("set_margin", "iiii", [0, 0, 0, 0]),
        _mk_msg("set_keyboard_interactivity", "u", [0]),
        _mk_msg("get_popup", "o", [ctypes.addressof(iface_xdg_popup)]),
        _mk_msg("ack_configure", "u", [0]),
        _mk_msg("destroy", "", []),
        _mk_msg("set_layer", "u", [0]),
    )
    ls_events = (_WlMessage * 2)(
        _mk_msg("configure", "uuu", [0, 0, 0]),
        _mk_msg("closed", "", []),
    )
    iface_layer_surface.method_count = 9
    iface_layer_surface.methods = ls_methods
    iface_layer_surface.event_count = 2
    iface_layer_surface.events = ls_events

    iface_layer_shell = _WlInterface()
    iface_layer_shell.name = b"zwlr_layer_shell_v1"
    iface_layer_shell.version = _LAYER_SHELL_VERSION
    zl_methods = (_WlMessage * 2)(
        # (new_id layer_surface, surface, output?, uint layer, string namespace)
        _mk_msg("get_layer_surface", "no?ous",
                [ctypes.addressof(iface_layer_surface), surface_iface, output_iface, 0, 0]),
        _mk_msg("destroy", "", []),
    )
    iface_layer_shell.method_count = 2
    iface_layer_shell.methods = zl_methods
    iface_layer_shell.event_count = 0
    iface_layer_shell.events = None

    _KEEP.extend([ls_methods, ls_events, zl_methods, iface_layer_surface, iface_layer_shell])


def _build_relative_pointer(lib: Any) -> None:
    global iface_rel_manager, iface_rel_pointer
    pointer_iface = _core_iface(lib, "wl_pointer_interface")

    iface_rel_pointer = _WlInterface()
    iface_rel_pointer.name = b"zwp_relative_pointer_v1"
    iface_rel_pointer.version = 1
    rp_methods = (_WlMessage * 1)(_mk_msg("destroy", "", []))
    rp_events = (_WlMessage * 1)(
        _mk_msg("relative_motion", "uuffff", [0, 0, 0, 0, 0, 0]))
    iface_rel_pointer.method_count = 1
    iface_rel_pointer.methods = rp_methods
    iface_rel_pointer.event_count = 1
    iface_rel_pointer.events = rp_events

    iface_rel_manager = _WlInterface()
    iface_rel_manager.name = b"zwp_relative_pointer_manager_v1"
    iface_rel_manager.version = 1
    rm_methods = (_WlMessage * 2)(
        _mk_msg("destroy", "", []),                                        # opcode 0
        _mk_msg("get_relative_pointer", "no",                               # opcode 1
                [ctypes.addressof(iface_rel_pointer), pointer_iface]),
    )
    iface_rel_manager.method_count = 2
    iface_rel_manager.methods = rm_methods
    iface_rel_manager.event_count = 0
    iface_rel_manager.events = None

    _KEEP.extend([rp_methods, rp_events, rm_methods, iface_rel_pointer, iface_rel_manager])


def _build_xdg_output(lib: Any) -> None:
    """xdg-output：多屏的**真实**逻辑位置/尺寸（wlroots 的 wl_output.geometry x/y 恒为 0）。"""
    global iface_xdg_manager, iface_xdg_output
    output_iface = _core_iface(lib, "wl_output_interface")

    iface_xdg_output = _WlInterface()
    iface_xdg_output.name = b"zxdg_output_v1"
    iface_xdg_output.version = 3
    xo_methods = (_WlMessage * 1)(_mk_msg("destroy", "", []))
    xo_events = (_WlMessage * 5)(
        _mk_msg("logical_position", "ii", [0, 0]),
        _mk_msg("logical_size", "ii", [0, 0]),
        _mk_msg("done", "", []),
        _mk_msg("name", "s", [0]),
        _mk_msg("description", "s", [0]),
    )
    iface_xdg_output.method_count = 1
    iface_xdg_output.methods = xo_methods
    iface_xdg_output.event_count = 5
    iface_xdg_output.events = xo_events

    iface_xdg_manager = _WlInterface()
    iface_xdg_manager.name = b"zxdg_output_manager_v1"
    iface_xdg_manager.version = 3
    xm_methods = (_WlMessage * 2)(
        _mk_msg("destroy", "", []),                                  # opcode 0
        _mk_msg("get_xdg_output", "no",                              # opcode 1
                [ctypes.addressof(iface_xdg_output), output_iface]),
    )
    iface_xdg_manager.method_count = 2
    iface_xdg_manager.methods = xm_methods
    iface_xdg_manager.event_count = 0
    iface_xdg_manager.events = None

    _KEEP.extend([xo_methods, xo_events, xm_methods, iface_xdg_output, iface_xdg_manager])


def read_interface(iface: _WlInterface) -> dict:
    """测试用：把 `wl_interface` 结构读回成可断言的字典。"""
    def msgs(ptr: Any, count: int) -> list[tuple[str, str]]:
        out = []
        for i in range(count):
            m = ptr[i]
            out.append((m.name.decode(), m.signature.decode()))
        return out

    return {"name": iface.name.decode(), "version": iface.version,
            "methods": msgs(iface.methods, iface.method_count) if iface.method_count else [],
            "events": msgs(iface.events, iface.event_count) if iface.event_count else []}


def _load_lib() -> Any:
    lib = ctypes.CDLL("libwayland-client.so.0")
    lib.wl_display_connect.argtypes = [ctypes.c_char_p]
    lib.wl_display_connect.restype = ctypes.c_void_p
    lib.wl_display_disconnect.argtypes = [ctypes.c_void_p]
    lib.wl_display_disconnect.restype = None
    lib.wl_display_get_fd.argtypes = [ctypes.c_void_p]
    lib.wl_display_get_fd.restype = ctypes.c_int
    lib.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
    lib.wl_display_roundtrip.restype = ctypes.c_int
    lib.wl_display_prepare_read.argtypes = [ctypes.c_void_p]
    lib.wl_display_prepare_read.restype = ctypes.c_int
    lib.wl_display_read_events.argtypes = [ctypes.c_void_p]
    lib.wl_display_read_events.restype = ctypes.c_int
    lib.wl_display_cancel_read.argtypes = [ctypes.c_void_p]
    lib.wl_display_cancel_read.restype = ctypes.c_int
    lib.wl_display_dispatch_pending.argtypes = [ctypes.c_void_p]
    lib.wl_display_dispatch_pending.restype = ctypes.c_int
    lib.wl_display_flush.argtypes = [ctypes.c_void_p]
    lib.wl_display_flush.restype = ctypes.c_int
    lib.wl_proxy_marshal_array_flags.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.POINTER(_WlArgument)]
    lib.wl_proxy_marshal_array_flags.restype = ctypes.c_void_p
    lib.wl_proxy_add_listener.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.wl_proxy_add_listener.restype = ctypes.c_int
    lib.wl_proxy_get_version.argtypes = [ctypes.c_void_p]
    lib.wl_proxy_get_version.restype = ctypes.c_uint32
    lib.wl_proxy_destroy.argtypes = [ctypes.c_void_p]
    lib.wl_proxy_destroy.restype = None
    _build_layer_shell(lib)
    _build_relative_pointer(lib)
    _build_xdg_output(lib)
    return lib


def _addr(proxy: Any) -> int:
    return int(proxy) if proxy else 0


def _ptr(proxy: Any) -> ctypes.c_void_p:
    return ctypes.c_void_p(int(proxy)) if proxy else ctypes.c_void_p(None)


def _fixed_to_float(v: int) -> float:
    return ctypes.c_int32(v).value / 256.0


def _cb(*argtypes: Any) -> Any:
    return ctypes.CFUNCTYPE(None, *argtypes)


def marshal_request(lib: Any, proxy: Any, opcode: int, args: list[tuple[str, Any]],
                    interface: int | None = None, version: int = 0,
                    flags: int = 0, before_send: Callable[[], bool] | None = None) -> Any:
    """数组式 marshalling 入口（窗口与测试共用）。

    ⚠️ `args` 是**与 XML 一字不差的槽位表**（含 new_id 空槽，值为 `("n", None)`）。
    少一个槽 → libwayland 会顺移读取后面的参数（实测：`create_pool` 少写 new_id 槽，
    把 size 当 fd 去 dup，报 `dup failed: Bad file descriptor`）。
    """
    arr = (_WlArgument * max(1, len(args)))()
    for i, (kind, val) in enumerate(args):
        if kind == "s":
            arr[i].s = val if (val is None or isinstance(val, bytes)) \
                else str(val).encode()
        elif kind == "o" or kind == "n":
            arr[i].o = ctypes.c_void_p(int(val)) if val else None
        elif kind == "h":
            arr[i].h = ctypes.c_int32(int(val)).value
        elif kind == "f":
            arr[i].f = ctypes.c_int32(int(val)).value
        elif kind == "u":
            arr[i].u = ctypes.c_uint32(int(val) & 0xFFFFFFFF).value
        elif kind == "i":
            arr[i].i = ctypes.c_int32(int(val)).value
        else:
            raise TypeError(f"未知参数类型 {kind!r}")
    native_args = (
        _ptr(proxy), int(opcode),
        ctypes.c_void_p(int(interface)) if interface else None,
        int(version), int(flags), arr)
    if before_send is not None and not before_send():
        return _NOT_SENT
    return lib.wl_proxy_marshal_array_flags(*native_args)


# ---- 监听器结构（回调对象由 `_listen()` 统一持引用 —— GC 掉就是野指针）


class _RegistryListener(ctypes.Structure):
    _fields_ = [
        ("global_", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                        ctypes.c_char_p, ctypes.c_uint32)),
        ("global_remove", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)),
    ]


class _ShmListener(ctypes.Structure):
    _fields_ = [("format", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32))]


class _BufferListener(ctypes.Structure):
    _fields_ = [("release", _cb(ctypes.c_void_p, ctypes.c_void_p))]


class _OutputListener(ctypes.Structure):
    _fields_ = [
        ("geometry", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                         ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
                         ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32)),
        ("mode", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                     ctypes.c_int32, ctypes.c_int32)),
        ("done", _cb(ctypes.c_void_p, ctypes.c_void_p)),
        ("scale", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)),
    ]


class _SeatListener(ctypes.Structure):
    _fields_ = [("capabilities", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32))]


class _PointerListener(ctypes.Structure):
    _fields_ = [
        ("enter", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                      ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32)),
        ("leave", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)),
        ("motion", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                       ctypes.c_int32, ctypes.c_int32)),
        ("button", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_uint32, ctypes.c_uint32)),
        ("axis", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                     ctypes.c_int32)),
        ("frame", _cb(ctypes.c_void_p, ctypes.c_void_p)),
    ]


class _RelPointerListener(ctypes.Structure):
    _fields_ = [
        ("relative_motion", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32,
                                ctypes.c_int32, ctypes.c_int32)),
    ]


class _XdgOutputListener(ctypes.Structure):
    _fields_ = [
        ("logical_position", _cb(ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_int32, ctypes.c_int32)),
        ("logical_size", _cb(ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_int32, ctypes.c_int32)),
        ("done", _cb(ctypes.c_void_p, ctypes.c_void_p)),
        ("name", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)),
        ("description", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)),
    ]


class _LayerSurfaceListener(ctypes.Structure):
    _fields_ = [
        ("configure", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.c_uint32, ctypes.c_uint32)),
        ("closed", _cb(ctypes.c_void_p, ctypes.c_void_p)),
    ]
