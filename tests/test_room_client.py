"""`RoomClient` 验收：用**本机 in-process 假中继**跑真 WebSocket，全程离线。

## 为什么必须自己写一个假中继

房间客户端的价值全在「连接会断」这件事上：重连退避、补发策略、成员表变动、
`stop()` 之后线程真的死掉。这些用 mock 对象测等于没测 —— 只有真的有一条会断的
TCP 连接才逼得出来。所以这里用 `websockets.serve` 在 127.0.0.1 的随机端口上起一个
和 `server/`（Cloudflare DO）同一套协议的假中继；验收命令带死代理跑也必须全绿。

## 逐条断言的铁律

* `stop()` 之后断言 **`not thread.is_alive()`**（先抓住线程对象），不是断言什么
  `running` 属性 —— 属性可能还挂着 True 而线程早死了，那种测试是假的。
* 调用方**永远拿不到 WS 对象**：重连会换对象，拿着旧引用会静默失效。
* 重连后**只补 final + 每个未完成句的最新 partial**，绝不重放全部 partial。
* 任何失败/降级都要在日志 + `on_status` 里留痕，禁静默。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import websockets                                                       # noqa: E402
from websockets.exceptions import ConnectionClosed                      # noqa: E402

from vlt.room.client import RoomClient                                  # noqa: E402
from vlt.room.model import ConnectionState, RoomConfig, RoomMessage     # noqa: E402
from vlt.room.protocol import (FRAME_ACK, FRAME_ERR, FRAME_FINAL, FRAME_HELLO,   # noqa: E402
                               FRAME_MEMBERS, FRAME_PING, FRAME_PONG, FRAME_SEG,
                               FRAME_WELCOME, ProtocolError, decode_frame,
                               encode_frame, new_peer_id, now_ms)

TEST_ROOM = "TEST1234"
TEXT_KINDS = (FRAME_SEG, FRAME_FINAL)


def wait_until(pred: Callable[[], bool], what: "str | Callable[[], str]",
               timeout: float = 10.0) -> None:
    """轮询等条件成立；超时就把「在等什么」说清楚（超时最难查的就是不知道卡在哪）。

    `what` 可以传**零参 callable**：那样它只在超时那一刻求值，报出来的是失败现场
    （实到几帧、连接数、客户端状态），而不是刚开始等待时的旧状态。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    detail = what() if callable(what) else what
    raise AssertionError(f"等了 {timeout}s 仍未满足：{detail}")


# ================================================================ 本机假中继


