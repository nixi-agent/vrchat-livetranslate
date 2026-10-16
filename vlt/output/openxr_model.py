"""OpenXR geometry, color processing and GL constants."""
from __future__ import annotations
import math
from typing import Any
from .overlay import OverlayConfig

# HTC Vive tracker 的 role 路径表（OpenXR 按 **role** 寻址 tracker，不像 OpenVR 那样按设备索引）。
# ⚠️ 未实测（spike 时用户用的是手柄不是 tracker）—— 所以 `tracker` 锚点在 Linux 上
#    是「按 role 顺序找一个能追踪的」，与 Windows 的「第 N 个 GenericTracker」语义略有差别。
TRACKER_ROLES = (
    "right_wrist", "left_wrist", "right_elbow", "left_elbow",
    "chest", "waist", "right_foot", "left_foot",
)


def euler_to_quaternion(rot_deg: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """欧拉角（度）→ 四元数 `(x, y, z, w)`。

    ⚠️ **顺序必须与 Windows 侧后端的 `openvr_overlay.build_matrix` 完全一致**（`Rz * Ry * Rx`），
    否则用户实测调好的 `rot: [-47, -16, 0]` 在 Linux 上会转到别的方向 ——
    配置语义必须两端一致，不然「同一个 config.yaml 两边都好看」就不成立了。

    实现上直接用四元数乘法 `qz ⊗ qy ⊗ qx`，比手写展开可靠。
    """
    rx, ry, rz = (math.radians(a) / 2.0 for a in rot_deg)
    qz = (math.cos(rz), 0.0, 0.0, math.sin(rz))     # (w, x, y, z)
    qy = (math.cos(ry), 0.0, math.sin(ry), 0.0)
    qx = (math.cos(rx), math.sin(rx), 0.0, 0.0)
    w, x, y, z = _qmul(_qmul(qz, qy), qx)
    return (x, y, z, w)


def _qmul(a: tuple[float, float, float, float],
          b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """四元数乘（Hamilton，`(w, x, y, z)` 顺序）。"""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def anchor_paths(anchor: str, tracker_index: int = 0) -> tuple[str | None, str | None]:
    """配置里的 `anchor` → (完整 pose 路径, 顶层用户路径)。

    返回 `(None, None)` 表示该锚点不用 action（`hmd` 用 VIEW 参考空间）。
    """
    if anchor == "left_hand":
        return "/user/hand/left/input/grip/pose", "/user/hand/left"
    if anchor == "right_hand":
        return "/user/hand/right/input/grip/pose", "/user/hand/right"
    if anchor == "tracker":
        idx = max(0, min(int(tracker_index), len(TRACKER_ROLES) - 1))
        base = f"/user/vive_tracker_htcx/role/{TRACKER_ROLES[idx]}"
        return f"{base}/input/grip/pose", base
    return None, None                      # hmd / 未知 → 用 VIEW 参考空间


# GL 内部格式（OpenXR 的 GL swapchain 格式就是 GL internal format）。
GL_RGBA8 = 0x8058               # UNORM：采样时**不做** sRGB→线性 解码
GL_SRGB8_ALPHA8 = 0x8C43        # sRGB：采样时硬件解码
GL_RGB10_A2 = 0x8059
GL_SRGB8 = 0x8C41               # sRGB（无 alpha，仅用于判定，不选它当交换链）

# 「采样时硬件会做 sRGB→线性 解码」的格式。判定按**白名单**而不是黑名单：
# 将来运行时冒出别的 sRGB 变体，宁可保守地按线性处理（多转一次是安全的，少转才发灰）。
SRGB_TEXTURE_FORMATS = (GL_SRGB8_ALPHA8, GL_SRGB8)


def format_needs_linearize(fmt: int) -> bool:
    """该交换链格式的纹素会不会被硬件自动做 sRGB→线性 解码；不会 → 我们要自己转。

    ⚠️ 这不是「可选优化」，是**颜色正确性**：`render_panel` 出来的是 sRGB 编码的
    8 位像素（PIL 语义，与 Windows 侧喂给 SteamVR 的同一份），而合成器（Monado
    `layer.comp` / WiVRn `layer_squasher.cpp`）按**线性**合成、输出前再
    `from_linear_to_srgb()` 编码一次。格式选成线性（`0x8058`）时没人解码我们的
    字节 → 等于拿 sRGB 值当线性值用 → 输出端再编码 = **双重 gamma**：
    暗部被抬到 5 倍（12 → 61、32 → 99），亮端几乎不动（255 → 255）。

    用户实测的现象就是**面板黑不下去**：底板 (12,14,20) 在 72.4% 覆盖下，
    双重 gamma 的**理论地板**是 (52,56,67)（背后不管多黑都到不了黑），
    而头显内截图实测 (51,56,68) 正好压在地板上。SteamVR 侧（`setOverlayRaw`
    的 RGBA8 被 SteamVR 当 sRGB）没有这个问题，所以「两端观感不一致」。
    """
    return int(fmt) not in SRGB_TEXTURE_FORMATS


def pick_swapchain_format(formats: list[int]) -> int:
    """挑一个 8 位 RGBA 的 GL 内部格式 —— **优先 sRGB 变体**。

    两条约束一起管：

    1. **必须 8 位**：不能直接取 `formats[0]` —— 实测列表里第一个是 0x805b（RGBA16F），
       而我们按 8 位上传 → 画面会错位。所以候选只有 8 位的三个。
    2. **优先 sRGB**：见 `format_needs_linearize()` 的说明，选线性格式会双重 gamma。
       顺序 sRGB8_ALPHA8 → RGBA8 → RGB10_A2；运行时只给线性格式时也照用，
       由上传路径（`XrOverlaySession._prepared()`）预线性化兜住正确性。
    """
    for cand in (GL_SRGB8_ALPHA8, GL_RGBA8, GL_RGB10_A2):
        if cand in formats:
            return cand
    return formats[0] if formats else GL_SRGB8_ALPHA8


def _srgb_to_linear_lut() -> list[int]:
    """sRGB 8 位 → 线性 8 位 的 256 项 LUT（`Image.point()` 用）。

    用 8 位线性装线性值是**有损**的（线性空间暗处台阶大），但误差只出现在暗端
    且 ≤2/255：`12 → 1/255`，合成器输出时编回 13（真值 12）；`32 → 4/255` → 34。
    对「面板黑不下去」这个问题来说，13 和 12 已经肉眼无差；真要更准只能换 16 位
    上传，不值得为一条兜底路径把上传路径改复杂（首选路径是 sRGB 交换链，零损失）。
    """
    out = []
    for v in range(256):
        x = v / 255.0
        lin = x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
        out.append(round(255.0 * lin))
    return out


_SRGB_TO_LINEAR = _srgb_to_linear_lut()


def linearize_rgb(img: Any) -> Any:
    """把 sRGB 编码的 **RGB** 通道转成线性；alpha 通道一动不动。

    ⚠️ 只动 RGB：预乘/未预乘的语义全在 alpha 上，动 alpha 会把
    `layer_alpha_flags()` 那套（`UNPREMULTIPLIED_ALPHA_BIT`）搞坏。
    调用方只在 `format_needs_linearize(self.format)` 为真时走这里。
    """
    from PIL import Image

    if img.mode != "RGBA":
        img = img.convert("RGBA")
    r, g, b, a = img.split()
    lut = _SRGB_TO_LINEAR
    return Image.merge("RGBA", (r.point(lut), g.point(lut), b.point(lut), a))


def layer_alpha_flags() -> Any:
    """合成层的 alpha flag —— **必须设**，否则整层被当成不透明。

    规范里图层默认按「alpha = 1.0」合成，只有设了 `BLEND_TEXTURE_SOURCE_ALPHA_BIT`
    才会去读贴图的 alpha。少了它的**实测现象**正是「面板蓝框外面多出一圈不透明黑边」：
    贴图里 alpha=0 的 12px 边条 + 圆角外的三角区（占 7.5% 像素）被当成实心黑画出来，
    底板自己的半透明（`bg_alpha`）也一起失效。

    还要带 `UNPREMULTIPLIED_ALPHA_BIT`：PIL 出来的是**未预乘**（straight）alpha，
    而运行时的默认假设是**预乘**。少了它，半透明像素会被当成预乘值 → 观感偏亮、
    白字顶到 255（Khronos 官方 `hello_xr` 就是这两条一起用；Windows 侧同理，
    我们不设 `VROverlayFlags_IsPremultiplied`，SteamVR 就按未预乘合成）。
    """
    import xr
    f = xr.CompositionLayerFlags
    return f.BLEND_TEXTURE_SOURCE_ALPHA_BIT | f.UNPREMULTIPLIED_ALPHA_BIT


def apply_overlay_alpha(img: Any, alpha: float) -> Any:
    """整层 alpha 乘子 —— Linux 侧的 `setOverlayAlpha()`（Windows 能力的对等物）。

    ⚠️ **只乘 alpha 通道，RGB 一动不动**。SteamVR 的 `SetOverlayAlpha` 就是这么做的
    （它只作用于「图层 alpha」），所以效果是「底板变淡、文字该多亮还多亮」。
    要是连 RGB 一起乘（那是**预乘空间**的算法），白字会先变灰再变暗 ——
    而用户要的恰恰是「界面半透明、文字不透明」。

    用 256 项 LUT 一趟算完（纯 C 速度）；调用方（帧循环）还会记忆化，
    正常情况下一帧渲染只过这一次。
    """
    k = max(0.0, min(1.0, float(alpha)))
    if k >= 1.0:
        return img
    lut = [round(i * k) for i in range(256)]
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lut))
    return out


