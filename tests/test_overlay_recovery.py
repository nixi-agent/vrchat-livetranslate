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


# ---------------------------------------------------------------- C：自愈断链
def test_unavailable_selfheals_and_repushes() -> None:
    """`available=False` 之后：退避窗口内不重试；到点必须自己救回来并重贴内容。

    故障注入：把 `tick()` 里的 `_maybe_reinit()` 删掉 → 本条红（面板永远停在最后一帧）。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.update_entries([("theirs", "Hello", "你好")], force=True)
    assert ov.available is True

    # —— 制造「硬重启失败」现场（真实成因：SteamVR 换代 / 合成器重启后接口指针全废）——
    INIT_FAIL["on"] = True
    ov.available = False
    oo.REINIT_BACKOFF_S = 30.0                      # 退避窗口 30s
    ov._schedule_reinit("硬重启失败")
    attempts = INIT_FAIL["count"]
    ov.tick()
    ov.tick()
    assert INIT_FAIL["count"] == attempts, (
        f"退避窗口内不该反复重试（会变成重建风暴）：{INIT_FAIL['count'] - attempts} 次")

    # —— 到点重试一次：仍然失败 → 保持不可用 + 重排下一次（不风暴、也不静默死掉）——
    oo.REINIT_BACKOFF_S = 0.0
    ov._reinit_at = time.monotonic() - 1.0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    assert INIT_FAIL["count"] - attempts == 1, "到点应正好重试一次"
    assert ov.available is False, "init 仍失败时不该假装可用"
    assert ov._reinit_at is not None, "失败后必须重排下一次（否则又变成永远不试）"
    assert "面板不可用 → 重试接管 SteamVR" in buf.getvalue(), f"缺重试留痕：{buf.getvalue()}"

    # —— SteamVR 回来了 → 同一条路必须把面板救回来，并把内容重贴回去 ——
    INIT_FAIL["on"] = False
    ov._reinit_at = time.monotonic() - 1.0
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    assert ov.available is True, f"init 恢复后 tick 没能救回面板：{ov.available}"
    assert ov._reinit_fails == 0, "救回来后退避计数要清零"
    assert "createOverlay" in _names(), f"没有重新接管 overlay：{_names()}"
    assert "setOverlayTexture" in _names() or "setOverlayRaw" in _names(), (
        f"恢复后没有把面板内容重贴回去：{_names()}")
    ov.close()
    print("  available=False → 退避重试自愈并重贴内容 OK")


def test_reinit_backoff_is_bounded() -> None:
    """退避必须**翻倍且封顶**（5s→10s→20s→40s→60s→60s）—— 真死 GPU 时不能风暴。"""
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    delays = []
    for _ in range(6):
        t0 = time.monotonic()
        ov._schedule_reinit("测试")
        delays.append(round(ov._reinit_at - t0))
    assert delays == [5, 10, 20, 40, 60, 60], f"退避节奏不对（应翻倍封顶 60s）：{delays}"
    ov.close()
    print(f"  不可用重试退避有界：{delays} OK")


def test_heartbeat_logs_even_when_unavailable() -> None:
    """心跳必须在不可用时**继续**打（其余路径全早退，日志一安静就再也查不出死在哪一刻）。"""
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.available = False
    ov.HEARTBEAT_S = 0.0                     # 立刻到点
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()
    assert "心跳" in out, f"不可用时心跳没了（等于事后无据可查）：{out}"
    assert "面板=不可用" in out, f"心跳里要能看出面板状态：{out}"
    assert "上传路=" in out, f"心跳里要能看出走的是哪条上传路：{out}"
    ov.close()
    print("  面板不可用时心跳仍留痕（含面板状态/上传路）OK")


# ---------------------------------------------------------------- B：兼容模式
def test_close_blocks_reinit_resurrection() -> None:
    """取消勾选（close）后，**已排定的**「重试接管」不许把面板复活（复审第 1 条）。

    场景：`available=False` → `_schedule_reinit()` 已排定 → 用户取消勾选 `close()`。
    旧守卫只看 `available`（close() 恰好把它置 False ⇒ 满足「可以往下走」）与
    `_reinit_at`（非 None）⇒ 下一次 tick 会 `_hard_reinit()` → `openvr.init()`，
    面板**自己回来**（正是本 PR 要消灭的那条症状，只不过换了一条路径）。
    判据：tick 之后不得出现 `init`/`shutdown`，且已排定的时间点被清掉。

    故障注入：把 `_maybe_reinit` 守卫里的 `self._closed` 删掉 → 本条红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.available = False                       # 模拟「面板不可用」
    ov._schedule_reinit("测试：已排定重试")
    assert ov._reinit_at is not None, "前置条件：应已排定一次重试"
    ov.close()                                 # 用户取消勾选

    # 护栏一（物理上不可能）：close() 把已排定的时间点直接作废。
    #   故障注入 B：删掉 `close()` 里的 `self._reinit_at = None` → 这一条红。
    assert ov._reinit_at is None, "close() 应把已排定的重试清掉（护栏要物理上不可能）"

    # 护栏二（守卫）：即使 `_reinit_at` 又被弄成「已到点」（旧状态残留 / 将来有人挪走清理），
    #   `_closed` 也必须拦住 —— ⚠️ 这一条**只有** `_closed` 能守：`available` 被 close()
    #   置成 False 恰好满足「可以重试」的条件。
    #   故障注入 A：删掉 `_maybe_reinit` 守卫里的 `self._closed` → 这一条红。
    ov._reinit_at = time.monotonic() - 1.0
    CALLS.clear()
    INIT_FAIL["count"] = 0
    ov._last_heartbeat = 0.0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()

    assert INIT_FAIL["count"] == 0, f"close() 之后 tick 又调了 openvr.init()（面板复活）：\n{out}"
    assert "shutdown" not in _names(), f"close() 之后 tick 又走了接管流程：\n{out}"
    assert ov.available is False, "close() 之后不该变成 available"
    print("  close() 之后「已排定的重试」不再复活面板 OK（两条护栏各验一次）")


