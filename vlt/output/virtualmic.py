"""虚拟声卡输出：把模型译音（24kHz 单声道 PCM）重采样后写进虚拟声卡，供 VRChat 麦克风拾取。"""
from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time
from typing import Callable

from ..platform import child_env
from ..session.chatgpt_output import allowed, current

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

    devices 参数用于测试注入；为 None 时走**平台层**的设备表（Windows 上它已经把设备
    收敛到 WASAPI 已启用那一套 —— 不然同一块声卡会在 MME/DirectSound 下重复出现，
    而回退链只取**第一条命中**，命中的往往是 MME 那条（44100Hz、延迟最高））。
    """
    from .. import platform

    chain = [p.lower() for p in (patterns or OUTPUT_DEVICE_FALLBACK)]
    devs = devices if devices is not None else platform.device_backend().query_devices()

    for kw in chain:
        for i, d in enumerate(devs):
            if d.get("max_output_channels", 0) > 0 and kw in str(d.get("name", "")).lower():
                # ⚠️ 平台层过滤过 host API，列表下标**不再等于** PortAudio 索引 → 优先 pa_index
                index = int(d.get("pa_index", i))
                return (index, str(d["name"]), int(d.get("default_samplerate", 48000)))
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
        device_fallbacks: "list[int] | tuple[int, ...]" = (),
    ) -> None:
        self._device_index = device_index
        self._open_device_index = device_index      # 实际打开成功的那条（回落时与上面不同）
        #: 首选设备打不开时按序再试的候选（Windows：同名输出设备在其它 host API 下的条目）。
        #  真机事故（2026-10-02）：Voicemeeter 的 WASAPI 端点会 `-9999 Unanticipated host error`
        #  （`WdmSyncIoctl … GLE = 0x490`），而它的 MME 条目能正常打开 —— 有候选就不至于直接判死。
        self._device_fallbacks = [int(i) for i in device_fallbacks if int(i) != device_index]
        self._device_name = device_name
        self._sample_rate = sample_rate
        self._buffer_ms = buffer_ms
        self._max_buffer_ms = max_buffer_ms
        self._on_status = on_status

        self._buf: collections.deque[tuple[bytes, bool, object]] = collections.deque()  # PCM、句尾、核准身份
        self._buf_bytes = 0
        self._head_started = False          # 队首那句是否已经开始播放（开始播的句子不丢）
        self._dropping_sentence = False
        self._lock = threading.Lock()
        self._stream = None
        self._primed = False
        self._last_push_ts = 0.0
        self._bytes_per_ms = sample_rate * 2 * 2 / 1000

    @property
    def device_name(self) -> str:
        return self._device_name

    def open(self) -> bool:
        """打开音频流。失败返回 False 并通过 on_status 报错。

        首选设备（界面/配置选中的那条，通常是 WASAPI 端点）打不开时，按 `device_fallbacks`
        逐个再试（同名设备在别的 host API 下的条目），**每次尝试都留痕**。
        """
        import sounddevice as sd

        last_exc: Exception | None = None
        for i, dev in enumerate([self._device_index, *self._device_fallbacks]):
            try:
                self._stream = sd.RawOutputStream(
                    samplerate=self._sample_rate,
                    channels=2,
                    dtype="int16",
                    device=dev,
                    callback=self._audio_callback,
                    blocksize=int(self._sample_rate * 0.02),
                )
                self._stream.start()
                if i == 0:
                    self._on_status("info",
                                    f"虚拟声卡已打开：#{dev} {self._device_name}")
                else:
                    self._on_status("warn",
                                    f"虚拟声卡 #{self._device_index} 打不开，"
                                    f"已回落到同名设备 #{dev}（{self._device_name}）")
                self._open_device_index = dev
                return True
            except Exception as exc:  # noqa: BLE001 — 换候选再试
                last_exc = exc
                self._stream = None
                self._on_status("warn", f"虚拟声卡 #{dev} 打不开：{exc}")
        self._on_status("error",
                        f"打开虚拟声卡失败（#{self._device_index} {self._device_name}）：{last_exc}")
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
            self._head_started = False
            self._dropping_sentence = False
        self._primed = False

    def push(self, pcm_48k_stereo: bytes) -> None:
        """推入已重采样的 48kHz 立体声 PCM。

        ⚠️ 超限时**只丢整句**，绝不在句子中间切断 —— 用户实测：原来从队首
        一个个 chunk 丢，表现为「上一句 TTS 还没说完就切到了下一句」。
        宁可让缓冲长一点（TTS 落后），也不要让人听到半句话。
        """
        approval = current()
        if not pcm_48k_stereo or not allowed(approval, 'mine'):
            return
        size = getattr(approval, '_audio_bytes', 0)
        complete = type(size) is int and size > 0 and len(pcm_48k_stereo) == size * 4
        if complete and size > 4_320_000:
            log.warning("[virtualmic] 完整候选超过 90 秒上限，未入队")
            return
        with self._lock:
            self._discard_invalid()
            if self._dropping_sentence and not complete:
                return
            self._dropping_sentence = False
            # Admission's exact mono byte count identifies the sealed stereo packet, not partial pushes.
            self._buf.append((pcm_48k_stereo, complete, approval))
            self._buf_bytes += len(pcm_48k_stereo)
            self._last_push_ts = time.monotonic()
            max_bytes = int(self._max_buffer_ms * self._bytes_per_ms)
            hard_bytes = max_bytes * 4          # 兜底硬上限（见下）
            while self._buf_bytes > max_bytes and not (complete and len(self._buf) == 1):
                if self._drop_oldest_whole_sentence():
                    continue
                # 没有可丢的完整句：保留已起播句，仅无句界时退化丢最旧 chunk。
                # 但**必须大声报出来**：这种情况说明句边界判定失效了。
                # 只在超过硬上限（4×）时才这么做，避免正常情况被误切。
                if self._buf_bytes > hard_bytes and len(self._buf) > 1:
                    if self._head_started and any(end for _, end, _ in self._buf):
                        dropped = 0
                        while not self._buf[-1][1]:
                            chunk, _, _ = self._buf.pop()
                            dropped += len(chunk)
                        self._buf_bytes -= dropped
                        if dropped:
                            self._dropping_sentence = True
                            log.warning("[virtualmic] 缓冲超硬上限：丢弃后续未封口句，"
                                        "保留正在播放的句尾，等待新句边界")
                        break
                    chunk, _e, _approval = self._buf.popleft()
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
            self._dropping_sentence = False
            self._discard_invalid()
            if not self._buf:
                self._head_started = False
            if self._buf and not self._buf[-1][1]:
                chunk, _, approval = self._buf[-1]
                self._buf[-1] = (chunk, True, approval)
            self._maybe_prime()

    def _drop_oldest_whole_sentence(self) -> int:
        """丢掉**最旧的一整句**（正在播的那句除外），返回丢弃的字节数。

        调用者必须已持有 self._lock。
        """
        items = list(self._buf)
        ends = [i for i, (_c, e, _approval) in enumerate(items) if e]
        if not ends:
            return 0                                    # 还没有任何完整句子
        # 正在播放的那句 = 第一个句尾之前（若已开始播，就不能动它）
        start = ends[0] + 1 if self._head_started else 0
        nxt = next((i for i in ends if i >= start), None)
        if nxt is None:
            return 0                                    # 后面没有更完整的句子
        dropped = sum(len(c) for c, _e, _approval in items[start:nxt + 1])
        self._buf = collections.deque(items[:start] + items[nxt + 1:])
        self._buf_bytes -= dropped
        if start == 0:
            self._head_started = False
        log.warning("[virtualmic] 缓冲超限：丢弃最旧的一整句 %.0fms（正在播的那句不丢、绝不切句）",
                    dropped / self._bytes_per_ms)
        return dropped

    def _discard_invalid(self) -> None:
        """调用方持锁；撤销剩余 PCM，不让旧队首阻挡新段落。"""
        kept = [item for item in self._buf if allowed(item[2], 'mine')]
        if len(kept) != len(self._buf):
            if not kept or kept[0] is not self._buf[0]:
                self._head_started = False
            self._buf = collections.deque(kept)
            self._buf_bytes = sum(len(item[0]) for item in kept)
            if not kept:
                self._primed = False
                self._dropping_sentence = False

    def _maybe_prime(self) -> None:
        """决定是不是可以起播了。调用者必须已持有 self._lock。

        起播条件：
        1. 攒够 buffer_ms（正常情况，避免开头断续）；
        2. 完整句已封口，不等待后续分片；
        3. **数据已经停更超过 PRIME_TIMEOUT_S** —— 兜底，否则整段译音短于
           buffer_ms 时会永远卡在缓冲里不出声（没人再来解封）。
        """
        self._discard_invalid()
        if self._primed or self._buf_bytes == 0:
            return
        need = int(self._buffer_ms * self._bytes_per_ms)
        idle = time.monotonic() - self._last_push_ts
        if self._buf_bytes >= need or self._buf[-1][1] or idle >= PRIME_TIMEOUT_S:
            self._primed = True

    def _can_drain(self, need_bytes: int) -> bool:
        """调用方持锁：句中短分片等补齐，封句或停更后才排空尾音。"""
        return self._primed and (
            self._buf_bytes >= need_bytes
            or bool(self._buf and self._buf[-1][1])
            or time.monotonic() - self._last_push_ts >= PRIME_TIMEOUT_S
        )

    def _drain(self, need_bytes: int) -> bytes:
        """从队首排空 need_bytes 字节，不足的部分补静音。**调用者必须已持有 self._lock。**

        抽出来是为了让两个平台共用同一段「排空 + 整句记账」逻辑：
        Windows 由 PortAudio 回调驱动、Linux 由写管道线程驱动，但
        「正在播的那句不能丢」「播完才允许整句丢弃」这些坑的记账必须只有一份。
        """
        out = bytearray()
        while len(out) < need_bytes and self._buf:
            chunk, ends, approval = self._buf[0]
            if not allowed(approval, 'mine'):
                self._buf.popleft()
                self._buf_bytes -= len(chunk)
                self._head_started = False
                continue
            self._head_started = True
            take = min(need_bytes - len(out), len(chunk))
            out.extend(chunk[:take])
            if take < len(chunk):
                self._buf[0] = (chunk[take:], ends, approval)
            else:
                self._buf.popleft()
                if ends:
                    self._head_started = False     # 这句播完了，下一句可以整句丢
            self._buf_bytes -= take
        if len(out) < need_bytes:
            out.extend(b"\x00" * (need_bytes - len(out)))
        return bytes(out)
    def _audio_callback(self, outdata: bytearray, frames: int, time_info, status) -> None:
        need_bytes = frames * 2 * 2
        if status:
            log.debug("[virtualmic] callback status: %s", status)
        with self._lock:
            self._maybe_prime()                        # 停更超时也要起播（短译音兜底）
            if not self._can_drain(need_bytes):
                outdata[:] = b"\x00" * need_bytes
                return
            outdata[:] = self._drain(need_bytes)


class PwCatVirtualMic(VirtualMic):
    """Linux 译音输出：把缓冲里的 PCM 写进 `pw-cat` 管道，而不是 PortAudio 回调。

    ## 为什么不走 PortAudio

    Linux 上译音要写进一个**运行时声明出来的** PipeWire 节点
    （`media.class=Audio/Sink/Internal`，见 `vlt/platform/linux.py`）——
    它不是用户事先装好的声卡设备，按名字用 PortAudio 打开既不可靠也没必要。
    `pw-cat --playback --target=<节点名>` 才是 PipeWire 原生的写法，
    而且 `find-defined-target` 保证音频只会进我们自己的节点、不会落到用户的扬声器上。

    ## 复用而不是重造

    抖动缓冲、整句丢弃、起播兜时这些语义全在父类里（那是踩坑换来的），
    这里只把「驱动方式」从 PortAudio 回调换成写管道线程：

        PortAudio：回调每次要恰好填满 frames 字节 → 不够就补静音
        pw-cat   ：写线程每次推 chunk 字节     → 不够就补静音（同样的语义）

    ⚠️ `--raw` 不能省：不加的话 pw-cat 会用 libsndfile 解析容器格式，
    实测报 `sndfile: failed to open audio file "-": Format not recognised` 且**根本没播出去**。
    """

    def __init__(self, target: str, *, sample_rate: int = 48000,
                 buffer_ms: int = 300, max_buffer_ms: int = 2000,
                 on_status: Callable[[str, str], None] = lambda *_a: None,
                 provider: Callable[[int], bytes] | None = None) -> None:
        super().__init__(device_index=0, device_name=target,
                         sample_rate=sample_rate, buffer_ms=buffer_ms,
                         max_buffer_ms=max_buffer_ms, on_status=on_status)
        self._target = target
        self._chunk = int(sample_rate * 0.02) * 2 * 2      # 20ms 立体声 s16le（对齐 PortAudio 的 blocksize）
        # ★ provider 模式（麦克风代理用）：每次如实产出 chunk 字节，**不做起播兜时**。
        #   父类那套 `_maybe_prime()` 是给 TTS 用的（先攒够 buffer_ms 才出声，避免半句起播）；
        #   而原声直通要**立刻出声**（低延迟），攒 300ms 只会平白加延迟。
        #   默认 None = 现有 TTS 语义**一字不变**。
        self._provider = provider
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._closed = False        # close() 幂等用；父类没有这个标志，得自己初始化

    def open(self) -> bool:
        """拉起 `pw-cat` 并启动写线程。失败返回 False 并报错（调用方据此禁用这条腿）。"""
        argv = [
            "pw-cat", "--playback", "--raw",
            f"--target={self._target}",
            "--format=s16", f"--rate={self._sample_rate}", "--channels=2",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE,
                                          env=child_env())
        except Exception as exc:  # noqa: BLE001
            self._on_status("error", f"启动 pw-cat 失败（{self._target}）：{exc}")
            return False
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True,
                                        name="vlt-pwcat-writer")
        self._writer.start()
        if self._provider is None:
            self._on_status("info", f"译音输出已接到虚拟声卡节点：{self._target}")
        else:
            # provider 模式 = 麦克风代理：文案带占位符，交给界面层查 i18n 词条
            # （见 `gui._on_proxy_status`；日志仍打中文原文）。
            self._on_status("info", "麦克风代理输出已接到虚拟声卡节点：{target}",
                            target=self._target)
        return True

    def _writer_loop(self) -> None:
        """按 PortAudio 回调的语义持续喂数据：起播前补静音，起播后排空缓冲（不足补静音）。

        `provider` 模式下（麦克风代理）跳过起播兜时：每次直接取 `chunk` 字节如实写出，
        由 provider 自己保证「不够就补静音」—— 与 `MicProxy._out_callback` 同一口径。
        """
        assert self._proc is not None and self._proc.stdin is not None
        try:
            while not self._stop.is_set():
                if self._provider is not None:
                    data = self._provider(self._chunk)
                else:
                    with self._lock:
                        self._maybe_prime()
                        if self._can_drain(self._chunk):
                            data = self._drain(self._chunk)
                        else:
                            data = b"\x00" * self._chunk      # 还没攒够 → 出静音（与回调一致）
                try:
                    self._proc.stdin.write(data)
                    self._proc.stdin.flush()
                except (BrokenPipeError, ValueError, OSError) as exc:
                    log.warning("[virtualmic] pw-cat 管道断了（%s）→ 停止喂数据",
                                type(exc).__name__)
                    break
        except Exception as exc:  # noqa: BLE001 — 写线程绝不能把异常抛到主线程
            log.warning("[virtualmic] 写线程退出：%s: %s", type(exc).__name__, exc)

    def close(self) -> None:
        """幂等。顺序：停写线程 → join → 才关 pw-cat（与 loopback 采集侧同一条纪律）。"""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        th = self._writer
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
            if th.is_alive():
                log.warning("[virtualmic] 写线程未在 2s 内退出，仍继续关闭 pw-cat")
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            for stream in (proc.stderr,):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self._buf.clear()
            self._buf_bytes = 0
            self._head_started = False
            self._dropping_sentence = False
        self._primed = False

    def stderr_tail(self) -> str:
        """pw-cat 的 stderr（排查用；进程结束后才读得到）。"""
        proc = self._proc
        if proc is None or proc.stderr is None or proc.poll() is None:
            return ""
        try:
            return proc.stderr.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""
