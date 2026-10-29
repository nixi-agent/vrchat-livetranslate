"""「开始/停止」单按钮开关的验收（两个按钮合并成一个 + 文案/颜色/可用态同源）。

跑法：.venv/Scripts/python.exe tests/test_power_toggle.py

## 钉住的四件事

1. `gui_layout.apply_power_state`：idle / running / stopping 三态的 (text, style, state)
   逐项正确；传 None 不抛（headless 下没有控件）；未知值当 idle（防御：绝不抛）。
2. 结构守卫（源码级，参照 test_level_probe.py 用源码文案断言的做法）：
   `build_controls` 里必须建 `_power_btn`、**必须不再有** `_stop_btn`，
   且 `t("开始翻译")` 在该函数里只出现一次（= 界面上只剩一个开始/停止按钮）。
3. 点击分发 `_on_power`：idle → 调 `_start`；running → 调 `_stop`；stopping → 两个都不调。
4. 引擎接线：`gui_engine.start()` → 记录到 "running"；`gui_engine.stop()` → "stopping"；
   投递 "stop_done" 事件跑一次 `poll()` → "idle"。三者都经 `ctx.power_state_fn` 这一个回调流出。

## 离线保证

全程 headless（不建 Tk 窗口）+ 占位控件，引擎换成 `_FakeEngine`，
**绝不碰真实音频设备 / 绝不真连 WebSocket**。构造 `TranslationGUI` 之前钉死界面语言
（CI 是英文系统，本项目踩过）。
"""
from __future__ import annotations

import contextlib
import inspect
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 构造 TranslationGUI 之前钉死界面语言：CI 是英文系统，不钉就会按 en 取词条。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

import vlt.gui_engine as gui_engine  # noqa: E402
import vlt.gui_layout as gui_layout  # noqa: E402
from vlt.i18n import t  # noqa: E402


# ---------------------------------------------------------------- 占位替身（复制自 test_proxy_wiring，不 import 别的测试文件）


class _Widget:
    """占位控件：只记 configure 的关键字（headless 下没有真 Tk 控件）。"""

    def __init__(self) -> None:
        self.kw: dict = {}

    def configure(self, **kw) -> None:  # noqa: ANN003
        self.kw.update(kw)

    def cget(self, key: str):
        return self.kw.get(key)


class _Var:
    """占位 Tk 变量（BooleanVar / StringVar 都只需要 get/set）。"""

    def __init__(self, value) -> None:               # noqa: ANN001
        self._v = value

    def get(self):
        return self._v

    def set(self, value) -> None:                    # noqa: ANN001
        self._v = value


class _Root:
    """占位 Tk root：只提供 after/after_cancel。"""

    def after(self, *_a, **_k) -> str:
        return "job"

    def after_cancel(self, *_a, **_k) -> None:
        return None


class _FakeEngine:
    """引擎替身：只记录构造参数与启停调用，绝不真连。"""

    instances: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.running = False
        self.start_calls = 0
        self.stop_requests = 0
        _FakeEngine.instances.append(self)

    def start(self) -> None:
        self.start_calls += 1
        self.running = True

    def request_stop(self) -> None:
        self.stop_requests += 1
        self.running = False

    def wait_stopped(self, timeout: float = 5.0) -> bool:   # noqa: ARG002
        return True


@contextlib.contextmanager
def _fake_engine_class():
    """把 `gui_engine.Engine` 换成替身（`EngineEvents` 保持真的：它只是个数据类）。"""
    saved = gui_engine.Engine
    _FakeEngine.instances.clear()
    gui_engine.Engine = _FakeEngine                  # type: ignore[assignment]
    try:
        yield _FakeEngine.instances
    finally:
        gui_engine.Engine = saved                    # type: ignore[assignment]


def _make_gui():                                     # noqa: ANN202
    """起一个 headless GUI，钉上占位 root 与一个假 API key（start() 要检查它）。"""
    from vlt.gui import TranslationGUI

    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        gui = TranslationGUI(headless=True)
    gui._root = _Root()                              # type: ignore[assignment]
    gui._power_btn = _Widget()                       # type: ignore[assignment]
    gui._cfg.session_base["api_key"] = "test-key-not-real"
    _i18n.set_language("zh")
    return gui


