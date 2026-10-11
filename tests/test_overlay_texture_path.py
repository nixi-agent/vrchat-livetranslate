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


def test_d3d_device_loss_rebuilds_texture() -> None:
    """D3D11 设备丢失（TDR / 驱动重置 / 适配器移除）→ 必须重建**持久纹理**。

    实测过不补这道时的形态：坏设备一直挂着 —— 连失败只让 `_recreate_overlay`/`_hard_reinit`
    反复重建 SteamVR 一侧（还会 shutdown+init openvr），面板永远回不来。
    判据不是「没抛异常」，而是**建出了新的纹理实例**、且后续帧重新走新纹理 + `setOverlayTexture`。
    """
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    dead = FakeD3D.instances[0]

    def _boom(_data: bytes) -> None:
        raise RuntimeError("Map(源纹理) 失败 hr=0x887A0005")     # DEVICE_REMOVED

    dead.upload = _boom                       # type: ignore[method-assign]
    dead.healthy = lambda: False              # type: ignore[method-assign]

    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):     # 顺便钉住「日志报的是真实那一步」
        for _ in range(5):                    # 连续失败到触发自愈阶梯
            ov.update_entries([("theirs", "Hello", "你好")], force=True)
    out = buf.getvalue()
    assert "D3D11 upload 失败" in out, f"失败日志应报出真实的操作（D3D11 upload）：{out[-400:]}"
    assert "setOverlayTexture 失败" not in out, (
        f"D3D 侧失败被报成了 overlay 接口失败（排障方向会被带偏）：{out[-400:]}")

    assert len(FakeD3D.instances) >= 2, (
        f"D3D11 设备丢失后应重建出**新的**纹理实例（现在只有 {len(FakeD3D.instances)} 个）")
    fresh = FakeD3D.instances[-1]
    assert fresh is not dead and fresh.closed is False, "新纹理不该是坏的那个、也不该被关掉"
    assert fresh.uploads >= 1, "重建后应继续用**新**纹理上传（坏设备不该再被喂数据）"
    assert dead.uploads == 0, "坏设备不该再收到 upload"
    assert "setOverlayTexture" in _names(), f"重建后应重新绑定贴图：{_names()}"
    # 关键判据：设备丢失时**不该**走破坏性的 openvr 硬重启 —— 问题在 D3D 一侧，重建纹理就够了。
    # （踩过：只靠下面那条硬重启顺带重建，也会「碰巧」建出新纹理，用例就守不住第一级修复了。）
    assert "D3D11 设备已丢失" in out, f"没有走「设备丢失 → 重建纹理」这条修复：{out[-400:]}"
    assert not [c for c in CALLS if c[0] == "shutdown"], (
        f"D3D 设备丢失却去 shutdown+init 了整个 openvr 连接（破坏性，且救不回来）：{_names()}")
    ov.close()
    print("  D3D11 设备丢失 → 重建持久纹理 OK")


def test_recreate_overlay_rebuilds_when_device_dead() -> None:
    """设备已死时重建 overlay → 必须**重建纹理**，而不是把一张死纹理绑到新 handle 上。

    实测踩过（复审第 5 条）：`_recreate_overlay()` 原来无条件重绑 → 死纹理绑到新 handle，
    只能等下一轮**破坏性**的硬重启（还会 shutdown+init openvr）才有机会修回来。
    """
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    dead = FakeD3D.instances[0]
    dead.healthy = lambda: False              # type: ignore[method-assign]
    n_before = len(FakeD3D.instances)

    assert ov._recreate_overlay() is True
    assert len(FakeD3D.instances) > n_before, "设备已死却没重建纹理（等于把死纹理绑到新 handle）"
    assert dead.closed is True, "旧（已死）纹理应被 close"
    assert ov._d3d is not dead and ov._d3d is not None
    ov.close()
    print("  设备已死时重建 overlay → 一并重建纹理 OK")


def test_tex_path_retries_once_after_transient_failure() -> None:
    """「D3D 路建不起来」后应再给**一次**机会，否则本会话永久退回旧路。

    实测场景（复审第 1 条）：设备丢失后那次重建恰好在 TDR 复位窗口内失败 → `_d3d` 永久停在
    None，而 `_init_texture_path()` 只在 `start()` 调过一次 → 静默跑回 `setOverlayRaw`
    （正是本 PR 要消灭的那条老路）。判据：**建出来了**、且**只再试一次**（别对真死 GPU 反复建设备）。
    """
    FailOnceD3D.attempts = 0
    oo = _fresh(FailOnceD3D)
    oo.TEX_RETRY_AFTER_S = 0.0              # 测试里不等 2s
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    assert ov._d3d is None, "第一次构造失败 → 此刻应无 D3D 纹理（暂时走旧路）"

    ov.tick()                               # ← 定点重试在这里兑现（不依赖上传失败）
    assert ov._d3d is not None, "给了重试机会却没建出 D3D 纹理（会一直停在旧路）"
    assert FailOnceD3D.attempts == 2, f"只该再试一次，实际试了 {FailOnceD3D.attempts} 次"

    CALLS.clear()
    ov.update_entries([("mine", "你好", "Hello")], force=True)
    assert "setOverlayTexture" in _names() and "setOverlayRaw" not in _names(), (
        f"恢复后应稳定走 D3D 路：{_names()}")
    ov.tick()
    ov.tick()
    assert FailOnceD3D.attempts == 2, f"定点重试只该一次，实际 {FailOnceD3D.attempts} 次"
    ov.close()
    print("  start 期构造失败 → tick 定点重试一次并切回 D3D 路 OK")