class RelayConn:
    """假中继上的一条连接。收发的帧都记下来，用来断言补发/ack/成员广播。"""

    def __init__(self, index: int, ws: Any) -> None:
        self.index = index
        self.ws = ws
        self.peer_id: str | None = None
        self.nick = ""
        self.hello: dict[str, Any] = {}
        self._frames: list[dict[str, Any]] = []      # 这条连接**发来**的帧
        self._sent: list[dict[str, Any]] = []        # 中继**发给**这条连接的帧
        self._rejected: list[dict[str, Any]] = []    # 被限速丢掉的帧（_frames 的子集）
        self._lock = threading.Lock()

    def record(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._frames.append(frame)

    def record_sent(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._sent.append(frame)

    def record_rejected(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._rejected.append(frame)

    def frames(self, kind: str | None = None) -> list[dict[str, Any]]:
        """这条连接**发来**的帧（`kind` 非空则只挑那种）。返回拷贝，读时不会被改。"""
        return self._pick(self._frames, kind)

    def sent_frames(self, kind: str | None = None) -> list[dict[str, Any]]:
        """中继**发给**这条连接的帧（ack / members / 扇出的 seg 都在这里）。"""
        return self._pick(self._sent, kind)

    def rejected(self, kind: str | None = None) -> list[dict[str, Any]]:
        """被限速丢掉的帧。收到的帧里既有放过的也有丢的，光看收到数断言不出限速生效。"""
        return self._pick(self._rejected, kind)

    def _pick(self, table: list[dict[str, Any]], kind: str | None) -> list[dict[str, Any]]:
        with self._lock:
            snap = list(table)
        return snap if kind is None else [f for f in snap if f.get("t") == kind]


class FakeRelay:
    """本机 in-process 假中继：和 `server/src/room.js` 同一套协议（少了 Hibernation）。

    刻意保持"傻"，和方案 §4 的服务端纪律一致，这样客户端测过的行为在真服务端上也成立：
    收到即扇出（不缓冲、不排序）、只盖 `srv_ts`、只对 `final` 回 `ack`、成员变动全员广播、
    鉴权在握手阶段做完、成员上限、每连接限速。
    """

    def __init__(self, *, host: str = "127.0.0.1", room_code: str = TEST_ROOM,
                 token: str = "", max_peers: int = 8, rate_limit: int = 0,
                 ack_finals: bool = True) -> None:
        self.host = host
        self.room_code = room_code
        self.token = token
        self.max_peers = max_peers
        self.rate_limit = rate_limit          # 每连接每秒帧数上限；0 = 不限速
        self.ack_finals = ack_finals
        self.silence = False                  # True = 装死：收下帧但什么都不回（测心跳超时）
        self.url = ""
        self.failed_hellos: list[str] = []
        self.paths: list[str] = []              # 每条连接握手时的请求路径（含查询串）
        self._port = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop_async: asyncio.Event | None = None
        self._live: list[RelayConn] = []      # 当前在线
        self._all: list[RelayConn] = []       # 历史上所有连接（重连测试要拿最新那条）
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期

    def start(self, timeout: float = 15.0) -> "FakeRelay":
        self._thread = threading.Thread(target=self._main, name="fake-relay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"假中继 {timeout}s 内没起来（端口没绑上）")
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """收干净：关所有连接 → 关监听 → 等线程死（别留僵尸线程/僵尸端口）。"""
        th = self._thread
        if th is None:
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError, asyncio.TimeoutError):
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout)
        th.join(timeout)
        if th.is_alive():
            print(f"  ⚠️ 假中继线程 {timeout}s 没退干净（daemon，进程退出会带走）", flush=True)
        self._thread = None
        self._loop = None

    def _main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:                       # noqa: BLE001
            print(f"  ⚠️ 假中继线程异常：{type(exc).__name__}: {exc}", flush=True)
            self._ready.set()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_async = asyncio.Event()
        async with websockets.serve(self._handler, self.host, self._port) as server:
            self._port = server.sockets[0].getsockname()[1]
            self.url = f"ws://{self.host}:{self._port}/ws"
            self._ready.set()
            await self._stop_async.wait()

    async def _shutdown(self) -> None:
        await self._close_all(reason="relay shutdown")
        if self._stop_async is not None:
            self._stop_async.set()

    # ------------------------------------------------------------ 测试控制面

    @property
    def connections(self) -> int:
        """历史上接受过的连接总数（客户端重连一次就 +1）。"""
        with self._lock:
            return len(self._all)

    @property
    def live(self) -> int:
        with self._lock:
            return len(self._live)

    def conn(self, index: int = -1) -> RelayConn:
        with self._lock:
            return self._all[index]

    def conn_by_nick(self, nick: str) -> RelayConn:
        """按昵称找**最新**那条连接（重连后要拿新连接上收到的帧来断言补发）。"""
        with self._lock:
            matches = [c for c in self._all if c.hello.get("nick") == nick]
        assert matches, f"没有昵称为 {nick!r} 的连接（现有：" \
                        f"{[c.hello.get('nick') for c in self._all]}）"
        return matches[-1]

    def all_frames(self, kind: str | None = None) -> list[dict[str, Any]]:
        """所有连接**发来**的帧。"""
        out: list[dict[str, Any]] = []
        with self._lock:
            conns = list(self._all)
        for c in conns:
            out.extend(c.frames(kind))
        return out

    def all_sent(self, kind: str | None = None) -> list[dict[str, Any]]:
        """中继**发出去**的所有帧（ack / members / 扇出的 seg 都在里面）。"""
        out: list[dict[str, Any]] = []
        with self._lock:
            conns = list(self._all)
        for c in conns:
            out.extend(c.sent_frames(kind))
        return out

    @staticmethod
    async def _send(conn: RelayConn, frame: dict[str, Any]) -> None:
        """发一帧给某条连接，同时记下来（测试要断言服务端发了什么、发给谁）。"""
        conn.record_sent(frame)
        with contextlib.suppress(ConnectionClosed, OSError):
            await conn.ws.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))

    def run(self, coro: Any, timeout: float = 5.0) -> Any:
        """（测试线程调用）在中继的事件循环里跑一个协程并等结果。"""
        loop = self._loop
        assert loop is not None and not loop.is_closed(), "假中继没起来"
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def kill_all(self) -> int:
        """★ 掐断所有在线连接（模拟服务端重启 / 网络抖动），用来逼出重连与补发。"""
        return self.run(self._close_all(reason="kill"))

    def kill_conn(self, nick: str) -> int:
        """★ 只掐断某个人的连接，其他人**保持在线**。

        测「A 重连后补发」时必须用这个：`kill_all()` 会把 B 也掐了，A 可能在 B 还没
        重连回来时就把补发帧发出去 —— 那时扇出对象是空的，B 永远收不到，测试偶发超时。
        """
        async def _do() -> int:
            with self._lock:
                target = next((c for c in self._live if c.nick == nick), None)
            if target is None:
                return 0
            with contextlib.suppress(Exception):
                await target.ws.close(code=1001, reason="kill")
            return 1

        return self.run(_do())

    def send_to_last(self, raw: str) -> None:
        """往最新那条连接塞一段**原文**（用来测未知帧类型这种编不出来的帧）。"""
        async def _do() -> None:
            with self._lock:
                target = self._all[-1] if self._all else None
            assert target is not None, "还没有任何连接，没法注入"
            await target.ws.send(raw)

        self.run(_do())

    def send_frame_to_last(self, kind: str, **fields: Any) -> None:
        """往最新那条连接塞一个合法帧（用来测 `err` 的处理）。"""
        self.send_to_last(encode_frame(kind, **fields))

    async def _close_all(self, reason: str = "") -> int:
        with self._lock:
            conns, self._live = list(self._live), []
        for c in conns:
            with contextlib.suppress(Exception):
                await c.ws.close(code=1001, reason=reason)
        return len(conns)

    # ------------------------------------------------------------ 协议实现

    def _new_conn(self, ws: Any) -> RelayConn:
        # 握手路径（含查询串）。真 Worker 要在进 DO 之前用查询串里的 room 做路由，
        # 所以客户端必须把房间码放进 URL —— 只写在 hello 帧里会直接吃 HTTP 400。
        path = str(getattr(getattr(ws, "request", None), "path", "") or "")
        with self._lock:
            conn = RelayConn(len(self._all), ws)
            self._all.append(conn)
            self._live.append(conn)
            self.paths.append(path)
        return conn

    async def _handler(self, ws: Any) -> None:
        conn = self._new_conn(ws)
        try:
            raw = await asyncio.wait_for(ws.recv(), 10.0)
            try:
                hello = decode_frame(raw)
            except ProtocolError as exc:
                self.failed_hellos.append(f"bad_frame: {exc}")
                await self._send(conn, {"t": FRAME_ERR, "code": "bad_frame", "msg": str(exc)})
                await ws.close(code=1002, reason="bad hello")
                return
            conn.hello = hello
            reject = self._check_hello(hello, ws.request.headers.get('Authorization', ''))
            if reject is not None:
                code, msg = reject
                self.failed_hellos.append(f"{code}: {msg}")
                await self._send(conn, {"t": FRAME_ERR, "code": code, "msg": msg})
                await ws.close(code=1008, reason=code)
                return
            conn.peer_id = new_peer_id()
            conn.nick = str(hello.get("nick") or "")
            conn.record(hello)
            await self._send(conn, {"t": FRAME_WELCOME, "me": conn.peer_id, "srv_ts": now_ms()})
            await self._broadcast_members()
            await self._pump(conn, ws)
        except (ConnectionClosed, asyncio.TimeoutError, OSError):
            pass
        except Exception as exc:                       # noqa: BLE001
            print(f"  ⚠️ 假中继处理连接异常：{type(exc).__name__}: {exc}", flush=True)
        finally:
            with self._lock:
                if conn in self._live:
                    self._live.remove(conn)
            if conn.peer_id is not None:
                await self._broadcast_members()

    def _check_hello(self, hello: dict[str, Any], authorization: str) -> tuple[str, str] | None:
        """鉴权（真服务端放在 Worker 层）：帧型 / 房间码 / 令牌 / 人数上限。"""
        if hello.get("t") != FRAME_HELLO:
            return "bad_frame", f"首帧必须是 hello，收到 {hello.get('t')!r}"
        if str(hello.get("room") or "") != self.room_code:
            return "bad_room", f"房间码不对：{hello.get('room')!r}"
        received = authorization[7:] if authorization.startswith('Bearer ') else ''
        if authorization.startswith('VLT '):
            received = base64.urlsafe_b64decode(authorization[4:]).decode('utf-8')
        if self.token and received != self.token:
            return "auth", "进房令牌不对"
        with self._lock:
            # 只数已经拿到成员 id 的：正在握手的这条连接不算人头
            seated = sum(1 for c in self._live if c.peer_id is not None)
        if seated >= self.max_peers:
            return "room_full", f"房间已满（上限 {self.max_peers} 人）"
        return None

    async def _pump(self, conn: RelayConn, ws: Any) -> None:
        bucket_start = time.monotonic()
        bucket_count = 0
        async for raw in ws:
            try:
                frame = decode_frame(raw)
            except ProtocolError as exc:
                await self._send(conn, {"t": FRAME_ERR, "code": "bad_frame", "msg": str(exc)})
                continue
            conn.record(frame)
            if self.silence:                      # 装死：收下但一个字都不回（测心跳超时）
                continue
            kind = frame.get("t")
            if self.rate_limit > 0 and kind in (*TEXT_KINDS, FRAME_PING):
                moment = time.monotonic()
                if moment - bucket_start >= 1.0:
                    bucket_start, bucket_count = moment, 0
                bucket_count += 1
                if bucket_count > self.rate_limit:
                    conn.record_rejected(frame)
                    await self._send(conn, {"t": FRAME_ERR, "code": "rate",
                                            "msg": f"超过 {self.rate_limit} 帧/秒"})
                    continue
            if kind == FRAME_PING:
                await self._send(conn, {"t": FRAME_PONG, "ts": now_ms()})
            elif kind in TEXT_KINDS:
                await self._fanout(conn, frame)
            # 其余帧（welcome/ack/…）收下就行，不处理

    async def _fanout(self, conn: RelayConn, frame: dict[str, Any]) -> None:
        """即收即转：盖上 `src`（谁说的）与 `srv_ts`（时钟基准），扇给房间里其他人。"""
        out = dict(frame)
        out["src"] = conn.peer_id
        out["srv_ts"] = now_ms()
        is_final = out.get("t") == FRAME_FINAL or bool(out.get("final"))
        with self._lock:
            others = [c for c in self._live if c is not conn and c.peer_id is not None]
        for other in others:
            await self._send(other, out)
        if is_final and self.ack_finals:
            await self._send(conn, {"t": FRAME_ACK, "utt": out.get("utt"),
                                    "seq": out.get("seq"), "ts": now_ms()})

    async def _broadcast_members(self) -> None:
        """成员变动全员广播（方案 §4：这事由服务端负责，客户端只消费）。"""
        with self._lock:
            targets = [c for c in self._live if c.peer_id is not None]
            members = [{"id": c.peer_id, "nick": c.nick, "online": True} for c in targets]
        frame = {"t": FRAME_MEMBERS, "members": members, "srv_ts": now_ms()}
        for c in targets:
            await self._send(c, frame)


