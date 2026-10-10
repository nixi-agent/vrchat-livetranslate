"""麦克风代理（Mic Proxy）：把「VRChat 里手动切麦克风」内化成程序内的一个二选一路由开关。

## 为什么要有它

改造前：VRChat 的麦克风要在「真实麦克风（原声）」和「虚拟声卡（译音）」之间来回切，
切换动作发生在 VRChat 里，很麻烦。

改造后：VRChat 的麦克风**永久固定**为虚拟声卡；虚拟声卡里放什么内容由本模块的开关决定——

    真实麦克风 ─采集→ 重采样 48k 立体声 → 直通环形缓冲 ─┐
                                                        ├─ 模式开关 → 虚拟声卡输出流 → VRChat
    翻译引擎的译音 PCM → TranslatedSink → 译音抖动缓冲 ─┘
        （passthrough 原声档）              （translated 译音档）

## 生命周期与翻译解耦

代理**程序一启动就工作**（GUI 构造即 start()），跟翻译是否启动无关：
    · 原声档（passthrough）：任何时候都可用，麦克风直通虚拟声卡；
    · 译音档（translated）：只有翻译在跑（`set_translation_active(True)`）时才允许切，
      否则没有译音源可放。翻译停止 → 自动回落并锁定原声档。

## 平台

本模块是**两端共用的本体**（档位语义、缓冲、采集线程、状态汇报都在这里）：

    * Windows → 本模块直接用（`platform/win.py` 构造，输出走 sounddevice/PortAudio 回调）；
    * Linux   → `vlt/output/micproxy_linux.py: LinuxMicProxy` **继承本类**，只覆盖
      `start` / `close` / 输出驱动（`pw-cat` 写管道 + 运行时声明的 PipeWire 节点）。

采集率与输出率一致（默认 48k）：Windows 用设备原生率、Linux 用请求的 `sample_rate`，
保证原声直通**全带宽**（曾因 Linux 硬编码 16k 导致原声发闷/电话音，见 `_mic_pump`）。

## 复用而非重造（踩过坑的逻辑只留一份）

    · 麦克风采集：复用 `platform.capture_backend().open_mic(...)` 返回的 AudioSource ——
      它已经处理了「WASAPI 共享模式只认设备原生采样率」「同名端点回落」「teardown 顺序」
      这些真机事故（见 `platform/win.py: open_mic` / `platform/audio.py`）。
    · 译音抖动缓冲：直接**借用** `VirtualMic` 的 push/end_sentence/整句丢弃/起播兜底逻辑
      （`_TranslatedBuffer` 继承它，只把「开自己的流」这一步禁掉），绝不再写第二份记账。

## ⚠️ 直通缓冲下限（本期既定取舍）

麦克风按**全带宽**采集：Windows 用设备原生采样率（`win.py` 会把 blocksize 强制成
`native*0.1`，块大小约 100ms），Linux 用调用方请求的 `sample_rate`（= 48k，见
`platform/linux.py:open_mic`）。所以直通环形缓冲的容量**必须 ≥ 一个输入块**
（否则每块进来就被削掉大半 → 严重断续）。默认 150ms、GUI 范围 60–500ms 即由此而来。
把 blocksize 降到 20ms 级以进一步压低延迟是后续优化，本期不做。
"""
from __future__ import annotations

import asyncio
import collections
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..devices import resolve_device_name
from .. import platform
from . import level_match
from .level_match import LevelConfig
from .virtualmic import VirtualMic, pick_output_device

#: 直通缓冲默认容量（毫秒）。必须 ≥ 一个麦克风输入块（~100ms），见模块头说明。
DEFAULT_PASSTHROUGH_MS = 150
#: 麦克风采集块大小（帧，@16k 名义 = 100ms）；实际块时长随设备原生采样率由 open_mic 决定。
MIC_BLOCKSIZE = 1600
#: 欠载告警的最小汇报间隔（秒）。
UNDERRUN_REPORT_S = 5.0


