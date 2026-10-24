"""房间握手与发送队列；生命周期和入站消息保留在 RoomClient。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..session.chatgpt_output import allowed, current
from .model import ConnectionState, log
from .protocol import (FRAME_ERR, FRAME_FINAL, FRAME_HELLO, FRAME_SEG, FRAME_WELCOME,
                       MAX_FRAME_BYTES, ProtocolError, decode_frame, encode_frame,
                       now_ms, should_send_partial)

CONNECT_TIMEOUT_S = 10.0        # TCP+WS 握手超时
WELCOME_TIMEOUT_S = 10.0        # 发完 hello 等 welcome 的超时
REPLAY_MAX = 64                 # 回放表上限（防长时间断线把它撑爆）
SEEN_FINALS_MAX = 512           # final 去重集合上限
STORE_MAX = 256                 # 每 (说话人, 句) 一条的合并存储上限


class _FatalRoomError(RuntimeError):
    """服务端明确拒绝（鉴权失败 / 房间满 / 房间码非法）→ 别再重连了，重连只会一直被拒。"""


@dataclass
class _Pending:
    """一句「还没被服务端 ack」的话，重连后按它补发。"""

    utt: str
    seq: int
    text: str
    lang: str
    is_final: bool
    approval: Any = None
    generation: int = 0
class _RoomTransport:
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

    def publish(self, utt: str, rev: int, text: str, is_final: bool) -> None:
        """把本机 ASR 源文广播到房间（**线程安全**，采集线程可直接调）。

        utt      : 句 id（调用方生成，**重连不要重置**，否则对端没法覆盖同一句）
        rev      : 句内递增版本号（会被抬成严格递增，见 `_next_seq`）
        text     : **全量快照**（不是增量；本类不会替你拼接）
        is_final : 定稿。final **必发**，不受 partial 节流影响

        没连上也不丢：帧留在回放表里，重连后按「final + 每个未完成句的最新 partial」补发。
        """
        approval = current()
        generation = self._approval_generation
        if self._stop_event.is_set() or not allowed(approval, 'mine'):
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
            if (self._stop_event.is_set() or (approval is not None
                    and generation != self._approval_generation)):
                return
            seq = self._next_seq(utt, rev)
            if not is_final:
                prev_text, prev_ms = self._last_partial.get(utt, ("", 0))
                if not should_send_partial(prev_text, prev_ms, snap, now,
                                           self.cfg.partial_min_ms):
                    return                             # 节流：内容没变 / 间隔太短
                self._last_partial[utt] = (snap, now)
            else:
                self._last_partial.pop(utt, None)
            pending = _Pending(utt=utt, seq=seq, text=snap,
                               lang=self.cfg.source_lang, is_final=bool(is_final),
                               approval=approval, generation=generation)
            self._replay[utt] = pending
            self._prune()
        self._send(FRAME_FINAL if is_final else FRAME_SEG,
                   _approval=approval, _pending=pending,
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

    def _send(self, kind: str, *, _approval=None, _pending=None, **fields: Any) -> bool:
        """把一帧交给事件循环异步发出（**不阻塞调用方**，失败只留痕不抛）。"""
        loop, ws = self._loop, self._ws
        if loop is None or ws is None or self._stop_event.is_set():
            self._note_offline(kind)
            return False
        frame = encode_frame(kind, **fields)
        coro = self._ws_send(frame, _approval, _pending)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as exc:                    # 事件循环正在收尾
            coro.close()
            log(f"⚠️ 事件循环已关，{kind} 帧没发出去：{exc}")
            return False
        fut.add_done_callback(self._on_send_done)
        return True

    def _invalidate_approved(self) -> None:
        with self._lock:
            self._approval_generation += 1
            self._replay = {key: p for key, p in self._replay.items() if p.approval is None}

    def _can_send(self, approval, pending=None) -> bool:
        with self._lock:
            if (not allowed(approval, 'mine') or (approval is not None and pending is not None
                    and pending.generation != self._approval_generation)):
                if pending is not None and self._replay.get(pending.utt) is pending:
                    self._replay.pop(pending.utt)
                log('丢弃失效房间候选（stale-result）')
                return False
            return not self._stop_event.is_set()

    async def _ws_send(self, frame: str, approval=None, pending=None) -> None:
        """（事件循环线程）真正发一帧。**每次动态取 `self._ws`** —— 重连会换对象。"""
        with self._lock:
            ws = self._ws
            if not self._can_send(approval, pending):
                return
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
            if not self._can_send(p.approval, p):
                continue
            try:
                frame = encode_frame(FRAME_FINAL if p.is_final else FRAME_SEG,
                                     utt=p.utt, seq=p.seq, text=p.text,
                                     lang=p.lang, final=p.is_final, ts=now_ms())
                if not self._can_send(p.approval, p):
                    continue
                await ws.send(frame)
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
