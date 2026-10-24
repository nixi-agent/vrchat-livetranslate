"""房间协议层验收：帧编解码往返、房间码、`seq` 乱序自愈、final 幂等、partial 覆盖式合并。

`vlt/room/protocol.py` 是**纯函数**（零 IO、零网络），所以这些断言全部可以离线跑，
不需要起任何 WebSocket。协议层写错，上面客户端和服务端再对也白搭 —— 这一层先钉死。

## 为什么每条都要测（都是真会发生的事故）

* **partial 累加** → 手腕屏出现「你你你好你你好」。partial 是全量快照，只能覆盖。
* **seq 只认更高** → 帧乱序/重传时，旧快照盖掉新快照，屏幕上的字「往回退」。
* **final 不幂等** → ack 丢了客户端会补发 final，同一句话在屏幕上叠好几条。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.room.protocol import (ALPHABET, FATAL_ERR_CODES, FRAME_KINDS, KNOWN_FIELDS,  # noqa: E402
                               ROOM_CODE_LEN, ProtocolError, apply_seg, decode_frame,
                               encode_frame, is_valid_room_code, merge_segment,
                               new_peer_id, new_room_code, normalize_room_code, now_ms,
                               should_accept, should_accept_final, should_send_partial)


def test_encode_decode_roundtrip() -> None:
    """★ 每种帧类型编出来再解回来，字段一个都不能丢、类型一个都不能变。"""
    cases = [
        ("hello", {"room": "ABCD1234", "tok": "secret", "nick": "小明", "ts": 1_700_000_000_000}),
        ("welcome", {"me": "p_ab12cd", "srv_ts": 1_700_000_000_111}),
        ("members", {"members": [{"id": "p_ab12cd", "nick": "小明", "online": True},
                                 {"id": "p_ff00ee", "nick": "阿华", "online": False}],
                     "srv_ts": 5}),
        ("seg", {"utt": "u1", "seq": 3, "text": "你好啊", "lang": "zh",
                 "final": False, "ts": 7, "src": "p_ab12cd", "srv_ts": 8}),
        ("final", {"utt": "u1", "seq": 9, "text": "你好啊，我到了", "lang": "zh",
                   "final": True, "ts": 9, "src": "p_ab12cd"}),
        ("ack", {"utt": "u1", "seq": 9, "ts": 10}),
        ("ping", {"ts": 11}),
        ("pong", {"ts": 12}),
        ("err", {"code": "auth", "msg": "令牌不对"}),
    ]
    for kind, fields in cases:
        raw = encode_frame(kind, **fields)
        assert isinstance(raw, str), f"{kind} 编出来不是 str：{type(raw)}"
        assert "\n" not in raw, f"{kind} 帧里不该有换行（单行 JSON 才好抓包）：{raw!r}"
        back = decode_frame(raw)
        assert back["t"] == kind, f"{kind} 帧类型丢了：{back}"
        for key, want in fields.items():
            assert back.get(key) == want, f"{kind}.{key} 往返后变了：{back.get(key)!r} != {want!r}"
    print(f"  编解码往返 OK（{len(cases)} 种帧）")


def test_encode_is_compact_and_utf8() -> None:
    """中文不转义、分隔符紧凑：帧要 <1KB，`\\uXXXX` 会把中文撑成 6 倍。"""
    raw = encode_frame("seg", utt="u1", seq=1, text="你好", lang="zh", final=False)
    assert "\\u" not in raw, f"中文被转义了（体积翻 6 倍）：{raw!r}"
    assert '", "' not in raw and '": ' not in raw, f"分隔符没压紧：{raw!r}"
    assert len(raw.encode("utf-8")) < 200, f"单帧太大：{len(raw.encode('utf-8'))}B"
    print(f"  紧凑编码 OK（{len(raw.encode('utf-8'))}B：{raw}）")


def test_encode_drops_none_fields() -> None:
    """值为 None 的字段直接丢掉：没令牌就别在帧里留个 `"tok":null`。"""
    raw = encode_frame("hello", room="ABCD1234", tok=None, nick="小明")
    assert "tok" not in raw, f"None 字段该被丢掉：{raw}"
    assert json.loads(raw) == {"t": "hello", "room": "ABCD1234", "nick": "小明"}
    print("  None 字段自动丢弃 OK")


def test_encode_rejects_unknown_kind_and_field() -> None:
    """★ 编码严：帧类型拼错、字段名没登记 → 立刻抛。

    拼错字段名（`txt` 写成 `text`）是「对端死活收不到内容」这类最难查的 bug，
    必须在开发期炸掉，而不是等到线上两头对日志。
    """
    for bad in ("SEG", "segment", "", None, 42, "seg "):
        try:
            encode_frame(bad)
        except ProtocolError:
            continue
        raise AssertionError(f"未知帧类型 {bad!r} 没被拦住")
    try:
        encode_frame("seg", utt="u1", txt="拼错了")
    except ProtocolError as exc:
        assert "txt" in str(exc), f"报错里应该点出是哪个字段：{exc}"
    else:
        raise AssertionError("没登记的字段名没被拦住")
    print(f"  编码严格校验 OK（帧类型 {len(FRAME_KINDS)} 种 / 字段 {len(KNOWN_FIELDS)} 个已登记）")


def test_decode_rejects_garbage() -> None:
    """坏 JSON / 不是对象 / 缺 `t` / `t` 不是字符串 → 一律 `ProtocolError`（不许静默）。"""
    bad = [
        "",                                     # 空串
        "not json at all",                      # 坏 JSON
        '{"t": "seg", ',                        # 截断的 JSON
        "[1,2,3]",                              # 是 JSON 但不是对象
        '"just a string"',                      # 同上
        "null",
        "{}",                                   # 缺 t
        '{"seq": 1, "text": "没有 t"}',          # 缺 t
        '{"t": 123}',                           # t 不是字符串
        '{"t": ""}',                            # t 是空串
        42,                                     # 连字符串都不是
        None,
        b"\xff\xfe\x00bad utf8",                # 非法 UTF-8 字节
    ]
    for raw in bad:
        try:
            decode_frame(raw)
        except ProtocolError:
            continue
        raise AssertionError(f"垃圾输入没被拦住：{raw!r}")
    print(f"  垃圾输入全部抛 ProtocolError OK（{len(bad)} 例）")


def test_decode_tolerates_unknown_fields_and_kinds() -> None:
    """★ 解码宽：未知字段原样保留（消费方忽略）、未知帧类型不抛。

    对端将来加字段/加帧类型，不该把老客户端打死 —— 否则升级服务端就是全网掉线。
    """
    frame = decode_frame('{"t":"seg","utt":"u1","seq":2,"text":"你好",'
                         '"lang":"zh","来自未来的字段":{"a":1}}')
    assert frame["text"] == "你好" and frame["seq"] == 2
    assert frame["来自未来的字段"] == {"a": 1}, "未知字段应原样保留，由消费方忽略"
    future = decode_frame('{"t":"hologram","data":"新帧类型"}')
    assert future["t"] == "hologram", "未知帧类型不该抛，交给消费方忽略"
    print("  解码宽容（未知字段/未知帧类型）OK")


def test_decode_normalizes_dirty_values() -> None:
    """脏类型不许把接收端打崩：`seq` 给字符串、`members` 给字典 → 归一，不抛。"""
    f = decode_frame('{"t":"seg","utt":"u1","seq":"7","text":123,"final":1,"ts":"x"}')
    assert f["seq"] == 7, f"字符串 seq 没归一成 int：{f['seq']!r}"
    assert f["text"] == "123", f"数字 text 没归一成 str：{f['text']!r}"
    assert f["final"] is True
    assert f["ts"] == 0, f"解析不动的 ts 应回落 0：{f['ts']!r}"
    m = decode_frame('{"t":"members","members":{"id":"x"}}')
    assert m["members"] == [], f"members 不是列表时应回落空列表：{m['members']!r}"
    b = decode_frame('{"t":"pong","ts":1}'.encode("utf-8"))
    assert b["t"] == "pong", "bytes 帧应该也能解（websockets 可能给 bytes）"
    print("  脏值归一 OK")


def test_decode_rejects_oversize_frame() -> None:
    """超大帧直接拒（防对端灌垃圾把内存打爆）。"""
    huge = json.dumps({"t": "seg", "utt": "u", "seq": 1, "text": "啊" * 20000},
                      ensure_ascii=False)
    try:
        decode_frame(huge)
    except ProtocolError as exc:
        assert "上限" in str(exc)
    else:
        raise AssertionError("超大帧没被拦住")
    print("  超大帧被拒 OK")


def test_room_code() -> None:
    """★ 房间码：8 位 Crockford Base32，字符集里**没有** I/L/O/U（手输不会认错）。"""
    assert len(ALPHABET) == 32, f"字符集不是 32 个：{len(ALPHABET)}"
    for ch in "ILOU":
        assert ch not in ALPHABET, f"字符集里混进了易混字符 {ch}"
    # 格式测试不应依赖有限随机空间恰好没有碰撞。
    codes = ["".join(ALPHABET[(value >> (5 * digit)) & 31]
                     for digit in range(ROOM_CODE_LEN - 1, -1, -1)) for value in range(300)]
    with patch('vlt.room.protocol.secrets.choice', side_effect="".join(codes * 2)) as random_char:
        for _ in range(300):
            code = new_room_code()
            assert len(code) == ROOM_CODE_LEN, f"长度不对：{code!r}"
            assert is_valid_room_code(code), f"自己生成的码居然不合法：{code!r}"
            assert all(c in ALPHABET for c in code)
        assert len({new_room_code() for _ in range(300)}) == 300, "300 次生成出现重复（随机源有问题）"
    assert random_char.call_args_list == [call(ALPHABET)] * (600 * ROOM_CODE_LEN)

    good = ["ABCD1234", "abcd1234", "  abcd-1234  ", "00000000", "ZZZZZZZZ",
            "ABCDI234"]                     # I → 1，口语里就是这么念的
    for s in good:
        assert is_valid_room_code(s), f"合法房间码被拒：{s!r} → {normalize_room_code(s)!r}"
    bad = ["", "ABC", "ABCD123", "ABCD12345", "ABCD12U4",           # U 是非法字符
           "ABCD-12", None, 42, ["ABCD1234"], "中文房间码啊啊"]
    for s in bad:
        assert not is_valid_room_code(s), f"非法房间码被放行：{s!r}"
    assert normalize_room_code(" ab-cdILOu ") == "ABCD110U", normalize_room_code(" ab-cdILOu ")
    assert normalize_room_code(None) == ""
    print(f"  房间码 OK（生成 300 个不重复；合法 {len(good)} 例 / 非法 {len(bad)} 例）")


def test_peer_id_format() -> None:
    """成员 id 格式：`p_` + 6 位 hex（方案 §4 的 `me` 字段）。"""
    with patch('vlt.room.protocol.secrets.token_hex', side_effect=[f'{i:06x}' for i in range(200)]) as random_hex:
        ids = {new_peer_id() for _ in range(200)}
    assert len(ids) == 200, "成员 id 撞了"
    for pid in ids:
        assert pid.startswith("p_") and len(pid) == 8, f"格式不对：{pid!r}"
        int(pid[2:], 16)                                # 后 6 位必须是 hex
    assert random_hex.call_args_list == [call(3)] * 200
    print("  成员 id 格式 OK")


def test_should_accept_seq_self_healing() -> None:
    """★ `seq` 乱序自愈：**只接受更高的**，相等和更低一律拒。"""
    assert should_accept(1, -1), "新句（current=-1）必须能收下第一帧"
    assert should_accept(0, -1)
    assert should_accept(5, 4)
    assert not should_accept(4, 4), "相等 = 重传，不该再刷一次界面"
    assert not should_accept(3, 5), "旧快照不许盖掉新快照（否则屏幕上的字往回退）"
    assert not should_accept(-1, 0)
    # 脏输入不许抛
    assert should_accept("7", 3), "字符串 seq 应尽力转成 int"
    assert not should_accept(None, 3), "转不动的值按 0 处理 → 拒"
    assert not should_accept("abc", 0)
    print("  seq 乱序自愈 OK")


def test_merge_segment_is_overwrite_not_append() -> None:
    """★ partial 合并 = 覆盖，**绝不累加**（累加就是「你你你好你你好」那个 bug）。"""
    assert merge_segment("你", "你好") == "你好"
    assert merge_segment("你好", "你好啊") == "你好啊"
    assert merge_segment("你好啊", "你") == "你", "全量快照说变短就变短（模型改口了），照实覆盖"
    assert merge_segment("你好", "") == "你好", "空快照不许把屏幕刷成空白"
    assert merge_segment("你好", "   ") == "你好"
    assert merge_segment("你好", None) == "你好"
    assert merge_segment("", "你好") == "你好"
    assert merge_segment(None, None) == ""
    # 连续喂 5 个快照，结果必须等于最后一个，而不是拼起来的 15 个字
    cur = ""
    for snap in ("我", "我在", "我在测", "我在测试", "我在测试翻译"):
        cur = merge_segment(cur, snap)
    assert cur == "我在测试翻译", f"覆盖式合并出了累加：{cur!r}"
    print("  partial 覆盖式合并 OK（绝不累加）")


def test_final_idempotent() -> None:
    """★ final 幂等：同一句的 final 只认第一次（ack 丢了会补发，服务端也可能重投）。"""
    seen: set[str] = set()
    key = "p_ab12cd:u1"
    assert should_accept_final(seen, key), "第一次 final 必须收"
    assert not should_accept_final(seen, key), "重复 final 必须被丢掉"
    assert not should_accept_final(seen, key), "第三次也一样"
    assert should_accept_final(seen, "p_ab12cd:u2"), "不同句互不影响"
    assert should_accept_final(seen, "p_ff00ee:u1"), "同一 utt、不同说话人也互不影响"
    assert not should_accept_final(seen, ""), "空 key 一律拒"
    assert not should_accept_final(seen, None)
    print("  final 幂等去重 OK")


def test_apply_seg_out_of_order_and_dedup() -> None:
    """★ 端到端把三条纪律串起来：乱序自愈 + 覆盖式合并 + final 幂等。"""
    store: dict[str, dict] = {}
    seen: set[str] = set()

    def feed(**kw):
        frame = {"t": "seg", "src": "p_a", "utt": "u1", "lang": "zh", "srv_ts": 0}
        frame.update(kw)
        return apply_seg(store, seen, frame)

    e = feed(seq=1, text="我")
    assert e and e["text"] == "我" and e["seq"] == 1
    e = feed(seq=2, text="我在")
    assert e["text"] == "我在", "新快照必须覆盖"
    assert feed(seq=1, text="我") is None, "★ 迟到的旧快照必须被挡掉（否则文字往回退）"
    assert feed(seq=2, text="我在") is None, "重复的同 seq 帧必须被挡掉"
    assert store["p_a:u1"]["text"] == "我在", f"存储被旧帧改坏了：{store}"
    e = feed(seq=5, text="我在测试翻译")           # 中间丢了几帧也没关系
    assert e["text"] == "我在测试翻译" and e["seq"] == 5
    assert feed(seq=3, text="我在测") is None, "丢帧后迟到的中间快照仍要挡掉"

    fin = feed(t="final", seq=6, text="我在测试翻译功能", final=True)
    assert fin and fin["final"] is True and fin["text"] == "我在测试翻译功能"
    assert feed(t="final", seq=6, text="我在测试翻译功能", final=True) is None, \
        "★ 重复 final 必须幂等（ack 丢了会补发，不幂等就会在屏幕上叠好几条）"
    assert feed(seq=7, text="定稿后又来的 partial") is None, "已定稿的句子不许再被 partial 改"

    # final 带着比现有 partial 更旧的 seq（补发场景）也必须收下 —— final 是终态
    store2: dict[str, dict] = {}
    seen2: set[str] = set()
    apply_seg(store2, seen2, {"t": "seg", "src": "p_b", "utt": "u9", "seq": 9, "text": "半句"})
    e2 = apply_seg(store2, seen2,
                   {"t": "final", "src": "p_b", "utt": "u9", "seq": 4, "text": "整句定稿"})
    assert e2 is not None and e2["text"] == "整句定稿", f"带旧 seq 的 final 被误杀：{e2}"
    assert e2["seq"] == 9, "seq 取两者的最大值（保持单调）"

    # 脏帧：缺 src / 缺 utt → 丢弃，不抛
    assert apply_seg(store, seen, {"t": "seg", "utt": "u1", "seq": 99, "text": "x"}) is None
    assert apply_seg(store, seen, {"t": "seg", "src": "p_a", "seq": 99, "text": "x"}) is None
    print("  apply_seg 三纪律合一 OK（乱序自愈 / 覆盖合并 / final 幂等）")


def test_apply_seg_keys_by_speaker() -> None:
    """两个人同时说同一句 id 也不能互相覆盖（key 是 `src:utt`）。"""
    store: dict[str, dict] = {}
    seen: set[str] = set()
    apply_seg(store, seen, {"t": "seg", "src": "p_a", "utt": "u1", "seq": 1, "text": "甲说的话"})
    apply_seg(store, seen, {"t": "seg", "src": "p_b", "utt": "u1", "seq": 1, "text": "乙说的话"})
    assert store["p_a:u1"]["text"] == "甲说的话"
    assert store["p_b:u1"]["text"] == "乙说的话"
    assert len(store) == 2
    print("  多说话人互不覆盖 OK")


def test_partial_throttle() -> None:
    """★ partial 节流：内容没变不发、间隔不够不发；`min_interval_ms<=0` = 不限流。"""
    now = now_ms()
    assert should_send_partial("", 0, "你好", now, 200), "第一帧必须发"
    assert not should_send_partial("你好", now, "你好", now + 1000, 200), "内容没变不该发"
    assert not should_send_partial("你好", now, "你好啊", now + 50, 200), "间隔不足 200ms 不该发"
    assert should_send_partial("你好", now, "你好啊", now + 200, 200), "间隔够了且内容变了就该发"
    assert should_send_partial("你好", now, "你好啊", now + 1, 0), "min_interval_ms=0 → 不限流"
    assert not should_send_partial("", 0, "", now, 0), "空文本不发"
    assert not should_send_partial("", 0, "   ", now, 0)
    assert not should_send_partial("", 0, None, now, 0)
    print("  partial 节流 OK")


def test_frame_constants_cover_plan() -> None:
    """帧类型表与字段表要跟方案 §4 对得上（少一个就是两端对不上话）。"""
    assert FRAME_KINDS == {"hello", "welcome", "members", "seg", "final",
                           "ack", "ping", "pong", "err"}, sorted(FRAME_KINDS)
    for f in ("t", "room", "tok", "nick", "me", "members", "utt", "seq", "text",
              "lang", "final", "ts", "srv_ts", "src"):
        assert f in KNOWN_FIELDS, f"方案 §4 的字段 {f} 没登记"
    assert {"auth", "room_full", "bad_room"} <= FATAL_ERR_CODES
    assert now_ms() > 1_700_000_000_000, "墙钟毫秒数明显不对"
    print(f"  协议常量对齐方案 §4 OK（{len(FRAME_KINDS)} 帧 / {len(KNOWN_FIELDS)} 字段）")


if __name__ == "__main__":
    print("test_room_protocol:")
    test_encode_decode_roundtrip()
    test_encode_is_compact_and_utf8()
    test_encode_drops_none_fields()
    test_encode_rejects_unknown_kind_and_field()
    test_decode_rejects_garbage()
    test_decode_tolerates_unknown_fields_and_kinds()
    test_decode_normalizes_dirty_values()
    test_decode_rejects_oversize_frame()
    test_room_code()
    test_peer_id_format()
    test_should_accept_seq_self_healing()
    test_merge_segment_is_overwrite_not_append()
    test_final_idempotent()
    test_apply_seg_out_of_order_and_dedup()
    test_apply_seg_keys_by_speaker()
    test_partial_throttle()
    test_frame_constants_cover_plan()
    print("ALL PASSED")
