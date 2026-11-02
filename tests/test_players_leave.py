"""玩家离开 / 换房间之后，右栏那一行必须消失（跨平台，Windows 也能跑）。

## 背景（两个真实踩过的坑）

1. **第一次换房不清表**。预热器通常在「已经进了房间」之后才启动，此时
   `warmup._world` 还是空串；旧代码的世界闸门写的是 `if self._world:` 才清，
   于是**第一次**换房只把 `_world` 赋值、跳过清表 —— 上一间房的人一直留在右栏，
   还能被勾选。
2. **租借期间名字表被冻结**。点「开始翻译」之后预热线程不再读包（`if leased:
   wait; continue`），它手里那份 `_names` 是冻结副本；而采集器的名字表只在
   `_decode` 的 `finally` 才回灌一次。结果是整场不更新：离开的人留着、新来的
   的人不出现。修法是 `PlayerLeg._refresh_warmup()` 在名字表版本变化时回灌。

`tests/test_players_warmup.py` 是 Linux-only（开头读 `/proc/self`），在 Windows
上直接 SKIP，所以这两条回归放在这个独立文件里。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import names                    # noqa: E402
from vlt.players.leg import PlayerLeg            # noqa: E402
from vlt.players.warmup import PlayerWarmup      # noqa: E402
from vlt.players_settings import PlayersSettings  # noqa: E402

FAILS: list[str] = []

WORLD_A = b"wrld_00000000-0000-4000-8000-000000000000:12345~region(jp)"
WORLD_B = b"wrld_11111111-1111-4111-8111-111111111111:67890~region(us)"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))
        FAILS.append(name)


def _warmup(log_names: dict[str, str] | None = None) -> PlayerWarmup:
    """纯内存预热器：不碰 socket、不读日志、不提权。"""
    w = PlayerWarmup(PlayersSettings())
    if log_names is not None:
        w._names = names.NameTable(log_names)
    return w


def _seed(w: PlayerWarmup, actor: int, user_id: str, display: str) -> None:
    w._names.feed(b"", code=253,
                  records=[names.PlayerRecord(actor=actor, user_id=user_id,
                                              display_name=display)])


def _join(w: PlayerWarmup, world: bytes) -> None:
    """出向「加入世界」事件（op 226）：`F3 02 E2 00` + 世界 ID。"""
    w._on_message(b"\xf3\x02\xe2\x00" + world)


def _leave(w: PlayerWarmup, actor: int) -> None:
    """入向「离开」事件（254）：`F3 04 FE ... FE 0D <u16 actor>`。"""
    w._on_message(b"\xf3\x04\xfe\x00" + b"\xfe\x0d" + actor.to_bytes(2, "little"))


def _actors(w: PlayerWarmup) -> list[int]:
    return [row["actor"] for row in w.player_rows()]


# ------------------------------------------------------------------ 换房间清表


def test_first_join_world_clears() -> None:
    print("[leave] 预热器还没见过世界时，第一条 226 也要清表")
    w = _warmup()
    _seed(w, 7, "usr_aaaa", "旧房间里的人")
    check("清表前右栏有一行", _actors(w) == [7], str(_actors(w)))
    check("清表前 _world 是空串", w._world == "", repr(w._world))

    _join(w, WORLD_A)
    check("第一次 226 就把上一间房的人清掉", _actors(w) == [], str(_actors(w)))
    check("_world 记下来了", w._world == WORLD_A.decode(), repr(w._world))


def test_join_same_world_keeps_rows() -> None:
    print("[leave] 同一个世界重复 226 不清表（别再把人无故抹掉）")
    w = _warmup()
    _join(w, WORLD_A)
    _seed(w, 9, "usr_bbbb", "同房间的人")
    _join(w, WORLD_A)
    check("同世界重复上报保留行", _actors(w) == [9], str(_actors(w)))


def test_join_other_world_clears_again() -> None:
    print("[leave] 换到另一个世界再清一次")
    w = _warmup()
    _join(w, WORLD_A)
    _seed(w, 9, "usr_bbbb", "A 房间的人")
    _join(w, WORLD_B)
    check("换世界后清空", _actors(w) == [], str(_actors(w)))
    check("_world 已更新", w._world == WORLD_B.decode(), repr(w._world))


def test_clear_keeps_log_names() -> None:
    print("[leave] 清表不破坏 output_log 兜底表")
    w = _warmup({"usr_cccc": "日志里的名字"})
    _seed(w, 11, "usr_cccc", "")          # 事件里没名字 → 回落日志表
    check("兜底名字生效", w._names.label(11) == "日志里的名字", w._names.label(11))

    _join(w, WORLD_A)
    check("清表后行没了", _actors(w) == [], str(_actors(w)))
    _seed(w, 12, "usr_cccc", "")
    check("清表后兜底表还在", w._names.label(12) == "日志里的名字",
          w._names.label(12))
    check("表长仍是 0 之外的 1 行", len(w._names) == 1, str(len(w._names)))


# ------------------------------------------------------------------ 玩家离开


def test_leave_event_drops_row() -> None:
    print("[leave] Event 254 → 那一行立刻消失")
    w = _warmup()
    _seed(w, 21, "usr_dddd", "要走的人")
    _seed(w, 22, "usr_eeee", "留下的人")
    check("两个人都在", sorted(_actors(w)) == [21, 22], str(_actors(w)))

    _leave(w, 21)
    check("离开的人没了", _actors(w) == [22], str(_actors(w)))

    before = w.name_events
    _leave(w, 21)                          # 重复离开：不该抛，也不该再算一次变化
    check("重复 254 不炸", True)
    check("名字事件计数照常累加", w.name_events == before + 1, str(w.name_events))


def test_leave_unknown_actor_is_noop() -> None:
    print("[leave] 未知 actor 的 254 是 no-op")
    w = _warmup()
    _seed(w, 22, "usr_eeee", "留下的人")
    _leave(w, 999)
    check("别人不受影响", _actors(w) == [22], str(_actors(w)))


# ------------------------------------------------- 租借期间回灌名字表（第 2 个坑）


class _FakeCapture:
    """只记录 `adopt_names` 被调用了几次（租借对象的最小替身）。"""

    def __init__(self) -> None:
        self.calls: list[names.NameTable] = []

    def adopt_names(self, table: names.NameTable) -> None:
        self.calls.append(table)


def test_refresh_warmup_forwards_table() -> None:
    print("[leave] 租借期间名字表变化要回灌预热器")
    leg = PlayerLeg(None, warmup=None)
    check("_cap 默认是 None", leg._cap is None, repr(leg._cap))

    leg._refresh_warmup(names.NameTable())          # 没租借 → 静默跳过
    cap = _FakeCapture()
    leg._cap = cap

    table = names.NameTable()
    table.feed(b"", code=253,
               records=[names.PlayerRecord(actor=31, user_id="usr_ffff",
                                           display_name="新来的人")])
    leg._refresh_warmup(table)
    check("回灌了一次", len(cap.calls) == 1, str(len(cap.calls)))
    check("回灌的是采集器那张表", cap.calls and cap.calls[0] is table)

    leg._cap = None
    leg._refresh_warmup(table)
    check("解绑后不再回灌", len(cap.calls) == 1, str(len(cap.calls)))


def test_refresh_warmup_swallows_errors() -> None:
    print("[leave] 回灌失败不能拖垮解码")

    class _Boom:
        def adopt_names(self, table: names.NameTable) -> None:
            raise RuntimeError("预热器已经停了")

    leg = PlayerLeg(None, warmup=None)
    leg._cap = _Boom()
    try:
        leg._refresh_warmup(names.NameTable())
        ok = True
    except Exception:                                  # noqa: BLE001
        ok = False
    check("异常被吞掉", ok)


def main() -> int:
    for test in (test_first_join_world_clears,
                 test_join_same_world_keeps_rows,
                 test_join_other_world_clears_again,
                 test_clear_keeps_log_names,
                 test_leave_event_drops_row,
                 test_leave_unknown_actor_is_noop,
                 test_refresh_warmup_forwards_table,
                 test_refresh_warmup_swallows_errors):
        test()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：{'、'.join(FAILS)}")
        return 1
    print("✅ 全部通过（离开 / 换房间后的右栏行清理）")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
