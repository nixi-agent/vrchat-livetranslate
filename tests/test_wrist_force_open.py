"""非翻译模式下勾选「手腕屏」：**必须真的尝试重建** + 每条失败/静默路径都要留痕。

## 为什么要有这个测试（用户 v0.11.1 真机报告）

「本来我是可以在非翻译模式下强制打开手腕屏的，现在不行了」= 不点「开始翻译」、
只勾主界面「手腕屏」勾选框时面板起不来。

这条路的代码与 v0.10.0 **逐字等价**（`gui._on_overlay_toggle` → `_start_overlay(force=True)`
→ `gui_sinks.start_overlay(ctx, force=True)`，已 diff 核对），所以能「静默失效」的只有状态类病因：

  ① `ctx.overlay_out` 里留着一个**已经死掉**的实例（`available=False`，例如硬重启 openvr 失败过，
     或被 `close()` 过）→ 旧写法开头 `if ctx.overlay_out is not None: return True` 直接返回真
     → 勾选框「点了什么都不做」，日志里也一行没有 = 用户看到的「勾了打不开、没有任何提示」。
  ② `start()` 失败 → 勾选被自动退回，但状态栏只写「先把 SteamVR 打开」；真因
     （锚点手柄没被追踪 / 没头显 / SteamVR 没跑）被掩盖，用户照那句话重试一辈子也没用。
  ③ `start()` 里 `createOverlay()` **已经成功**、后面某步抛异常时（实测：锚点设备无效 →
     `OverlayError_InvalidTrackedDevice`）旧写法从不 `destroyOverlay` → overlay 对象留在 SteamVR 里
     → 下次勾选撞 `OverlayError_KeyInUse`（真机日志里那串「key 已被占用 → 清理残留」就是它）。
  ④ 硬重启失败后 `available=False`，而上传/心跳/热重载全部 `if self.available` 早退
     → 面板再也救不回来（勾选框仍显示勾着，用户只能重启程序）。

用例全部用假对象（假 openvr / 假工厂），**不碰真 SteamVR，也不碰真 D3D**。
"""
from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _cfgbox import sandbox_config  # noqa: E402

sandbox_config(reset=True)

# ⚠️ 界面语言必须在**构造 TranslationGUI 之前**钉死（本仓库的铁律）：状态栏文案走 `t()`，
#    本机中文系统全绿、GitHub CI（英文系统）会一片红 —— 实测踩过（PR #98 的 CI 就红在这一条：
#    断言里写「已自动取消勾选」，而 CI 拿到的是 "The wrist overlay could not start, so the
#    tick was reverted: …"）。口径与 test_api_key_gui.py / test_config_save.py 完全一致。
import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

import test_overlay_steamvr as fakeov  # noqa: E402  复用假 openvr 与调用记录
from vlt import platform  # noqa: E402
from vlt.output.openvr_overlay import WristOverlay as _RealWristOverlay  # noqa: E402

# 与 test_overlay_conversation 同口径：平台工厂返回**真** WristOverlay，由假 openvr 驱动。
# （Linux 上平台工厂返回 OpenXrOverlay，会真去连运行时）
platform.create_wrist_overlay = (  # type: ignore[assignment]
    lambda cfg, config_path=None, dry_run=False:
        _RealWristOverlay(cfg, config_path=config_path, dry_run=dry_run))