def _arm_engine_ctx(gui):                            # noqa: ANN001, ANN202
    """把 headless GUI 的 EngineCtx 武装到「能跑 start()/stop()」的最小可用状态。"""
    gui._sync_engine_ctx()
    ctx = gui._engine_ctx
    ctx.direction_var = _Var("mine")
    ctx.chatbox_var = _Var(True)
    ctx.overlay_var = _Var(False)                    # 手腕屏/桌面字幕都不勾 → 两条腿直接短路
    ctx.desktop_var = _Var(False)
    ctx.vmic_var = _Var(True)
    ctx.lang_pair = {"source": "zh", "target": "en"}
    ctx.refresh_api_key_fn = None                    # 别让用例去碰真实 key 解析
    ctx.start_room_fn = None
    ctx.refresh_room_status_fn = None
    ctx.sync_gate_probe_fn = None
    ctx.set_text_input_enabled_fn = None
    ctx.specs = [("mine", "mine", "mic", "zh", "en")]
    ctx.sinks = {"chatbox"}
    ctx.pending_starts = 1
    return ctx


# ---------------------------------------------------------------- 1) apply_power_state 三态矩阵


def test_apply_power_state_matrix() -> None:
    """idle / running / stopping 三态的 (text, style, state) 逐项正确。"""
    # idle：蓝底「开始翻译」，可点
    w = _Widget()
    gui_layout.apply_power_state(w, "idle")
    assert w.kw.get("text") == t("开始翻译"), f"idle 文案不对：{w.kw!r}"
    assert w.kw.get("style") == "Power.TButton", f"idle 应是蓝底 Power：{w.kw!r}"
    assert w.kw.get("state") == "normal", f"idle 应可点：{w.kw!r}"

    # running：红底「停止翻译」，可点
    w = _Widget()
    gui_layout.apply_power_state(w, "running")
    assert w.kw.get("text") == t("停止翻译"), f"running 文案不对：{w.kw!r}"
    assert w.kw.get("style") == "PowerDanger.TButton", f"running 应是红底 PowerDanger：{w.kw!r}"
    assert w.kw.get("state") == "normal", f"running 应可点：{w.kw!r}"

    # stopping：红底「停止翻译」，但置灰
    w = _Widget()
    gui_layout.apply_power_state(w, "stopping")
    assert w.kw.get("text") == t("停止翻译"), f"stopping 文案不对：{w.kw!r}"
    assert w.kw.get("style") == "PowerDanger.TButton", f"stopping 应是红底 PowerDanger：{w.kw!r}"
    assert w.kw.get("state") == "disabled", f"stopping 应置灰：{w.kw!r}"
    print("  ✓ apply_power_state 三态矩阵：idle/running/stopping 的 text+style+state 全对")


def test_apply_power_state_none_and_garbage() -> None:
    """传 None 不抛（headless 无控件）；未知值当 idle（防御：绝不抛）。"""
    gui_layout.apply_power_state(None, "running")     # 不抛即通过
    gui_layout.apply_power_state(None, "idle")        # 同上

    w = _Widget()
    gui_layout.apply_power_state(w, "garbage")        # 未知值 → 当 idle
    assert w.kw.get("text") == t("开始翻译"), f"未知值应回落到 idle 文案：{w.kw!r}"
    assert w.kw.get("style") == "Power.TButton", f"未知值应回落到 idle 蓝底：{w.kw!r}"
    assert w.kw.get("state") == "normal", f"未知值应回落到 idle 可点：{w.kw!r}"
    print("  ✓ apply_power_state 防御：None 不抛、未知值当 idle")


# ---------------------------------------------------------------- 2) 结构守卫（源码级）


def test_build_controls_has_single_power_button() -> None:
    """`build_controls` 里只建一个 `_power_btn`、不再有 `_stop_btn`，且开始文案只出现一次。"""
    src = inspect.getsource(gui_layout.build_controls)
    assert "_power_btn" in src, "build_controls 里必须建 _power_btn（单按钮开关）"
    assert '"Power.TButton"' in src, (
        "初始（未翻译）那只按钮必须用 Power.TButton —— 用 Primary 的话点一下就会换尺寸")
    assert "_stop_btn" not in src, (
        "build_controls 里不许再出现 _stop_btn —— 两个按钮已合并成一个，"
        "留旧控件就是没删干净")
    n_start = src.count('t("开始翻译")')
    assert n_start == 1, (
        f'build_controls 里 t("开始翻译") 只该出现一次（= 只剩一个开始/停止按钮），'
        f"实际 {n_start} 次")
    print("  ✓ 结构守卫：build_controls 只有一个 _power_btn、无 _stop_btn、开始文案仅 1 次")