def _test_process_guard(action: str = "真的打开虚拟声卡与麦克风采集") -> dict | None:
    """防呆：**测试进程里拒绝做「会碰用户音频图」的真实操作**。

    与 `vlt/platform/linux.py` / `vlt/output/openxr_overlay.py` 的那两份同款（本仓库
    这是第三份，原因是三个入口分属不同模块、彼此不能 import；口径与判据完全一致）。

    写它同样不是洁癖：本仓库实测**踩过四次**「单元测试绕过打桩、真的拉起了设备」。
    在 Windows 上尤其危险 —— 用户机器上通常真的装着 VoiceMeeter / VB-Cable，
    测试一跑就会打开他们的虚拟声卡、实时把麦克风直通出去（对方能听到）。

    正确修法是测试两侧都打桩（见 `tests/test_micproxy.py`），这里只是**最后一道保险**：
    万一又漏了，宁可这条腿在测试里不启用，也不能动用户的音频。
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return None
    p = Path(path)
    if p.name.startswith("test_") or "tests" in p.parts:
        # 返回**参数**而不是成句：模板留在调用点，好让界面层拿去查 i18n 词条
        return {"name": p.name, "action": action}
    return None

MODE_PASSTHROUGH = "passthrough"
MODE_TRANSLATED = "translated"


def resample_to_48k_stereo(pcm: bytes, src_rate: int, src_channels: int = 1) -> bytes:
    """任意采样率/声道的 s16le PCM → 48kHz 立体声 s16le。

    流程：多声道取均值降为单声道 →（降采样时）**先抗混叠低通** → 在原始采样点时间轴上
    线性插值到 48k → 单声道复制到双声道。

    * 与 `virtualmic.resample_24k_mono_to_48k_stereo` 的区别：那个是写死 24k→48k（2 倍），
      这里的**源采样率不固定**。
    * Linux 麦克风代理现在直接在 48k 采集（见 `platform/linux.py:open_mic`），所以本函数
      在 Linux 上退化为「单声道复制双声道」；Windows 按设备原生率采集，44.1k 走升采样、
      96k/192k 走**降采样**。
    * ⚠️ 降采样（src>48k）必须先低通，否则 24kHz 以上的分量会**混叠**折叠进可听频段
      （线性插值直接取点 = 无滤波 decimation，是典型的「怪声」来源）。
    """
    from ..audio_dsp import lowpass
    import numpy as np

    if len(pcm) % 2 != 0:
        pcm = pcm[: len(pcm) - 1]
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if src_channels > 1:
        n = len(a) // src_channels * src_channels
        a = a[:n].reshape(-1, src_channels).mean(axis=1)
    if a.size == 0:
        return b""
    if src_rate != 48000:
        if src_rate > 48000:
            # 降采样：先在源域低通到 48k 奈奎斯特以下（取 20k 留过渡带），再插值取点。
            a = lowpass(a, src_rate, cutoff_hz=20000.0)
        n_out = int(len(a) * 48000 / src_rate)
        if n_out < 1:
            return b""
        pos = np.arange(n_out) * (src_rate / 48000.0)
        a = np.interp(pos, np.arange(len(a)), a)
    mono = np.clip(a, -32768, 32767).astype(np.int16)
    return np.repeat(mono, 2).tobytes()          # 单声道 → 立体声


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
        """取一块译音数据；还没起播/不足则返回 None（调用方补静音）。"""
        with self._lock:
            self._maybe_prime()
            if not self._primed or self._buf_bytes < need_bytes:
                return None
            return self._drain(need_bytes)

    def reset(self) -> None:
        """清空缓冲并复位起播状态（切换档位时用：切过去立刻是新内容，不念旧账）。"""
        with self._lock:
            self._buf.clear()
            self._buf_bytes = 0
            self._primed = False
            self._head_started = False

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
        """麦克风「说话电平」参考（dBFS）；取不到就是 None —— 引擎据此决定是否回落。"""
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


class MicProxy:
    """常驻麦克风代理：麦克风直通虚拟声卡（原声档）/ 译音灌虚拟声卡（译音档），一键切换。

    线程模型：一个守护线程跑独立 asyncio loop 消费麦克风 AudioSource；
    虚拟声卡输出走 sounddevice 的 PortAudio 回调（音频线程）。无跨线程共享事件循环。
    """

    def __init__(
        self,
        *,
        audio_cfg: dict,
        mic_name: str | None = None,
        #: `on_status(level, msg, **params)`：`msg` 是**中文模板**（同时充当 i18n 词条 key），
        #: 带值的地方走 `**params`。界面层负责「日志用中文原文、状态栏走 t()」——见
        #: `gui._on_proxy_status`。这样日志全仓统一中文、不会中英混排。
        on_status: Callable[..., None] = lambda *_a, **k: None,
    ) -> None:
        self._audio_cfg = audio_cfg or {}
        self._mic_name = mic_name or None
        self._on_status = on_status

        self._sample_rate = int(self._audio_cfg.get("sample_rate", 48000))
        self._max_buffer_ms = int(self._audio_cfg.get("max_buffer_ms", 2000))
        proxy_cfg = self._audio_cfg.get("proxy") or {}
        self._passthrough_ms = int(proxy_cfg.get("passthrough_buffer_ms", DEFAULT_PASSTHROUGH_MS))
        self._translated_ms = int(self._audio_cfg.get("buffer_ms", 300))

        self._blocksize = int(self._sample_rate * 0.02)      # 20ms 一块（对齐 VirtualMic）
        self._bytes_per_ms = self._sample_rate * 2 * 2 / 1000

        self._ring = _Ring(int(self._passthrough_ms * self._bytes_per_ms))
        self._translated: _TranslatedBuffer | None = None
        self._out_stream = None
        self._out_device: tuple[int, str] | None = None
        self._mic_src = None

        self._mode = MODE_PASSTHROUGH
        self._translation_active = False
        self._state_lock = threading.RLock()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._opened = False
        self._closed = False
        self._underruns = 0
        #: 是否已收到过第一块麦克风数据。启动阶段环形缓冲还空着时**不计**欠载 ——
        #: 以前「先开输出、后起采集」会实测量化出启动头 ~200ms 的欠载告警刷屏
        #: （日志实证：9~10 次/5s 只在启动窗口出现，稳态为 0）。见 `_prime_ring`。
        self._got_mic_data = False
        self._sink = TranslatedSink(self)

        # ---- 译音音量匹配（见 vlt/output/level_match.py）----
        # 麦克风「说话电平」参考**复用 `_mic_pump` 已经在读的那些块**，不再开第二个采集
        # 设备（同一只麦克风开两条流，在 WASAPI 独占/共享模式下都可能顶掉对方）。
        self._level_cfg = LevelConfig.from_dict(self._audio_cfg.get("level") or {})
        self._mic_ref = level_match.MicReference(self._level_cfg)
        #: 「麦克风电平参考取不到」只报一次（不去重就会每 5s 刷一条告警）。
        self._ref_warned = False
        self._pump_started_at = 0.0

    # ------------------------------------------------------------------ 属性
    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def underrun_count(self) -> int:
        return self._underruns

    @property
    def translated_sink(self) -> TranslatedSink:
        return self._sink

    @property
    def mic_reference_db(self) -> float | None:
        """麦克风「说话电平」参考（dBFS）；有声证据不足就是 None（**不猜**）。"""
        return self._mic_ref.db()

    @property
    def level_config(self) -> LevelConfig:
        return self._level_cfg

    def set_level_config(self, cfg: LevelConfig) -> None:
        """UI 改了「译音音量」参数后热更新。**不清历史**：拖一下滑块不该逼用户重说话。"""
        self._level_cfg = cfg
        self._mic_ref.set_config(cfg)
        self._ref_warned = False          # 换了参数就允许再报一次「参考取不到」

    # ------------------------------------------------------------------ 启停
    def start(self) -> bool:
        """启动麦克风直通线程 → 等环形缓冲攒到第一块 → 再打开虚拟声卡输出流。

        顺序刻意如此（2026-10）：原来是「先开输出、后起采集」，于是输出流一开就对着
        **还没数据的**环形缓冲要数据 —— 实测表现为启动头 ~200ms 的欠载爆音 + 告警刷屏
        （日志实证：`直通缓冲欠载 9~10 次/5s` 只出现在启动/换麦窗口，稳态为 0）。
        先起采集、等到数据落进环形缓冲再开输出，这段空窗从源头消失。失败只降级
        （返回 False），不抛异常。
        """
        if self._opened or self._closed:
            return self._opened
        blocked = _test_process_guard()
        if blocked:
            self._on_status("warn",
                            "[proxy] 检测到测试进程（{name}）→ 拒绝{action}（这条腿不启用）",
                            **blocked)
            return False
        dev = self._pick_output_device()
        if dev is None:
            return False
        idx, name, _rate, fallbacks = dev
        self._translated = _TranslatedBuffer(
            device_index=idx, device_name=name, sample_rate=self._sample_rate,
            buffer_ms=self._translated_ms, max_buffer_ms=self._max_buffer_ms,
            on_status=self._on_status,
        )
        self._start_mic_thread()
        self._prime_ring()
        if not self._open_output(idx, name, fallbacks):
            self._translated = None
            self._stop_mic_thread()
            return False
        self._opened = True
        return True

    def _start_mic_thread(self) -> None:
        """拉起麦克风直通采集线程（幂等前置：先清停止位）。"""
        self._stop.clear()
        self._thread = threading.Thread(target=self._mic_thread_run, daemon=True,
                                        name="vlt-micproxy")
        self._thread.start()

    def _stop_mic_thread(self) -> None:
        """停采集线程并 join（`_stop` 只属于采集线程，见 `start()`/`close()`）。"""
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)                  # 采集线程内部对 read 有超时，2s 足够退出
        self._thread = None

    def _prime_ring(self, timeout: float = 2.0) -> None:
        """等直通环形缓冲攒到 **~半个缓冲** 的第一批数据再开输出。

        短超时兜底：采集线程若已退出（麦打不开/被拔），立刻返回、绝不永久挂住启动。
        """
        target = max(1, int(self._bytes_per_ms * self._passthrough_ms * 0.5))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ring.available() >= target:
                return
            if self._thread is None or not self._thread.is_alive():
                return
            time.sleep(0.02)

    def close(self) -> None:
        """幂等关闭：先停麦克风线程，再关输出流（顺序与采集侧同一条纪律）。"""
        if self._closed:
            return
        self._closed = True
        self._opened = False
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
        self._thread = None
        if self._out_stream is not None:
            try:
                self._out_stream.stop()
                self._out_stream.close()
            except Exception:              # noqa: BLE001
                pass
            self._out_stream = None

    # ------------------------------------------------------------------ 输出设备
    def _pick_output_device(self):
        """复刻 `engine._make_audio_out` 的选设备口径：device_name 优先 → 回退链。

        返回 (index, name, rate, fallbacks) 或 None（不可用，已留痕）。
        """
        device_name = self._audio_cfg.get("device_name") or ""
        picked = None
        fallbacks: list[int] = []
        if device_name:
            idx = resolve_device_name(device_name, "output")
            if idx is not None:
                import sounddevice as sd
                info = sd.query_devices(idx)
                picked = (idx, str(info["name"]), int(info.get("default_samplerate", 48000)))
                fallbacks = platform.output_device_fallbacks(device_name, exclude=idx)
            else:
                self._on_status("warn", "[proxy] 未找到输出设备 {dev}，回退到回退链",
                                dev=repr(device_name))
        if picked is None:
            patterns = self._audio_cfg.get("device")
            try:
                picked = pick_output_device(patterns)
            except Exception as exc:       # noqa: BLE001
                self._on_status("error", "[proxy] 枚举输出设备失败：{err}（其余功能不受影响）",
                                err=exc)
                return None
        if picked is None:
            # ⚠️ 别在这条**实现**字符串里写死具体虚拟声卡商品名（VoiceMeeter/VB-Audio）：
            #    本模块虽只在 Windows 用，但源码级平台隔离守卫会扫全 vlt/，大写商品名会
            #    被判成「Linux 侧混进 Windows 实现」。与 virtualmic.py 同口径，只说「虚拟声卡」。
            self._on_status("error",
                            "[proxy] 没找到匹配的虚拟声卡输出设备（虚拟声卡装好了吗？）"
                            "→ 麦克风代理不可用，其余功能不受影响。")
            return None
        idx, name, rate = picked
        # 与 `engine._make_audio_out` 同一条纪律：回退链挑中的设备也要算同名回落候选，
        # 否则首选端点打不开时代理这条腿只试一次就被判死（逐条留痕见 `_open_output`）。
        if not fallbacks:
            fallbacks = platform.output_device_fallbacks(name, exclude=idx)
        return (idx, name, rate, fallbacks)

    def _open_output(self, idx: int, name: str, fallbacks: list[int]) -> bool:
        """打开虚拟声卡输出流（含同名端点回落，逐次留痕）。"""
        import sounddevice as sd

        last_exc: Exception | None = None
        for dev in [idx, *fallbacks]:
            try:
                stream = sd.RawOutputStream(
                    samplerate=self._sample_rate, channels=2, dtype="int16",
                    device=dev, callback=self._out_callback, blocksize=self._blocksize,
                )
                stream.start()
                self._out_stream = stream
                self._out_device = (dev, name)
                self._on_status("info", "[proxy] 虚拟声卡已打开：#{idx} {name}",
                                idx=dev, name=name)
                return True
            except Exception as exc:       # noqa: BLE001 — 换候选再试
                last_exc = exc
                self._on_status("warn", "[proxy] 虚拟声卡 #{idx} 打不开：{err}",
                                idx=dev, err=exc)
        self._on_status("error", "[proxy] 打开虚拟声卡失败：{err}（其余功能不受影响）",
                        err=last_exc)
        return False

    def _out_callback(self, outdata: bytearray, frames: int, time_info, status) -> None:
        need = frames * 2 * 2
        if status:
            pass
        if self._mode == MODE_TRANSLATED and self._translated is not None:
            data = self._translated.drain_block(need)
            outdata[:] = (b"\x00" * need) if data is None else data
            return
        data, under = self._ring.drain(need)
        outdata[:] = data
        # 收到第一块麦克风数据之前不计欠载：启动/换麦瞬间环形缓冲本来就是空的，那不是故障。
        if under and self._got_mic_data:
            self._underruns += 1

    # ------------------------------------------------------------------ 麦克风直通
    def _mic_thread_run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._mic_pump())
        except Exception as exc:           # noqa: BLE001 — 线程绝不能把异常抛出去
            self._on_status("error", "[proxy] 麦克风直通线程异常退出：{kind}: {err}",
                            kind=type(exc).__name__, err=exc)
        finally:
            try:
                self._loop.close()
            except Exception:              # noqa: BLE001
                pass

    async def _mic_pump(self) -> None:
        # ★ 采集率 = 虚拟声卡的输出率（48k），**不要**用 16k：
        #   直通是把真人原声送去虚拟麦，降到 16k 会丢掉 8kHz 以上（齿音/气息/明亮度），
        #   再插值升回 48k 也补不回来 → 听感发闷、像电话音（原声下尤其明显）。
        #   Windows 侧 `open_mic` 本就会改用设备原生率，这里显式传 `_sample_rate`
        #   让 Linux 侧也走全带宽（Linux 以前硬编码 16k，是本次音质问题的根因）。
        src = platform.capture_backend().open_mic(
            self._mic_name, rate=self._sample_rate, channels=None, blocksize=MIC_BLOCKSIZE)
        self._mic_src = src
        self._on_status("info", "[proxy] 麦克风直通已启动（{rate}Hz {channels}ch → 48k 立体声）",
                        rate=src.rate, channels=src.channels)
        last_report = time.monotonic()
        last_under = self._underruns
        self._pump_started_at = last_report
        try:
            while not self._stop.is_set():
                chunk = await src.read(timeout=0.5)
                if chunk:
                    # 始终填充直通环形缓冲（保持新鲜）；译音档时输出回调不取它，
                    # 切回原声档立刻有最近 ~passthrough_ms 的麦克风数据，无需等下一块。
                    self._ring.push(resample_to_48k_stereo(chunk, src.rate, src.channels))
                    self._got_mic_data = True   # 收到第一块 → 之后才计欠载（见 _out_callback）
                    # ★ 译音音量匹配的参考电平也从这里取：**复用同一块数据**，不开第二个
                    #   采集设备。译音档下**照样更新**（刻意不冻结 —— 回灌/自激不在本期范围，
                    #   冻结逻辑自己加进来只会把「参考为什么不动」变成新的谜）。
                    self._mic_ref.feed(chunk, src.rate, src.channels, now=time.monotonic())
                now = time.monotonic()
                if now - last_report >= UNDERRUN_REPORT_S:
                    delta = self._underruns - last_under
                    if delta > 0:
                        self._on_status(
                            "warn",
                            "[proxy] 直通缓冲欠载 {n} 次/{secs}s（可能爆音）："
                            "可在 设置→音频 调大直通缓冲",
                            n=delta, secs=f"{UNDERRUN_REPORT_S:.0f}")
                    self._report_level_reference(now)
                    last_report = now
                    last_under = self._underruns
        finally:
            try:
                src.close()
            except Exception:              # noqa: BLE001
                pass

    def _report_level_reference(self, now: float) -> None:
        """`follow_mic` 模式下麦克风参考长期取不到 → 报**一次**（去重，不刷屏）。

        为什么值得报：这条腿静默回落成固定增益后，用户只会觉得「音量匹配没用」，
        而真实原因（麦克风一直没被判定为在说话 / 门限太严 / 选错设备）在界面上看不出来。
        """
        if self._ref_warned or self._level_cfg.mode != level_match.MODE_FOLLOW_MIC:
            return
        if self._mic_ref.db() is not None:
            return
        # 刚起泵的前 mic_window_s 属于「还没说够话」，不是故障，别急着报。
        if self._pump_started_at and (now - self._pump_started_at) < self._level_cfg.mic_window_s:
            return
        self._ref_warned = True
        self._on_status(
            "warn",
            "[proxy] 麦克风电平参考取不到 → 译音音量匹配回落固定增益 {db} dB",
            db=f"{self._level_cfg.fixed_gain_db:g}")

    # ------------------------------------------------------------------ 档位切换
    def set_translation_active(self, active: bool) -> None:
        """翻译启停时由 GUI 调用。停止翻译 → 强制回落原声档（译音源没了）。"""
        with self._state_lock:
            self._translation_active = bool(active)
            if not active and self._mode == MODE_TRANSLATED:
                self._mode = MODE_PASSTHROUGH
                self._ring.clear()

    def set_mode(self, mode: str) -> bool:
        """切换档位。译音档仅在翻译运行时允许。返回是否切换成功。"""
        with self._state_lock:
            if mode not in (MODE_PASSTHROUGH, MODE_TRANSLATED):
                return False
            if mode == MODE_TRANSLATED and not self._translation_active:
                self._on_status("warn", "[proxy] 翻译未运行，无法切到译音档（保持原声）")
                return False
            if mode == self._mode:
                return True
            self._mode = mode
            # 切换即清空**两侧**缓冲：立刻生效、不念旧账（切过去听到的是新内容）。
            self._ring.clear()
            if self._translated is not None:
                self._translated.reset()
            self._on_status("info",
                            "[proxy] 已切到「译音」档" if mode == MODE_TRANSLATED
                            else "[proxy] 已切到「原声」档")
            return True

    # ------------------------------------------------------------------ 缓冲参数
    def reopen_with(self, passthrough_ms: int | None = None,
                    translated_buffer_ms: int | None = None) -> None:
        """设置页改缓冲后调用：更新容量/起播线。

        两者都是**软件侧参数**（环形缓冲容量、译音起播线），PortAudio 输出流的
        blocksize 不受影响 → **无需重开音频流**，也就没有静音间隙，改动即时生效。
        """
        if passthrough_ms is not None:
            self._passthrough_ms = int(passthrough_ms)
            self._ring.set_cap(int(self._passthrough_ms * self._bytes_per_ms))
        if translated_buffer_ms is not None and self._translated is not None:
            self._translated_ms = int(translated_buffer_ms)
            self._translated.set_buffer_ms(self._translated_ms)

    # ------------------------------------------------------------------ 换麦克风
    def reopen_mic(self, mic_name: str | None) -> None:
        """切换**直通麦克风设备**：只重启麦克风采集线程，虚拟声卡输出流不动。

        为什么不是「关掉再重开整个代理」：重开虚拟声卡会断声，而且运行中引擎手里那条
        `translated_sink` 会失效（见 `gui._restart_proxy` 的说明）。只换输入侧则
        VRChat 那侧（虚拟麦）连续不断，翻译桥接也不受影响。

        `mic_name=None` = 回到系统默认输入设备。未启动时只记下设备名（配置已落盘，
        下次 `start()` 生效）；幂等；**绝不抛异常**（代理一条纪律：任何一步失败只降级）。
        """
        new = mic_name or None
        if new == self._mic_name and self._thread is not None and self._thread.is_alive():
            return
        self._mic_name = new
        if not self._opened:
            return
        # `_stop` 只属于麦克风采集线程（见 start()/close()），这里复用它做一次「停→起」。
        self._got_mic_data = False                # 新麦的第一块到达前不计欠载（同启动口径）
        self._stop_mic_thread()
        try:
            self._ring.clear()                    # 丢掉旧麦遗留数据，免得切换瞬间放一小段旧麦
        except Exception:                         # noqa: BLE001 — 清缓冲失败不该挡切换
            pass
        self._start_mic_thread()
        self._on_status("info", "[proxy] 直通麦克风已切换：{name}",
                        name=new or "系统默认")