def test_device_loss_rebuild_failure_then_retry_recovers() -> None:
    """复审第 1 条的**真场景**：设备已建好 → 设备丢失 → 那次重建瞬时失败 → 恢复。

    上一版把重试挂在 `_escalate` 且要求 `stage == "none"`：设备丢失分支先跑并把阶段推成
    "rebuild" → 判据恒不成立；raw 路随后成功又没人再调 `_escalate` → 会话静默跑回旧路。
    本用例按这条路径构造，断言 `tick()` 的定点重试救回来、且只试一次。
    """
    FailSecondD3D.attempts = 0
    oo = _fresh(FailSecondD3D)
    oo.TEX_RETRY_AFTER_S = 0.0
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    dead = FakeD3D.instances[0]
    dead.upload = _boom_upload                  # type: ignore[method-assign]
    dead.healthy = lambda: False                # type: ignore[method-assign]

    # ⚠️ 正好 3 帧：第 3 帧触发自愈阶梯（设备丢失分支 → 重建失败）。**别多跑**——第 4 帧的 raw 成功
    #    会把阶段重置回 "none"，那样「重建失败时阶段必须保持 none」这条判据就失去分辨力了（踩过）。
    for _ in range(3):
        ov.update_entries([("theirs", "Hello", "你好")], force=True)
    assert FailSecondD3D.attempts == 2, f"没走到「设备丢失后重建」：{FailSecondD3D.attempts} 次"
    assert ov._d3d is None, "那次重建应失败（模拟 TDR 复位窗口）"
    assert ov._recover_stage == "none", (
        f"重建失败时阶段必须保持 none（否则下一级直接是破坏性的硬重启）：{ov._recover_stage}")

    CALLS.clear()
    ov.tick()                                   # ← 定点重试
    assert ov._d3d is not None, "tick 的定点重试没建出纹理（会话会静默退回旧路）"
    assert FailSecondD3D.attempts == 3, f"应正好再试一次：{FailSecondD3D.attempts} 次"

    ov.update_entries([("mine", "你好", "Hello")], force=True)
    assert "setOverlayTexture" in _names() and "setOverlayRaw" not in _names(), (
        f"恢复后应稳定走 D3D 路：{_names()}")
    assert not [c for c in CALLS if c[0] == "shutdown"], "不该为此去硬重启 openvr"
    ov.tick()
    ov.tick()
    assert FailSecondD3D.attempts == 3, f"定点重试只该一次：{FailSecondD3D.attempts} 次"
    ov.close()
    print("  设备丢失→重建瞬时失败→tick 定点重试救回 OK")