def test_power_styles_are_same_size() -> None:
    """两个档位样式必须**同尺寸**（字体 + 内边距同一份常量）。

    用户实测反馈：合并成单按钮后「按一下变大、按一下变小」—— 根因是蓝底用了
    `Primary.TButton`（粗体 + padding (20,9)）、红底用了 `Danger.TButton`（常规字体 +
    padding (14,6)）。这里做**源码级**守卫（真窗口里的实测尺寸一致性由
    `out/test_power_probe.py` 验，那才是最终判据）。
    """
    import vlt.ui_tk as ui_tk

    src = inspect.getsource(ui_tk.apply_theme)
    assert '"Power.TButton"' in src and '"PowerDanger.TButton"' in src, \
        "两个档位样式必须都在 apply_theme 里定义"
    n_font = src.count("font=_POWER_FONT")
    assert n_font == 2, (
        "两个 Power 样式的字体必须取自同一份常量 _POWER_FONT，"
        f"否则换档时按钮尺寸会变（实际 {n_font} 处）")
    n_pad = src.count("padding=_POWER_PAD")
    assert n_pad == 2, (
        "两个 Power 样式的内边距必须取自同一份常量 _POWER_PAD，"
        f"否则换档时按钮尺寸会变（实际 {n_pad} 处）")
    ap = inspect.getsource(gui_layout.apply_power_state)
    assert '"Primary.TButton"' not in ap and '"Danger.TButton"' not in ap, (
        "apply_power_state 不许在尺寸不同的 Primary/Danger 之间切换（会按一下变大变小）")
    print("  ✓ 两个档位样式同字体同内边距（单按钮尺寸恒定，不许退回 Primary/Danger）")


# ---------------------------------------------------------------- 3) 点击分发


def test_on_power_dispatch() -> None:
    """_on_power：idle→start、running→stop、stopping→两个都不调。"""
    gui = _make_gui()
    calls: list[str] = []

    def _fake_start() -> None:
        calls.append("start")

    def _fake_stop() -> None:
        calls.append("stop")

    gui._start = _fake_start                         # type: ignore[method-assign]
    gui._stop = _fake_stop                           # type: ignore[method-assign]

    gui._power_state = "idle"
    calls.clear()
    gui._on_power()
    assert calls == ["start"], f"idle 时点按钮应调 _start：{calls!r}"

    gui._power_state = "running"
    calls.clear()
    gui._on_power()
    assert calls == ["stop"], f"running 时点按钮应调 _stop：{calls!r}"

    gui._power_state = "stopping"
    calls.clear()
    gui._on_power()
    assert calls == [], f"stopping 时按钮已置灰，两个都不该调：{calls!r}"
    print("  ✓ _on_power 分发：idle→start、running→stop、stopping→都不调")


def test_set_power_state_refreshes_widget() -> None:
    """_set_power_state 既记下状态，又把 _power_btn 刷成对应外观（唯一入口）。"""
    gui = _make_gui()
    gui._set_power_state("running")
    assert gui._power_state == "running", f"状态没记下：{gui._power_state!r}"
    assert gui._power_btn.kw.get("text") == t("停止翻译"), f"按钮文案没刷：{gui._power_btn.kw!r}"
    assert gui._power_btn.kw.get("style") == "PowerDanger.TButton", f"按钮样式没刷：{gui._power_btn.kw!r}"
    gui._set_power_state("idle")
    assert gui._power_state == "idle"
    assert gui._power_btn.kw.get("text") == t("开始翻译"), f"按钮文案没刷回：{gui._power_btn.kw!r}"
    assert gui._power_btn.kw.get("style") == "Power.TButton", f"按钮样式没刷回：{gui._power_btn.kw!r}"
    print("  ✓ _set_power_state：记状态 + 刷按钮（文案/颜色同源）")


# ---------------------------------------------------------------- 4) 引擎接线


