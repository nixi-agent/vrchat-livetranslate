"""离线验证手腕屏的「持久 D3D11 贴图 + SetOverlayTexture」上传路径（**不开真实设备/SteamVR**）。

## 为什么要有这个测试

`vlt/output/openvr_overlay.py` 新增了一条上传路：自建 D3D11 设备 + 一张只建一次的持久纹理，
每帧只 `UpdateSubresource` 更新内容再 `SetOverlayTexture`，用来替掉每帧新建贴图、约 200 次就
撞墙的 `setOverlayRaw`（详见 `vlt/output/win_d3d11.py` 的模块 docstring）。真机出屏与 600 次
连刷已由用户侧 spike 验过；本测试只负责**接线正确性**，全部用假对象注入，绝不碰真实 D3D/SteamVR：

  · 有 D3D 层（假对象注入）→ `_upload` 走 `setOverlayTexture`，且**不**调用 `setOverlayRaw`；
  · D3D 建不起来 → 降级走 `setOverlayRaw`，且有一行降级留痕；
  · `setOverlayTexture` 报「不支持 / InvalidTexture」→ **永久回退**且**只留痕一次**；
  · 重建 / 硬重启后 → `setOverlayTexture` 被重新调用（新 handle 上没有任何贴图）；
  · `size_px` 变化后 → D3D 纹理被**重建**以匹配新尺寸。

注入手法：把 `vlt.output.openvr_overlay.D3D11Texture` 换成假类（`_init_texture_path` /
`_rebuild_d3d` 在调用时才查这个名字），再装一个**带 `setOverlayTexture` 的假 openvr**。
（现有的 `tests/test_overlay_steamvr.py` 用的假 openvr **没有** `setOverlayTexture`，因此那套
用例天然走旧的 raw 路，行为一字不变 —— 见 `_init_texture_path` 的能力判据。）
"""
from __future__ import annotations

import contextlib
import io
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CALLS: list[tuple] = []
TEX_UNSUPPORTED = {"on": False}     # True → setOverlayTexture 抛 InvalidTexture（模拟不支持）
RAW_FAIL = {"remaining": 0}         # >0 时 setOverlayRaw 抛错


# ------------------------------------------------------------------ 假 openvr
class _FakeHmdMatrix34_t:
    def __init__(self) -> None:
        self.m = [[0.0] * 4 for _ in range(3)]


class _FakeTexture_t:
    def __init__(self) -> None:
        self.handle = None
        self.eType = -1
        self.eColorSpace = -1


class _FakeIVROverlay:
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
        CALLS.append(("setOverlayTransformTrackedDeviceRelative", device))

    def setOverlayRaw(self, handle, buf, w, h, depth):  # noqa: ANN001
        if RAW_FAIL["remaining"] > 0:
            RAW_FAIL["remaining"] -= 1
            raise RuntimeError("OverlayError_RequestFailed: 模拟贴图上传失败")
        CALLS.append(("setOverlayRaw", w, h, depth))

    def setOverlayTexture(self, handle, tex):  # noqa: ANN001
        if TEX_UNSUPPORTED["on"]:
            raise RuntimeError("OverlayError_InvalidTexture")
        CALLS.append(("setOverlayTexture", handle, tex.eType, tex.eColorSpace,
                      getattr(tex.handle, "value", tex.handle)))

    def showOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("showOverlay", handle))

    def findOverlay(self, key):  # noqa: ANN001, ANN201
        return 7

    def isOverlayVisible(self, handle):  # noqa: ANN001, ANN201
        return True

    def destroyOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("destroyOverlay", handle))


class _FakeIVRSystem:
    """**故意不提供 `.overlay` 属性** —— 真实 pyopenvr 就是这样。"""

    def getTrackedDeviceIndexForControllerRole(self, role):  # noqa: ANN001, ANN201
        return 3

    def getTrackedDeviceClass(self, i):  # noqa: ANN001, ANN201
        return 4


def _install_fake_openvr():
    m = types.ModuleType("openvr")
    m.VRApplication_Background = 3
    m.TrackedControllerRole_LeftHand = 1
    m.TrackedControllerRole_RightHand = 2
    m.TrackedDeviceClass_GenericTracker = 4
    m.k_unMaxTrackedDeviceCount = 64
    m.k_unTrackedDeviceIndex_Hmd = 0
    m.TextureType_DirectX = 0
    m.TextureType_DXGISharedHandle = 5      # 实测唯一可行的那条（见 win_d3d11 docstring）
    m.ColorSpace_Auto = 0
    m.HmdMatrix34_t = _FakeHmdMatrix34_t
    m.Texture_t = _FakeTexture_t
    m.init = lambda app_type: _FakeIVRSystem()
    m.VR_IsHmdPresent = lambda: True
    m.IVROverlay = lambda: _FakeIVROverlay()
    m.shutdown = lambda: CALLS.append(("shutdown",))
    sys.modules["openvr"] = m
    return m


