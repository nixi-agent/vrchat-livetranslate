"""Offline OpenXR regression cases; invoked by test_openxr_overlay."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.openxr_overlay import (  # noqa: E402
    CYLINDER_EXT,
    TRACKER_ROLES,
    XrOverlaySession,
    _SRGB_TO_LINEAR,
    anchor_paths,
    effective_curvature,
    euler_to_quaternion,
    format_needs_linearize,
    layer_geometry,
    linearize_rgb,
    pick_swapchain_format,
    rotate_vector,
)

def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rot_rx(t):
    c, s = math.cos(t), math.sin(t)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _rot_ry(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rot_rz(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _ref_matrix(rot_deg):
    """参照实现：Rz · Ry · Rx（与 Windows 侧 build_matrix 同一约定，但代码路径不同）。"""
    rx, ry, rz = (math.radians(a) for a in rot_deg)
    return _mat_mul(_mat_mul(_rot_rz(rz), _rot_ry(ry)), _rot_rx(rx))


def _quat_rotate(q, v):
    """用四元数转一个向量（q 为 (x,y,z,w)）—— 与被测实现的推导路径不同。"""
    x, y, z, w = q
    # v' = v + 2*w*(q_vec × v) + 2*(q_vec × (q_vec × v))
    qv = (x, y, z)
    cross = lambda a, b: (a[1] * b[2] - a[2] * b[1],       # noqa: E731
                          a[2] * b[0] - a[0] * b[2],
                          a[0] * b[1] - a[1] * b[0])
    t = cross(qv, v)
    t2 = cross(qv, t)
    return tuple(v[i] + 2.0 * w * t[i] + 2.0 * t2[i] for i in range(3))


def _mat_vec(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


# ---------------------------------------------------------------- 测试

def test_quaternion_matches_matrix_convention():
    """★ 核心：四元数转出来的方向必须与 Rz·Ry·Rx 矩阵一致。

    这直接决定「用户调好的 rot 在 Linux 上是不是同一个方向」。
    """
    cases = [
        (0.0, 0.0, 0.0), (-47.0, -16.0, 0.0),      # config 里实测调好的那组
        (90.0, 0.0, 0.0), (0.0, 90.0, 0.0), (0.0, 0.0, 90.0),
        (30.0, -60.0, 120.0), (-15.5, 22.25, -33.75),
    ]
    basis = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.3, -0.7, 0.5)]
    worst = 0.0
    for rot in cases:
        q = euler_to_quaternion(rot)
        m = _ref_matrix(rot)
        for v in basis:
            got = _quat_rotate(q, v)
            want = _mat_vec(m, v)
            err = max(abs(got[i] - want[i]) for i in range(3))
            worst = max(worst, err)
            assert err < 1e-9, (
                f"rot={rot} v={v}：四元数转出 {got}，矩阵转出 {want}（差 {err:.2e}）\n"
                f"—— 说明 euler_to_quaternion 的旋转顺序与 Windows 侧 build_matrix 不一致")
    print(f"  四元数与 Rz·Ry·Rx 矩阵一致 OK（{len(cases)} 组角度，最大误差 {worst:.1e}）")


def test_quaternion_is_normalized():
    """四元数必须是单位四元数，否则贴图会被缩放。"""
    for rot in ((0, 0, 0), (-47, -16, 0), (123, -45, 67)):
        x, y, z, w = euler_to_quaternion(rot)
        n = math.sqrt(x * x + y * y + z * z + w * w)
        assert abs(n - 1.0) < 1e-12, f"rot={rot} 的模是 {n}"
    print("  四元数都是单位长度 OK")


def test_anchor_paths_mapping():
    """锚点 → OpenXR 路径的映射表（两端语义要对得上）。"""
    assert anchor_paths("right_hand") == (
        "/user/hand/right/input/grip/pose", "/user/hand/right")
    assert anchor_paths("left_hand") == (
        "/user/hand/left/input/grip/pose", "/user/hand/left")
    full, top = anchor_paths("tracker", 0)
    assert full == f"{top}/input/grip/pose" and top.startswith("/user/vive_tracker_htcx/role/")
    # tracker_index 越界要夹住，不能 IndexError
    full_oob, _ = anchor_paths("tracker", 999)
    assert full_oob == f"/user/vive_tracker_htcx/role/{TRACKER_ROLES[-1]}/input/grip/pose"
    # hmd 不用 action（走 VIEW 参考空间）
    assert anchor_paths("hmd") == (None, None)
    assert anchor_paths("不认识的东西") == (None, None)
    print("  锚点路径映射 OK（含越界夹取）")


def test_swapchain_format_prefers_srgb():
    """★ 格式必须**优先 sRGB** —— 颜色空间选错的表现是「面板黑不下去」。

    合成器按线性合成、输出前再 `from_linear_to_srgb()`（Monado `layer.comp` 的
    `main()`，WiVRn 侧 `k_do_color_correction=true`），所以：

    * 格式 = sRGB → 硬件采样时解码，端到端恒等；
    * 格式 = 线性 → 我们的 sRGB 字节被当线性值再编码一次 = **双重 gamma**，
      底板 (12,14,20) 显示成 ≈(52,56,67)（用户实测头显内截图 (51,56,68) 正压地板）。

    另一条约束是**必须 8 位**：实测可用列表的第一个是 0x805b(RGBA16F)，
    直接取 `formats[0]` 会拿 16 位浮点、而我们按 8 位上传。
    """
    real = [0x805B, 0x881A, 0x8C43, 0x8058, 0x8CAC, 0x81A5]
    assert pick_swapchain_format(real) == 0x8C43, "没优先选 GL_SRGB8_ALPHA8"
    assert pick_swapchain_format([0x805B, 0x8058]) == 0x8058, \
        "没有 sRGB 格式时应退到 GL_RGBA8（上传前会预线性化）"
    assert pick_swapchain_format([0x805B, 0x8059]) == 0x8059
    assert pick_swapchain_format([0x805B]) == 0x805B, "都没有时应退回第一个而不是崩"
    assert pick_swapchain_format([]) == 0x8C43, "空列表要有兜底"
    print("  格式选择优先 sRGB OK（没有 sRGB 才退线性）")


def test_color_space_needs_linearize_and_lut():
    """★ 颜色空间契约：sRGB 格式交硬件解码；线性格式必须自己线性化。

    判定用**白名单**：认不出的格式一律当线性（多转一次是安全的，少转就是发灰）。
    LUT 的验收标准不是「值好看」，而是**往返误差** —— 线性化过的像素经合成器
    再编码回 sRGB 后，必须回到原值附近（≤2/255，8 位线性装暗端的量化代价）。
    """
    assert format_needs_linearize(0x8C43) is False, "GL_SRGB8_ALPHA8 不该再转"
    assert format_needs_linearize(0x8C41) is False, "GL_SRGB8 不该再转"
    assert format_needs_linearize(0x8058) is True, "GL_RGBA8 是线性 → 必须转"
    assert format_needs_linearize(0x8059) is True, "RGB10_A2 是线性 → 必须转"
    assert format_needs_linearize(0x805B) is True, "认不出的一律按线性（保守）"

    lut = _SRGB_TO_LINEAR
    assert len(lut) == 256 and lut[0] == 0 and lut[255] == 255
    assert all(lut[i] <= lut[i + 1] for i in range(255)), "LUT 必须单调不减"
    for v, want in ((12, 1), (64, 13), (128, 55), (192, 134)):
        assert lut[v] == want, f"LUT[{v}] 应为 {want}，实得 {lut[v]}"

    def l2s(x: float) -> int:            # 合成器输出端的编码（独立于我们的 LUT 实现）
        x = max(0.0, min(1.0, x))
        return round(255 * (12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055))

    for v in (0, 12, 20, 32, 64, 96, 128, 160, 192, 235, 255):
        back = l2s(lut[v] / 255.0)
        assert abs(back - v) <= 2, f"{v} 线性化后往返回来是 {back}（误差 >2）"
    # ★ 双重 gamma 的对照：不线性化时暗端会被抬到 5 倍 —— 这正是用户看到的现象
    assert l2s(12 / 255.0) == 61 and l2s(32 / 255.0) == 99, "双重 gamma 的特征值变了？"
    print("  颜色空间：判定表 + LUT 往返 ≤2/255 OK（双重 gamma 特征值 12→61 / 32→99）")


def test_linearize_rgb_touches_only_rgb():
    """线性化**只动 RGB**，alpha 一个 bit 都不许改。

    预乘/未预乘的语义全在 alpha 上（`layer_alpha_flags()` 的
    `UNPREMULTIPLIED_ALPHA_BIT`）—— 动 alpha 就等于把那条修好的路又搞坏。
    """
    from PIL import Image

    img = Image.new("RGBA", (4, 1))
    img.putdata([(255, 255, 255, 255), (12, 14, 20, 205),
                 (0, 0, 0, 0), (128, 64, 32, 128)])
    out = linearize_rgb(img)
    assert [out.getpixel((x, 0))[3] for x in range(4)] == [255, 205, 0, 128], "alpha 被改了"
    assert out.getpixel((0, 0)) == (255, 255, 255, 255), "白必须留在白"
    assert out.getpixel((2, 0))[:3] == (0, 0, 0), "黑必须留在黑"
    assert out.getpixel((1, 0)) == (_SRGB_TO_LINEAR[12], _SRGB_TO_LINEAR[14],
                                    _SRGB_TO_LINEAR[20], 205)
    assert linearize_rgb(img.convert("RGB")).mode == "RGBA", "非 RGBA 输入要能收下"
    print("  线性化只动 RGB（alpha / 白 / 黑 都不动）OK")


def test_prepared_linearizes_only_for_linear_swapchain_format():
    """★ 上传路径接线：格式是线性 → 字节被线性化；格式是 sRGB → 字节原样。

    缓存键必须含格式 —— 换链后若协商到另一种格式（`rebuild_swapchain()`），
    拿旧字节贴上去颜色就错了。
    """
    from PIL import Image

    img = Image.new("RGBA", (2, 1), (12, 64, 255, 205))
    sess = XrOverlaySession(None, (2, 1))            # type: ignore[arg-type]
    sess.format = 0x8058                             # 线性 → 要转
    assert sess._prepared(img, 1.0) == linearize_rgb(img).tobytes()   # noqa: SLF001
    sess.format = 0x8C43                             # sRGB → 原样
    assert sess._prepared(img, 1.0) == img.tobytes(), "sRGB 格式不该再转一次"  # noqa: SLF001
    sess.format = 0x8058                             # 换回来 → 缓存不该复用旧字节
    assert sess._prepared(img, 1.0) == linearize_rgb(img).tobytes()   # noqa: SLF001
    print("  _prepared 按交换链格式决定是否线性化（缓存键含格式）OK")


def test_layer_geometry_matches_windows_curvature():
    """★ 弯曲度的定义必须与 Windows（`SetOverlayCurvature`）**同一个口径**。

    openvr.h 原文：「curvature 是占整圆的比例，1 = 完全闭合的圆柱；给定半径时
    curvature = overlay.width / (2π·r)」⇒ 我们这边就必须是
    `central_angle = 2π·curvature`、`radius = width/central_angle`，
    而且 **width_m 是弧长**（不是弦长 —— 旧实现按弦长算，还带 5× 系数与 1.5 rad 上限，
    结果 0.3 以上滑到底没区别，和 Windows 也对不上）。
    """
    aspect, w = 1024 / 440, 0.23

    flat = layer_geometry(w, aspect, 0.0)
    assert flat["kind"] == "quad" and flat["size"][0] == w
    assert abs(flat["size"][1] - w / aspect) < 1e-9
    assert layer_geometry(w, aspect, 0.005)["kind"] == "quad", "小到看不出弯就该走平面"
    assert layer_geometry(w, aspect, -1.0)["kind"] == "quad", "负值也该走平面"

    for c in (0.05, 0.15, 0.2, 0.3, 0.5):
        g = layer_geometry(w, aspect, c)
        assert g["kind"] == "cylinder"
        assert abs(g["central_angle"] - 2 * math.pi * c) < 1e-9, f"c={c} 的圆心角不是 2π·c"
        assert abs(g["radius"] * g["central_angle"] - w) < 1e-9, "弧长必须等于 width_m"
        assert abs(g["aspect_ratio"] - aspect) < 1e-9
        # ★ 半径不能当位置用：柱面层要把「轴」沿面板局部 +Z 挪 radius（在 submit 里做）
        assert g["pose_offset"] == (0.0, 0.0, g["radius"])

    # 弯曲 0.5 = 半圈（180°）：弦长只有弧长的 2/π ≈ 64%，这是 Windows 口径的必然结果
    half = layer_geometry(w, aspect, 0.5)
    assert abs(half["central_angle"] - math.pi) < 1e-3
    chord = 2 * half["radius"] * math.sin(half["central_angle"] / 2)
    assert abs(chord / w - 2 / math.pi) < 1e-3

    # 极端值要夹住，不能出 NaN / 除零（aspect=0 也要能算）
    for c in (1.0, 5.0):
        g = layer_geometry(w, 0.0, c)
        assert g["kind"] == "cylinder" and g["radius"] > 0
        assert g["central_angle"] < 2 * math.pi, "圆心角越界（规范要求 < 2π）"
    print("  层几何换算 OK（2π 口径 · 弧长=宽度 · 带 pose_offset）")


def test_cylinder_pose_offset_rotates_with_panel():
    """★ 柱面层的「轴」偏移必须跟着**面板的 rot** 转，不能拿世界 Z。

    双路验证（与四元数那条同一风格）：实现用四元数转局部 (0,0,r)，测试用
    Rz·Ry·Rx 矩阵转。最后再钉住真正的目的：**弧面中点要落回原位置**
    （`pose + R·(0,0,−radius) == cfg.pos`）—— 不成立就是用户看到的「面板飘走」。
    """
    for rot in ((0.0, 0.0, 0.0), (-47.0, -16.0, 0.0), (30.0, -60.0, 120.0)):
        g = layer_geometry(0.23, 1024 / 440, 0.2)
        r = g["radius"]
        got = rotate_vector(rot, g["pose_offset"])
        want = _mat_vec(_ref_matrix(rot), (0.0, 0.0, r))
        assert max(abs(got[i] - want[i]) for i in range(3)) < 1e-9, \
            f"rot={rot}：偏移方向与 Rz·Ry·Rx 不一致"

        # 弧面中点（局部 (0,0,-r)）加上 pose 偏移后必须回到原点 = 面板位置不动
        back = rotate_vector(rot, (0.0, 0.0, -r))
        total = [got[i] + back[i] for i in range(3)]
        assert max(abs(v) for v in total) < 1e-9, f"rot={rot}：弧面中点没回到面板位置"

    # 不转的时候就是纯 +Z，长度正好是半径（对齐 Monado layer_cylinder.vert 的 z = -cos(a)*r）
    r0 = layer_geometry(0.23, 1024 / 440, 0.2)
    off = rotate_vector((0.0, 0.0, 0.0), r0["pose_offset"])
    assert abs(off[0]) < 1e-12 and abs(off[1]) < 1e-12
    assert abs(off[2] - r0["radius"]) < 1e-12
    print("  柱面 pose 偏移 OK（随面板 rot 旋转 · 弧面中点回到原位）")


def test_effective_curvature_degrades_without_extension():
    """★ #26-5：运行时没有柱面扩展 → 这一帧按平面层提交，**不许**硬构造柱面层。

    柱面合成层是 `XR_KHR_composition_layer_cylinder` 扩展；拿不到还构造的话
    `xrEndFrame` 会整帧失败（整条手腕屏没了），而这里要的只是「不弯」。
    """
    aspect, w = 1024 / 440, 0.23
    assert effective_curvature(0.3, [CYLINDER_EXT]) == 0.3, "有扩展就该保留弯曲度"
    assert effective_curvature(0.3, ["XR_EXTX_overlay"]) == 0.0, "没扩展必须归 0"
    assert effective_curvature(0.3, []) == 0.0
    assert effective_curvature(0.0, []) == 0.0 and effective_curvature(-1.0, []) == 0.0

    # 归 0 之后几何确实退回平面（这才是 submit 里真正会走的路）
    assert layer_geometry(w, aspect, effective_curvature(0.3, []))["kind"] == "quad"
    assert layer_geometry(w, aspect, effective_curvature(0.3, [CYLINDER_EXT]))["kind"] == "cylinder"
    print("  柱面扩展缺失 → effective_curvature 归 0、退回平面 OK")
