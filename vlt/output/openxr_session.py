"""OpenXR session, swapchain and frame submission."""
from __future__ import annotations
import ctypes
import logging
import time
from typing import Any
from ..session.chatgpt_output import allowed, current
from .overlay import OverlayConfig
from .openxr_gl import _GlBackend
from .openxr_actions import _XrActions
from .openxr_model import (
    CYLINDER_EXT,
    apply_overlay_alpha,
    effective_curvature,
    euler_to_quaternion,
    format_needs_linearize,
    layer_alpha_flags,
    layer_geometry,
    linearize_rgb,
    pick_swapchain_format,
    rotate_vector,
)

log = logging.getLogger("vlt.output.openxr_overlay")

class XrError(RuntimeError):
    """OpenXR 调用失败（带上是哪一步，便于日志定位）。"""


class XrOverlaySession(_XrActions):
    """一套「overlay session + swapchain + 每帧提交」的最小封装。

    刻意做成薄封装：所有 xr 调用集中在这里，`OpenXrOverlay` 只管业务
    （渲染、热重载、自愈），两层职责分明。
    """

    def __init__(self, gl: "_GlBackend", size_px: tuple[int, int]) -> None:
        self._gl = gl
        self.size_px = size_px
        self.instance: Any = None
        self.system_id: Any = None
        self.session: Any = None
        self.swapchain: Any = None
        self.textures: list[int] = []
        self.format = 0
        self.ref_space: Any = None          # LOCAL
        self.view_space: Any = None         # VIEW（anchor=hmd 用）
        self.action_set: Any = None
        # 锚点 → 动作/空间。**一次建全**：动作集一旦 attach 就变成 immutable，
        # 之后 `xrCreateAction` / `xrSuggestInteractionProfileBindings` /
        # `xrAttachSessionActionSets` 一律返回 XR_ERROR_ACTIONSETS_ALREADY_ATTACHED
        # （规范原文见 `_ensure_actions`）。所以切锚点**不能**再建动作，只能换用哪个 space。
        self._actions: dict[tuple[str, int], Any] = {}
        self._spaces: dict[tuple[str, int], Any] = {}
        self._actions_ready = False
        self._anchor_key: tuple[str, int] | None = None
        self._frame_state: Any = None
        # 「整层 alpha + 转字节」的记忆化缓存：(帧, alpha, bytes)。
        # 帧循环每帧重提同一张图，没有它就要每帧白跑一次 LUT。
        self._prep: tuple[Any, float, int, bytes] | None = None
        # 本会话**实际启用**的扩展名（`create()` 传进来的那批）。`submit()` 靠它判断
        # 运行时到底支不支持柱面层，不支持就退回平面（见 `effective_curvature()`）。
        self.extensions: list[str] = []
        self._cylinder_warned = False        # 柱面降级只在第一次留痕，不每帧刷日志
        # 会话状态（IDLE→READY→SYNCHRONIZED→VISIBLE→FOCUSED）。**很关键**：
        # `xrSyncActions` 在非 FOCUSED 时会直接抛 `XR_ERROR_SESSION_NOT_FOCUSED`
        # （Monado 源码 oxr_input.c 明写 "Can only call this function if the session
        #  state is focused"），所以提交前要看一眼它。
        self.state: Any = None

    # ---------- 建立 / 销毁 ----------
    def create(self, extensions: list[str]) -> None:
        """建 instance → system → overlay session（三层都建，任一层失败就抛）。"""
        import xr
        self._xr = xr
        self.extensions = list(extensions)   # 记下真正启用的那批（submit 判断柱面能力用）
        self.instance = xr.create_instance(xr.InstanceCreateInfo(
            application_info=xr.ApplicationInfo(application_name="VRChat LiveTranslate",
                                                application_version=1),
            enabled_extension_names=extensions))
        self.system_id = xr.get_system(self.instance)
        self._begin_session()
        self._create_ref_spaces()
        self._create_swapchain()
        log.info("[overlay:xr] 会话就绪：%sx%s format=0x%x（%s），扩展 %d 个",
                 self.size_px[0], self.size_px[1], self.format,
                 "线性纹理 → 像素已预线性化"
                 if format_needs_linearize(self.format)
                 else "sRGB 纹理 → 采样时硬件解码",
                 len(extensions))

    def _begin_session(self) -> None:
        """建 overlay session 并推进到 running。"""
        import xr
        overlay_info = xr.SessionCreateInfoOverlayEXTX(
            create_flags=xr.OverlaySessionCreateFlagsEXTX(0),   # 规范要求必须 0
            session_layers_placement=100)
        binding = self._gl.binding()
        # 链顺序：SessionCreateInfo → GraphicsBinding → Overlay（顺序不能反）
        binding.next = ctypes.cast(ctypes.pointer(overlay_info), ctypes.c_void_p)
        create_info = xr.SessionCreateInfo(system_id=self.system_id)
        create_info.next = ctypes.cast(ctypes.pointer(binding), ctypes.c_void_p)

        # ⚠️ 必须先查图形需求：Monado 的 EGL 分支检查 `gotten_requirements`
        xr.get_opengl_graphics_requirements_khr(self.instance, self.system_id)
        self.session = xr.create_session(self.instance, create_info)

        # ⚠️ 建完还要泵事件到 READY 才能 begin（否则 SESSION_NOT_RUNNING）
        deadline = time.monotonic() + 5.0
        state = None
        while time.monotonic() < deadline:
            s = self.pump_events()
            if s is not None:
                state = s
            if state == xr.SessionState.READY:
                break
            if state in (xr.SessionState.EXITING, xr.SessionState.LOSS_PENDING):
                raise XrError(f"会话进入 {state}")
            time.sleep(0.01)
        else:
            raise XrError(f"等 READY 超时（当前 {state}）")

        configs = list(xr.enumerate_view_configurations(self.instance, self.system_id))
        want = xr.ViewConfigurationType.PRIMARY_STEREO
        vct = want if want in configs else (configs[0] if configs else want)
        xr.begin_session(self.session, xr.SessionBeginInfo(primary_view_configuration_type=vct))
        for _ in range(50):
            self.pump_events()
            time.sleep(0.01)

    def _create_ref_spaces(self) -> None:
        # ⚠️ `create_reference_space` 的第一个参数是 **session**，不是 instance。
        #    写错时错误信息很不直观（`expected Session instance instead of Instance`），
        #    而且参数**个数**是对的、静态扫不出来 —— 这条是真机冒烟才抓到的。
        #    `tests/test_openxr_overlay.py` 里有一条专门扫 self.* 句柄错配的用例守着它。
        import xr
        self.ref_space = xr.create_reference_space(self.session, xr.ReferenceSpaceCreateInfo(
            reference_space_type=xr.ReferenceSpaceType.LOCAL))
        self.view_space = xr.create_reference_space(self.session, xr.ReferenceSpaceCreateInfo(
            reference_space_type=xr.ReferenceSpaceType.VIEW))

    def _create_swapchain(self) -> None:
        import xr
        self.swapchain = None
        formats = list(xr.enumerate_swapchain_formats(self.session))
        self.format = pick_swapchain_format(formats)
        if format_needs_linearize(self.format):
            # 降级**不许静默**：这条说明运行时没给 sRGB 的 8 位格式，我们靠预线性化
            # 兜的正确性（暗端会有 ±2/255 的量化误差，不是错色）。
            log.warning("[overlay:xr] ⚠️ 运行时没提供 sRGB 的 8 位 RGBA 交换链格式"
                        "（可用：%s）→ 用 format=0x%x（线性）并在上传前预线性化像素",
                        [hex(int(f)) for f in formats[:12]], self.format)
        w, h = self.size_px
        self.swapchain = xr.create_swapchain(self.session, xr.SwapchainCreateInfo(
            format=self.format, sample_count=1, width=w, height=h,
            face_count=1, array_size=1, mip_count=1))
        imgs = xr.enumerate_swapchain_images(self.swapchain, xr.SwapchainImageOpenGLKHR)
        self.textures = [int(i.image) for i in imgs]

    def rebuild_swapchain(self) -> None:
        """贴图上传反复失败时的第一级恢复。"""
        import xr
        if self.swapchain is not None:
            try:
                xr.destroy_swapchain(self.swapchain)
            except Exception:  # noqa: BLE001
                pass
        self._create_swapchain()

    def set_size(self, size_px: tuple[int, int]) -> None:
        """改面板像素尺寸：更新尺寸并**重建交换链**（「面板高」滑块热重载用）。

        ⚠️ 只改配置、不动交换链的话：新面板图比交换链大 → `glTexSubImage2D` 越界
        （GL_INVALID_VALUE，整张不写）+ `SwapchainSubImage.image_rect` 越界
        （`XR_ERROR_SWAPCHAIN_RECT_INVALID`）→ 每帧 `xrEndFrame` 都失败。
        而自愈重建用的还是**本对象的旧尺寸** → 面板永久卡死（用户实测：
        430 → 440 一步就触发，日志连刷 232 次「已重建 swapchain」也不恢复，
        只能重启应用）。同尺寸调用幂等（不白重建）。
        """
        size_px = (max(1, int(size_px[0])), max(1, int(size_px[1])))
        if size_px == tuple(self.size_px):
            return
        self.size_px = size_px
        if self.swapchain is not None:
            self.rebuild_swapchain()

    # ---------- 锚点 ----------

    # 手部锚点建议绑定的交互 profile。只给一个的话，控制器型号对不上就完全没有 pose。
    def pump_events(self) -> Any:
        """把事件队列取空，返回最后见到的 session state。

        ⚠️ 必须从**缓冲区起始**强转（不是 `varying`）：`EventDataBuffer.varying` 在偏移 16，
        而 `EventDataSessionStateChanged.session/state` 在 16/24 —— 从 varying 起算会偏 16 字节，
        读出来的 state 是垃圾（实测得到 0=UNKNOWN，害我误判成「收不到事件」）。
        """
        import xr
        state = None
        want = int(xr.StructureType.EVENT_DATA_SESSION_STATE_CHANGED)
        while True:
            try:
                ev = xr.poll_event(self.instance)
            except Exception:  # noqa: BLE001 — XR_EVENT_UNAVAILABLE：队列空了
                break
            try:
                etype = int(ev.type)
            except Exception:  # noqa: BLE001
                break
            if etype == want:
                ssc = ctypes.cast(ctypes.pointer(ev),
                                  ctypes.POINTER(xr.EventDataSessionStateChanged)).contents
                try:
                    state = xr.SessionState(int(ssc.state))
                    self.state = state
                except ValueError:
                    pass
        return state

    def submit(self, image: Any, cfg: OverlayConfig,
               alpha: float | None = None) -> bool:
        """Submit only the original approved frame; balance rejection with an empty frame."""
        import xr
        approval = current()
        session, swapchain = self.session, self.swapchain
        def valid():
            return (self.session is session and self.swapchain is swapchain
                    and allowed(approval))
        if not valid():
            return False
        w, h = image.size
        self.pump_events()
        frame = xr.wait_frame(session)
        self._frame_state = frame
        xr.begin_frame(session)
        acquired = waited = ended = False
        try:
            if not valid():
                return False
            idx = xr.acquire_swapchain_image(swapchain)
            acquired = True
            xr.wait_swapchain_image(swapchain, xr.SwapchainImageWaitInfo(timeout=1_000_000_000))
            waited = True
            if self.action_set is not None and self.state is not None \
                    and int(self.state) >= int(xr.SessionState.SYNCHRONIZED):
                # ⚠️ 动作必须**每帧**同步。只同步一次、或只在 FOCUSED 时才同步，pose 会一直停在
                #    「未追踪」→ 层落到 LOCAL 原点：实测表现正是「黑色长方形固定不动、不跟手也不跟头」。
                #    非 FOCUSED 时 Monado 可能抛 `XR_ERROR_SESSION_NOT_FOCUSED`，吃掉即可
                #    （同步失败不该让整帧作废）。
                try:
                    xr.sync_actions(self.session, xr.ActionsSyncInfo(active_action_sets=[
                        xr.ActiveActionSet(action_set=self.action_set,
                                           subaction_path=xr.Path(0))]))
                except Exception as exc:  # noqa: BLE001
                    log.debug("[overlay:xr] sync_actions 失败（本帧不同步）：%s: %s",
                              type(exc).__name__, exc)
            rgba = self._prepared(image, cfg.alpha if alpha is None else alpha)
            if not valid():
                return False
            uploaded = self._gl.upload(self.textures[idx], w, h, rgba)
            if uploaded is False or not valid():
                return False
            acquired = False  # Unknown release outcome must not be retried.
            xr.release_swapchain_image(swapchain)
            if not valid():
                return False
            sub = xr.SwapchainSubImage(
                swapchain=self.swapchain,
                image_rect=xr.Rect2Di(offset=xr.Offset2Di(0, 0),
                                      extent=xr.Extent2Di(w, h)))
            curv = effective_curvature(cfg.curvature, self.extensions)
            if cfg.curvature > 0.0 and curv == 0.0 and not self._cylinder_warned:
                self._cylinder_warned = True        # 只留一次痕，别每帧刷
                log.warning("[overlay:xr] ⚠️ 运行时没有 %s 扩展 → 弯曲度 %.2f 退回平面层"
                            "（面板仍在，只是不弯；要弯曲请改用支持该扩展的运行时）",
                            CYLINDER_EXT, cfg.curvature)
            geo = layer_geometry(cfg.width_m, w / h if h else 1.0, curv)
            # ★ 柱面层的 pose 是**圆柱的轴（圆心）**，不是面板位置：不补偿的话圆心会落在
            #   用户设的位置、而可见弧面整体沿局部 −Z 漂出 radius 远（实测现象就是
            #   「圆点跑到我设的位置、面板飘走了」）。把轴沿面板局部 +Z 挪 radius，
            #   弧面中点就回到与平面层相同的位置 —— 也就是 Windows/SteamVR 的观感
            #   （中心不动，两侧朝你卷）。
            pos = cfg.pos
            if geo["kind"] == "cylinder":
                ox, oy, oz = rotate_vector(cfg.rot, geo["pose_offset"])
                pos = (pos[0] + ox, pos[1] + oy, pos[2] + oz)
            pose = xr.Posef(
                orientation=xr.Quaternionf(*euler_to_quaternion(cfg.rot)),
                position=xr.Vector3f(*pos))
            space = self.anchor_space(cfg.anchor, cfg.tracker_index)
            # ★ 两个 flag 缺一不可：BLEND 让贴图的 alpha 真的生效（否则整层不透明 →
            #   蓝框外一圈黑边），UNPREMULTIPLIED 声明我们给的是未预乘 alpha
            #   （PIL 的语义，与 Windows 侧给 SteamVR 的一致）。见 layer_alpha_flags()。
            if geo["kind"] == "cylinder":
                layer = xr.CompositionLayerCylinderKHR(
                    layer_flags=layer_alpha_flags(), sub_image=sub, pose=pose, space=space,
                    radius=geo["radius"], central_angle=geo["central_angle"],
                    aspect_ratio=geo["aspect_ratio"])
            else:
                layer = xr.CompositionLayerQuad(
                    layer_flags=layer_alpha_flags(), sub_image=sub, pose=pose, space=space,
                    size=xr.Extent2Df(*geo["size"]),
                    eye_visibility=xr.EyeVisibility.BOTH)
            ended = True
            return self._end_frame(session, frame, layer)
        finally:
            try:
                if acquired and waited:
                    xr.release_swapchain_image(swapchain)
            finally:
                if not ended:
                    self._end_frame(session, frame)

    def clear(self):
        """Retire a revoked layer without acquiring or uploading its cached pixels."""
        import xr
        session = self.session
        if session is None:
            return False
        frame = xr.wait_frame(session)
        xr.begin_frame(session)
        self._end_frame(session, frame)
        return True

    def _end_frame(self, session, frame, layer=None):
        import xr
        arr = None
        if layer is not None:
            base_t = ctypes.POINTER(xr.CompositionLayerBaseHeader)
            arr = (base_t * 1)(ctypes.cast(ctypes.pointer(layer), base_t))
        info = xr.FrameEndInfo(display_time=frame.predicted_display_time,
            environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE,
            layer_count=1 if arr is not None else 0, layers=arr)
        if layer is not None and (self.session is not session or not allowed(current())):
            info = xr.FrameEndInfo(display_time=frame.predicted_display_time,
                environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE, layer_count=0, layers=None)
            layer = None
        xr.end_frame(session, info)
        return layer is not None

    def _prepared(self, image: Any, alpha: float) -> bytes:
        """按 `(帧, 整层 alpha, 交换链格式)` 记忆化「乘 alpha → 可选线性化 → 转字节」。

        帧循环**每帧都要重提同一张图**（OpenXR 的 composition layer 不是持久对象），
        所以这里必须缓存，否则每帧白过一次 1024x440 的 LUT。缓存里**持有这帧的
        引用**，`id()` 就不可能被回收复用，键也就不会撞车。

        格式也进键：`rebuild_swapchain()` 若协商到不同格式，线性化与否会变
        （见 `format_needs_linearize()`），拿旧字节贴上去颜色就错了。
        """
        k = max(0.0, min(1.0, float(alpha)))
        cached = self._prep
        if cached is not None and cached[0] is image and cached[1] == k \
                and cached[2] == self.format:
            return cached[3]
        img = apply_overlay_alpha(image, k)
        if format_needs_linearize(self.format):
            img = linearize_rgb(img)     # 只动 RGB；alpha 由上面的乘子决定
        data = img.tobytes()
        self._prep = (image, k, self.format, data)
        return data

    def destroy(self) -> None:
        """幂等销毁（顺序：swapchain → session → instance）。"""
        import xr
        for attr, fn in (("swapchain", "destroy_swapchain"), ("session", "destroy_session"),
                         ("instance", "destroy_instance")):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                getattr(xr, fn)(obj)
            except Exception:  # noqa: BLE001
                pass
            setattr(self, attr, None)
