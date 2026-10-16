"""OpenXR EGL/GLX contexts and native pixel upload."""
from __future__ import annotations
import ctypes
import logging
from typing import Any
from ..session.chatgpt_output import allowed, current
from .openxr_model import (
    EGL_ALPHA_SIZE,
    EGL_BLUE_SIZE,
    EGL_CONTEXT_MAJOR_VERSION,
    EGL_CONTEXT_MINOR_VERSION,
    EGL_GREEN_SIZE,
    EGL_NONE,
    EGL_NO_SURFACE,
    EGL_OPENGL_API,
    EGL_OPENGL_BIT,
    EGL_PBUFFER_BIT,
    EGL_PLATFORM_WAYLAND_KHR,
    EGL_RED_SIZE,
    EGL_RENDERABLE_TYPE,
    EGL_SURFACE_TYPE,
    GLX_ALPHA_SIZE,
    GLX_BLUE_SIZE,
    GLX_CONTEXT_CORE_PROFILE_BIT_ARB,
    GLX_CONTEXT_MAJOR_VERSION_ARB,
    GLX_CONTEXT_MINOR_VERSION_ARB,
    GLX_CONTEXT_PROFILE_MASK_ARB,
    GLX_DRAWABLE_TYPE,
    GLX_GREEN_SIZE,
    GLX_PBUFFER_BIT,
    GLX_PBUFFER_HEIGHT,
    GLX_PBUFFER_WIDTH,
    GLX_RED_SIZE,
    GLX_RENDER_TYPE,
    GLX_RGBA_BIT,
    GLX_RGBA_TYPE,
    GLX_VISUAL_ID,
    GLX_X_RENDERABLE,
    GL_RGBA,
    GL_TEXTURE_2D,
    GL_UNSIGNED_BYTE,
)

log = logging.getLogger("vlt.output.openxr_overlay")

class _GlBackend:
    """GL 后端的公共部分（纯 ctypes，不引 PyOpenGL/glfw 做 GL 调用）。

    两个后端（Wayland-EGL / X11-GLX）只差两件事：**怎么建 current context**
    （子类 `__init__`）与**给 xrCreateSession 的图形绑定结构**（子类 `binding()`）；
    贴图上传、字符串查询走同一份 libGL，完全共用。

    ⚠️ 每个 extern 函数都要写全 `argtypes`/`restype` —— 不写的话 ctypes 把 64 位指针
    当 32 位 int 传，直接段错误（spike 阶段实测崩过一次）。
    """

    name = "?"
    #: 建 session 需要运行时提供的**实例扩展**。后端不同、清单不同：
    #: Wayland 要 `XR_MNDX_egl_enable`；X11 的 XLIB 绑定本身在 `XR_KHR_opengl_enable` 里。
    REQUIRED_EXTENSIONS: tuple[str, ...] = ("XR_EXTX_overlay", "XR_KHR_opengl_enable")

    def __init__(self) -> None:
        self.gl = ctypes.CDLL("libGL.so.1")
        self._bind_gl()

    def _bind_gl(self) -> None:
        self.gl.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.gl.glBindTexture.restype = None
        self.gl.glTexImage2D.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        self.gl.glTexImage2D.restype = None
        self.gl.glTexSubImage2D.argtypes = [ctypes.c_uint, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int,
                                            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        self.gl.glTexSubImage2D.restype = None
        self.gl.glGetString.argtypes = [ctypes.c_uint]
        self.gl.glGetString.restype = ctypes.c_char_p
        self.gl.glGetError.argtypes = []
        self.gl.glGetError.restype = ctypes.c_uint
        self.gl.glFinish.argtypes = []
        self.gl.glFinish.restype = None

    def binding(self) -> Any:
        """xrCreateSession 链上的图形绑定结构（`create()` 返回前不得释放）。"""
        raise NotImplementedError

    def gl_string(self, which: int) -> str:
        p = self.gl.glGetString(which)
        return p.decode() if p else "?"

    def upload(self, texture_id: int, width: int, height: int, rgba: bytes) -> bool:
        """把一张 RGBA 贴图写进 swapchain 给我们的 GL 纹理。

        ⚠️ 必须用 `glTexSubImage2D`，**不能**用 `glTexImage2D`：swapchain 的纹理是运行时
        已经完整分配的（immutable），重新 `glTexImage2D` 会返回 `GL_INVALID_OPERATION
        (0x502)` —— 上传静默失败、纹理保持初始黑色。实测现象就是「面板纯黑、没有任何文字」。

        ⚠️ 还要按行倒序：OpenGL 纹理原点在**左下**，而 PIL 图像是**自上而下**，
        不翻转的话面板内容整体倒置。
        """
        import numpy as np
        approval = current()
        if not allowed(approval):
            return False
        arr = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)[::-1]
        buf = ctypes.create_string_buffer(arr.tobytes(), len(rgba))
        if not allowed(approval):
            return False
        self.gl.glBindTexture(GL_TEXTURE_2D, texture_id)
        if not allowed(approval):
            return False
        self.gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, width, height,
                                GL_RGBA, GL_UNSIGNED_BYTE, buf)
        err = self.gl.glGetError()
        if err:
            log.warning("[overlay:xr] GL 上传出错 glGetError=0x%x（texture=%s %dx%d）",
                        err, texture_id, width, height)
        self.gl.glFinish()
        if err:
            raise RuntimeError(f"GL upload failed: 0x{err:x}")
        return True

    def close(self) -> None:
        raise NotImplementedError


