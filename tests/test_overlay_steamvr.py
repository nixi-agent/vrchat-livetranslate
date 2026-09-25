"""验证手腕 Overlay 真能接管 SteamVR —— 用假 openvr 复刻真实 API 形状。

## 为什么必须有这个测试

手腕屏此前只做过**离线渲染 / dry-run** 验证，`start()` 里"接管 SteamVR"那条路径
**一次都没在真实 API 上跑过**，于是留着一个致命错误：

    self._overlay = self._vr.overlay      # ✗ IVRSystem 上没有 overlay 属性

正确写法是模块级工厂 `openvr.IVROverlay()`（与 `openvr.IVRSystem()` 同族）。
用户实测报错（带完整日志，留痕机制生效）：

    [mine][error] 运行错误：'IVRSystem' object has no attribute 'overlay'
    [theirs][error] 运行错误：'IVRSystem' object has no attribute 'overlay'

而且那行写在 `try` **之外**，异常冒到引擎 → **两条翻译腿一起被打死**
（手腕屏挂不上不该拖垮 chatbox）。

本测试用假 openvr 复刻真实 API：**故意让假 IVRSystem 没有 `overlay` 属性**——
一旦有人再写 `self._vr.overlay`，这里立刻红。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CALLS: list[tuple] = []
RAW_FAIL_PLAN = {"remaining": 0}     # >0 时 setOverlayRaw 抛错（模拟 OverlayError_RequestFailed）


class _FakeHmdMatrix34_t:
    def __init__(self) -> None:
        self.m = [[0.0] * 4 for _ in range(3)]


class _FakeIVROverlay:
    def __init__(self, fail: bool = False) -> None:
        if fail:
            raise RuntimeError("模拟 IVROverlay 初始化失败")

    def createOverlay(self, key, name):  # noqa: ANN001, ANN201
        CALLS.append(("createOverlay", key))
        return 7

    def setOverlayWidthInMeters(self, handle, w):  # noqa: ANN001
        CALLS.append(("setOverlayWidthInMeters", round(float(w), 4)))

    def setOverlayAlpha(self, handle, a):  # noqa: ANN001
        CALLS.append(("setOverlayAlpha", round(float(a), 4)))

    def setOverlayCurvature(self, handle, c):  # noqa: ANN001
        CALLS.append(("setOverlayCurvature", round(float(c), 4)))

    def setOverlayTransformTrackedDeviceRelative(self, handle, device, mat):  # noqa: ANN001
        CALLS.append(("setOverlayTransformTrackedDeviceRelative", device, mat.m[0][3]))

    def setOverlayRaw(self, handle, buf, w, h, depth):  # noqa: ANN001
        if RAW_FAIL_PLAN["remaining"] > 0:
            RAW_FAIL_PLAN["remaining"] -= 1
            raise RuntimeError("OverlayError_RequestFailed: 模拟 SteamVR 贴图上传失败")
        CALLS.append(("setOverlayRaw", w, h, depth))

    def showOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("showOverlay", handle))

    def destroyOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("destroyOverlay", handle))


class _FakeIVRSystem:
    """**故意不提供 `.overlay` 属性** —— 真实 pyopenvr 就是这样（已核对 2.12.1401）。"""

    def getTrackedDeviceIndexForControllerRole(self, role):  # noqa: ANN001, ANN201
        return 3

    def getTrackedDeviceClass(self, i):  # noqa: ANN001, ANN201
        return 4


def _install_fake_openvr(overlay_factory=None):  # noqa: ANN001
    m = types.ModuleType("openvr")
    m.VRApplication_Background = 3
    m.TrackedControllerRole_LeftHand = 1
    m.TrackedControllerRole_RightHand = 2
    m.TrackedDeviceClass_GenericTracker = 4
    m.k_unMaxTrackedDeviceCount = 64
    m.k_unTrackedDeviceIndex_Hmd = 0
    m.HmdMatrix34_t = _FakeHmdMatrix34_t
    m.init = lambda app_type: _FakeIVRSystem()
    m.IVROverlay = overlay_factory or (lambda: _FakeIVROverlay())
    m.shutdown = lambda: CALLS.append(("shutdown",))
    sys.modules["openvr"] = m
    return m


def _cfg(tmp: Path, **over):
    from vlt.output.overlay import OverlayConfig

    cfg = OverlayConfig()
    cfg.anchor = over.get("anchor", "right_hand")
    cfg.pos = over.get("pos", (0.0, 0.06, 0.02))
    cfg.rot = over.get("rot", (0.0, 0.0, 0.0))
    cfg.width_m = over.get("width_m", 0.24)
    cfg.curvature = over.get("curvature", 0.0)
    cfg.alpha = over.get("alpha", 0.9)
    return cfg


def _names() -> list[str]:
    return [c[0] for c in CALLS]


def test_start_takes_over_steamvr() -> None:
    CALLS.clear()
    _install_fake_openvr()
    from vlt.output.overlay import WristOverlay

    ov = WristOverlay(_cfg(Path(".")))
    ok = ov.start()
    print("  调用序列:", _names())
    assert ok is True, "start() 应返回 True（假 SteamVR 可用）"
    assert "createOverlay" in _names(), "没有创建 overlay"
    assert "showOverlay" in _names(), "没有显示 overlay"
    # ★ 关键回归点：创建时就必须应用变换。
    # 旧代码里 _apply_transform() 开头有 `if not self.available: return`，
    # 而 available 直到 start() 末尾才置位 → 变换压根没应用过（面板停在默认位置）。
    assert "setOverlayTransformTrackedDeviceRelative" in _names(), \
        "创建后没有应用位姿变换（available 置位顺序 bug 复发了）"
    assert ov._device == 3, f"锚点应解析到右手控制器 device=3，实际 {ov._device}"
    ov.close()
    print("  start/transform/show 全部到位 OK")


def test_ivrsystem_has_no_overlay_attribute() -> None:
    """回归守卫：谁再把接口写成 `self._vr.overlay`，这里立刻红。"""
    CALLS.clear()
    fake = _install_fake_openvr()
    sys_mod = fake.init(0)
    assert not hasattr(sys_mod, "overlay"), "假 IVRSystem 不该有 overlay 属性（真实 API 也没有）"
    # 模块级工厂必须存在，且可用
    assert hasattr(fake, "IVROverlay")
    ovl = fake.IVROverlay()
    assert hasattr(ovl, "createOverlay")
    print("  IVRSystem 无 overlay 属性 / IVROverlay 是模块级工厂 OK")


def test_overlay_failure_degrades_without_raising() -> None:
    """IVROverlay() 抛异常时：start() 返回 False 且**不抛**（否则会打死翻译腿）。"""
    CALLS.clear()
    _install_fake_openvr(overlay_factory=lambda: _FakeIVROverlay(fail=True))
    from vlt.output.overlay import WristOverlay

    ov = WristOverlay(_cfg(Path(".")))
    try:
        ok = ov.start()
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"start() 不该把异常抛出去（会拖垮引擎）：{exc!r}") from exc
    assert ok is False
    assert ov.available is False
    print("  overlay 初始化失败 → 返回 False 且不抛异常 OK")


def test_update_uploads_texture() -> None:
    CALLS.clear()
    _install_fake_openvr()
    from vlt.output.overlay import WristOverlay

    ov = WristOverlay(_cfg(Path(".")))
    ov.start()
    CALLS.clear()
    ov.update("Hello, this is a test.", "你好，这是测试。")
    assert "setOverlayRaw" in _names(), f"没有上传贴图：{_names()}"
    w, h, depth = next(c[1:] for c in CALLS if c[0] == "setOverlayRaw")
    assert (w, h, depth) == (1024, 320, 4), f"贴图尺寸/通道不对：{w}x{h}x{depth}"
    ov.close()
    print(f"  贴图上传 OK（{w}x{h}x{depth}）")


def test_hot_reload_reapplies_geometry(tmp: Path | None = None) -> None:
    CALLS.clear()
    _install_fake_openvr()
    from vlt.output.overlay import WristOverlay

    cfgdir = ROOT / "out" / "overlay_cfg_test"
    cfgdir.mkdir(parents=True, exist_ok=True)
    cfgfile = cfgdir / "config.yaml"
    cfgfile.write_text(
        "overlay:\n  anchor: right_hand\n  offset:\n    pos: [0.0, 0.06, 0.02]\n"
        "    rot: [0, 0, 0]\n    width_m: 0.24\n    curvature: 0.0\n    alpha: 0.9\n",
        encoding="utf-8")

    ov = WristOverlay(_cfg(cfgdir), config_path=cfgfile)
    ov.start()
    CALLS.clear()

    # 改三个参数（含弯曲 —— 旧代码的比对漏了它，改了不起作用）
    cfgfile.write_text(
        "overlay:\n  anchor: left_hand\n  offset:\n    pos: [-0.1, 0.2, 0.05]\n"
        "    rot: [0, 0, 0]\n    width_m: 0.42\n    curvature: 0.2\n    alpha: 0.5\n",
        encoding="utf-8")
    ov.tick()
    print("  热重载后调用:", _names())
    assert "setOverlayTransformTrackedDeviceRelative" in _names(), \
        "改了位置/锚点后没有重新应用变换"
    assert "setOverlayCurvature" in _names(), "改了弯曲后没有应用（旧代码比对漏了 curvature）"
    dev = next(c[1] for c in CALLS if c[0] == "setOverlayTransformTrackedDeviceRelative")
    assert dev == 3, f"锚点切换后应重新解析设备，实际 {dev}"
    assert ov.cfg.width_m == 0.42 and abs(ov.cfg.alpha - 0.5) < 1e-6
    ov.close()
    print("  热重载（含弯曲/锚点）OK")


def test_hot_reload_font_rerenders() -> None:
    """改字号 / 面板高度必须**重新渲染贴图**（只重应用变换是不够的）。

    用户要能自己调字号：字调小了，同样的高度就能塞下更多字。
    如果热重载只比对几何参数，界面上拖字号滑块会「看着生效、屏上没变」。
    """
    CALLS.clear()
    _install_fake_openvr()
    from vlt.output.overlay import WristOverlay

    d = ROOT / "out" / "overlay_cfg_font"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "config.yaml"
    f.write_text("overlay:\n  font_size: 42\n  source_font_size: 30\n  size_px: [1024, 320]\n",
                 encoding="utf-8")

    ov = WristOverlay(_cfg(d), config_path=f)
    ov.start()
    ov.update_entries([("theirs", "Hello", "你好"), ("mine", "早上好", "Good morning")],
                      force=True)
    assert "setOverlayRaw" in _names(), "首帧没上传贴图"
    CALLS.clear()

    f.write_text("overlay:\n  font_size: 30\n  source_font_size: 22\n  size_px: [1024, 420]\n",
                 encoding="utf-8")
    ov.tick()
    print("  改字号后调用:", _names())
    assert "setOverlayRaw" in _names(), "改了字号没有重新渲染贴图（界面上会「看着生效、屏上没变」）"
    assert ov.cfg.font_size == 30 and ov.cfg.size_px == (1024, 420)
    # 重新渲染的图确实变高了（面板高度跟着走）
    from vlt.output.overlay import render_conversation
    img = render_conversation([("theirs", "Hello", "你好")], ov.cfg)
    assert img.size == (1024, 420), f"面板高度没生效：{img.size}"
    ov.close()
    print("  字号/面板高改动 → 重新渲染 OK")


def test_upload_failure_recovers_and_logs_quietly() -> None:
    """★ 回归：贴图上传连续失败时要「降噪 + 自动重建恢复」。

    用户实测：一晚上连续失败 86 次，**每帧打一行**把日志淹没了（371 行贴图日志里
    混着 86 行错误），而且手腕屏就停在最后一帧不动。
    """
    import contextlib
    import io

    CALLS.clear()
    RAW_FAIL_PLAN["remaining"] = 0
    _install_fake_openvr()
    from vlt.output.overlay import WristOverlay

    ov = WristOverlay(_cfg(Path(".")))
    ov.start()
    ov.update_entries([("theirs", "hi", "你好")], force=True)
    CALLS.clear()

    # 恰好失败 3 次：第 3 次触发重建，重建时会立刻把内容贴回去 → 应报「恢复上传」
    RAW_FAIL_PLAN["remaining"] = 3
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(4):
            ov.update_entries([("theirs", f"hi{i}", f"你好{i}")], force=True)
    out = buf.getvalue()

    fail_lines = [ln for ln in out.splitlines() if "setOverlayRaw 失败" in ln]
    assert len(fail_lines) == 1, f"失败日志只应报一次（降噪），实际 {len(fail_lines)} 条：\n{out}"
    assert "重建 overlay" in out, f"连续失败 3 次时应尝试重建 overlay：\n{out}"
    recreated = sum(1 for c in CALLS if c[0] == "createOverlay")
    assert recreated >= 1, f"没有真的重建（createOverlay 调用 {recreated} 次）"
    assert "恢复上传" in out, f"恢复时应有明确日志：\n{out}"
    assert ov._upload_fails == 0, f"恢复后失败计数应清零，实际 {ov._upload_fails}"
    assert sum(1 for c in CALLS if c[0] == "setOverlayRaw") >= 1, "重建后没有把内容贴回去"
    ov.close()
    print("  上传失败降噪 + 自动重建 + 恢复日志 OK")


def test_upload_failure_keeps_retrying_rebuild() -> None:
    """★ 回归：上传一直失败时要**反复**重建，不能只在第 3 次试一次。

    用户实测日志（2026-09-25 19:26）：第 3 次失败时重建过一次，之后又连续失败
    50/100/150 次，**再也没有第二次补救** —— 手腕屏一直停在最后一帧，直到用户
    在 19:28 手动重启引擎。「自愈只试一次」等于没自愈。
    """
    import contextlib
    import io

    CALLS.clear()
    RAW_FAIL_PLAN["remaining"] = 10 ** 9          # 一直失败
    _install_fake_openvr()
    from vlt.output.overlay import WristOverlay

    ov = WristOverlay(_cfg(Path(".")))
    ov.start()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            for i in range(60):
                ov.update_entries([("theirs", f"hi{i}", f"你好{i}")], force=True)
        out = buf.getvalue()
        recreated = sum(1 for c in CALLS if c[0] == "createOverlay")
        assert recreated >= 3, \
            f"持续失败时应反复重建（start 一次 + 第 3 次 + 第 50 次），实际 createOverlay {recreated} 次：\n{out}"
        assert "已连续失败 50 次" in out, f"缺 50 次的汇总日志（降噪不能把信息降没）：\n{out}"
        assert ov._upload_fails >= 60, f"失败计数不应被重建清零（要能继续累计触发下一次），实际 {ov._upload_fails}"
    finally:
        RAW_FAIL_PLAN["remaining"] = 0
        ov.close()
    print(f"  持续失败会反复重建 OK（createOverlay {recreated} 次）")


if __name__ == "__main__":
    print("test_overlay_steamvr:")
    test_start_takes_over_steamvr()
    test_ivrsystem_has_no_overlay_attribute()
    test_overlay_failure_degrades_without_raising()
    test_update_uploads_texture()
    test_hot_reload_reapplies_geometry()
    test_hot_reload_font_rerenders()
    test_upload_failure_recovers_and_logs_quietly()
    test_upload_failure_keeps_retrying_rebuild()
    print("ALL PASSED")