def test_heartbeat_skips_closed_instance() -> None:
    """已 close() 的实例不再打心跳（少刷日志、少探活）—— 复审第 2 条。

    与 `test_heartbeat_logs_even_when_unavailable` 成对：**不可用**要留痕，**已关闭**要安静。
    判据用「探测次数 + 留痕」两条：`findOverlay`/`isOverlayVisible` 一次都不该被调。

    故障注入：把心跳条件里的 `not self._closed` 删掉 → 本条红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov._last_heartbeat = 0.0                   # 下一跳必到点
    ov.close()

    probes = {"find": 0, "vis": 0}
    ovl = ov._overlay

    def _find(key):  # noqa: ANN001, ANN202
        probes["find"] += 1
        return 7

    def _vis(handle):  # noqa: ANN001, ANN202
        probes["vis"] += 1
        return True

    ovl.findOverlay, ovl.isOverlayVisible = _find, _vis
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()

    assert "心跳" not in out, f"close() 之后还在打心跳：\n{out}"
    assert probes["find"] == 0 and probes["vis"] == 0, \
        f"close() 之后还在探活：findOverlay={probes['find']} isOverlayVisible={probes['vis']}"
    print("  close() 之后不再打心跳 / 不再探活 OK")


def test_raw_path_rebuilds_before_the_200_wall() -> None:
    """兼容模式（旧路）必须**主动**重建，而不是等每 ~200 次上传挂掉之后再自愈。

    故障注入：删掉 `_upload_raw` 里的主动重建 → `createOverlay` 只会有最初那一次 → 本条红。
    """
    oo = _fresh(tex_api=False)                 # 老 SteamVR：没有 setOverlayTexture
    oo.RAW_REBUILD_EVERY_FRAMES = 3            # 测试里缩短（生产 150）
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(3):                     # 第 3 帧到点 → 主动重建
            ov.update_entries([("theirs", f"hi{i}", f"你好{i}")], force=True)
    out = buf.getvalue()
    assert _count("createOverlay") == 1, f"应正好主动重建一次：{_names()}"
    assert _count("destroyOverlay") == 1, f"重建前应先销毁旧 overlay：{_names()}"
    assert "主动重建" in out, f"缺主动重建留痕：{out}"
    assert _count("setOverlayRaw") >= 4, (
        f"重建后必须把面板内容重贴回去（3 帧 + 重贴 1 帧）：{_names()}")
    ov.close()
    print("  兼容模式按帧数主动重建（不等 ~200 次挂掉）OK")


def test_degraded_note_is_loud_and_clears_on_recovery() -> None:
    """降级必须**响亮可查**：`degraded_note` 要有面向用户的说明 + 日志说清代价；恢复后清空。"""
    # ① 老 SteamVR（接口没有 setOverlayTexture）→ 兼容模式：说明 + 留痕
    oo = _fresh(tex_api=False)
    ov = oo.WristOverlay(_cfg())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert ov.start() is True
    out = buf.getvalue()
    assert ov.degraded_note and "兼容" in ov.degraded_note, f"降级说明缺失：{ov.degraded_note!r}"
    assert "不支持贴图直传" in out, f"缺降级留痕：{out}"
    ov.close()

    # ② setOverlayTexture 报不支持 → 永久回退：日志要把**已知代价**写出来（用户可感知）
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    TEX_UNSUPPORTED["on"] = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.update_entries([("theirs", "Hello", "你好")], force=True)
    out = buf.getvalue()
    assert ov.degraded_note, "永久回退后必须有面向用户的降级说明"
    assert "已知代价" in out and "200" in out, f"没把旧路的已知代价说清楚：{out}"
    assert ov._tex_disabled is True
    ov.close()

    # ③ 瞬时失败 → tick 定点重试成功 → 说明必须被清掉（否则状态栏会一直挂着警告）
    FailOnceD3D.attempts = 0
    oo = _fresh(FailOnceD3D)
    oo.TEX_RETRY_AFTER_S = 0.0
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    assert ov.degraded_note, "首次构造失败后应给出「暂时降级」说明"
    ov.tick()                                  # 定点重试 → 成功
    assert ov._d3d is not None
    assert ov.degraded_note is None, f"恢复后降级说明必须清空：{ov.degraded_note!r}"
    ov.close()
    print("  降级说明响亮可查、恢复后清空 OK")


def test_engine_relays_degraded_note_once() -> None:
    """引擎必须把降级说明转发到状态栏（**只在变化时**一次），并且在 tick 里调用它。"""
    from vlt.engine import Engine, EngineEvents
    from vlt.output.overlay import DEGRADED_NOTE_RECOVERED, DegradedNoteRelay

    eng = Engine.__new__(Engine)               # 只测这一个方法，不构造整台引擎
    seen: list[tuple[str, str]] = []
    eng._events = EngineEvents(on_status=lambda lvl, msg: seen.append((lvl, msg)))
    eng._note_relay = DegradedNoteRelay()
    eng._overlay = types.SimpleNamespace(degraded_note="手腕屏暂时退回兼容刷新方式（测试）")
    eng._relay_overlay_note()
    eng._relay_overlay_note()                  # 同一说明不重复推
    assert seen == [("warn", "手腕屏暂时退回兼容刷新方式（测试）")], f"应在变化时推一次：{seen}"

    # 恢复：必须再推一条 info 把那条警告**顶掉**（状态栏只显示最新一条，没人再推就等于
    # 「降级说明永远挂在那里」—— 判据 1 的「恢复后说明消失」靠它，见 issue #99）
    eng._overlay.degraded_note = None
    eng._relay_overlay_note()
    assert seen == [("warn", "手腕屏暂时退回兼容刷新方式（测试）"),
                    ("info", DEGRADED_NOTE_RECOVERED)], f"恢复要改写状态栏：{seen}"
    eng._relay_overlay_note()                  # 已经是「正常」了，不再重复推
    assert len(seen) == 2, f"恢复不该反复打扰用户：{seen}"

    # 没有 degraded_note 属性的后端（Linux / 假对象）必须静默跳过
    eng._overlay = object()
    eng._note_relay = DegradedNoteRelay()
    eng._relay_overlay_note()
    assert len(seen) == 2, "可选属性缺失时不该抛异常/不该推空消息"

    # 接线守卫：`_relay_overlay_note()` 必须真的挂在每帧的 `_pump_loop` 里（别只定义不调用）
    src = Path(ROOT / "vlt" / "engine.py").read_text(encoding="utf-8")
    # `_pump_loop` 是 async def（AsyncFunctionDef），别只挑 FunctionDef（踩过：KeyError）
    fns = {n.name: n for n in ast.walk(ast.parse(src))
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    pump = ast.unparse(fns["_pump_loop"])
    assert "_relay_overlay_note" in pump and "tick()" in pump, (
        f"_pump_loop 里没有转发降级说明（等于用户永远看不到）：{pump}")
    print("  降级说明经 on_status 推一次（恢复时改写）+ 接线在 _pump_loop OK")


def test_close_blocks_tex_retry_resurrection() -> None:
    """D3D 定点重试必须和 `_maybe_reinit` / `_maybe_apply_anchor` 同一套纪律：`close()` 过的不复活。

    场景（复审 #99 第 2 条）：D3D 路失败 → `_schedule_tex_retry()` 已排定 → 用户取消勾选
    `close()` → 下一次 `tick()`。旧写法会**真的新建一个 D3D11 设备/纹理**（真机泄漏到进程退出）
    再拿 `handle=None` 调 `setOverlayTexture`。

    两条护栏各验一次（缺一条就有一半路径能复活）：
      * 护栏一：`close()` 把已排定的时间点直接作废 —— 故障注入 B：删掉 `close()` 里的
        `self._tex_retry_at = None` → 第一条断言红；
      * 护栏二：`_maybe_retry_tex_path()` 的 `_closed` 守卫 —— 故障注入 A：把它删掉 →
        下面「时间点还在也必须拦住」那一段红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    # 模拟「D3D 路不可用 → 已排定 2s 后定点重试」
    ov._tex_retried, ov._tex_retry_at, ov.available = False, 0.0, False
    ov.close()

    # 护栏一（物理上不可能）：close() 把已排定的时间点直接作废
    assert ov._tex_retry_at is None, "close() 应把已排定的 D3D 定点重试清掉（护栏要物理上不可能）"

    # 护栏二（`_closed`）：就算时间点还在，已 close() 的实例也不许再建 D3D 设备
    ov._tex_retry_at = 0.0                     # 故障注入 A 靠这一段抓（护栏一被删时时间点正是这个）
    before = len(FakeD3D.instances)
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()
    names = _names()
    assert "setOverlayTexture" not in names, f"close() 之后 tick 又提交了贴图：{names}"
    assert len(FakeD3D.instances) == before, (
        f"close() 之后 tick 又建了 D3D 设备（真机上是泄漏）：{len(FakeD3D.instances)}")
    assert "再试一次启用 D3D11 持久贴图路径" not in out, f"close() 之后还在做定点重试：{out}"
    assert "shutdown" not in names, f"close() 之后不该再走接管流程：{names}"
    print("  close() 之后不做 D3D 定点重试（两条护栏各验一次）OK")


