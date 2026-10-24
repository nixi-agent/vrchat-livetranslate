"""耳返（sidetone）：把本程序送进虚拟声卡的译音**同时**放给用户自己听。

## 为什么单独一路输出

译音只往虚拟声卡里灌（VRChat 的麦克风），用户自己是听不到的 —— 想确认「对方听到的
是什么样」「这句译得对不对」只能靠别人反馈。耳返就是把同一段 PCM 再送到一个**用户选的
输出设备**（通常是耳机），音量独立可调，不影响对方听到的响度。

## 数据从哪来

唯一 tap 点是 `micproxy.MicProxy._drain_translated()`：那是**实际发给虚拟声卡**的那批
字节（已过 `output.audio.level` 的增益 + 限幅），同一时刻再给耳返一份 —— 所以「你听到的」
与「对方听到的」不只是同一段音频，而是**同一条播放时间轴**。

⚠️ 不要在 `TranslatedSink.push()` 处 tap：那是 TTS 突发到达的入口，一个 delta 常有几百 ms。
旧实现就是在这里 tap、且监听缓冲只有 120ms（满丢最旧）→ 会把**正在播的句子截断**
（真机症状：「上一句还没播完就开始播下一句」，而进游戏那一路完全正常）。

## 与采集回路的关系（重要）

耳返放出来的声音，如果落在「VRChat 音频」那个采集端点上，就会本程序自己再听一遍
（端点 loopback）→ 再翻一遍。两种处置（`vlt/engine.effective_loopback_mode`）：

* 采集方式已经是 `process`（只抓 VRChat 进程）→ 天然隔离，耳返直接可用；
* 采集方式还是 `endpoint` → **自动切到 process**（能切的话）并留痕；切不了
  （旧系统）就把耳返关掉并明说原因，绝不静默留着一个会自我回译的回路。

声学回路（耳返从外放漏进真实麦克风）不在这里处理：那是物理路径，需要闸门/AEC，
见 `docs/dev/` 的已知限制。

## 缓冲

监听缓冲沿用 #108 的 **120ms、满丢最旧**。真机实测确认：播放侧 tap 的 20ms 块 +
120ms 缓冲在 Linux 上也足够，不需要额外的「整块队列 + monotonic 定速输出」。

## 平台

* Windows → sounddevice / PortAudio 回调（默认输出或按名选的端点）；
* Linux   → `pw-cat --playback` 管道 + 写线程（PipeWire 原生，与采集/译音同一条路）。
  Linux 产物本来就不带 sounddevice（见 requirements-linux.txt），这里也不要求它，
  所以耳返在两端都只依赖已有的东西。

两端的抖动缓冲、补零、音量乘法完全共用（`_callback()` 只有这一份）。

## 失败口径

打开设备失败（端点被占、WASAPI 怪错误、pw-cat 起不来）→ 只禁用耳返：日志一行、
状态栏一条 warn，**翻译与译音输出完全不受影响**。测试/自检进程里一律不构造
（会真开音频设备）。
"""
from __future__ import annotations

import logging
from collections import deque
from contextlib import contextmanager
import subprocess
import threading
from weakref import WeakSet

from .. import platform
from ..session.chatgpt_output import allowed, current

log = logging.getLogger(__name__)

#: 耳返自己的抖动缓冲上限（毫秒）。比虚拟声卡的 `buffer_ms` 小得多 —— 监听要跟手，
#: 允许偶尔欠载（补静音）比让用户听到「慢半拍」更可接受。
DEFAULT_BUFFER_MS = 120
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2          # int16 立体声
BLOCK_FRAMES = 480                      # 10ms
_ROUTE_LOCK = threading.RLock()
_CAPTURE_ENDPOINTS = {}
_MONITORS = WeakSet()


def _feedback(device_name):
    if not _CAPTURE_ENDPOINTS:
        return False
    from ..engine import monitor_capture_conflict, _strip_loopback_suffix
    return any(device_name.strip().casefold() == _strip_loopback_suffix(cfg['loopback_device']).strip().casefold()
               or monitor_capture_conflict({'device_name': device_name}, cfg)
               for cfg in _CAPTURE_ENDPOINTS.values())


