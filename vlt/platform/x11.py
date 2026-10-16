"""X11 原生桌面叠加窗（Linux 独占）：ctypes 直调 libX11 的 32 位 ARGB 窗。

## 为什么需要它（Tk 那条腿在 X11 上为什么不行）

桌面字幕的 Tk 回落路径靠 `-transparentcolor` 色键抠透明 —— 那是 **Windows 专属**
属性（见 `vlt/output/desktop_overlay.py:_build_window`），X11 的 Tk 根本没这个能力，
Tk 窗在 X11 上就是一块**不透明矩形**。要让像素真的透过去，唯一的路是自建一个
**32 位 visual（ARGB）覆盖窗** + `XPutImage` 出预乘像素，由合成器（picom 等）
做真正的 alpha 混合 —— 这就是本模块。

## 与 Wayland 后端（`vlt/platform/wayland.py`）的关系

两条腿遵守同一份窗口契约（`vlt/platform/base.py:DesktopWindow`），出图用**同一份**
预乘 BGRA 像素（`vlt/platform/overlay_pixels.py`）。差异只在机制：

| 能力 | Wayland 后端 | 本后端 |
|---|---|---|
| 置顶 | layer-shell 的 overlay 层 | 覆盖窗（override-redirect）+ 周期性 `XRaiseWindow` |
| 定位 | anchor + margin（输出局部坐标） | `XMoveWindow`（全局 root 坐标） |
| 穿透 | `wl_surface.set_input_region(空)` | X Shape 输入区（复用 `linux.set_click_through`） |
| 拖动 | 双源（relative-pointer / 本地坐标差） | 绝对坐标（`x_root/y_root`）+ `XGrabPointer` |
| 跨屏拖动 | 拖动中夹边、**松手换面**（协议所迫） | **全程自由跨屏**（X11 没有「面必须属于某块输出」的约束） |

## 实测钉死的实现事实（改之前先读）

* **32 位 visual**：`XMatchVisualInfo(screen, 32, TrueColor)`；窗口属性必须用
  `background_pixel=0 + border_pixel=0`（`CWBackPixel|CWBorderPixel`）——
  用 `background_pixmap=None`（`CWBackPixmap`）实测直接 `BadMatch`（Xvfb 与
  Xorg 同款行为，见 `tests/test_x11_window.py` 建窗用例）。
* **XImage 的数据缓冲是我们自己的**：`XCreateImage` 传 `data=NULL` **不会**分配
  （实测 `data` 是空指针，随后 memmove 直接段错误）；传自己的缓冲之后，
  **绝不能调 `XDestroyImage`**（它会把 `data` 也 free 掉，而那内存归 Python）——
  只 `XFree` XImage 结构体本身。
* `XImage.byte_order` 必须是 `LSBFirst`、`bits_per_pixel` 32（x86_64 恒成立，
  这里显式检查并拒绝，防「字节序不符却照画」这种最难查的花屏）。
* **没有合成器时逐像素 alpha 无人混合**（早期行为：透明区显示为黑，用户实测报过
  「明显黑色边框」）——建窗时探测 `_NET_WM_CM_S<n>` 选主，没有就**自动降级**：
  每帧按面板 alpha 裁 1 位形状蒙版（`overlay_pixels.alpha_mask_bits`），黑框消失；
  圆角变锯齿、底板是实色（没合成器时物理上修不了），日志有一行说明。有合成器时
  **不裁形**，保持圆润的逐像素透明。
* **错误护栏复用 `linux._guarded`**：Tk 进程里未被接管的 X 错误会被 Tk 的
  错误处理器升级成致命错误，凡是可能落在「已销毁句柄」上的请求都要裹上它。

## 线程约定

与 Tk / Wayland 后端一致：**只有 GUI 主线程**碰这个对象；`tick()` 用非阻塞的
`XPending` 泵事件，绝不阻塞界面。
"""
from __future__ import annotations

import ctypes
import os
from typing import Any, Callable