def test_start_resets_session_heal_state() -> None:
    """`start()` = 新的一轮会话：上一轮的自愈档位/一次性名额必须归零（复审 #99 第 3 条）。

    旧写法的实测后果（本机复现）：
      * 退避从上一轮的档位接着走 —— 下一次「面板不可用」排定 **40s** 而不是 5s；
      * 「整个会话只给一次」的 D3D 定点重试名额已被上一轮用掉（`_schedule_tex_retry` 成 no-op）；
      * `_upload_fails` 攒着 → 新会话第一次失败就凑够 3 次、立刻升级成重建/硬重启。

    故障注入：把 `start()` 里那段归零删掉 → 本条红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov._reinit_fails = 3                       # 上一轮的退避档位
    ov._tex_retried = True                     # 上一轮已用掉定点重试名额
    ov._anchor_tries = 9
    ov._upload_fails = 2
    ov._fails_in_stage = 2
    ov._recover_stage = "rebuild"
    ov._tex_retry_at = time.monotonic() + 5.0
    ov.close()
    assert ov.start() is True                  # 重新勾选 = 新的一轮
    assert (ov._reinit_fails, ov._tex_retried, ov._anchor_tries,
            ov._upload_fails, ov._fails_in_stage) == (0, False, 0, 0, 0), (
        f"start() 没把会话级自愈状态归零：{ov._reinit_fails}/{ov._tex_retried}/"
        f"{ov._anchor_tries}/{ov._upload_fails}/{ov._fails_in_stage}")
    assert ov._recover_stage == "none", f"阶段没归零：{ov._recover_stage}"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov._schedule_reinit(oo.OVERLAY_ERR_OTHER)
    out = buf.getvalue()
    delay = ov._reinit_at - time.monotonic()
    assert abs(delay - oo.REINIT_BACKOFF_S) < 0.6, (
        f"新会话的退避应从 {oo.REINIT_BACKOFF_S:.0f}s 起步，实测 {delay:.0f}s：{out}")
    assert "（第 1 次尝试）" in out, f"新会话应从第 1 次算起：{out}"

    ov._tex_retry_at = None
    ov._schedule_tex_retry()
    assert ov._tex_retry_at is not None, "D3D 定点重试名额应随新会话重新可用（旧写法是 no-op）"
    ov.close()
    print("  start() 归零会话级自愈状态 OK（退避 5s 起步 + 定点名额可用）")


if __name__ == "__main__":
    print("test_overlay_recovery:")
    test_unavailable_selfheals_and_repushes()
    test_reinit_backoff_is_bounded()
    test_heartbeat_logs_even_when_unavailable()
    test_close_blocks_reinit_resurrection()
    test_heartbeat_skips_closed_instance()
    test_raw_path_rebuilds_before_the_200_wall()
    test_degraded_note_is_loud_and_clears_on_recovery()
    test_engine_relays_degraded_note_once()
    test_close_blocks_tex_retry_resurrection()
    test_start_resets_session_heal_state()
    print("ALL PASSED")