# ================================================================ 测试脚手架


class Recorder:
    """收集 `on_message` / `on_status`（回调在房间线程上跑，所以要加锁）。"""

    def __init__(self) -> None:
        self.msgs: list[RoomMessage] = []
        self.statuses: list[str] = []
        self._lock = threading.Lock()

    def on_message(self, msg: RoomMessage) -> None:
        with self._lock:
            self.msgs.append(msg)

    def on_status(self, text: str) -> None:
        with self._lock:
            self.statuses.append(text)

    def texts(self) -> list[str]:
        with self._lock:
            return [m.text for m in self.msgs]

    def finals(self) -> list[RoomMessage]:
        with self._lock:
            return [m for m in self.msgs if m.is_final]

    def final_map(self) -> dict[str, str]:
        with self._lock:
            return {m.utt: m.text for m in self.msgs if m.is_final}

    def status(self) -> str:
        with self._lock:
            return "\n".join(self.statuses)


def make_config(relay: FakeRelay, nick: str, **over: Any) -> RoomConfig:
    """测试用房间配置：退避压到 50ms、心跳关掉、partial 不限流（要的是确定性）。"""
    base: dict[str, Any] = dict(server_url=relay.url, room_code=TEST_ROOM, nickname=nick,
                                reconnect_backoff=[0.05, 0.1], heartbeat_s=0,
                                partial_min_ms=0)
    base.update(over)
    return RoomConfig.from_dict(base)


@contextlib.contextmanager
def relay_ctx(**kw: Any):
    r = FakeRelay(**kw).start()
    try:
        yield r
    finally:
        r.stop()


@contextlib.contextmanager
def client_ctx(relay: FakeRelay, nick: str, rec: Recorder | None = None, **cfg: Any):
    rec = rec or Recorder()
    client = RoomClient(make_config(relay, nick, **cfg), rec.on_message, rec.on_status)
    client.start()
    try:
        yield client, rec
    finally:
        client.stop(timeout=5.0)


def wait_online(client: RoomClient, what: str = "客户端上线") -> None:
    wait_until(lambda: client.state().is_online, what)


def wait_both_ready(r: FakeRelay, a: RoomClient, b: RoomClient) -> None:
    """等两端都上线且互相看得见（成员广播到达）—— 大多数用例的共同前提。"""
    wait_online(a, "A 上线")
    wait_online(b, "B 上线")
    wait_until(lambda: a.state().peer_count == 2 and b.state().peer_count == 2,
               "两端都该看到 2 人")


# ================================================================ 测试用例


def test_public_surface_never_leaks_ws() -> None:
    """★ 契约：公开接口只有 5 个，**没有任何一个能拿到 WS 对象**。

    重连会换 WS 对象，调用方拿着旧引用会静默失效（文本还在往死连接上灌）。
    """
    rec = Recorder()
    client = RoomClient(RoomConfig(server_url="ws://127.0.0.1:1/ws", room_code=TEST_ROOM),
                        rec.on_message, rec.on_status)
    public = {n for n in dir(client) if not n.startswith("_")}
    assert public == {"cfg", "publish", "start", "state", "stop"}, \
        f"公开接口变了（新增/改名都要过这条）：{sorted(public)}"
    for name in ("ws", "websocket", "conn", "connection", "socket", "get_ws"):
        assert not hasattr(client, name), f"暴露了 {name}（重连会换对象，调用方会拿到死引用）"
    assert not any(f in ("ws", "websocket", "socket") for f in RoomMessage.__dataclass_fields__)
    assert not any(f in ("ws", "websocket", "socket") for f in client.state().__dataclass_fields__)
    client.stop(timeout=1.0)
    print(f"  公开接口只有 {sorted(public)}，拿不到 WS 对象 OK")


def test_missing_config_is_not_silent() -> None:
    """★ 配置不齐时不许静默：`on_status` 留一条人能读懂的原因，且**不启后台线程**。"""
    cases = [
        (RoomConfig(), "server_url"),
        (RoomConfig(server_url="https://x/ws"), "ws://"),
        (RoomConfig(server_url="wss://x/ws"), "room_code"),
    ]
    for cfg, keyword in cases:
        rec = Recorder()
        client = RoomClient(cfg, rec.on_message, rec.on_status)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            client.start()
            assert client.state().conn is ConnectionState.ERROR, f"{cfg} 没进 ERROR 态"
            assert client._thread is None, "配置不齐时不该启后台线程"
            # ★ 2026-09-28 主控补：光看状态回调不够 —— GUI 轮询的是 state() 快照，
            #   拒绝启动时 last_error 为空的话，界面上只会显示「错误」两个字，看不到原因。
            assert keyword in client.state().last_error, (
                f"state().last_error 里没有可读原因（应提到 {keyword!r}）："
                f"{client.state().last_error!r}（summary={client.state().summary!r}）")
            client.stop(timeout=1.0)
        status = rec.status()
        assert keyword in status, f"状态回调里没有可读原因（应提到 {keyword!r}）：{status!r}"
        assert "[room]" in buf.getvalue(), f"日志里也得留一条：{buf.getvalue()!r}"
    print(f"  缺配置 → 状态回调 + 日志各留可读原因、不启线程 OK（{len(cases)} 例）")


