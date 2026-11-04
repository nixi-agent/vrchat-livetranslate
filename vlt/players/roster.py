"""房间名单：以 VRChat 自己的 output_log 为准，回答「现在这间房里到底有谁」。

## 为什么不能用 Photon 名字表当名单

`vlt/players/names.py` 的 `NameTable` 是**事件表**：只有我们看得到流量、且
对方的名字事件（Event 255/253）真的到达时，表里才会有他。它有两个先天缺口：

1. **换房间不清空**：上一间房的人不会自己消失（我们只在绑新 host 时才 `clear()`
   一次，而 UU 加速下 host 不变），右栏就一直挂着上一间房的人；
2. **错过就永远缺**：抓包比进房晚、碎片丢了、Event 255 那次没监听，那个人就
   永远不会出现。

## 日志里有什么（本机实测，2026-11-04）

VRChat 每次进房间都会往 `output_log_*.txt` 追加：

```
2026.11.04 11:38:02 Debug      -  [Behaviour] Entering Room: ORBITAL GENESIS V4․0․0
2026.11.04 11:38:14 Debug      -  [Behaviour] OnPlayerJoined EMeowSystem (usr_3a6252c9-88e9-4062-a5a4-7a10aa0517dc)
2026.11.04 11:59:27 Debug      -  [Behaviour] OnPlayerLeft EMeowSystem (usr_3a6252c9-88e9-4062-a5a4-7a10aa0517dc)
```

换房间时的顺序是「先一串 `OnPlayerLeft`，再一条 `Entering Room`」，所以在
`Entering Room` 处整表清空是安全的（不会误清新房间的人）。这条口径与 VRCX
（`Dotnet/LogWatcher.cs` 的 `ParseLogOnPlayerJoinedOrLeft`）一致。

## 与 `NameTable` 的分工

- 本模块出**行**（谁在房间里、叫什么）：它是权威来源，不在名单里的人一律不显示；
- `NameTable` 出**列**（actor 号、名字事件的实时性）：用来把 `user_id` 映射到
  Photon 的 actor，从而接上距离、说话状态、翻译会话。

两边都用 `user_id` 对齐（日志里老格式可能没有 `usr_…`，那就退化成按名字对齐）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import names

#: 换房间 / 离开房间（整份日志里唯一会「重置名单」的两条）
ROOM_MARK = "[Behaviour] Entering Room: "
LEFT_ROOM_MARK = "[Behaviour] OnLeftRoom"
JOIN_MARK = "[Behaviour] OnPlayerJoined"
LEFT_MARK = "[Behaviour] OnPlayerLeft"

#: `名字 (usr_…)`：显示名可能含空格、中文、`․`（U+2024）甚至括号，
#: 所以**从行尾**锚定 user id，前面的整段才是名字。
_USER_SUFFIX = re.compile(r"^(?P<name>.*?)\s*\((?P<uid>(?i:usr_)[0-9a-fA-F-]{36})\)\s*$")


def parse_user_info(text: str) -> tuple[str, str]:
    """把 `名字 (usr_…)` 拆成 `(名字, user_id)`；老格式没有 id 时返回 `(名字, "")`。"""
    body = (text or "").strip()
    if not body:
        return "", ""
    match = _USER_SUFFIX.match(body)
    if match:
        return match.group("name").strip(), match.group("uid").lower()
    return body, ""


@dataclass(frozen=True)
class Member:
    """房间里的一个人。"""

    user_id: str                  # 没学到时是空串（老日志格式）
    name: str

    @property
    def key(self) -> str:
        """同一个人只占一行：有 user_id 用 id，没有就按名字（大小写不敏感）。"""
        return self.user_id or "name:" + self.name.casefold()


class Roster:
    """当前房间的成员表；`observe_line()` 喂日志行，`members()` 出行。

    设计成**无锁**的：调用方（预热器）自己在 `_roster_lock` 里用；这样这里不必
    为一次 `members()` 拷贝再套一层锁。
    """

    def __init__(self) -> None:
        self._order: list[str] = []
        self._by_key: dict[str, Member] = {}
        self.world = ""
        self.version = 0
        #: 日志里读到过任何事件（含「进房间」）。置位之后名单就是权威的，即使为空
        #: 也代表「这个房间里确实没人」，不能再退回按名字表凑人。
        self.synced = False
        #: 统计（日志里报出来，便于判断到底是日志没跟上还是解析不对）
        self.rooms = 0
        self.joins = 0
        self.lefts = 0

    # ---------------------------------------------------------------- 变更

    def set_world(self, world: str) -> None:
        """进新房间：整表清空（顺序见模块文档：leave 先到、这条后到）。"""
        self._order.clear()
        self._by_key.clear()
        self.world = (world or "").strip()
        self.rooms += 1
        self.version += 1

    def add(self, name: str, user_id: str = "") -> bool:
        member = Member(user_id or "", (name or "").strip())
        if not member.name and not member.user_id:
            return False
        key = member.key
        if key in self._by_key:
            # 同一人重复 join（换 avatar / 重连）只更新名字，不动顺序
            if self._by_key[key].name == member.name:
                return False
            self._by_key[key] = member
        else:
            self._by_key[key] = member
            self._order.append(key)
        self.joins += 1
        self.version += 1
        return True

    def remove(self, name: str = "", user_id: str = "") -> bool:
        """有人离开。优先按 user_id 认，其次按名字（老格式 / 名字事件还没来）。"""
        keys: list[str] = []
        if user_id:
            keys.append(user_id)
        if name:
            keys.append("name:" + name.casefold())
        for key in keys:
            if key in self._by_key:
                self._remove_key(key)
                return True
        # 兜底：这次日志没带 usr_，但我们表里那个人是带 id 存的 —— 按名字找一遍
        if name:
            wanted = name.casefold()
            for key, member in list(self._by_key.items()):
                if member.name.casefold() == wanted:
                    self._remove_key(key)
                    return True
        return False

    def _remove_key(self, key: str) -> None:
        self._by_key.pop(key, None)
        try:
            self._order.remove(key)
        except ValueError:
            pass
        self.lefts += 1
        self.version += 1

    def clear(self) -> None:
        """离开世界（`OnLeftRoom`）：人没了，世界名也留着给状态页看。"""
        self._order.clear()
        self._by_key.clear()
        self.version += 1

    # ---------------------------------------------------------------- 查询

    def members(self) -> list[Member]:
        return [self._by_key[key] for key in self._order if key in self._by_key]

    def contains(self, user_id: str) -> bool:
        return bool(user_id) and user_id.lower() in self._by_key

    def name_of(self, user_id: str) -> str:
        member = self._by_key.get((user_id or "").lower())
        return member.name if member else ""

    def __len__(self) -> int:
        return len(self._by_key)

    # ---------------------------------------------------------------- 解析

    def observe_line(self, line: str) -> str:
        """喂一行日志，返回事件类型：`world`/`join`/`left`/`leave`/`""`（无关行）。"""
        event = self._observe(line)
        if event:
            self.synced = True
        return event

    def _observe(self, line: str) -> str:
        if not line:
            return ""
        if LEFT_ROOM_MARK in line:
            self.clear()
            return "leave"
        if ROOM_MARK in line:
            cut = line.rfind(ROOM_MARK)
            self.set_world(line[cut + len(ROOM_MARK):])
            return "world"
        if JOIN_MARK in line:
            # `OnPlayerJoined:Unnamed`（老版本的无名事件）不是进房间
            if "] OnPlayerJoined:" in line:
                return ""
            cut = line.rfind(JOIN_MARK)
            name, user_id = parse_user_info(line[cut + len(JOIN_MARK):])
            return "join" if self.add(name, user_id) else ""
        if LEFT_MARK in line:
            # `OnPlayerLeftRoom` / `OnPlayerLeft:` 都不是「某人离开」
            if "] OnPlayerLeftRoom" in line or "] OnPlayerLeft:" in line:
                return ""
            cut = line.rfind(LEFT_MARK)
            name, user_id = parse_user_info(line[cut + len(LEFT_MARK):])
            return "left" if self.remove(name, user_id) else ""
        return ""


class LogTail:
    """跟着 VRChat 的 output_log 增量读行。

    每份日志是**追加写**的，VRChat 每次启动新建一个文件；换文件或文件变短
    （被截断/轮转）时从头重读一遍 —— 重放历史正好能把「当前名单」算出来，
    所以不需要额外保存状态，程序重启也能立即得到正确名单。
    """

    #: 一次性最多读这么多字节（防止有人把几 MB 日志塞进来时卡住调用线程；
    #: 正常一份日志 0.5–1.5 MB，首轮整读没问题）
    MAX_READ = 8 << 20

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = Path(directory).expanduser() if directory else None
        self._path: Path | None = None
        self._offset = 0
        self._partial = ""
        #: 已经读进来的总行数（诊断用）
        self.lines = 0

    @property
    def directory(self) -> Path:
        return self._dir if self._dir is not None else names.default_log_dir()

    @property
    def path(self) -> Path | None:
        return self._path

    def poll(self) -> list[str]:
        """返回**新增的完整行**；没有新内容时返回空列表。"""
        folder = self.directory
        path = names.latest_output_log(folder)
        if path is None:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if path != self._path or size < self._offset:
            # 新文件 / 被截断：从头重放（Roster 会被 `Entering Room` 重置）
            self._path = path
            self._offset = 0
            self._partial = ""
        if size <= self._offset:
            return []
        want = min(size - self._offset, self.MAX_READ)
        try:
            with path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(want)
        except OSError:
            return []
        if not chunk:
            return []
        self._offset += len(chunk)
        text = self._partial + chunk.decode("utf-8", "replace")
        parts = text.split("\n")
        self._partial = parts.pop()               # 末尾可能是半行，留到下次
        self.lines += len(parts)
        # VRChat 的日志是 CRLF：行尾的 `\r` 要剥掉，否则世界名/玩家名会带一个回车
        return [line.rstrip("\r") for line in parts]