def rotate_vector(rot_deg: tuple[float, float, float],
                  v: tuple[float, float, float]) -> tuple[float, float, float]:
    """把**面板局部**的向量 `v` 按面板的 `rot` 转到父空间。

    ⚠️ 柱面层的 pose 偏移必须在**面板自己的坐标系**里做：直接拿世界系的 Z 会在面板
    转过去之后指到完全不同的方向（手腕屏默认 `rot=[-47,-16,0]`，世界 Z 与面板法线
    差了几十度，面板会整块歪出去）。用的是与 `euler_to_quaternion` 同一套 Rz·Ry·Rx 约定。
    """
    x, y, z, w = euler_to_quaternion(rot_deg)
    vx, vy, vz = v
    # v' = v + 2w(q×v) + 2q×(q×v)
    cx, cy, cz = y * vz - z * vy, z * vx - x * vz, x * vy - y * vx
    c2x, c2y, c2z = y * cz - z * cy, z * cx - x * cz, x * cy - y * cx
    return (vx + 2.0 * (w * cx + c2x),
            vy + 2.0 * (w * cy + c2y),
            vz + 2.0 * (w * cz + c2z))


def layer_geometry(width_m: float, aspect: float, curvature: float) -> dict:
    """把「面板宽 + 宽高比 + 弯曲度」换算成合成层参数。

    `curvature` 沿用 Windows 侧 `SetOverlayCurvature` 的语义（openvr.h 原文：
    「curvature 是占整圆的比例，1 = 完全闭合的圆柱；给定半径时
    curvature = overlay.width / (2π·r)」）：

        central_angle = 2π · curvature        radius = width_m / central_angle

    即 **width_m 是弧长**（弯曲 0.5 = 180° 时，看到的弦长只有弧长的 64% —— 与 Windows 一致）。

    ⚠️ 半径**不能**直接当层的位置用：OpenXR 柱面层里 `pose` 是**圆柱的轴（圆心）**，
    可见弧面在 pose 局部 −Z 方向、距原点 `radius` 处（Monado `layer_cylinder.vert`：
    `x = sin(a)·r`、`z = −cos(a)·r` ⇒ 表面点满足 x²+z²=r²，轴过 pose 原点）。
    所以这里一并给出 **pose_offset**（面板局部 +Z 上挪 radius），
    由调用方用面板的 rot 转过去加到位置上 —— 见 `submit()` 与 `rotate_vector()`。
    """
    aspect = aspect if aspect > 0 else 1.0
    # 小到这个程度就按平面处理：半径已经是宽度的 1/(2πc) 倍（c=0.005 → r≈32×宽），
    # 肉眼与平面无异，再小只会让半径往几百米上飙（数值上没必要，规范里 inf 也是「无限柱」）。
    if curvature <= 0.005:
        return {"kind": "quad", "size": (width_m, width_m / aspect)}
    angle = min(2.0 * math.pi - 1e-3, 2.0 * math.pi * curvature)
    return {"kind": "cylinder", "radius": width_m / angle,
            "central_angle": angle, "aspect_ratio": aspect,
            "pose_offset": (0.0, 0.0, width_m / angle)}