def test_base_reuse_copy_list_only_names_the_class_sets() -> None:
    """`base=` 复用路径的拷贝表里，每个名字都必须是**类里真的会设置**的属性。

    实测踩过：表里留了 `_update`，而本模块刻意不绑 `UpdateSubresource`（共享纹理不能直写）
    → 任何 `D3D11Texture(..., base=...)` 必抛 `AttributeError`，而生产路径不用 base=，
    会被上层 `except` 吞掉静默降级回 raw —— 一用就炸却没人发现（色空间比对就是唯一用户）。
    这条守卫是静态的（AST 读源码，不开设备）：新增/改名时不至于再漏。
    """
    import ast
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import vlt.output.win_d3d11 as _wd          # 只读源码做静态检查，不构造 D3D 设备

    src = pathlib.Path(_wd.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "D3D11Texture")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")

    copied: list[str] = []
    for node in ast.walk(init):
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            copied += [e.value for e in node.iter.elts
                       if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    assert copied, "没解析出 __init__ 里的拷贝表（for attr in (...)）"

    assigned: set[str] = set()
    for node in ast.walk(cls):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:                      # 元组赋值（self.a, self.b = ...）也要算
            for t in ast.walk(target):
                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    assigned.add(t.attr)
    missing = sorted(set(copied) - assigned)
    assert not missing, (
        f"拷贝表里的这些名字类里根本不会设置（base= 复用路径必炸）：{missing}；"
        f"类里实际会设置的：{sorted(assigned)}")
    # 顺带守住 base= 分支的结构：先定归属（失败路径才知道该不该放 device/ctx）→ 建纹理 → 标记非自建。
    base_branch = [n for n in ast.walk(init) if isinstance(n, ast.If)]
    src_text = ast.unparse(init)
    assert "_owns_device = False" in src_text and "_make_shared_pair(fmt)" in src_text, (
        "base= 复用分支少了「定归属 / 建共享纹理」其中之一")
    assert src_text.index("_owns_device = False") < src_text.index("_make_shared_pair(fmt)"), (
        "base= 分支必须先定 _owns_device 再建纹理（否则构造失败时释放归属无从判断）")
    assert "_release_owned" in ast.unparse(cls), "构造失败路径应复用 _release_owned() 释放半成品"
    assert base_branch, "基类分支解析异常"
    print(f"  base= 拷贝表与实例属性一致（{len(set(copied))} 项）+ 分支结构 OK")


def test_upload_flushes_after_copy() -> None:
    """`upload()` 必须在 `CopyResource` **之后** Flush，且 `flush=False` 真的能关掉它。

    为什么这条离线用例非有不可：共享贴图的命令是排进 GPU 队列的，不 Flush 时跨进程的
    vrcompositor 会一直读到**上一帧**（用户报的「手腕屏比桌面慢几秒」）。
    真机判据在 `scripts/verify/verify_d3d11_shared_readback.py`（独立设备/独立进程回读：
    不 Flush 12/12 帧读到旧帧；Flush 后 12/12 全新）。这条用例守的是**代码形状**：
    顺序错（先 Flush 后 Copy）、漏绑 `_flush`、或 `flush=False` 开关失效，都必须立刻变红。

    手法：`object.__new__` 绕开 `__init__`（不建真实 D3D 设备），只把 upload 依赖的
    几个绑定换成记录用的假函数。
    """
    import ctypes as _ct

    from vlt.output.win_d3d11 import D3D11_MAPPED_SUBRESOURCE, D3D11Texture, S_OK

    calls: list[str] = []
    mapped_buf = _ct.create_string_buffer(64)   # 保持引用：cast 出来的指针不能悬空
    tex = D3D11Texture.__new__(D3D11Texture)          # 绕开 __init__：不碰真实设备
    tex.width, tex.height, tex.row_pitch = 4, 2, 16

    def fake_map(_ctx, _res, _idx, _flag, _n, out):  # noqa: ANN001, ANN202
        calls.append("map")
        out._obj.pData = _ct.cast(mapped_buf, _ct.c_void_p).value
        out._obj.RowPitch = 16
        return S_OK

    def fake_unmap(_ctx, _res, _idx):  # noqa: ANN001
        calls.append("unmap")

    def fake_copy(_ctx, _dst, _src):  # noqa: ANN001
        calls.append("copy")

    def fake_flush(_ctx):  # noqa: ANN001
        calls.append("flush")

    tex._map, tex._unmap, tex._copy, tex._flush = fake_map, fake_unmap, fake_copy, fake_flush
    tex.ctx, tex._src, tex.texture = 0x1, 0x2, 0x3

    tex.upload(bytes(tex.row_pitch * tex.height))
    assert calls == ["map", "unmap", "copy", "flush"], (
        f"upload() 的调用顺序必须是 map→unmap→copy→flush（Flush 必须在 CopyResource 之后）：{calls}")

    calls.clear()
    tex.upload(bytes(tex.row_pitch * tex.height), flush=False)
    assert calls == ["map", "unmap", "copy"], (
        f"flush=False 必须真的不 Flush（验证脚本靠它做修复前/后的 A/B）：{calls}")

    # 常量来源：mingw-w64 `d3d11.h` 的 ID3D11DeviceContextVtbl 与 wine 的 `d3d11.idl`
    # 两处解析都给出 111（见 win_d3d11.py docstring）。写错一个索引 = 调到别的 COM 方法。
    import vlt.output.win_d3d11 as _wd
    assert _wd.CTX_FLUSH == 111, f"ID3D11DeviceContext::Flush 的虚表索引应为 111：{_wd.CTX_FLUSH}"
    assert hasattr(tex, "_flush"), "upload 依赖的 _flush 绑定必须存在"
    _ = D3D11_MAPPED_SUBRESOURCE
    print("  upload() 在 CopyResource 之后 Flush（且 flush=False 可关）OK")


if __name__ == "__main__":
    print("test_overlay_texture_path:")
    test_upload_flushes_after_copy()
    test_texture_path_uses_setoverlaytexture()
    test_d3d_unavailable_falls_back_to_raw()
    test_unsupported_texture_permanent_fallback_logs_once()
    test_recreate_and_reinit_rebind_texture()
    test_size_change_rebuilds_texture()
    test_d3d_device_loss_rebuilds_texture()
    test_recreate_overlay_rebuilds_when_device_dead()
    test_device_loss_rebuild_failure_then_retry_recovers()
    test_tex_path_retries_once_after_transient_failure()
    test_base_reuse_copy_list_only_names_the_class_sets()
    print("ALL PASSED")
