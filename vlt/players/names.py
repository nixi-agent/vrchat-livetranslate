"""把 Photon 事件里的玩家记录解析成 `actor → (usr_id, displayName)`。

## 真实结构（P0-b 实测，2026-10）

VRChat 用 **Event 42 / 253 / 255** 广播玩家记录，属性字典在参数 245 里，
actor 号在参数 254（`FE 0D` + u16 LE）：

```
f3 04 2a 02                          event 42，2 个参数
  f5 15                              param 245，type 0x15 = 属性字典
    0d                               count = 13
      07 0e "showSocialRank" 1c      key + value（0x1c 是无负载的布尔变体）
      ...
      07 04 "user"  → 07 02 "id" → 07 28 "usr_xxxx…"
                     → 07 0b "displayName" → 07 15 "伤感身法绝活哥"
  fe 0d 26 05                        param 254 = actor 0x0526
```

- **字符串是 `07 <长度> <UTF-8>`，长度是 LEB128**（不是 2 字节 BE）；
- 加入房间时的 Event 255 里，第 1 个参数（252，type 0x49）是**整张 actor 列表**，
  每个条目形如 `fe 0d <actor> f9 15 <属性字典>` —— 所以一条消息里可能有**多个**
  actor/usr 对；
- 同一玩家换名字/换 avatar 时会再来一条 42。

## 为什么不写"完美"的结构化解析

0x15（字典）里还有若干我们没见过的类型码（0x1c、0x49、0x0a…），硬解析一旦在
某个类型上判断错长度，后面全错位、还**静默**。所以这里用两条路径：

1. **能结构化就结构化**（`parse_records` 里先试 `photon` 的值解析）；
2. **模式兜底**：`usr_` UUID + 其后紧跟的 `displayName` typed string + 最近的
   `fe 0d` actor 标记。这条路已经在真实抓包上验证过（解出 5 个真名，含中文）。

兜底要求"`usr_` 和 `displayName` 同时存在"，所以不会把 `Event 74`（自己的
avatar 库存记录，也含 `usr_`）误认成玩家记录。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

USR_RE = re.compile(
    rb"usr_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
#: 严格校验：VRChat 用户 ID 必须是完整 UUID 形式（`usr_` + 36 位）。
USR_ID_RE = re.compile(
    r"^usr_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ACTOR_MARK = b"\xfe\x0d"
DISPLAY_KEY = b"displayName"
USER_KEY = b"\x07\x04user"      # `user` 键：type=0x07(string) + len=4 + "user"
NAME_EVENT_CODES = (42, 253, 255)     # 带玩家记录的事件
LEAVE_EVENT = 254                     # 离开：要清掉该 actor，防串名
MAX_NAME_BYTES = 128
NAME_SEARCH_WINDOW = 512              # displayName 只在 usr_ 之后这么远内找
USER_FIELD_WINDOW = 400               # `user` 键之后找 id 的窗口
USER_KEY_RE = re.compile(re.escape(USER_KEY))


@dataclass(frozen=True)
class PlayerRecord:
    actor: int
    user_id: str
    display_name: str = ""
    source: str = "event"             # event | log


def _read_leb(data: bytes, off: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if off >= len(data) or shift > 21:
            raise ValueError("LEB128 越界")
        byte = data[off]
        off += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, off
        shift += 7


def actor_markers(payload: bytes) -> list[tuple[int, int]]:
    """所有 `fe 0d <u16>` 标记 → [(偏移, actor)]。值域只收 1..65535。"""
    out: list[tuple[int, int]] = []
    start = 0
    while True:
        pos = payload.find(ACTOR_MARK, start)
        if pos < 0 or pos + 4 > len(payload):
            return out
        start = pos + 1
        actor = int.from_bytes(payload[pos + 2:pos + 4], "little")
        if 1 <= actor < (1 << 16):
            out.append((pos, actor))


def last_actor(payload: bytes) -> int | None:
    """消息尾部那个 actor（Event 42/253 的 param 254）。"""
    markers = actor_markers(payload)
    return markers[-1][1] if markers else None


def _nearest_actor(markers: list[tuple[int, int]], pos: int) -> int | None:
    """离 `pos` 最近的 actor 标记；等距时取**前面**那个。

    为什么要"最近"：Event 255 的 actor 列表是 `actor → 属性`（标记在 usr 前），
    而 Event 42 是 `属性 → actor`（标记在 usr 后）。两种布局都被这一条覆盖。
    """
    best: tuple[int, int] | None = None
    for offset, actor in markers:
        distance = abs(offset - pos)
        if best is None or distance < best[0] or (distance == best[0] and offset <= pos):
            best = (distance, actor)
    return best[1] if best else None


def display_name_after(payload: bytes, start: int) -> str:
    """在 `start` 之后找 `displayName` 并读出它的字符串值。

    编码是 `07 <LEB 长度> <UTF-8>`；`08` 表示 null（隐藏名字），返回空串。
    """
    limit = min(len(payload), start + NAME_SEARCH_WINDOW)
    index = payload.find(DISPLAY_KEY, start, limit)
    if index < 0:
        return ""
    off = index + len(DISPLAY_KEY)
    if off >= len(payload):
        return ""
    ptype = payload[off]
    if ptype == 0x08:                 # null：玩家隐藏了名字
        return ""
    if ptype != 0x07:
        return ""
    try:
        size, off = _read_leb(payload, off + 1)
    except ValueError:
        return ""
    if not (0 < size <= MAX_NAME_BYTES) or off + size > len(payload):
        return ""
    return payload[off:off + size].decode("utf-8", "replace").strip()


def _actor_before(payload: bytes, pos: int, *, window: int = 4096) -> int | None:
    """在 `user` 键之前找「actor 键 + 属性字典」：`0d <u16> 15`。

    Event 253（进房间时的属性表）是 `{0d <actor>: 15 <props>}` 的字典，**没有**
    `fe 0d` 标记 —— 只靠标记会把这份"全员名单"整条丢掉（P0 实测 223KB、26 人）。
    向后找到上一个 `user` 键就停，避免跨条目。
    """
    start = max(0, pos - window)
    prev = payload.rfind(USER_KEY, start, pos)
    if prev >= 0:
        start = prev + len(USER_KEY)
    for j in range(pos - 1, start - 1, -1):
        if (payload[j] == 0x0D and j + 3 < len(payload)
                and payload[j + 3] == 0x15):
            actor = int.from_bytes(payload[j + 1:j + 3], "little")
            if 1 <= actor < (1 << 16):
                return actor
    return None


# ---------------------------------------------------------------- 结构化解析
#
# 2026-10 真机日志：12 条名字事件全部 `记录=0`。原因是**启发式依赖字段顺序**
# （要求 `usr_` 之后才出现 `displayName`），而真实属性表现在是
# `user → {displayName, id, ...}` 这种顺序，而且值类型大量用到
# `0x15`(map)、`0x1c`(无负载)、`0x49`(数组) —— 旧的 regex 与参数解析器都不认识。
#
# 所以这里按 Photon Protocol18 把整条事件**结构化**读出来，按 key 取字段，
# 与顺序无关；正则那条路保留作兜底（老版本 / 畸形包）。


class _Other:
    """「解析成功但内容不关心」的占位（float/bytes/数组/null）。

    必须和 `None`（解析失败）区分开：否则一个 float 字段就会让整张 map 作废
    （而属性表里 float 很常见）。
    """


OTHER = _Other()


class _Txt:
    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


class _Num:
    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value


class _Dic:
    __slots__ = ("items",)

    def __init__(self, items: list[tuple[object, object]]) -> None:
        self.items = items


class _Reader:
    """Protocol18 的只读解析器（节点数 / 深度 / 长度全部有上限）。"""

    MAX_DEPTH = 16
    MAX_NODES = 32768

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset
        self.nodes = 0

    def take(self, length: int) -> bytes | None:
        end = self.offset + length
        if length < 0 or end > len(self.data):
            return None
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def byte(self) -> int | None:
        chunk = self.take(1)
        return chunk[0] if chunk else None

    def varint(self) -> int | None:
        value = 0
        for shift in range(0, 70, 7):
            byte = self.byte()
            if byte is None:
                return None
            if shift == 63 and byte > 1:
                return None
            value |= (byte & 0x7F) << shift
            if byte < 128:
                return value
        return None

    def length(self) -> int | None:
        return self.varint()

    def count(self) -> int | None:
        value = self.length()
        return value if value is not None and value <= 4096 else None

    def descriptor(self, kind: int, depth: int) -> object | None:
        if depth > self.MAX_DEPTH:
            return None
        if kind == 20:                          # 字典描述符：key 类型 + value 类型
            key = self.byte()
            if key is None:
                return None
            key_desc = self.descriptor(key, depth + 1)
            value = self.byte()
            if value is None or key_desc is None:
                return None
            value_desc = self.descriptor(value, depth + 1)
            return (20, key_desc, value_desc)
        return ("plain", kind)

    def dynamic(self, depth: int = 0) -> object | None:
        kind = self.byte()
        if kind is None or kind == 0:
            return None
        desc = self.descriptor(kind, depth)
        if desc is None:
            return None
        return self.value(desc, depth)

    def value(self, desc: object, depth: int = 0) -> object | None:
        self.nodes += 1
        if depth > self.MAX_DEPTH or self.nodes > self.MAX_NODES:
            return None
        if isinstance(desc, tuple) and desc and desc[0] == 20:
            _, key_desc, value_desc = desc
            count = self.count()
            if count is None:
                return None
            items: list[tuple[object, object]] = []
            for _ in range(count):
                key = self.value(key_desc, depth + 1)
                val = self.value(value_desc, depth + 1)
                if key is None or val is None:
                    return None
                items.append((key, val))
            return _Dic(items)

        kind = desc[1] if isinstance(desc, tuple) else int(desc)  # type: ignore[arg-type]
        if kind == 0:
            return self.dynamic(depth + 1)
        if kind == 7:                           # string：LEB128 长度 + UTF-8
            size = self.length()
            if size is None:
                return None
            raw = self.take(size)
            return None if raw is None else _Txt(raw.decode("utf-8", "replace"))
        if kind == 21:                          # map：count 组 (dynamic, dynamic)
            count = self.count()
            if count is None:
                return None
            items = []
            for _ in range(count):
                key = self.dynamic(depth + 1)
                val = self.dynamic(depth + 1)
                if key is None or val is None:
                    return None
                items.append((key, val))
            return _Dic(items)
        if kind in (3, 11, 15, 12, 16):
            raw = self.take(1)
            if raw is None:
                return None
            return _Num(-raw[0] if kind in (12, 16) else raw[0])
        if kind in (4, 13, 17, 14, 18):
            raw = self.take(2)
            if raw is None:
                return None
            word = int.from_bytes(raw, "little")
            return _Num(-word if kind in (14, 18) else word)
        if kind in (9, 10):                     # 压缩整数：zigzag LEB128
            raw = self.varint()
            if raw is None:
                return None
            return _Num((raw >> 1) ^ -(raw & 1))
        if kind == 2:
            return _Num(1) if self.take(1) is not None else None
        if kind == 5:
            return None if self.take(4) is None else OTHER
        if kind == 6:
            return None if self.take(8) is None else OTHER
        if kind in (8, 27, 28):
            return OTHER                        # 无负载
        if 29 <= kind <= 34:
            return _Num(0)                      # 无负载的 0 值
        if kind == 19 or 128 <= kind <= 255:
            if kind == 19 and self.byte() is None:
                return None
            size = self.length()
            if size is None or self.take(size) is None:
                return None
            return OTHER
        if kind in (66, 67):                    # 布尔/位压缩数组
            size = self.length()
            if size is None:
                return None
            need = (size + 7) // 8 if kind == 66 else size
            return None if self.take(need) is None else OTHER
        if kind == 23 or kind in (68, 69, 70, 71, 73, 74, 85):
            count = self.count()
            if count is None:
                return None
            for _ in range(count):
                item = (self.dynamic(depth + 1) if kind == 23
                        else self.value(("plain", kind - 64), depth + 1))
                if item is None:
                    return None
            return OTHER
        return None                             # 未知类型：整条消息作废


EVENT_CODES = (42, 253, 254, 255)


def parse_event(payload: bytes) -> tuple[int, dict[int, object], dict] | None:
    """一条事件消息 → (event, {参数码: 值}, 诊断)。不是事件返回 None。

    事件头是 `F3 04 <event> <count>`；不可靠通道会在前面多 4 字节序号，
    所以在**前 12 字节内**找 `F3 04` 对齐（同语音那条路径）。
    """
    if len(payload) > 1 << 20:
        return None
    at = -1
    for skip in range(0, min(12, max(0, len(payload) - 1))):
        if payload[skip] == 0xF3 and payload[skip + 1] == 0x04:
            at = skip
            break
    if at < 0 or at + 3 > len(payload):
        return None
    event = payload[at + 2]
    reader = _Reader(payload, at + 3)
    count = reader.byte()
    if count is None:
        return None
    params: dict[int, object] = {}
    notes: dict = {"event": event, "params": [], "typed": False, "error": ""}
    for _ in range(count):
        code = reader.byte()
        if code is None:
            break
        value = reader.dynamic()
        if value is None:
            break
        params[code] = value
        notes["params"].append(code)
    notes["typed"] = len(params) == count and reader.offset == len(payload)
    if not notes["typed"]:
        notes["error"] = f"params={len(params)}/{count} off={reader.offset}/{len(payload)}"
    return event, params, notes


def _field(value: object, name: str) -> object | None:
    if not isinstance(value, _Dic):
        return None
    for key, val in value.items:
        if isinstance(key, _Txt) and key.value == name:
            return val
    return None


def _text(value: object) -> str:
    return value.value if isinstance(value, _Txt) else ""


def _int(value: object) -> int | None:
    return value.value if isinstance(value, _Num) and 0 < value.value < (1 << 16) else None


def _clean_name(raw: str) -> str:
    """显示名清洗：长度上限 128、去掉控制字符（含换行，防日志/文件注入）。"""
    text = raw.strip()
    if not text or len(text) > 128:
        return ""
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        return ""
    return text


def _record_from(actor: int, props: object) -> PlayerRecord | None:
    """属性表 → 记录。**只认 `user` 子表**（avatar 的 authorId 也是 usr_ 形式）。"""
    user = _field(props, "user")
    if user is None:
        return None
    name = _clean_name(_text(_field(user, "displayName")))
    raw_id = _text(_field(user, "id")).lower()
    user_id = raw_id if USR_ID_RE.match(raw_id) else ""
    if not name and not user_id:
        return None
    return PlayerRecord(actor=actor, user_id=user_id, display_name=name,
                        source="event")


def _records_typed(payload: bytes) -> tuple[list[PlayerRecord], dict]:
    parsed = parse_event(payload)
    if parsed is None:
        return [], {"typed": False, "error": "不是事件消息"}
    event, params, notes = parsed
    if event == 254:
        return [], notes
    props = params.get(245 if event == 42 else 251 if event == 253 else 249)
    if props is None:
        return [], notes
    out: list[PlayerRecord] = []
    # ① 单条记录：属性表里有 `user`
    actor = _int(params.get(254))
    if actor is not None:
        record = _record_from(actor, props)
        if record is not None:
            out.append(record)
    # ② 快照：属性表本身是「actor → 属性」
    if isinstance(props, _Dic):
        for key, val in props.items:
            other = _int(key)
            if other is None:
                continue
            record = _record_from(other, val)
            if record is not None and record not in out:
                out.append(record)
        notes["fields"] = [k.value for k, _v in props.items if isinstance(k, _Txt)][:12]
    return out, notes


def parse_records_ex(payload: bytes) -> tuple[list[PlayerRecord], dict]:
    """结构化解析优先，启发式兜底。返回 (记录, 诊断)（诊断只含字段名，不含人名）。"""
    typed, notes = _records_typed(payload)
    if typed:
        return typed, notes
    heuristic = _parse_records_heuristic(payload)
    if heuristic:
        notes["fallback"] = "heuristic"
    return heuristic, notes


def _parse_records_heuristic(payload: bytes) -> list[PlayerRecord]:
    markers = actor_markers(payload)
    out: dict[tuple[int, str], PlayerRecord] = {}
    # 策略 B：Event 253 的"actor → 属性字典"布局（**没有** fe 0d 标记）。
    # 每个 `user` 键对应一条记录：往前找 actor 键，往后找 id / displayName。
    # ⚠️ 只在"没有标记"时才用 —— 42/255 那种带标记的消息里也有 `user` 键，
    #    用"往前找 0d ?? ?? 15"会撞上字典的计数字节，可能绑错 actor。
    for match in (USER_KEY_RE.finditer(payload) if not markers else ()):
        actor = _actor_before(payload, match.start())
        if actor is None:
            continue
        usr = USR_RE.search(payload, match.end(), match.end() + USER_FIELD_WINDOW)
        if usr is None:
            continue
        user_id = usr.group().decode("ascii", "replace").lower()
        name = display_name_after(payload, match.end())
        out[(actor, user_id)] = PlayerRecord(actor=actor, user_id=user_id,
                                            display_name=name)

    for match in USR_RE.finditer(payload):
        user_id = match.group().decode("ascii", "replace").lower()
        if any(key[1] == user_id for key in out):
            continue                       # 策略 B 已经收录（它带 actor 键）
        actor = _nearest_actor(markers, match.start())
        if actor is None:
            continue
        # 必须同时有 displayName 键：Event 74（自己的 avatar 库存）也带 usr_，
        # 但没有 displayName，不筛掉就会把别人的名字记到自己头上。
        window_end = min(len(payload), match.end() + NAME_SEARCH_WINDOW)
        if payload.find(DISPLAY_KEY, match.end(), window_end) < 0:
            continue
        name = display_name_after(payload, match.end())
        out[(actor, user_id)] = PlayerRecord(actor=actor, user_id=user_id,
                                            display_name=name)
    return list(out.values())



def parse_records(payload: bytes) -> list[PlayerRecord]:
    """从一条事件消息里解出所有 (actor, usr_id, displayName)。"""
    return parse_records_ex(payload)[0]


def parse_leave(payload: bytes) -> int | None:
    """离开事件（254）的 actor：先按类型解析，再退回 `fe 0d` 标记。"""
    parsed = parse_event(payload)
    if parsed is not None and parsed[0] == 254:
        actor = _int(parsed[1].get(254))
        if actor is not None:
            return actor
    return last_actor(payload)


class NameTable:
    """actor → 玩家记录。actor 号会被复用，所以离开时**必须**清掉。

    `version` 是单调递增的变更计数：调用方（分玩家腿）每次要判断「名字表变了没」
    时比一个整数就够了，不必每个包复制一份 dict。
    """

    def __init__(self, log_names: dict[str, str] | None = None) -> None:
        self._by_actor: dict[int, PlayerRecord] = {}
        self._log_names = dict(log_names or {})
        self.version = 0

    # ------------------------------------------------------------ 更新

    def feed(self, payload: bytes, *, code: int,
             records: list[PlayerRecord] | None = None) -> list[PlayerRecord]:
        """喂一条事件消息。返回**发生变化**的记录（给日志/界面用）。

        `records` 是调用方已经解析好的结果（采集器要拿诊断信息，避免重复解析）。
        """
        if code == LEAVE_EVENT:
            actor = parse_leave(payload)
            if actor is not None:
                self.forget(actor)
            return []
        if code not in NAME_EVENT_CODES:
            return []
        changed: list[PlayerRecord] = []
        for record in (records if records is not None else parse_records(payload)):
            if not record.display_name:
                # 记录里没有名字（隐藏/字段缺失）→ 用 output_log 的兜底
                record = PlayerRecord(actor=record.actor, user_id=record.user_id,
                                      display_name=self._log_names.get(record.user_id, ""),
                                      source="log" if record.user_id in self._log_names
                                      else "event")
            old = self._by_actor.get(record.actor)
            if old != record:
                self._by_actor[record.actor] = record
                self.version += 1
                changed.append(record)
        return changed

    def forget(self, actor: int) -> None:
        if self._by_actor.pop(actor, None) is not None:
            self.version += 1

    def copy(self) -> "NameTable":
        """一份独立副本（预热线程与翻译腿各自持有，互不影响）。"""
        clone = NameTable(dict(self._log_names))
        clone._by_actor = dict(self._by_actor)
        clone.version = self.version
        return clone

    def clear(self) -> None:
        """换世界时整表清空：actor 号只对「这个房间的这次加入」有意义。

        清表而不是留着旧记录，是因为 actor 会被复用 —— 留着必然串名。
        """
        if self._by_actor:
            self._by_actor.clear()
            self.version += 1

    # ------------------------------------------------------------ 查询

    def get(self, actor: int) -> PlayerRecord | None:
        return self._by_actor.get(actor)

    def label(self, actor: int) -> str:
        """给人看的短标签：真名 → usr_ 前 8 位 → actor<N>。"""
        record = self._by_actor.get(actor)
        if record is None:
            return f"actor{actor}"
        if record.display_name:
            return record.display_name
        return f"usr_{record.user_id[4:12]}" if record.user_id else f"actor{actor}"

    def snapshot(self) -> dict[int, PlayerRecord]:
        return dict(self._by_actor)

    def __len__(self) -> int:
        return len(self._by_actor)


LOG_JOIN_RE = re.compile(r"OnPlayerJoined\s+(.+?)\s+\((usr_[0-9a-fA-F-]{36})\)")


def parse_output_log(path: str | Path) -> dict[str, str]:
    """从 VRChat 的 `output_log_*.txt` 里解析 `usr_ → displayName`（兜底用）。

    这条**不提供 actor 号**，只能补"事件里只有 id、没有名字"的情况。
    """
    names: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return names
    for match in LOG_JOIN_RE.finditer(text):
        name, user_id = match.group(1).strip(), match.group(2).lower()
        if name and user_id:
            names[user_id] = name
    return names


def latest_output_log(directory: str | Path) -> Path | None:
    """按修改时间取最新一份 output_log（VRChat 每次启动新建一个文件）。"""
    folder = Path(directory)
    try:
        logs = [p for p in folder.glob("output_log_*.txt") if p.is_file()]
    except OSError:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


#: VRChat 的日志目录候选（Steam AppID 438100）。第一条是 Windows 原生位置
#: （`%USERPROFILE%\AppData\LocalLow\VRChat\VRChat`，Unity 写的后台日志就在这儿），
#: 其后覆盖 Linux 原生与 Proton，最后是 Steam 装在别处 / Flatpak 的情况。取**第一
#: 个存在的**，都不存在时返回第一个候选（好让状态栏能说出「我去哪儿找过」）。
LOG_DIR_CANDIDATES = (
    "~/AppData/LocalLow/VRChat/VRChat",
    "~/.local/share/VRChat/VRChat",
    "~/.steam/steam/steamapps/compatdata/438100/pfx/drive_c/users/steamuser"
    "/AppData/LocalLow/VRChat/VRChat",
    "~/.local/share/Steam/steamapps/compatdata/438100/pfx/drive_c/users/steamuser"
    "/AppData/LocalLow/VRChat/VRChat",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/438100"
    "/pfx/drive_c/users/steamuser/AppData/LocalLow/VRChat/VRChat",
)


def default_log_dir() -> Path:
    """VRChat 的 output_log 目录（自动挑一个存在的）。"""
    candidates = [Path(p).expanduser() for p in LOG_DIR_CANDIDATES]
    for path in candidates:
        try:
            if path.is_dir():
                return path
        except OSError:
            continue
    return candidates[0]


_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, *, limit: int = 40) -> str:
    """把显示名变成安全的文件名片段（中文/emoji 保留，路径分隔与控制字符替换）。"""
    cleaned = _BAD_FILENAME.sub("_", (name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip("._")
    return cleaned
