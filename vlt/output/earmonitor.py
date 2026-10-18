"""耳返（sidetone）：把本程序送进虚拟声卡的译音**同时**放给用户自己听。

## 为什么单独一路输出

译音只往虚拟声卡里灌（VRChat 的麦克风），用户自己是听不到的 —— 想确认「对方听到的
是什么样」「这句译得对不对」只能靠别人反馈。耳返就是把同一段 PCM 再送到一个**用户选的
输出设备**（通常是耳机），音量独立可调，不影响对方听到的响度。

## 数据从哪来

唯一 tap 点是 `micproxy.MicProxy._drain_translated()`：那是**实际发给虚拟声卡**的那批
字节（已过 `output.audio.level` 的增益 + 限幅），同一时刻再给耳返一份 —— 所以「你听到的」
与「对方听到的」不只是同一段音频，而是**同一条播放时间轴**。

⚠️ 不要在 `TranslatedSink.push()` 处 tap：那是 TTS 突发到达的入口，一个 delta 常有几百 ms，
而本模块的缓冲只有 120ms（满丢最旧）→ 会把**正在播的句子截断**（真机症状：
「上一句还没播完就开始播下一句」，而进游戏那一路完全正常）。

## 与采集回路的关系（重要）

耳返放出来的声音，如果落在「VRChat 音频」那个采集端点上，就会本程序自己再听一遍
（端点 loopback）→ 再翻一遍。两种处置（`vlt/engine.effective_loopback_mode`）：

* 采集方式已经是 `process`（只抓 VRChat 进程）→ 天然隔离，耳返直接可用；
* 采集方式还是 `endpoint` → **自动切到 process**（能切的话）并留痕；切不了
  （旧系统）就把耳返关掉并明说原因，绝不静默留着一个会自我回译的回路。

声学回路（耳返从外放漏进真实麦克风）不在这里处理：那是物理路径，需要闸门/AEC，
见 `docs/dev/` 的已知限制。

## 失败口径

打开设备失败（端点被占、WASAPI 怪错误）→ 只禁用耳返：日志一行、状态栏一条 warn，
**翻译与译音输出完全不受影响**。测试/自检进程里一律不构造（会真开音频设备）。
"""
from __future__ import annotations

import logging
import threading

from .. import platform

log = logging.getLogger(__name__)

#: 耳返自己的抖动缓冲上限（毫秒）。比虚拟声卡的 `buffer_ms` 小得多 —— 监听要跟手，
#: 允许偶尔欠载（补静音）比让用户听到「慢半拍」更可接受。
DEFAULT_BUFFER_MS = 120
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2          # int16 立体声
BLOCK_FRAMES = 480                      # 10ms


class EarMonitor:
    """一路独立的监听输出。`push()` 线程安全，回调只做「取数据 + 乘音量」。"""

    label = "monitor"

    def __init__(self, device_name: str = "", volume: float = 70.0,
                 on_status=None) -> None:      # noqa: ANN001
        self._device_name = str(device_name or "")
        self._volume = self._clamp(volume)
        self._on_status = on_status or (lambda *_a: None)
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._cap_bytes = int(SAMPLE_RATE * BYTES_PER_FRAME * DEFAULT_BUFFER_MS / 1000)
        self._stream = None
        self._opened = False
        self._dropped_chunks = 0
        self._real_name = ""

    # ------------------------------------------------------------ 对外
    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def device_name(self) -> str:
        return self._real_name or self._device_name

    @property
    def volume(self) -> float:
        return self._volume

    def push(self, pcm_48k_stereo: bytes) -> None:
        """送一段 PCM 进监听缓冲。**不阻塞**：满了就丢最旧的（宁可丢监听，不拖引擎）。"""
        if not self._opened or not pcm_48k_stereo:
            return
        with self._lock:
            self._buf.extend(pcm_48k_stereo)
            over = len(self._buf) - self._cap_bytes
            if over > 0:
                del self._buf[:over]
                self._dropped_chunks += 1

    def set_volume(self, volume: float) -> None:
        self._volume = self._clamp(volume)

    def open(self) -> bool:
        """打开监听输出。失败返回 False（只影响耳返）。"""
        if self._opened:
            return True
        if not platform.IS_WINDOWS:
            print("[monitor] 耳返暂时只在 Windows 上实现（Linux 侧留空实现）", flush=True)
            self._on_status("warn", "耳返暂不支持当前平台")
            return False
        try:
            import sounddevice as sd
        except Exception as exc:                       # noqa: BLE001
            print(f"[monitor] 打开耳返失败（sounddevice 不可用）：{exc}", flush=True)
            return False

        candidates: list[int | None] = [None]
        if self._device_name:
            try:
                from ..devices import resolve_device_name
                idx = resolve_device_name(self._device_name, "output")
            except Exception:                          # noqa: BLE001
                idx = None
            if idx is None:
                print(f"[monitor] ⚠️ 找不到耳返设备 {self._device_name!r} → 用系统默认输出",
                      flush=True)
                candidates = [None]
            else:
                candidates = [int(idx)]
                for fb in platform.output_device_fallbacks(self._device_name, exclude=idx):
                    candidates.append(int(fb))

        last_exc: Exception | None = None
        for cand in candidates:
            try:
                stream = sd.OutputStream(
                    device=cand, samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype="int16", blocksize=BLOCK_FRAMES, latency="low",
                    callback=self._callback)
                stream.start()
                self._stream = stream
                self._opened = True
                self._real_name = str(sd.query_devices(cand)["name"]) if cand is not None \
                    else str(sd.query_devices(kind="output")["name"])
                print(f"[monitor] 耳返已打开：{self._real_name!r}（音量 {self._volume:.0f}%）",
                      flush=True)
                return True
            except Exception as exc:                   # noqa: BLE001 — 逐个候选试
                last_exc = exc
                print(f"[monitor] 耳返设备 #{cand} 打不开：{exc}", flush=True)
        print(f"[monitor] ❌ 耳返打开失败 → 已禁用（翻译/译音输出不受影响）：{last_exc}",
              flush=True)
        self._on_status("warn", "耳返设备打不开，已禁用耳返（其余功能不受影响）")
        return False

    def close(self) -> None:
        stream, self._stream = self._stream, None
        self._opened = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:                          # noqa: BLE001
                pass
            print("[monitor] 耳返已关闭", flush=True)
        with self._lock:
            self._buf.clear()

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _clamp(volume: float) -> float:
        try:
            v = float(volume)
        except (TypeError, ValueError):
            v = 70.0
        return max(0.0, min(100.0, v))

    def _callback(self, outdata, frames, time_info, status) -> None:   # noqa: ANN001, ARG002
        need = int(frames) * BYTES_PER_FRAME
        with self._lock:
            take = min(len(self._buf), need)
            chunk = bytes(self._buf[:take])
            del self._buf[:take]
        if take < need:
            chunk += b"\x00" * (need - take)
        gain = self._volume / 100.0
        if gain >= 1.0:
            outdata[:] = self._as_frames(chunk)
            return
        import numpy as np
        a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) * gain
        np.clip(a, -32768.0, 32767.0, out=a)
        outdata[:] = a.astype(np.int16).reshape(-1, CHANNELS)

    @staticmethod
    def _as_frames(chunk: bytes):
        import numpy as np
        return np.frombuffer(chunk, dtype=np.int16).reshape(-1, CHANNELS)
