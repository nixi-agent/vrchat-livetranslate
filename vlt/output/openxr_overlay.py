"""手腕屏的 **OpenXR** 后端（Linux 独占）。

这是 Windows 侧 `openvr.IVROverlay()` 的**结构对等物**：我们的进程作为
`XR_EXTX_overlay` overlay session 直接连 Monado/WiVRn 的合成器，
**不需要任何第三方 overlay 管理器、不需要注册面板、不写配置文件、不重启任何服务**。

## 下面每一条都是 spike 实测出来的，不是推断（`scripts/spike_openxr.py`）

| 结论 | 依据 |
|---|---|
| 图形绑定**按会话类型二选一**：Wayland → `XR_MNDX_egl_enable` + `GraphicsBindingEGLMNDX`；X11 → `XR_KHR_opengl_enable` + `GraphicsBindingOpenGLXlibKHR` | Monado 的 `oxr_session.c` 只分发 XLIB/WIN32/ES_ANDROID/VULKAN/**EGL_MNDX**/D3D，**没有 `OPENGL_WAYLAND_KHR`**；XLIB 与 EGL_MNDX 两条路 WiVRn/Monado 都支持 |
| 会话链 = `SessionCreateInfo → GraphicsBindingEGLMNDX → SessionCreateInfoOverlayEXTX` | `createFlags` 必须为 0（规范要求） |
| 建 session **前**必须调 `xrGetOpenGLGraphicsRequirementsKHR` | Monado 检查 `sys->gotten_requirements`，否则 `GRAPHICS_REQUIREMENTS_CALL_MISSING` |
| 建完 session **必须泵事件到 READY 再 `xrBeginSession`** | `oxr_session_begin()` 首句就要求 `XR_SESSION_STATE_READY`，否则 `SESSION_NOT_RUNNING` |
| swapchain **必须优先 sRGB 变体 `GL_SRGB8_ALPHA8 (0x8C43)`**，只有 8 位线性格式时才退而求其次并在上传前预线性化（`format_needs_linearize()`）；acquire 后必须 `wait_swapchain_image` | 合成器按**线性**合成、输出前再 `from_linear_to_srgb()`（Monado `shaders/layer.comp` 的 `main()`，注释原文「no automatic conversion in hardware available」；WiVRn 侧 `layer_squasher.cpp` 的 `k_do_color_correction=true`）。层纹理标成线性 = 我们的 sRGB 字节被当线性值再编码一次 = **双重 gamma → 面板黑不下去**。实测：底板 (12,14,20) 在 72.4% 覆盖下显示成 **(51,56,68)** —— 而双重 gamma 的理论地板正是 (52,56,67)（不管背后多黑）；漏 wait 则「上传成功但画面不动」 |
| Quad ✅ / **Cylinder ✅**（弯曲可用） | spike E/F 两项实测通过 |
| 手部 pose **可用** | spike G 实测 `position_tracked`，profile 匹配到 `oculus/touch_controller` |

## 踩过的坑（都有注释标出，别重蹈）

1. **`xrPollEvent` 的结果要从缓冲区起始强转**，不能从 `varying[]` 起 ——
   `EventDataBuffer{type(0),next(8),varying(16)}` vs `EventDataSessionStateChanged{type(0),next(8),session(16),state(24)}`，从 varying 读会偏 16 字节、读到垃圾 0。
2. **必须调 `xrSyncActions`**，否则动作状态永远不更新（pose 读不到）。
3. **niri 下 X11/GLX 走不通 —— 但真正的 Xorg 没这个问题**：niri 的 X11 是精简的
   `xwayland-satellite`，`glXChooseFBConfig`/`glXCreatePbuffer` 能过但**所有**
   context 创建方式都被拒（`GLXBadFBConfig` / `BadValue`）。所以 GL 后端按会话类型选：
   有 Wayland 优先走 libwayland-client + libEGL（niri 的常规路径），否则走 X11/GLX
   （`XlibGlxContext`，pbuffer context + XLIB 图形绑定）。
   ⚠️ **X11 路径的真机验证仍是待办**（2026-10：开发机上没有「X11 显示 + 同一环境跑
      运行时」的验证环境）。离线覆盖到：Xvfb+GLX 下真实建上下文、binding 结构体、
      按后端的扩展清单（`tests/test_overlay_glx.py` / `tests/test_openxr_overlay.py`）。
   强制指定后端可用环境变量 `VLT_OVERLAY_GL=wayland|x11`（排查用）。
4. 建议**多个** interaction profile，别只给一个。
5. `xrWaitFrame` 没有超时参数。
"""
from __future__ import annotations
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any
from ..session.chatgpt_output import allowed, current, scope
from .overlay import OverlayConfig, render_conversation, render_panel
from .openxr_gl import _GlBackend as _GlBackend, EglGlContext as EglGlContext, XlibGlxContext as XlibGlxContext
from .openxr_session import XrOverlaySession as XrOverlaySession, XrError as XrError
from .openxr_maintenance import _XrMaintenance
from .openxr_demo import render_alpha_test as render_alpha_test, _smoke
from .openxr_model import (
    CYLINDER_EXT as CYLINDER_EXT,
    EGL_ALPHA_SIZE as EGL_ALPHA_SIZE,
    EGL_BLUE_SIZE as EGL_BLUE_SIZE,
    EGL_CONTEXT_MAJOR_VERSION as EGL_CONTEXT_MAJOR_VERSION,
    EGL_CONTEXT_MINOR_VERSION as EGL_CONTEXT_MINOR_VERSION,
    EGL_GREEN_SIZE as EGL_GREEN_SIZE,
    EGL_NONE as EGL_NONE,
    EGL_NO_SURFACE as EGL_NO_SURFACE,
    EGL_OPENGL_API as EGL_OPENGL_API,
    EGL_OPENGL_BIT as EGL_OPENGL_BIT,
    EGL_PBUFFER_BIT as EGL_PBUFFER_BIT,
    EGL_PLATFORM_WAYLAND_KHR as EGL_PLATFORM_WAYLAND_KHR,
    EGL_RED_SIZE as EGL_RED_SIZE,
    EGL_RENDERABLE_TYPE as EGL_RENDERABLE_TYPE,
    EGL_SURFACE_TYPE as EGL_SURFACE_TYPE,
    GLX_ALPHA_SIZE as GLX_ALPHA_SIZE,
    GLX_BLUE_SIZE as GLX_BLUE_SIZE,
    GLX_CONTEXT_CORE_PROFILE_BIT_ARB as GLX_CONTEXT_CORE_PROFILE_BIT_ARB,
    GLX_CONTEXT_MAJOR_VERSION_ARB as GLX_CONTEXT_MAJOR_VERSION_ARB,
    GLX_CONTEXT_MINOR_VERSION_ARB as GLX_CONTEXT_MINOR_VERSION_ARB,
    GLX_CONTEXT_PROFILE_MASK_ARB as GLX_CONTEXT_PROFILE_MASK_ARB,
    GLX_DRAWABLE_TYPE as GLX_DRAWABLE_TYPE,
    GLX_GREEN_SIZE as GLX_GREEN_SIZE,
    GLX_PBUFFER_BIT as GLX_PBUFFER_BIT,
    GLX_PBUFFER_HEIGHT as GLX_PBUFFER_HEIGHT,
    GLX_PBUFFER_WIDTH as GLX_PBUFFER_WIDTH,
    GLX_RED_SIZE as GLX_RED_SIZE,
    GLX_RENDER_TYPE as GLX_RENDER_TYPE,
    GLX_RGBA_BIT as GLX_RGBA_BIT,
    GLX_RGBA_TYPE as GLX_RGBA_TYPE,
    GLX_VISUAL_ID as GLX_VISUAL_ID,
    GLX_X_RENDERABLE as GLX_X_RENDERABLE,
    GL_RENDERER as GL_RENDERER,
    GL_RGB10_A2 as GL_RGB10_A2,
    GL_RGBA as GL_RGBA,
    GL_RGBA8 as GL_RGBA8,
    GL_SRGB8 as GL_SRGB8,
    GL_SRGB8_ALPHA8 as GL_SRGB8_ALPHA8,
    GL_TEXTURE_2D as GL_TEXTURE_2D,
    GL_UNSIGNED_BYTE as GL_UNSIGNED_BYTE,
    GL_VENDOR as GL_VENDOR,
    GL_VERSION as GL_VERSION,
    SRGB_TEXTURE_FORMATS as SRGB_TEXTURE_FORMATS,
    TRACKER_EXT as TRACKER_EXT,
    TRACKER_ROLES as TRACKER_ROLES,
    _SRGB_TO_LINEAR as _SRGB_TO_LINEAR,
    _qmul as _qmul,
    _srgb_to_linear_lut as _srgb_to_linear_lut,
    anchor_paths as anchor_paths,
    apply_overlay_alpha as apply_overlay_alpha,
    effective_curvature as effective_curvature,
    euler_to_quaternion as euler_to_quaternion,
    format_needs_linearize as format_needs_linearize,
    layer_alpha_flags as layer_alpha_flags,
    layer_geometry as layer_geometry,
    linearize_rgb as linearize_rgb,
    pick_swapchain_format as pick_swapchain_format,
    rotate_vector as rotate_vector,
    should_rebuild as should_rebuild,
)

