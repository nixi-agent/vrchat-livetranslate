"""GUI 那条 tick 必须把「手腕屏降级说明」推到状态栏（issue #99 判据 1）。

## 为什么要有这个测试

GUI 下 `Engine._overlay` **恒为 None**：手腕屏实例由**界面**持有（`gui_engine.start_engine`
把 overlay / desktop 从引擎自己的 sinks 里剔掉），每帧的 `tick()` 也走界面自己那条
（`gui_chat.poll` 的 tick 段）。所以引擎 `_pump_loop` 里那句 `_relay_overlay_note()` 在 GUI 下
**一次都跑不到** —— D3D 路降级时状态栏永远不出现说明，用户只能靠猜（只有日志里有）。

判据（照 issue #99 验收判据 1）：降级 → 状态栏出现说明；恢复 → 说明消失（被顶掉）。
离线可跑：假 overlay（只有 `tick()` / `degraded_note`）+ 假 root（`after` 空实现），不起窗口。
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.gui_chat import ChatCtx, poll  # noqa: E402
from vlt.output.overlay import DEGRADED_NOTE_RECOVERED, DegradedNoteRelay  # noqa: E402

NOTE = "手腕屏走的是兼容刷新方式（这套 SteamVR 不支持贴图直传）：内容偶尔会短暂不更新"


class FakeRoot:
    """`poll()` 结尾只调 `root.after(50, …)` —— 记下来就行，不需要真 Tk 事件循环。"""

    def __init__(self) -> None:
        self.jobs: list = []

    def after(self, _ms: int, fn) -> str:
        self.jobs.append(fn)
        return f"job{len(self.jobs)}"


class FakeOverlay:
    """手腕屏替身：本用例只关心 `tick()` 有没有跑 + `degraded_note` 的转发。"""

    def __init__(self) -> None:
        self.degraded_note: str | None = None
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1


def _poll_once(ctx: ChatCtx, root: FakeRoot, status: list[tuple[str, str]]) -> None:
    """跑一跳 `poll()`（不带 Tk 事件循环：直接调一次即可，结尾的 after 被 FakeRoot 吃掉）。"""
    poll(ctx, root,
         lambda lvl, msg: status.append((lvl, msg)),
         lambda *a, **k: None,
         lambda: None,
         {})


def test_relay_pushes_on_change_only() -> None:
    """判重本身：降级推一条 warn、同一句不重复推、恢复推一条 info、换实例重新开始。"""
    status: list[tuple[str, str]] = []
    ov = FakeOverlay()
    relay = DegradedNoteRelay()

    relay.relay(ov, lambda lvl, msg: status.append((lvl, msg)))
    assert status == [], f"正常时不该推任何消息：{status}"

    ov.degraded_note = NOTE
    relay.relay(ov, lambda lvl, msg: status.append((lvl, msg)))
    assert status == [("warn", NOTE)], f"降级应推一条 warn（后端给的说明）：{status}"
    relay.relay(ov, lambda lvl, msg: status.append((lvl, msg)))
    relay.relay(ov, lambda lvl, msg: status.append((lvl, msg)))
    assert len(status) == 1, f"同一句说明不该每帧重复推：{status}"

    ov.degraded_note = None
    relay.relay(ov, lambda lvl, msg: status.append((lvl, msg)))
    assert status[-1] == ("info", DEGRADED_NOTE_RECOVERED), f"恢复要顶掉那条警告：{status}"
    relay.relay(ov, lambda lvl, msg: status.append((lvl, msg)))
    assert len(status) == 2, f"恢复后不该反复推：{status}"

    # 没有 degraded_note 属性的后端（Linux 后端 / 极简替身）必须静默跳过
    relay2 = DegradedNoteRelay()
    relay2.relay(object(), lambda lvl, msg: status.append((lvl, msg)))
    assert len(status) == 2, "可选属性缺失时不该抛异常/不该推空消息"

    # 换实例（界面关掉手腕屏又重新接管）= 新的一轮：**同一句说明**必须能重新推出去
    #   ⚠️ 这一段必须接在「降级中」的状态后面（此时 relay 记得「这句推过了」），
    #      上面已经恢复成正常就测不出来了 —— 实测第一版这么写：删掉换实例的归零仍全绿（假绿）。
    ov_a, ov_b = FakeOverlay(), FakeOverlay()
    ov_a.degraded_note = ov_b.degraded_note = NOTE
    relay3 = DegradedNoteRelay()
    relay3.relay(ov_a, lambda lvl, msg: status.append((lvl, msg)))
    assert status[-1] == ("warn", NOTE)
    relay3.relay(ov_b, lambda lvl, msg: status.append((lvl, msg)))
    assert status[-1] == ("warn", NOTE), f"换实例后同一句说明被静默丢掉了：{status}"
    assert len(status) == 4, f"换实例应恰好再推一条：{status}"
    print("  判重：降级一条 / 恢复一条 / 换实例重新开始 OK")


def test_gui_poll_relays_degraded_note() -> None:
    """GUI 那条 tick（`gui_chat.poll`）必须把说明推到状态栏 —— #98 复审缺的正是这条接线。"""
    status: list[tuple[str, str]] = []
    root = FakeRoot()
    ov = FakeOverlay()
    ctx = ChatCtx(q=queue.Queue(), engines_ref=[], overlay_out_fn=lambda: ov)

    _poll_once(ctx, root, status)
    assert ov.ticks == 1, "poll 一跳必须 tick 手腕屏一次（否则这条用例本身是空转的）"
    assert status == [], f"正常时不该推消息：{status}"

    ov.degraded_note = NOTE
    _poll_once(ctx, root, status)
    assert status == [("warn", NOTE)], f"GUI 路必须把降级说明推给状态栏：{status}"

    _poll_once(ctx, root, status)
    assert len(status) == 1, f"同一说明不该每跳重复推：{status}"

    ov.degraded_note = None
    _poll_once(ctx, root, status)
    assert status[-1] == ("info", DEGRADED_NOTE_RECOVERED), (
        f"恢复后说明必须消失（推到状态栏那句要被顶掉）：{status}")

    # 界面把实例关掉（取消勾选）→ 不存在实例时不许抛异常、也不许推「已恢复」的空话
    ticks_before = ov.ticks
    ctx.overlay_out_fn = lambda: None
    _poll_once(ctx, root, status)
    assert ov.ticks == ticks_before, "实例已被移除，不该再 tick 它"
    assert len(status) == 2, f"没有实例时不该有任何动静：{status}"
    print("  GUI 路：降级进状态栏 + 恢复后说明消失 OK")


if __name__ == "__main__":
    print("test_overlay_degraded_note:")
    test_relay_pushes_on_change_only()
    test_gui_poll_relays_degraded_note()
    print("ALL PASSED")