from PIL import Image

from .overlay_pixels import alpha_mask_bits, clamp01, premultiplied_bgra
from ..session.chatgpt_output import allowed, current
# 复用 linux.py 的现成工具（都是连接无关的）：
#   * `_guarded` —— 说明与理由见 linux.py 模块内文档（X 错误护栏）；
#   * `set_click_through` —— X Shape 输入区（协议级穿透）；
#   * `set_window_shape` —— 1 位形状蒙版（无合成器时的降级裁形，见 _draw）；
#   * `set_tool_window` —— WM_HINTS input=False（不抢焦点）。
from .linux import _guarded, set_click_through, set_tool_window, set_window_shape

_LOG = "[desktop:x11]"

from .x11_bindings import (  # noqa: F401 — keep existing import locations
    _CW_BACK_PIXEL, _CW_BORDER_PIXEL, _CW_OVERRIDE_REDIRECT, _CW_EVENT_MASK,
    _CW_COLORMAP, _ALLOC_NONE, _INPUT_OUTPUT, _ZPIXMAP,
    _LSB_FIRST, _TRUE_COLOR, _EXPOSURE_MASK, _BUTTON_PRESS_MASK,
    _BUTTON_RELEASE_MASK, _BUTTON_MOTION_MASK, _STRUCTURE_NOTIFY_MASK, _EVENT_MASK,
    _CW_MASK, _BUTTON1, _GRAB_EVENT_MASK, _GRAB_MODE_ASYNC,
    _GRAB_SUCCESS, _EXPOSE, _BUTTON_PRESS, _BUTTON_RELEASE,
    _MOTION_NOTIFY, drag_target, _XVisualInfo, _XSetWindowAttributes,
    _XImage, _XPointerEvent, _X11_DEAD, _load_xlib,
)

# ================================================================ 窗口对象


