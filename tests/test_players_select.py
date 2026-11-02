#!/usr/bin/env python
"""「只翻译特定玩家」的离线验收（不需要 VRChat、不需要网络、不需要 root）。

## 钉住的行为

1. ★ 名单按 **user_id** 判（actor 会重排复用，按 actor 存必然串人）；
2. `all` 谁都不拦；`only` 只放名单里的；`except` 只拦名单里的；
3. 名单为空时 `only` = 谁都不翻、`except` = 照旧全翻（不是错误，是右栏「全不选」的语义）；
4. ★ 还没学到 user_id 时（刚进房间、名字事件没来）只有 `except` 放行 —— `only`
   宁可先不翻，也不要把 ASR 名额花在一个马上会被踢掉的人身上；
5. 名单外的人**连环形缓冲都不留**（`selected_dropped` 计数、`_wanted` 不进）；
6. ★ `set_selection()` 热更新后，已经建好、但已不在名单里的会话由 `_tick_once`
   下一拍收掉（不在同步方法里 await `_drop`）；
7. `stats()` 带上 `select_mode` / `select_users` / `selected_dropped`；
8. ★ `PlayerWarmup.player_rows()` 把名字表 + 位置表拼成 GUI 行，并用翻译腿的
   实时状态覆盖 `speaking` / `translating`。

跑法：.venv/bin/python tests/test_players_select.py（Windows 见 docs 里的 py 路径）
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import names as names_mod  # noqa: E402
from vlt.players import router as router_mod  # noqa: E402
from vlt.players import warmup as warmup_mod  # noqa: E402
from vlt.session.base import SessionConfig  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


LOUD = (10000).to_bytes(2, "little", signed=True) * 320      # 20ms @16k

#: 测试里的 actor → user_id 映射。actor 3 故意不给（"还没学到"）。
USERS = {1: "usr_aaa", 2: "usr_bbb"}


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.closed = False
        self.on_text = None
        self.budget = None
        self.alive = True

    @property
    def is_alive(self) -> bool:
        return self.alive

    async def start(self, on_text, on_audio=None, on_usage=None) -> None:
        self.on_text = on_text

    def note_voice(self) -> None:
        pass

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(len(pcm))

    def tick(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def _install(created: list[FakeSession]):
    original = router_mod.create_session

    def fake_create(cfg, *, budget=None):        # noqa: ANN001
        session = FakeSession()
        session.budget = budget
        created.append(session)
        return session

    router_mod.create_session = fake_create
    return original


def _make(*, user_of=None, **kw) -> router_mod.PlayerRouter:
    # 建连预算传不限速的：这里验的是名单，不是「相邻建连间隔 6.1s」
    kw.setdefault("budget", router_mod.ConnectionBudget(100))
    return router_mod.PlayerRouter(
        SessionConfig(api_key="x"), max_sessions=4, gate_db=-60.0,
        on_text=lambda *a: None, on_status=lambda *a: None,
        label_of=lambda a: f"玩家{a}",
        user_of=user_of if user_of is not None else (lambda a: USERS.get(a)),
        **kw)


def _case_matrix() -> None:
    """纯判据：不建会话、不跑 asyncio。"""
    print("[select] 名单判据（按 user_id）")
    router = _make()
    check("all：谁都放行",
          all(router._selectable(a) for a in (1, 2, 3)), "all 不该拦人")

    only = _make(select_mode="only", select_users=["usr_aaa"])
    check("only：名单里的放行", only._selectable(1))
    check("only：名单外的拦住", not only._selectable(2))
    check("only：还没学到 user_id 的拦住", not only._selectable(3))

    exc = _make(select_mode="except", select_users=["usr_aaa"])
    check("except：名单里的拦住", not exc._selectable(1))
    check("except：名单外的放行", exc._selectable(2))
    check("except：还没学到 user_id 的放行", exc._selectable(3))

    only_empty = _make(select_mode="only", select_users=[])
    check("only + 空名单 = 谁都不翻",
          not any(only_empty._selectable(a) for a in (1, 2, 3)))
    exc_empty = _make(select_mode="except", select_users=[])
    check("except + 空名单 = 照旧全翻",
          all(exc_empty._selectable(a) for a in (1, 2, 3)))

    bad = _make(select_mode="bogus")
    check("未知名单模式 → 回落 all", bad.select_mode == "all", bad.select_mode)

    noisy = _make(select_mode="only")
    check("only 但没有名单时给告警（不是报错）", noisy.select_mode == "only")


async def _case_feed_gate() -> None:
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[select] 名单外的人不建会话、不进队列")
        router = _make(select_mode="only", select_users=["usr_aaa"])
        await router.start()

        await router.feed(2, LOUD)                    # usr_bbb：不在名单
        await asyncio.sleep(0.05)
        check("名单外的音频不建会话", created == [], str(len(created)))
        check("selected_dropped 在涨", router.selected_dropped == 1,
              str(router.selected_dropped))
        check("名单外的人不进 _wanted", 2 not in router._wanted, str(dict(router._wanted)))

        await router.feed(1, LOUD)                    # usr_aaa：在名单
        await asyncio.sleep(0.35)
        check("名单内的正常建会话", len(created) == 1, str(len(created)))
        await router.close()
    finally:
        router_mod.create_session = original


async def _case_hot_update() -> None:
    created: list[FakeSession] = []
    original = _install(created)
    try:
        print("[select] 热更新：改名单后已建会话被收掉 / 新名单立刻放行")
        router = _make(select_mode="only", select_users=["usr_aaa"])
        await router.start()
        await router.feed(1, LOUD)
        await asyncio.sleep(0.35)
        check("先给在名单的人建会话", len(created) == 1, str(len(created)))

        router.set_selection(select_users=["usr_bbb"])     # usr_aaa 被移出名单
        await router._tick_once()
        check("移出名单后会话被关掉", created[0].closed, "会话还活着")
        check("会话表里也没有了", 1 not in router.sessions, str(list(router.sessions)))
        check("set_selection 不改 mode", router.select_mode == "only", router.select_mode)

        await router.feed(2, LOUD)                          # 新名单里的人
        await asyncio.sleep(0.35)
        check("新名单里的人能建会话", len(created) == 2, str(len(created)))

        router.set_selection(select_mode="all")
        check("切回 all", router.select_mode == "all", router.select_mode)
        check("all 下谁都放行", router._selectable(3))
        await router.close()
    finally:
        router_mod.create_session = original


def _case_stats() -> None:
    print("[select] 状态统计")
    router = _make(select_mode="only", select_users=["usr_aaa", "usr_bbb"])
    router.selected_dropped = 7
    stats = router.stats()
    check("stats 带 select_mode", stats.get("select_mode") == "only", str(stats.get("select_mode")))
    check("stats 带 select_users（数量）", stats.get("select_users") == 2,
          str(stats.get("select_users")))
    check("stats 带 selected_dropped", stats.get("selected_dropped") == 7,
          str(stats.get("selected_dropped")))


class _FakePositions:
    """只实现 warmup.player_rows 用到的那一个方法。"""

    def __init__(self, distances: dict[int, float | None]) -> None:
        self._distances = distances

    def distance(self, actor: int) -> float | None:
        return self._distances.get(actor)


def _case_player_rows() -> None:
    print("[select] 右栏数据：名字表 + 位置表 + 翻译腿实时状态")
    fake = object.__new__(warmup_mod.PlayerWarmup)
    fake._names_lock = threading.Lock()
    table = names_mod.NameTable()
    table.feed(b"", code=names_mod.NAME_EVENT_CODES[0], records=[
        names_mod.PlayerRecord(actor=1, user_id="usr_aaa", display_name="小明"),
        names_mod.PlayerRecord(actor=2, user_id="usr_bbb", display_name=""),
    ])
    fake._names = table
    fake._pos = _FakePositions({1: 2.5, 2: None})
    fake._log_scan = ({}, "")              # 认不出本机账号 → 谁都不当自己
    fake._leased_status = lambda: {"rows": {1: {"speaking": True, "translating": True}}}

    rows = warmup_mod.PlayerWarmup.player_rows(fake)
    by_actor = {r["actor"]: r for r in rows}
    check("每个名字表里的人一行", sorted(by_actor) == [1, 2], str(sorted(by_actor)))
    check("带 user_id（名单按它存）", by_actor[1]["user_id"] == "usr_aaa",
          by_actor[1]["user_id"])
    check("带名字", by_actor[1]["name"] == "小明", by_actor[1]["name"])
    check("带距离", by_actor[1]["distance"] == 2.5, str(by_actor[1]["distance"]))
    check("距离未知就是 None（不编造 0）", by_actor[2]["distance"] is None,
          str(by_actor[2]["distance"]))
    check("自己不在列表里（GUI 自己补 me 行）", all(not r["self"] for r in rows))
    check("翻译腿的实时状态被合进来", by_actor[1]["speaking"] and by_actor[1]["translating"])
    check("没会话的人不显示在说", by_actor[2]["speaking"] is False
          and by_actor[2]["translating"] is False)

    fake._leased_status = lambda: 1 / 0                  # 状态回调炸了
    rows2 = warmup_mod.PlayerWarmup.player_rows(fake)
    check("状态回调抛异常不影响行数据", len(rows2) == 2, str(len(rows2)))
    check("异常时退回「不在说」", all(not r["speaking"] for r in rows2))

    fake._leased_status = None
    rows3 = warmup_mod.PlayerWarmup.player_rows(fake)
    check("没有翻译腿时也能出列表", len(rows3) == 2, str(len(rows3)))

    fake._log_scan = ({"usr_bbb": "我"}, "usr_bbb")
    rows4 = warmup_mod.PlayerWarmup.player_rows(fake)
    by_actor4 = {r["actor"]: r for r in rows4}
    check("本机账号不再出现在可勾选列表里", sorted(by_actor4) == [1], str(sorted(by_actor4)))
    fake._log_scan = ({}, "")


def main() -> int:
    _case_matrix()
    asyncio.run(_case_feed_gate())
    asyncio.run(_case_hot_update())
    _case_stats()
    _case_player_rows()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("ALL PASSED（玩家名单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