class EglGlContext(_GlBackend):
    """Wayland + EGL 的 **surfaceless** context（`XR_MNDX_egl_enable` 图形绑定）。

    这是 niri / KDE / GNOME 等 Wayland 会话的常规路径（本机实测通过）。
    """

    name = "wayland-egl"
    REQUIRED_EXTENSIONS = ("XR_EXTX_overlay", "XR_KHR_opengl_enable", "XR_MNDX_egl_enable")

    def __init__(self) -> None:
        super().__init__()
        self.wl = ctypes.CDLL("libwayland-client.so.0")
        self.egl = ctypes.CDLL("libEGL.so.1")
        self._bind_egl()

        self.wl_display = self.egl_display = self.egl_context = None
        try:
            self.wl_display = self.wl.wl_display_connect(None)
            if not self.wl_display:
                raise RuntimeError("wl_display_connect 失败（WAYLAND_DISPLAY 没设？）")
            self.egl_display = self.egl.eglGetPlatformDisplay(
                EGL_PLATFORM_WAYLAND_KHR, self.wl_display, None)
            if not self.egl_display:
                raise RuntimeError(f"eglGetPlatformDisplay 失败 0x{self.egl.eglGetError():x}")
            maj, minr = ctypes.c_int(), ctypes.c_int()
            if not self.egl.eglInitialize(self.egl_display, ctypes.byref(maj), ctypes.byref(minr)):
                raise RuntimeError(f"eglInitialize 失败 0x{self.egl.eglGetError():x}")
            if not self.egl.eglBindAPI(EGL_OPENGL_API):
                raise RuntimeError(f"eglBindAPI 失败 0x{self.egl.eglGetError():x}")
            cfg_attribs = (ctypes.c_int * 13)(
                EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
                EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE)
            self.egl_config = ctypes.c_void_p()
            num = ctypes.c_int()
            if not self.egl.eglChooseConfig(self.egl_display, cfg_attribs,
                                            ctypes.byref(self.egl_config), 1, ctypes.byref(num)) \
                    or num.value == 0:
                raise RuntimeError(f"eglChooseConfig 失败 0x{self.egl.eglGetError():x}")
            ctx_attribs = (ctypes.c_int * 5)(EGL_CONTEXT_MAJOR_VERSION, 3,
                                             EGL_CONTEXT_MINOR_VERSION, 3, EGL_NONE)
            self.egl_context = self.egl.eglCreateContext(self.egl_display, self.egl_config,
                                                         None, ctx_attribs)
            if not self.egl_context:
                self.egl_context = self.egl.eglCreateContext(self.egl_display, self.egl_config,
                                                             None, None)
            if not self.egl_context:
                raise RuntimeError(f"eglCreateContext 失败 0x{self.egl.eglGetError():x}")
            if not self.egl.eglMakeCurrent(self.egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE,
                                           self.egl_context):
                raise RuntimeError(f"eglMakeCurrent(surfaceless) 失败 0x{self.egl.eglGetError():x}")
        except BaseException:
            self.close()
            raise

    def _bind_egl(self) -> None:
        self.wl.wl_display_connect.argtypes = [ctypes.c_char_p]
        self.wl.wl_display_connect.restype = ctypes.c_void_p
        self.wl.wl_display_disconnect.argtypes = [ctypes.c_void_p]
        self.wl.wl_display_disconnect.restype = None
        self.egl.eglGetPlatformDisplay.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        self.egl.eglGetPlatformDisplay.restype = ctypes.c_void_p
        self.egl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                           ctypes.POINTER(ctypes.c_int)]
        self.egl.eglInitialize.restype = ctypes.c_uint
        self.egl.eglBindAPI.argtypes = [ctypes.c_uint]
        self.egl.eglBindAPI.restype = ctypes.c_uint
        self.egl.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_int)]
        self.egl.eglChooseConfig.restype = ctypes.c_uint
        self.egl.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.egl.eglCreateContext.restype = ctypes.c_void_p
        self.egl.eglMakeCurrent.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p]
        self.egl.eglMakeCurrent.restype = ctypes.c_uint
        self.egl.eglDestroyContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.egl.eglDestroyContext.restype = ctypes.c_uint
        self.egl.eglTerminate.argtypes = [ctypes.c_void_p]
        self.egl.eglTerminate.restype = ctypes.c_uint
        self.egl.eglGetError.restype = ctypes.c_uint
        self.egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
        self.egl.eglGetProcAddress.restype = ctypes.c_void_p

    def get_proc_address_addr(self) -> int:
        """Monado 会**真的调用**我们提供的 `getProcAddress`。"""
        return ctypes.cast(self.egl.eglGetProcAddress, ctypes.c_void_p).value

    def binding(self) -> Any:
        """构造 `GraphicsBindingEGLMNDX`（三个指针字段的类型来自 PyOpenGL）。

        ⚠️ 不能给 int / c_void_p，必须 `ctypes.cast(c_void_p(addr), 该类型)`，
            否则报 "expected EGLDisplay instead of int"。
        """
        import xr
        t = {n: tp for n, tp in xr.GraphicsBindingEGLMNDX._fields_}
        return xr.GraphicsBindingEGLMNDX(
            get_proc_address=t["get_proc_address"](self.get_proc_address_addr()),
            display=ctypes.cast(ctypes.c_void_p(self.egl_display), t["display"]),
            config=ctypes.cast(ctypes.c_void_p(self.egl_config.value), t["config"]),
            context=ctypes.cast(ctypes.c_void_p(self.egl_context), t["context"]))

    def close(self) -> None:
        display, context, wayland = self.egl_display, self.egl_context, self.wl_display
        self.egl_display = self.egl_context = self.wl_display = None
        releases = []
        if display and context:
            releases.extend(((self.egl.eglMakeCurrent,
                              (display, EGL_NO_SURFACE, EGL_NO_SURFACE, None)),
                             (self.egl.eglDestroyContext, (display, context))))
        if display:
            releases.append((self.egl.eglTerminate, (display,)))
        if wayland:
            releases.append((self.wl.wl_display_disconnect, (wayland,)))
        for fn, args in releases:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                pass


