#!/usr/bin/env python
"""「快封句」验收：麦克风也静了 → 不白等那 3s（但绝不在你说话时抢跑）。

## 实测依据（2026-10-01 真链路，2 句）

| 指标 | 短句(2.0s) | 长句(4.8s) |
|---|---|---|
| 首条译文**上屏** | 开口后 +1.26s | 开口后 +3.29s |
| 累计译文**停止增长** | 说完前 0.89s | 说完前 0.74s |
| 终版（我们自己的 tick 兜底发的） | 说完后 +2.14s | 说完后 +2.44s |

服务端在用户停止说话后的 **8s 内一条事件都不发**（既无 `response.text.done` 也无
`response.done`）→ 没有语义完成信号可用，只能靠定时器；而译文早在说完前就不长了
→ 那 3s 全是白等。故加**双条件**：麦克风静音 ≥ 0.5s（你确实说完了）时，文字静默 1.1s 就封。

⚠️ 为什么快路径**必须**带「麦克风已静」：实测连续说话时相邻 delta 可间隔 **2.3s**
（那是句子**中间**的停顿）—— 只看文字静默会把半句当最终版发出去（抢跑）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.session.base import (DEFAULT_FAST_FINAL_MIC_QUIET_S,   # noqa: E402
                              DEFAULT_FAST_FINAL_SILENCE_S,
                              DEFAULT_FINAL_SILENCE_S, SessionConfig, should_finalize)
from vlt.session.qwen38 import QwenLiveTranslateSession          # noqa: E402


def test_pure_table() -> bool:
    ok = True
    cases = [
        # (文字静默, 麦克风静默, 期望, 说明)
        (3.10, 0.30, True,  "慢路径：麦克风还在说，但文字静默过了 3s → 照旧封"),
        (2.30, 0.20, False, "**不抢跑**：连续说话时那个 2.3s 大间隔（麦克风没静）"),
        (1.20, 1.00, True,  "快路径：麦克风静了 1s + 文字静默 1.2s → 封（省 ~1.8s）"),
        (1.20, 0.20, False, "**不抢跑**：文字静默够了但麦克风还在说"),
        (0.90, 5.00, False, "麦克风静很久了，但文字才静 0.9s（服务端可能还在追）"),
        (1.10, 0.50, True,  "正好卡在阈值上（≥ 即封）"),
        (1.09, 0.50, False, "差一点就不封"),
    ]
    for text_q, mic_q, want, why in cases:
        got = should_finalize(text_quiet_s=text_q, mic_quiet_s=mic_q)
        cond = got is want
        print(f"  文字静默={text_q:.2f}s 麦克风静默={mic_q:.2f}s → {got}"
              f"（期望 {want}）{'OK' if cond else '✗'}  {why}")
        ok &= cond
    return ok


def test_pure_edges() -> bool:
    ok = True
    # 从未上送过音频（没有音频在流 = 没人在说话）→ 快路径按「已静」处理
    got = should_finalize(text_quiet_s=1.2, mic_quiet_s=None)
    cond = got is True
    print(f"  从未上送音频 + 文字静默 1.2s → {got}（期望 True）  {'OK' if cond else '✗'}")
    ok &= cond
    # 关掉快路径（fast_final_silence_s=None）→ 退回纯 3.0s 行为
    got = should_finalize(text_quiet_s=1.2, mic_quiet_s=9.9, fast_silence_s=None)
    cond = got is False
    print(f"  关掉快路径 + 文字静默 1.2s → {got}（期望 False）  {'OK' if cond else '✗'}")
    ok &= cond
    got = should_finalize(text_quiet_s=3.1, mic_quiet_s=9.9, fast_silence_s=None)
    cond = got is True
    print(f"  关掉快路径 + 文字静默 3.1s → {got}（期望 True，慢路径还在）  {'OK' if cond else '✗'}")
    ok &= cond
    # 阈值可调
    got = should_finalize(text_quiet_s=0.6, mic_quiet_s=1.0, fast_silence_s=0.55)
    cond = got is True
    print(f"  自定义快阈值 0.55s + 文字静默 0.6s → {got}（期望 True）  {'OK' if cond else '✗'}")
    ok &= cond
    # 默认值一致性（config 默认 1.1 / 0.5，别被悄悄改大）
    cond = (DEFAULT_FAST_FINAL_SILENCE_S == 1.1 and DEFAULT_FAST_FINAL_MIC_QUIET_S == 0.5
            and DEFAULT_FINAL_SILENCE_S == 3.0)
    print(f"  默认阈值：快={DEFAULT_FAST_FINAL_SILENCE_S}s/麦克风={DEFAULT_FAST_FINAL_MIC_QUIET_S}s"
          f"，慢={DEFAULT_FINAL_SILENCE_S}s  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_tick_wiring() -> bool:
    """真实 `QwenLiveTranslateSession.tick()` 接线：两条路径都要按判据行动。"""
    ok = True

    def sealed(*, text_quiet: float, mic_quiet: float | None,
               cfg: SessionConfig | None = None) -> bool:
        s = QwenLiveTranslateSession(cfg or SessionConfig(api_key="x"))
        s._buf = ["こんにちは。"]                     # 有一段累计译文
        now = time.perf_counter()
        s._last_text_at = now - text_quiet
        s._last_audio_at = (now - mic_quiet) if mic_quiet is not None else 0.0
        out: list[bool] = []
        s._emit = lambda **kw: out.append(bool(kw.get("is_final")))   # type: ignore[method-assign]
        s.tick()
        return bool(out and out[0])

    cond = sealed(text_quiet=1.2, mic_quiet=1.0) is True
    print(f"  真实 tick：文字静默 1.2s + 麦克风静 1.0s → 封句  {'OK' if cond else '✗'}")
    ok &= cond
    cond = sealed(text_quiet=1.2, mic_quiet=0.2) is False
    print(f"  真实 tick：文字静默 1.2s + 麦克风还在说 → 不封  {'OK' if cond else '✗'}")
    ok &= cond
    cond = sealed(text_quiet=3.1, mic_quiet=0.2) is True
    print(f"  真实 tick：文字静默 3.1s（麦克风还在说）→ 慢路径封  {'OK' if cond else '✗'}")
    ok &= cond
    cond = sealed(text_quiet=1.2, mic_quiet=1.0,
                  cfg=SessionConfig(api_key="x", fast_final_silence_s=None)) is False
    print(f"  真实 tick：关掉快路径 → 不封  {'OK' if cond else '✗'}")
    ok &= cond
    # 已经封过同一段文本 → 不重复发（既有行为，别被改坏）
    s = QwenLiveTranslateSession(SessionConfig(api_key="x"))
    s._buf = ["こんにちは。"]
    now = time.perf_counter()
    s._last_text_at, s._last_audio_at = now - 5.0, now - 5.0
    s._last_final_text = "こんにちは。"
    out: list[bool] = []
    s._emit = lambda **kw: out.append(True)          # type: ignore[method-assign]
    s.tick()
    cond = not out
    print(f"  真实 tick：同一段文本已封过 → 不重复发  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


if __name__ == "__main__":
    print("test_fast_finalize:")
    print(" 1) 纯函数判据表（含两条「不抢跑」）")
    ok = test_pure_table()
    print(" 2) 纯函数边界（从未上送/关掉快路径/可调阈值/默认值）")
    ok &= test_pure_edges()
    print(" 3) 真实 tick 接线")
    ok &= test_tick_wiring()
    assert ok, "快封句用例失败（见上）"
    print("ALL PASSED")
