"""玩家位置与距离（来自 Photon **Event 12** 的根节点坐标）。

## 实测结构（P0-b 标定，2026-10）

Event 12（玩家移动/IK）的参数 245 是一个 byte[]，body 布局：

```
body[0:4]   u32le  服务器时间（用于插值）
body[4]     flags  低半字节 == 0x07 ⇔ 本条**带位置块**
body[6:18]  f32le  x, y, z   ← 世界坐标（根节点/脚下，不是头显）
body[18:]   变长   IK/骨骼姿态（8-bit 量化在这里，**不含位置**）
```

判据 `body[4] & 0x0F == 0x07` 在真实抓包上验证过：**5243/5243 条**都解出合理坐标，
不带标志的 4569 条里只有 7 条数值假阳性（可忽略）。

## 精度与覆盖（别把这两点忘了）

- 与 Event 14 的 float 视点对比：静止时 ~0.3 m 常数偏置（根 vs 眼高），
  快速走动最坏 ~1 m（25 m 衰减曲线上约 1 dB）；
- **位置是按需下发的**：移动中的玩家 70~100% 的消息带位置，静坐者只有 ~2%。
  所以必须缓存「最后一次已知位置」，并接受它可能过期（默认 30s）。

## 本机位置从哪来

我们自己也在发 Event 12（方向 = 出向），用的是同一套字段。所以不需要知道
"我是哪个 actor"，只要把出向的位置存成 `self` 即可。
"""
from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass

from . import photon

EVENT_POSITION = 12
POS_FLAG_MASK = 0x0F
POS_FLAG_VALUE = 0x07
POS_OFFSET = 6
POS_MIN_BODY = 18
MAX_COORD = 1e5
DEFAULT_MAX_AGE_S = 30.0


@dataclass(frozen=True)
class Position:
    x: float
    y: float
    z: float
    ts: float

    def distance_to(self, other: "Position") -> float:
        return math.dist((self.x, self.y, self.z), (other.x, other.y, other.z))


def parse_body(body: bytes) -> tuple[float, float, float] | None:
    """直接给 **Event 12 的 byte[] body**，检查位置块并解出坐标。"""
    if len(body) < POS_MIN_BODY:
        return None
    if body[4] & POS_FLAG_MASK != POS_FLAG_VALUE:
        return None
    x, y, z = struct.unpack_from("<fff", body, POS_OFFSET)
    if not all(math.isfinite(v) for v in (x, y, z)):
        return None
    if max(abs(x), abs(y), abs(z)) > MAX_COORD:
        return None
    if x == 0.0 and y == 0.0 and z == 0.0:
        # 全 0 = 还没初始化（实测出现在刚进入世界的第一条）
        return None
    return x, y, z


def parse_position(payload: bytes, *, ts: float | None = None) -> Position | None:
    """从一条 Event 12 的**消息 payload**里取根节点坐标；没有位置块返回 None。

    不依赖参数表：直接找 `F5 43`（参数 245 + byte[]）后读 LEB128 长度 —— 与
    `photon.scan_voice_body` 同一套思路（类型表一旦错位，结构化解析就全废）。
    """
    marker = payload.find(b"\xf5\x43")
    if marker < 0:
        return None
    off = marker + 2
    size = 0
    shift = 0
    while off < len(payload):
        byte = payload[off]
        off += 1
        size |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
        if shift > 21:
            return None
    if size < POS_MIN_BODY or off + size > len(payload):
        return None
    coords = parse_body(payload[off:off + size])
    if coords is None:
        return None
    x, y, z = coords
    return Position(x=x, y=y, z=z, ts=ts if ts is not None else time.time())


class PositionTable:
    """actor → 最后已知位置，外加本机位置。过期数据按「未知」处理。

    **线程安全**：预热器（写）和翻译腿的 collector（读写）、GUI 状态栏（读）
    会从不同线程碰同一张表，所以内部所有读写都过一把 RLock。表很小、操作都是
    O(1)，锁的开销可以忽略。
    """

    def __init__(self, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        self.max_age_s = max_age_s
        self._by_actor: dict[int, Position] = {}
        self._self: Position | None = None
        self.updates = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 写入

    def observe_body(self, actor: int, body: bytes, *, ts: float | None = None,
                     is_self: bool = False) -> Position | None:
        """直接给 Event 12 的 body（出向 RaiseEvent 里拿到的是这个）。"""
        coords = parse_body(body)
        if coords is None:
            return None
        return self._store(actor, coords, ts=ts, is_self=is_self)

    def _store(self, actor: int, coords, *, ts, is_self: bool) -> Position:
        position = Position(x=coords[0], y=coords[1], z=coords[2],
                            ts=ts if ts is not None else time.time())
        with self._lock:
            self.updates += 1
            if is_self:
                self._self = position
            else:
                self._by_actor[actor] = position
        return position

    def observe(self, actor: int, payload: bytes, *, ts: float | None = None,
                is_self: bool = False) -> Position | None:
        position = parse_position(payload, ts=ts)
        if position is None:
            return None
        return self._store(actor, (position.x, position.y, position.z),
                           ts=position.ts, is_self=is_self)

    def forget(self, actor: int) -> None:
        with self._lock:
            self._by_actor.pop(actor, None)

    def clear_actors(self) -> None:
        """换世界时清掉所有**别人**的位置；自己的位置跟着会话走，保留。"""
        with self._lock:
            self._by_actor.clear()

    # ------------------------------------------------------------ 查询

    def get(self, actor: int, *, now: float | None = None) -> Position | None:
        with self._lock:
            position = self._by_actor.get(actor)
        if position is None:
            return None
        now = now if now is not None else time.time()
        if now - position.ts > self.max_age_s:
            return None
        return position

    @property
    def self_position(self) -> Position | None:
        with self._lock:
            return self._self

    def distance(self, actor: int, *, now: float | None = None) -> float | None:
        """到本机的 3D 距离；要么没本机位置、要么该玩家位置过期 → None。"""
        origin = self.self_position
        if origin is None:
            return None
        position = self.get(actor, now=now)
        if position is None:
            return None
        return origin.distance_to(position)

    def snapshot(self) -> dict[int, Position]:
        with self._lock:
            return dict(self._by_actor)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_actor)


def format_distance(meters: float | None) -> str:
    """给字幕/日志用的短标签：`8.3m` / `<1m` / 未知返回空串。"""
    if meters is None:
        return ""
    if meters < 1.0:
        return "<1m"
    if meters < 100.0:
        return f"{meters:.1f}m"
    return f"{meters:.0f}m"


def is_position_message(kind: int, code: int) -> bool:
    return kind == photon.TYPE_EVENT and code == EVENT_POSITION
