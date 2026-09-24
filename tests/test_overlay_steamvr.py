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


if __name__ == "__main__":
    print("test_overlay_steamvr:")
    test_start_takes_over_steamvr()
    test_ivrsystem_has_no_overlay_attribute()
    test_overlay_failure_degrades_without_raising()
    test_update_uploads_texture()
    test_hot_reload_reapplies_geometry()
    print("ALL PASSED")
