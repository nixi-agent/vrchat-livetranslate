"""房间中继的数据模型：配置、成员、消息、连接态。

风格对齐 `vlt/session/base.py`：dataclass + 完整类型注解 + 中文 docstring。
`RoomConfig.from_dict()` 是全仓库「脏配置不许打死程序」这条铁律的一部分 ——
用户在 `config.yaml` 里写错类型、写负数、写半个字段，都必须**留痕 + 回落默认**，
绝不抛异常（抛了就是整个同传起不来，代价远大于一行警告）。
"""
from __future__ import annotations

import getpass
import ipaddress
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from .protocol import ROOM_CODE_LEN, is_valid_room_code, normalize_room_code


def log(msg: str) -> None:
    """房间链路统一日志：行首 `HH:MM:SS.mmm` 时间戳 + `[room]` 前缀。

    时间戳自己加而不依赖 `crashlog` 的 Tee：Tee 只在 GUI 启动路径上装，
    无头脚本 / 单测里没装，没戳就没法回答「两次重连隔了多久」。
    """
    stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{stamp} [room] {msg}", flush=True)


def _warn(key: str, raw: Any, why: str, default: Any) -> Any:
    """脏值留痕并回落默认（对齐 `vlt/config.py` 的 `[config] ⚠️ ...` 口径）。"""
    log(f"⚠️ room.{key}={raw!r} 非法（{why}）→ 回落默认值 {default!r}")
    return default


def _as_bool(key: str, raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on", "是"):
            return True
        if low in ("0", "false", "no", "off", "否", ""):
            return False
    if isinstance(raw, (int, float)):
        return bool(raw)
    return _warn(key, raw, "不是布尔值", default)


def _as_str(key: str, raw: Any, default: str) -> str:
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (int, float, bool)):
        return _warn(key, raw, "不是字符串", default)
    return _warn(key, raw, f"类型是 {type(raw).__name__}", default)


def _as_float(key: str, raw: Any, default: float, lo: float, hi: float) -> float:
    """转 float 并夹在 [lo, hi]；越界/脏值都算非法（留痕 + 回落默认）。"""
    return _num(key, raw, default, lo, hi, as_int=False)


def _as_int(key: str, raw: Any, default: int, lo: int, hi: int) -> int:
    """转 int 并夹在 [lo, hi]；小数也算非法（`max_peers: 3.5` 是写错了，不是取整）。"""
    return int(_num(key, raw, default, lo, hi, as_int=True))