# ------------------------------------------------------------------ 假 D3D11Texture
class FakeD3D:
    """替身：记录构造尺寸/上传次数/是否 close，绝不开真实设备。"""

    instances: list["FakeD3D"] = []

    def __init__(self, width: int, height: int, fmt: int = 28, base=None) -> None:  # noqa: ANN001
        self.width, self.height = width, height
        self.texture = 0x12340000
        self.srv = 0x56780000
        # 有意与 .texture 取不同值：调用方必须传 shared_handle（配 TextureType_DXGISharedHandle），
        # 传 .texture 是**实测被 SteamVR 一律拒绝**的旧配方（详见 win_d3d11.py docstring）。
        self.shared_handle = 0xDEAD0000
        self.uploads = 0
        self.closed = False
        FakeD3D.instances.append(self)

    def upload(self, data: bytes) -> None:
        self.uploads += 1

    def healthy(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _raising_d3d(width: int, height: int, fmt: int = 28, base=None):  # noqa: ANN001
    raise RuntimeError("D3D11CreateDevice 失败（HARDWARE/WARP 都不行）")


# ------------------------------------------------------------------ 工具
def _cfg():
    from vlt.output.overlay import OverlayConfig

    return OverlayConfig()


def _names() -> list[str]:
    return [c[0] for c in CALLS]


def _count(name: str) -> int:
    return sum(1 for c in CALLS if c[0] == name)


def _fresh(monkey_cls):
    """重置全局状态 + 装假 openvr + 注入假 D3D 类，返回 openvr_overlay 模块。"""
    CALLS.clear()
    FakeD3D.instances.clear()
    TEX_UNSUPPORTED["on"] = False
    RAW_FAIL["remaining"] = 0
    _install_fake_openvr()
    import vlt.output.openvr_overlay as oo
    oo.D3D11Texture = monkey_cls
    return oo


# ------------------------------------------------------------------ 用例
def test_texture_path_uses_setoverlaytexture() -> None:
    """有 D3D 层 → `_upload` 走 setOverlayTexture，且**不**碰 setOverlayRaw。"""
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    assert ov._d3d is not None, "start() 后应已建好持久 D3D 纹理"
    built = FakeD3D.instances[0]
    assert (built.width, built.height) == (1024, 440), f"纹理尺寸应取 size_px：{built.width}x{built.height}"
    assert _count("setOverlayTexture") >= 1, "start() 里应立刻绑一次贴图"

    CALLS.clear()
    ov.update_entries([("theirs", "Hello", "你好"), ("mine", "早上好", "Good morning")], force=True)
    names = _names()
    assert "setOverlayTexture" in names, f"没有走 SetOverlayTexture：{names}"
    assert "setOverlayRaw" not in names, f"D3D 路不该再调 setOverlayRaw：{names}"
    assert built.uploads >= 1, "没有把帧内容 upload 进持久纹理"
    # 交给 SteamVR 的语义必须是「DXGI 共享句柄 + DXGISharedHandle(5)」：本机真 SteamVR 实测，
    # eType=0 配 .texture / .srv 一律 OverlayError_InvalidTexture（用户机器上更阴——
    # 不报错但屏幕上什么都不画，所以这里必须把句柄也断言住，光看「没抛异常」不算证据）。
    _, _handle, etype, ecs, tex_handle = next(c for c in CALLS if c[0] == "setOverlayTexture")
    assert etype == 5, f"eType 必须是 DXGISharedHandle(5)，现在是 {etype}"
    assert tex_handle == built.shared_handle, (
        f"交给 SteamVR 的必须是 DXGI 共享句柄 0x{built.shared_handle:X}，现在是 0x{(tex_handle or 0):X}")
    assert tex_handle != built.texture, "传了 ID3D11Texture 指针 —— 这是实测被拒的旧配方"
    assert ecs == oo.OVERLAY_COLOR_SPACE, f"色空间应取模块常量：{ecs}"
    ov.close()
    print("  D3D 路走 setOverlayTexture（共享句柄 + eType=5）、不碰 setOverlayRaw OK")


def test_d3d_unavailable_falls_back_to_raw() -> None:
    """D3D 建不起来 → 降级 setOverlayRaw，且有一行降级留痕。"""
    oo = _fresh(_raising_d3d)
    ov = oo.WristOverlay(_cfg())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert ov.start() is True
    out = buf.getvalue()
    assert ov._d3d is None, "建不起来时不该留下 D3D 对象"
    assert "降级" in out and "setOverlayRaw" in out, f"缺降级留痕：{out}"

    CALLS.clear()
    ov.update_entries([("theirs", "Hello", "你好")], force=True)
    names = _names()
    assert "setOverlayRaw" in names, f"降级后应走 setOverlayRaw：{names}"
    assert "setOverlayTexture" not in names, f"降级后不该再碰 setOverlayTexture：{names}"
    ov.close()
    print("  D3D 不可用 → 降级 setOverlayRaw + 一行留痕 OK")


def test_unsupported_texture_permanent_fallback_logs_once() -> None:
    """setOverlayTexture 报「不支持/InvalidTexture」→ 永久回退，且**只留痕一次**。"""
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True               # 建的时候支持（还没打开不支持开关）
    first = FakeD3D.instances[0]
    CALLS.clear()                           # 抹掉 start() 那次成功绑定，只看回退这几帧
    TEX_UNSUPPORTED["on"] = True            # 之后 setOverlayTexture 一律报 InvalidTexture

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(4):
            ov.update_entries([("theirs", f"hi{i}", f"你好{i}")], force=True)
    out = buf.getvalue()

    assert ov._tex_disabled is True, "应置永久回退标志"
    assert ov._d3d is None, "永久回退时应释放 D3D 对象"
    assert first.closed is True, "回退时应 close 掉持久纹理"
    fallback_lines = [ln for ln in out.splitlines() if "永久回退" in ln]
    assert len(fallback_lines) == 1, f"永久回退只应留痕一次，实际 {len(fallback_lines)} 条：\n{out}"
    assert _count("setOverlayRaw") >= 1, f"回退后应改用 setOverlayRaw：{_names()}"
    # 回退后不再尝试 setOverlayTexture（第 1 帧失败那次之后再无）
    assert _count("setOverlayTexture") == 0, f"回退后不该再调 setOverlayTexture：{_names()}"

    TEX_UNSUPPORTED["on"] = False
    ov.close()
    print("  setOverlayTexture 不支持 → 永久回退且只留痕一次 OK")


def test_recreate_and_reinit_rebind_texture() -> None:
    """重建 / 硬重启后 setOverlayTexture 被重新调用（新 handle 上没有任何贴图）。"""
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.update_entries([("theirs", "Hello", "你好")], force=True)   # 有点内容，重建后会回贴

    CALLS.clear()
    assert ov._recreate_overlay() is True
    assert _count("setOverlayTexture") >= 1, f"重建后没有重新绑贴图：{_names()}"

    CALLS.clear()
    assert ov._hard_reinit() is True
    assert ("shutdown",) in CALLS, "硬重启应真的 shutdown openvr"
    assert _count("setOverlayTexture") >= 1, f"硬重启后没有重新绑贴图：{_names()}"
    ov.close()
    print("  重建 / 硬重启后重新 setOverlayTexture OK")


def test_size_change_rebuilds_texture() -> None:
    """size_px 变化后 D3D 纹理被重建以匹配新尺寸（否则 upload 字节数对不上）。"""
    oo = _fresh(FakeD3D)
    d = ROOT / "out" / "overlay_cfg_tex"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "config.yaml"
    f.write_text("overlay:\n  size_px: [1024, 440]\n", encoding="utf-8")

    from vlt.output.overlay import OverlayConfig
    ov = oo.WristOverlay(OverlayConfig(), config_path=f)
    assert ov.start() is True
    assert (ov._d3d.width, ov._d3d.height) == (1024, 440)
    old = ov._d3d
    ov.update_entries([("theirs", "Hello", "你好")], force=True)   # 有内容，热重载后会重渲

    f.write_text("overlay:\n  size_px: [800, 600]\n", encoding="utf-8")
    ov.tick()

    assert ov.cfg.size_px == (800, 600), f"配置没热重载：{ov.cfg.size_px}"
    assert ov._d3d is not None and ov._d3d is not old, "尺寸变化后应重建出**新的** D3D 纹理"
    assert (ov._d3d.width, ov._d3d.height) == (800, 600), f"新纹理尺寸不对：{ov._d3d.width}x{ov._d3d.height}"
    assert old.closed is True, "旧纹理应被 close"
    assert any((t.width, t.height) == (800, 600) for t in FakeD3D.instances), "没有按新尺寸建纹理"
    ov.close()
    print("  size_px 变化 → D3D 纹理重建 OK")


if __name__ == "__main__":
    print("test_overlay_texture_path:")
    test_texture_path_uses_setoverlaytexture()
    test_d3d_unavailable_falls_back_to_raw()
    test_unsupported_texture_permanent_fallback_logs_once()
    test_recreate_and_reinit_rebind_texture()
    test_size_change_rebuilds_texture()
    print("ALL PASSED")
