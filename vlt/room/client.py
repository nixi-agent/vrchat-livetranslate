"""`RoomClient`：房间文本中继的客户端（后台线程 + asyncio WebSocket）。

沿用的 VLT 既有铁律（每一条都是踩过的坑）：
    · **停止 = 置位线程安全的 `stop_event`**。测试断言用 `not thread.is_alive()`，
      不要断言什么 `running` 属性 —— 属性可能还挂着 True 而线程早死了。
    · **调用方绝不持有 WS 对象**：重连会换对象，拿着旧引用会静默失效
      （音频/文本还在往死连接上灌）。所以 `publish()` 内部每次动态取当前连接，
      本类也**不提供任何返回 ws 的接口**。
    · **连接不能静默失败**：连不上、缺配置、发不出去、心跳超时，一律
      「日志 + `on_status` 回调」各留一条人能读懂的原因。禁静默降级。
    · **重连只补 final + 每个未完成句的最新 partial**，不重放全部 partial
      （重放等于把别人的屏幕倒带重演一遍，还会瞬间撞上服务端 20 帧/秒限速）。

线程模型：
    调用方线程（采集/GUI） ──publish()/stop()──▶ 本类的后台线程（跑一个 asyncio 事件循环）
    `on_message` / `on_status` **在房间线程上回调**，实现方必须自己保证线程安全且快速返回
    （批次 2 接 GUI 时要 `after()` 转回主线程）。

代理：本类**不写任何代理/分流代码**（方案 §1.3 用户口径「网络这块直接不用管」），
`server_url` 直连，是否走系统代理交给 `websockets` 的默认行为。
"""
from __future__ import annotations

import asyncio
import base64
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .model import ConnectionState, Peer, RoomConfig, RoomMessage, RoomState, log
from .protocol import (FATAL_ERR_CODES, FRAME_ACK, FRAME_ERR, FRAME_FINAL, FRAME_HELLO,
                       FRAME_MEMBERS, FRAME_PING, FRAME_PONG, FRAME_SEG, FRAME_WELCOME,
                       MAX_FRAME_BYTES, ProtocolError, apply_seg, decode_frame,
                       encode_frame, normalize_room_code, now_ms, should_send_partial)

MessageHandler = Callable[[RoomMessage], None]
StatusHandler = Callable[[str], None]

CONNECT_TIMEOUT_S = 10.0        # TCP+WS 握手超时
WELCOME_TIMEOUT_S = 10.0        # 发完 hello 等 welcome 的超时
REPLAY_MAX = 64                 # 回放表上限（防长时间断线把它撑爆）
SEEN_FINALS_MAX = 512           # final 去重集合上限
STORE_MAX = 256                 # 每 (说话人, 句) 一条的合并存储上限

# 服务端认哪几个查询参数当令牌（与 server/src/index.js 的 TOKEN_QUERY_KEYS 同一口径）
TOKEN_QUERY_KEYS = ("k", "tok", "token")
# 连接日志里必须把令牌抹掉：日志会跟着 crashlog 落到用户硬盘上，明文令牌不该留在那儿
_TOKEN_IN_URL_RE = re.compile(r"([?&](?:%s)=)[^&]*" % "|".join(TOKEN_QUERY_KEYS))


class _FatalRoomError(RuntimeError):
    """服务端明确拒绝（鉴权失败 / 房间满 / 房间码非法）→ 别再重连了，重连只会一直被拒。"""


#: Worker **在建连之前**用 HTTP 状态码拒掉时的语义（对端是 CF Worker 时必踩）。
#: 协议里那个 `err{code:"auth"}` 只在 DO 层出现；鉴权/房间码校验发生在 Worker 层，
#: 客户端根本收不到 `err` 帧，只会看到握手被拒 —— 所以这两条路径要在这里补齐。
HTTP_FATAL_REASONS: dict[int, str] = {
    400: "房间码被服务端拒绝（8 位 Crockford Base32，不含 I/L/O/U）",
    401: "这个房间要令牌，但本机没配 room.token",
    403: "令牌不对（room.token 与服务端 ROOM_TOKEN_HASH 对不上）",
}


