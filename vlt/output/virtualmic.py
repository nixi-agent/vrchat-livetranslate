"""虚拟声卡输出：把模型译音（24kHz 单声道 PCM）重采样后写进虚拟声卡，供 VRChat 麦克风拾取。"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

OUTPUT_DEVICE_FALLBACK = ["voicemeeter input", "voicemeeter aux input", "cable input", "vb-audio"]

# 数据停更多久就强制起播（秒）。兜底用：整段译音短于 buffer_ms 时，
# 光等"攒够"会永远不出声，没人再来解封。
PRIME_TIMEOUT_S = 0.35


def resample_24k_mono_to_48k_stereo(pcm: bytes) -> bytes:
    """24kHz 单声道 s16le → 48kHz 立体声 s16le（2 倍线性插值 + 单声道复制到双声道）。

    每个输入样本产生 4 个输出样本（2 倍升采样 × 2 声道），
    输出字节数 == len(pcm) * 4。

    用 numpy 向量化：实测 8.56s 音频 2ms（纯 Python 循环要 160ms），
    输出逐字节一致。这台机器同时还在跑 VRChat，音频路径上没必要白烧 CPU。
    """
    import numpy as np

    if len(pcm) % 2 != 0:
        pcm = pcm[: len(pcm) - (len(pcm) % 2)]
    n_samples = len(pcm) // 2
    if n_samples == 0:
        return b""

    a = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
    if n_samples == 1:
        up = np.array([a[0], a[0]], dtype=np.int16)
    else:
        mid = (a[:-1] + a[1:]) // 2          # 相邻样本中点，等价于 2 倍线性插值
        up = np.empty(n_samples * 2, dtype=np.int16)
        up[0::2] = a
        up[1:-1:2] = mid
        up[-1] = a[-1]
    return np.repeat(up, 2).astype(np.int16).tobytes()   # 单声道 → 立体声


def pick_output_device(
    patterns: list[str] | None = None,
    devices: list[dict] | None = None,
) -> tuple[int, str, int] | None:
    """按名称回退链找输出设备。返回 (index, name, sample_rate) 或 None。

    devices 参数用于测试注入；为 None 时实时查询 sounddevice。
    """
    import sounddevice as sd

    chain = [p.lower() for p in (patterns or OUTPUT_DEVICE_FALLBACK)]
    devs = devices if devices is not None else list(sd.query_devices())

    for kw in chain:
        for i, d in enumerate(devs):
            if d.get("max_output_channels", 0) > 0 and kw in str(d.get("name", "")).lower():
                return (i, str(d["name"]), int(d.get("default_samplerate", 48000)))
    return None


class VirtualMic:
    """常开音频流 + 抖动缓冲 + 欠载补静音。

    模型回调（引擎线程）通过 push() 推入重采样后的 PCM；
    PortAudio 回调（音频线程）从 deque 取数据，取不到就写静音。
    """

    def __init__(
        self,
        device_index: int,
        device_name: str,
        sample_rate: int = 48000,
        buffer_ms: int = 300,
        max_buffer_ms: int = 2000,
        on_status: Callable[[str, str], None] = lambda *_a: None,
    ) -> None:
        self._device_index = device_index
        self._device_name = device_name
        self._sample_rate = sample_rate
        self._buffer_ms = buffer_ms
        self._max_buffer_ms = max_buffer_ms
        self._on_status = on_status

        self._buf: collections.deque[tuple[bytes, bool]] = collections.deque()   # (chunk, 是否句尾)
        self._buf_bytes = 0
        self._head_started = False          # 队首那句是否已经开始播放（开始播的句子不丢）
        self._lock = threading.Lock()
        self._stream = None
        self._primed = False
        self._last_push_ts = 0.0
        self._bytes_per_ms = sample_rate * 2 * 2 / 1000

    @property
    def device_name(self) -> str:
        return self._device_name

    def open(self) -> bool:
        """打开音频流。失败返回 False 并通过 on_status 报错。"""
        try:
            import sounddevice as sd

            self._stream = sd.RawOutputStream(
                samplerate=self._sample_rate,
                channels=2,
                dtype="int16",
                device=self._device_index,
                callback=self._audio_callback,
                blocksize=int(self._sample_rate * 0.02),
            )
            self._stream.start()
            self._on_status("info", f"虚拟声卡已打开：#{self._device_index} {self._device_name}")
            return True
        except Exception as exc:
            self._on_status("error", f"打开虚拟声卡失败（#{self._device_index} {self._device_name}）：{exc}")
            return False

    def close(self) -> None:
        """关闭音频流（幂等）。"""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._buf.clear()
            self._buf_bytes = 0
        self._primed = False

    def push(self, pcm_48k_stereo: bytes) -> None:
        """推入已重采样的 48kHz 立体声 PCM。

        ⚠️ 超限时**只丢整句**，绝不在句子中间切断 —— 用户实测：原来从队首
        一个个 chunk 丢，表现为「上一句 TTS 还没说完就切到了下一句」。
        宁可让缓冲长一点（TTS 落后），也不要让人听到半句话。
        """
        if not pcm_48k_stereo:
            return
        with self._lock:
            self._buf.append((pcm_48k_stereo, False))
            self._buf_bytes += len(pcm_48k_stereo)
            self._last_push_ts = time.monotonic()
            max_bytes = int(self._max_buffer_ms * self._bytes_per_ms)
            hard_bytes = max_bytes * 4          # 兜底硬上限（见下）
            while self._buf_bytes > max_bytes:
                if self._drop_oldest_whole_sentence():
                    continue
                # 一句完整的都没有（异常情况：句尾标记一直没来）→ 退化丢最旧 chunk，
                # 但**必须大声报出来**：这种情况说明句边界判定失效了。
                # 只在超过硬上限（4×）时才这么做，避免正常情况被误切。
                if self._buf_bytes > hard_bytes and len(self._buf) > 1:
                    chunk, _e = self._buf.popleft()
                    self._buf_bytes -= len(chunk)
                    log.warning("[virtualmic] 没有任何完整句子可丢、缓冲已超硬上限 %.0fms："
                                "退化丢弃最旧 %.0fms 的 chunk（句尾标记一直没来？）",
                                hard_bytes / self._bytes_per_ms,
                                len(chunk) / self._bytes_per_ms)
                    continue
                break
            self._maybe_prime()

    def end_sentence(self) -> None:
        """把刚推完的音频封成**一句**（打句尾标记）。

        引擎在两个响应之间的静默间隔处调用。有了句尾标记，缓冲超限时才能整句丢弃；
        另外短句封口后可以立刻起播，不必干等 buffer_ms。
        """
        with self._lock:
            if self._buf and not self._buf[-1][1]:
                chunk, _ = self._buf[-1]
                self._buf[-1] = (chunk, True)
            self._maybe_prime()

    def _drop_oldest_whole_sentence(self) -> int:
        """丢掉**最旧的一整句**（正在播的那句除外），返回丢弃的字节数。

        调用者必须已持有 self._lock。
        """
        items = list(self._buf)
        ends = [i for i, (_c, e) in enumerate(items) if e]
        if not ends:
            return 0                                    # 还没有任何完整句子
        # 正在播放的那句 = 第一个句尾之前（若已开始播，就不能动它）
        start = ends[0] + 1 if self._head_started else 0
        nxt = next((i for i in ends if i >= start), None)
        if nxt is None:
            return 0                                    # 后面没有更完整的句子
        dropped = sum(len(c) for c, _e in items[start:nxt + 1])
        self._buf = collections.deque(items[:start] + items[nxt + 1:])
        self._buf_bytes -= dropped
        if start == 0:
            self._head_started = False
        log.warning("[virtualmic] 缓冲超限：丢弃最旧的一整句 %.0fms（正在播的那句不丢、绝不切句）",
                    dropped / self._bytes_per_ms)
        return dropped

    def _maybe_prime(self) -> None:
        """决定是不是可以起播了。调用者必须已持有 self._lock。

        两种起播条件：
        1. 攒够 buffer_ms（正常情况，避免开头断续）；
        2. **数据已经停更超过 PRIME_TIMEOUT_S** —— 兜底，否则整段译音短于
           buffer_ms 时会永远卡在缓冲里不出声（没人再来解封）。
        """
        if self._primed or self._buf_bytes == 0:
            return
        need = int(self._buffer_ms * self._bytes_per_ms)
        idle = time.monotonic() - self._last_push_ts
        if self._buf_bytes >= need or idle >= PRIME_TIMEOUT_S:
            self._primed = True

    def _audio_callback(self, outdata: bytearray, frames: int, time_info, status) -> None:
        need_bytes = frames * 2 * 2
        if status:
            log.debug("[virtualmic] callback status: %s", status)
        with self._lock:
            self._maybe_prime()                        # 停更超时也要起播（短译音兜底）
            if not self._primed or self._buf_bytes < need_bytes:
                outdata[:] = b"\x00" * need_bytes
                return
            out = bytearray()
            while len(out) < need_bytes and self._buf:
                chunk, ends = self._buf[0]
                self._head_started = True
                take = min(need_bytes - len(out), len(chunk))
                out.extend(chunk[:take])
                if take < len(chunk):
                    self._buf[0] = (chunk[take:], ends)
                else:
                    self._buf.popleft()
                    if ends:
                        self._head_started = False     # 这句播完了，下一句可以整句丢
                self._buf_bytes -= take
            if len(out) < need_bytes:
                out.extend(b"\x00" * (need_bytes - len(out)))
            outdata[:] = out
