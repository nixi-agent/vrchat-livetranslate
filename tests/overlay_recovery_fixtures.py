"""手腕屏「面板不可用自愈断链」+「兼容模式（旧路）降级留痕 / 主动重建」的离线守卫。

## 为什么要有这个测试（三条实测出来的真问题）

1. **自愈断链**：`openvr_overlay.WristOverlay` 里上传 / 心跳 / 热重载**全都**有
   `if not self.available` 早退，而 `_hard_reinit()` 失败会把 `available` 置 False。
   于是从那一刻起：没有任何上传会发生 → 没有失败 → `_escalate()` 再也不会被调用 →
   `_hard_reinit` 里那句「下轮失败时再试」**永远等不到**。用户看到的就是「手腕屏内容不
   再更新 / 比桌面慢」而日志一片安静。判据：`available=False` 后 tick 必须能自己救回来，
   且退避有界（不能变成重建风暴）。

2. **兼容模式（`setOverlayRaw` 旧路）的僵住**：旧路每约 200 次上传必挂
   （`OverlayError_RequestFailed`，见 `vlt/output/win_d3d11.py` 的 docstring 与用户真机日志
   「重建=20/硬重启=20」）。原来**等它挂了**才靠自愈阶梯救 → 症状（僵住数秒）已经发生。
   现在按帧数主动重建，把「僵住数秒」换成「一次极短的刷新」。

3. **降级必须响亮**：用户报「手腕屏比桌面晚几秒」时，如果只打一行日志、界面什么都不说，
   他只能靠猜。`degraded_note` 由引擎每帧转发到状态栏（`Engine._relay_overlay_note`）。

全部离线：假 openvr + 假 D3D，不碰真实 steamvr/d3d11。
"""

from __future__ import annotations

import ast

import contextlib

import io

import sys

import time

import types

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

import vlt.output.openvr_overlay as _oo_mod  # noqa: E402  取模块常量的原始值（用例会临时改）

_ORIG_TUNING = (_oo_mod.REINIT_BACKOFF_S, _oo_mod.REINIT_BACKOFF_MAX_S,
                _oo_mod.RAW_REBUILD_EVERY_FRAMES)

CALLS: list[tuple] = []

INIT_FAIL = {"on": False, "count": 0}   # True → openvr.init() 抛错（模拟 SteamVR 换代/没起来）

TEX_UNSUPPORTED = {"on": False}         # True → setOverlayTexture 抛 InvalidTexture

HAS_TEX_API = {"on": True}              # False → 假 overlay 干脆没有 setOverlayTexture

class _FakeHmdMatrix34_t:
    def __init__(self) -> None:
        self.m = [[0.0] * 4 for _ in range(3)]

class _FakeTexture_t:
    def __init__(self) -> None:
        self.handle = None
        self.eType = -1
        self.eColorSpace = -1

class _RawOnlyOverlay:
    """只有旧路接口的 overlay（老 SteamVR 的真实形状）。"""

    def createOverlay(self, key, name):  # noqa: ANN001, ANN201
        CALLS.append(("createOverlay", key))
        return 7

    def destroyOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("destroyOverlay", handle))

    def setOverlayWidthInMeters(self, handle, w):  # noqa: ANN001
        CALLS.append(("setOverlayWidthInMeters", round(float(w), 4)))

    def setOverlayAlpha(self, handle, a):  # noqa: ANN001
        CALLS.append(("setOverlayAlpha", round(float(a), 4)))

    def setOverlayCurvature(self, handle, c):  # noqa: ANN001
        CALLS.append(("setOverlayCurvature", round(float(c), 4)))

    def setOverlayTransformTrackedDeviceRelative(self, handle, device, mat):  # noqa: ANN001
        CALLS.append(("setOverlayTransformTrackedDeviceRelative", device))

    def setOverlayRaw(self, handle, buf, w, h, depth):  # noqa: ANN001
        CALLS.append(("setOverlayRaw", w, h, depth))

    def showOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("showOverlay", handle))

    def findOverlay(self, key):  # noqa: ANN001, ANN201
        return 7

    def isOverlayVisible(self, handle):  # noqa: ANN001, ANN201
        return True