# ------------------------------------------------------------------ 假「活」手腕屏
class FakeLiveOverlay:
    """假实例：start() 成功、可 close、可推内容。用来验证「重建后拿到的确实是新实例」。"""

    instances: list["FakeLiveOverlay"] = []

    def __init__(self, cfg=None, config_path=None, dry_run=False) -> None:  # noqa: ANN001
        self.available = True
        self.alive = True
        self.closed = False
        self.frames = 0
        self.entries: list = []
        FakeLiveOverlay.instances.append(self)

    def start(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True
        self.alive = False

    def update_entries(self, entries, force: bool = False) -> None:  # noqa: ANN001
        self.frames += 1
        self.entries = list(entries or [])


class FakeDeadOverlay:
    """假「已死」实例：`available=False`（= 硬重启 openvr 失败过 / 已被 close）。

    ⚠️ 刻意**不**提供 `start()`：旧写法下它会被直接 `return True` 返回给界面，
    界面于是以为屏已经起来了 —— 这正是「勾了没反应」的机制。
    """

    def __init__(self) -> None:
        self.available = False
        self.alive = False
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_ctx(overlay_out=None):  # noqa: ANN001
    from vlt.config import load_config
    from vlt.gui_engine_context import EngineCtx

    ctx = EngineCtx()
    ctx.cfg = load_config(require_key=False)
    ctx.bubbles = []
    ctx.sinks = set()
    ctx.overlay_out = overlay_out
    return ctx


# ------------------------------------------------------------------ ① 陈旧实例必须重建
def test_dead_instance_is_rebuilt_instead_of_silently_returning_true() -> None:
    """`available=False` 的陈旧实例 → 勾选（force=True）必须**重建**，而不是直接 return True。

    判据是行为，不是「碰巧出现了新对象」：旧实例必须被 close、必须**真的调了工厂**、
    并且有一行留痕（禁静默 —— 用户看不到任何日志就是本次回归最难查的地方）。
    """
    from vlt import gui_sinks

    FakeLiveOverlay.instances.clear()
    dead = FakeDeadOverlay()
    ctx = _make_ctx(overlay_out=dead)

    real = platform.create_wrist_overlay
    platform.create_wrist_overlay = (  # type: ignore[assignment]
        lambda cfg, config_path=None, dry_run=False: FakeLiveOverlay())
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ok = gui_sinks.start_overlay(ctx, force=True)
    finally:
        platform.create_wrist_overlay = real
    out = buf.getvalue()

    assert ok is True, f"陈旧实例应被重建并返回 True：\n{out}"
    assert dead.closed is True, "陈旧（已死）实例没有被关掉"
    assert len(FakeLiveOverlay.instances) == 1, (
        f"没有重建实例（工厂调用 {len(FakeLiveOverlay.instances)} 次）—— 旧写法会直接 return True")
    assert ctx.overlay_out is not dead and ctx.overlay_out is FakeLiveOverlay.instances[0], \
        "ctx.overlay_out 没有换成新实例"
    assert "失效" in out or "重建" in out, f"重建这条路没有任何留痕（用户看到的就是「点了没反应」）：\n{out}"
    print("  陈旧实例 → 关掉并重建 + 留痕 OK")


def test_live_instance_is_reused_not_rebuilt() -> None:
    """反例（守住既有的 KeyInUse 修复）：实例还活着时必须**复用**，不许重复建。"""
    from vlt import gui_sinks

    live = FakeLiveOverlay()
    FakeLiveOverlay.instances.clear()      # 只数「工厂新建出来的」实例（上面的 live 是手工造的）
    ctx = _make_ctx(overlay_out=live)
    assert gui_sinks.start_overlay(ctx, force=True) is True
    assert len(FakeLiveOverlay.instances) == 0, "活实例被重复创建（会撞 OverlayError_KeyInUse）"
    assert ctx.overlay_out is live
    print("  活实例复用、不重建 OK")


# ------------------------------------------------------------------ ②③ 失败要留真因 + 不残留
def _install_fake_openvr_with_bad_anchor(err: str = "OverlayError_InvalidTrackedDevice: ") -> None:
    """假 openvr：应用变换时抛错 —— 复刻真机 `OverlayError_InvalidTrackedDevice`。

    真机形态（本机真 SteamVR + null 驱动、无控制器，用户配置 `anchor: right_hand`）：
        [overlay] ⚠️ 锚点设备（right_hand）还没被 SteamVR 追踪：面板先留着，每 1s 自动重试
    而 `createOverlay()` 在那之前**已经成功**了。

    `err` 换一个非锚点类错误（如 `OverlayError_RequestFailed`）时用来验「其它错误照旧判失败」。
    """
    def _factory():  # noqa: ANN202
        ov = fakeov._FakeIVROverlay()

        def _boom(handle, device, mat):  # noqa: ANN001, ANN202
            raise RuntimeError(err)

        ov.setOverlayTransformTrackedDeviceRelative = _boom
        return ov

    fakeov._install_fake_openvr(overlay_factory=_factory)


def test_anchor_not_tracked_keeps_panel_for_retry() -> None:
    """锚点设备没被追踪 → **留住面板**（不许判失败、不许销毁 handle），等 tick 重试贴上去。

    这是用户报的「非翻译模式下勾选手腕屏打不开」的正面修复：旧写法让 start() 返回 False →
    界面把勾选退回 + 一句「先把 SteamVR 打开」，而用户照着重试永远是白费力气（面板其实
    建得出来，差的只是「贴到哪台设备上」）。见 `openvr_overlay._maybe_apply_anchor`。
    """
    from vlt.output.overlay import OverlayConfig

    fakeov.CALLS.clear()
    _install_fake_openvr_with_bad_anchor()
    ov = _RealWristOverlay(OverlayConfig())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = ov.start()
    out = buf.getvalue()

    assert ok is True, "锚点设备没被追踪**不该**让 start() 判失败（用户报的就是这条）"
    assert ov.pending_anchor is True, "应进入「等锚点」状态（由 tick 重试）"
    assert ov.available is True and ov.alive is True, "面板必须仍然活着"
    assert "createOverlay" in [c[0] for c in fakeov.CALLS], "前置条件：createOverlay 应已成功"
    assert "destroyOverlay" not in [c[0] for c in fakeov.CALLS], (
        f"不许销毁刚建好的面板（会留成 SteamVR 里的残留 → 下次撞 KeyInUse）：\n{out}")
    assert "追踪" in out and "重试" in out, f"日志里没写清「面板留着 + 会自动重试」：\n{out}"
    print("  锚点没被追踪 → 面板留着 + 自动重试（不退回勾选）OK")


def test_other_start_failure_destroys_overlay_and_names_the_real_reason() -> None:
    """非锚点类失败（贴图 / 句柄…）：start() 判失败 → 必须销毁 overlay + 报出真因码。"""
    from vlt.output.overlay import OVERLAY_ERR_OTHER, OverlayConfig

    fakeov.CALLS.clear()
    _install_fake_openvr_with_bad_anchor("OverlayError_RequestFailed: ")
    ov = _RealWristOverlay(OverlayConfig())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = ov.start()
    out = buf.getvalue()

    assert ok is False, "非锚点类错误必须照旧判失败（别被锚点容错顺手放过）"
    assert "createOverlay" in [c[0] for c in fakeov.CALLS], "前置条件：createOverlay 应已成功"
    assert "destroyOverlay" in [c[0] for c in fakeov.CALLS], (
        f"start() 失败后没有销毁已创建的 overlay（会留成 SteamVR 里的残留 → 下次撞 KeyInUse）：\n{out}")
    assert ov.last_error == OVERLAY_ERR_OTHER, (
        f"没把真因报成「其它（非锚点）」：{ov.last_error!r}\n{out}")
    print("  非锚点类失败 → 销毁残留 overlay + 报出真因码 OK")


def test_toggle_failure_shows_real_reason_in_status_bar() -> None:
    """勾选被自动退回时，状态栏必须给出**真因**（旧文案只会说「先把 SteamVR 打开」）。"""
    import tkinter as tk

    from vlt import platform as plat
    from vlt.gui import TranslationGUI
    from vlt.output.overlay import OVERLAY_ERR_ANCHOR, OVERLAY_ERR_RUNTIME

    class _FailOv:
        def __init__(self, reason: str) -> None:
            self.available = False
            self.alive = False
            self.last_error = reason

        def start(self) -> bool:
            return False

        def close(self) -> None:
            pass

    msgs: list[tuple[str, str]] = []
    got: dict = {}
    real = plat.create_wrist_overlay
    gui = TranslationGUI(headless=True)
    gui._root = tk.Tk()
    gui._root.withdraw()
    gui._save_ui_state = lambda: None          # 不写盘（沙箱也只是保险）
    gui._set_status = lambda lvl, msg: msgs.append((lvl, msg))
    gui._overlay_var = tk.BooleanVar(value=True)
    try:
        for code in (OVERLAY_ERR_ANCHOR, OVERLAY_ERR_RUNTIME):
            msgs.clear()
            plat.create_wrist_overlay = (  # type: ignore[assignment]
                lambda *a, _c=code, **kw: _FailOv(_c))
            gui._overlay_var.set(True)
            gui._on_overlay_toggle()           # 只勾选，不开始翻译
            got[code] = (gui._overlay_var.get(), list(msgs), gui._overlay_out)
    finally:
        plat.create_wrist_overlay = real
        try:
            gui._root.destroy()
        except Exception:  # noqa: BLE001
            pass

    for code, (var, msgs_c, out) in got.items():
        assert var is False, f"[{code}] 启动失败后勾选没有自动退回"
        assert out is None, f"[{code}] 失败后不该留下 overlay 引用"
        assert msgs_c and msgs_c[-1][0] == "error", f"[{code}] 没有给用户可见提示：{msgs_c}"
        text = msgs_c[-1][1]
        assert "已自动取消勾选" in text, f"[{code}] 提示里没写清勾选已退回：{text}"
        assert "SteamVR 打开" not in text.replace("先把它打开", ""), (
            f"[{code}] 仍然在无差别地说「先把 SteamVR 打开」（真因被掩盖）：{text}")
    assert "锚点" in got[OVERLAY_ERR_ANCHOR][1][-1][1], (
        f"锚点设备无效这条没给出真因：{got[OVERLAY_ERR_ANCHOR][1]}")
    assert "SteamVR" in got[OVERLAY_ERR_RUNTIME][1][-1][1], (
        f"运行时没在跑这条没说清是 SteamVR：{got[OVERLAY_ERR_RUNTIME][1]}")
    print(f"  状态栏真因 OK：{got[OVERLAY_ERR_ANCHOR][1][-1][1]} / {got[OVERLAY_ERR_RUNTIME][1][-1][1]}")


# ------------------------------------------------------------------ ④ 不再有「救不回来」死角
def test_not_available_instance_self_heals_on_tick() -> None:
    """`available=False`（硬重启也失败过）→ tick() 应定期重试接管，而不是永久静默。

    实测过的断链：上传 / 心跳 / 热重载全部 `if self.available` 早退，而 `available=False`
    只有勾选框那条路能救（还得用户自己发现）→ 面板再也回不来。
    这里按真实顺序构造：面板好好起着 → SteamVR 没了 → 硬重启失败（available=False）→
    SteamVR 回来 → tick() 自己把接管救回来。
    """
    from vlt.output.overlay import OverlayConfig

    def _factory_create_boom():  # noqa: ANN202
        """SteamVR「没了」：createOverlay 失败（硬重启就会卡在这一步）。"""
        fake = fakeov._FakeIVROverlay()

        def _boom(key, name):  # noqa: ANN001, ANN202
            raise RuntimeError("OverlayError_RequestFailed: 模拟 SteamVR 已退出")

        fake.createOverlay = _boom
        return fake

    fakeov.CALLS.clear()
    fakeov.OVERLAY_GONE["gone"] = False
    fakeov._install_fake_openvr()
    ov = _RealWristOverlay(OverlayConfig())
    assert ov.start() is True

    fakeov._install_fake_openvr(overlay_factory=_factory_create_boom)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert ov._hard_reinit() is False, "前置条件：SteamVR 没了时硬重启应失败"
    assert ov.available is False and ov.alive is False, "硬重启失败后应处于不可用状态"
    assert ov._reinit_at is not None, \
        "不可用之后必须排下一次重试（否则 tick 永远不会重试）；退避机制见 openvr_overlay._schedule_reinit"

    fakeov._install_fake_openvr()           # SteamVR 回来了
    fakeov.CALLS.clear()
    ov._reinit_at = time.monotonic() - 1.0  # 推到重试点（测试里不等退避窗口）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()

    assert ov.available is True, f"tick() 没有把不可用状态救回来（永久死角）：\n{out}"
    assert ov.alive is True, "自愈后 alive 仍为假"
    assert "shutdown" in [c[0] for c in fakeov.CALLS], "自愈应重走 openvr 接管（shutdown+init）"
    assert "重试" in out or "接管" in out, f"自愈没有留痕：\n{out}"

    # 已经 close（用户取消勾选）的实例**绝不能**自愈 —— 否则勾选框显示关掉、屏却自己回来了。
    ov.close()
    fakeov.CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    assert ov.available is False and ov.alive is False, "close() 之后的实例被自己救活了（取消勾选会失效）"
    assert "shutdown" not in [c[0] for c in fakeov.CALLS], "close() 之后的实例仍在自愈"
    print("  available=False → tick 自愈 OK（close 后不自愈）")


# ------------------------------------------------------------------ ⑤ 原因码要能落到能行动的那一档
def test_reason_codes_cover_the_real_failure_shapes() -> None:
    """错误类名 → 原因码：实测遇到的每一种都要落到**能行动**的那一档（别全掉进 other）。

    这些类名都是真机/本机实测见过的（见各行的出处）。
    """
    from vlt import gui_sinks
    from vlt.output import overlay as ov
    from vlt.output.openvr_overlay import _reason_code

    cases = {
        # SteamVR 没跑（用户最常见）
        "InitError_Init_NoServerForBackgroundApp": ov.OVERLAY_ERR_RUNTIME,
        # 本机实测：null 驱动下 SteamVR 在跑、vrcompositor 起不来 →
        # [overlay] ⚠️ 创建 overlay 失败…：InitError_IPC_CompositorConnectFailed:
        "InitError_IPC_CompositorConnectFailed": ov.OVERLAY_ERR_COMPOSITOR,
        "InitError_Init_HmdNotFoundPresenceFailed": ov.OVERLAY_ERR_HMD,
        # 本机实测（无控制器 + anchor=right_hand）→ OverlayError_InvalidTrackedDevice
        "OverlayError_InvalidTrackedDevice": ov.OVERLAY_ERR_ANCHOR,
        "OverlayError_RequestFailed": ov.OVERLAY_ERR_OTHER,
    }
    for err, want in cases.items():
        got = _reason_code(err)
        assert got == want, f"{err} → {got}（期望 {want}）"

    codes = [ov.OVERLAY_ERR_RUNTIME, ov.OVERLAY_ERR_COMPOSITOR, ov.OVERLAY_ERR_HMD,
             ov.OVERLAY_ERR_ANCHOR, ov.OVERLAY_ERR_OTHER]
    texts = {c: gui_sinks.overlay_error_text(c) for c in codes}
    assert all(t.strip() for t in texts.values()), texts
    assert len(set(texts.values())) == len(codes), f"有重复文案（真因分不开）：{texts}"
    assert gui_sinks.overlay_error_text("没见过的码") == texts[ov.OVERLAY_ERR_OTHER], \
        "未知码必须回落到 other 那条文案"
    print(f"  原因码归类 + {len(codes)} 档文案互不相同 OK")


if __name__ == "__main__":
    print("test_wrist_force_open:")
    test_dead_instance_is_rebuilt_instead_of_silently_returning_true()
    test_live_instance_is_reused_not_rebuilt()
    test_anchor_not_tracked_keeps_panel_for_retry()
    test_other_start_failure_destroys_overlay_and_names_the_real_reason()
    test_toggle_failure_shows_real_reason_in_status_bar()
    test_not_available_instance_self_heals_on_tick()
    test_reason_codes_cover_the_real_failure_shapes()
    print("ALL PASSED")