class ArgbWindow:
    """一块 32 位 ARGB 覆盖窗的最小封装（遵守 `base.DesktopWindow` 契约）。

    生命周期：`__init__` 连 X 服务 + 建窗（失败保持 `available=False`）；
    之后 `tick()` 泵事件、`set_panel()` 出新图、`move()`/`set_*()` 改状态；
    `close()` 幂等。
    """

    #: 建不起来时保持 False（调用方据此回落 Tk）
    available = False

    def __init__(self, size: tuple[int, int], alpha: float = 1.0,
                 click_through: bool = True,
                 on_drag_end: Callable[[int, int], None] | None = None,
                 _x11: Any = None) -> None:
        self._size = (max(1, int(size[0])), max(1, int(size[1])))
        self._alpha = clamp01(alpha)
        self._click_through = bool(click_through)
        self._draggable = False
        self._on_drag_end = on_drag_end
        self._pos = (0, 0)
        self._panel: Image.Image | None = None
        self._panel_approval: Any = None
        self._dirty = False
        self._dead = False

        self._x11: Any = None
        self._dpy: Any = None
        self._screen = 0
        self._root = 0
        self._visual: _XVisualInfo | None = None
        self._cmap = 0
        self._win = 0
        self._gc: Any = None
        self._img: Any = None
        self._buf: Any = None
        self._has_compositor = True   # 建窗时探测；没有 → 逐像素 alpha 无人混合

        self._drag_active = False
        self._drag_anchor_pos: tuple[int, int] = (0, 0)
        self._drag_anchor_root: tuple[int, int] = (0, 0)
        self._buttons_seen = 0        # 收到的指针按键总数（穿透验证 / 排障用）
        self._tick_count = 0
        self._shape_warned = False

        try:
            self._setup(_x11)
            self.available = True
        except Exception as exc:  # noqa: BLE001 — 建不起来就保持 available=False
            print(f"{_LOG} ⚠️ 原生 X11 窗建不起来（回落 Tk）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            self._teardown()

    # ---------- 建立 / 销毁 ----------

    def _setup(self, x11: Any = None) -> None:
        self._x11 = x11 if x11 is not None else _load_xlib()
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("没有 DISPLAY（纯 Wayland 且无 XWayland？）")
        dpy = self._x11.XOpenDisplay(None)
        if not dpy:
            raise RuntimeError("XOpenDisplay 失败（连不上 X 服务？）")
        self._dpy = dpy
        screen = int(self._x11.XDefaultScreen(dpy))
        self._screen = screen
        self._root = int(self._x11.XRootWindow(dpy, screen))

        vi = _XVisualInfo()
        if not self._x11.XMatchVisualInfo(dpy, screen, 32, _TRUE_COLOR, ctypes.byref(vi)):
            raise RuntimeError("本 X 服务没有 32 位 TrueColor visual（ARGB 窗前提）")
        self._visual = vi
        self._cmap = int(self._x11.XCreateColormap(dpy, self._root, vi.visual, _ALLOC_NONE))
        if not self._cmap:
            raise RuntimeError("XCreateColormap 失败")

        attrs = _XSetWindowAttributes()
        # ⚠️ 必须用 background_pixel/border_pixel（=0，32 位 visual 下的全透明黑）。
        # 用 background_pixmap=None 实测 BadMatch（继承父窗 24 位底的深度冲突）。
        attrs.background_pixel = 0
        attrs.border_pixel = 0
        attrs.colormap = self._cmap
        attrs.override_redirect = 1
        attrs.event_mask = _EVENT_MASK
        w, h = self._size
        win = _guarded(self._x11, dpy, lambda: self._x11.XCreateWindow(
            dpy, ctypes.c_ulong(self._root), int(self._pos[0]), int(self._pos[1]),
            w, h, 0, 32, _INPUT_OUTPUT, vi.visual, _CW_MASK, ctypes.byref(attrs)))
        if not win:
            raise RuntimeError("XCreateWindow 失败（没有 32 位 visual？）")
        self._win = int(win)

        gc = _guarded(self._x11, dpy,
                      lambda: self._x11.XCreateGC(dpy, ctypes.c_ulong(self._win), 0, None))
        if not gc:
            raise RuntimeError("XCreateGC 失败")
        self._gc = gc
        self._make_image(w, h)

        self._has_compositor = self._compositor_note()
        if not set_tool_window(self._win):
            print(f"{_LOG} ⚠️ 「不抢焦点」没设上（WM_HINTS）→ 点面板可能把焦点从游戏抢走",
                  flush=True)
        self._apply_input_region()
        if _guarded(self._x11, dpy,
                    lambda: self._x11.XMapRaised(dpy, ctypes.c_ulong(self._win))) is None:
            raise RuntimeError("XMapRaised 失败")

    def _compositor_note(self) -> bool:
        """探测合成器并说明降级路径；返回是否有合成器。

        没有合成器时逐像素 alpha **无人混合**：若不处理，透明区会显示为黑（早期版本
        的行为，用户实测报过）。这里返回 False，`_draw` 会改用 1 位形状蒙版把透明区
        裁掉 —— 黑框没了（圆角变锯齿、底板仍是实色，这些没有合成器时物理上修不了）。
        """
        owner = 0
        try:
            atom = int(self._x11.XInternAtom(
                self._dpy, f"_NET_WM_CM_S{self._screen}".encode(), 0))
            if atom:
                owner = int(self._x11.XGetSelectionOwner(self._dpy, ctypes.c_ulong(atom)))
        except Exception:  # noqa: BLE001 — 探测失败就按没有，不值得拦建窗
            owner = 0
        if not owner:
            print(f"{_LOG} ⚠️ 没检测到合成器（_NET_WM_CM_S{self._screen} 无主）："
                  f"逐像素 alpha 无人混合 → 已改用 1 位形状蒙版裁掉透明区"
                  f"（没有黑框；圆角为锯齿、底板是实色）。起一个合成器（如 picom）"
                  f"后**重启程序**即恢复圆润透明。", flush=True)
        return bool(owner)

    def _make_image(self, w: int, h: int) -> None:
        """（重）建出图缓冲：Python 持有数据，XImage 只是借用。

        ⚠️ `XCreateImage` 的 data 传 NULL **不会**分配（实测空指针），必须自备；
        自备之后不能 `XDestroyImage`（那会把我们的缓冲 free 掉）——释放走
        `_free_image()` 的 `XFree`（只释放 Xlib 分配的结构体）。
        """
        self._free_image()
        self._buf = ctypes.create_string_buffer(w * 4 * h)
        img = self._x11.XCreateImage(
            self._dpy, self._visual.visual, 32, _ZPIXMAP, 0,
            ctypes.cast(self._buf, ctypes.c_void_p), w, h, 32, 0)
        if not img:
            raise RuntimeError("XCreateImage 失败")
        self._img = img
        meta = img.contents
        if meta.byte_order != _LSB_FIRST or meta.bits_per_pixel != 32:
            error = RuntimeError(f"XImage 布局不符（byte_order={meta.byte_order} bpp={meta.bits_per_pixel}）—— 拒绝画错色")
            self._free_image()
            raise error

    def _free_image(self) -> None:
        if self._img is not None:
            # 只释放结构体；数据缓冲归 Python（见 _make_image 的 ⚠️）
            self._x11.XFree(ctypes.cast(self._img, ctypes.c_void_p))
            self._img = None
        self._buf = None

    def close(self) -> None:
        self._panel = self._panel_approval = None
        self._dirty = False
        self._dead = True
        if self._dpy is None and not self._win:
            self.available = False
            return
        self._teardown()

    def _teardown(self) -> None:
        self._dead = True
        if self._x11 is None:
            self.available = False
            return
        try:
            self._drag_active = False
            if self._dpy and self._win:
                _guarded(self._x11, self._dpy,
                         lambda: self._x11.XUngrabPointer(self._dpy, 0))
            self._free_image()
            if self._dpy and self._gc:
                _guarded(self._x11, self._dpy,
                         lambda: self._x11.XFreeGC(self._dpy, self._gc))
                self._gc = None
            if self._dpy and self._win:
                _guarded(self._x11, self._dpy, lambda: self._x11.XDestroyWindow(
                    self._dpy, ctypes.c_ulong(self._win)))
                self._win = 0
            if self._dpy and self._cmap:
                _guarded(self._x11, self._dpy, lambda: self._x11.XFreeColormap(
                    self._dpy, ctypes.c_ulong(self._cmap)))
                self._cmap = 0
            if self._dpy:
                self._x11.XCloseDisplay(self._dpy)
                self._dpy = None
        except Exception as exc:  # noqa: BLE001 — 关窗失败不值得打断退出流程
            print(f"{_LOG} ⚠️ 关闭原生 X11 窗时报错（已忽略）："
                  f"{type(exc).__name__}: {exc}", flush=True)
        finally:
            self._dead = True
            self.available = False
            self._img = None
            self._buf = None

    def _mark_dead(self, why: str) -> None:
        if self._dead:
            return
        print(f"{_LOG} ⚠️ {why} → 面板停更（不崩进程）", flush=True)
        self._dead = True
        self.available = False
        self._panel = self._panel_approval = None
        self._dirty = False

    # ---------- 出图 ----------

    def _draw(self) -> None:
        if self._dead or self._img is None or self._panel is None or not self._win:
            return
        w, h = self._size
        panel, approval, alpha = self._panel, self._panel_approval, self._alpha
        if alpha > 0 and not allowed(approval):
            return
        img = panel
        if img.size != (w, h):            # 尺寸还没跟上（或来了怪帧）→ 补透明边
            padded = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            padded.paste(img, (0, 0))
            img = padded
        data = premultiplied_bgra(img, alpha)
        buf, native_image, win = self._buf, self._img, self._win
        def valid():
            return (not self._dead and self._panel is panel and self._buf is buf
                    and self._img is native_image and self._win == win and self._alpha == alpha
                    and (alpha == 0 or allowed(approval)))
        if not valid():
            return
        ctypes.memmove(buf, data, len(data))
        # The X error guard synchronizes first; recheck inside its native callback.
        res = _guarded(self._x11, self._dpy, lambda: self._x11.XPutImage(
            self._dpy, ctypes.c_ulong(win), self._gc, native_image, 0, 0, 0, 0, w, h)
            if valid() else None)
        if not valid():
            return
        if res is None:                   # 只有 X 错误才走这里（成功时返回 0/1 都可能）
            self._mark_dead("XPutImage 报错（窗口被外部销毁？）")
            return
        self._dirty = False
        if not self._has_compositor:
            # 无合成器降级：按面板 alpha 裁 1 位形状蒙版（黑框没了；圆角为锯齿）。
            # 口径与 Tk 回落路径完全一致（overlay_pixels.alpha_mask_bits），只是
            # Tk 那边抠的是键色底，这里抠的是"没人混合的透明区"。
            try:
                mask = alpha_mask_bits(img)
                if not set_window_shape(self._win, mask, w, h) and not self._shape_warned:
                    self._shape_warned = True
                    print(f"{_LOG} ⚠️ 形状蒙版没设上（X Shape 不可用？）"
                          f"→ 无合成器时透明区会显黑", flush=True)
            except Exception as exc:  # noqa: BLE001 — 裁形失败不许带崩出图
                if not self._shape_warned:
                    self._shape_warned = True
                    print(f"{_LOG} ⚠️ 形状蒙版失败（已忽略）："
                          f"{type(exc).__name__}: {exc}", flush=True)

    # ---------- 事件泵 / 指针 ----------

    def tick(self) -> None:
        """泵事件 + 补画（GUI 每 50ms 一跳）。绝不阻塞、绝不抛。"""
        if self._dead:
            return
        ev = (ctypes.c_long * 24)()
        try:
            while self._x11.XPending(self._dpy):
                self._x11.XNextEvent(self._dpy, ctypes.byref(ev))
                etype = int(ev[0]) & 0x7F
                if etype == _EXPOSE:
                    self._draw()          # 无 backing store：暴露就得重画
                elif etype == _BUTTON_PRESS:
                    self._on_press(self._as_pointer_event(ev))
                elif etype == _BUTTON_RELEASE:
                    self._on_release(self._as_pointer_event(ev))
                elif etype == _MOTION_NOTIFY:
                    self._on_motion(self._as_pointer_event(ev))
        except Exception as exc:  # noqa: BLE001 — 泵挂了也不能带崩共享 tick
            self._mark_dead(f"事件泵异常 {type(exc).__name__}: {exc}")
            return
        if self._dirty:
            self._draw()
            if self._dead:
                return
        self._tick_count += 1
        if self._tick_count % 20 == 1:    # 约 1s 抬一次：别被后起的全屏窗盖住
            if _guarded(self._x11, self._dpy, lambda: self._x11.XRaiseWindow(
                    self._dpy, ctypes.c_ulong(self._win))) is None:
                self._mark_dead("XRaiseWindow 报错（窗口被外部销毁？）")

    @staticmethod
    def _as_pointer_event(ev: Any) -> _XPointerEvent:
        return ctypes.cast(ctypes.byref(ev), ctypes.POINTER(_XPointerEvent)).contents

    def _on_press(self, e: _XPointerEvent) -> None:
        self._buttons_seen += 1
        if int(e.detail) != _BUTTON1 or not self._draggable:
            return
        self._drag_active = True
        self._drag_anchor_pos = self._pos
        self._drag_anchor_root = (int(e.x_root), int(e.y_root))
        grab = _guarded(self._x11, self._dpy, lambda: self._x11.XGrabPointer(
            self._dpy, ctypes.c_ulong(self._win), 0, _GRAB_EVENT_MASK,
            _GRAB_MODE_ASYNC, _GRAB_MODE_ASYNC, 0, 0, 0))
        if grab != _GRAB_SUCCESS:        # None（X 错误）或非 0 状态码
            print(f"{_LOG} ⚠️ 指针抓取失败（GrabStatus={grab}）"
                  f"→ 拖动只在面板范围内跟手", flush=True)
        print(f"{_LOG} 已抓住面板（拖动中，放开结束）", flush=True)

    def _on_motion(self, e: _XPointerEvent) -> None:
        if not self._drag_active:
            return
        self.move(*drag_target(self._drag_anchor_pos, self._drag_anchor_root,
                               (int(e.x_root), int(e.y_root))))

    def _on_release(self, e: _XPointerEvent) -> None:
        del e
        if not self._drag_active:
            return
        self._drag_active = False
        _guarded(self._x11, self._dpy, lambda: self._x11.XUngrabPointer(self._dpy, 0))
        if self._on_drag_end is not None:
            try:
                self._on_drag_end(self._pos[0], self._pos[1])
            except Exception as exc:  # noqa: BLE001
                print(f"{_LOG} ⚠️ 拖动回调出错（已忽略）："
                      f"{type(exc).__name__}: {exc}", flush=True)
        print(f"{_LOG} 拖动结束，落点 {self._pos}", flush=True)

    def _apply_input_region(self) -> None:
        """穿透 = X Shape 输入区置空；解锁拖动 = 恢复默认（整窗）。

        ⚠️ 语义与 Wayland 后端一致：**拖动优先** —— 解锁拖动时必须能收到指针
        事件，所以哪怕 `click_through=True` 也要把输入区恢复成整窗。
        """
        if self._dead or not self._win:
            return
        want_empty = not (self._draggable or not self._click_through)
        if not set_click_through(self._win, want_empty) and not self._shape_warned:
            self._shape_warned = True
            print(f"{_LOG} ⚠️ 鼠标穿透没设上（X Shape 不可用？）→ 面板会挡住鼠标",
                  flush=True)

    # ---------- 对外（窗口契约） ----------

    def set_panel(self, image: Image.Image, alpha: float | None = None) -> None:
        """贴一帧面板；`alpha=None` 保持当前整层乘子（窗口契约）。"""
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
        """移到全局屏幕坐标（X11 就是 root 坐标；多屏含负坐标都对）。"""
        if self._dead:
            return
        self._pos = (int(x), int(y))
        if not self._win:
            return
        if _guarded(self._x11, self._dpy, lambda: self._x11.XMoveWindow(
                self._dpy, ctypes.c_ulong(self._win),
                int(self._pos[0]), int(self._pos[1]))) is None:
            self._mark_dead("XMoveWindow 报错（窗口被外部销毁？）")

    def set_size(self, size: tuple[int, int]) -> None:
        size = (max(1, int(size[0])), max(1, int(size[1])))
        if size == self._size:
            return
        self._size = size
        if self._dead or not self._win:
            return
        try:
            self._make_image(*size)
        except Exception as exc:  # noqa: BLE001
            self._mark_dead(f"改尺寸失败 {type(exc).__name__}: {exc}")
            return
        if _guarded(self._x11, self._dpy, lambda: self._x11.XResizeWindow(
                self._dpy, ctypes.c_ulong(self._win), *size)) is None:
            self._mark_dead("XResizeWindow 报错（窗口被外部销毁？）")
            return
        self._dirty = True
        self._draw()

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
        self._apply_input_region()

    def set_draggable(self, on: bool) -> None:
        on = bool(on)
        if on == self._draggable:
            return
        self._draggable = on
        if not on and self._drag_active:
            self._drag_active = False
            _guarded(self._x11, self._dpy, lambda: self._x11.XUngrabPointer(self._dpy, 0))
        self._apply_input_region()

    @property
    def position(self) -> tuple[int, int]:
        return self._pos