def _stop_feedback(monitor):
    monitor.close()
    print('[monitor] 耳返已关闭：与正在采集的输出端点相同（避免自身译音回译）', flush=True)
    monitor._on_status('warn', '耳返已关闭')


@contextmanager
def endpoint_capture(device_name):
    owner = object()
    try:
        with _ROUTE_LOCK:
            _CAPTURE_ENDPOINTS[owner] = {'loopback_device': device_name}
            for monitor in tuple(_MONITORS):
                if monitor.opened and _feedback(monitor.device_name):
                    _stop_feedback(monitor)
        yield
    finally:
        with _ROUTE_LOCK:
            _CAPTURE_ENDPOINTS.pop(owner, None)


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
        self._approvals = deque()
        self._cap_bytes = int(SAMPLE_RATE * BYTES_PER_FRAME * DEFAULT_BUFFER_MS / 1000)
        self._stream = None
        self._proc: subprocess.Popen | None = None      # Linux：pw-cat 管道
        self._writer: threading.Thread | None = None    # Linux：往管道喂数据的线程
        self._stop = threading.Event()
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
        approval = current()
        if not self._opened or not pcm_48k_stereo or not allowed(approval, 'mine'):
            return
        with self._lock:
            self._buf.extend(pcm_48k_stereo)
            self._approvals.append((len(pcm_48k_stereo), approval))
            over = len(self._buf) - self._cap_bytes
            if over > 0:
                self._take(over)
                self._dropped_chunks += 1

    def set_volume(self, volume: float) -> None:
        self._volume = self._clamp(volume)

    def open(self) -> bool:
        with _ROUTE_LOCK:
            if _feedback(self.device_name):
                _stop_feedback(self)
                return False
            opened = self._open_unchecked()
            if opened and _feedback(self.device_name):
                _stop_feedback(self)
                return False
            if opened:
                _MONITORS.add(self)
            return opened

    def _open_unchecked(self) -> bool:
        """打开监听输出。失败返回 False（只影响耳返）。"""
        if self._opened:
            return True
        if platform.IS_LINUX:
            return self._open_pwcat()
        if not platform.IS_WINDOWS:
            print("[monitor] 耳返暂不支持当前平台", flush=True)
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
        with _ROUTE_LOCK:
            self._close_unchecked()
            _MONITORS.discard(self)

    def _close_unchecked(self) -> None:
        self._opened = False
        self._stop.set()
        stream, self._stream = self._stream, None
        proc, self._proc = self._proc, None
        writer, self._writer = self._writer, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:                          # noqa: BLE001
                pass
            print("[monitor] 耳返已关闭", flush=True)
        if proc is not None:
            # 先终止 pw-cat：写线程可能正阻塞在管道写里，只有这样它才会立刻退出。
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:                          # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                      # noqa: BLE001
                    pass
            for pipe in (proc.stdin, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:                      # noqa: BLE001
                    pass
            print("[monitor] 耳返已关闭", flush=True)
        if writer is not None and writer.is_alive() \
                and writer is not threading.current_thread():
            writer.join(timeout=2)
        self._stop.clear()
        with self._lock:
            self._buf.clear()
            self._approvals.clear()

    # ------------------------------------------------------------ Linux 输出
    def _open_pwcat(self) -> bool:
        """Linux：`pw-cat --playback` 管道 + 写线程（与虚拟声卡同一条 PipeWire 原生路径）。

        为什么不用 sounddevice：Linux 产物刻意不带 PortAudio（requirements-linux.txt），
        而 `pw-cat` 本来就是译音输出/试听在用的系统工具 —— 不新增任何依赖。
        """
        target = self._linux_target()
        argv = ["pw-cat", "--playback", "--raw",
                "--format=s16", f"--rate={SAMPLE_RATE}", f"--channels={CHANNELS}",
                # 默认 4/15；只有设备/图需要重采样时才起作用，顺手把重采样质量抬高。
                "--quality=10"]
        if target:
            argv.append(f"--target={target}")
        argv.append("-")
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    env=platform.child_env())
        except Exception as exc:                       # noqa: BLE001
            print(f"[monitor] 启动 pw-cat 失败：{exc}", flush=True)
            self._on_status("warn", "耳返设备打不开，已禁用耳返（其余功能不受影响）")
            return False
        self._proc = proc
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True,
                                        name="vlt-earmonitor")
        self._writer.start()
        self._opened = True
        self._real_name = target or "系统默认输出"
        print(f"[monitor] 耳返已打开：{self._real_name!r}（pw-cat，音量 {self._volume:.0f}%）",
              flush=True)
        return True

    def _linux_target(self) -> str:
        """配置里的设备名 → `pw-cat --target` 要的 PipeWire `node.name`。

        界面在 Linux 上直接存 `node.name`（重连/改描述仍指向同一节点）；
        手写配置可能是描述名，按描述精确匹配一次。找不到就留痕并回落系统默认输出
        —— 与 Windows 侧「找不到 → 用默认设备」同一口径。
        """
        want = str(self._device_name or "").strip()
        if not want:
            return ""
        try:
            for d in platform.device_backend().query_devices():
                node = str(d.get("node_name") or "")
                name = str(d.get("name") or "")
                if want in (node, name):
                    return node or want
        except Exception as exc:                       # noqa: BLE001
            print(f"[monitor] 耳返设备解析失败（{exc}）→ 用系统默认输出", flush=True)
            return ""
        print(f"[monitor] ⚠️ 找不到耳返设备 {want!r} → 用系统默认输出", flush=True)
        return ""

    def _writer_loop(self) -> None:
        """把缓冲里的 PCM 每 10ms 一块喂给 pw-cat：不足补零、按音量缩放。

        节拍由 pw-cat 的**管道背压**决定（与 `PwCatVirtualMic` 同一条路）。
        实测确认：管道背压节拍 + 120ms 缓冲足够，不再额外做 monotonic 定速
        （见模块头「缓冲」）。
        """
        import numpy as np

        proc = self._proc
        assert proc is not None and proc.stdin is not None
        while not self._stop.is_set():
            block = np.zeros((BLOCK_FRAMES, CHANNELS), dtype=np.int16)
            self._callback(block, BLOCK_FRAMES, None, None)
            try:
                proc.stdin.write(block.tobytes())
            except (BrokenPipeError, ValueError, OSError) as exc:
                if self._stop.is_set():
                    return                         # 正常关闭：进程是我们自己终止的
                print(f"[monitor] pw-cat 管道断开（{type(exc).__name__}: {exc}）→ 已禁用耳返",
                      flush=True)
                self._disable("耳返设备打不开，已禁用耳返（其余功能不受影响）")
                return
            if proc.poll() is not None:
                if self._stop.is_set():
                    return
                err = b""
                try:
                    if proc.stderr is not None:
                        err = proc.stderr.read() or b""
                except Exception:                      # noqa: BLE001
                    pass
                tail = err.decode("utf-8", "replace").strip()
                print("[monitor] ❌ pw-cat 已退出 → 已禁用耳返"
                      + (f"：{tail[:400]}" if tail else ""), flush=True)
                self._disable("耳返设备打不开，已禁用耳返（其余功能不受影响）")
                return

    def _disable(self, msg: str) -> None:
        """运行中失败：停用并留痕（进程由 `close()` 或下面这段收尾）。"""
        self._opened = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:                          # noqa: BLE001
                pass
        with self._lock:
            self._buf.clear()
            self._approvals.clear()
        self._on_status("warn", msg)

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _clamp(volume: float) -> float:
        try:
            v = float(volume)
        except (TypeError, ValueError):
            v = 70.0
        return max(0.0, min(100.0, v))

    def _take(self, size: int) -> bytes:
        # Playback is delayed: preserve byte boundaries and recheck each original approval.
        data = bytearray(self._buf[:size])
        del self._buf[:size]
        offset = 0
        while offset < len(data):
            count, approval = self._approvals.popleft()
            take = min(count, len(data) - offset)
            if not allowed(approval, 'mine'):
                data[offset:offset + take] = b'\x00' * take
            if count > take:
                self._approvals.appendleft((count - take, approval))
            offset += take
        return bytes(data)

    def _callback(self, outdata, frames, time_info, status) -> None:   # noqa: ANN001, ARG002
        need = int(frames) * BYTES_PER_FRAME
        with self._lock:
            take = min(len(self._buf), need)
            chunk = self._take(take)
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
