"""界面级端到端复验：真 Tk 窗口里，手腕屏「降级说明」必须进状态栏、恢复后消失（issue #99 判据 1）。

**作用**：证明 GUI 那条 tick（`gui_chat.poll`）真的把 `degraded_note` 推到了**状态栏控件**上。
只跑单测时最容易漏的正是这类「接线在另一个 tick 里」的缺口：GUI 下 `Engine._overlay` 恒为 None
（手腕屏实例由界面持有），引擎 `_pump_loop` 里那条转发在这里一次都跑不到 —— 旧代码里降级说明
只在日志里，状态栏永远不出现（#98 合并后复审发现）。

**降级怎么制造**：判据里写的是「假 D3D 或真机均可」。本脚本用**假手腕屏实例**
（只有 `tick()` / `degraded_note`，与真后端同形）翻 `degraded_note`，这样不依赖 SteamVR / 头显，
可重复；「真 D3D 失败触发降级」那条由 `tests/test_overlay_recovery.py` 的假 D3D 覆盖。

**需要的环境**：Windows + **真实桌面会话**（要能开 Tk 窗口；无头会话里 Tk 起不来会直接报错）。
不需要 VRChat / SteamVR / 麦克风 / 头显。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/verify_overlay_degraded_note_gui.py

**会不会写盘**：只写沙箱 —— 配置被指到 `out/_sandbox_config_overlay_note.yaml`，
**绝不碰真实 `config.yaml`**。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # scripts/verify/ -> 仓库根
sys.path.insert(0, str(ROOT))

if sys.platform != "win32":
    print("❌ 本脚本需要在真实桌面会话里开 Tk 窗口（当前不是 Windows）", file=sys.stderr)
    raise SystemExit(2)

# 会话里可能残留测试注入的假 key（见 skill）：清掉，避免污染任何后续分支
os.environ.pop("DASHSCOPE_API_KEY", None)

NOTE = "手腕屏走的是兼容刷新方式（这套 SteamVR 不支持贴图直传）：内容偶尔会短暂不更新"

FAILS: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(label)


class FakeWristOverlay:
    """手腕屏替身：与真后端同形的那几个面（tick / degraded_note / alive / available）。"""

    def __init__(self) -> None:
        self.degraded_note: str | None = None
        self.available = True
        self.pending_anchor = False
        self.ticks = 0

    @property
    def alive(self) -> bool:
        return True

    def tick(self) -> None:
        self.ticks += 1

    def update_entries(self, *_a, **_k) -> None:      # 聊天区推送时会被调用
        pass

    def close(self) -> None:
        pass


def pump(gui, seconds: float = 0.4) -> None:
    """跑真实的 Tk 事件循环 —— `poll` 是 `root.after(50)` 自排的，必须让 after 真跑起来。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        gui._root.update()
        time.sleep(0.02)


def main() -> int:
    # ---- 沙箱：任何写盘都落在 out/ 里 ----
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    sandbox = out_dir / "_sandbox_config_overlay_note.yaml"
    source = ROOT / "config.yaml" if (ROOT / "config.yaml").exists() else ROOT / "config.example.yaml"
    if not source.exists():
        print("❌ 找不到 config.yaml / config.example.yaml")
        return 1
    shutil.copyfile(source, sandbox)
    print(f"[sandbox] 基线配置 = {source.name} → {sandbox.name}")

    import vlt.config as C
    import vlt.gui as G
    import vlt.i18n as I

    G.DEFAULT_CONFIG = sandbox
    C.DEFAULT_CONFIG = sandbox
    I.detect_system_language = lambda: "zh"          # 界面语言钉死（见 skill）

    from vlt.output.overlay import DEGRADED_NOTE_RECOVERED

    print("=== 1. 起界面（真实 Tk）===")
    gui = G.TranslationGUI()
    gui._root.update()
    check(True, "TranslationGUI 起来了")
    check(gui._overlay_out is None, "默认没勾 → 没有手腕屏实例")
    ov = FakeWristOverlay()
    gui._overlay_out = ov                           # 冒充「界面持有的手腕屏实例」
    pump(gui)
    check(ov.ticks > 0, "界面那条 tick 真在跑（否则本脚本是空转的）", f"ticks={ov.ticks}")
    status0 = gui._status_label.cget("text")
    check(NOTE not in status0 and DEGRADED_NOTE_RECOVERED not in status0,
          "正常时状态栏没有降级说明", status0)

    print("=== 2. 降到兼容刷新路 → 状态栏必须出现说明 ===")
    ov.degraded_note = NOTE
    pump(gui)
    status1 = gui._status_label.cget("text")
    check(NOTE in status1, "状态栏出现降级说明", status1)
    check(gui._chat_ctx.last_status_level == "warn", "级别是 warn", gui._chat_ctx.last_status_level)

    print("=== 3. 恢复持久贴图路 → 说明必须消失（被顶掉）===")
    ov.degraded_note = None
    pump(gui)
    status2 = gui._status_label.cget("text")
    check(DEGRADED_NOTE_RECOVERED in status2 and NOTE not in status2,
          "状态栏已改为「已恢复正常」（那条降级说明不再挂着）", status2)

    print("=== 4. 取消勾选（实例被移除）→ 不许抛异常、也不许再推 ===")
    ticks_before = ov.ticks
    gui._overlay_out = None
    pump(gui)
    check(ov.ticks == ticks_before, "实例已移除 → 不再 tick 它")
    check(gui._status_label.cget("text") == status2, "状态栏不再被改写", gui._status_label.cget("text"))

    gui._root.destroy()
    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("✅ GUI 路降级说明端到端通过（真窗口 / 真状态栏 / 真 poll）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
