"""Shared passthrough and translated audio buffers; device lifecycle stays in MicProxy."""
from __future__ import annotations
import collections
import threading
from typing import TYPE_CHECKING
from .virtualmic import VirtualMic

# shortcut: runtime type reflection needs micproxy globals; revisit if a caller uses get_type_hints.
if TYPE_CHECKING:
    from .micproxy import MicProxy

class _Ring:
    """直通环形缓冲：容量满时**丢最旧**（保持低延迟），与译音腿「宁慢不切句」语义相反。"""

    def __init__(self, cap_bytes: int) -> None:
        self._dq: collections.deque[bytes] = collections.deque()
        self._n = 0
        self._cap = max(1, int(cap_bytes))
        self._lock = threading.Lock()

    def set_cap(self, cap_bytes: int) -> None:
        with self._lock:
            self._cap = max(1, int(cap_bytes))
            self._trim()

    def _trim(self) -> None:
        # 至少留一块（len>1 才丢）：避免把刚推进来、还没播的那块也削掉。
        while self._n > self._cap and len(self._dq) > 1:
            old = self._dq.popleft()
            self._n -= len(old)

    def push(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._dq.append(data)
            self._n += len(data)
            self._trim()

    def drain(self, need: int) -> tuple[bytes, bool]:
        """排空 need 字节，不足补静音。返回 (数据, 是否欠载)。"""
        with self._lock:
            out = bytearray()
            while len(out) < need and self._dq:
                c = self._dq[0]
                take = min(need - len(out), len(c))
                out.extend(c[:take])
                if take < len(c):
                    self._dq[0] = c[take:]
                else:
                    self._dq.popleft()
                self._n -= take
            under = len(out) < need
            if under:
                out.extend(b"\x00" * (need - len(out)))
            return bytes(out), under

    def available(self) -> int:
        """当前缓冲字节数（启动等「攒到第一块」时用，见 `MicProxy._prime_ring`）。"""
        with self._lock:
            return self._n

    def clear(self) -> None:
        with self._lock:
            self._dq.clear()
            self._n = 0


class _TranslatedBuffer(VirtualMic):
    """只借用 `VirtualMic` 的抖动缓冲/整句丢弃/起播逻辑，**绝不开自己的音频流**。

    虚拟声卡输出流由 `MicProxy` 独占持有（只有一条）；本类只当「译音档的数据缓冲」用，
    输出回调在 translated 档时调 `drain_block()` 取数据。
    """

    def open(self) -> bool:              # buffer-only：父类会开流，这里禁掉
        return True

    def drain_block(self, need_bytes: int) -> bytes | None:
        """取一块译音数据，尾块补静音；未起播/无数据返回 None。"""
        with self._lock:
            self._maybe_prime()
            if not self._can_drain(need_bytes) or self._buf_bytes == 0:
                return None
            return self._drain(need_bytes)

    def reset(self) -> None:
        """清空缓冲并复位起播状态（切换档位时用：切过去立刻是新内容，不念旧账）。"""
        with self._lock:
            self._buf.clear()
            self._buf_bytes = 0
            self._primed = False
            self._head_started = False
            self._dropping_sentence = False

    def set_buffer_ms(self, ms: int) -> None:
        with self._lock:
            self._buffer_ms = int(ms)


class TranslatedSink:
    """引擎侧看到的「译音输出」鸭子类型垫片：内部转发到 MicProxy 的译音抖动缓冲。

    引擎只认 `push(pcm48)` / `end_sentence()` / `device_name` / `close()`，不感知 MicProxy。
    `close()` 是**空操作**——缓冲归代理管，引擎停翻译时不能把它关掉。
    """

    def __init__(self, proxy: "MicProxy") -> None:
        self._p = proxy

    @property
    def device_name(self) -> str:
        dev = self._p._out_device
        return dev[1] if dev else "proxy"

    @property
    def opened(self) -> bool:
        return self._p.opened

    @property
    def mic_reference_db(self) -> float | None:
        return self._p.mic_reference_db

    def push(self, pcm_48k_stereo: bytes) -> None:
        buf = self._p._translated
        if buf is not None:
            buf.push(pcm_48k_stereo)

    def end_sentence(self) -> None:
        buf = self._p._translated
        if buf is not None:
            buf.end_sentence()

    def close(self) -> None:             # 归代理管，引擎不关
        pass
