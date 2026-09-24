"""chatbox 输出：OSC /chatbox/input。

硬约束（均已核实，见 notes/VRChat同传-阿里实时同传接入规格.md 与 chatbox 调研笔记）：
- bool 参数必须走 OSC 类型标签 `T`/`F`，**不能发 int32 payload**，否则 VRChat 会弹输入框
- 单条 **144 字符** + **最多 9 行**
- 限流是**漏桶 5 条 / 5 秒**，超发**静默丢弃**（没有回告）→ 必须自己做令牌桶
- 目标 `127.0.0.1:9000`（VRChat 的 OSC 接收端口）
"""
from __future__ import annotations

import socket
import time
from collections import deque

from pythonosc.osc_message_builder import OscMessageBuilder

ADDRESS = "/chatbox/input"


class TokenBucket:
    """漏桶：window_s 秒内最多 capacity 条，另加最小间隔避免连发。"""

    def __init__(self, capacity: int = 5, window_s: float = 5.0, min_gap_s: float = 0.4) -> None:
        self.capacity = capacity
        self.window_s = window_s
        self.min_gap_s = min_gap_s
        self._stamps: deque[float] = deque()
        self.dropped = 0

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self._stamps and now - self._stamps[0] > self.window_s:
            self._stamps.popleft()
        if self._stamps and (now - self._stamps[-1]) < self.min_gap_s:
            self.dropped += 1
            return False
        if len(self._stamps) >= self.capacity:
            self.dropped += 1
            return False
        self._stamps.append(now)
        return True


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
        self._pending: list[str] = []  # 最终版暂存队列（不可弃，等窗口到了补发）
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
        text = self.sanitize(text)
        if not text:
            return False
        if not self.bucket.allow():
            if is_final:
                # 最终版必达：不丢，入队等限流窗口（由 app 的 pump 周期补发）
                if text not in self._pending:
                    self._pending.append(text)
                print(f"[chatbox] ⏳ 限流暂缓，最终版入队（pending={len(self._pending)}）: {text[:40]}…")
            else:
                self.sent_dropped += 1
                print(f"[chatbox] ⚠️ 限流丢弃增量（bucket dropped={self.bucket.dropped}）: {text[:40]}…")
            return False
        return self._dispatch(text, is_final)

    def flush_pending(self) -> int:
        """把暂存的最终版逐条补发（受同一个令牌桶约束）。返回本次成功条数。"""
        sent = 0
        while self._pending:
            if not self.bucket.allow():
                break
            text = self._pending.pop(0)
            self._dispatch(text, True)
            sent += 1
        return sent

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _dispatch(self, text: str, is_final: bool) -> bool:
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
