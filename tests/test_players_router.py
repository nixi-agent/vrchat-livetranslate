#!/usr/bin/env python
"""分玩家路由器的离线验收（不需要 VRChat、不需要网络、不需要 root）。

## 钉住的行为

1. 门限以下的块**不建会话**（省钱）；有人说话才建，之后复用；
2. ★ 并发块只建**一条**会话（采集线程每 20ms 一块，每块是独立协程）；
3. 建会话失败按 5/10/15/30s 退避，半成品必须关掉；
4. 会话被服务端掐掉后 tick 立刻发现，退避后重建；
5. 所有会话共享**同一个** RPM 预算（条数 + 相邻最小间隔）；
6. ★ 名额满且**都在说话**时：off 模式不踢人、新人的块丢弃并警告（`overflow_dropped`）；
7. ★ 名额满但有人**空闲 >15s**：回收空闲者；距离模式优先回收更远的；
   `order` 模式下全都在说话时，**更近**的新来者让最远者让位（距离未知不换）；
8. ★ 排队期间的音频（最近 2s）在会话就绪后补发，句首不丢。

跑法：xvfb-run -a .venv/bin/python tests/test_players_router.py（不需要显示，加 xvfb 只是沿用仓库口径）
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import router as router_mod  # noqa: E402
from vlt.session.base import SessionConfig, TextDelta  # noqa: E402
from vlt.session.qwen38 import ConnectionBudget  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


LOUD = (10000).to_bytes(2, "little", signed=True) * 320      # 20ms @16k
QUIET = b"\x00\x00" * 320


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.closed = False
        self.on_text = None
        self.budget = None
        self.alive = True
        self.voice_notes = 0

    @property
    def is_alive(self) -> bool:
        return self.alive

    async def start(self, on_text, on_audio=None, on_usage=None) -> None:
        self.on_text = on_text

    def note_voice(self) -> None:
        self.voice_notes += 1

    async def send_audio(self, pcm: bytes) -> None:
        if not self.alive:
            raise RuntimeError("连接已断")
        self.sent.append(len(pcm))

    def tick(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class FailingSession(FakeSession):
    """start() 就失败：用来钉「失败退避 + 半成品必须关掉」。"""

    async def start(self, on_text, on_audio=None, on_usage=None) -> None:
        raise RuntimeError("服务端拒绝：Throttling.RateQuota")


def _install(created: list[FakeSession], cls=FakeSession):
    original = router_mod.create_session

    def fake_create(cfg, *, budget=None):        # noqa: ANN001
        session = cls()
        session.budget = budget
        created.append(session)
        return session

    router_mod.create_session = fake_create
    return original


async def _settle(seconds: float = 0.35) -> None:
    """等建连循环跑完一轮（它合并 0.2s 内的唤醒）。"""
    await asyncio.sleep(seconds)


def _make(*, max_sessions: int = 1, **kw) -> router_mod.PlayerRouter:
    return router_mod.PlayerRouter(
        SessionConfig(api_key="x"), max_sessions=max_sessions, gate_db=-60.0,
        on_text=kw.pop("on_text", lambda *a: None),
        on_status=kw.pop("on_status", lambda *a: None),
        label_of=lambda a: f"玩家{a}", **kw)


async def _case_basic() -> None:
    created: list[FakeSession] = []
    original = _install(created)
    try:
        texts: list[tuple] = []
        budget = ConnectionBudget(100)
        router = _make(max_sessions=1, on_text=lambda *a: texts.append(a),
                       budget=budget)
        await router.start()

        print("[router] 门限 / 建会话 / 复用 / 补发")
        await router.feed(1, QUIET)
        await _settle(0.05)
        check("静音不建会话", created == [], str(len(created)))

        await router.feed(1, LOUD)
        await _settle()
        check("有人说话才建会话", len(created) == 1, str(len(created)))
        check("音频被送进会话（含 preroll 补发）",
              bool(created[0].sent) and created[0].sent[-1] == len(LOUD),
              str(created[0].sent))
        check("共用注入的预算", created[0].budget is not None)

        await router.feed(1, LOUD)
        await _settle(0.05)
        check("同一个 actor 复用会话", len(created) == 1, str(len(created)))
        check("已发块计数在涨", router.sent_chunks >= 2, str(router.sent_chunks))

        print("[router] 文本增量")
        created[0].on_text(TextDelta(confirmed="你好", source="hello", is_final=False))
        check("首条增量立即上抛", len(texts) == 1, str(texts))
        check("带上 actor / 名字", texts and texts[0][0] == 1 and texts[0][1] == "玩家1",
              str(texts[:1]))
        check("带上原文与译文",
              texts and texts[0][2] == "hello" and texts[0][3] == "你好", str(texts[:1]))

        await router.close()
        check("收尾关掉全部会话", created[-1].closed)
    finally:
        router_mod.create_session = original


async def _case_concurrent() -> None:
    """一个 20ms 块一个协程：并发进来只能建一条会话。"""
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[router] 并发建会话（一人说话的 1 秒 = 50 块）")
        router = _make(max_sessions=3, budget=ConnectionBudget(100))
        await router.start()
        await asyncio.gather(*(router.feed(1, LOUD) for _ in range(50)))
        await _settle()
        check("50 个并发块只建 1 条会话", len(created) == 1, str(len(created)))
        check("会话表只有一条", len(router.sessions) == 1, str(list(router.sessions)))
        check("队列里没有残留", router.stats()["queued"] == 0, str(router.stats()))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_overflow() -> None:
    """名额满 + 都在说话（off 模式）= 不踢人；有人空闲 = 回收空闲者。"""
    created: list[FakeSession] = []
    original = _install(created)
    status: list[str] = []
    try:
        print("[router] 名额满：不踢正在说话的人")
        router = _make(max_sessions=1, on_status=lambda lvl, msg: status.append(msg),
                       budget=ConnectionBudget(100))
        await router.start()
        await router.feed(1, LOUD)
        await _settle()
        check("第一个说话的人拿到会话", len(created) == 1, str(len(created)))

        for _ in range(25):                  # 第二个人也在说话（0.5s 音频）
            await router.feed(2, LOUD)
        await _settle()
        check("第二个人没有抢走会话", len(created) == 1, str(len(created)))
        check("第一个人的会话还在", 1 in router.sessions, str(list(router.sessions)))
        check("排队里记着第二个人", router.stats()["queued"] == 1, str(router.stats()))
        check("给了「暂不翻译」的警告",
              any("暂不翻译" in m for m in status), str(status[:3]))

        # 第一个人沉默 >15s → 变成可回收
        router.sessions[1].last_audio -= router_mod.HOT_SILENCE_S + 5
        router.sessions[1].last_text = 0.0
        await router.feed(2, LOUD)
        await _settle()
        check("空闲者被回收", created[0].closed and 1 not in router.sessions,
              str(list(router.sessions)))
        check("第二个人拿到会话", 2 in router.sessions and len(created) == 2,
              str(len(created)))
        check("排队期间的音频被补发（句首不丢）",
              len(created[1].sent) >= 20, str(len(created[1].sent)))
        check("补发不算「溢出丢弃」", router.stats()["overflow_dropped"] == 0,
              str(router.stats()))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_distance() -> None:
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[router] 距离门限 / 距离排序（空闲优先，全忙按距离让位）")
        dists = {1: 5.0, 2: 50.0, 3: 2.0, 4: 80.0}
        gate = _make(max_sessions=2, distance_mode="gate", max_distance_m=10.0,
                     distance_of=lambda a: dists.get(a),
                     budget=ConnectionBudget(100))
        await gate.start()
        await gate.feed(2, LOUD)
        await _settle()
        check("超过门限的人不建会话", created == [], str(len(created)))
        check("门限丢弃计数", gate.distance_dropped == 1, str(gate.distance_dropped))
        await gate.feed(1, LOUD)
        await _settle()
        check("门限内的人正常建会话", len(created) == 1, str(len(created)))
        await gate.close()

        order = _make(max_sessions=2, distance_mode="order",
                      distance_of=lambda a: dists.get(a),
                      budget=ConnectionBudget(100))
        await order.start()
        await order.feed(1, LOUD)            # 5m
        await _settle()
        await order.feed(2, LOUD)            # 50m
        await _settle()
        check("两条会话都建起来", len(created) == 3, str(len(created)))
        # 都沉默 → 都变成可回收
        for st in order.sessions.values():
            st.last_audio -= router_mod.HOT_SILENCE_S + 5
            st.last_text = 0.0
        await order.feed(3, LOUD)            # 2m：更近 → 回收 50m 那个
        await _settle()
        check("优先回收更远的人", 2 not in order.sessions and 1 in order.sessions,
              str(list(order.sessions)))
        # 再来一个 80m 的，剩下的都空闲 → 比所有人都远，不值得换
        for st in order.sessions.values():
            st.last_audio -= router_mod.HOT_SILENCE_S + 5
            st.last_text = 0.0
        before = len(order.sessions)
        await order.feed(4, LOUD)
        await _settle()
        check("更远的人不踢空闲的近距离者", len(order.sessions) == before
              and 4 not in order.sessions, str(list(order.sessions)))
        await order.close()

        # 全都在说话：只有更近的新来者才让最远者让位（严格 <，无滞回）
        status_hot: list[str] = []
        hot = _make(max_sessions=2, distance_mode="order",
                    distance_of=lambda a: dists.get(a),
                    on_status=lambda lvl, msg: status_hot.append(msg),
                    budget=ConnectionBudget(100))
        await hot.start()
        await hot.feed(1, LOUD)              # 5m
        await _settle()
        await hot.feed(2, LOUD)              # 50m
        await _settle()
        await hot.feed(3, LOUD)              # 2m：两人都在说话，仍应换掉 50m
        await _settle()
        check("全员在说话：更近的人让最远者让位",
              2 not in hot.sessions and 3 in hot.sessions, str(list(hot.sessions)))
        check("按距离换人计数", hot.stats()["distance_evictions"] == 1,
              str(hot.stats()))
        await hot.feed(4, LOUD)              # 80m：比在场的都远 → 不换
        await _settle()
        check("全员在说话：更远的人不换", 4 not in hot.sessions,
              str(list(hot.sessions)))
        dists[5] = None                       # 距离未知的新来者 → 不能凭猜踢人
        await hot.feed(5, LOUD)
        await _settle()
        check("全员在说话：距离未知不换人", 5 not in hot.sessions,
              str(list(hot.sessions)))
        check("距离未知的警告有单独口径",
              any("按距离排序" in m for m in status_hot), str(status_hot[:3]))
        await hot.close()

        # 全员距离未知：没有排序依据 → 不换人（也不退化成 LRU 踢正在说话的人）
        blind = _make(max_sessions=1, distance_mode="order",
                      distance_of=lambda a: None,
                      budget=ConnectionBudget(100))
        await blind.start()
        await blind.feed(1, LOUD)
        await _settle()
        await blind.feed(2, LOUD)
        await _settle()
        check("全员距离未知：不按距离换人",
              2 not in blind.sessions and 1 in blind.sessions,
              str(list(blind.sessions)))
        await blind.close()
    finally:
        router_mod.create_session = original


async def _case_backoff() -> None:
    """建会话失败：按 5/10/15/30s 退避，且半成品必须关掉。"""
    created: list[FakeSession] = []
    original = _install(created, FailingSession)
    status: list[str] = []
    try:
        print("[router] 建会话失败 → 退避 + 关半成品")
        router = _make(max_sessions=3, on_status=lambda lvl, msg: status.append(msg),
                       budget=ConnectionBudget(100))
        await router.start()
        await router.feed(1, LOUD)
        await _settle()
        check("失败只试一次", len(created) == 1, str(len(created)))
        check("半成品被关掉（不留泄漏连接）", created[0].closed)
        first = router._retry_at.get(1, 0.0) - time.monotonic()    # noqa: SLF001
        check("第一次退避 ≈5s", 4.0 < first <= 5.0, f"{first:.2f}s")
        await router.feed(1, LOUD)
        await _settle()
        check("退避期内不再试", len(created) == 1, str(len(created)))
        router._retry_at[1] = 0.0                                 # noqa: SLF001
        await router.feed(1, LOUD)
        await _settle()
        check("退避到期后重试", len(created) == 2, str(len(created)))
        second = router._retry_at.get(1, 0.0) - time.monotonic()   # noqa: SLF001
        check("失败阶梯递增到 ~10s", 9.0 < second <= 10.0, f"{second:.2f}s")
        check("状态栏给了原因与等待秒数",
              any("5s 后重试" in m for m in status), str(status[:2]))
        check("失败计数", router.stats()["create_failures"] == 2, str(router.stats()))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_dead_session() -> None:
    """会话被服务端掐掉 → tick 立刻发现并重建（受退避约束）。"""
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[router] 断线检测 → 退避后重建")
        router = _make(max_sessions=3, budget=ConnectionBudget(100))
        await router.start()
        await router.feed(1, LOUD)
        await _settle()
        check("先有一条会话", len(created) == 1 and 1 in router.sessions)
        created[0].alive = False
        await router._tick_once()                                 # noqa: SLF001
        check("断线会话被移除", 1 not in router.sessions, str(list(router.sessions)))
        check("断线会话被关闭", created[0].closed)
        await router.feed(1, LOUD)
        await _settle()
        check("退避期内不重建", len(created) == 1, str(len(created)))
        router._retry_at[1] = 0.0                                 # noqa: SLF001
        await router.feed(1, LOUD)
        await _settle()
        check("退避到期后重建", len(created) == 2 and 1 in router.sessions,
              str(len(created)))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_voice_signal() -> None:
    """响块要上报 `note_voice()`，静音 tail 不能报（否则快封句会被静音骗开）。"""
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[router] 有人说话 → note_voice（快封句依赖它）")
        router = _make(max_sessions=1, budget=ConnectionBudget(100))
        await router.start()
        await router.feed(1, LOUD)
        await _settle()
        check("响块上报了 note_voice", created[0].voice_notes > 0,
              str(created[0].voice_notes))
        before = created[0].voice_notes
        quiet = (3).to_bytes(2, "little", signed=True) * 320     # ≈ -80 dBFS
        for _ in range(5):
            await router.feed(1, quiet)
        await _settle(0.05)
        check("静音块不上报", created[0].voice_notes == before,
              f"{created[0].voice_notes} vs {before}")
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_distance_scope() -> None:
    """距离只在建会话时判一次：建好之后走到 50m 也不许中途丢块。"""
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[router] 距离只在建会话时判（中途不丢）")
        dists = {1: 5.0}
        router = _make(max_sessions=1, distance_mode="gate", max_distance_m=6.0,
                       distance_of=lambda a: dists.get(a),
                       budget=ConnectionBudget(100))
        await router.start()
        await router.feed(1, LOUD)
        await _settle()
        check("6m 内建了会话", len(created) == 1 and 1 in router.sessions)
        sent = router.sent_chunks
        dists[1] = 50.0                       # 人走远了
        for _ in range(10):
            await router.feed(1, LOUD)
        await _settle(0.05)
        check("走远后音频仍在上送（不按块丢）", router.sent_chunks > sent,
              f"{router.sent_chunks} vs {sent}")
        check("不算距离丢弃", router.stats()["distance_dropped"] == 0,
              str(router.stats()))

        dists[2] = 50.0
        await router.feed(2, LOUD)
        await _settle()
        check("超出距离的人不建会话", len(created) == 1, str(len(created)))
        check("记一次「因距离没建」", router.stats()["distance_dropped"] == 1,
              str(router.stats()))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_raw_preroll() -> None:
    """排队期缓存原始音频：门限以下的轻声句首也要补发。"""
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[router] 原始音频补发（轻声句首不丢）")
        router = _make(max_sessions=1, budget=ConnectionBudget(100))
        await router.start()
        soft = (46).to_bytes(2, "little", signed=True) * 320     # ≈ -57 dBFS
        for _ in range(10):
            await router.feed(1, soft)        # 门限以下：一块都不上送
        await _settle(0.05)
        check("轻声不建会话也不上送", created == [] and router.sent_chunks == 0)
        await router.feed(1, LOUD)            # 终于过线
        await _settle()
        check("建会话", len(created) == 1, str(len(created)))
        check("补发包含之前的轻声块", len(created[0].sent) >= 11,
              str(len(created[0].sent)))
        await router.close()
    finally:
        router_mod.create_session = original


def _case_level_summary() -> None:
    print("[router] 电平画像")
    check("样本太少返回空", router_mod.level_summary([-30] * 5) == "")
    text = router_mod.level_summary([-20, -30, -40, -50] * 10)
    check("p50/p90 有值", text == "p50=-30 p90=-20", text)
    check("空输入不炸", router_mod.level_summary([]) == "")


async def _case_budget() -> None:
    """预算的两道闸：条数窗口 + 相邻建连最小间隔。"""
    print("[router] 建连预算：最大间隔与排队计数")
    budget = ConnectionBudget(9, min_interval_s=0.12)
    t0 = time.monotonic()
    await budget.acquire()
    first = time.monotonic() - t0
    check("第一条不必等", first < 0.05, f"{first:.3f}s")
    await budget.acquire()
    gap = time.monotonic() - t0
    check("第二条按最小间隔排队", gap >= 0.10, f"{gap:.3f}s")
    check("排队/成功计数", budget.waits == 1 and budget.acquired == 2,
          f"waits={budget.waits} acquired={budget.acquired}")

    tight = ConnectionBudget(1)
    tight._stamps.append(time.monotonic() - 61.0)     # noqa: SLF001 - 伪造一分钟前的记录
    t = time.monotonic()
    await tight.acquire()
    check("超过 60s 的旧记录会被清掉", time.monotonic() - t < 0.05,
          f"{time.monotonic() - t:.3f}s")


def main() -> int:
    asyncio.run(_case_basic())
    asyncio.run(_case_concurrent())
    asyncio.run(_case_overflow())
    asyncio.run(_case_distance())
    asyncio.run(_case_backoff())
    asyncio.run(_case_dead_session())
    asyncio.run(_case_voice_signal())
    asyncio.run(_case_distance_scope())
    asyncio.run(_case_raw_preroll())
    _case_level_summary()
    asyncio.run(_case_budget())
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（分玩家路由器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
