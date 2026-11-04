"""房间名单（roster）回归：名单只认 output_log 的四类行。

背景（真实踩过的坑）：右栏原来直接拿 Photon 名字事件表当名单，于是

1. **换房间不清空** —— `NameTable.clear()` 只在「绑到新 host」时调，而 UU 加速下
   Photon 的 host 不变（本地端口换来换去，对端一直是隧道），所以上一间房的人
   会一直挂在右栏里；
2. **错过就永远缺** —— 抓包晚于进房时 Join 事件早过去了，那个人再也不会出现。

VRChat 自己写的 `output_log_*.txt` 里两件事都有，口径与 VRCX 的
`Dotnet/LogWatcher.cs` 一致：

```text
[Behaviour] Entering Room: 入梦居 Dreamfall Chambers
[Behaviour] OnPlayerJoined EMeowSystem (usr_3a6252c9-88e9-4062-a5a4-7a10aa0517dc)
[Behaviour] OnPlayerLeft EMeowSystem (usr_3a6252c9-88e9-4062-a5a4-7a10aa0517dc)
[Behaviour] OnLeftRoom
```

换房间时是**先一串 `OnPlayerLeft`、再 `Entering Room`**，所以在 `Entering Room`
处清空是安全的。本测试把这些行喂给 `Roster.observe_line()`，确认四种事件的行
为；另外覆盖 `LogTail` 的增量读取（尤其是「末尾半行不能吐出去」）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.players import roster  # noqa: E402

_UID_A = "usr_3a6252c9-88e9-4062-a5a4-7a10aa0517dc"
_UID_B = "usr_aa57de30-516b-4047-95b6-30601a6c91a2"

FAILED: list[str] = []


def _joined(name: str, uid: str = "") -> str:
    tail = f" ({uid})" if uid else ""
    return f"2026.11.04 11:38:14 Debug      -  [Behaviour] OnPlayerJoined {name}{tail}"


def _left(name: str, uid: str = "") -> str:
    tail = f" ({uid})" if uid else ""
    return f"2026.11.04 11:38:20 Debug      -  [Behaviour] OnPlayerLeft {name}{tail}"


def _room(world: str) -> str:
    return f"2026.11.04 11:38:01 Debug      -  [Behaviour] Entering Room: {world}"


def test_parse_user_info() -> None:
    """行尾的 `(usr_…)` 要摘出来；名字里可以有空格、中文、`․`。"""
    name, uid = roster.parse_user_info(f"EMeowSystem ({_UID_A})")
    assert name == "EMeowSystem" and uid == _UID_A, (name, uid)
    name, uid = roster.parse_user_info(f"超级有节操的逆袭 ({_UID_B.upper()})")
    assert name == "超级有节操的逆袭" and uid == _UID_B, (name, uid)   # 统一小写
    # 老格式：只有显示名，没有 usr_ 后缀
    assert roster.parse_user_info("Unnamed") == ("Unnamed", "")
    # 名字本身带括号，但不能误当成 id
    assert roster.parse_user_info("A (B)") == ("A (B)", "")
    assert roster.parse_user_info("   ") == ("", "")
    print("  ✅ parse_user_info")


def test_join_and_leave() -> None:
    """进/出房间靠 Join/Left 两条事件维护，重复 Join 不重复出行。"""
    r = roster.Roster()
    assert r.observe_line(_room("VRChat Home")) == "world", "Entering Room 要报 world"
    assert r.world == "VRChat Home"
    assert r.observe_line(_joined("EMeowSystem", _UID_A)) == "join"
    assert r.observe_line(_joined("CeciliaFox", _UID_B)) == "join"
    assert len(r) == 2
    # 同一 uid 再 Join 一次（换 actor 的情况）只更新名字、不新增行
    assert r.observe_line(_joined("EMeowSystem2", _UID_A)) == "join"
    assert len(r) == 2, f"重复 Join 不该多出一行：{r.members()}"
    assert r.name_of(_UID_A) == "EMeowSystem2"
    # 离开
    assert r.observe_line(_left("CeciliaFox", _UID_B)) == "left"
    assert len(r) == 1
    assert not r.contains(_UID_B)
    # 离开不存在的人：返回 False，名单不变
    assert r.observe_line(_left("Nope")) == ""
    assert len(r) == 1
    print("  ✅ join / left")


def test_ignored_lines() -> None:
    """`OnPlayerLeftRoom`（整个房间的噪声）与 `OnPlayerJoined:`（冒号变体）不入名单。"""
    r = roster.Roster()
    r.set_world("W")
    assert r.observe_line("2026.11.04 11:38:20 Debug      -  [Behaviour] OnPlayerLeftRoom") == ""
    assert len(r) == 0
    assert r.observe_line("2026.11.04 11:38:20 Debug      -  [Behaviour] OnPlayerJoined:Unnamed") == ""
    assert len(r) == 0
    # 无关行
    assert r.observe_line("2026.11.04 11:38:20 Debug      -  [Behaviour] Joining wrld_123") == ""
    print("  ✅ 忽略噪声行")


def test_room_switch_clears() -> None:
    """换房间：先一串 OnPlayerLeft，再 Entering Room —— 新房间名单从空开始。"""
    r = roster.Roster()
    r.observe_line(_room("旧房间"))
    r.observe_line(_joined("A", _UID_A))
    r.observe_line(_joined("B", _UID_B))
    assert len(r) == 2
    assert r.observe_line(_left("A", _UID_A)) == "left"
    assert r.observe_line(_left("B", _UID_B)) == "left"
    assert len(r) == 0
    assert r.observe_line(_room("新房间")) == "world"
    assert r.world == "新房间" and len(r) == 0
    r.observe_line(_joined("C", "usr_11111111-2222-3333-4444-555555555555"))
    assert [m.name for m in r.members()] == ["C"], r.members()
    # 即使漏了 Leave（直接跳房间），Entering Room 也要把旧的清掉
    assert r.observe_line(_room("第三个房间")) == "world"
    assert len(r) == 0
    print("  ✅ 换房间清空")


def test_left_room() -> None:
    """`OnLeftRoom`（离开世界）清空人，但保留世界名。"""
    r = roster.Roster()
    r.observe_line(_room("W"))
    r.observe_line(_joined("A", _UID_A))
    assert r.observe_line("2026.11.04 11:38:20 Debug      -  [Behaviour] OnLeftRoom") == "leave"
    assert len(r) == 0 and r.world == "W"
    print("  ✅ OnLeftRoom")


def test_name_only_members() -> None:
    """没有 usr_ 的老格式只能按名字认人：同名同 id 才算一个人，不同 id 不串行。"""
    r = roster.Roster()
    r.set_world("W")
    r.add("Unnamed")                              # 直接调 API 也行
    r.add("Unnamed")
    assert len(r) == 1
    r.add("Same", "usr_11111111-2222-3333-4444-555555555555")
    r.add("Same", "usr_99999999-8888-7777-6666-555555555555")
    assert len(r) == 3, f"同名不同 id 该是两个人：{r.members()}"
    # 只有名字的 remove 也要能删掉
    assert r.remove("Unnamed") is True
    assert len(r) == 2
    print("  ✅ 无 id 成员")


def test_log_tail() -> None:
    """增量读：只吐新行；末尾半行留到下次；文件换了/变短了从头重放。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        log = folder / "output_log_2026-11-04_11-27-21.txt"
        log.write_text(_room("W1") + "\n", encoding="utf-8")
        tail = roster.LogTail(folder)
        first = tail.poll()
        assert first == [_room("W1")], first

        # 追加两行，第二行故意不写完（模拟正在写的那一行）
        with log.open("a", encoding="utf-8") as fh:
            fh.write(_joined("A", _UID_A) + "\n")
            fh.write("2026.11.04 11:38:15 Debug      -  [Behaviour] OnPlayerJo")
        second = tail.poll()
        assert second == [_joined("A", _UID_A)], second      # 半行不能吐出去

        # 补完那半行
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"ined B ({_UID_B})\n")
        third = tail.poll()
        assert third == [f"2026.11.04 11:38:15 Debug      -  [Behaviour] OnPlayerJoined B ({_UID_B})"], third

        # 文件被清空（VRChat 重启会新建）：从头重放，不能卡在旧 offset
        log.write_text(_room("W2") + "\n", encoding="utf-8")
        fourth = tail.poll()
        assert fourth == [_room("W2")], fourth
        print("  ✅ LogTail 增量/半行/截断")