def test_connect_and_welcome() -> None:
    """连上假中继 → 发 hello → 收 welcome → 状态 ONLINE，`me` 是服务端分配的 id。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (client, rec):
            wait_online(client)
            st = client.state()
            assert st.conn is ConnectionState.ONLINE
            assert st.room_code == TEST_ROOM and st.nickname == "小明"
            assert st.me.startswith("p_") and len(st.me) == 8, f"成员 id 格式不对：{st.me!r}"
            assert st.sent_frames >= 1 and st.recv_frames >= 1
            assert st.reconnects == 0 and st.send_fails == 0 and st.last_error == ""
            hello = r.conn(0).hello
            assert hello["t"] == FRAME_HELLO and hello["room"] == TEST_ROOM
            assert hello["nick"] == "小明" and hello["ts"] > 0, hello
            assert "tok" not in hello, "没令牌时不该在帧里留个空 tok"
            assert "已进房" in rec.status(), f"进房成功该给一条状态：{rec.status()!r}"
            assert client._thread is not None and client._thread.is_alive()
            assert st.peer_count == 1, f"应看到自己 1 人：{st.peers}"
            summary = st.summary
    assert "TEST1234" in summary and "1 人" in summary, summary
    print(f"  连接 + welcome OK（我的 id={st.me}，状态文案「{summary}」）")


def test_members_broadcast() -> None:
    """★ 成员变动由服务端广播：B 进房 → A 看到 2 人；B 走了 → A 回到 1 人。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, _ra):
            wait_online(a)
            wait_until(lambda: a.state().peer_count == 1, "A 应先看到自己 1 人")
            rec_b = Recorder()
            b = RoomClient(make_config(r, "阿华"), rec_b.on_message, rec_b.on_status)
            b.start()
            try:
                wait_both_ready(r, a, b)
                nicks = {p.display for p in a.state().peers}
                assert nicks == {"小明", "阿华"}, f"成员表昵称不对：{nicks}"
                assert b.state().me != a.state().me, "两个人拿到了同一个成员 id"
                assert all(p.online for p in a.state().peers)
                assert r.conn_by_nick("阿华").hello["nick"] == "阿华"
            finally:
                b.stop(timeout=5.0)
            wait_until(lambda: a.state().peer_count == 1, "B 走后 A 应回到 1 人")
            assert {p.display for p in a.state().peers} == {"小明"}
            assert a.state().is_online, "B 离房不该把 A 也弄掉线"
    print("  成员表广播（进房 2 人 / 离房 1 人）OK")


def test_send_receive_consistency() -> None:
    """★ A 发一串 partial + 一个 final，B 收到的**最终文本与 A 一致**，且覆盖不叠字。"""
    snaps = ["我", "我在", "我在测", "我在测试", "我在测试翻译功能"]
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, rec_a):
            with client_ctx(r, "阿华") as (b, rec_b):
                wait_both_ready(r, a, b)
                for i, snap in enumerate(snaps, start=1):
                    a.publish("u1", i, snap, is_final=(i == len(snaps)))
                wait_until(lambda: bool(rec_b.finals()), "B 应收到 final")
                got = rec_b.texts()
                assert got == snaps, f"B 收到的文本序列不对（覆盖式，不该叠字）：{got}"
                last = rec_b.msgs[-1]
                assert last.text == snaps[-1], f"★ 最终文本不一致：{last.text!r} != {snaps[-1]!r}"
                assert last.is_final and last.utt == "u1" and last.seq == len(snaps)
                assert last.lang == "zh", f"源语言应显式声明 zh：{last.lang!r}"
                assert last.nick == "小明", f"说话人昵称不对：{last.nick!r}"
                assert last.peer_id == a.state().me, "说话人 id 对不上"
                assert last.srv_ts > 0, "服务端没盖 srv_ts（时钟基准丢了）"
                assert last.display == last.text, "本期没翻译，display 应等于原文"
                assert rec_a.texts() == [], f"A 收到了自己发的回音：{rec_a.texts()}"
                # 服务端只对 final 回 ack，且 ack 之后回放表清空
                wait_until(lambda: bool(r.all_sent(FRAME_ACK)), "服务端应对 final 回 ack")
                assert len(r.all_sent(FRAME_ACK)) == 1, \
                    f"partial 不该被 ack：{r.all_sent(FRAME_ACK)}"
                wait_until(lambda: a.state().pending_replay == 0,
                           "final 被 ack 后应从回放表移除")
                # 扇给 B 的帧必须带 src（谁说的），且不回环给 A
                fanned = r.conn_by_nick("阿华").sent_frames()
                fanned = [f for f in fanned if f.get("t") in TEXT_KINDS]
                assert len(fanned) == len(snaps), f"B 应收到 {len(snaps)} 帧，实际 {len(fanned)}"
                assert all(f["src"] == a.state().me for f in fanned), "扇出帧缺 src"
                assert all(f["srv_ts"] > 0 for f in fanned), "扇出帧没盖 srv_ts"
                echoed = [f for f in r.conn_by_nick("小明").sent_frames()
                          if f.get("t") in TEXT_KINDS]
                assert echoed == [], f"★ 帧回环给了发送者本人：{echoed}"
    print(f"  收发一致 OK（{len(snaps)} 帧快照 → 最终文本 {last.text!r}，1 个 ack）")