class _TexOverlay(_RawOnlyOverlay):
    """新路接口也在的 overlay。"""

    def setOverlayTexture(self, handle, tex):  # noqa: ANN001
        if TEX_UNSUPPORTED["on"]:
            raise RuntimeError("OverlayError_InvalidTexture")
        CALLS.append(("setOverlayTexture", handle, tex.eType,
                      getattr(tex.handle, "value", tex.handle)))

def _install_fake_openvr() -> types.ModuleType:
    m = types.ModuleType("openvr")
    m.VRApplication_Background = 3
    m.TrackedControllerRole_LeftHand = 1
    m.TrackedControllerRole_RightHand = 2
    m.TrackedDeviceClass_GenericTracker = 4
    m.k_unMaxTrackedDeviceCount = 64
    m.k_unTrackedDeviceIndex_Hmd = 0
    m.TextureType_DirectX = 0
    m.TextureType_DXGISharedHandle = 5
    m.ColorSpace_Auto = 0
    m.HmdMatrix34_t = _FakeHmdMatrix34_t
    m.Texture_t = _FakeTexture_t

    def _init(_app_type):  # noqa: ANN001, ANN202
        INIT_FAIL["count"] += 1
        if INIT_FAIL["on"]:
            raise RuntimeError("InitError_Init_NoServerForBackgroundApp")
        return types.SimpleNamespace()

    m.init = _init
    m.VR_IsHmdPresent = lambda: True
    m.IVROverlay = lambda: (_TexOverlay() if HAS_TEX_API["on"] else _RawOnlyOverlay())
    m.shutdown = lambda: CALLS.append(("shutdown",))
    sys.modules["openvr"] = m
    return m

class FakeD3D:
    instances: list["FakeD3D"] = []

    def __init__(self, width: int, height: int, fmt: int = 28, base=None) -> None:  # noqa: ANN001
        self.width, self.height = width, height
        self.texture, self.srv, self.shared_handle = 0x1234, 0x5678, 0xDEAD
        self.uploads, self.closed = 0, False
        FakeD3D.instances.append(self)

    def upload(self, data: bytes) -> None:
        self.uploads += 1

    def healthy(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

class FailOnceD3D(FakeD3D):
    """第一次构造就抛（TDR 复位窗口），之后正常。"""

    attempts = 0

    def __init__(self, width: int, height: int, fmt: int = 28, base=None) -> None:  # noqa: ANN001
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("D3D11CreateDevice 失败 hr=0x887A0005")
        super().__init__(width, height, fmt, base)

def _fresh(d3d_cls=FakeD3D, tex_api: bool = True):
    CALLS.clear()
    FakeD3D.instances.clear()
    INIT_FAIL["on"] = False
    INIT_FAIL["count"] = 0
    TEX_UNSUPPORTED["on"] = False
    HAS_TEX_API["on"] = tex_api
    _install_fake_openvr()
    import vlt.output.openvr_overlay as oo
    oo.D3D11Texture = d3d_cls
    # ⚠️ 用例会就地改这些模块常量（缩短等待/帧数）→ 每个用例开头必须还原，
    #    否则「上一个用例改的」会静默污染下一个（实测：退避测试被前一个用例的 0.0 带跑偏）。
    oo.REINIT_BACKOFF_S, oo.REINIT_BACKOFF_MAX_S, oo.RAW_REBUILD_EVERY_FRAMES = _ORIG_TUNING
    return oo

def _cfg():
    from vlt.output.overlay import OverlayConfig

    return OverlayConfig()

def _count(name: str) -> int:
    return sum(1 for c in CALLS if c[0] == name)

def _names() -> list[str]:
    return [c[0] for c in CALLS]

__all__ = ['CALLS', 'FailOnceD3D', 'FakeD3D', 'INIT_FAIL', 'Path', 'ROOT', 'TEX_UNSUPPORTED', '_cfg', '_count', '_fresh', '_names', 'ast', 'contextlib', 'io', 'time', 'types']
