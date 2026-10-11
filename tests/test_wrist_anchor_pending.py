"""锚点设备还没被追踪时，**不许把整块面板判死** —— 用户报的「非翻译模式下强制打开手腕屏不行」。

## 为什么必须有这个测试

`start()` 里 `_apply_transform()` 是紧跟 `createOverlay()` 的。锚点设备（`right_hand` 手柄 /
tracker）**没被 SteamVR 追踪**时它会抛 `OverlayError_InvalidTrackedDevice`（本机真 SteamVR
实测：SteamVR 明明在跑，只是没头显 / 手柄没醒），旧写法让整个 `start()` 失败 → 界面把勾选
退回、只给一句「先把 SteamVR 打开」。用户照着那句话重试是**白费力气**：面板本身建得出来，
差的只是「贴到哪台设备上」。而这种情况恰恰是「不翻译、人在桌面前勾一下手腕屏」时的常态。

现在的口径：
  * 锚点类失败 → 面板**留着**（`available=True`、不 destroyOverlay，免得留 KeyInUse 残留）、
    进 `pending_anchor` 状态，交给 `tick()` 里的 `_maybe_apply_anchor` 定期重试；
    设备一被追踪面板就自己出现，不需要用户再勾一次。
  * **非锚点类**失败（贴图 / 句柄 / 其它）→ 照旧判失败并销毁本次 handle（不许被容错盖住）。
  * `close()` 过的实例绝不复活（用户取消勾选后，屏不许自己回来）。

跑法：`.venv/Scripts/python.exe tests/test_wrist_anchor_pending.py`
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CALLS: list[tuple] = []
TRACKED = {"on": False}          # False = 锚点设备还没被追踪（真机实测的失败条件）
FAIL_KIND = {"kind": "anchor"}   # anchor = InvalidTrackedDevice；other = 贴图/句柄类错误


class _FakeHmdMatrix34_t:
    def __init__(self) -> None:
        self.m = [[0.0] * 4 for _ in range(3)]


class _FakeIVRSystem:
    def getTrackedDeviceIndexForControllerRole(self, role):  # noqa: ANN001, ANN201
        return 3

    def getTrackedDeviceClass(self, i):  # noqa: ANN001, ANN201
        return 4


class _FakeIVROverlay:
    """只复刻本测试用到的 API 形状；`setOverlayTransform…` 按 TRACKED 决定是否抛。"""

    def createOverlay(self, key, name):  # noqa: ANN001, ANN201
        CALLS.append(("createOverlay", key))
        return 7

    def showOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("showOverlay",))

    def destroyOverlay(self, handle):  # noqa: ANN001
        CALLS.append(("destroyOverlay",))

    def findOverlay(self, key):  # noqa: ANN001, ANN201
        return 7

    def isOverlayVisible(self, handle):  # noqa: ANN001, ANN201
        return True

    def setOverlayTransformTrackedDeviceRelative(self, handle, device, mat):  # noqa: ANN001
        CALLS.append(("setTransform", device))
        if TRACKED["on"]:
            return
        raise RuntimeError("OverlayError_InvalidTrackedDevice" if FAIL_KIND["kind"] == "anchor"
                           else "OverlayError_RequestFailed")

    def setOverlayWidthInMeters(self, handle, w):  # noqa: ANN001
        CALLS.append(("setWidth", round(float(w), 4)))

    def setOverlayAlpha(self, handle, a):  # noqa: ANN001
        CALLS.append(("setAlpha", round(float(a), 4)))

    def setOverlayCurvature(self, handle, c):  # noqa: ANN001
        CALLS.append(("setCurvature", round(float(c), 4)))


def _install_fake_openvr() -> None:
    m = types.ModuleType("openvr")
    m.VRApplication_Background = 3
    m.TrackedControllerRole_LeftHand = 1
    m.TrackedControllerRole_RightHand = 2
    m.TrackedDeviceClass_GenericTracker = 4
    m.k_unMaxTrackedDeviceCount = 64
    m.k_unTrackedDeviceIndex_Hmd = 0
    m.HmdMatrix34_t = _FakeHmdMatrix34_t
    m.init = lambda app_type: _FakeIVRSystem()
    m.VR_IsHmdPresent = lambda: True
    m.IVROverlay = _FakeIVROverlay
    m.shutdown = lambda: CALLS.append(("shutdown",))
    sys.modules["openvr"] = m


def _cfg():
    from vlt.output.overlay import OverlayConfig

    cfg = OverlayConfig()
    cfg.anchor = "right_hand"
    return cfg


def _names() -> list[str]:
    return [c[0] for c in CALLS]


def _new_overlay():
    from vlt.output.openvr_overlay import WristOverlay

    # config_path=None：别去读真实 config.yaml（测试纪律）
    return WristOverlay(_cfg(), config_path=None)


def _reset(kind: str = "anchor", tracked: bool = False) -> None:
    CALLS.clear()
    TRACKED["on"] = tracked
    FAIL_KIND["kind"] = kind
    _install_fake_openvr()


def test_anchor_pending_keeps_panel_and_retries_until_ready() -> None:
    """锚点没被追踪 → 面板留着 + 勾选不算失败；设备就绪后由 tick 贴上，不用用户再勾一次。"""
    _reset(kind="anchor", tracked=False)
    ov = _new_overlay()
    ok = ov.start()

    assert ok is True, "锚点设备还没被追踪**不该**让整个面板判失败（用户报的『勾了打不开』就是这条）"
    assert ov.available is True and ov.alive is True, "面板必须仍然活着"
    assert ov.pending_anchor is True, "应进入「等锚点」状态"
    assert "showOverlay" in _names(), "面板必须仍然显示出来"
    assert "destroyOverlay" not in _names(), \
        "不许把刚建好的面板销毁（会往 SteamVR 里留同名 key → 下次撞 KeyInUse）"

    n_transforms = _names().count("setTransform")
    ov.tick()                                       # 还没到重试点：不该重试
    assert _names().count("setTransform") == n_transforms, "未到 ANCHOR_RETRY_S 不该重试"

    ov._anchor_retry_at = 0.0                       # 把时间推到重试点
    ov.tick()
    assert _names().count("setTransform") == n_transforms + 1, "到点必须重试一次应用变换"
    assert ov.pending_anchor is True, "还没有设备 → 应继续保持「等锚点」"

    TRACKED["on"] = True                            # 用户唤醒手柄 / 戴上头显
    ov._anchor_retry_at = 0.0
    ov.tick()
    assert ov.pending_anchor is False, "设备就绪后必须清掉「等锚点」状态"
    assert ("setWidth", 0.23) in CALLS or any(c[0] == "setWidth" for c in CALLS), \
        "就绪后应把宽度/透明度/弯曲一起补上（走的是同一条 _apply_transform）"
    ov.close()
    print("  锚点没就绪→面板留着+定期重试；就绪→自动贴上 OK")


def test_non_anchor_error_still_fails() -> None:
    """非锚点类错误（贴图/句柄…）照旧必须判失败并销毁 handle —— 容错别把真问题盖住。"""
    _reset(kind="other", tracked=False)
    ov = _new_overlay()
    ok = ov.start()

    assert ok is False, "非锚点类错误必须照旧判失败（不许被锚点容错顺手放过）"
    assert ov.available is False
    assert ov.pending_anchor is False
    assert "destroyOverlay" in _names(), "失败路径必须销毁本次 handle（防残留 KeyInUse）"
    print("  非锚点类错误仍然判失败 + 销毁 handle OK")


def test_close_never_resurrects_panel() -> None:
    """取消勾选（close）后，tick 不许再把面板「自己弄回来」。"""
    _reset(kind="anchor", tracked=False)
    ov = _new_overlay()
    assert ov.start() is True and ov.pending_anchor is True
    ov.close()
    assert ov.pending_anchor is False, "close() 必须清掉「等锚点」状态"

    TRACKED["on"] = True
    ov._anchor_retry_at = 0.0
    n_transforms = _names().count("setTransform")
    ov.tick()
    assert _names().count("setTransform") == n_transforms, \
        "close() 过的实例不许复活（取消勾选后屏自己回来 = 状态与界面不符）"
    print("  close() 之后不再自愈 OK")


def main() -> int:
    tests = (test_anchor_pending_keeps_panel_and_retries_until_ready,
             test_non_anchor_error_still_fails,
             test_close_never_resurrects_panel)
    for t in tests:
        print(f"[{t.__name__}]")
        t()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