def test_ack_only_for_final() -> None:
    """partial 不回 ack → 留在回放表等补发；final 回 ack → 从回放表移除。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, _ra):
            with client_ctx(r, "阿华") as (b, rec_b):
                wait_both_ready(r, a, b)
                for i in range(1, 4):
                    a.publish(f"u{i}", 1, f"第{i}句半成品", is_final=False)
                wait_until(lambda: rec_b.texts().count("第1句半成品") == 1, "B 应收到 partial")
                assert a.state().pending_replay == 3, \
                    f"partial 不该被 ack 掉（应留在回放表），实际 {a.state().pending_replay}"
                assert not r.all_sent(FRAME_ACK), "partial 不该有 ack"
                a.publish("u9", 1, "这句定稿了", is_final=True)
                wait_until(lambda: a.state().pending_replay == 3,
                           "final 被 ack 后回放表应少一条")
                assert rec_b.final_map().get("u9") == "这句定稿了"
    print("  只对 final 回 ack OK（3 条 partial 仍待补发，final 已 ack）")


def test_publish_is_thread_safe() -> None:
    """★ 多线程同时 `publish()`：一帧不丢、一句不串、调用线程不卡、不抛异常。"""
    threads, per_thread = 8, 25
    total = threads * per_thread
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, _ra):
            with client_ctx(r, "阿华") as (b, rec_b):
                wait_both_ready(r, a, b)
                errors: list[str] = []
                barrier = threading.Barrier(threads)

                def worker(idx: int) -> None:
                    try:
                        barrier.wait(5.0)                     # 一起冲，把并发压到最大
                        for n in range(per_thread):
                            a.publish(f"t{idx}", n + 1, f"线程{idx}第{n + 1}拍",
                                      is_final=(n == per_thread - 1))
                    except Exception as exc:                  # noqa: BLE001
                        errors.append(f"线程{idx}: {type(exc).__name__}: {exc}")

                workers = [threading.Thread(target=worker, args=(i,), daemon=True)
                           for i in range(threads)]
                for t in workers:
                    t.start()
                for t in workers:
                    t.join(15.0)
                    assert not t.is_alive(), "★ publish 卡住了调用线程（采集线程会被拖死）"
                assert not errors, f"publish 在并发下抛了异常：{errors}"

                wait_until(lambda: a.state().sent_frames >= total,
                           f"应发出 {total} 帧，实际 {a.state().sent_frames}", timeout=20.0)
                assert a.state().send_fails == 0, f"有发送失败：{a.state().send_fails}"
                wait_until(lambda: len(rec_b.final_map()) == threads,
                           f"B 应收到 {threads} 个 final，实际 {len(rec_b.final_map())}",
                           timeout=20.0)
                finals = rec_b.final_map()
                for idx in range(threads):
                    want = f"线程{idx}第{per_thread}拍"
                    assert finals.get(f"t{idx}") == want, \
                        f"★ 并发下串句了：t{idx} 收到 {finals.get(f't{idx}')!r}，应为 {want!r}"
                # 中继侧也要收到正好这么多文本帧（一帧不丢）
                relay_texts = [f for f in r.conn_by_nick("小明").frames() if f.get("t") in TEXT_KINDS]
                assert len(relay_texts) == total, \
                    f"★ 中继只收到 {len(relay_texts)} 帧，应 {total} 帧（并发下丢帧了）"
                assert len({(f["utt"], f["seq"]) for f in relay_texts}) == total, "出现重复帧"
    print(f"  publish 线程安全 OK（{threads} 线程 × {per_thread} 帧 = {total} 帧，零丢失零串句）")


def test_publish_before_connect_is_not_lost() -> None:
    """★ 还没 `start()` 就 publish（开机竞态）→ 帧留在回放表，连上后自动补发。"""
    with relay_ctx(ack_finals=False) as r:
        rec = Recorder()
        client = RoomClient(make_config(r, "小明"), rec.on_message, rec.on_status)
        buf = io.StringIO()
        # 「未连接丢帧」只在第 1 次和每 50 次留痕（防刷屏），所以痕迹要在**第一次** publish 抓
        with contextlib.redirect_stdout(buf):
            client.publish("u0", 1, "开机前先说一句", is_final=True)
        offline_trace = buf.getvalue()
        assert "未连接" in offline_trace, f"未连接时发帧必须留痕：{offline_trace!r}"
        assert "补发" in offline_trace, f"痕迹要说清数据没丢：{offline_trace!r}"
        assert client.state().conn is ConnectionState.IDLE, "没 start() 就该是 IDLE"
        assert client.state().pending_replay == 1, "★ 没连上就把帧扔了（final 必达被破坏）"
        client.publish("u1", 1, "还有一句", is_final=True)
        client.start()
        try:
            wait_online(client)
            wait_until(lambda: len(r.conn_by_nick("小明").frames(FRAME_FINAL)) >= 2,
                       "连上后应把两条 final 都补发出去")
            finals = {f["utt"]: f for f in r.conn_by_nick("小明").frames(FRAME_FINAL)}
            assert finals["u0"]["text"] == "开机前先说一句" and finals["u0"]["seq"] == 1
            assert finals["u1"]["text"] == "还有一句"
        finally:
            client.stop(timeout=5.0)
    print("  未连接时 publish 不丢，连上后补发 OK（2 条 final）")


def test_reconnect_replays_only_final_and_latest_partial() -> None:
    """★ 重连后**只补 final + 每个未完成句的最新 partial**，绝不重放全部 partial。

    重放全部 partial 等于把房间里其他人的屏幕倒带重演一遍，还会瞬间撞上服务端限速。
    """
    with relay_ctx(ack_finals=False) as r:      # 不回 ack → final 留在回放表，才测得到补发
        with client_ctx(r, "小明") as (a, _ra):
            with client_ctx(r, "阿华") as (b, rec_b):
                wait_both_ready(r, a, b)
                a.publish("u1", 1, "我", False)
                a.publish("u1", 2, "我在", False)
                a.publish("u1", 3, "我在测试", False)        # u1 一直没定稿 → 未完成句
                a.publish("u2", 1, "另一句", False)
                a.publish("u2", 2, "另一句定稿了", True)      # u2 定稿但没被 ack

                # ⚠️ 别用 `r.conn(0)`：两个 `client_ctx` 是**并发连接**，假中继的连接下标 =
                #    accept 顺序 —— 慢 runner 上「阿华」可能先连上，`conn(0)` 就不是小明、
                #    其 frames() 恒为 0，用例偶发超时（CI 实测踩到过）。按昵称取连接才稳。
                conn_a = r.conn_by_nick("小明")

                def _arrived() -> int:
                    return (len(conn_a.frames(FRAME_SEG)) +
                            len(conn_a.frames(FRAME_FINAL)))

                def _why() -> str:
                    texts = [f.get("text") for f in conn_a.frames() if f.get("t") in TEXT_KINDS]
                    st = a.state()
                    return (f"断线前 5 帧应已到达中继（小明 conn 实到 {_arrived()} 帧：{texts}；"
                            f"中继连接总数 {r.connections}；A {st.summary}；"
                            f"A 已发 {st.sent_frames} 帧 / 发送失败 {st.send_fails}；"
                            f"A 最后错误 {st.last_error!r}；回放表 {st.pending_replay} 条）")

                wait_until(lambda: _arrived() == 5, _why)
                assert a.state().pending_replay == 2, \
                    f"回放表应留 2 句（u1 未完成 + u2 未 ack 的 final），" \
                    f"实际 {a.state().pending_replay}"
                before = r.connections

                # ★ 只掐 A：B 全程在线，A 的补发帧才一定有人接。
                #   两条一起掐的话，A 可能在 B 还没重连回来时就补发完了 → 扇出对象为空。
                killed = r.kill_conn("小明")
                assert killed == 1, f"应掐断 A 那一条，实际 {killed}"
                wait_until(lambda: r.connections == before + 1,
                           f"A 该重连回来（{before} → {r.connections}）", timeout=20.0)
                wait_online(a, "A 重连后应重新上线")
                assert b.state().is_online, f"B 不该掉线（只掐了 A）：{b.state().summary}"
                assert a.state().reconnects >= 1, f"重连计数没动：{a.state().reconnects}"

                new_a = r.conn_by_nick("小明")
                assert new_a.index >= before, f"拿到的不是新连接：{new_a.index} < {before}"
                wait_until(lambda: len(new_a.frames(FRAME_SEG)) +
                           len(new_a.frames(FRAME_FINAL)) >= 2,
                           "A 的新连接上应看到补发帧")
                time.sleep(0.4)               # 再等一下，确认没有多余的补发跟过来
                replayed = [f for f in new_a.frames() if f.get("t") in TEXT_KINDS]
                assert len(replayed) == 2, \
                    f"★ 应只补发 2 帧（u1 最新 partial + u2 final），实际 {len(replayed)} 帧：" \
                    f"{[(f.get('utt'), f.get('t'), f.get('text')) for f in replayed]}"
                by_utt = {f["utt"]: f for f in replayed}
                assert set(by_utt) == {"u1", "u2"}, f"补发的句子不对：{sorted(by_utt)}"
                assert by_utt["u1"]["t"] == FRAME_SEG, "未完成句应补 partial"
                assert by_utt["u1"]["text"] == "我在测试", \
                    f"★ 只该补最新快照，实际补了 {by_utt['u1']['text']!r}"
                assert by_utt["u1"]["seq"] == 3, \
                    f"补发要沿用原 seq（接收方按 seq 去重）：{by_utt['u1']['seq']}"
                assert by_utt["u2"]["t"] == FRAME_FINAL and by_utt["u2"]["final"] is True
                assert by_utt["u2"]["text"] == "另一句定稿了"
                assert by_utt["u2"]["seq"] == 2
                # 补发不许把 B 的屏幕改坏：最终文本仍须与 A 一致
                wait_until(lambda: rec_b.final_map().get("u2") == "另一句定稿了",
                           "B 端应收到补发来的 final")
                assert rec_b.texts().count("我") <= 1, \
                    f"★ 重放历史 partial 了（B 端出现重复旧快照）：{rec_b.texts()}"
    print("  重连只补 final + 未完成句最新 partial OK（断线前 5 帧 → 重连补 2 帧）")


def test_reconnect_leaves_traces_and_recovers() -> None:
    """★ 断线不许静默：状态回调要能看到「连接中断 → 重连 → 已进房」全过程。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, rec):
            with client_ctx(r, "阿华") as (b, rec_b):
                wait_both_ready(r, a, b)
                rec.statuses.clear()
                r.kill_all()
                # ★ 必须等**两端**都回来：kill_all 掐的是两条连接，只等 A 就 publish 的话，
                #   B 还没重连上 → 扇出对象是空的 → B 永远收不到这一句（偶发超时的真凶）。
                wait_both_ready(r, a, b)
                wait_until(lambda: a.state().reconnects >= 1, "重连计数应 +1")
                # ★ 必须等到这一轮重连**真的走完**再看状态串：`reconnects` 是在「掉线留痕」那一刻
                #   就 +1 的，而 kill_all 之后 state 可能还残留着旧连接（wait_both_ready 会被
                #   旧状态立刻满足）。直接读 status 就可能只读到「连接中断…」那一行 —— CI 上真红过。
                wait_until(lambda: "已进房" in rec.status(), "重连后应重新进房（状态串必须留痕）")
                status = rec.status()
                assert "连接中断" in status, f"断线必须留痕：{status!r}"
                assert "重连" in status, f"重连必须留痕：{status!r}"
                assert "已进房" in status, f"重连成功必须留痕：{status!r}"
                assert a.state().conn is ConnectionState.ONLINE
                assert a.state().last_error == "", f"重连成功后不该还挂着错误：{a.state().last_error}"
                # 重连后仍然能收发，而且发到的是**新**连接
                a.publish("after", 1, "重连后说的话", is_final=True)
                wait_until(lambda: rec_b.final_map().get("after") == "重连后说的话",
                           "重连后应能继续发消息")
                new_a = r.conn_by_nick("小明")
                assert new_a.index > 0, "A 应该在一条新连接上"
                assert any(f.get("utt") == "after" for f in new_a.frames(FRAME_FINAL)), \
                    "★ 重连后的帧发到了旧连接上（调用方拿着死引用那个经典 bug）"
    print(f"  断线留痕 + 自动重连 + 重连后收发正常 OK（重连 {a.state().reconnects} 次）")


