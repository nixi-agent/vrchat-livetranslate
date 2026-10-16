"""chatbox 输出：OSC /chatbox/input。

硬约束（均已核实，见 notes/VRChat同传-阿里实时同传接入规格.md 与 chatbox 调研笔记）：
- bool 参数必须走 OSC 类型标签 `T`/`F`，**不能发 int32 payload**，否则 VRChat 会弹输入框
- 单条 **144 字符** + **最多 9 行**
- 限流是**漏桶 5 条 / 5 秒**，超发**静默丢弃**（没有回告）→ 必须自己做令牌桶
- 目标 `127.0.0.1:9000`（VRChat 的 OSC 接收端口）
"""
from __future__ import annotations

import socket
import threading
import time
from collections import deque

from pythonosc.osc_message_builder import OscMessageBuilder
from ..session.chatgpt_output import allowed, current

ADDRESS = "/chatbox/input"


class TokenBucket:
    """漏桶：window_s 秒内最多 capacity 条，另加最小间隔避免连发。"""

    def __init__(self, capacity: int = 5, window_s: float = 5.0, min_gap_s: float = 0.4) -> None:
        self.capacity = capacity
        self.window_s = window_s
        self.min_gap_s = min_gap_s
        self._stamps: deque[tuple[float, object]] = deque()
        self._lock = threading.Lock()
        self.dropped = 0

    def allow(self, now: float | None = None) -> bool:
        return self.reserve(now) is not None

    def reserve(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._stamps and now - self._stamps[0][0] > self.window_s:
                self._stamps.popleft()
            if ((self._stamps and now - self._stamps[-1][0] < self.min_gap_s)
                    or len(self._stamps) >= self.capacity):
                self.dropped += 1
                return None
            token = object()
            self._stamps.append((now, token))
            return token

    def refund(self, token):
        with self._lock:
            for index, (_, reserved) in enumerate(self._stamps):
                if reserved is token:
                    del self._stamps[index]
                    break


class Chatbox:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000, max_chars: int = 144,
                 max_lines: int = 9, bucket: TokenBucket | None = None,
                 notification_sound: bool = True, dry_run: bool = False) -> None:
        self.host, self.port = host, port
        self.max_chars, self.max_lines = max_chars, max_lines
        self.bucket = bucket or TokenBucket()
        self.notification_sound = notification_sound
        self.dry_run = dry_run
        self._sock: socket.socket | None = None
        self.sent_ok = 0
        self.sent_dropped = 0        # 增量被限流丢弃（可弃）
        self._pending: list[tuple] = []  # 文本与核准 closure；消费前再次检查
        self.last_datagram: bytes | None = None

    def _ensure_sock(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._sock

    @staticmethod
    def build_datagram(text: str, notification_sound: bool = True) -> bytes:
        """构造 /chatbox/input 报文：typetags 必须是 `,sTT`（bool 走类型标签，无 payload）。"""
        b = OscMessageBuilder(address=ADDRESS)
        b.add_arg(text)
        b.add_arg(True)                       # sendImmediately = True
        if notification_sound:
            b.add_arg(True)                   # play notification SFX
        return b.build()._dgram

    def sanitize(self, text: str) -> str:
        text = " ".join(text.split())         # 折叠所有空白/换行
        if len(text) > self.max_chars:
            text = text[-self.max_chars:]
        # 行数受 max_lines 约束；折叠后只有 1 行，故此处天然满足 9 行上限
        return text

    def send(self, text: str, is_final: bool = False) -> bool:
        approval = current()
        if not allowed(approval):
            return False
        text = self.sanitize(text)
        if not text:
            return False
        token = self.bucket.reserve()
        if token is None:
            if is_final:
                # 最终版必达：不丢，入队等限流窗口（由 app 的 pump 周期补发）
                item = (text, approval)
                if item not in self._pending:
                    self._pending.append(item)
                print(f"[chatbox] ⏳ 限流暂缓，最终版入队（pending={len(self._pending)}）: {text[:40]}…")
            else:
                self.sent_dropped += 1
                print(f"[chatbox] ⚠️ 限流丢弃增量（bucket dropped={self.bucket.dropped}）: {text[:40]}…")
            return False
        return self._dispatch(text, is_final, approval, token)

    def flush_pending(self, newest_first: bool = False) -> int:
        """把暂存的最终版逐条补发（受同一个令牌桶约束）。返回本次成功条数。

        `newest_first=True` 只给停止收尾用：那儿的墙钟预算常常只够放一两条，
        按 FIFO 就会先送最旧的那句、把用户**刚说完**的那句挤掉。
        """
        sent = 0
        while self._pending:
            index = -1 if newest_first else 0
            if not allowed(self._pending[index][1]):
                self._pending.pop(index)
                continue
            token = self.bucket.reserve()
            if token is None:
                break
            text, approval = self._pending.pop(index)
            sent += int(self._dispatch(text, True, approval, token))
        return sent

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _dispatch(self, text: str, is_final: bool, approval=None, token=None) -> bool:
        if not allowed(approval):
            self.bucket.refund(token)
            return False
        dgram = self.build_datagram(text, self.notification_sound)
        self.last_datagram = dgram
        if self.dry_run:
            self.sent_ok += 1
            print(f"[chatbox][dry-run] {len(dgram)}B final={is_final} | {text}")
            return True
        try:
            self._ensure_sock().sendto(dgram, (self.host, self.port))
        except Exception as exc:  # noqa: BLE001
            print(f"[chatbox] ❌ 发送失败：{type(exc).__name__}: {exc}")
            return False
        self.sent_ok += 1
        tag = "最终版" if is_final else "增量"
        print(f"[chatbox] → {tag} ({len(dgram)}B) {text[:60]}")
        return True

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
