"""离线验证手腕屏的「持久 D3D11 贴图 + SetOverlayTexture」上传路径（**不开真实设备/SteamVR**）。

## 为什么要有这个测试

`vlt/output/openvr_overlay.py` 新增了一条上传路：自建 D3D11 设备 + 一张只建一次的持久纹理，
每帧更新它的内容再 `SetOverlayTexture`，用来替掉每帧新建贴图、约 200 次就撞墙的 `setOverlayRaw`
（详见 `vlt/output/win_d3d11.py` 的模块 docstring）。

⚠️ 内容更新走的是 `DYNAMIC + CPU_ACCESS_WRITE` 源纹理 → `Map(WRITE_DISCARD)` → `CopyResource`，
**不是** `UpdateSubresource`（对共享纹理直写会把它弄坏、之后永久 `InvalidTexture`）。
真机出屏与 700 次连刷已验（`scripts/verify_overlay_texture.py`）；本测试只负责**接线正确性**，
全部用假对象注入，绝不碰真实 D3D/SteamVR：

  · 有 D3D 层（假对象注入）→ `_upload` 走 `setOverlayTexture`，且**不**调用 `setOverlayRaw`；
  · D3D 建不起来 → 降级走 `setOverlayRaw`，且有一行降级留痕；
  · `setOverlayTexture` 报「不支持 / InvalidTexture」→ **永久回退**且**只留痕一次**；
  · 重建 / 硬重启后 → `setOverlayTexture` 被重新调用（新 handle 上没有任何贴图）；
  · `size_px` 变化后 → D3D 纹理被**重建**以匹配新尺寸；
  · **D3D11 设备丢失**（TDR / 驱动重置 / 适配器移除，`healthy()` 为假）→ 重建持久纹理，
    而不是一直挂在坏设备上（只重建 SteamVR 一侧是救不回来的）。

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

def _boom_upload(_data: bytes) -> None:
    """模拟设备丢失后 `Map()` 恒失败（DEVICE_REMOVED）。"""
    raise RuntimeError("Map(源纹理) 失败 hr=0x887A0005")

class FailSecondD3D(FakeD3D):
    """第 1 次构造成功、**第 2 次抛**（设备丢失后那次重建在 TDR 复位窗口内失败）、之后正常。"""

    attempts = 0

    def __init__(self, width: int, height: int, fmt: int = 28, base=None) -> None:  # noqa: ANN001
        type(self).attempts += 1
        if type(self).attempts == 2:
            raise RuntimeError("D3D11CreateDevice 失败 hr=0x887A0005（DEVICE_REMOVED）")
        super().__init__(width, height, fmt, base)

class FailOnceD3D(FakeD3D):
    """第一次构造就抛（模拟 TDR 复位窗口内 `D3D11CreateDevice` **瞬时**失败），之后正常。"""

    attempts = 0

    def __init__(self, width: int, height: int, fmt: int = 28, base=None) -> None:  # noqa: ANN001
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("D3D11CreateDevice 失败 hr=0x887A0005（DEVICE_REMOVED）")
        super().__init__(width, height, fmt, base)

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

__all__ = ['CALLS', 'FailOnceD3D', 'FailSecondD3D', 'FakeD3D', 'ROOT', 'TEX_UNSUPPORTED', '_boom_upload', '_cfg', '_count', '_fresh', '_names', '_raising_d3d', 'contextlib', 'io', 'sys']