# 柱面合成层是 Khronos 扩展（`XR_KHR_composition_layer_cylinder`），**并非所有运行时
# 都提供**。运行时没有它、却还构造 `CompositionLayerCylinderKHR` 的话，`xrEndFrame`
# 会整帧失败 → 手腕屏这条腿整个没了。所以按「运行时实际启用的扩展」决定用不用柱面，
# 拿不到就退回平面层，并在调用点留一行 `[overlay:xr]`（降级不许静默）。
CYLINDER_EXT = "XR_KHR_composition_layer_cylinder"

# HTC Vive Tracker 的交互 profile 是**扩展**（`XR_HTCX_vive_tracker_interaction`）：
# `/user/vive_tracker_htcx/role/...` 这族路径只有在该扩展启用时才存在，否则
# `xrStringToPath` 直接 `XR_ERROR_PATH_UNSUPPORTED`（实测：没接 tracker 的 WiVRn/Monado
# 就是这样）。所以「要不要建 tracker 锚点」按**启用的扩展**判断，而不是撞上去看报错。
TRACKER_EXT = "XR_HTCX_vive_tracker_interaction"


def effective_curvature(curvature: float, extensions: list[str]) -> float:
    """按**运行时实际启用**的扩展决定这一帧用不用柱面层。

    没有 `XR_KHR_composition_layer_cylinder` 时把 curvature 归 0 —— `layer_geometry()`
    据此返回平面 `quad`。用户看到的仍是不弯的面板，而不是整条手腕屏消失。
    """
    return curvature if CYLINDER_EXT in extensions else 0.0