def test_engine_wiring_running_stopping_idle() -> None:
    """start()→"running"、stop()→"stopping"、stop_done 经 poll()→"idle"，全走 power_state_fn。"""
    gui = _make_gui()
    recorded: list[str] = []

    def _recorder(state: str) -> None:
        recorded.append(state)

    # 把唯一刷新入口顶成记录器：引擎 ctx 与 chat ctx 都经它流出状态。
    gui._set_power_state = _recorder                 # type: ignore[method-assign]
    ctx = _arm_engine_ctx(gui)
    ctx.power_state_fn = _recorder                   # 引擎路径直接用记录器

    with contextlib.redirect_stdout(io.StringIO()):  # 吞掉 [gui] 启动/收尾留痕（本用例只看状态流）
        with _fake_engine_class():
            gui_engine.start(ctx)
        assert "running" in recorded, f"start() 后应记录到 running：{recorded!r}"

        recorded.clear()
        gui_engine.stop(ctx)
        assert "stopping" in recorded, f"stop() 收尾期间应记录到 stopping：{recorded!r}"

        # 投递 stop_done 事件 → 跑 poll() → chat ctx 经 power_state_fn 把按钮刷回 idle
        recorded.clear()
        gui._q.put(("stop_done", 0.05, 1, False))
        deadline = time.time() + 3.0
        while time.time() < deadline and "idle" not in recorded:
            gui._poll()
            time.sleep(0.02)
    assert "idle" in recorded, f"stop_done 经 poll() 后应记录到 idle：{recorded!r}"
    print("  ✓ 引擎接线：start→running、stop→stopping、stop_done+poll→idle（同一回调）")


def test_players_only_stop_returns_to_idle() -> None:
    """「别人说」+ 分玩家模式：没有混音引擎，停止也必须回到 idle。

    真实事故：分玩家模式把 theirs 混音腿从 specs 里删掉 → `ctx.engines` 为空。
    `stop()` 在 `not engines` 分支直接 return，既不投递 `stop_done` 也不刷 idle，
    按钮永远卡在「开始翻译」的运行态（分玩家腿其实已经停了）。
    """
    gui = _make_gui()
    recorded: list[str] = []
    gui._set_power_state = recorded.append             # type: ignore[method-assign]
    ctx = _arm_engine_ctx(gui)
    ctx.power_state_fn = recorded.append
    ctx.direction_var = _Var("theirs")                 # 只有「别人说」→ 没有混音腿
    ctx.cfg.players = {"mode": "decrypt"}

    class _FakeLeg:
        running = True
        stop_calls = 0

        def stop(self, timeout: float = 8.0) -> None:  # noqa: ARG002
            _FakeLeg.stop_calls += 1
            _FakeLeg.running = False

    started: list[bool] = []

    def _fake_start_leg(ctx_, _cfg) -> None:           # noqa: ANN001
        started.append(True)
        ctx_.players_leg = _FakeLeg()

    saved = gui_engine._start_players_leg
    gui_engine._start_players_leg = _fake_start_leg    # type: ignore[assignment]
    try:
        with _fake_engine_class() as instances:
            with contextlib.redirect_stdout(io.StringIO()):
                gui_engine.start(ctx)
            assert started, "分玩家模式没有启动分玩家腿"
            assert recorded == ["running"], f"start 后应只记录 running：{recorded!r}"
            assert not instances, "「别人说」+ 分玩家模式不该再建混音引擎"
    finally:
        gui_engine._start_players_leg = saved

    with contextlib.redirect_stdout(io.StringIO()):
        gui_engine.stop(ctx)
    assert "stopping" in recorded, f"停止收尾期间应记录到 stopping：{recorded!r}"
    assert ctx.players_leg is None, "stop() 应立即摘掉 players_leg（防重复点开始）"

    deadline = time.time() + 3.0
    while time.time() < deadline and "idle" not in recorded:
        gui._poll()
        time.sleep(0.02)
    assert "idle" in recorded, f"停止完成后应回到 idle：{recorded!r}"
    assert _FakeLeg.stop_calls == 1, f"分玩家腿 stop 应调用一次：{_FakeLeg.stop_calls}"
    print("  ✓ 只有分玩家腿时：stop→stopping→stop_done+poll→idle")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_apply_power_state_matrix,
        test_apply_power_state_none_and_garbage,
        test_build_controls_has_single_power_button,
        test_power_styles_are_same_size,
        test_on_power_dispatch,
        test_set_power_state_refreshes_widget,
        test_engine_wiring_running_stopping_idle,
        test_players_only_stop_returns_to_idle,
    ]
    print("test_power_toggle:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