def test_backoff_schedule() -> None:
    """连不上时按退避表重试，**每次都留痕**（不许闷头疯狂重连打爆服务端）。"""
    dead = RoomConfig.from_dict({"server_url": "ws://127.0.0.1:1/ws", "room_code": TEST_ROOM,
                                 "nickname": "小明", "reconnect_backoff": [0.1, 0.2, 0.4],
                                 "heartbeat_s": 0})
    assert [dead.backoff_delay(i) for i in range(5)] == [0.1, 0.2, 0.4, 0.4, 0.4], \
        "超出退避表应停在最后一档"
    rec = Recorder()
    client = RoomClient(dead, rec.on_message, rec.on_status)
    started = time.monotonic()
    client.start()
    try:
        wait_until(lambda: client.state().reconnects >= 3, "应至少重试 3 次", timeout=20.0)
        elapsed = time.monotonic() - started
        st = client.state()
        assert st.conn is ConnectionState.RECONNECTING, st.conn
        assert not st.is_online
        assert st.last_error, "last_error 应记下失败原因"
        assert "ConnectionRefused" in st.last_error or "Errno" in st.last_error \
            or "refused" in st.last_error.lower(), f"失败原因应可读：{st.last_error!r}"
        status = rec.status()
        assert status.count("连不上房间服务端") >= 3, f"每次失败都该留痕：\n{status}"
        assert "重连" in status
        assert elapsed >= 0.25, f"退避没生效（{elapsed:.2f}s 就重试了 3 次，会打爆服务端）"
    finally:
        client.stop(timeout=5.0)
    print(f"  退避重连 OK（{st.reconnects} 次尝试 / {elapsed:.2f}s；末次错误 "
          f"{st.last_error[:44]}…）")


def test_stop_kills_thread_and_is_idempotent() -> None:
    """★ `stop()` 之后**线程真的死**（断言 `not t.is_alive()`），且不阻塞超过 5 秒。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, _ra):
            with client_ctx(r, "阿华") as (b, _rb):
                wait_both_ready(r, a, b)
                threads_before = threading.active_count()
                thread_a, thread_b = a._thread, b._thread        # ★ 先抓住线程对象
                assert thread_a is not None and thread_a.is_alive(), "前提：A 的线程本来是活的"
                assert thread_b is not None and thread_b.is_alive(), "前提：B 的线程本来是活的"

                b.stop(timeout=5.0)
                assert not thread_b.is_alive(), "★ stop() 之后 B 的房间线程还活着"
                wait_until(lambda: a.state().peer_count == 1, "B 停了，A 应看到它离房")

                t0 = time.monotonic()
                a.stop(timeout=5.0)
                elapsed = time.monotonic() - t0
                assert not thread_a.is_alive(), "★ stop() 之后 A 的房间线程还活着"
                assert elapsed < 5.0, f"★ stop() 阻塞了 {elapsed:.2f}s（硬上限 5s）"
                assert a.state().conn is ConnectionState.STOPPED
                assert threading.active_count() <= threads_before - 1, \
                    f"线程没回收干净：{threads_before} → {threading.active_count()}"

                t0 = time.monotonic()
                a.stop(timeout=5.0)                    # 幂等：再调不炸、不卡
                a.stop(timeout=0.1)
                assert time.monotonic() - t0 < 1.0, "幂等的 stop() 不该再阻塞"
                assert a.state().conn is ConnectionState.STOPPED
                assert not thread_a.is_alive()
                a.publish("u1", 1, "停了以后说的话", is_final=True)   # 不许抛
    print(f"  stop() 线程真的死 + 幂等 + 不阻塞 OK（A 停止耗时 {elapsed * 1000:.0f}ms）")


def test_stop_during_backoff_is_fast() -> None:
    """★ 在 30 秒退避等待中 `stop()` 也必须秒退（否则退出程序要干等半分钟）。"""
    dead = RoomConfig.from_dict({"server_url": "ws://127.0.0.1:1/ws", "room_code": TEST_ROOM,
                                 "reconnect_backoff": [30], "heartbeat_s": 0})
    client = RoomClient(dead, lambda m: None)
    client.start()
    try:
        wait_until(lambda: client.state().reconnects >= 1, "应先进入退避等待", timeout=15.0)
        thread = client._thread
        t0 = time.monotonic()
        client.stop(timeout=5.0)
        elapsed = time.monotonic() - t0
        assert not thread.is_alive(), "★ 退避中 stop() 之后线程还活着"
        assert elapsed < 2.0, f"★ 退避中 stop() 花了 {elapsed:.2f}s（应立刻打断 30s 退避）"
    finally:
        client.stop(timeout=1.0)
    print(f"  退避中 stop() 秒退 OK（{elapsed * 1000:.0f}ms，退避档是 30s）")


def test_restart_after_stop() -> None:
    """`stop()` 之后还能再 `start()`（GUI 上取消勾选再勾选就是这条路径）。"""
    with relay_ctx() as r:
        rec = Recorder()
        client = RoomClient(make_config(r, "小明"), rec.on_message, rec.on_status)
        client.start()
        wait_online(client, "第一次上线")
        first = client._thread
        client.stop(timeout=5.0)
        assert not first.is_alive()
        client.start()
        try:
            wait_online(client, "第二次上线")
            assert client._thread is not first, "重启后应是新线程"
            assert client.state().conn is ConnectionState.ONLINE
            assert client.state().me, "重启后应重新拿到成员 id"
        finally:
            client.stop(timeout=5.0)
    print("  stop 后可重新 start OK")


def test_err_and_unknown_frames_do_not_kill_connection() -> None:
    """非致命 `err` 帧 / 未知帧类型：留痕但**连接保持**（一句脏数据不该让房间哑掉）。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, rec):
            wait_online(a)
            conns = r.connections
            r.send_frame_to_last(FRAME_ERR, code="rate", msg="超过 20 帧/秒")
            wait_until(lambda: "20 帧/秒" in rec.status(), "非致命 err 要在状态回调里留痕")
            time.sleep(0.3)
            assert a.state().is_online, "★ 非致命 err 不该断线"
            assert "20 帧/秒" in a.state().last_error, a.state().last_error
            assert r.connections == conns, "非致命 err 不该触发重连"

            r.send_to_last('{"t":"hologram","data":"来自未来的帧"}')
            time.sleep(0.4)
            assert a.state().is_online, "★ 未知帧类型不该断线（对端升级不该打死老客户端）"
            assert r.connections == conns

            r.send_to_last("这不是 JSON")
            time.sleep(0.4)
            assert a.state().is_online, "★ 坏帧不该断线（丢弃 + 留痕就够了）"
            # 连接确实还活着：再发一句能正常到达对端
            with client_ctx(r, "阿华") as (b, rec_b):
                wait_both_ready(r, a, b)
                a.publish("alive", 1, "脏帧之后还能说话", is_final=True)
                wait_until(lambda: rec_b.final_map().get("alive") == "脏帧之后还能说话",
                           "脏帧之后连接应仍然可用")
    print("  非致命 err / 未知帧 / 坏 JSON → 留痕但不断线 OK")


