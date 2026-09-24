"""节流合并器：把会话层的高频增量，压成「首条立即发 + 每 2 秒一次快照 + 句末必刷最终版」。

为什么要合并：chatbox 有 5 条/5 秒的漏桶限流，且气泡内容是被**整体替换**的，
所以推给它的应该是「当前最完整的译文快照」，而不是碎片增量。
"""
from __future__ import annotations

import time
from typing import Callable

from ..session.base import TextDelta

Sink = Callable[[str, bool], None]      # (text, is_final)


class Merger:
    def __init__(self, sink: Sink, interval_s: float = 2.0, carry_over: bool = False,
                 max_chars: int = 144) -> None:
        self.sink = sink
        self.interval_s = interval_s
        self.carry_over = carry_over
        self.max_chars = max_chars
        self._last_sent: str = ""
        self._last_sent_at: float = 0.0
        self._final_sent_for_current = False
        self._carry: str = ""            # 上一段最终译文（carry_over 时作为前缀）
        # 埋点
        self.sent_count = 0
        self.skipped_identical = 0
        self.first_sent_at: float | None = None

    def start_utterance(self) -> None:
        """新一段响应开始（response.created）。"""
        self._final_sent_for_current = False

    def push(self, delta: TextDelta) -> None:
        text = delta.display                     # confirmed + pending（预测部分会被替换，不累加）
        if not text:
            return
        now = time.monotonic()

        if delta.is_final:
            self._carry = text
            self._emit(text, is_final=True, now=now)
            self._final_sent_for_current = True
            return

        # 首个 delta 立即发；之后按 interval_s 节流
        if self._last_sent_at == 0.0 or (now - self._last_sent_at) >= self.interval_s:
            self._emit(text, is_final=False, now=now)

    def _emit(self, text: str, is_final: bool, now: float) -> None:
        payload = self._compose(text, is_final)
        if not payload:
            return
        # 终版必达：即使与上次相同也要发（用户要求「翻译完成后再最后刷新一次」）；
        # 但非终版若内容没变就不浪费限流配额。
        if not is_final and payload == self._last_sent:
            self.skipped_identical += 1
            self._last_sent_at = now       # 仍然重置窗口，避免下一拍重复判定
            return
        self.sink(payload, is_final)
        self.sent_count += 1
        if self.first_sent_at is None:
            self.first_sent_at = time.perf_counter()
        self._last_sent = payload
        self._last_sent_at = now

    def _compose(self, text: str, is_final: bool) -> str:
        prefix = self._carry if (self.carry_over and not is_final) else ""
        if prefix and prefix != text and not text.startswith(prefix):
            text = f"{prefix} {text}"
        text = " ".join(text.split())          # 折叠空白，避免气泡里出现空行
        if len(text) > self.max_chars:
            text = text[-self.max_chars:]      # 超长时保留最新内容（同传场景后文更重要）
        return text

    def reset(self) -> None:
        self._last_sent = ""
        self._last_sent_at = 0.0
        self._final_sent_for_current = False
