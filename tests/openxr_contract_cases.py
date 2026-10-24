"""Offline OpenXR regression cases; invoked by test_openxr_overlay."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.openxr_overlay import (  # noqa: E402
    CYLINDER_EXT,
    OpenXrOverlay,
    apply_overlay_alpha,
    layer_alpha_flags,
    render_alpha_test,
    should_rebuild,
)
from vlt.output.overlay import OverlayConfig, render_panel  # noqa: E402


def _backend_source():
    folder = ROOT / 'vlt' / 'output'
    paths = sorted(folder.glob('openxr_*.py'))
    assert len(paths) >= 7, 'OpenXR implementation modules missing'
    return '\n'.join(p.read_text(encoding='utf-8') for p in paths)



def test_extensions_does_not_gamble_on_cylinder_when_unknown():
    """★ #26-5：枚举失败时**不赌**可选扩展 —— 只请求必需项，绝不带上柱面。

    请求一个运行时没有的扩展会让 `create_instance` 直接失败（整条腿消失），
    比「不弯」严重得多。
    """
    import types

    class _E:
        def __init__(self, name: str) -> None:
            self.extension_name = name.encode()

    fake = types.ModuleType("xr")
    fake.enumerate_instance_extension_properties = lambda: [  # type: ignore[attr-defined]
        _E("XR_EXTX_overlay"), _E("XR_KHR_opengl_enable"), _E("XR_MNDX_egl_enable")]

    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    try:
        got = OpenXrOverlay._extensions()          # noqa: SLF001
        assert CYLINDER_EXT not in got, f"运行时没报柱面扩展却启用了它：{got}"
        assert "XR_EXTX_overlay" in got, "必需项不该被漏掉"

        # 枚举抛异常（旧实现走「全都要」，会把柱面也塞进去）→ 只赌必需项
        def boom():
            raise RuntimeError("模拟：枚举设备失败")

        fake.enumerate_instance_extension_properties = boom  # type: ignore[attr-defined]
        got2 = OpenXrOverlay._extensions()         # noqa: SLF001
        assert CYLINDER_EXT not in got2, f"枚举失败时不该赌柱面：{got2}"
        assert "XR_KHR_opengl_enable" in got2, "枚举失败也要保住必需项"

        # 运行时确实报了柱面扩展 → 保留（降级只在真拿不到时发生）
        fake.enumerate_instance_extension_properties = lambda: [  # type: ignore[attr-defined]
            _E(CYLINDER_EXT), _E("XR_EXTX_overlay"), _E("XR_KHR_opengl_enable"),
            _E("XR_MNDX_egl_enable")]
        assert CYLINDER_EXT in OpenXrOverlay._extensions()  # noqa: SLF001
    finally:
        if saved is None:
            sys.modules.pop("xr", None)
        else:
            sys.modules["xr"] = saved
    print("  _extensions 不赌柱面 OK（枚举失败只留必需项 · 真有扩展才启用）")


def test_submit_records_and_uses_enabled_extensions():
    """★ #26-5 接线：`submit()` 用**本会话实际启用的扩展**判定柱面能力。

    纯逻辑测试跑不了真 XR，但可以扫源码钉住「submit 里确实调了 effective_curvature
    并用 self.extensions」，避免哪天有人把它改回无条件的 `cfg.curvature`。
    """
    src = _backend_source()
    assert "effective_curvature(cfg.curvature, self.extensions)" in src, \
        "submit() 必须按实际启用的扩展决定弯曲度"
    assert "self.extensions = list(extensions)" in src, \
        "create() 必须记下实际启用的扩展"
    print("  submit/create 接线 OK（记下扩展 · 按扩展决定弯曲度）")


def test_should_rebuild_detects_changes():
    """热重载判定：几何/锚点 → 重应用变换；字号/尺寸/**透明度/配色** → 必须重渲贴图。"""
    base = OverlayConfig()
    assert should_rebuild(base, OverlayConfig()) == (False, False), "没改却说改了"

    import dataclasses
    geo = dataclasses.replace(base, width_m=0.30)
    assert should_rebuild(base, geo) == (True, False), "改宽度没被识别成几何变化"

    anchor = dataclasses.replace(base, anchor="hmd", tracker_index=2)
    assert should_rebuild(base, anchor) == (True, False), "改锚点没被识别"

    render = dataclasses.replace(base, font_size=44)
    assert should_rebuild(base, render) == (False, True), "改字号没被识别成需要重渲"

    # ★ 界面上新加的「底板/原文不透明度」滑块走的就是这条路：不重渲 = 拖着没反应
    for field, val in (("bg_alpha", 120), ("source_alpha", 255), ("border_alpha", 200),
                       ("separator", False), ("color_bg", (30, 30, 30)),
                       ("color_theirs", (1, 2, 3))):
        assert should_rebuild(base, dataclasses.replace(base, **{field: val})) == (False, True), \
            f"改 {field} 没被识别成需要重渲"

    both = dataclasses.replace(base, font_size=44, pos=(1.0, 0.0, 0.0))
    assert should_rebuild(base, both) == (True, True)
    print("  热重载判定 OK（含底板/原文不透明度、配色）")


def test_layer_alpha_flags_value():
    """★ 图层 alpha 的两个 flag 值必须是「贴图 alpha 生效 + 未预乘」。

    这是「蓝框外一圈黑」的那个开关：不设 `BLEND_TEXTURE_SOURCE_ALPHA_BIT`，
    整个图层会按不透明合成（贴图里 alpha=0 的边距被当成实心黑）。
    """
    try:
        import xr
    except ImportError:
        print("  ⏭ 没装 pyopenxr（非 Linux 环境），跳过")
        return
    f = int(layer_alpha_flags())
    want = int(xr.CompositionLayerFlags.BLEND_TEXTURE_SOURCE_ALPHA_BIT) \
        | int(xr.CompositionLayerFlags.UNPREMULTIPLIED_ALPHA_BIT)
    assert f == want, f"flag 不对：0x{f:x}，应该是 0x{want:x}（BLEND | UNPREMULTIPLIED）"
    assert f & 0x2, "少了 BLEND_TEXTURE_SOURCE_ALPHA_BIT → 图层不透明 → 一圈黑边"
    assert f & 0x4, "少了 UNPREMULTIPLIED_ALPHA_BIT → 未预乘的贴图会被当预乘 → 偏亮"
    print(f"  图层 alpha flag OK（0x{f:x}）")


def test_apply_overlay_alpha_scales_only_alpha():
    """整层 alpha 乘子 = Linux 侧的 `setOverlayAlpha()`：**只动 alpha，RGB 不碰**。

    只动 alpha 才能做到「底板变淡、文字仍实心」；连 RGB 一起乘（预乘空间的做法）
    会让白字先变灰再变暗 —— 那就不是用户要的东西了。
    """
    from PIL import Image

    img = Image.new("RGBA", (4, 1))
    img.putdata([(255, 255, 255, 255), (12, 14, 20, 205), (0, 0, 0, 0), (10, 20, 30, 128)])

    def _px(im):  # 不用 getdata()：Pillow 14 起弃用，getpixel 一直稳定
        return [im.getpixel((x, 0)) for x in range(4)]

    before = _px(img)
    assert apply_overlay_alpha(img, 1.0) is img, "alpha=1 时不该白拷一份"

    for k, want_alpha in ((0.0, [0, 0, 0, 0]), (0.5, [128, 102, 0, 64]),
                          (0.9, [230, 184, 0, 115])):
        out = _px(apply_overlay_alpha(img, k))
        assert [p[3] for p in out] == want_alpha, \
            f"k={k}：alpha 应为 {want_alpha}，实得 {[p[3] for p in out]}"
        assert [p[:3] for p in out] == [p[:3] for p in before], \
            f"k={k}：RGB 被改了（文字会跟着变暗）"

    # 越界值要夹住，不能崩、也不能溢出（alpha=0 的像素乘完还得是 0）
    assert [p[3] for p in _px(apply_overlay_alpha(img, 5.0))] == [255, 205, 0, 128]
    assert [p[3] for p in _px(apply_overlay_alpha(img, -1.0))] == [0, 0, 0, 0]
    print("  整层 alpha 乘子 OK（只乘 alpha、端点/越界都对）")


def test_panels_have_transparent_margin_and_translucent_plate():
    """贴图侧的独立判据：**边距全透明 + 底板半透明**。

    与上面的 flag 是两条互不相干的失效路径 —— 任何一条坏了，用户看到的一样是
    「蓝框外一圈黑」。这条不需要头显就能跑。
    """
    cfg = OverlayConfig()
    w, h = cfg.size_px
    for name, img in (("面板", render_panel("你好，测试。", "hello", cfg)),
                      ("alpha 判定图", render_alpha_test(cfg))):
        px = img.convert("RGBA").load()
        for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
                   (w // 2, 0), (0, h // 2), (w - 1, h // 2)):
            assert px[xy][3] == 0, f"{name} 的 {xy} 不是全透明（{px[xy]}）→ 屏上就是黑边"
        # 四角必须真的透明（圆角）；中间靠底那一块是底板 → 必须是配置的半透明值
        assert px[(w // 2, h - 20)][3] == cfg.bg_alpha, \
            f"{name} 的底板不是半透明（得到 {px[(w // 2, h - 20)]}）"
    print("  贴图边距全透明 + 底板半透明 OK")


def test_backend_config_field():
    """`overlay.backend` 的默认与解析（null 用来彻底关掉手腕屏）。"""
    assert OverlayConfig().backend == "auto"
    assert OverlayConfig.from_dict({}).backend == "auto"
    assert OverlayConfig.from_dict({"backend": "null"}).backend == "null"
    assert OverlayConfig.from_dict({"backend": None}).backend == "auto"
    print("  backend 配置字段 OK")


def test_composition_layers_declare_alpha_flags():
    """★ 回归锁：合成层**必须**声明 alpha flag（少了就是一整层不透明 → 一圈黑边）。

    flag 值本身的正确性由 `test_layer_alpha_flags_value()` 管；这条只管
    「**每一处**合成层构造都真的把 flag 传下去了」—— Quad 与 Cylinder 两条分支
    都得传，漏一处就是「换个曲率又冒出黑边」这种半好半坏的状态。
    """
    import ast
    tree = ast.parse(_backend_source())

    layers = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "xr"):
            continue
        if node.func.attr not in ("CompositionLayerQuad", "CompositionLayerCylinderKHR"):
            continue
        layers.append((node.lineno, node.func.attr,
                       {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}))

    assert layers, "一处合成层构造都没扫到 —— 源码结构变了？"
    names = {name for _, name, _ in layers}
    assert "CompositionLayerQuad" in names and "CompositionLayerCylinderKHR" in names, \
        f"应有 Quad 与 Cylinder 两条分支，实际只有 {names}"
    for lineno, name, kw in layers:
        assert "layer_flags" in kw, (
            f"L{lineno} xr.{name}(…) 没传 layer_flags —— 图层会按不透明合成，"
            f"贴图里的透明边距会变成一圈黑边")
        assert "layer_alpha_flags" in kw["layer_flags"], (
            f"L{lineno} layer_flags={kw['layer_flags']}，应当是 layer_alpha_flags() "
            f"（BLEND_TEXTURE_SOURCE_ALPHA | UNPREMULTIPLIED_ALPHA）")
    print(f"  合成层都带 alpha flag OK（{len(layers)} 处：Quad + Cylinder）")


def test_backend_xr_calls_are_wellformed():
    """★ 静态扫描产品后端里的所有 `xr.*` 调用，抓三类只有真机才会暴露的错：

      1. **句柄错配**：`xr.create_reference_space(self.instance, …)` —— 第一个参数要的是
         **session**。参数**个数是对的**，所以个数检查抓不到；报错信息
         （`expected Session instance instead of Instance`）也不指向具体那一行。
         **这条是真机冒烟抓出来的**，所以固化成测试，别再让人戴头显来抓。
      2. 位置参数个数不足（漏参数）
      3. 结构体关键字字段名写错

    没装 pyopenxr（Windows 构建）时跳过 —— 那个模块本来就不该进 Windows 产物。
    """
    import ast
    try:
        import xr
    except ImportError:
        print("  ⏭ 没装 pyopenxr（非 Linux 环境），跳过")
        return

    tree = ast.parse(_backend_source())
    # 句柄类型 → 实参里应当出现的字样（只看 self.<attr>，局部变量名太自由、误报多）
    handles = {"Session": ("session", "_sess"), "Instance": ("instance",),
               "Swapchain": ("swapchain",), "ActionSet": ("action_set", "aset"),
               "Action": ("action", "act"), "Space": ("space",)}

    bad_handle, bad_arity, bad_field, calls = [], [], [], 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "xr"):
            continue
        cls_or_fn = getattr(xr, node.func.attr, None)
        if cls_or_fn is None:
            bad_field.append(f"L{node.lineno} xr.{node.func.attr} 不存在")
            continue
        calls += 1
        # —— 结构体构造：关键字字段名
        if node.keywords and hasattr(cls_or_fn, "_fields_"):
            allowed = set(dir(cls_or_fn()))
            for kw in node.keywords:
                if kw.arg and kw.arg not in allowed:
                    bad_field.append(f"L{node.lineno} xr.{node.func.attr}({kw.arg}=…) 不是有效字段")
            continue
        # —— 枚举：构造就是「传一个值」，**别按签名判** ——
        #    `inspect.signature` 对枚举类报的参数个数**随 Python 版本变**
        #    （3.11 报 1 个、3.14 报 2 个，因为 __new__ 的来源不同），
        #    按签名判会在某个版本上凭空报「参数不足」（实测在 3.14 上踩到）。
        if isinstance(cls_or_fn, type) and hasattr(cls_or_fn, "__members__"):
            continue
        # —— 函数调用：参数个数 + 句柄错配
        try:
            params = list(inspect.signature(cls_or_fn).parameters.values())
        except (TypeError, ValueError):
            continue
        need = len([q for q in params
                    if q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD) and q.default is q.empty])
        if len(node.args) < need:
            bad_arity.append(f"L{node.lineno} xr.{node.func.attr} 传 {len(node.args)} 个，至少要 {need}")
        for i, arg in enumerate(node.args):
            if i >= len(params):
                break
            name = getattr(params[i].annotation, "__name__", "")
            hint = handles.get(name)
            src = ast.unparse(arg).lower()
            if hint and src.startswith("self.") and not any(h in src for h in hint):
                bad_handle.append(f"L{node.lineno} xr.{node.func.attr} 参数{i+1} 期望 {name}，"
                                  f"实参是 `{ast.unparse(arg)}`")

    assert not bad_handle, "句柄类型错配（参数个数对、类型错）：\n  " + "\n  ".join(bad_handle)
    assert not bad_arity, "位置参数个数不足：\n  " + "\n  ".join(bad_arity)
    assert not bad_field, "xr.* 名字/字段名无效：\n  " + "\n  ".join(bad_field)
    print(f"  后端 {calls} 处 xr.* 调用：句柄/个数/字段 全部正确 OK")


def test_extensions_per_backend_required():
    """★ 后端决定必需扩展清单：X11/XLIB 不该要求 `XR_MNDX_egl_enable`。

    MNDX 是 Wayland 的 EGL 绑定专有扩展；X11 走 `XR_KHR_opengl_enable` 的 XLIB 分支。
    """
    import types

    from vlt.output.openxr_overlay import EglGlContext, XlibGlxContext

    assert "XR_MNDX_egl_enable" in EglGlContext.REQUIRED_EXTENSIONS
    assert "XR_MNDX_egl_enable" not in XlibGlxContext.REQUIRED_EXTENSIONS
    assert "XR_KHR_opengl_enable" in XlibGlxContext.REQUIRED_EXTENSIONS

    class _E:
        def __init__(self, name: str) -> None:
            self.extension_name = name.encode()

    fake = types.ModuleType("xr")
    fake.enumerate_instance_extension_properties = lambda: [  # type: ignore[attr-defined]
        _E("XR_EXTX_overlay"), _E("XR_KHR_opengl_enable"), _E("XR_MNDX_egl_enable")]
    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    try:
        got_x11 = OpenXrOverlay._extensions(XlibGlxContext.REQUIRED_EXTENSIONS)  # noqa: SLF001
        assert "XR_MNDX_egl_enable" not in got_x11, f"X11 不该要 MNDX：{got_x11}"
        assert "XR_KHR_opengl_enable" in got_x11, got_x11
        got_way = OpenXrOverlay._extensions(EglGlContext.REQUIRED_EXTENSIONS)    # noqa: SLF001
        assert "XR_MNDX_egl_enable" in got_way, f"Wayland 需要 MNDX：{got_way}"
    finally:
        if saved is None:
            sys.modules.pop("xr", None)
        else:
            sys.modules["xr"] = saved
    print("  后端必需扩展清单 OK（X11 不要 MNDX · Wayland 要）")


def test_glx_binding_struct_fields():
    """★ XLIB 绑定结构体：指针字段必须 cast 成 pyopenxr/PyOpenGL 声明的类型。

    不建真上下文（`__new__` 绕过 __init__ 塞假地址）—— 这条只钉「结构体构造」；
    真上下文在 `tests/test_overlay_glx.py` 里用 Xvfb+GLX 跑。
    pyopenxr 在 Windows 测试机上没装 → 跳过（该后端本来就是 Linux 独占）。
    """
    try:
        import xr
    except Exception:  # noqa: BLE001
        print("  SKIP: 没有 pyopenxr（Windows 测试机不装）")
        return

    import ctypes as _ct

    from vlt.output.openxr_overlay import XlibGlxContext

    ctx = XlibGlxContext.__new__(XlibGlxContext)
    ctx.display = 0xDEAD0000
    ctx.fbconfig = 0xDEAD0001
    ctx.visualid = 0x21
    ctx.pbuffer = 0xDEAD0002
    ctx.context = 0xDEAD0003
    b = ctx.binding()
    assert type(b) is xr.GraphicsBindingOpenGLXlibKHR, type(b)
    fields = dict(xr.GraphicsBindingOpenGLXlibKHR._fields_)
    assert _ct.cast(b.x_display, _ct.c_void_p).value == 0xDEAD0000
    assert b.visualid == 0x21
    assert _ct.cast(b.glx_fbconfig, _ct.c_void_p).value == 0xDEAD0001
    assert b.glx_drawable == 0xDEAD0002
    assert _ct.cast(b.glx_context, _ct.c_void_p).value == 0xDEAD0003
    # 类型必须真是声明的那几个（给错类型运行时会 TypeError）
    assert b.x_display.__class__ is fields["x_display"]
    print("  XLIB 绑定结构体字段 OK（含指针 cast 类型）")


def test_create_gl_context_selection():
    """★ 后端选择：Wayland 优先、失败回退 X11；`VLT_OVERLAY_GL` 可强制。"""
    import os

    import vlt.output.openxr_overlay as O

    class _Ok:
        def __init__(self, name: str, log: list[str]) -> None:
            self.name = name
            log.append(name)

    class _Fail:
        def __init__(self, name: str, log: list[str]) -> None:
            log.append(name)
            raise RuntimeError(f"{name} 建不起来（模拟）")

    saved_backends = dict(O._GL_BACKENDS)
    saved_env = {k: os.environ.get(k)
                 for k in ("WAYLAND_DISPLAY", "DISPLAY", "VLT_OVERLAY_GL")}
    log: list[str] = []
    try:
        O._GL_BACKENDS.clear()
        O._GL_BACKENDS.update(wayland=lambda: _Ok("wayland", log),
                              x11=lambda: _Ok("x11", log))
        os.environ["WAYLAND_DISPLAY"] = "wayland-1"
        os.environ["DISPLAY"] = ":0"
        os.environ.pop("VLT_OVERLAY_GL", None)
        assert O.create_gl_context().name == "wayland", f"两个都有时应先 Wayland：{log}"

        # Wayland 建不起来 → 回退 X11
        log.clear()
        O._GL_BACKENDS["wayland"] = lambda: _Fail("wayland", log)
        assert O.create_gl_context().name == "x11", f"Wayland 失败应回退 X11：{log}"

        # 只有 DISPLAY（Xorg 会话）→ 直接 X11，别去碰 Wayland
        log.clear()
        O._GL_BACKENDS["wayland"] = lambda: _Ok("wayland", log)
        os.environ.pop("WAYLAND_DISPLAY", None)
        assert O.create_gl_context().name == "x11", f"Xorg 会话应该直接走 X11：{log}"
        assert log == ["x11"], f"不该先试 Wayland：{log}"

        # 强制开关：只试指定那条
        log.clear()
        os.environ["WAYLAND_DISPLAY"] = "wayland-1"
        os.environ["VLT_OVERLAY_GL"] = "wayland"
        assert O.create_gl_context().name == "wayland", log
        assert log == ["wayland"], log

        # 强制的那条失败 → 直接抛（不回退，便于排查）
        log.clear()
        O._GL_BACKENDS["x11"] = lambda: _Fail("x11", log)
        os.environ["VLT_OVERLAY_GL"] = "x11"
        try:
            O.create_gl_context()
        except RuntimeError as exc:
            assert "x11" in str(exc), str(exc)
        else:
            raise AssertionError("强制 x11 且失败时应抛 RuntimeError")
        assert log == ["x11"], f"强制模式不该试别的后端：{log}"

        # 两条都失败（auto、环境里两个都看不到）→ 两条都试、错误信息里两边的都有
        log.clear()
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("DISPLAY", None)
        os.environ.pop("VLT_OVERLAY_GL", None)
        O._GL_BACKENDS["wayland"] = lambda: _Fail("wayland", log)
        try:
            O.create_gl_context()
        except RuntimeError as exc:
            msg = str(exc)
            assert "wayland" in msg and "x11" in msg, msg
        else:
            raise AssertionError("全失败应抛 RuntimeError")
    finally:
        O._GL_BACKENDS.clear()
        O._GL_BACKENDS.update(saved_backends)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  GL 后端选择 OK（Wayland 优先 · 回退 X11 · 强制开关）")