def should_rebuild(old: OverlayConfig, new: OverlayConfig) -> tuple[bool, bool]:
    """配置热重载时判断要做什么：返回 `(几何/锚点变了, 渲染参数变了)`。

    几何变了要重应用变换（位置/宽/透明度/弯曲/锚点）；
    渲染参数变了必须**重新渲染一帧贴图**，否则界面上拖字号滑块会「看着生效、屏上没变」。

    与 `openvr_overlay.WristOverlay.tick()` 里的判断口径保持一致（刻意重复这两组比较，
    不动那边已经过测试的代码）。
    """
    geo = (new.pos, new.rot, new.width_m, new.alpha, new.curvature) != (
        old.pos, old.rot, old.width_m, old.alpha, old.curvature)
    anchor = (new.anchor != old.anchor or new.tracker_index != old.tracker_index)
    render = (new.font_size != old.font_size
              or new.source_font_size != old.source_font_size
              or new.size_px != old.size_px
              or new.max_lines != old.max_lines
              or new.show_source != old.show_source
              # ↓ 这些**只影响贴图像素**（底板/原文/边框/分隔线/两端色条）：
              #   不重渲的话，界面上拖「底板不透明度/原文不透明度」就只是看着生效。
              or new.bg_alpha != old.bg_alpha
              or new.source_alpha != old.source_alpha
              or new.border_alpha != old.border_alpha
              or new.separator != old.separator
              or new.color_bg != old.color_bg
              or new.color_border != old.color_border
              or new.color_source != old.color_source
              or new.color_translation != old.color_translation
              or new.color_mine != old.color_mine
              or new.color_theirs != old.color_theirs)
    return (geo or anchor), render


# ================================================================ GL / EGL（Wayland surfaceless）

EGL_PLATFORM_WAYLAND_KHR = 0x31D8
EGL_OPENGL_API = 0x30A2
EGL_NONE = 0x3038
EGL_SURFACE_TYPE, EGL_PBUFFER_BIT = 0x3033, 0x0001
EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT = 0x3040, 0x0008
EGL_RED_SIZE, EGL_GREEN_SIZE, EGL_BLUE_SIZE, EGL_ALPHA_SIZE = 0x3024, 0x3023, 0x3022, 0x3021
EGL_CONTEXT_MAJOR_VERSION, EGL_CONTEXT_MINOR_VERSION = 0x3098, 0x30FB
EGL_NO_SURFACE = 0
GL_TEXTURE_2D, GL_RGBA, GL_UNSIGNED_BYTE = 0x0DE1, 0x1908, 0x1401
GL_VENDOR, GL_RENDERER, GL_VERSION = 0x1F00, 0x1F01, 0x1F02


# GLX 属性（取值与 /usr/include/GL/glx.h、glxext.h 一致；不要手写魔数到调用点）
GLX_RED_SIZE, GLX_GREEN_SIZE, GLX_BLUE_SIZE, GLX_ALPHA_SIZE = 8, 9, 10, 11
GLX_DRAWABLE_TYPE, GLX_RENDER_TYPE, GLX_X_RENDERABLE = 0x8010, 0x8011, 0x8012
GLX_RGBA_BIT, GLX_PBUFFER_BIT = 0x00000001, 0x00000004
GLX_VISUAL_ID, GLX_RGBA_TYPE = 0x800B, 0x8014
GLX_PBUFFER_WIDTH, GLX_PBUFFER_HEIGHT = 0x8041, 0x8040
GLX_CONTEXT_MAJOR_VERSION_ARB, GLX_CONTEXT_MINOR_VERSION_ARB = 0x2091, 0x2092
GLX_CONTEXT_PROFILE_MASK_ARB, GLX_CONTEXT_CORE_PROFILE_BIT_ARB = 0x9126, 0x00000001
