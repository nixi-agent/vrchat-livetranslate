"""Linux Xlib ABI declarations and lazy library binding."""
import ctypes
from typing import Any

# ---------------------------------------------------------------- Xlib 常量（X11/X.h）
_CW_BACK_PIXEL = 1 << 1
_CW_BORDER_PIXEL = 1 << 3
_CW_OVERRIDE_REDIRECT = 1 << 9
_CW_EVENT_MASK = 1 << 11
_CW_COLORMAP = 1 << 13
_ALLOC_NONE = 0
_INPUT_OUTPUT = 1
_ZPIXMAP = 2
_LSB_FIRST = 0
_TRUE_COLOR = 4
_EXPOSURE_MASK = 1 << 15
_BUTTON_PRESS_MASK = 1 << 2
_BUTTON_RELEASE_MASK = 1 << 3
_BUTTON_MOTION_MASK = 1 << 13
_STRUCTURE_NOTIFY_MASK = 1 << 17
_EVENT_MASK = (_EXPOSURE_MASK | _BUTTON_PRESS_MASK | _BUTTON_RELEASE_MASK
               | _BUTTON_MOTION_MASK | _STRUCTURE_NOTIFY_MASK)
_CW_MASK = _CW_BACK_PIXEL | _CW_BORDER_PIXEL | _CW_COLORMAP | _CW_OVERRIDE_REDIRECT | _CW_EVENT_MASK
_BUTTON1 = 1
_GRAB_EVENT_MASK = _BUTTON_PRESS_MASK | _BUTTON_RELEASE_MASK | _BUTTON_MOTION_MASK
_GRAB_MODE_ASYNC = 1
_GRAB_SUCCESS = 0
_EXPOSE = 12
_BUTTON_PRESS = 4
_BUTTON_RELEASE = 5
_MOTION_NOTIFY = 6


# ================================================================ 纯逻辑（离线可测）


def drag_target(grab_pos: tuple[int, int], grab_root: tuple[int, int],
                cur_root: tuple[int, int]) -> tuple[int, int]:
    """拖动中窗口左上角的目标位置 = 抓取时的窗口位置 + 指针位移。

    X11 与 Wayland 的拖动源不同：这里 `x_root/y_root` 是**绝对**的屏幕坐标，
    按下/移动/松开全程真实（没有 niri 那种「焦点坐标被 click-grab 冻结」的坑），
    所以不需要 Wayland 那套双源状态机，一步算式就够。
    """
    return (int(grab_pos[0]) + int(cur_root[0]) - int(grab_root[0]),
            int(grab_pos[1]) + int(cur_root[1]) - int(grab_root[1]))


# ================================================================ libX11 绑定


class _XVisualInfo(ctypes.Structure):
    """XVisualInfo（Xutil.h；XMatchVisualInfo 的回填结构）。"""

    _fields_ = [
        ("visual", ctypes.c_void_p), ("visualid", ctypes.c_ulong),
        ("screen", ctypes.c_int), ("depth", ctypes.c_int),
        ("c_class", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("colormap_size", ctypes.c_int), ("bits_per_rgb", ctypes.c_int),
    ]


class _XSetWindowAttributes(ctypes.Structure):
    """XSetWindowAttributes（Xlib.h；字段顺序/对齐按 C  ABI）。"""

    _fields_ = [
        ("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
        ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
        ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong),
    ]


class _XImage(ctypes.Structure):
    """XImage 的前半段（Xlib.h）。只用来看元数据 + 拿 data 指针，不改布局。"""

    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
    ]


class _XPointerEvent(ctypes.Structure):
    """XButtonEvent / XMotionEvent 的公共前缀（两者到 `state` 为止布局一致）。

    直接用 `(c_long * 24)` 的原始事件缓冲强转过来读字段，不手写 XEvent 联合体。
    `detail` 对 motion 事件是 `is_hint`，本模块不读。
    """

    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("detail", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


_X11: Any = None
_X11_DEAD = False


def _load_xlib() -> Any:
    """惰性加载 libX11 并声明全部原型；加载失败 → RuntimeError（调用方回落）。

    声明 `argtypes/restype` 不是洁癖：ctypes 默认按 C int 传参会把 64 位指针
    截断，Xlib 这种处处 Pointer 的库上必踩（会直接段错误）。
    """
    global _X11, _X11_DEAD
    if _X11_DEAD:
        raise RuntimeError("libX11 之前加载失败过（不再重试）")
    if _X11 is not None:
        return _X11
    try:
        x11 = ctypes.CDLL("libX11.so.6")
    except OSError as exc:
        _X11_DEAD = True
        raise RuntimeError(f"加载 libX11.so.6 失败：{exc}") from exc
    p, u, i, cp = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_char_p
    for name, res, args in [
        ("XOpenDisplay", p, [cp]),
        ("XCloseDisplay", i, [p]),
        ("XDefaultScreen", i, [p]),
        ("XRootWindow", u, [p, i]),
        ("XMatchVisualInfo", i, [p, i, i, i, ctypes.POINTER(_XVisualInfo)]),
        ("XCreateColormap", u, [p, u, p, i]),
        ("XFreeColormap", i, [p, u]),
        ("XCreateWindow", u, [p, u, i, i, i, i, i, i, i, p, u,
                             ctypes.POINTER(_XSetWindowAttributes)]),
        ("XDestroyWindow", i, [p, u]),
        ("XMapRaised", i, [p, u]),
        ("XMoveWindow", i, [p, u, i, i]),
        ("XResizeWindow", i, [p, u, i, i]),
        ("XRaiseWindow", i, [p, u]),
        ("XCreateGC", p, [p, u, u, p]),
        ("XFreeGC", i, [p, p]),
        ("XCreateImage", ctypes.POINTER(_XImage),
         [p, p, i, i, i, p, i, i, i, i]),
        ("XPutImage", i, [p, u, p, ctypes.POINTER(_XImage), i, i, i, i, i, i]),
        ("XGrabPointer", i, [p, u, i, u, i, i, u, u, u]),
        ("XUngrabPointer", i, [p, u]),
        ("XInternAtom", u, [p, cp, i]),
        ("XGetSelectionOwner", u, [p, u]),
        ("XFree", i, [p]),
        ("XPending", i, [p]),
        ("XNextEvent", i, [p, p]),
        # 下面两个供 linux._guarded 在本连接上使用（它的实现见 linux.py）
        ("XSync", i, [p, i]),
        ("XSetErrorHandler", p, [p]),
    ]:
        fn = getattr(x11, name)
        fn.restype = res
        fn.argtypes = args
    _X11 = x11
    return x11
