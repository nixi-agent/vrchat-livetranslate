"""可编程引擎：把 app.py 的「会话 + 节流器 + chatbox + overlay」组装抽成可启停的引擎。

GUI 与 CLI 共用同一个 Engine 类；区别只在事件回调和音频源。
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from math import gcd
from pathlib import Path
from typing import Callable

import numpy as np

from .config import AppConfig, Direction, load_config
from .output.chatbox import Chatbox, TokenBucket
from .output.merger import Merger
from .output.overlay import OverlayConfig, WristOverlay
from .output.virtualmic import VirtualMic, pick_output_device, resample_24k_mono_to_48k_stereo
from .session.base import SessionConfig, TextDelta, create_session

ROOT = Path(__file__).resolve().parent.parent
CHUNK_BYTES = 3200          # 100ms @16kHz s16le mono

LOOPBACK_FALLBACK = ["steam streaming speakers", "vive virtual", "cable input", "voicemeeter"]


@dataclass
class EngineEvents:
    on_text: Callable[[str, str, bool], None] = lambda *_a: None     # (原文, 译文, is_final)
    on_status: Callable[[str, str], None] = lambda *_a: None         # (level, msg)
    on_stats: Callable[[dict], None] = lambda *_a: None              # 计数、延迟等


class Engine:
    """可编程启停的同传引擎。

    start() 非阻塞（内部起守护线程跑 asyncio 事件循环）；
    stop() 幂等，可重复调用，5 秒内必须返回。
    """

    def __init__(
        self,
        cfg: AppConfig,
        direction: str,
        source: str,
        sinks: set[str],
        events: EngineEvents,
        *,
        settle_s: float = 8.0,
        dry_run: bool = False,
        overlay_dry_run: bool = False,
        no_realtime: bool = False,
        config_path: str | Path | None = None,
        audio_out: bool | None = None,
        audio_device: list[str] | None = None,
    ) -> None:
        self._cfg = cfg
        self._direction = direction
        self._source = source
        self._sinks = sinks
        self._events = events
        self._settle_s = settle_s
        self._dry_run = dry_run
        self._overlay_dry_run = overlay_dry_run
        self._no_realtime = no_realtime
        self._config_path = config_path
        self._audio_out_override = audio_out
        self._audio_device_override = audio_device

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._stopped = threading.Event()

        self._session = None
        self._chatbox: Chatbox | None = None
        self._merger: Merger | None = None
        self._overlay: WristOverlay | None = None
        self._virtualmic: VirtualMic | None = None
        self._pump_task: asyncio.Task | None = None

        self._connect_ts: list[float] = []

    # ---------------------------------------------------------------- 公开接口

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping = False
        self._stopped.clear()
        t = threading.Thread(target=self._thread_run, daemon=True, name="vlt-engine")
        self._thread = t
        t.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stopping = True
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._schedule_stop)
        self._stopped.wait(timeout)
        self._thread.join(timeout)
        self._thread = None

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return not self._stopping and self._thread is not None and self._thread.is_alive()

    @property
    def chatbox(self) -> Chatbox | None:
        return self._chatbox

    @property
    def merger(self) -> Merger | None:
        return self._merger

    @property
    def virtualmic(self) -> VirtualMic | None:
        return self._virtualmic

    @property
    def session(self):
        return self._session

    def set_languages(self, source_lang: str | None, target_lang: str) -> bool:
        """运行时切换语言：重建会话。预算不足时返回 False 并通过 on_status 告知。"""
        d = self._cfg.directions.get(self._direction)
        if d is None:
            return False
        d.source_lang = source_lang
        d.target_lang = target_lang
        if self._session is not None and self._loop is not None and self._loop.is_running():
            now = time.monotonic()
            rpm = int(self._cfg.session_base.get("max_new_sessions_per_minute", 4))
            recent = [t for t in self._connect_ts if now - t < 60]
            if len(recent) >= rpm:
                self._events.on_status("warn",
                    f"连接预算不足（{len(recent)}/{rpm} min），请稍后再试")
                return False
            asyncio.run_coroutine_threadsafe(self._rebuild_session(), self._loop)
        return True

    # ---------------------------------------------------------------- 内部生命周期

    def _schedule_stop(self) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)

    def _thread_run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_run())
        except Exception as exc:
            if not self._stopping:
                self._events.on_status("error", f"引擎异常：{exc}")
        finally:
            self._loop_ref = None
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()
            except Exception:
                pass
            self._loop = None
            self._stopped.set()

    async def _async_run(self) -> None:
        try:
            await self._build_and_run()
        except Exception as exc:
            self._events.on_status("error", f"运行错误：{exc}")
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        try:
            if self._pump_task is not None:
                self._pump_task.cancel()
                try:
                    await asyncio.wait_for(self._pump_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                self._pump_task = None
        except Exception:
            pass
        try:
            if self._chatbox is not None:
                for _ in range(12):
                    if self._chatbox.pending_count == 0:
                        break
                    self._chatbox.flush_pending()
                    await asyncio.sleep(0.5)
        except Exception:
            pass
        try:
            if self._overlay is not None:
                self._overlay.close()
        except Exception:
            pass
        try:
            if self._virtualmic is not None:
                self._virtualmic.close()
                self._virtualmic = None
        except Exception:
            pass
        try:
            if self._session is not None:
                await self._session.close()
        except Exception:
            pass
        try:
            if self._chatbox is not None:
                self._chatbox.close()
        except Exception:
            pass

    async def _build_and_run(self) -> None:
        scfg = self._cfg.directions[self._direction].to_session_config(self._cfg.session_base)

        if "chatbox" in self._sinks:
            cb = self._cfg.chatbox or {}
            self._chatbox = Chatbox(
                host=cb.get("host", "127.0.0.1"),
                port=int(cb.get("port", 9000)),
                max_chars=int(cb.get("max_chars", 144)),
                max_lines=int(cb.get("max_lines", 9)),
                bucket=TokenBucket(
                    capacity=int(cb.get("bucket_capacity", 5)),
                    window_s=float(cb.get("bucket_window_s", 5.0)),
                    min_gap_s=float(cb.get("min_gap_s", 0.4)),
                ),
                notification_sound=bool(cb.get("notification_sound", True)),
                dry_run=self._dry_run,
            )

        if "overlay" in self._sinks:
            self._overlay = WristOverlay(
                OverlayConfig.from_dict(self._cfg.overlay),
                config_path=Path(self._config_path) if self._config_path else None,
                dry_run=self._overlay_dry_run,
            )
            self._overlay.start()

        audio_cfg = (self._cfg.output or {}).get("audio") or {}
        audio_enabled = self._audio_out_override if self._audio_out_override is not None else audio_cfg.get("enabled", False)
        d = self._cfg.directions.get(self._direction)
        if audio_enabled and d is not None and d.output_audio:
            self._setup_virtualmic(audio_cfg)

        merger_cfg = self._cfg.merger or {}
        cb_cfg = self._cfg.chatbox or {}
        self._merger = Merger(
            sink=lambda text, is_final: self._chatbox.send(text, is_final) if self._chatbox else None,
            interval_s=float(merger_cfg.get("interval_s", cb_cfg.get("interval_s", 2.0))),
            carry_over=bool(merger_cfg.get("carry_over", False)),
            max_chars=int(cb_cfg.get("max_chars", 144)),
        )

        await self._create_session(scfg)

        self._pump_task = asyncio.create_task(self._pump_loop())
        await self._feed_audio()

        self._events.on_status("info", f"等待收尾（{self._settle_s}s）…")
        await asyncio.sleep(self._settle_s)

    def _setup_virtualmic(self, audio_cfg: dict) -> None:
        patterns = self._audio_device_override or audio_cfg.get("device")
        try:
            picked = pick_output_device(patterns)
        except Exception as exc:
            self._events.on_status("error", f"枚举输出设备失败：{exc}（其余功能不受影响）")
            return
        if picked is None:
            chain = ", ".join(patterns) if patterns else "(默认回退链)"
            self._events.on_status("error",
                f"没找到匹配的输出设备（回退链：{chain}）。虚拟声卡装好了吗？其余功能不受影响。")
            return
        idx, name, rate = picked
        self._virtualmic = VirtualMic(
            device_index=idx,
            device_name=name,
            sample_rate=int(audio_cfg.get("sample_rate", 48000)),
            buffer_ms=int(audio_cfg.get("buffer_ms", 300)),
            max_buffer_ms=int(audio_cfg.get("max_buffer_ms", 2000)),
            on_status=self._events.on_status,
        )
        if not self._virtualmic.open():
            self._virtualmic = None

    async def _create_session(self, scfg: SessionConfig) -> None:
        now = time.monotonic()
        rpm = int(self._cfg.session_base.get("max_new_sessions_per_minute", 4))
        recent = [t for t in self._connect_ts if now - t < 60]
        if len(recent) >= rpm:
            wait = 60.0 - (now - recent[0]) + 0.05
            self._events.on_status("warn",
                f"连接预算已满（{len(recent)}/{rpm}/min），等待 {wait:.0f}s")
            await asyncio.sleep(wait)

        self._session = create_session(scfg)
        t0 = time.perf_counter()
        await self._session.start(
            on_text=self._on_text,
            on_audio=self._on_audio,
            on_usage=self._on_usage,
        )
        self._connect_ts.append(time.monotonic())
        ms = (time.perf_counter() - t0) * 1000
        self._events.on_status("info", f"会话已建立（{ms:.0f}ms）")
        self._events.on_stats({"connect_ms": round(ms, 1)})

    async def _rebuild_session(self) -> None:
        try:
            if self._session is not None:
                await self._session.close()
                self._session = None
            scfg = self._cfg.directions[self._direction].to_session_config(self._cfg.session_base)
            await self._create_session(scfg)
            d = self._cfg.directions[self._direction]
            self._events.on_status("info",
                f"语言已切换：{d.source_lang or '自动'}→{d.target_lang}")
        except Exception as exc:
            self._events.on_status("error", f"切换语言失败：{exc}")

    async def _pump_loop(self) -> None:
        while True:
            await asyncio.sleep(0.3)
            if self._chatbox is not None:
                self._chatbox.flush_pending()
            if self._overlay is not None:
                self._overlay.tick()
            if self._session is not None:
                self._session.tick()

    async def _feed_audio(self) -> None:
        if self._source.startswith("pcm:"):
            await self._feed_pcm(self._source[4:])
        elif self._source == "mic":
            await run_mic(self._session, None, device_pattern=None)
        elif self._source == "loopback":
            await run_loopback(self._session, None)

    async def _feed_pcm(self, path: str) -> None:
        pcm = Path(path).read_bytes()
        total = len(pcm)
        self._events.on_status("info",
            f"推送 PCM：{total} bytes ≈ {total / 2 / 16000:.2f}s（按实时节奏）")
        t0 = time.perf_counter()
        for i in range(0, total, CHUNK_BYTES):
            if self._stopping:
                break
            await self._session.send_audio(pcm[i:i + CHUNK_BYTES])
            if not self._no_realtime:
                await asyncio.sleep(CHUNK_BYTES / 2 / 16000)
        elapsed = time.perf_counter() - t0
        self._events.on_status("info", f"音频推送完毕（{elapsed:.2f}s）")

    # ---------------------------------------------------------------- 回调桥接

    def _on_text(self, d: TextDelta) -> None:
        text = d.display
        if text:
            self._events.on_text(d.source or "", text, d.is_final)
        if self._overlay is not None:
            self._overlay.update(text, d.source or "")
        if self._merger is not None and "chatbox" in self._sinks:
            self._merger.push(d)

    def _on_audio(self, pcm: bytes) -> None:
        if self._virtualmic is not None:
            stereo = resample_24k_mono_to_48k_stereo(pcm)
            self._virtualmic.push(stereo)

    def _on_usage(self, u: dict) -> None:
        self._events.on_stats({k: v for k, v in u.items() if isinstance(v, int)})


# ================================================================ 音频采集函数


async def run_mic(session, tele, seconds: float = 0.0, device_pattern: str | None = None) -> None:
    """麦克风采集（16kHz 单声道）。seconds=0 → 一直跑到 Ctrl+C。"""
    import sounddevice as sd

    dev_index = pick_input_device(device_pattern)
    if device_pattern and dev_index is None:
        print(f"[mic] ⚠️ 没找到匹配 '{device_pattern}' 的输入设备，改用系统默认设备")
    info = sd.query_devices(dev_index) if dev_index is not None else sd.query_devices(kind="input")
    print(f"[mic] 使用设备 #{dev_index if dev_index is not None else '(默认)'} : {info['name']}  "
          f"{int(info['default_samplerate'])}Hz")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[mic] ⚠️ {status}")
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

    print("[mic] 开始采集" + ("（Ctrl+C 结束）" if seconds <= 0 else f"（{seconds:.0f}s）"))
    with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                           blocksize=CHUNK_BYTES // 2, callback=callback, device=dev_index):
        end = None if seconds <= 0 else time.perf_counter() + seconds
        while end is None or time.perf_counter() < end:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await session.send_audio(chunk)
            if tele is not None:
                tele.add("mic_chunk", bytes=len(chunk))
    print("[mic] 采集结束")


def pick_input_device(pattern: str | None) -> int | None:
    """按名称子串匹配输入设备。"""
    if not pattern:
        return None
    import sounddevice as sd

    want = pattern.lower()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and want in str(d["name"]).lower():
            return i
    return None


def pick_loopback_device(patterns: list[str] | None = None):
    """返回 (index, name, rate, channels)。找不到返回 None。"""
    import pyaudiowpatch as pyaudio

    chain = [p.lower() for p in (patterns or LOOPBACK_FALLBACK)]
    p = pyaudio.PyAudio()
    try:
        loops = list(p.get_loopback_device_info_generator())
        if not loops:
            return None, p
        for kw in chain:
            for d in loops:
                if kw in str(d["name"]).lower():
                    return (d["index"], d["name"], int(d["defaultSampleRate"]),
                            int(d["maxInputChannels"])), p
        try:
            default_out = p.get_host_api_info_by_type(pyaudio.paWASAPI).get("defaultOutputDevice", -1)
            for d in loops:
                if d.get("index") == default_out or str(d["name"]).startswith("默认"):
                    return (d["index"], d["name"], int(d["defaultSampleRate"]),
                            int(d["maxInputChannels"])), p
        except Exception:
            pass
        d = loops[0]
        return (d["index"], d["name"], int(d["defaultSampleRate"]), int(d["maxInputChannels"])), p
    except Exception:
        p.terminate()
        raise


def to_16k_mono(pcm: bytes, rate: int, channels: int) -> bytes:
    """任意采样率/声道 → 16kHz 单声道 s16le。"""
    a = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        a = a[: len(a) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate == 16000:
        return a.astype(np.int16).tobytes()
    if rate % 16000 == 0:
        f = rate // 16000
        n = len(a) // f * f
        return a[:n].reshape(-1, f).mean(axis=1).astype(np.int16).tobytes()
    g = gcd(rate, 16000)
    up, down = 16000 // g, rate // g
    n = len(a) // down * down
    if n == 0:
        return b""
    x = np.arange(0, n, down)
    xi = np.arange(0, len(x) * up) / up
    y = np.interp(xi, np.arange(len(x)), a[:n:down])
    return y.astype(np.int16).tobytes()


async def run_loopback(session, tele, patterns: list[str] | None = None, seconds: float = 0.0) -> None:
    """采集 VRChat 的播放输出（= 别人说话）→ 推给会话。"""
    import pyaudiowpatch as pyaudio

    loop = asyncio.get_running_loop()
    picked, p = pick_loopback_device(patterns)
    if picked is None:
        print("[loopback] ❌ 没找到任何 loopback 设备（VRChat 在跑吗？在物理控制台会话里吗？）")
        p.terminate()
        return
    idx, name, rate, channels = picked
    print(f"[loopback] 采集端点 #{idx}「{name}」{rate}Hz ×{channels}ch → 16kHz 单声道")

    stream = p.open(format=pyaudio.paInt16, channels=min(2, channels or 2), rate=rate,
                    frames_per_buffer=int(rate * 0.1), input=True, input_device_index=idx)
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def reader():
        while True:
            try:
                data = stream.read(int(rate * 0.1), exception_on_overflow=False)
            except Exception:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    print("[loopback] 开始采集" + ("（Ctrl+C 结束）" if seconds <= 0 else f"（{seconds:.0f}s）"))
    end = None if seconds <= 0 else time.perf_counter() + seconds
    sent_bytes = 0
    try:
        while end is None or time.perf_counter() < end:
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            pcm16 = to_16k_mono(raw, rate, min(2, channels or 2))
            if pcm16:
                await session.send_audio(pcm16)
                sent_bytes += len(pcm16)
                if tele is not None:
                    tele.add("loopback_chunk", bytes=len(pcm16))
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        p.terminate()
    print(f"[loopback] 采集结束，共 {sent_bytes} bytes ≈ {sent_bytes / 2 / 16000:.0f}s")


def list_devices() -> None:
    import sounddevice as sd

    print("=== 输入设备（麦克风）===")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i:3d} | in={d['max_input_channels']} | {int(d['default_samplerate'])}Hz | {d['name']}")
    print("\n=== 输出设备 ===")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            print(f"  {i:3d} | out={d['max_output_channels']} | {int(d['default_samplerate'])}Hz | {d['name']}")