def _num(key: str, raw: Any, default: Any, lo: float, hi: float, *, as_int: bool) -> Any:
    """数字字段归一内核。`default` 原样带进告警文案，int 字段就显示 int，别显示成 8.0。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return _warn(key, raw, "不是数字", default)
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return _warn(key, raw, "解析不成数字", default)
    if val != val or val in (float("inf"), float("-inf")):      # NaN / inf
        return _warn(key, raw, "不是有限数", default)
    if val < lo or val > hi:
        return _warn(key, raw, f"超出允许范围 [{lo:g}, {hi:g}]", default)
    if as_int and not val.is_integer():
        return _warn(key, raw, "必须是整数", default)
    return int(val) if as_int else val


# ------------------------------------------------------------------ 默认值（唯一真相）

DEFAULT_BACKOFF: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0)
DEFAULT_HEARTBEAT_S = 20.0
DEFAULT_MAX_PEERS = 8
DEFAULT_PARTIAL_MIN_MS = 200
DEFAULT_SOURCE_LANG = "zh"
NICK_MAX_CHARS = 16


class ConnectionState(str, Enum):
    """房间连接态（继承 `str` 是为了能直接塞进 GUI 状态栏 / 日志格式化）。"""

    IDLE = "idle"                    # 还没 start()
    CONNECTING = "connecting"        # 首次连接中
    ONLINE = "online"                # 已收到 welcome，能收发
    RECONNECTING = "reconnecting"    # 掉线后退避重连中
    STOPPED = "stopped"              # 主动 stop()
    ERROR = "error"                  # 致命错误（鉴权失败 / 房间满），不再重连


@dataclass(frozen=True)
class Peer:
    """房间里的一个成员。

    id     : 服务端分配的 `p_xxxxxx`
    nick   : 显示用昵称
    online : 服务端说它在不在线（掉线的成员会保留一会儿，重连后 id 可能变）
    """

    id: str
    nick: str = ""
    online: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "Peer | None":
        """从 `members` 帧里的一项构造；脏数据返回 `None`（调用方跳过）。"""
        if not isinstance(raw, dict):
            return None
        pid = raw.get("id")
        if not isinstance(pid, str) or not pid:
            return None
        nick = raw.get("nick")
        return cls(id=pid,
                   nick=nick.strip() if isinstance(nick, str) else "",
                   online=bool(raw.get("online", True)))

    @property
    def display(self) -> str:
        """手腕屏上显示谁在说：有昵称用昵称，没有就退到成员 id。"""
        return self.nick or self.id


@dataclass
class RoomConfig:
    """房间链路的全部可配置项（对应 `config.yaml` 的 `room:` 段）。

    `enabled` 默认 False：房间链路是新增能力，默认关才不会影响现有单机用户
    （方案 §11「与现有单机链路互相影响」那条风险的处置）。
    """

    enabled: bool = False
    server_url: str = ""             # 例 wss://room.example.com/ws
    room_code: str = ""              # 8 位 Crockford Base32；空 = 没进任何房间
    token: str = ""                  # 进房令牌（服务端只存哈希）
    nickname: str = ""               # 空 = 取系统用户名
    broadcast_source: bool = True    # 广播本机 ASR 源文
    show_remote: bool = True         # 远端条目上手腕屏
    max_peers: int = DEFAULT_MAX_PEERS
    reconnect_backoff: tuple[float, ...] = DEFAULT_BACKOFF
    heartbeat_s: float = DEFAULT_HEARTBEAT_S       # 0 = 不发心跳
    source_lang: str = DEFAULT_SOURCE_LANG         # 本期恒 zh（显式声明，不做自动检测）
    partial_min_ms: int = DEFAULT_PARTIAL_MIN_MS   # partial 节流间隔；0 = 不限流

    # ------------------------------------------------------------ 构造与序列化

    @classmethod
    def from_dict(cls, raw: Any) -> "RoomConfig":
        """从 YAML 解出来的 dict 构造，**任何脏输入都不许抛异常**。

        缺字段 → 用默认；类型错 / 越界 → 打一行 `[room] ⚠️ ...` 再回落默认。
        `raw` 整个不是 dict（用户把 `room:` 写成了一行字符串）也一样处理。
        """
        base = cls()
        if raw is None:
            return base
        if not isinstance(raw, dict):
            _warn("<整段>", raw, f"room 段必须是键值对，收到 {type(raw).__name__}", "全部默认值")
            return base

        d: dict[str, Any] = raw
        enabled = _as_bool("enabled", d.get("enabled", base.enabled), base.enabled)
        server_url = _as_str("server_url", d.get("server_url", base.server_url), base.server_url)
        token = _as_str("token", d.get("token", base.token), base.token)

        code = _as_str("room_code", d.get("room_code", base.room_code), base.room_code)
        if code:
            norm = normalize_room_code(code)
            if not is_valid_room_code(norm):
                _warn("room_code", code,
                      f"不是 {ROOM_CODE_LEN} 位 Crockford Base32（字符集去掉了 I/L/O/U）", "")
                code = ""
            else:
                code = norm

        nick = _as_str("nickname", d.get("nickname", base.nickname), base.nickname)
        if len(nick) > NICK_MAX_CHARS:
            log(f"⚠️ room.nickname 超过 {NICK_MAX_CHARS} 字符 → 截断为 {nick[:NICK_MAX_CHARS]!r}")
            nick = nick[:NICK_MAX_CHARS]

        backoff = _as_backoff(d.get("reconnect_backoff", base.reconnect_backoff))
        return cls(
            enabled=enabled,
            server_url=server_url,
            room_code=code,
            token=token,
            nickname=nick,
            broadcast_source=_as_bool("broadcast_source",
                                      d.get("broadcast_source", base.broadcast_source),
                                      base.broadcast_source),
            show_remote=_as_bool("show_remote", d.get("show_remote", base.show_remote),
                                 base.show_remote),
            max_peers=_as_int("max_peers", d.get("max_peers", base.max_peers),
                              DEFAULT_MAX_PEERS, 1, DEFAULT_MAX_PEERS),
            reconnect_backoff=backoff,
            heartbeat_s=_as_float("heartbeat_s", d.get("heartbeat_s", base.heartbeat_s),
                                  DEFAULT_HEARTBEAT_S, 0.0, 3600.0),
            source_lang=_as_str("source_lang", d.get("source_lang", base.source_lang),
                                base.source_lang) or DEFAULT_SOURCE_LANG,
            partial_min_ms=_as_int("partial_min_ms",
                                   d.get("partial_min_ms", base.partial_min_ms),
                                   DEFAULT_PARTIAL_MIN_MS, 0, 5000),
        )

    def to_dict(self) -> dict[str, Any]:
        """写回 YAML 用的纯 dict（tuple → list，其余原样）。"""
        d: dict[str, Any] = {}
        for key, val in self.__dict__.items():
            d[key] = list(val) if isinstance(val, tuple) else val
        return d

    def with_overrides(self, **kwargs: Any) -> "RoomConfig":
        """复制一份并覆盖若干字段（GUI 上改房间码时用，不改原对象）。"""
        return replace(self, **kwargs)

    # ------------------------------------------------------------ 派生属性

    @property
    def effective_nickname(self) -> str:
        """实际用的昵称：配置里留空就取系统用户名（方案 §12 的推荐口径）。"""
        if self.nickname:
            return self.nickname
        for getter in (getpass.getuser, lambda: os.environ.get("USERNAME", "")):
            try:
                name = (getter() or "").strip()
            except Exception:                       # noqa: BLE001  取用户名失败不算事
                name = ""
            if name:
                return name[:NICK_MAX_CHARS]
        return "玩家"

    def backoff_delay(self, attempt: int) -> float:
        """第 `attempt` 次重连（从 0 数）之前等多久；超出退避表就停在最后一档。"""
        table = self.reconnect_backoff or DEFAULT_BACKOFF
        idx = max(0, min(int(attempt), len(table) - 1))
        return float(table[idx])

    def unavailable_reason(self) -> str | None:
        """房间链路不可用的原因；`None` = 可以连。

        这里必须给出**人能读懂**的原因：连不上又不说为什么，是 VLT 反复踩过的坑
        （方案 §6.1「WS 连接不能静默失败」）。
        """
        url = self.server_url.strip()
        if not url:
            return "没填 server_url（config.yaml 的 room.server_url）"
        if not (url.startswith("wss://") or url.startswith("ws://")):
            return "server_url 不是 ws:// 或 wss:// 开头"
        try:
            url.encode('utf-8')
            parsed = urlsplit(url)
            host = parsed.hostname
            if not host or parsed.username or parsed.password:
                return "server_url 必须包含主机名，不能包含登录凭据"
            if parsed.scheme == 'ws':
                local = host.lower() == 'localhost'
                try:
                    local = local or ipaddress.ip_address(host).is_loopback
                except ValueError:
                    pass
                if not local:
                    return "远程房间必须使用 wss://；ws:// 仅限本机 loopback"
        except ValueError:
            return "server_url 格式无效"
        if not self.room_code:
            return "没填 room_code（8 位房间码，两端必须一致）"
        return None


def _as_backoff(raw: Any) -> tuple[float, ...]:
    """退避表归一：只收「非负数字的序列」，脏项丢掉，全脏就回落默认。"""
    if raw is None:
        return DEFAULT_BACKOFF
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = [raw]                                 # 写成单个数字也算一份退避表
    if isinstance(raw, str):
        raw = [p for p in raw.replace("，", ",").split(",") if p.strip()]
    if not isinstance(raw, (list, tuple)):
        _warn("reconnect_backoff", raw, "不是列表", DEFAULT_BACKOFF)
        return DEFAULT_BACKOFF
    out: list[float] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        try:
            val = float(str(item).strip())
        except (TypeError, ValueError):
            log(f"⚠️ room.reconnect_backoff 里的 {item!r} 不是数字 → 丢掉这一档")
            continue
        if val != val or val < 0 or val > 3600:
            log(f"⚠️ room.reconnect_backoff 里的 {item!r} 超出 [0, 3600] → 丢掉这一档")
            continue
        out.append(val)
    if not out:
        _warn("reconnect_backoff", raw, "没有一档可用", DEFAULT_BACKOFF)
        return DEFAULT_BACKOFF
    return tuple(out)


# ------------------------------------------------------------------ 运行态


@dataclass(frozen=True)
class RoomMessage:
    """远端成员的一句话（交给上层进手腕屏 / 将来的翻译与 TTS 口子）。

    text 是**全量快照**（覆盖式，不是增量）；`is_final=True` 表示这句已定稿。
    `translated` 本期恒为空串：同语言房间用不上翻译，字段先留着，
    将来接 `Translator` 时不用改渲染签名（方案 §3 的注释）。
    """

    peer_id: str
    nick: str
    utt: str
    seq: int
    text: str
    lang: str = DEFAULT_SOURCE_LANG
    is_final: bool = False
    srv_ts: int = 0
    translated: str = ""

    @property
    def display(self) -> str:
        """给用户看的文本：有译文用译文，没有就用原文（本期=原文）。"""
        return (self.translated or self.text).strip()


@dataclass
class RoomState:
    """`RoomClient.state()` 返回的**快照**（值拷贝，调用方拿着不会被后台线程改）。

    peers 用 tuple 而不是 list：快照交给 GUI 线程读，可变容器会被后台线程改出竞态。
    """

    conn: ConnectionState = ConnectionState.IDLE
    room_code: str = ""
    me: str = ""
    nickname: str = ""
    peers: tuple[Peer, ...] = field(default_factory=tuple)
    last_error: str = ""
    sent_frames: int = 0
    recv_frames: int = 0
    send_fails: int = 0
    reconnects: int = 0
    pending_replay: int = 0        # 还没被 ack、重连后要补发的句子数

    @property
    def is_online(self) -> bool:
        return self.conn is ConnectionState.ONLINE

    @property
    def peer_count(self) -> int:
        """在线人数（含自己）—— GUI 状态栏显示的就是这个数。"""
        return sum(1 for p in self.peers if p.online)

    @property
    def summary(self) -> str:
        """一行状态文案（GUI 状态栏 / 日志共用，避免出现两份口径）。"""
        label = {
            ConnectionState.IDLE: "未连接",
            ConnectionState.CONNECTING: "连接中",
            ConnectionState.ONLINE: f"已连接 · {self.peer_count} 人",
            ConnectionState.RECONNECTING: f"重连中（第 {self.reconnects} 次）",
            ConnectionState.STOPPED: "已停止",
            ConnectionState.ERROR: f"错误：{self.last_error}" if self.last_error else "错误",
        }[self.conn]
        return f"房间 {self.room_code or '—'} · {label}"
