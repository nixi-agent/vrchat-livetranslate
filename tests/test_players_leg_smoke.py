#!/usr/bin/env python
"""分玩家腿的**启动/停止**冒烟（不需要 VRChat、不需要提权）。

## 钉住的行为

1. 等待游戏时状态栏有话说，`stop()` 能在一个超时内把线程收掉；
2. 预热没就绪（socket/密钥还没好）时腿**只等待并 warn** —— 绝不回退到「一次性
   助手」，否则点开始翻译会再弹一次 pkexec，把正在进行的授权卡住。

跑法：xvfb-run -a .venv/bin/python tests/test_players_leg_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import leg as leg_mod  # noqa: E402
from vlt.players.leg import PlayerLeg  # noqa: E402
from vlt.session.base import SessionConfig  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


class _StubWarmup:
    """假预热器：`ready=False` 时模拟「已授权、正在取钥」。"""

    def __init__(self, *, ready: bool = False) -> None:
        self._ready = ready
        self.key = object() if ready else None

    def available(self) -> bool:
        return self._ready

    def status(self) -> dict:
        return {"phase": "acquiring", "detail": "已授权，正在获取密钥（约 1~20 秒）…"}

    def lease(self):
        return None


def _leg(warmup, status: list[tuple[str, str]]) -> PlayerLeg:
    return PlayerLeg(
        SessionConfig(api_key="x"),
        on_status=lambda lvl, msg: status.append((lvl, msg)),
        max_sessions=1, pre_seconds=1.0, warmup=warmup)


def main() -> int:
    status: list[tuple[str, str]] = []
    leg = _leg(_StubWarmup(), status)
    leg._wait_host = lambda seconds: None          # noqa: SLF001 - 测试打桩
    leg.start()
    check("线程起来了", leg.running)
    time.sleep(0.6)
    check("等待游戏时状态栏有话说",
          any("VRChat" in msg for _lvl, msg in status), str(status))
    t0 = time.monotonic()
    leg.stop(timeout=5.0)
    elapsed = time.monotonic() - t0
    check("stop 能及时收掉线程", not leg.running, str(leg.running))
    check("stop 不卡（<3s）", elapsed < 3.0, f"{elapsed:.2f}s")

    # 游戏在、但预热还在取钥：只能等，不能自己提权
    wait_status: list[tuple[str, str]] = []
    waiting = _leg(_StubWarmup(ready=False), wait_status)
    host = SimpleNamespace(pid=1, starttime=0.0, name="VRChat")
    waiting._wait_host = lambda seconds: host       # noqa: SLF001 - 测试打桩
    waiting.start()
    time.sleep(0.6)
    msgs = [m for _lvl, m in wait_status]
    check("预热没就绪时只等待（warn）",
          any("等待预热" in m for m in msgs), str(wait_status))
    check("提示不再单独弹授权",
          any("不会单独弹授权" in m for m in msgs), str(wait_status))
    check("等待期间没有走一次性助手",
          not hasattr(leg_mod, "HelperSession") and not hasattr(leg_mod, "_HelperCapture"),
          str([n for n in ("HelperSession", "_HelperCapture") if hasattr(leg_mod, n)]))
    waiting.stop(timeout=5.0)
    check("等待中的腿也能及时停", not waiting.running, str(waiting.running))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家腿生命周期）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