# Xlib 错误处理回调签名：int handler(Display*, XErrorEvent*)
_X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class XlibGlxContext(_GlBackend):
    """X11 + GLX 的 **pbuffer** context（`GraphicsBindingOpenGLXlibKHR` 图形绑定）。

    用在真正的 Xorg 会话（或带完整 XWayland/GLX 的桌面）。
    ⚠️ niri 的精简 `xwayland-satellite` 没有可用的 GLX 渲染 —— 那条路上会建不起来，
       但这没关系：niri 是 Wayland 会话，走上面的 EGL 后端（`create_gl_context()` 选择）。

    为什么用 pbuffer 而不是窗口：贴图只是上传到 swapchain 的纹理，不需要可见窗口；
    pbuffer 不需要窗口管理器配合，Xvfb/无头环境也能建起来（离线测试就靠这条）。

    ⚠️⚠️ **建上下文期间必须接管 Xlib 的错误处理器**：GLX 的失败（比如
    `glXCreateContextAttribsARB` 在不支持 GLX 渲染的 X server 上）走的是 Xlib
    **异步错误**通道，而 Xlib 默认处理器会把**整个进程**杀掉（打印一行
    `X Error of failed request: GLXBadFBConfig` 然后 exit）—— 实测踩过：
    在 niri 的 `xwayland-satellite` 上只是建 overlay 的 GL context，却把整个应用带走了。
    这里在作用域内装自己的处理器（吞掉 + 记账），`XSync` 把异步错误冲出来，
    然后还原原处理器；有错就抛 RuntimeError 交给 `create_gl_context()` 回退。
    （处理器是 Xlib 进程级全局的，窗口期只有毫秒级；期间别的线程的 X 错误会被
      一并吞掉——但那本来会导致 exit，吞掉反而是更安全的偏置。）
    """

    name = "x11-glx"
    REQUIRED_EXTENSIONS = ("XR_EXTX_overlay", "XR_KHR_opengl_enable")

    def __init__(self, width: int = 64, height: int = 64) -> None:
        super().__init__()
        self.x = ctypes.CDLL("libX11.so.6")
        self.glx = ctypes.CDLL("libGL.so.1")     # GLX 符号在 libGL 里
        self._bind_x()

        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("XOpenDisplay 失败（DISPLAY 没设或 X server 连不上？）")
        self._fbconfigs_raw = None
        self.pbuffer = 0
        self.context = None
        self._xerror_hits: list[int] = []

        def _on_xerror(_display, _event):          # noqa: ANN001 — ctypes 回调
            self._xerror_hits.append(1)
            return 0                                # 忽略（默认处理器会 exit，绝不能走它）

        self._xerror_cb = _X_ERROR_HANDLER(_on_xerror)   # 必须留引用：GC 掉就是野指针
        old_handler = self.x.XSetErrorHandler(self._xerror_cb)
        try:
            try:
                err_base = ctypes.c_int(0)
                evt_base = ctypes.c_int(0)
                if not self.glx.glXQueryExtension(self.display, ctypes.byref(err_base),
                                                  ctypes.byref(evt_base)):
                    raise RuntimeError("X server 没有 GLX 扩展")

                screen = self.x.XDefaultScreen(self.display)
                attribs = (ctypes.c_int * 15)(
                    GLX_X_RENDERABLE, 1,
                    GLX_DRAWABLE_TYPE, GLX_PBUFFER_BIT,
                    GLX_RENDER_TYPE, GLX_RGBA_BIT,
                    GLX_RED_SIZE, 8, GLX_GREEN_SIZE, 8,
                    GLX_BLUE_SIZE, 8, GLX_ALPHA_SIZE, 8, 0)
                nelem = ctypes.c_int(0)
                raw = self.glx.glXChooseFBConfig(self.display, screen, attribs,
                                                 ctypes.byref(nelem))
                if not raw or nelem.value == 0:
                    raise RuntimeError("glXChooseFBConfig 没找到可用配置")
                self._fbconfigs_raw = raw
                fbconfig = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))[0]
                self.fbconfig = fbconfig
                vid = ctypes.c_int(0)
                self.glx.glXGetFBConfigAttrib(self.display, ctypes.c_void_p(fbconfig),
                                              GLX_VISUAL_ID, ctypes.byref(vid))
                self.visualid = vid.value

                pb_attribs = (ctypes.c_int * 5)(GLX_PBUFFER_WIDTH, width,
                                                GLX_PBUFFER_HEIGHT, height, 0)
                self.pbuffer = self.glx.glXCreatePbuffer(self.display,
                                                         ctypes.c_void_p(fbconfig), pb_attribs)
                if not self.pbuffer:
                    raise RuntimeError("glXCreatePbuffer 失败")
                self.context = self._create_context(fbconfig)
                if not self.context:
                    raise RuntimeError("glXCreate*Context 全部失败")
                if not self.glx.glXMakeCurrent(self.display, self.pbuffer, self.context):
                    raise RuntimeError("glXMakeCurrent 失败")

                # 把异步 X 错误冲出来：没这一步错误会「迟到」到处理器还原之后
                self.x.XSync(self.display, 0)
                if self._xerror_hits:
                    raise RuntimeError("GLX 建上下文收到 X 错误"
                                       "（这个 X server 不支持 GLX 渲染？）")
            except Exception:
                self.close()          # 建到一半失败也要把 X 连接/上下文收干净
                raise
        finally:
            self.x.XSetErrorHandler(old_handler)

    def _create_context(self, fbconfig: int) -> int | None:
        """先试 3.3 core（与 EGL 后端同口径），不行退回兼容 profile。"""
        try:
            proc = self.glx.glXGetProcAddressARB(b"glXCreateContextAttribsARB")
            if proc:
                fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_int))(proc)
                attribs = (ctypes.c_int * 7)(
                    GLX_CONTEXT_MAJOR_VERSION_ARB, 3,
                    GLX_CONTEXT_MINOR_VERSION_ARB, 3,
                    GLX_CONTEXT_PROFILE_MASK_ARB, GLX_CONTEXT_CORE_PROFILE_BIT_ARB, 0)
                ctx = fn(self.display, ctypes.c_void_p(fbconfig), None, 1, attribs)
                if ctx:
                    return ctx
        except Exception:  # noqa: BLE001 — 拿不到 ARB 入口就退回老接口
            pass
        return self.glx.glXCreateNewContext(self.display, ctypes.c_void_p(fbconfig),
                                            GLX_RGBA_TYPE, None, 1)

    def _bind_x(self) -> None:
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XDefaultScreen.argtypes = [ctypes.c_void_p]
        self.x.XDefaultScreen.restype = ctypes.c_int
        self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x.XCloseDisplay.restype = ctypes.c_int
        self.x.XFree.argtypes = [ctypes.c_void_p]
        self.x.XFree.restype = ctypes.c_int
        # ⚠️ 建上下文期间要接管错误处理器 + 主动 XSync（见 __init__ 的说明）
        self.x.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        self.x.XSetErrorHandler.restype = ctypes.c_void_p
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XSync.restype = ctypes.c_int
        self.glx.glXQueryExtension.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                               ctypes.POINTER(ctypes.c_int)]
        self.glx.glXQueryExtension.restype = ctypes.c_int
        self.glx.glXChooseFBConfig.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.POINTER(ctypes.c_int),
                                               ctypes.POINTER(ctypes.c_int)]
        self.glx.glXChooseFBConfig.restype = ctypes.c_void_p
        self.glx.glXGetFBConfigAttrib.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                  ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.glx.glXGetFBConfigAttrib.restype = ctypes.c_int
        self.glx.glXCreatePbuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_int)]
        self.glx.glXCreatePbuffer.restype = ctypes.c_ulong
        self.glx.glXCreateNewContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.glx.glXCreateNewContext.restype = ctypes.c_void_p
        self.glx.glXMakeCurrent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        self.glx.glXMakeCurrent.restype = ctypes.c_int
        self.glx.glXGetProcAddressARB.argtypes = [ctypes.c_char_p]
        self.glx.glXGetProcAddressARB.restype = ctypes.c_void_p
        self.glx.glXDestroyContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.glx.glXDestroyContext.restype = None
        self.glx.glXDestroyPbuffer.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.glx.glXDestroyPbuffer.restype = None

    def binding(self) -> Any:
        """构造 `GraphicsBindingOpenGLXlibKHR`（字段类型来自 PyOpenGL 的 GLX 实现）。

        ⚠️ 与 EGL 那条一样：指针字段必须 cast 成 PyOpenGL 声明的类型，不能给裸 int。
        """
        import xr
        t = {n: tp for n, tp in xr.GraphicsBindingOpenGLXlibKHR._fields_}
        return xr.GraphicsBindingOpenGLXlibKHR(
            x_display=ctypes.cast(ctypes.c_void_p(self.display), t["x_display"]),
            visualid=int(self.visualid),
            glx_fbconfig=ctypes.cast(ctypes.c_void_p(self.fbconfig), t["glx_fbconfig"]),
            glx_drawable=int(self.pbuffer),
            glx_context=ctypes.cast(ctypes.c_void_p(self.context), t["glx_context"]))

    def close(self) -> None:
        display = getattr(self, "display", None)
        if not display:
            return
        try:
            if self.context:
                self.glx.glXMakeCurrent(display, 0, None)
                self.glx.glXDestroyContext(display, self.context)
                self.context = None
            if self.pbuffer:
                self.glx.glXDestroyPbuffer(display, self.pbuffer)
                self.pbuffer = 0
            if self._fbconfigs_raw:
                self.x.XFree(self._fbconfigs_raw)
                self._fbconfigs_raw = None
        finally:
            self.display = None
            try:
                self.x.XCloseDisplay(display)
            except Exception:  # noqa: BLE001
                pass