def test_log_tail_missing() -> None:
    """目录里没有日志（VRChat 没跑过）：返回空列表，不抛异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        tail = roster.LogTail(Path(tmp))
        assert tail.poll() == []
    print("  ✅ LogTail 无日志")


def test_synced_flag() -> None:
    """`synced` 一旦置位就不再回落：空名单意味着「房间确实没人」。

    右栏靠它决定要不要退回「按名字表凑人」的兜底分支 —— 刚换房间那一瞬名单是空的，
    那时必须显示空，不能把上一间房的人拉回来。
    """
    r = roster.Roster()
    assert not r.synced
    r.observe_line("2026.11.04 11:38:20 Debug      -  [Behaviour] Joining wrld_123")
    assert not r.synced, "无关行不该置位"
    r.observe_line(_room("第一个房间"))
    assert r.synced
    r.set_world("第二个房间")                 # 换房后清空，但已经同步过
    assert r.synced and len(r) == 0
    print("  ✅ synced 标志")


def main() -> int:
    tests = (
        ("parse_user_info", test_parse_user_info),
        ("join / left", test_join_and_leave),
        ("忽略噪声行", test_ignored_lines),
        ("换房间清空", test_room_switch_clears),
        ("OnLeftRoom", test_left_room),
        ("无 id 成员", test_name_only_members),
        ("LogTail 增量", test_log_tail),
        ("LogTail 空目录", test_log_tail_missing),
        ("synced 标志", test_synced_flag),
    )
    for label, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print(f"  ❌ {label}：{exc}")
            FAILED.append(label)
        except Exception as exc:                    # noqa: BLE001
            print(f"  ❌ {label}：{type(exc).__name__}: {exc}")
            FAILED.append(label)
    if FAILED:
        print(f"\n❌ 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(f"\n✅ 全部通过（{len(tests)} 组）")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