def test_fatal_err_stops_retrying() -> None:
    """★ 致命 `err`（鉴权失败）→ 断开且**不再重连**（重连只会一直被拒、白烧请求）。"""
    with relay_ctx(token="正确的令牌") as r:
        rec = Recorder()
        client = RoomClient(make_config(r, "小明", token="错的令牌",
                                        reconnect_backoff=[0.05]),
                            rec.on_message, rec.on_status)
        client.start()
        thread = client._thread
        try:
            wait_until(lambda: client.state().conn is ConnectionState.ERROR,
                       "鉴权失败应进 ERROR 态", timeout=15.0)
            conns = r.connections
            time.sleep(0.6)
            assert r.connections == conns, \
                f"★ 致命错误后还在重连：{conns} → {r.connections}（会一直被拒）"
            status = rec.status()
            assert "不再重连" in status, f"必须说清不再重连：{status!r}"
            assert "auth" in status and "令牌" in status, f"必须带上服务端的原因：{status!r}"
            assert client.state().last_error, "last_error 应记下原因"
            wait_until(lambda: not thread.is_alive(), "致命错误后线程应退出", timeout=6.0)
        finally:
            client.stop(timeout=5.0)
        assert r.failed_hellos and r.failed_hellos[0].startswith("auth"), r.failed_hellos
    print("  致命 err → 不重连 + 线程退出 + 留痕 OK")


def test_room_full_is_reported() -> None:
    """房间满（这里把上限压到 2 好测）→ 第三个人拿到可读原因，不是干等超时。"""
    with relay_ctx(max_peers=2) as r:
        with client_ctx(r, "甲") as (a, _ra):
            with client_ctx(r, "乙") as (b, _rb):
                wait_both_ready(r, a, b)
                rec = Recorder()
                c = RoomClient(make_config(r, "丙", reconnect_backoff=[0.1]),
                               rec.on_message, rec.on_status)
                c.start()
                try:
                    wait_until(lambda: "已满" in rec.status(),
                               f"房间满要给可读原因：{rec.status()!r}", timeout=15.0)
                    assert c.state().conn is ConnectionState.ERROR
                    assert not c.state().is_online
                    assert "room_full" in c.state().last_error, c.state().last_error
                    assert a.state().peer_count == 2, "被拒的人不该出现在成员表里"
                finally:
                    c.stop(timeout=5.0)
    print("  房间满 → 可读原因（room_full）+ 不进成员表 OK")


def test_rate_limit_is_reported_not_fatal() -> None:
    """服务端限速（20 帧/秒的口径）→ 客户端收到 err 但**不掉线**，超出的帧被丢。"""
    with relay_ctx(rate_limit=5) as r:
        with client_ctx(r, "小明") as (a, rec):
            wait_online(a)
            for i in range(40):
                a.publish("spam", i + 1, f"刷屏第{i + 1}帧", is_final=False)
            wait_until(lambda: "帧/秒" in rec.status(), "限速要在状态回调里留痕", timeout=10.0)
            wait_until(lambda: len(r.conn(0).frames(FRAME_SEG)) == 40,
                       f"40 帧应都已到达中继，实际 {len(r.conn(0).frames(FRAME_SEG))}",
                       timeout=10.0)
            time.sleep(0.3)
            assert a.state().is_online, "★ 被限速不该掉线（下一拍还能继续说）"
            assert r.connections == 1, "被限速不该触发重连"
            # conn.frames() 是「中继收到的」，被丢的也在里面 → 得减掉 rejected 才是真放过的
            dropped = len(r.conn(0).rejected(FRAME_SEG))
            accepted = 40 - dropped
            assert dropped > 0, "★ 限速没生效（40 帧全放过了）"
            # 上界给 2 个桶的量：突发 40 帧可能横跨两个 1 秒窗口（每窗放过 rate_limit 帧）
            assert accepted <= 2 * r.rate_limit, \
                f"限速口径不对：放过 {accepted} 帧（上限 {r.rate_limit} 帧/秒）"
    print(f"  限速留痕但不断线 OK（发 40 帧，放过 {accepted} 帧、丢 {dropped} 帧）")