@dataclass
class _Pending:
    """一句「还没被服务端 ack」的话，重连后按它补发。"""

    utt: str
    seq: int
    text: str
    lang: str
    is_final: bool


class RoomClient:
    """一个房间成员。契约见方案 §6.1。"""

    def __init__(self, cfg: RoomConfig, on_message: MessageHandler,
                 on_status: StatusHandler | None = None) -> None:
        self.cfg = cfg
        self._on_message = on_message
        self._on_status = on_status
        self._nick = cfg.effective_nickname          # 缓存：别每次 state() 都去问系统
        # 归一化后的房间码：服务端 DO 的名字是 Worker 拿归一化结果 idFromName() 出来的，
        # DO 还会用它逐字比对 hello 里的 room —— 原样发用户手输的 `abcd-1234` 会被判 bad_room。
        self._room = normalize_room_code(cfg.room_code) or cfg.room_code

        self._lock = threading.RLock()               # 保护下面这些跨线程字段
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()              # 后台线程已把事件循环建起来
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None                         # 私有！**绝不外泄**（重连会换对象）

        self._conn = ConnectionState.IDLE
        self._me = ""
        self._peers: dict[str, Peer] = {}
        self._last_error = ""
        self._reconnects = 0
        self._sent = 0
        self._recv = 0
        self._send_fails = 0
        self._offline_dropped = 0

        self._replay: dict[str, _Pending] = {}       # utt → 待 ack 的最新一帧
        self._seq_by_utt: dict[str, int] = {}        # utt → 已发出的最高 seq
        self._last_partial: dict[str, tuple[str, int]] = {}   # utt → (上次发的文本, 墙钟 ms)
        self._store: dict[str, dict[str, Any]] = {}  # "src:utt" → 合并后的最新条目
        self._seen_finals: set[str] = set()
        self._acked = 0
        self._rev_warned: set[str] = set()
        self._mute_warned = False                      # broadcast_source=false 只报一次

        self._stop_async: asyncio.Event | None = None
        self._main_task: asyncio.Task | None = None
        self._last_inbound = 0

    def __repr__(self) -> str:
        return (f"<RoomClient {self._room or '(无房间码)'} "
                f"{self._conn.value} 成员{len(self._peers)}人>")

    # ================================================================ 生命周期

    def start(self) -> None:
        """启动后台线程（**幂等**：已在跑就直接返回）。

        配置不齐时不静默：日志 + 状态回调各留一条原因，然后**不启动线程**。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        reason = self.cfg.unavailable_reason()
        if reason:
            # 光走 _notify（日志 + 状态回调）不够：GUI 轮询的是 state() 快照，
            # 不写进 _last_error 的话，界面上只有「错误」两个字，看不到原因。
            with self._lock:
                self._last_error = reason
            self._set_conn(ConnectionState.ERROR)
            self._notify(f"❌ 房间链路没启动：{reason}")
            return
        self._stop_event.clear()
        self._ready.clear()
        with self._lock:
            self._last_error = ""
        self._set_conn(ConnectionState.CONNECTING)
        self._notify(f"启动房间链路（房间 {self._room}，昵称 {self._nick}，"
                     f"服务端 {self._masked_url(self._connect_url())}）")
        self._thread = threading.Thread(
            target=self._thread_main, name=f"room-{self._room}", daemon=True)
        self._thread.start()
        # 只等「事件循环建起来」，不等连上 —— 连接是异步的，start() 不能卡住调用方
        if not self._ready.wait(5.0):
            log("⚠️ 后台线程 5s 内没建起事件循环（start() 不再等，连接会在后台继续）")

    def stop(self, timeout: float = 5.0) -> None:
        """停止（**幂等**，且**绝不阻塞超过 5 秒**）。

        `timeout` 会被夹到 [0, 5]：房间链路是旁路功能，不许拖住退出流程。
        超时也不会硬杀（Python 杀不了线程），但会留一行痕迹；线程是 daemon，
        进程退出时会被带走。
        """
        wait = max(0.0, min(float(timeout), 5.0))
        th = self._thread
        already = self._stop_event.is_set()
        self._stop_event.set()
        if already and (th is None or not th.is_alive()):
            return                                     # 幂等：第二次调用什么都不做
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._wake_stop)
            except RuntimeError as exc:                # 循环已经在收尾
                log(f"停止时事件循环已关（{exc}）→ 只等线程自己退")
        if th is not None and th.is_alive():
            th.join(wait)
            if th.is_alive():
                log(f"⚠️ 等了 {wait:.1f}s 房间线程仍在跑（daemon 线程，进程退出会带走）")
        self._loop = None
        self._ws = None
        self._set_conn(ConnectionState.STOPPED)
        log("已停止")

    def _wake_stop(self) -> None:
        """（在事件循环线程里跑）立刻打断重连退避与握手等待，让线程能秒退。"""
        if self._stop_async is not None:
            self._stop_async.set()
        task = self._main_task
        if task is not None and not task.done():
            task.cancel()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:                       # noqa: BLE001
            # 房间线程绝不能带着异常悄悄死掉：那是「为什么房间没反应」查不出来的根源
            self._set_conn(ConnectionState.ERROR)
            self._notify(f"❌ 房间线程异常退出：{type(exc).__name__}: {exc}")

    async def _run(self) -> None:
        """主循环：连 → 跑到断 → 退避 → 再连，直到 `stop()`。"""
        self._loop = asyncio.get_running_loop()
        self._stop_async = asyncio.Event()
        self._main_task = asyncio.current_task()
        self._ready.set()
        attempt = 0
        try:
            while not self._stop_event.is_set():
                established = False
                try:
                    established = await self._connect_once()
                except asyncio.CancelledError:
                    log("收到停止信号 → 结束重连循环")
                    break
                except _FatalRoomError as exc:
                    with self._lock:
                        self._last_error = str(exc)
                    self._set_conn(ConnectionState.ERROR)
                    self._notify(f"❌ 服务端拒绝，不再重连：{exc}")
                    break
                except Exception as exc:               # noqa: BLE001
                    err = f"{type(exc).__name__}: {exc}"
                    with self._lock:
                        self._last_error = err
                    log(f"⚠️ 连接失败：{err}")
                if self._stop_event.is_set():
                    break
                if established:
                    attempt = 0                        # 连上过 → 下次掉线从退避表第一档重来
                with self._lock:
                    self._reconnects += 1
                    n = self._reconnects
                delay = self.cfg.backoff_delay(attempt)
                attempt += 1
                self._set_conn(ConnectionState.RECONNECTING)
                head = "⚠️ 连接中断" if established else "⚠️ 连不上房间服务端"
                self._notify(f"{head}，{delay:g}s 后重连（第 {n} 次）")
                try:
                    await asyncio.wait_for(self._stop_async.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._loop = None
            self._ws = None

    def _connect_url(self) -> str:
        """房间码用于 Worker 路由；旧 URL 内的令牌迁移至 Authorization。"""
        base = self.cfg.server_url.strip()
        parts = urlsplit(base)
        query: dict[str, str] = dict(parse_qsl(parts.query, keep_blank_values=True))
        if self._room and not query.get("room"):
            query["room"] = self._room
        for key in TOKEN_QUERY_KEYS:
            query.pop(key, None)
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(query), parts.fragment))

    def _auth_token(self) -> str:
        query = dict(parse_qsl(urlsplit(self.cfg.server_url.strip()).query, keep_blank_values=True))
        return next((query[key] for key in TOKEN_QUERY_KEYS if query.get(key)),
                    (self.cfg.token or '').strip())

    def _auth_header(self) -> dict[str, str] | None:
        token = self._auth_token()
        if not token:
            return None
        # Bearer 的 token68 不接受 Unicode；保留普通令牌对旧 Worker 的兼容性。
        value = 'Bearer ' + token if re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', token) else (
            'VLT ' + base64.urlsafe_b64encode(token.encode('utf-8')).decode('ascii'))
        return {'Authorization': value}

    @staticmethod
    def _masked_url(url: str) -> str:
        """把 URL 里的令牌换成 `***` 再给人看（日志会落到用户硬盘上）。"""
        return _TOKEN_IN_URL_RE.sub(r"\1***", url)

    @staticmethod
    def _fatal_from_http(status: int) -> Exception:
        """把 Worker 在建连前回的 HTTP 状态码翻成异常（400/401/403 属于致命，不再重连）。

        没有映到的状态码（5xx / 网络层）走 `ProtocolError` —— 那是**临时**故障，
        该退避重连，不能一刀切成「永不重连」。
        """
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = 0
        reason = HTTP_FATAL_REASONS.get(code)
        if reason:
            return _FatalRoomError(f"HTTP {code}：{reason}")
        return ProtocolError(f"服务端拒绝握手：HTTP {code or '未知'}")

    async def _open_ws(self, url: str):
        """建连；把 Worker 的 HTTP 拒绝翻成致命错误（收不到 `err` 帧的那一类）。

        CF Worker 在**把请求交给 DO 之前**就做完了房间码校验与鉴权，被拒时回的是
        HTTP 400/401/403，连接根本没升级成 WebSocket，客户端收不到协议里的
        `err{code:"auth"}`。实测在真部署上踩到：不在这里翻译，客户端会把 403
        当成网络抖动，按退避**无限重连**下去（正是协议里要避免的「一直撞墙」）。
        """
        import websockets                              # 延迟导入：没开房间功能就不该付这个开销

        try:
            connection = websockets.connect(
                url,
                additional_headers=self._auth_header(),
                open_timeout=CONNECT_TIMEOUT_S,
                close_timeout=2.0,
                # 心跳走**应用层** ping/pong：DO 休眠时协议层 ping 由 CF 边缘代答，
                # 答了也不代表房间还活着，会掩盖真实掉线。
                ping_interval=None,
                max_size=MAX_FRAME_BYTES,
            )
            # 不跟随重定向，避免将 Authorization 转交其他端点。
            connection.process_redirect = lambda exc: exc
            return await connection
        except websockets.exceptions.InvalidStatus as exc:
            raise self._fatal_from_http(getattr(exc.response, "status_code", 0)) from exc

    async def _connect_once(self) -> bool:
        """连一次，跑到连接断开为止。返回是否**成功进过房**（收到 welcome）。"""
        url = self._connect_url()
        log(f"连接 {self._masked_url(url)}（房间 {self._room}，昵称 {self._nick}）")
        async with await self._open_ws(url) as ws:
            self._ws = ws
            try:
                await ws.send(encode_frame(
                    FRAME_HELLO, room=self._room,
                    nick=self._nick, ts=now_ms()))
                with self._lock:
                    self._sent += 1
                frame = decode_frame(await asyncio.wait_for(ws.recv(), WELCOME_TIMEOUT_S))
                self._last_inbound = now_ms()
                with self._lock:
                    self._recv += 1
                if frame.get("t") == FRAME_ERR:
                    raise self._fatal_from_err(frame)
                if frame.get("t") != FRAME_WELCOME:
                    raise ProtocolError(f"首帧不是 welcome，是 {frame.get('t')!r}")
                with self._lock:
                    self._me = str(frame.get("me") or "")
                    self._offline_dropped = 0
                self._set_conn(ConnectionState.ONLINE)
                self._notify(f"✅ 已进房 {self._room}（我的成员 id：{self._me or '(服务端没给)'}）")
                await self._replay_pending(ws)
                try:
                    await self._pump(ws)
                except _FatalRoomError:
                    raise                              # 服务端明确拒绝 → 交给 _run 停掉重连
                except Exception as exc:               # noqa: BLE001
                    # 连上之后掉线（ConnectionClosed* 等）：就地留痕，交给上层退避重连。
                    # 不能让它冒充「连不上」——那是两种完全不同的故障，日志要能区分。
                    err = f"{type(exc).__name__}: {exc}"
                    with self._lock:
                        self._last_error = err
                    log(f"⚠️ 连接断开：{err}")
                return True
            finally:
                self._ws = None

    @staticmethod
    def _fatal_from_err(frame: dict[str, Any]) -> Exception:
        """把服务端的 `err` 帧翻成异常：致命码走 `_FatalRoomError`（不再重连）。"""
        code = str(frame.get("code") or "")
        msg = str(frame.get("msg") or frame.get("text") or code or "服务端没给原因")
        if code in FATAL_ERR_CODES:
            return _FatalRoomError(f"{code}：{msg}")
        return ProtocolError(f"服务端报错 {code or '(无码)'}：{msg}")

    async def _pump(self, ws: Any) -> None:
        """收发 + 心跳三路并发，任一路结束就收摊（断线由上层重连）。"""
        tasks = [
            asyncio.ensure_future(self._recv_loop(ws)),
            asyncio.ensure_future(self._beat(ws)),
            asyncio.ensure_future(self._stop_async.wait() if self._stop_async else
                                  asyncio.sleep(3600)),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._stop_event.is_set():
            return
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                raise exc
        log("⚠️ 连接被对端正常关闭（没有异常）→ 走重连")

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            self._last_inbound = now_ms()
            if self._stop_event.is_set():
                return
            try:
                frame = decode_frame(raw)
            except ProtocolError as exc:
                log(f"⚠️ 丢弃坏帧（连接保持）：{exc}")
                continue
            with self._lock:
                self._recv += 1
            try:
                self._handle_frame(frame)
            except _FatalRoomError:
                raise
            except Exception as exc:                   # noqa: BLE001
                # 处理单帧出错不该打死连接（否则一句脏数据就掉线，用户体验莫名其妙）
                log(f"⚠️ 处理 {frame.get('t')!r} 帧时异常（连接保持）："
                    f"{type(exc).__name__}: {exc}")

    async def _beat(self, ws: Any) -> None:
        """应用层心跳：定时发 ping，太久没收到任何入站帧就判死并主动重连。"""
        interval = float(self.cfg.heartbeat_s)
        if interval <= 0:
            log("heartbeat_s=0 → 不发应用层心跳（掉线只能靠 TCP 自己发现，可能等很久）")
            if self._stop_async is not None:
                await self._stop_async.wait()
            return
        deadline_ms = interval * 2000 + 5000           # 两个心跳周期 + 5s 宽限
        while True:
            await asyncio.sleep(interval)
            if self._stop_event.is_set():
                return
            silent_ms = now_ms() - self._last_inbound
            if silent_ms > deadline_ms:
                with self._lock:
                    self._last_error = f"心跳超时：{silent_ms / 1000:.0f}s 无入站帧"
                self._notify(f"⚠️ {silent_ms / 1000:.1f}s 没收到任何入站帧（心跳超时）"
                             f"→ 判定连接已死，主动重连")
                try:
                    await ws.close(code=1001, reason="heartbeat timeout")
                except Exception as exc:               # noqa: BLE001
                    log(f"关旧连接时异常（忽略）：{type(exc).__name__}: {exc}")
                return
            try:
                await ws.send(encode_frame(FRAME_PING, ts=now_ms()))
                with self._lock:
                    self._sent += 1
            except Exception as exc:                   # noqa: BLE001
                self._notify(f"⚠️ 心跳发送失败 → 走重连：{type(exc).__name__}: {exc}")
                return

    # ================================================================ 发送

    def publish(self, utt: str, rev: int, text: str, is_final: bool) -> None:
        """把本机 ASR 源文广播到房间（**线程安全**，采集线程可直接调）。

        utt      : 句 id（调用方生成，**重连不要重置**，否则对端没法覆盖同一句）
        rev      : 句内递增版本号（会被抬成严格递增，见 `_next_seq`）
        text     : **全量快照**（不是增量；本类不会替你拼接）
        is_final : 定稿。final **必发**，不受 partial 节流影响

        没连上也不丢：帧留在回放表里，重连后按「final + 每个未完成句的最新 partial」补发。
        """
        if self._stop_event.is_set():
            return
        if not self.cfg.broadcast_source:
            with self._lock:
                first = not self._mute_warned
                self._mute_warned = True
            if first:
                log("room.broadcast_source=false → 本机源文不广播（只收不发）")
            return
        if not utt:
            log("⚠️ publish 收到空 utt（没法覆盖/去重）→ 丢弃这一帧")
            return
        snap = text.strip() if isinstance(text, str) else ""
        if not snap:
            return
        now = now_ms()
        with self._lock:
            seq = self._next_seq(utt, rev)
            if not is_final:
                prev_text, prev_ms = self._last_partial.get(utt, ("", 0))
                if not should_send_partial(prev_text, prev_ms, snap, now,
                                           self.cfg.partial_min_ms):
                    return                             # 节流：内容没变 / 间隔太短
                self._last_partial[utt] = (snap, now)
            else:
                self._last_partial.pop(utt, None)
            self._replay[utt] = _Pending(utt=utt, seq=seq, text=snap,
                                         lang=self.cfg.source_lang, is_final=bool(is_final))
            self._prune()
        self._send(FRAME_FINAL if is_final else FRAME_SEG,
                   utt=utt, seq=seq, text=snap, lang=self.cfg.source_lang,
                   final=bool(is_final), ts=now)

    def _next_seq(self, utt: str, rev: Any) -> int:
        """把调用方给的 `rev` 抬成**严格递增**的 `seq`（调用方必须已持有 `_lock`）。

        协议要求 seq 句内递增，接收方只认更高值；万一调用方给了倒退的 rev，
        这里兜住并留痕（同一句只留一次痕，避免刷屏）。
        """
        last = self._seq_by_utt.get(utt, -1)
        try:
            want = int(rev)
        except (TypeError, ValueError):
            want = last + 1
            log(f"⚠️ publish 的 rev={rev!r} 不是整数（utt={utt}）→ 用 {want}")
        if want <= last:
            want = last + 1
            if utt not in self._rev_warned:
                self._rev_warned.add(utt)
                log(f"⚠️ publish 的 rev 倒退了（utt={utt}）→ 已强制递增到 {want}"
                    f"（同一句只报一次）")
        self._seq_by_utt[utt] = want
        return want

    def _send(self, kind: str, **fields: Any) -> bool:
        """把一帧交给事件循环异步发出（**不阻塞调用方**，失败只留痕不抛）。"""
        loop, ws = self._loop, self._ws
        if loop is None or ws is None or self._stop_event.is_set():
            self._note_offline(kind)
            return False
        frame = encode_frame(kind, **fields)
        try:
            fut = asyncio.run_coroutine_threadsafe(self._ws_send(frame), loop)
        except RuntimeError as exc:                    # 事件循环正在收尾
            log(f"⚠️ 事件循环已关，{kind} 帧没发出去：{exc}")
            return False
        fut.add_done_callback(self._on_send_done)
        return True

    async def _ws_send(self, frame: str) -> None:
        """（事件循环线程）真正发一帧。**每次动态取 `self._ws`** —— 重连会换对象。"""
        ws = self._ws
        if ws is None:
            raise ConnectionError("WS 尚未就绪")
        await ws.send(frame)
        with self._lock:
            self._sent += 1

    def _on_send_done(self, fut: Any) -> None:
        try:
            exc = fut.exception()
        except Exception as e:                         # noqa: BLE001  被取消时会抛
            exc = e
        if exc is None:
            with self._lock:
                self._offline_dropped = 0
            return
        with self._lock:
            self._send_fails += 1
            n = self._send_fails
        if n == 1 or n % 20 == 0:                      # 只报第一次和每 20 次，避免刷屏
            log(f"⚠️ 发送失败（累计 {n} 次）：{type(exc).__name__}: {exc}"
                f"；final/未完成句已留在回放表，重连后补发")

    def _note_offline(self, kind: str) -> None:
        """未连接时发帧：留痕（第一次 + 每 50 次），并说明这些数据不会凭空消失。"""
        with self._lock:
            self._offline_dropped += 1
            n = self._offline_dropped
        if n == 1 or n % 50 == 0:
            log(f"⚠️ 当前未连接，{kind} 帧没发出去（累计 {n} 帧）；"
                f"final 与每个未完成句的最新 partial 会在重连后补发")

    async def _replay_pending(self, ws: Any) -> None:
        """重连后补发：**只补 final + 每个未完成句的最新 partial**。

        seq 沿用原值（不重新编号）：接收方按 seq 只认更高值、按 (src, utt) 对 final 幂等，
        所以重发同一帧是安全的 —— 收过的会被挡掉，没收过的正好补上。
        """
        with self._lock:
            items = list(self._replay.values())
        if not items:
            return
        finals = [p for p in items if p.is_final]
        partials = [p for p in items if not p.is_final]
        log(f"重连补发：final {len(finals)} 条 + 未完成句最新 partial {len(partials)} 条"
            f"（不重放历史 partial）")
        for p in items:
            try:
                await ws.send(encode_frame(FRAME_FINAL if p.is_final else FRAME_SEG,
                                           utt=p.utt, seq=p.seq, text=p.text,
                                           lang=p.lang, final=p.is_final, ts=now_ms()))
                with self._lock:
                    self._sent += 1
            except Exception as exc:                   # noqa: BLE001
                log(f"⚠️ 补发 utt={p.utt} 失败（下一次重连再补）：{type(exc).__name__}: {exc}")
                return

    def _prune(self) -> None:
        """给各个按句增长的容器封顶（调用方必须已持有 `_lock`）。

        长时间挂机 + 一直说话，这些 dict 会无限长；封顶丢的是最老的，
        而最老的句子早就定稿上屏了，丢掉不影响正确性。
        """
        for table, cap in ((self._replay, REPLAY_MAX), (self._seq_by_utt, STORE_MAX),
                           (self._last_partial, STORE_MAX), (self._store, STORE_MAX)):
            while len(table) > cap:
                table.pop(next(iter(table)))
        while len(self._seen_finals) > SEEN_FINALS_MAX:
            self._seen_finals.pop()                    # set 无序：丢哪个都行，只是去重窗口
        while len(self._rev_warned) > STORE_MAX:
            self._rev_warned.pop()

    # ================================================================ 接收

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind in (FRAME_SEG, FRAME_FINAL):
            self._on_seg(frame)
        elif kind == FRAME_MEMBERS:
            self._on_members(frame)
        elif kind == FRAME_ACK:
            self._on_ack(frame)
        elif kind == FRAME_PONG:
            return                                     # 心跳回包：入站时间已在 _recv_loop 记过
        elif kind == FRAME_PING:
            self._send(FRAME_PONG, ts=now_ms())
        elif kind == FRAME_WELCOME:
            log("⚠️ 连接中途又收到 welcome（服务端重发了？）→ 只更新成员 id")
            with self._lock:
                self._me = str(frame.get("me") or self._me)
        elif kind == FRAME_ERR:
            code = str(frame.get("code") or "")
            msg = str(frame.get("msg") or frame.get("text") or "(无原因)")
            with self._lock:
                self._last_error = f"{code or 'err'}：{msg}"
                err = self._last_error
            if code in FATAL_ERR_CODES:
                self._notify(f"❌ 服务端致命错误 {err} → 断开且不再重连")
                raise _FatalRoomError(err)
            self._notify(f"⚠️ 服务端报错（连接保持）：{err}")
        else:
            log(f"忽略未知帧类型 {kind!r}（对端可能比本机新）")

    def _on_seg(self, frame: dict[str, Any]) -> None:
        src = str(frame.get("src") or "")
        if not src:
            log("⚠️ 收到没有 src 的文本帧 → 丢弃（不知道是谁说的，没法上屏）")
            return
        if src == self._me:
            return                                     # 自己发的回音：防往返，直接丢
        with self._lock:
            entry = apply_seg(self._store, self._seen_finals, frame)
            self._prune()
        if entry is None:
            return                                     # 乱序/重复/迟到的旧快照，协议层已挡掉
        msg = RoomMessage(
            peer_id=src,
            nick=self._nick_of(src),
            utt=str(entry.get("utt") or ""),
            seq=int(entry.get("seq") or 0),
            text=str(entry.get("text") or ""),
            lang=str(entry.get("lang") or self.cfg.source_lang),
            is_final=bool(entry.get("final")),
            srv_ts=int(entry.get("srv_ts") or 0),
        )
        cb = self._on_message
        if cb is None:
            return
        try:
            cb(msg)
        except Exception as exc:                       # noqa: BLE001
            # 上层回调抛异常绝不能打死房间线程（否则一个人的 bug 让整个房间哑掉）
            log(f"⚠️ on_message 回调抛异常（房间链路继续）：{type(exc).__name__}: {exc}")

    def _on_members(self, frame: dict[str, Any]) -> None:
        peers: dict[str, Peer] = {}
        for item in frame.get("members") or []:
            peer = Peer.from_dict(item)
            if peer is None:
                log(f"⚠️ members 帧里有一项不是合法成员（已跳过）：{item!r}")
                continue
            peers[peer.id] = peer
        with self._lock:
            old = self._peers
            changed = (set(old) != set(peers)
                       or any(old[p].nick != peers[p].nick for p in peers if p in old))
            self._peers = peers
            over = len(peers) > self.cfg.max_peers
        if changed:
            names = "、".join(f"{p.display}({p.id})" for p in peers.values())
            log(f"成员 {len(peers)} 人：{names or '(空)'}")
        if over:
            log(f"⚠️ 房间 {len(peers)} 人，超过配置上限 {self.cfg.max_peers}"
                f"（上限由服务端强制，本机只报不拦）")

    def _on_ack(self, frame: dict[str, Any]) -> None:
        """final 的 ack：收到就把这句移出回放表（不再补发）。"""
        utt = str(frame.get("utt") or "")
        with self._lock:
            pend = self._replay.get(utt)
            if pend is not None and pend.is_final:
                del self._replay[utt]
                self._acked += 1
                return
        log(f"收到 ack 但回放表里没有 utt={utt!r}（可能已被 ack 过或过期淘汰）")

    def _nick_of(self, peer_id: str) -> str:
        with self._lock:
            peer = self._peers.get(peer_id)
        return peer.display if peer else peer_id

    # ================================================================ 状态

    def state(self) -> RoomState:
        """当前状态**快照**（值拷贝：调用方拿着不会被后台线程改出竞态）。"""
        with self._lock:
            return RoomState(
                conn=self._conn,
                room_code=self._room,
                me=self._me,
                nickname=self._nick,
                peers=tuple(self._peers.values()),
                last_error=self._last_error,
                sent_frames=self._sent,
                recv_frames=self._recv,
                send_fails=self._send_fails,
                reconnects=self._reconnects,
                pending_replay=len(self._replay),
            )

    def _set_conn(self, conn: ConnectionState) -> None:
        with self._lock:
            if self._conn is conn:
                return
            self._conn = conn
        log(f"连接态 → {conn.value}")

    def _notify(self, msg: str) -> None:
        """状态回调 + 日志各留一条（禁静默降级：失败必须两处都看得见）。"""
        log(msg)
        cb = self._on_status
        if cb is None:
            return
        try:
            cb(msg)
        except Exception as exc:                       # noqa: BLE001
            log(f"⚠️ on_status 回调抛异常（忽略，房间链路继续）：{type(exc).__name__}: {exc}")