log = logging.getLogger("vlt.output.openxr_overlay")

_GL_BACKENDS: dict[str, type[_GlBackend]] = {"wayland": EglGlContext, "x11": XlibGlxContext}


def create_gl_context() -> _GlBackend:
    """挑一个能建起来的 GL 后端并返回（Wayland 优先，失败回退 X11）。

    选择顺序：
      * 环境里**两个**都设了（Wayland 会话带 XWayland）→ 先试 Wayland（niri 的
        xwayland-satellite 没有 GLX，Wayland 才是主路）；
      * 只有 DISPLAY（Xorg 会话）→ 走 X11/GLX；
      * 两个都没有 → 两条都试一遍，把两边的错误都报出来（便于排查）。
    可用 `VLT_OVERLAY_GL=wayland|x11` 强制指定（只试那一条，失败直接抛）。
    """
    import os
    forced = (os.environ.get("VLT_OVERLAY_GL") or "").strip().lower()
    if forced and forced not in _GL_BACKENDS:
        log.warning("[overlay:xr] VLT_OVERLAY_GL=%r 不认识（只认 wayland/x11）→ 忽略", forced)
        forced = ""
    if forced:
        order = [forced]
    else:
        order = []
        if os.environ.get("WAYLAND_DISPLAY"):
            order.append("wayland")
        if os.environ.get("DISPLAY"):
            order.append("x11")
        if not order:
            order = ["wayland", "x11"]

    errors: list[str] = []
    for kind in order:
        try:
            backend = _GL_BACKENDS[kind]()
        except Exception as exc:  # noqa: BLE001 — 换下一个后端；全失败时统一报
            errors.append(f"{kind}: {type(exc).__name__}: {exc}")
            log.warning("[overlay:xr] GL 后端 %s 建不起来（%s）→ 试下一个", kind, exc)
            continue
        log.info("[overlay:xr] GL 后端：%s", backend.name)
        return backend
    raise RuntimeError("GL 后端都建不起来：" + "；".join(errors))