def test_heartbeat_pings_and_detects_dead_link() -> None:
    """心跳：按 `heartbeat_s` 发 ping；太久没有任何入站帧 → 判死、留痕、主动重连。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明", heartbeat_s=0.2) as (a, rec):
            wait_online(a)
            wait_until(lambda: len(r.conn_by_nick("小明").frames(FRAME_PING)) >= 2,
                       "应按 heartbeat_s 定时发 ping")
            wait_until(lambda: bool(r.conn_by_nick("小明").sent_frames(FRAME_PONG)),
                       "中继应回 pong（ping/pong 往返必须通，否则心跳形同虚设）")

            r.silence = True                      # 中继装死：不关连接，也不回任何帧
            a._last_inbound = now_ms() - 60_000   # 把「上次入站」拨到 60s 前，立刻触发超时判定
            # 留痕在前（_notify 在 _connect_once 里），reconnects 在后（回到 _run 才 +1），
            # 所以必须**分两次等**：合成一个 or 条件会偶发在两者之间读到 0。
            wait_until(lambda: "心跳" in rec.status(),
                       "★ 心跳超时必须判死并留痕（不能干等 TCP 自己发现）", timeout=15.0)
            wait_until(lambda: a.state().reconnects >= 1,
                       f"判死之后应触发重连（留痕已出现：{rec.status()[-120:]}）", timeout=15.0)
            r.silence = False
            wait_online(a, "中继恢复后应重新上线")
    print(f"  心跳 ping + 超时判死 + 恢复 OK（重连 {a.state().reconnects} 次）")


def test_state_snapshot_is_isolated() -> None:
    """`state()` 返回的是**快照**：调用方拿着不会被后台线程改出竞态。"""
    with relay_ctx() as r:
        with client_ctx(r, "小明") as (a, _ra):
            with client_ctx(r, "阿华") as (b, _rb):
                wait_both_ready(r, a, b)
                snap = a.state()
                assert len(snap.peers) == 2 and snap.conn is ConnectionState.ONLINE
                b.stop(timeout=5.0)
                wait_until(lambda: a.state().peer_count == 1, "A 应看到 B 离房")
                assert len(snap.peers) == 2, "★ 旧快照被后台线程改了（peers 必须是值拷贝）"
                assert snap.conn is ConnectionState.ONLINE, "旧快照里的连接态也不该变"
                assert isinstance(snap.peers, tuple), "peers 必须是 tuple（list 会被改）"
                assert a.state().peer_count == 1, "新快照应反映最新状态"
    print("  state() 快照隔离 OK")


def test_connect_url_carries_room_and_masks_token() -> None:
    """连接 URL 必须自带房间码，且**日志里不许出现明文令牌**。

    这两条都是真服务端逼出来的：
      · Worker 在把请求交给 DO **之前**就要用查询串里的 `room` 做路由（`idFromName`）
        和鉴权 —— 房间码只写在 hello 帧里，握手阶段直接吃 HTTP 400（实测过）。
      · 令牌走查询串会被 wrangler 的请求日志和客户端的连接日志原样打出来，
        而日志跟着 crashlog 落到用户硬盘上，明文令牌不该留在那儿。
    """
    token = "s3cr3t-TOKEN-9"
    with relay_ctx(token=token) as r:
        cfg = make_config(r, "小明", token=token)
        rec = Recorder()
        client = RoomClient(cfg, rec.on_message, rec.on_status)

        url = client._connect_url()
        assert f"room={TEST_ROOM}" in url, \
            f"★ 连接 URL 必须自带房间码（真 Worker 靠它路由，缺了直接 400）：{url}"
        assert token not in url, '令牌只能进入 Authorization，不能进入 URL'

        masked = client._masked_url(url)
        assert token not in masked, f"★ 掩码没生效，明文令牌还在：{masked}"
        assert f"room={TEST_ROOM}" in masked, "掩码不该把房间码也抹掉（排查要用）"

        # 用户已经手写了 room=/k= 就不覆盖（尊重手写，别搞出两份口径）
        hand = RoomClient(RoomConfig(server_url=f"{r.url}?room=HANDWRIT&k=mine",
                                     room_code=TEST_ROOM, nickname="小明"), lambda m: None)
        assert hand._connect_url() == f"{r.url}?room=HANDWRIT", \
            f"★ 手写的 room=/k= 被覆盖了：{hand._connect_url()}"
        assert hand._auth_token() == 'mine', '旧 query token 应迁移到 header'

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            client.start()
            wait_online(client, "带令牌也应能进房")
        client.stop(timeout=5.0)
        assert not client._thread.is_alive(), "stop 之后线程还活着"

        logged = buf.getvalue()
        leaked = [ln for ln in logged.splitlines() if token in ln]
        assert not leaked, f"★ 连接日志里有明文令牌：{leaked}"
        assert f"room={TEST_ROOM}" in logged, f"日志里应能看出连的是哪个房间：{logged[-200:]}"

        assert any(f"room={TEST_ROOM}" in p for p in r.paths), \
            f"★ 握手请求的路径里没带房间码：{r.paths}"
        assert not any("k=" in p for p in r.paths), '握手路径不应带令牌'
        assert 'tok' not in r.conn().hello, 'HELLO 不应重传令牌'
        last_path = r.paths[-1]
    print(f"  连接 URL 自带房间码 + 令牌掩码 OK（握手路径 {last_path}，日志无明文令牌）")


def _start_http_rejector(status: int):
    """起一个只会用 HTTP 状态码拒绝握手的服务器（模拟 Worker 在建连前那一拒）。

    ⚠️ 必须手写 HTTP/1.1 响应：`http.server` 默认回 HTTP/1.0，websockets 会判成
    「不是合法 HTTP 响应」（`InvalidMessage`）而**不是** `InvalidStatus` —— 那就没测到
    真实路径（本测试第一版正是这么写错的：假服务器抛的异常类型与真 Worker 不是同一个）。
    """
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    reason = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden"}.get(status, "Error")
    resp = (f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n"
            f"Connection: close\r\n\r\n").encode()

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return                       # 监听套接字已关 → 收干净退出
            try:
                conn.recv(4096)
                conn.sendall(resp)
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    return f"ws://127.0.0.1:{srv.getsockname()[1]}/ws", srv


def test_http_rejection_is_fatal() -> None:
    """★ Worker 在**建连前**用 HTTP 拒绝（401 缺令牌 / 403 令牌不对）→ 当致命错，不许重连。

    真 CF Worker 的房间码校验与鉴权都在 Worker 层做完，被拒时连接根本没升级成
    WebSocket，客户端**收不到**协议里那个 `err{code:"auth"}`。
    `test_fatal_err_stops_retrying` 走的是假中继的 `err` 帧那条路，覆盖不到这里 ——
    2026-09-28 在真部署上实测踩到：403 被当成网络抖动，客户端按退避无限重连。
    """
    for status, keyword in ((403, "令牌不对"), (401, "没配 room.token")):
        url, srv = _start_http_rejector(status)
        try:
            rec = Recorder()
            client = RoomClient(
                RoomConfig(server_url=url, room_code=TEST_ROOM, token="t",
                           reconnect_backoff=[0.05]),
                rec.on_message, rec.on_status)
            client.start()
            thread = client._thread
            try:
                wait_until(lambda: client.state().conn is ConnectionState.ERROR,
                           f"HTTP {status} 应进 ERROR 态", timeout=15.0)
                st = client.state()
                assert st.reconnects == 0, \
                    f"★ HTTP {status} 后还在重连（{st.reconnects} 次）—— 这类错重连也没用"
                assert keyword in st.last_error, \
                    f"★ 原因不可读：{st.last_error!r}（应提到 {keyword!r}）"
                assert "不再重连" in rec.status(), f"必须说清不再重连：{rec.status()!r}"
                wait_until(lambda: not thread.is_alive(), "致命错误后线程应退出", timeout=6.0)
            finally:
                client.stop(timeout=5.0)
        finally:
            srv.close()
    print("  Worker 建连前 HTTP 拒绝 → 致命错 / 零重连 / 原因可读 OK（401/403）")


if __name__ == "__main__":
    print("test_room_client:")
    test_public_surface_never_leaks_ws()
    test_missing_config_is_not_silent()
    test_connect_and_welcome()
    test_members_broadcast()
    test_send_receive_consistency()
    test_ack_only_for_final()
    test_publish_is_thread_safe()
    test_publish_before_connect_is_not_lost()
    test_reconnect_replays_only_final_and_latest_partial()
    test_reconnect_leaves_traces_and_recovers()
    test_backoff_schedule()
    test_stop_kills_thread_and_is_idempotent()
    test_stop_during_backoff_is_fast()
    test_restart_after_stop()
    test_err_and_unknown_frames_do_not_kill_connection()
    test_fatal_err_stops_retrying()
    test_room_full_is_reported()
    test_rate_limit_is_reported_not_fatal()
    test_heartbeat_pings_and_detects_dead_link()
    test_state_snapshot_is_isolated()
    test_connect_url_carries_room_and_masks_token()
    test_http_rejection_is_fatal()
    print("ALL PASSED")
