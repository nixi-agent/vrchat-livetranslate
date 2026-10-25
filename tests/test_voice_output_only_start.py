"""只勾「译音输出」也能开始翻译（v0.12.2 用户实测回归）。

## 症状

主界面「输出」行只勾「译音输出」（不要 chatbox / 手腕屏 / 桌面字幕），点「开始翻译」
被一句「请至少选择一个输出」挡住；再勾上 chatbox 才能开始。失败的几次点击在
用户日志里**一行都没有**（旧守卫是静默的）。

## 根因

`vlt/gui_engine.start()` 拼 sinks 时只认 `chatbox_var / overlay_var / desktop_var`，
`vmic_var`（译音输出）不算任何输出。可方向含「我说的话」时译音本来就是一条真输出
（`Direction.output_audio`），后面也确实按 `want_audio` 配了。守卫与真实能力不一致。

## 本文件钉住

1. 只勾译音输出 + 方向 mine / dual → **能启动**，`ctx.sinks` 含 `voice`，
   `Direction.output_audio` 为 True，启动日志的 `输出=` 里出现 `voice`；
2. 只勾译音输出 + 方向 theirs → **明确 warn**（含「我说的话」）且**不启动**，
   不允许只丢一句含糊的「请至少选择一个输出」；
3. 四个输出全不勾 → warn「请至少选择一个输出」+ 不启动；
4. 守卫拦人时**必须留痕**：`[gui] ⚠️` 一行里带四个开关的真实布尔值与方向
   （禁静默降级；下次只看用户日志就能定位）；
5. 防修过头：theirs + 勾了 chatbox（译音仍无效）→ 照常启动，译音被忽略但要 warn。

## 离线保证

沙箱配置（`_cfgbox.sandbox_config`）+ 钉死界面语言 zh + headless GUI；
`Engine` / 手腕屏 / 桌面字幕全部换成假对象，绝不真连 WebSocket、绝不开音频设备。
走的是用户那条路：`TranslationGUI._start()` → `gui_engine.start()`。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _cfgbox import sandbox_config  # noqa: E402

CONFIG = sandbox_config(reset=True)

import vlt.i18n as _i18n  # noqa: E402

# 构造 TranslationGUI 之前钉死界面语言（CI 是英文系统，不钉会按 en 取词条）。
_i18n.detect_system_language = lambda: "zh"

import vlt.gui_engine as gui_engine  # noqa: E402


# ---------------------------------------------------------------- 替身（不开真设备）


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

    def __init__(self, value=False) -> None:  # noqa: ANN001
        self._v = value

    def get(self):
        return self._v

    def set(self, value) -> None:  # noqa: ANN001
        self._v = value


class _Root:
    """占位 Tk root：只提供 after/after_cancel（双方向错峰 300ms 不会真的跑）。"""

    def after(self, *_a, **_k) -> str:
        return "job"

    def after_cancel(self, *_a, **_k) -> None:
        return None


class _FakeEngine:
    """引擎替身：只记录构造参数与 start 调用，绝不真连。"""

    instances: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.running = False
        self.start_calls = 0
        _FakeEngine.instances.append(self)

    def start(self) -> None:
        self.start_calls += 1
        self.running = True


@contextlib.contextmanager
def _fake_engine_class():
    """把 `gui_engine.Engine` 换成替身；手腕屏 / 桌面字幕也换成不碰设备的假实现。"""
    saved = (gui_engine.Engine, gui_engine.start_overlay, gui_engine.start_desktop)
    _FakeEngine.instances.clear()
    gui_engine.Engine = _FakeEngine                       # type: ignore[assignment]
    gui_engine.start_overlay = lambda _ctx, **_kw: False  # type: ignore[assignment]
    gui_engine.start_desktop = lambda _ctx, **_kw: False  # type: ignore[assignment]
    try:
        yield _FakeEngine.instances
    finally:
        gui_engine.Engine, gui_engine.start_overlay, gui_engine.start_desktop = saved


def _make_gui(direction: str = "mine", *, chatbox: bool = False,
              overlay: bool = False, desktop: bool = False, vmic: bool = False):
    """headless GUI + 四个输出开关的占位变量（等于主界面勾选状态）。"""
    from vlt.gui import TranslationGUI

    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        gui = TranslationGUI(headless=True)
    _i18n.set_language("zh")
    gui._cfg.session_base["api_key"] = "test-key-not-real"
    gui._root = _Root()                                  # type: ignore[assignment]
    gui._power_btn = _Widget()                           # type: ignore[assignment]
    gui._direction_var = _Var(direction)
    gui._chatbox_var = _Var(chatbox)
    gui._overlay_var = _Var(overlay)
    gui._desktop_var = _Var(desktop)
    gui._vmic_var = _Var(vmic)
    # 状态栏文本要能被读到（headless 不建控件）：给它一对占位控件。
    gui._chat_ctx.status_dot = _Widget()
    gui._chat_ctx.status_label = _Widget()
    # 房间 / 门限探针 / 输入框都不在本用例范围内（避免真连、真开探针）。
    # 关键：refresh_api_key_fn 会去凭据存储 / 百炼 CLI 配置里捞**真实 key**（本仓库踩过：
    # 测试把真 key 顶进内存，日志里能看到 116 字符），这里一律不解析。
    gui._engine_ctx.refresh_api_key_fn = None
    gui._engine_ctx.start_room_fn = None
    gui._engine_ctx.refresh_room_status_fn = None
    gui._engine_ctx.sync_gate_probe_fn = None
    gui._engine_ctx.set_text_input_enabled_fn = None
    return gui


def _status_text(gui) -> str:                            # noqa: ANN001
    return str(gui._chat_ctx.status_label.cget("text") or "")


def _run_start(gui):                                      # noqa: ANN001, ANN202
    """跑用户那条路 `_start()`，返回 (日志, 引擎实例)。"""
    buf = io.StringIO()
    with _fake_engine_class() as instances, contextlib.redirect_stdout(buf):
        gui._start()
    return buf.getvalue(), instances


# ---------------------------------------------------------------- ① mine / dual：必须能开始


def test_voice_only_mine_and_dual_starts() -> None:
    for d in ("mine", "dual"):
        gui = _make_gui(d, vmic=True)
        log, instances = _run_start(gui)
        ctx = gui._engine_ctx

        assert "voice" in ctx.sinks, f"{d}：译音输出应被算作有效输出，实际 sinks={ctx.sinks!r}"
        assert instances, f"{d}：只勾译音输出时必须真的启动引擎（旧代码在这里被守卫拦下）"
        assert instances[0].start_calls == 1, f"{d}：引擎没被 start()"
        assert gui._power_state == "running", f"{d}：启动后按钮应进入 running，实际 {gui._power_state!r}"

        assert "请至少选择一个输出" not in _status_text(gui), \
            f"{d}：不该再弹「请至少选择一个输出」，实际状态栏={_status_text(gui)!r}"
        assert "输出检查未通过" not in log, f"{d}：启动路径不该走守卫拦人：{log!r}"

        # 真的会出声：mine 腿的 output_audio 必须是 True（不只是「能点开始」）
        mine_dir = gui._cfg.directions.get("mine")
        assert mine_dir is not None and mine_dir.output_audio is True, \
            f"{d}：mine 腿 output_audio 应为 True，实际 {getattr(mine_dir, 'output_audio', None)!r}"
        assert (gui._cfg.output.get("audio") or {}).get("enabled") is True, \
            f"{d}：output.audio.enabled 应为 True"

        # 启动日志的「输出=」里要出现 voice（旧日志继续骗人的那行）
        head = [ln for ln in log.splitlines() if "[gui] 启动：" in ln]
        assert head, f"{d}：缺启动日志行：{log!r}"
        outs = head[0].split("输出=")[1].split("|")[0].strip()
        assert "voice" in outs, f"{d}：启动日志的 输出= 必须含 voice，实际「{outs}」（{head[0]}）"
        print(f"  ✓ 方向={d}：只勾译音输出 → 启动成功，sinks={sorted(ctx.sinks)}，"
              f"日志 输出={outs}，output_audio=True")


# ---------------------------------------------------------------- ② theirs：明确 warn + 不启动


def test_voice_only_theirs_warns_and_does_not_start() -> None:
    gui = _make_gui("theirs", vmic=True)
    log, instances = _run_start(gui)

    assert not instances, "方向只翻别人 + 只勾译音输出：译音不会出声，绝不能真启动"
    assert gui._power_state == "idle", f"未启动时按钮应保持 idle，实际 {gui._power_state!r}"

    status = _status_text(gui)
    assert "我说的话" in status, f"必须说清「译音只对『我说的话』有效」，实际状态栏={status!r}"
    assert "请至少选择一个输出" not in status, \
        f"不能只丢含糊的「请至少选择一个输出」，实际状态栏={status!r}"
    assert gui._chat_ctx.last_status_level == "warn", gui._chat_ctx.last_status_level

    # 禁静默降级：拦下必须有一行 ⚠️ 日志，且带四个开关真实值与方向
    assert "[gui] ⚠️" in log, f"守卫拦人必须留痕：{log!r}"
    assert "chatbox=False overlay=False desktop=False vmic=True" in log, \
        f"日志缺四个开关的实际布尔值：{log!r}"
    assert "方向=theirs" in log, f"日志缺方向：{log!r}"
    assert "[gui] 启动：方向=" not in log, f"守卫拦下后不该出现启动日志行：{log!r}"
    print("  ✓ 方向=theirs：明确 warn（含「我说的话」）且不启动；日志留痕含四开关+方向")


# ---------------------------------------------------------------- ③ 一个都不勾


def test_no_output_warns_and_does_not_start() -> None:
    gui = _make_gui("mine")
    log, instances = _run_start(gui)

    assert not instances, "四个输出都不勾时必须拦下"
    status = _status_text(gui)
    assert "请至少选择一个输出" in status, f"一个都没勾时仍要给出这条提示，实际={status!r}"
    assert gui._chat_ctx.last_status_level == "warn"
    assert "[gui] ⚠️" in log and "chatbox=False overlay=False desktop=False vmic=False" in log, \
        f"全不勾也要留痕（含四开关）：{log!r}"
    assert "方向=mine" in log, f"日志缺方向：{log!r}"
    print("  ✓ 四个输出全不勾：warn + 不启动，且 ⚠️ 日志含四个 False 与方向")


# ---------------------------------------------------------------- ④ 防修过头


def test_theirs_with_chatbox_still_starts_and_ignores_voice() -> None:
    """theirs + chatbox + 译音：照常启动（chatbox 是有效输出），但译音要被忽略并 warn。"""
    gui = _make_gui("theirs", chatbox=True, vmic=True)
    log, instances = _run_start(gui)
    ctx = gui._engine_ctx

    assert instances, "有 chatbox 这个有效输出时不能被新守卫拦下"
    assert "voice" not in ctx.sinks, f"theirs 方向不该把译音算成有效输出：{ctx.sinks!r}"
    assert "chatbox" in ctx.sinks
    status = _status_text(gui)
    assert "译音输出" in status and "我说的话" in status, \
        f"译音被忽略时必须 warn 说清原因，实际={status!r}"
    assert gui._chat_ctx.last_status_level == "warn"
    print(f"  ✓ 方向=theirs + chatbox：照常启动，译音被忽略并 warn「{status}」")


# ---------------------------------------------------------------- 入口


def main() -> int:
    print("test_voice_output_only_start:")
    cases = [
        ("只勾译音 + mine/dual 能开始", test_voice_only_mine_and_dual_starts),
        ("只勾译音 + theirs 明确 warn 且不启动", test_voice_only_theirs_warns_and_does_not_start),
        ("四个输出全不勾 warn + 不启动", test_no_output_warns_and_does_not_start),
        ("theirs + chatbox 照常启动（防修过头）", test_theirs_with_chatbox_still_starts_and_ignores_voice),
    ]
    failed = 0
    for name, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ❌ {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