# ================================================================ OpenXR 会话


def _test_process_guard() -> str | None:
    """防呆：**测试进程里拒绝建立 OpenXR 会话**。

    为什么要有这条：Overlay 会话是连到用户**正在用的** XR 合成器上的。
    调试期间反复建/断会话会直接干扰用户当时的 OpenXR 应用（实测被投诉过）。
    单元测试只需要验配置/渲染/纯逻辑，**不该**碰运行时 —— 真机验证应当是
    用户明确授权的单独一次，而不是「顺手跑一下测试」。

    正常使用（`python -m vlt.gui` / `-m vlt.app`）与专门的诊断脚本
    （`scripts/spike_openxr.py`，在 scripts/ 下）都不会命中这个判断。
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return None
    p = Path(path)
    if p.name.startswith("test_") or "tests" in p.parts:
        return f"检测到测试进程（{p.name}）→ 拒绝建立 OpenXR 会话"
    return None

class OpenXrOverlay(_XrMaintenance):
    """手腕屏的 OpenXR 后端。

    公开接口与 `openvr_overlay.WristOverlay`（Windows 侧后端）保持一致，
    所以 `engine.py` / `gui.py` 不需要知道自己在哪个平台：

        cfg / config_path / dry_run / available / frames_updated
        start() / update() / update_entries() / tick() / close()

    自愈分级（照搬 Windows 侧踩出来的思路）：
        贴图失败 → 重建 swapchain → 仍失败 → 重建整个 session → 再失败 → 重建 instance
    ⚠️ OpenXR 没有 `openvr.shutdown()` 那种「一行硬重启」，所以三级都要自己写。
    """

    HEARTBEAT_S = 30.0

    def __init__(self, cfg: OverlayConfig, config_path: Path | None = None,
                 dry_run: bool = False) -> None:
        from ..paths import APP_DIR
        self.cfg = cfg
        self.config_path = config_path
        self.dry_run = dry_run
        self.available = False
        self.frames_updated = 0
        self._gl: _GlBackend | None = None
        self._sess: XrOverlaySession | None = None
        self._last_render: tuple[str, str] | None = None
        self._last_entries: tuple | None = None
        self._last_cfg_mtime = 0.0
        self._frames_dir = APP_DIR / "out" / "overlay_frames"
        # 自愈状态机
        self._fails = 0
        self._stage = "none"            # none / swapchain / session / instance
        self._fails_in_stage = 0
        self._last_ok_at = 0.0
        self._last_heartbeat = 0.0
        self._rebuilds = 0
        self._reinits = 0
        # 「最近一次内容」缓存（热重载 / 自愈后重画用）
        self._last_render_cached: tuple[str, str] | None = None
        self._last_entries_cached: tuple | None = None
        # ★ 后台帧循环：OpenXR 的 composition layer 是**每帧**提交的（内容不变也要重提，
        #   否则面板会在静止几帧后消失 —— 实测「黑色长方形出现一下又没了」就是这个），
        #   而 `xrWaitFrame` 没有超时参数、session 未 running 时会**永久阻塞**
        #   （实测：连续提交会卡死，被 timeout 杀掉退出码 124）。
        #   两件事都不能发生在调用方的线程里（engine 是 asyncio 循环、GUI 是主线程），
        #   所以帧循环放独立线程，`update()` 只渲染并放到「待显示」槽，绝不阻塞。
        self._frame_thread: threading.Thread | None = None
        self._frame_stop = threading.Event()
        self._init_done = threading.Event()
        self._img_lock = threading.Lock()
        self._epoch = 0
        self._tracking = None
        self._content = self._last_approval = None
        self._pending_img: Any | None = None     # 新渲染的一帧（待上传）
        self._shown_img: Any | None = None       # 当前该显示的一帧（每帧重提用）
        # 「最后一次**有新内容**的时刻」——`fade_after_s` 超时后整层 alpha 归零
        # （Windows 侧是 `setOverlayAlpha(0)`，语义刻意保持一致）
        self._last_content_at = 0.0

    @property
    def anchor_tracked(self) -> bool | None:
        with self._img_lock:
            snapshot = self._tracking
            if (snapshot is None or snapshot[0] is not self._sess or self._frame_stop.is_set()
                    or snapshot[1:3] != (self.cfg.anchor, self.cfg.tracker_index)):
                return None
            return snapshot[3]

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """建立 overlay 会话。失败返回 False（调用方据此禁用这一条腿，别的功能不受影响）。"""
        if self._frame_thread is not None and self._frame_thread.is_alive():
            return self.available and not self._frame_stop.is_set()
        if not self.cfg.enabled:
            log.info("[overlay:xr] 配置里已禁用")
            return False
        if self.dry_run:
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            log.info("[overlay:xr][dry-run] 不连运行时，渲染结果写到 %s", self._frames_dir)
            return False
        blocked = _test_process_guard()
        if blocked:
            log.warning("[overlay:xr] %s（该功能不启用；测试不该碰用户的运行时）", blocked)
            return False
        # ★ 整条 XR/GL 生命线必须在**同一个线程**里：`eglMakeCurrent` 把 context 绑在
        #   调用它的线程上，换个线程再 glTexImage2D / xrWaitFrame 就是错的 —— 实测表现
        #   是「帧循环线程里 session 一直停在 READY(2)、提交报 SessionNotRunning」。
        self._frame_stop.clear()
        self._init_done = threading.Event()
        self._frame_thread = threading.Thread(target=self._xr_main, daemon=True, name="vlt-xr")
        self._frame_thread.start()
        if not self._init_done.wait(timeout=30.0):
            log.warning("[overlay:xr] ⚠️ 初始化超时（30s）——手腕屏已禁用，其它输出不受影响")
            self.close()
            return False
        return self.available

    def _xr_main(self) -> None:
        """XR 主循环：建 GL/session → 帧循环 → 清理，**全在这一个线程里**。"""
        try:
            self._gl = create_gl_context()
            self._bring_up(rebuild_gl=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] ⚠️ 建立 overlay 会话失败：%s: %s"
                        "（手腕屏已禁用，其它输出不受影响）", type(exc).__name__, exc)
            self._teardown(keep_gl=False)
            self._init_done.set()
            return
        self.available = True
        if self._frame_stop.is_set():
            self.available = False
            self._teardown(keep_gl=False)
            self._init_done.set()
            return
        log.info("[overlay:xr] ✅ 已挂到 %s（%sx%s，GL %s，会话状态 %s）",
                 self.cfg.anchor, self.cfg.size_px[0], self.cfg.size_px[1],
                 self._gl.gl_string(GL_RENDERER), self._sess.state)
        self._init_done.set()
        try:
            self._frame_loop()
        finally:
            self.available = False
            self._teardown(keep_gl=False)     # 同线程清理（GL context 线程绑定）
    def _frame_loop(self) -> None:
        """后台帧循环：泵事件 → 每帧同步动作 → 每帧重提「当前该显示的那一帧」。

        * **每帧**提交是硬要求：OpenXR 的 composition layer 不是持久对象，
          内容不变也要重提，否则面板静止几帧后就没了（实测到的现象）。
        * `xrWaitFrame` **没有超时**，session 未 running 时会永久阻塞 —— 所以只在
          session 至少 READY 时才进帧循环；这样即使运行时抽风，卡住的也只是这条
          daemon 线程，engine 的 asyncio 循环 / GUI 主线程不受影响。
        * `sync_actions` 也在每帧做：动作状态只有同步后才更新，只同步一次的话
          锚点会一直停在「未追踪」→ 面板固定在原点、不跟手也不跟头。
        """
        import xr
        while not self._frame_stop.is_set():
            sess = self._sess
            if sess is None:
                return
            self._reload_config_if_changed()      # 热重载也放这里（ensure_anchor 是 XR 调用）
            if self._frame_stop.is_set() or self._sess is not sess:
                continue
            try:
                sess.pump_events()
            except Exception:  # noqa: BLE001
                pass
            with self._img_lock:
                img = self._pending_img
                if img is not None:
                    self._pending_img = None
                    self._shown_img = img
                else:
                    img = self._shown_img
            if img is None:
                time.sleep(0.05)
                continue
            packet = img
            img, approval, epoch = packet
            def valid(who=None):
                return (epoch == self._epoch and not self._frame_stop.is_set()
                        and allowed(approval, who))
            state = sess.state
            if state is None or not (int(xr.SessionState.READY) <= int(state)
                                     <= int(xr.SessionState.FOCUSED)):
                time.sleep(0.05)        # 未 running 时绝不进 wait_frame（会吊死）
                continue
            try:
                if not valid():
                    sess.clear()
                    with self._img_lock:
                        if self._shown_img is packet:
                            self._shown_img = None
                    time.sleep(0.05)
                    continue
                with scope(valid):
                    result = sess.submit(img, self.cfg, self._layer_alpha())
            except Exception as exc:  # noqa: BLE001
                self._fails += 1
                self._fails_in_stage += 1
                if self._fails == 1:
                    log.warning("[overlay:xr] ❌ 提交层失败：%s: %s", type(exc).__name__, exc)
                if self._fails % 30 == 0:
                    log.warning("[overlay:xr] ❌ 已连续失败 %d 次（面板停在最后一帧）", self._fails)
                self._escalate()
                time.sleep(0.05)
                continue
            if not valid() or self._sess is not sess or result is False:
                continue
            anchor, index = self.cfg.anchor, self.cfg.tracker_index
            try:
                tracked = sess.anchor_tracked(anchor, index)
            except Exception:  # noqa: BLE001
                tracked = None
            with self._img_lock:
                self._tracking = (sess, anchor, index, tracked)
            self.frames_updated += 1
            self._last_ok_at = time.monotonic()
            if self._fails:
                log.info("[overlay:xr] ✅ 恢复上传（连续失败 %d 次，期间重建 %d 次、重开会话 %d 次）",
                         self._fails, self._rebuilds, self._reinits)
                self._fails = 0
                self._stage = "none"
                self._fails_in_stage = 0
            if self.frames_updated == 1 or self.frames_updated % 300 == 0:
                log.info("[overlay:xr] ← 面板已更新（第 %d 帧）", self.frames_updated)

    def _bring_up(self, *, rebuild_gl: bool) -> None:
        """（重新）建一套 XR 会话，并把当前内容画上去。"""
        if rebuild_gl or self._gl is None:
            if self._gl is not None:
                self._gl.close()
            self._gl = create_gl_context()
        self._sess = XrOverlaySession(self._gl, self.cfg.size_px)
        self._sess.create(self._extensions(self._gl.REQUIRED_EXTENSIONS))
        self._sess.ensure_anchor(self.cfg.anchor, self.cfg.tracker_index)
        # 先记下「当前内容」，重建后要照原样画回去（否则自愈后屏幕是空的）
        self._last_render = None
        self._last_entries = None
        self._repaint()
    def close(self) -> None:
        """请求 XR 线程退出并等它收尾。

        teardown 由**该线程自己**执行：GL context 是线程绑定的，换个线程销毁同样不安全。
        """
        self._frame_stop.set()
        with self._img_lock:
            self._epoch += 1
            self._tracking = None
            self._pending_img = self._shown_img = self._content = self._last_approval = None
            self._last_render = self._last_entries = None
            self._last_render_cached = self._last_entries_cached = None
            self.available = False
        th = self._frame_thread
        if th is not None and th.is_alive():
            th.join(timeout=5.0)
            if th.is_alive():
                log.warning("[overlay:xr] XR 线程未在 5s 内退出（可能卡在 wait_frame），放弃等待")
        if th is None or not th.is_alive():
            self._frame_thread = None

    def _teardown(self, *, keep_gl: bool) -> None:
        if self._sess is not None:
            self._sess.destroy()
            self._sess = None
        if not keep_gl and self._gl is not None:
            self._gl.close()
            self._gl = None

    # ---------- 内容 ----------
    def update(self, text: str, source: str = "", force: bool = False) -> None:
        """渲染一帧并交给后台帧循环 —— **立即返回，不阻塞调用方**。"""
        self._update_content('latest', (str(text), str(source)), render_panel, force)

    def update_entries(self, entries: list, force: bool = False) -> None:
        """渲染会话面板并交给后台帧循环 —— **立即返回，不阻塞调用方**。"""
        rows = tuple(tuple(row) for row in (entries or []))
        self._update_content('conversation', rows, render_conversation, force)

    def _repaint(self):
        content = self._content
        if content is None:
            return
        kind, data, approval, epoch = content
        with scope(approval):
            renderer = render_panel if kind == 'latest' else render_conversation
            self._update_content(kind, data, renderer, True, epoch)

    def _queue_frame(self, img, content=None) -> bool:  # noqa: ANN001
        """把一帧放进「待显示」槽（线程安全、立即返回）。

        真正的 `wait_frame → sync_actions → 上传 → end_frame` 由后台帧循环执行：
        `xrWaitFrame` 没有超时，绝不能让它跑在 engine 的事件循环或 GUI 主线程上。
        """
        approval, epoch = (content[2], content[3]) if content else (current(), self._epoch)
        if not allowed(approval):
            return False
        img = img.copy()
        if not allowed(approval):
            return False
        with self._img_lock:
            if epoch != self._epoch or self._frame_stop.is_set():
                return False
            if content is not None and self._content is not content:
                return False
            self._pending_img = (img, approval, epoch)
            self._last_content_at = time.monotonic()
        return True

    def _write_demo(self, img) -> None:  # noqa: ANN001
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        out = self._frames_dir / "latest.png"
        img.save(out)
        log.info("[overlay:xr][dry-run] 已写出 %s", out)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="OpenXR 手腕屏后端（冒烟自检）")
    ap.add_argument("--smoke", type=float, default=15.0, metavar="秒",
                    help="建会话并持续提交示例面板，默认 15 秒")
    ap.add_argument("--alpha-test", action="store_true",
                    help="改提交「透明边距 + 25/50/75/100%% 半透明块 + 实心白字 + "
                         "0/12/32/64/128/192/255 灰阶带」判定图，"
                         "用来肉眼验收通透性与颜色空间（黑不下去 = 双重 gamma）")
    args = ap.parse_args()
    raise SystemExit(_smoke(args.smoke, args.alpha_test))
