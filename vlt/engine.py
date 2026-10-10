"""可编程引擎：把 app.py 的「会话 + 节流器 + chatbox + overlay」组装抽成可启停的引擎。

GUI 与 CLI 共用同一个 Engine 类；区别只在事件回调和音频源。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import numpy as np

log = logging.getLogger(__name__)

from .audio_dsp import lowpass as _lowpass
from .config import AppConfig, Direction
from .devices import enumerate_mic_devices, resolve_device_name
from . import endpoints
from . import platform
from .platform.audio import MixedAudioSource
from .platform.base import LoopbackTarget
from .output.chatbox import Chatbox, TokenBucket
from .output.merger import Merger
from .output.overlay import OverlayConfig
from .output.virtualmic import VirtualMic, pick_output_device, resample_24k_mono_to_48k_stereo
from .session.base import SessionConfig, TextDelta, create_session
from .textin import DEFAULT_MODEL as DEFAULT_TEXT_MODEL
from .textin import DEFAULT_TIMEOUT_S as DEFAULT_TEXT_TIMEOUT_S
from .textin import TextTranslateError, split_for_chatbox, terms_from_mapping, translate_text
from .tts import DEFAULT_MODEL as DEFAULT_TTS_MODEL
from .tts import DEFAULT_TIMEOUT_S as DEFAULT_TTS_TIMEOUT_S
from .tts import DEFAULT_VOICE as DEFAULT_TTS_VOICE
from .tts import TtsError, TtsStreamTruncated, synthesize, synthesize_stream

ROOT = Path(__file__).resolve().parent.parent
CHUNK_BYTES = 3200          # 100ms @16kHz s16le mono
# 音频流静默超过这个时长 = 上一句说完了（给虚拟麦打句尾标记的兜底触发）
SENTENCE_GAP_S = 0.6
# 收尾时排空 chatbox 的**墙钟**预算（不是「轮数」）。为什么不写轮数：排空速度由令牌桶的
# min_gap_s 决定（默认 0.4s 才放一条），轮数和它一耦合，改限流就等于改停止耗时 ——
# 上一版把 12 轮收成 4 轮后，积压里**最新那条**（用户刚说完的那句）连发都发不出去
# 就被 close() 掐掉了。现在总耗时有硬上限，且最新一条优先发（见 _drain_chatbox）。
CHATBOX_DRAIN_BUDGET_S = 2.0
CHATBOX_DRAIN_TICK_S = 0.05     # 隔一会儿再问一次令牌桶；刻意不与 min_gap_s 耦合

# ---- chatbox 气泡显示哪种文本（界面上是 `chatbox` 勾选框右边那颗切换按钮）----
# 互斥二选一，**只影响 chatbox 气泡**：手腕屏 / 桌面字幕 / 聊天区恒为译文。
CHATBOX_TEXT_TRANSLATED = "translated"   # 默认：气泡显示译文
CHATBOX_TEXT_SOURCE = "source"           # 气泡显示 ASR 源文（「我说的话」本身）
_CHATBOX_TEXT_VALUES = (CHATBOX_TEXT_TRANSLATED, CHATBOX_TEXT_SOURCE)
# 脏值只在**首次见到**时留痕一次：本函数每条文本增量都要调，不去重就会刷屏。
_CHATBOX_TEXT_WARNED: set[str] = set()


def chatbox_text_mode(cfg) -> str:
    """读 `ui.chatbox_text`：`translated`（默认）| `source`。脏值回落 `translated` 并留痕。

    每次调用都现读（**不缓存**）：界面点一下切换就要在同一次会话里立刻生效，
    所以判据不能是启动时算好的一份快照。
    """
    raw = (getattr(cfg, "ui", None) or {}).get("chatbox_text", CHATBOX_TEXT_TRANSLATED)
    if isinstance(raw, str) and raw.strip() in _CHATBOX_TEXT_VALUES:
        return raw.strip()
    key = repr(raw)
    if key not in _CHATBOX_TEXT_WARNED:
        _CHATBOX_TEXT_WARNED.add(key)
        print(f"[config] ui.chatbox_text={raw!r} 不是 "
              f"{' / '.join(_CHATBOX_TEXT_VALUES)} 之一 → 回落 translated（气泡显示译文）",
              flush=True)
    return CHATBOX_TEXT_TRANSLATED


# 输入音量判「有声音」的峰值门限（16k s16le）。只用于黑匣子统计，不影响功能。
SILENCE_PEAK = 220

# Linux 采集 VRChat 输出流时，每隔这么久回查一次 PipeWire 图 ——
# 用来发现「VRChat 退出了」（目标节点消失），及时收尾回到等待。
VRCHAT_RECHECK_S = 3.0

# ---- 输入门限（只接在 loopback = VRChat 输出「别人说话」那条腿上）----
# 治什么：VRChat 把人声混成一路输出，远处玩家声音小、听都听不清，以往照样被
# 送给模型 → 白花钱，还容易被翻成乱话。这里按块 RMS 判响度，达不到门限就不上送。
# 取值口径（dBFS，0 = 满量程；RMS 比峰值严，正常近处说话的块大致落在 -30~-20）：
#   越高（越接近 0）= 过滤越狠，只留贴近耳边的人；越低 = 越宽松，远处的人也翻。
INPUT_GATE_DEFAULT_ENABLED = True
INPUT_GATE_DEFAULT_DB = -45.0
INPUT_GATE_DEFAULT_HOLD_MS = 500.0     # 超阈值后，回落多久内仍继续送（防句中断裂）
INPUT_GATE_DEFAULT_PREROLL_MS = 250    # 开闸时补发的、越阈值之前的音频（防丢句首）
INPUT_GATE_MIN_DB = -70.0
INPUT_GATE_MAX_DB = -10.0
LEVEL_FLOOR_DB = -120.0                # 电平下限（纯数字静音时用它，避免 log10(0)）

# 挑「系统声采集」目标时的关键词回退链（小写匹配）。
# **只用于 Windows**（匹配 WASAPI loopback 设备名）。
# Linux 不走这里：它按应用流节点精确匹配 VRChat（见 pick_vrchat_target），
# 因为 Linux 上「VRChat 音频」根本不在 sink 列表里，而且抓默认 sink 会混入别的应用。
LOOPBACK_FALLBACK = [
    "steam streaming speakers", "vive virtual", "cable input", "voicemeeter",
    "vrchat", "vrc", "steam streaming",
]


# ================================================================ 配置校验（非法值留痕 + 回落默认）


def _warn_default(key: str, val, why: str, default, section: str = "session") -> None:
    print(f"[config] ⚠️ {section}.{key}={val!r} 非法（{why}）→ 回落默认值 {default!r}", flush=True)


def _cfg_bool(base: dict, key: str, default: bool, section: str = "session") -> bool:
    v = base.get(key, default)
    if isinstance(v, bool):
        return v
    _warn_default(key, v, "应为 true/false", default, section)
    return default


def _cfg_pos_float(base: dict, key: str, default: float) -> float:
    v = base.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _warn_default(key, v, "应为正数", default)
        return default
    if float(v) <= 0:
        _warn_default(key, v, "必须 > 0", default)
        return default
    return float(v)


def silence_gate_settings(base: dict | None) -> tuple[bool, float, float]:
    """读取并校验静音闸门配置（纯函数，离线可测）：(enabled, after_s, preroll_s)。

    任何一项非法都**明确留痕**并回落默认值（绝不静默带病运行）。
    """
    base = base or {}
    enabled = _cfg_bool(base, "silence_gate_enabled", True)
    after = _cfg_pos_float(base, "silence_gate_after_s", 30.0)
    preroll = _cfg_pos_float(base, "silence_gate_preroll_s", 1.0)
    if preroll > after:
        # preroll 比闸门阈值还长没有意义（闸还没关缓冲就先满了）
        print(f"[config] ⚠️ session.silence_gate_preroll_s={preroll} 大于 "
              f"silence_gate_after_s={after} → 回落默认值 1.0", flush=True)
        preroll = min(1.0, after)
    return enabled, after, preroll


def chunk_level_db(chunk: bytes) -> float:
    """一个 100ms 音频块的电平（dBFS，满量程 = 0）。纯函数，离线可测。

    用 **RMS** 而不是峰值：峰值只反映块里最响的那几个采样 —— 按键、咳嗽、音效
    的一个尖刺就能把峰值顶上来，但它不代表「这句话整体有多响」。远处玩家小声说话
    的块峰值可能偶发偏高、RMS 却一直很低，正是要拦的对象。
    """
    if not chunk:
        return LEVEL_FLOOR_DB
    arr = np.frombuffer(chunk, dtype="<i2")
    if arr.size == 0:
        return LEVEL_FLOOR_DB
    x = arr.astype(np.float64) / 32768.0
    rms = float(np.sqrt(float(np.mean(x * x))))
    if rms <= 0.0:
        return LEVEL_FLOOR_DB
    return max(LEVEL_FLOOR_DB, 20.0 * float(np.log10(rms)))


def input_gate_settings(base: dict | None) -> tuple[bool, float, float, int]:
    """读取并校验输入门限配置（纯函数，离线可测）：(enabled, threshold_db, hold_ms, preroll_ms)。

    非法值一律**留痕 + 回落默认**（与本文件其它设置同一纪律），字段缺省也算合法。
    """
    base = base or {}
    enabled = _cfg_bool(base, "gate_enabled", INPUT_GATE_DEFAULT_ENABLED, "capture")
    db = base.get("gate_db", INPUT_GATE_DEFAULT_DB)
    if (isinstance(db, bool) or not isinstance(db, (int, float))
            or not (INPUT_GATE_MIN_DB <= float(db) <= INPUT_GATE_MAX_DB)):
        _warn_default("gate_db", db,
                      f"应在 {INPUT_GATE_MIN_DB:g} ~ {INPUT_GATE_MAX_DB:g} dBFS 之间",
                      INPUT_GATE_DEFAULT_DB, "capture")
        db = INPUT_GATE_DEFAULT_DB
    hold = base.get("gate_hold_ms", INPUT_GATE_DEFAULT_HOLD_MS)
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or float(hold) < 0:
        _warn_default("gate_hold_ms", hold, "应为 ≥0 的毫秒数",
                      INPUT_GATE_DEFAULT_HOLD_MS, "capture")
        hold = INPUT_GATE_DEFAULT_HOLD_MS
    preroll = base.get("gate_preroll_ms", INPUT_GATE_DEFAULT_PREROLL_MS)
    if isinstance(preroll, bool) or not isinstance(preroll, (int, float)) or float(preroll) < 0:
        _warn_default("gate_preroll_ms", preroll, "应为 ≥0 的毫秒数",
                      INPUT_GATE_DEFAULT_PREROLL_MS, "capture")
        preroll = INPUT_GATE_DEFAULT_PREROLL_MS
    return enabled, float(db), float(hold), int(preroll)


def repeat_guard_settings(base: dict | None) -> tuple[bool, int, float]:
    """读取并校验本地 repeat 抑制配置（纯函数）：(enabled, hits, ratio)。"""
    base = base or {}
    enabled = _cfg_bool(base, "repeat_guard_enabled", True)
    hits = base.get("repeat_guard_hits", 3)
    if isinstance(hits, bool) or not isinstance(hits, (int, float)) or int(hits) < 2:
        _warn_default("repeat_guard_hits", hits, "应为 ≥2 的整数", 3)
        hits = 3
    ratio = base.get("repeat_guard_ratio", 0.9)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not (0 < float(ratio) <= 1):
        _warn_default("repeat_guard_ratio", ratio, "应在 (0, 1] 区间", 0.9)
        ratio = 0.9
    return enabled, int(hits), float(ratio)


def is_repeat_streak(texts: list[str], hits: int = 3, ratio: float = 0.9,
                     min_len: int = 1) -> bool:
    """最近 hits 条文本是否高度雷同（模型 repeat 判据；纯函数，离线可测）。

    判定故意保守，三条都满足才算 repeat：
      ① 条数足够（len(texts) >= hits 且 hits >= 2）；
      ② 每条都非空、长度 >= min_len（「嗯。」「好。」这类短应答天然会连撞，不配当证据）；
      ③ 尾部 hits 条与其中第一条的相似度全部 >= ratio。
    反例（必须不判）：「好」→「好的」（SequenceMatcher 相似度 0.667 < 0.9）。
    """
    if hits < 2 or len(texts) < hits:
        return False
    tail = [(t or "").strip() for t in texts[-hits:]]
    if any(len(t) < max(1, min_len) for t in tail):
        return False
    first = tail[0]
    return all(SequenceMatcher(None, first, t).ratio() >= ratio for t in tail[1:])


class _SilenceGate:
    """长静音闸门 + preroll（治服务端 `model repeat output happened` 掉线）。

    用户实测：静音块占比经常 60~80%，模型被静音喂久了会进入 repeat 状态，
    服务端报 COMMON_ERROR 后 1011 掐线（4 小时日志里两条腿各断 8 次）。
    这里在连续静音 ≥ after_s 时**暂停上送**；闸住期间只保留最后 preroll_s 秒音频，
    声音恢复时先补发这段 preroll —— 句首的低能量部分（峰值还没到门限）不会丢。

    与打断/回合检测的关系：只**暂停上送**，不伪造、不改写音频；
    服务端 turn_detection 看到的仍是「静音 → （preroll）→ 说话」，与真人说话节奏一致。
    """

    def __init__(self, enabled: bool, after_s: float, preroll_s: float) -> None:
        self.enabled = enabled
        self.after_s = after_s
        self.preroll_s = preroll_s
        self.closed = False
        self._silent_since: float | None = None      # 连续静音的起点（None = 最近一块有声）
        self._preroll: deque[tuple[bytes, float]] = deque()
        self._preroll_dur = 0.0
        self.gated_chunks = 0        # 本次闸住累计拦截的块数（关闸时清零）
        self.replay_count = 0        # 累计补发 preroll 的次数

    def feed(self, chunk: bytes, *, loud: bool, dur_s: float, now: float) -> list[bytes]:
        """返回本次真正要上送的块序列（0..N 块；N>1 是开闸补发 preroll 的情形）。"""
        if loud:
            self._silent_since = None
            if self.closed:
                replay = [c for c, _ in self._preroll]
                dur = self._preroll_dur
                self._preroll.clear()
                self._preroll_dur = 0.0
                self.closed = False
                self.replay_count += 1
                print(f"[engine] 静音闸门打开：检测到声音 → 先补发 preroll "
                      f"{len(replay)} 块（≈{dur:.1f}s，不丢句首），随后恢复正常上送"
                      f"（本次闸住共拦截 {self.gated_chunks} 块）", flush=True)
                return replay + [chunk]
            return [chunk]
        # —— 静音块 ——
        if self._silent_since is None:
            self._silent_since = now
        if not self.closed:
            if not self.enabled or (now - self._silent_since) < self.after_s:
                return [chunk]
            self.closed = True
            self.gated_chunks = 0
            print(f"[engine] 静音闸门关闭：已连续静音 ≥{self.after_s:g}s → 暂停上送音频"
                  f"（保留最近 {self.preroll_s:g}s 作 preroll；防服务端 repeat 掉线）",
                  flush=True)
        self.gated_chunks += 1
        self._remember(chunk, dur_s)
        return []

    def _remember(self, chunk: bytes, dur_s: float) -> None:
        if self.preroll_s <= 0:
            return
        self._preroll.append((chunk, dur_s))
        self._preroll_dur += dur_s
        # +1μs 容差：0.1+0.1+0.1=0.30000000000000004，没容差会多吃掉一块（实测踩到）
        while self._preroll and self._preroll_dur > self.preroll_s + 1e-6:
            _, d = self._preroll.popleft()
            self._preroll_dur -= d


class _LevelGate:
    """输入响度门限：音量达不到门限的音频块**不上送**（只接 loopback 采集）。

    治什么：VRChat 输出是所有人混好的一路音频，远处玩家声音小、本来就听不清，
    以往照样送给模型 —— 白花钱，还容易被翻成乱话。这里按块 RMS（不是峰值）判响度，
    低于门限就不送；门限以上的照常送，并保证不把句子切坏：

    - **hold**：越阈值后的一段时间内，即使块回落到门限以下也继续送 ——
      说话句中的停顿、句尾渐弱不会被切碎（默认 500ms）。
    - **preroll**：开闸时把越阈值之前的一小段（默认 250ms）补上 ——
      句首辅音/起音不丢，否则识别会掉字。
    - 只**暂停上送**，不改写、不伪造音频（与服务端 turn_detection 的节奏假设一致）。

    与 `_SilenceGate` 的关系：职责不同、串联工作。本闸门治「小声」（用户要的过滤），
    静音闸门治「长时间真静音」（防服务端 repeat 掉线）。本闸门拦掉的块不会进入
    静音闸门，所以也不会被算进它的静音计时 —— 两者不会互相抵消。
    """

    def __init__(self, enabled: bool, threshold_db: float, hold_ms: float,
                 preroll_ms: int) -> None:
        self.enabled = enabled
        self.threshold_db = threshold_db
        self.hold_ms = hold_ms
        self.preroll_ms = preroll_ms
        self.is_open = False                 # 当前是否在上送（开闸）
        self._last_loud_ts: float | None = None
        self._preroll: deque[tuple[bytes, float]] = deque()
        self._preroll_ms = 0.0
        self.level_db = LEVEL_FLOOR_DB       # 最近一块的电平（界面实时显示用）
        self.dropped_chunks = 0              # 累计拦截（未上送）的块数
        self.opened = 0                      # 开闸次数
        self.replay_count = 0                # 补发 preroll 的次数

    def feed(self, chunk: bytes, *, now: float, dur_s: float) -> list[bytes]:
        """返回本次真正要上送的块序列（0..N 块；N>1 = 开闸补发 preroll 的情形）。"""
        db = chunk_level_db(chunk)
        self.level_db = db
        if not self.enabled:
            return [chunk]
        if db >= self.threshold_db:
            self._last_loud_ts = now
            if self.is_open:
                return [chunk]
            self.is_open = True
            self.opened += 1
            replay = [c for c, _ in self._preroll]
            self._preroll.clear()
            self._preroll_ms = 0.0
            if replay:
                self.replay_count += 1
                print(f"[gate] 输入门限：{db:.1f} dBFS ≥ 门限 {self.threshold_db:g} dBFS → "
                      f"开始上送，并补发之前 {len(replay)} 块（{self.preroll_ms}ms 内，"
                      f"防丢句首）", flush=True)
            return replay + [chunk]

        # —— 低于门限 ——
        if self.is_open:
            held = (self._last_loud_ts is not None
                    and (now - self._last_loud_ts) * 1000.0 <= self.hold_ms)
            if held:
                return [chunk]              # hold 期内继续送：句中的停顿不切碎
            self.is_open = False
            print(f"[gate] 输入门限：已回落 {self.hold_ms:g}ms 低于门限 "
                  f"{self.threshold_db:g} dBFS（当前 {db:.1f}）→ 暂停上送"
                  f"（累计已拦截 {self.dropped_chunks} 块）", flush=True)
        self.dropped_chunks += 1
        self._remember(chunk, dur_s)
        return []

    def _remember(self, chunk: bytes, dur_s: float) -> None:
        if self.preroll_ms <= 0:
            return
        self._preroll.append((chunk, dur_s))
        self._preroll_ms += dur_s * 1000.0
        # +1μs 容差：0.1+0.1+0.1=0.30000000000000004，没容差会多吃掉一块（同 _SilenceGate）
        while self._preroll and self._preroll_ms > self.preroll_ms + 1e-3:
            _, d = self._preroll.popleft()
            self._preroll_ms -= d * 1000.0


@dataclass
class EngineEvents:
    on_text: Callable[[str, str, bool], None] = lambda *_a: None     # (原文, 译文, is_final)
    on_status: Callable[[str, str], None] = lambda *_a: None         # (level, msg)
    on_stats: Callable[[dict], None] = lambda *_a: None              # 计数、延迟等


class _SessionProxy:
    """把采集循环的 `send_audio` 转发到**当前**会话。

    采集循环（麦克风/loopback）是长跑任务，启动时拿到的 session 引用会在
    自动重连后失效 —— 走代理每次动态取，重连后音频自然发到新连接上。
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine
        self.sent_bytes = 0
        self.chunks = 0
        self.silent_chunks = 0
        self.send_fails = 0          # 发送失败的块数（不计入静音统计，只用于留痕/诊断）
        self._voice_note_warned = False   # 「上报有人说话」失败只留一次痕（每块都打会刷屏）
        # 长静音闸门（②）：配置非法时 silence_gate_settings 已留痕并回落默认值
        en, after_s, preroll_s = silence_gate_settings(
            getattr(engine._cfg, "session_base", None))          # noqa: SLF001
        self._gate = _SilenceGate(enabled=en, after_s=after_s, preroll_s=preroll_s)

    async def send_audio(self, pcm: bytes) -> None:
        eng = self._engine
        # —— 输入侧埋点（黑匣子用）：这个类能看到**每一个**要发出去的输入块 ——
        eng._audio_in_chunks += 1                    # noqa: SLF001
        self.chunks += 1
        try:
            import numpy as np

            arr = np.frombuffer(pcm, dtype="<i2")
            peak = int(np.abs(arr).max()) if arr.size else 0
        except Exception:  # noqa: BLE001
            peak = 0
        loud = peak >= SILENCE_PEAK
        if loud:
            eng._last_loud_ts = time.monotonic()     # noqa: SLF001
            # 顺手把这声「有人在说」上报给会话：静默兜底的快路径靠它判断「上游已无人在说话」
            # （⚠️ 不能拿「距上次上送音频的间隔」代替 —— 麦克风腿没有闸门，静音块照样
            #   每 ~0.1s 上送一次，那个间隔恒为 ~0.1s；见 session/base.note_voice 的说明）。
            self._note_voice()
        else:
            self.silent_chunks += 1
            eng._silent_chunks += 1                  # noqa: SLF001
        # —— 长静音闸门：连续静音超阈值就暂停上送（闸住期间只留 preroll），
        #    声音恢复时先补发 preroll、再上送当前块 —— 绝不丢句首。
        #    闸住的块不算 sent_bytes（本来就没发），诊断里有 gate 自己的计数。
        chunks = self._gate.feed(pcm, loud=loud, dur_s=len(pcm) / 2 / 16000.0,
                                 now=time.monotonic())
        for chunk in chunks:
            await self._send_one(chunk)

    def _note_voice(self) -> None:
        """把「这一块有人在说话」上报给**当前**会话（重连后自动落到新会话上）。

        失败绝不能影响音频上送：这里吞掉异常、只留**一次**痕（每块都打会刷屏）。
        """
        session = self._engine._session             # noqa: SLF001
        if session is None:
            return
        try:
            session.note_voice()
        except Exception as exc:  # noqa: BLE001
            if not self._voice_note_warned:
                self._voice_note_warned = True
                print(f"[session] ⚠️ 上报「有人说话」失败（快封句退回慢路径）：{exc}",
                      flush=True)

    async def _send_one(self, pcm: bytes) -> None:
        session = self._engine._session             # noqa: SLF001
        # 发送量按「尝试发送」口径计：出问题时要靠它判断"到底说了多少话"，
        # 漏计会让诊断把"一直在说话"误读成"几乎没说话"。
        self.sent_bytes += len(pcm)
        if session is None:
            # 还没连上（或正在重连）也要计入——这是"输入侧"的度量，用于诊断
            return
        try:
            await session.send_audio(pcm)
        except Exception as exc:  # noqa: BLE001
            # ★ 采集循环是**长跑任务**，绝不能因为一次发送失败就整条腿死掉。
            # 用户实测日志（2026-09-25）：服务端 1011 掐断连接后，这里抛出的
            # ConnectionClosedError 一路冒到 `_run` 外层 → 该腿「运行错误」结束，
            # `_pump_loop` 里的看门狗根本没机会重连（日志里「事件循环中断」与
            # 「运行错误」只差 7ms，重连那条日志一行都没有）。
            # 这里只留痕并丢弃这一块：会话的接收循环已把它标成不健康，看门狗会在
            # 0.3s 内发起重连，之后的音频由本代理发到**新**会话上。
            self.send_fails += 1
            if self.send_fails == 1 or self.send_fails % 500 == 0:
                print(f"[session] ⚠️ 发送音频失败（第 {self.send_fails} 次，丢弃该块，"
                      f"等看门狗重连）：{type(exc).__name__}: {exc}", flush=True)
            return


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
        audio_sink=None,
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
        # 外部注入的译音输出（麦克风代理的 TranslatedSink）：非 None 时引擎**不自建**
        # VirtualMic，而是把译音 PCM 灌进代理那条常驻输出流（原声/译音一键切换）。
        # 归代理管生命周期 —— 引擎停翻译时**绝不能** close 它（见 _cleanup 的 _owns_virtualmic）。
        self._audio_sink = audio_sink

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._stopped = threading.Event()
        # 采集（mic/loopback）是无限循环，靠这个事件唤醒退出——stop() 与 _async_stop() 都会置位
        self._stop_event = threading.Event()

        self._session = None
        self._chatbox: Chatbox | None = None
        self._merger: Merger | None = None
        self._overlay: Any | None = None
        self._virtualmic: VirtualMic | None = None
        # True = _virtualmic 是引擎自建的（停翻译时要 close）；False = 外部注入的共享 sink
        # （归代理管，引擎不碰它的生命周期）。
        self._owns_virtualmic = False
        # 流式合成**串行**锁：同一时刻只让一路往虚拟声卡写分片（并发写会让分片交错，
        # 听感是「整段反复重念」—— 连打两条也会）。懒建：首次用时在事件循环线程里创建。
        self._speak_lock_obj: asyncio.Lock | None = None
        # 采集循环是长跑任务，不能直接持有 session 对象：重连会换新对象，
        # 旧引用会把音频继续发到死连接上。统一走这个代理。
        self._proxy = _SessionProxy(self)
        self._reconnect_task: asyncio.Task | None = None
        self._last_audio_ts = 0.0      # 上一段 TTS 音频到达时刻（判句边界）
        self._pending_seal = False     # 上一句终版文本已到 → 下一段音频前封句尾
        # ---- 黑匣子：断线定位用（不影响功能，只在断线时打出来）----
        self._audio_in_chunks = 0      # 发送出去的输入音频块数
        self._silent_chunks = 0        # 其中判为静音的块数
        self._last_loud_ts = 0.0       # 最近一次"有声音"的时刻
        self._audio_chunks = 0         # 收到的 TTS 音频块数
        self._text_deltas = 0          # 收到的译文本条数
        self._text_hist: deque[tuple[float, str]] = deque(maxlen=40)
        self._session_started_at = 0.0
        self._progress_at = None
        self._pump_task: asyncio.Task | None = None

        self._connect_ts: list[float] = []

        # ---- 本地 repeat 抑制（③）：模型连续吐高度雷同的最终译文 →
        # 不往 chatbox / 手腕屏刷（对方不该看到「对对对对对」），并主动重建会话 ----
        self._repeat_guard = repeat_guard_settings(getattr(cfg, "session_base", None))
        self._final_hist: deque[str] = deque(maxlen=self._repeat_guard[1])
        self._repeat_suppressed = False
        self._repeat_text = ""
        self._repeat_dropped = 0

        # ---- 输入门限（④，只作用于 loopback = VRChat 输出「别人说话」）----
        # 配置在 capture 段（capture.gate_*）；非法值由 input_gate_settings 留痕并回落默认值。
        # 麦克风腿（我说的话）**不走**这里：自己贴着麦说话，不该被响度门限拦。
        self._input_gate = _LevelGate(*input_gate_settings(
            (cfg.output or {}).get("capture") or {}))

        # ---- 出网端点（打字翻译 / 打字译音）：从 base_url 的 host 派生，启动时算一次并缓存 ----
        # 单一真相源 = session.base_url（见 vlt/endpoints.py 的「宿主派生」口径）：这里派生出
        # 另外两条 HTTP 端点，绝不在别处再写一份域名。派生失败（手写的 base_url 缺 scheme
        # 之类）→ **留痕**并回落千问云默认端点，绝不让 Engine 构造就崩；真正连接时实时那条腿
        # 会用 SessionConfig.url 再报一次明确错误。
        sb = cfg.session_base or {}
        if sb.get('provider') == endpoints.PROVIDER_CHATGPT:
            self._chat_endpoint = self._tts_endpoint = None
            print('[net] ChatGPT 订阅语音：不使用打字翻译/译音 HTTP 端点', flush=True)
            return
        base_url = sb.get("base_url") or endpoints.default_base_url(endpoints.DEFAULT_PROVIDER)
        try:
            self._chat_endpoint = endpoints.chat_url(base_url)
            self._tts_endpoint = endpoints.multimodal_url(base_url)
        except ValueError as exc:
            print(f"[net] ⚠️ 出网端点派生失败（{exc}）→ 打字翻译/译音回落千问云默认端点",
                  flush=True)
            _dflt = endpoints.default_base_url(endpoints.DEFAULT_PROVIDER)
            self._chat_endpoint = endpoints.chat_url(_dflt)
            self._tts_endpoint = endpoints.multimodal_url(_dflt)

    # ---------------------------------------------------------------- 公开接口

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping = False
        self._stopped.clear()
        self._stop_event.clear()
        t = threading.Thread(target=self._thread_run, daemon=True, name="vlt-engine")
        self._thread = t
        t.start()

    def request_stop(self) -> None:
        """只**发**停止信号，不等收尾（幂等、线程安全、立即返回）。

        为什么要有这个入口：`stop()` 会在**调用方线程**上等引擎收尾（本仓库停止时
        实测 ~10s，见下），而界面要停的不止一个引擎 —— 顺序调用就等于让第二个引擎
        在前一个收尾期间继续采集/上送（用户真机日志：点停止后 loopback 腿又跑了 10s）。
        界面现在先对所有引擎 `request_stop()`（并发下发，采集立刻停），收尾交给后台线程。
        """
        self._stopping = True
        self._stop_event.set()          # 直接置位（线程安全）：采集循环下个周期就退出
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._schedule_stop)

    def wait_stopped(self, timeout: float = 5.0) -> bool:
        """等本引擎**真正收尾完**（线程退出）。返回 False = 超时（线程可能还在跑）。

        只等收尾、不发信号 —— 与 `request_stop()` 配对给界面用。
        """
        ok = self._stopped.wait(timeout)
        if ok and self._thread is not None:
            self._thread.join(0.5)
            self._thread = None
        return ok

    def stop(self, timeout: float = 5.0) -> None:
        """阻塞版停止（CLI / 测试 / 关窗收尾用）：发信号后等收尾，最多 timeout 秒。

        界面按钮**不要**直接用它 —— 它在调用方线程上等，正是「停止翻译后窗口无响应」
        的来源（实测双向下 20.04s 无响应）。界面走 `request_stop()` + 后台 `wait_stopped()`。
        """
        if self._thread is None:
            return
        self.request_stop()
        self.wait_stopped(timeout)
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
    def chatbox_text_mode(self) -> str:
        """当前 chatbox 气泡显示哪种文本（每条现读，界面点一下立刻生效）。"""
        return chatbox_text_mode(self._cfg)

    def chatbox_payload(self, d: TextDelta) -> str:
        """本条 TextDelta 该往 chatbox 送什么文本（空串 = 本条不发）。

        译文模式：`display`（译文，与改动前逐字节一致）；
        原文模式：`d.source`（ASR 源文）。源文为空就返回空串 —— **绝不回落译文、
        绝不发占位符**（气泡里语言突然跳变比空一拍更糟）。
        """
        if self.chatbox_text_mode == CHATBOX_TEXT_SOURCE:
            return (d.source or "").strip()
        return d.display

    @property
    def virtualmic(self) -> VirtualMic | None:
        return self._virtualmic

    @property
    def input_gate(self) -> _LevelGate:
        """输入门限实例（loopback 腿用；界面读它的 level_db 显示实时电平）。"""
        return self._input_gate

    @property
    def session(self):
        return self._session

    @property
    def _chatbox_wanted(self) -> bool:
        """chatbox 只对「我说的话」方向有意义——对方的译文进手腕屏 / 聊天区，不进气泡。"""
        return "chatbox" in self._sinks and self._direction == "mine"

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

    def set_glossary(self, glossary: dict[str, str]) -> bool:
        """运行时更新**全局专有词库**：立即生效，但要重建会话。

        为什么必须重建：`corpus.phrases` 是 `session.update` 下发的会话级配置，
        服务端不会中途换词库（实时那条腿没有「热更新词库」的事件）。所以这里的
        语义与 `set_languages` 完全一致 —— 改内存 → 重建会话 → 新词库上线。
        打字那条腿不用重建：它每次调用都现读配置。

        返回 False 表示**这次没能生效**（预算不足），调用方必须让用户看见，
        绝不能出现「界面上词库已保存、实际还是老词库」这种静默不一致。
        """
        self._cfg.session_base["glossary"] = dict(glossary or {})
        return self._apply_hotwords_change("专有词库")

    def set_direction_hotwords(self, name: str, hotwords: dict[str, str],
                               label: str = "") -> bool:
        """运行时更新**某一条腿**的热词表（同名词条覆盖全局那几条）。

        与 `set_glossary` 的唯一区别是**写哪儿**：全局写 `session_base["glossary"]`，
        方向级写 `directions[name].hotwords` —— 合并口径仍是唯一那处
        `config.merge_hotwords`（方向级优先），两条腿都从它取，不另起一套。

        `label` 只用于给用户看的提示（界面传「我说」/「别人说」，
        别把内部 key `mine`/`theirs` 甩到状态栏里）。

        为什么要这个入口：两个方向的目标语言通常不同 —— 同一个社团名在
        「别人说 → 中文」要中文译名，在「我说 → 英文」却要保持原样。写进全局那份
        会同时作用到两侧（实测过反方向的译名被换掉），所以得能分开改。
        """
        d = self._cfg.directions.get(name)
        if d is None:
            # 未知方向：宁可返回 False + 留痕，也不能静默成功 ——
            # 调用方（界面）会把「没生效」如实显示出来。
            print(f"[engine] ⚠️ 配置里没有方向 {name!r}，热词未生效（只改了磁盘，内存里没这个方向）",
                  flush=True)
            return False
        d.hotwords = dict(hotwords or {})
        return self._apply_hotwords_change(f"「{label or name}」的热词")

    def _apply_hotwords_change(self, what: str) -> bool:
        """词库类改动共用的收尾：没会话就只改内存；有会话就按预算重建。

        `what` 是给用户看的名字（进警告文案），调用方给。
        """
        if self._session is None or self._loop is None or not self._loop.is_running():
            return True          # 还没开会话：下次创建时自然带上新词库
        now = time.monotonic()
        rpm = int(self._cfg.session_base.get("max_new_sessions_per_minute", 4))
        recent = [t for t in self._connect_ts if now - t < 60]
        if len(recent) >= rpm:
            self._events.on_status("warn",
                f"{what}已保存，但连接预算不足（{len(recent)}/{rpm} min），"
                f"本次未重建会话；停止后重新开始翻译即可生效")
            return False
        asyncio.run_coroutine_threadsafe(self._rebuild_session(), self._loop)
        return True

    # ---------------------------------------------------------------- 内部生命周期

    def _schedule_stop(self) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)

    async def _async_stop(self) -> None:
        """打断采集循环，让 _async_run 自然收尾。

        采集（mic / loopback）是**无限循环**，靠 stop_event 唤醒；
        PCM 源则在每个 chunk 之间检查 _stopping。不能用 cancel() 硬砍主协程，
        那样 _cleanup() 里的 await 会被打断，音频流/会话关不干净。
        """
        self._stopping = True
        self._stop_event.set()

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
            await self._drain_chatbox()
        except Exception:
            pass
        try:
            if self._overlay is not None:
                self._overlay.close()
        except Exception:
            pass
        try:
            if self._virtualmic is not None and self._owns_virtualmic:
                self._virtualmic.close()
            self._virtualmic = None
            self._owns_virtualmic = False
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

    async def _drain_chatbox(self) -> None:
        """停止收尾时排空 chatbox：**最新一条优先** + 墙钟有界 + 丢弃留痕。

        三个约束互相拉扯，少一个都不行：
        ① 有界 —— 排空就卡在收尾路径上，白等就是「点了停止还要卡好几秒」；
        ② 不丢最新一条 —— 积压里最值钱的恰是**最后**那句（用户刚说完、正等着上屏），
           按 FIFO 从最旧的开始发，预算一到就把最新那条挤掉了（上一版正是如此）；
        ③ 丢了要看得见 —— 本仓库禁静默丢弃，否则「译文少了一条」根本无从查起。
        """
        cb = self._chatbox
        if cb is None:
            return
        backlog = cb.pending_count
        if not backlog:
            return
        deadline = time.monotonic() + CHATBOX_DRAIN_BUDGET_S
        newest_sent = False
        while time.monotonic() < deadline:      # 至少跑一轮 ⇒ 最新一条必有一次机会
            if not newest_sent:
                newest_sent = cb.flush_pending(newest_first=True) > 0
            else:
                cb.flush_pending()              # 剩下的按原顺序补发，能发几条是几条
            if cb.pending_count == 0:
                break
            await asyncio.sleep(CHATBOX_DRAIN_TICK_S)
        left = cb.pending_count
        if left:
            why = ("最新一条也没排上（限流窗口整段都满）" if not newest_sent
                   else "最新一条已优先发出")
            print(f"[chatbox] 停止时放弃 {left}/{backlog} 条未发完的译文（{why}；"
                  f"排空预算 {CHATBOX_DRAIN_BUDGET_S:.1f}s，限流窗口内排不完，不会重发）",
                  flush=True)

    async def _build_and_run(self) -> None:
        scfg = self._cfg.directions[self._direction].to_session_config(self._cfg.session_base)

        # 宿主 + 线路必须留痕：换线路排查时第一眼要看它（describe 内部对 host 解析失败会降级，
        # key 绝不出现）。
        print(f"[net] {endpoints.describe(scfg.provider, scfg.base_url)}", flush=True)

        if "chatbox" in self._sinks and not self._chatbox_wanted:
            msg = ("chatbox 只发『我说的话』的译文"
                   "（当前方向不含『我说』→ 本次 chatbox 不会输出；"
                   "对方的译文照常进手腕屏 / 聊天区）")
            self._events.on_status("warn", msg)
            print(f"[engine] ⚠️ {msg}", flush=True)

        if self._chatbox_wanted:
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
            try:
                # 手腕屏后端按平台选（Windows=pyopenvr / Linux=自建 OpenXR），
                # 共享代码里不出现任何后端名字 —— 见 vlt/platform/__init__.py
                self._overlay = platform.create_wrist_overlay(
                    OverlayConfig.from_dict(self._cfg.overlay),
                    config_path=Path(self._config_path) if self._config_path else None,
                    dry_run=self._overlay_dry_run,
                )
                if not self._overlay.start() and not self._overlay_dry_run:
                    # start() 内部已打印原因（SteamVR 没跑 / overlay 创建失败）
                    self._overlay = None
            except Exception as exc:  # noqa: BLE001
                # 手腕屏挂不上**绝不能拖垮翻译**：chatbox / 译音照常工作。
                # 但必须留痕——用户实测过 overlay 报错把两条腿一起打死的场景
                # （`'IVRSystem' object has no attribute 'overlay'`）。
                self._overlay = None
                print(f"[overlay] ⚠️ 初始化异常，手腕屏已禁用（其余输出不受影响）："
                      f"{type(exc).__name__}: {exc}", flush=True)

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

        # 被 stop() 打断时不要再等收尾（否则点"停止翻译"后要卡 8 秒才响应）；
        # 未发完的最终版由 _cleanup() 里的 flush_pending 补发。
        if self._stopping:
            return
        self._events.on_status("info", f"等待收尾（{self._settle_s}s）…")
        await asyncio.sleep(self._settle_s)

    def _setup_virtualmic(self, audio_cfg: dict) -> None:
        """建立译音输出（虚拟声卡）。失败只禁用这一条腿，绝不影响其它功能。

        流程对两个平台是**同一条**：
            造出对象（可能顺带声明设备）→ 调 open() → 失败就清成 None
        Linux 的「造」这一步还会**运行时声明**一对 PipeWire 节点
        （无配置文件、不重启任何服务、不改任何全局状态），见 `vlt/platform/linux.py`。

        若外部注入了 `audio_sink`（麦克风代理的 TranslatedSink）→ 直接用它，
        **不自建 VirtualMic、不 pick 设备**：译音灌进代理那条常驻输出流，
        由代理的档位开关决定此刻放原声还是译音。
        """
        if self._audio_sink is not None:
            self._virtualmic = self._audio_sink
            self._owns_virtualmic = False
            return
        vm = self._make_audio_out(audio_cfg)
        if vm is None:
            return
        self._virtualmic = vm
        self._owns_virtualmic = True
        if not vm.open():
            self._virtualmic = None
            self._owns_virtualmic = False
            print(f"[virtualmic] 打开失败 → 译音输出已禁用（其余功能不受影响）：{vm.device_name}",
                  flush=True)
        # 打开成功不用再打印：open() 自己会报（Windows 经 on_status → 日志 + 状态栏）。
        # 这里以前多打了一行，还误用了不存在的属性 `sample_rate`（真实叫 `_sample_rate`），
        # 结果设备打开成功后那行日志直接抛 AttributeError，把整条翻译腿打死了 ——
        # 用户机器上有 VoiceMeeter 才会走到这条分支，我本机没虚拟声卡，本地测试全绿。

    def _make_audio_out(self, audio_cfg: dict):
        """造出译音输出对象（**不打开**）。返回 None = 这条腿不可用。

        Windows：按设备名/回退链找一个**已装好**的虚拟声卡（VB-Cable / VoiceMeeter），
                 用 PortAudio 打开 —— 那段逻辑原样保留，没动。
        Linux  ：运行时声明一对 PipeWire 节点（可写入端 + 虚拟麦），
                 再用 pw-cat 把 PCM 写进可写入端。
        """
        if platform.IS_LINUX:
            return platform.open_audio_out(audio_cfg, self._events.on_status)

        device_name = audio_cfg.get("device_name") or ""
        picked = None
        fallbacks: list[int] = []
        if device_name:
            idx = resolve_device_name(device_name, "output")
            if idx is not None:
                import sounddevice as sd
                info = sd.query_devices(idx)
                picked = (idx, str(info["name"]), int(info.get("default_samplerate", 48000)))
                # 同名设备在别的 host API 下的条目：WASAPI 端点打不开时按序回落
                # （Voicemeeter 实测 `-9999`，见 platform.output_device_fallbacks 的说明）
                fallbacks = platform.output_device_fallbacks(device_name, exclude=idx)
                self._events.on_status("info", f"按名称选中输出设备：{device_name!r} → #{idx}")
            else:
                self._events.on_status("warn",
                    f"未找到输出设备 '{device_name}'，回退到回退链")
        if picked is None:
            patterns = self._audio_device_override or audio_cfg.get("device")
            try:
                picked = pick_output_device(patterns)
            except Exception as exc:
                self._events.on_status("error", f"枚举输出设备失败：{exc}（其余功能不受影响）")
                return None
        if picked is None:
            chain = ", ".join(patterns) if patterns else "(默认回退链)"
            self._events.on_status("error",
                f"没找到匹配的输出设备（回退链：{chain}）。虚拟声卡装好了吗？其余功能不受影响。")
            return None
        idx, name, rate = picked
        # 同名回落候选：**两条选设备的分支都要算** —— 「自动回退链」档挑中的设备同样可能
        # 打不开（虚拟声卡的 WASAPI 端点真机必现 `-9999`），而这条分支原先 `fallbacks`
        # 一直是空的 → 只试一次就把「译音输出」整条腿判死（2026-10-06 测试者真机复现：
        # 设备名一空、档位落到回退链，译音输出直接禁用；指定设备名时反而没事）。
        if not fallbacks:
            fallbacks = platform.output_device_fallbacks(name, exclude=idx)
        return VirtualMic(
            device_index=idx,
            device_name=name,
            sample_rate=int(audio_cfg.get("sample_rate", 48000)),
            buffer_ms=int(audio_cfg.get("buffer_ms", 300)),
            max_buffer_ms=int(audio_cfg.get("max_buffer_ms", 2000)),
            on_status=self._events.on_status,
            device_fallbacks=fallbacks,
        )

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
        # 新会话 = 新的一页：repeat 抑制状态清零（上一段的重复历史不带过来）
        self._repeat_suppressed = False
        self._repeat_dropped = 0
        self._final_hist.clear()
        t0 = time.perf_counter()
        await self._session.start(
            on_text=self._on_text,
            on_audio=self._on_audio,
            on_usage=self._on_usage,
        )
        self._connect_ts.append(time.monotonic())
        self._session_started_at = time.monotonic()
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
                self._log_audio_progress()
                await self._watchdog()      # 会话挂了就自动重连（不再让这条腿永久死掉）

    def _log_audio_progress(self, now=None) -> None:
        if self._cfg.session_base.get('provider') != endpoints.PROVIDER_CHATGPT or self._session is None:
            return
        now = time.monotonic() if now is None else now
        if self._progress_at is not None and now-self._progress_at < 30:
            return
        self._progress_at = now
        try:
            snapshot = self._session.diagnostics()
            keys = ('alive', 'phase', 'bridge', 'source_events', 'translation_events',
                    'source_age_s', 'translation_age_s', 'source_chars', 'translation_chars',
                    'finalized', 'bridge_pending_bytes', 'bridge_consumed_bytes')
            progress = {key: snapshot.get(key) for key in keys
                        if type(snapshot.get(key)) in (bool, int, float, str, type(None))}
            progress.update(input_chunks=self._audio_in_chunks, send_fails=self._proxy.send_fails,
                            gate_dropped=self._input_gate.dropped_chunks,
                            gate_opened=self._input_gate.opened,
                            output_deltas=self._text_deltas,
                            repeat_dropped=self._repeat_dropped)
            print(f'[{self._direction}][progress] {json.dumps(progress)}', flush=True)
        except Exception:  # noqa: BLE001 — 诊断不能中断采集或泄露错误内容。
            print(f'[{self._direction}][progress] unavailable', flush=True)

    async def _feed_audio(self) -> None:
        if self._source.startswith("pcm:"):
            await self._feed_pcm(self._source[4:])
        elif self._source == "mic":
            capture_cfg = (self._cfg.output or {}).get("capture") or {}
            mic_name = capture_cfg.get("mic_device") or None
            await run_mic(self._proxy, None, device_pattern=mic_name, device_name=mic_name,
                          stop_event=self._stop_event)
        elif self._source == "loopback":
            capture_cfg = (self._cfg.output or {}).get("capture") or {}
            # Linux 上 loopback_device 配置被忽略（界面已不提供该下拉）：
            # 采集目标固定为「等 VRChat 的输出流」，见 run_loopback。
            loopback_name = None if platform.IS_LINUX else (capture_cfg.get("loopback_device") or None)
            if platform.IS_LINUX and capture_cfg.get("loopback_device"):
                print("[loopback] Linux：忽略配置里的 loopback_device="
                      f"{capture_cfg.get('loopback_device')!r}（改为自动等待 VRChat 音频输出）",
                      flush=True)
            await run_loopback(self._proxy, None, device_name=loopback_name,
                               stop_event=self._stop_event,
                               on_status=self._events.on_status,
                               gate=self._input_gate)
            # 输入门限只接在这条腿：VRChat 输出 = 别人说话，远处小声的玩家该被滤掉。

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
        if self._swallow_repeat(d, text):
            return                       # repeat 垃圾：chatbox / 手腕屏 / 界面气泡全都不刷
        if text:
            self._text_deltas += 1
            self._text_hist.append((time.monotonic(), text))
            self._events.on_text(d.source or "", text, d.is_final)
        if d.is_final:
            # 一句译音出完了 → 下一段音频起始处给它封句尾（虚拟麦整句丢弃的依据）
            self._pending_seal = True
        if self._overlay is not None:
            self._overlay.update(text, d.source or "")
        if self._merger is not None and self._chatbox_wanted:
            # 只换「往 chatbox 送什么」这一步：节流 / 去重 / 句末必刷全部复用同一个
            # Merger（构造一个载荷 TextDelta 给它，Merger 自身一行不改）。
            payload = self.chatbox_payload(d)
            if payload:
                self._merger.push(TextDelta(confirmed=payload, pending="",
                                            is_final=d.is_final, source=d.source or ""))
            elif self.chatbox_text_mode == CHATBOX_TEXT_SOURCE:
                # 原文模式下源文还没到 → 本条不发（不入 pending 队列、不重发旧内容）。
                # 留痕只走 print（crashlog 的 Tee 会落进日志文件），**不进状态栏**、
                # 不碰任何用户可见的表面；每条都打，不限频（用户明确要求）。
                print(f"[chatbox] 原文为空，本条跳过（is_final={d.is_final}）", flush=True)

    # 终版短于这个长度不参与 repeat 判定：「嗯。」「好。」这类短应答天然会连撞，
    # 把它们当证据会误杀正常对话（宁可晚一句判出真 repeat，也不可误杀真译文）。
    _REPEAT_MIN_LEN = 4

    def _swallow_repeat(self, d: TextDelta, text: str) -> bool:
        """本地 repeat 抑制（③）。返回 True = 本条是重复垃圾，所有输出面都不要刷。

        触发条件：连续 hits 条**最终译文**相似度 ≥ ratio（is_repeat_streak 纯函数判定）。
        触发后：打一行 [engine] 留痕（绝不静默）、当前及后续重复内容不再刷出、
        并按「服务端致命 error」的同一条思路主动重建会话（看门狗既有路径）。
        解除：出现一条明显不同的终版（模型恢复多样），或会话重建（_create_session 清零）。
        """
        enabled, hits, ratio = self._repeat_guard
        if not enabled:
            return False
        if self._repeat_suppressed:
            # 抑制中：只有「和那段重复内容明显不同」的终版出现才解除（模型已恢复多样）
            if d.is_final and text and \
                    SequenceMatcher(None, self._repeat_text, text).ratio() < ratio:
                self._repeat_suppressed = False
                self._final_hist.clear()
                print("[engine] 译文恢复多样 → 解除 repeat 抑制", flush=True)
                return False
            self._repeat_dropped += 1
            if self._repeat_dropped == 1 or self._repeat_dropped % 20 == 0:
                print(f"[engine] repeat 抑制中：已丢弃 {self._repeat_dropped} 条重复输出"
                      f"（会话重建后自动恢复）", flush=True)
            return True
        if not (d.is_final and text):
            return False
        if len(text.strip()) < self._REPEAT_MIN_LEN:
            # 短终版打断连击：模型还在正常出不一样的短内容，不配当 repeat 证据
            self._final_hist.clear()
            return False
        self._final_hist.append(text)
        if not is_repeat_streak(list(self._final_hist), hits=hits, ratio=ratio,
                                min_len=self._REPEAT_MIN_LEN):
            return False
        self._repeat_suppressed = True
        self._repeat_text = text
        self._repeat_dropped = 1
        print(f"[engine] ⚠️ 连续 {hits} 条最终译文高度雷同（相似度≥{ratio:.2f}），判为模型 repeat"
              f" → 这段重复不再往 chatbox / 手腕屏刷，并主动重建会话：{text[:60]!r}", flush=True)
        self._abort_session_for_repeat()
        return True

    def _abort_session_for_repeat(self) -> None:
        """按「服务端致命 error → 主动重建」的同一思路：把会话标死，看门狗走既有重连路径。"""
        s = self._session
        reason = f"本地 repeat 抑制：连续 {self._repeat_guard[1]} 条最终译文雷同"
        abort = getattr(s, "_abort_on_fatal", None)
        if callable(abort):
            abort(reason)                # is_alive=False → 看门狗 0.3s 内重连
            return
        # 会话实现没有主动放弃接口（测试替身 / 未来别的实现）→ 直接走重连调度，
        # 效果与看门狗路径一致（带退避 + RPM 预算，不会更凶）。
        if s is not None and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop(reason))

    def _on_audio(self, pcm: bytes) -> None:
        if self._virtualmic is None:
            return
        self._audio_chunks += 1
        stereo = resample_24k_mono_to_48k_stereo(pcm)
        # 句子边界（两条触发，缺一不可）：
        # ① 上一句的终版文本已到 → 现在这段音频属于新的一句；
        # ② 音频流静默 ≥ SENTENCE_GAP_S（终版文本没来时兜底）。
        # 虚拟麦靠句尾标记**整句**丢弃；没有它就只能从句中切断，
        # 用户实测就是「上一句 TTS 没说完就切到下一句」。
        now = time.monotonic()
        if self._pending_seal or (self._last_audio_ts
                                  and now - self._last_audio_ts >= SENTENCE_GAP_S):
            self._virtualmic.end_sentence()
        self._pending_seal = False
        self._last_audio_ts = now
        self._virtualmic.push(stereo)

    def _on_usage(self, u: dict) -> None:
        self._events.on_stats({k: v for k, v in u.items() if isinstance(v, int)})

    # ---------------------------------------------------------------- 打字输入

    def send_text(self, text: str) -> bool:
        """打字替代说话：翻译后当作「我说的一句终版」送进本引擎的输出面。

        线程安全（界面线程直接调用）；空文本 / 引擎没在跑 / 方向不是「我说」→ False。
        """
        text = (text or "").strip()
        if not text or self._loop is None or not self.running or self._chat_endpoint is None:
            return False
        if self._direction != "mine":
            # 打字替代的是**麦克风**，只对「我说」方向有意义；「别人说」那条腿的
            # 目标语言是用户自己的母语，把打字内容塞进去等于自己跟自己翻译。
            return False
        asyncio.run_coroutine_threadsafe(self._async_send_text(text), self._loop)
        return True

    async def _async_send_text(self, text: str) -> None:
        if self._chat_endpoint is None:
            return
        d = self._cfg.directions.get(self._direction) or Direction()
        tcfg = self._cfg.text_input or {}
        try:
            translated = await asyncio.to_thread(
                translate_text, text,
                target_lang=d.target_lang or "en",
                source_lang=d.source_lang,
                model=str(tcfg.get("model") or DEFAULT_TEXT_MODEL),
                api_key=str(self._cfg.session_base.get("api_key") or ""),
                timeout=float(tcfg.get("timeout_s", DEFAULT_TEXT_TIMEOUT_S)),
                # 地址按当前线路从 base_url 的 host 派生（启动时已算好并缓存）。
                endpoint=self._chat_endpoint,
                # 专有词库：与说话那条腿同一份（全局 + 方向级覆盖），
                # 让社团名/人名/术语按用户指定译法走，而不是被模型自由发挥。
                terms=terms_from_mapping(self._cfg.merged_hotwords(self._direction)),
            )
        except TextTranslateError as exc:
            self._events.on_status("error", f"打字翻译失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self._events.on_status("error", f"打字翻译异常：{type(exc).__name__}: {exc}")
            return

        # 下游与说话**完全一致**：界面气泡 + 手腕屏。唯一区别在 chatbox：
        # 说话是流式增量，超长时保留最新 144 字（越新越重要）；打字是一整句，
        # 截尾会吃掉开头、看着像翻译坏了 → 按上限切成多条依次发（受漏桶约束时
        # Chatbox 会把最终版排队补发，不会丢）。
        self._events.on_text(text, translated, True)
        if self._overlay is not None:
            self._overlay.update(translated, text)
        if self._chatbox is not None and self._chatbox_wanted:
            limit = int((self._cfg.chatbox or {}).get("max_chars", 144))
            # 原文模式：chatbox 收到的是你**敲的那句字**本身（与语音链路口径一致 ——
            # 气泡里始终是「我说的话」）。翻译仍然照常做（界面气泡 / 手腕屏 / TTS 都用它），
            # 只是不进 chatbox；所以这一步保持在翻译完成之后，不动共用时序。
            payload = text if self.chatbox_text_mode == CHATBOX_TEXT_SOURCE else translated
            for chunk in split_for_chatbox(payload, limit):
                self._chatbox.send(chunk, True)

        # 打字也要出声：文本翻译接口**不回音频**，所以补一步 TTS 再喂虚拟声卡。
        # 用的是**同一个** VirtualMic 实例（与语音共用一条流，句尾标记交给它管），
        # 所以「说话 + 打字」交替时不会互相打断、缓冲超限也照旧整句丢弃。
        spoke_s = 0.0
        truncated = False
        tts_cfg = tcfg.get("tts") or {}
        if self._virtualmic is not None and tts_cfg.get("enabled", True):
            kw = dict(
                voice=str(tts_cfg.get("voice") or DEFAULT_TTS_VOICE),
                model=str(tts_cfg.get("model") or DEFAULT_TTS_MODEL),
                api_key=str(self._cfg.session_base.get("api_key") or ""),
                language=d.target_lang,
                timeout=float(tts_cfg.get("timeout_s", DEFAULT_TTS_TIMEOUT_S)),
                reuse_conn=bool(tts_cfg.get("reuse_conn", True)),
                # 多模态地址同样按线路派生；整段/流式两条路共用这份 kw，故都带上 endpoint。
                endpoint=self._tts_endpoint,
            )
            try:
                if tts_cfg.get("stream", True):
                    # 流式（SSE）：首段音频 ~0.4s 就起播（整段合成要等 1.6~1.9s 才开口）。
                    # 走同一个串行锁：连打两条也不会两路分片交错（听感「反复重念」）。
                    async with self._speak_lock():
                        spoke_s, truncated = await asyncio.to_thread(
                            self._speak_stream, translated, kw)
                else:
                    pcm24 = await asyncio.to_thread(synthesize, translated, **kw)
                    self._virtualmic.push(resample_24k_mono_to_48k_stereo(pcm24))
                    self._virtualmic.end_sentence()
                    spoke_s = len(pcm24) / 2 / 24000
            except TtsError as exc:
                self._events.on_status("warn", f"打字译音失败：{exc}（文字输出不受影响）")
            except Exception as exc:  # noqa: BLE001
                self._events.on_status("warn",
                    f"打字译音异常：{type(exc).__name__}: {exc}（文字输出不受影响）")

        tail = f"，已出声 {spoke_s:.1f}s" if spoke_s else ""
        self._events.on_status("info", f"打字已送出（{len(text)} 字 → {d.target_lang}{tail}）")
        if truncated:
            # 中途断流：已播的部分保留（宁可少说半句），但必须让用户看见 ——
            # 否则「这句好像没说完」在界面上完全没有痕迹。
            self._events.on_status(
                "warn",
                f"打字译音只念了一半（网络/服务端中断，已保留已出声的 {spoke_s:.1f}s）")

    # ---------------------------------------------------------------- 流式出声

    def _speak_lock(self) -> asyncio.Lock:
        """流式合成的串行锁（懒建：首次调用发生在事件循环线程里）。

        为什么必须串行：分片是**按时间顺序**写进同一个抖动缓冲的，两路同时写会让
        彼此的分片交错，听感就是「整段反复重念」（总时长和转写都看不出问题）。
        """
        lock = self._speak_lock_obj
        if lock is None:
            lock = asyncio.Lock()
            self._speak_lock_obj = lock
        return lock

    def _speak_stream(self, text: str, kw: dict) -> tuple[float, bool]:
        """把流式合成的分片**就地**喂给虚拟声卡，返回 `(推入的秒数, 是否中途断了)`。

        在**工作线程**里跑（`asyncio.to_thread`）：迭代 SSE 是阻塞 IO。
        虚拟声卡自带抖动缓冲（攒到 buffer_ms 起播 / 停更 0.35s 强制起播），
        所以第一个分片就能让它开口 —— 打字腿「开口」从 ~1.7s 降到 ~0.5s。

        中途断流（`TtsStreamTruncated`）**保留已推入的部分**（宁可少说半句），
        把「断了」这件事交给调用方去提示 —— 用户听出「这句好像没说完」时，
        界面上不能什么都不说。
        """
        total = 0
        truncated = False
        try:
            for pcm24 in synthesize_stream(text, **kw):
                self._virtualmic.push(resample_24k_mono_to_48k_stereo(pcm24))
                total += len(pcm24)
        except TtsStreamTruncated:
            truncated = True
        finally:
            self._virtualmic.end_sentence()
        return total / 2 / 24000, truncated

    # ---------------------------------------------------------------- 断线自愈

    async def _watchdog(self) -> None:
        """会话挂了就自动重连。

        用户实测：服务端返回 1011 `model repeat output happened` 掐断连接后，
        那条腿直接「运行错误」结束，只能重开界面。现在改为自动重连（带退避，
        并复用 `_create_session` 里的 RPM 预算等待）。
        """
        if self._stopping or self._session is None:
            return
        if getattr(self._session, "is_alive", True):
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return                                  # 已有重连在跑
        reason = getattr(self._session, "fail_reason", "") or "连接已断开"
        self._dump_diagnostics(reason)             # 断线先留证据，再重连
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(reason))

    def _dump_diagnostics(self, reason: str) -> None:
        """断线时把「黑匣子」打出来 —— 用来定位服务端为什么掐断连接。

        专门回答两个问题：
        ① 断开前模型是不是在**重复输出**（连续多条译文本内容雷同）；
        ② 音频输入是不是**长期静音/噪声**（静音喂太久是模型 repeat 的常见诱因）。
        这些埋点平时不影响功能，只在断线时输出，所以可以放心留着。
        """
        now = time.monotonic()
        alive = (now - self._session_started_at) if self._session_started_at else 0.0
        sent_s = self._proxy.sent_bytes / 2 / 16000
        silence_ratio = (self._silent_chunks / self._audio_in_chunks * 100
                         if self._audio_in_chunks else 0.0)
        lines = [
            "─" * 64,
            f"[diag] 断线诊断（{reason}）",
            f"[diag] 会话存活 {alive:.0f}s | 发送输入音频 {sent_s:.1f}s "
            f"（{self._audio_in_chunks} 块，其中静音 {self._silent_chunks} 块 = {silence_ratio:.0f}%）",
            f"[diag] 收到译文本 {self._text_deltas} 条 | 收到 TTS 音频 {self._audio_chunks} 段",
        ]
        if self._last_loud_ts:
            lines.append(f"[diag] 最近一次检测到有效声音：{now - self._last_loud_ts:.1f}s 前"
                         f"（峰值门限 {SILENCE_PEAK}）")
        else:
            lines.append("[diag] 整段会话**从未**检测到有效声音 —— 一直在喂静音/噪声")
        gate = getattr(self._proxy, "_gate", None)
        if gate is not None and (gate.closed or gate.gated_chunks or gate.replay_count):
            lines.append(f"[diag] 静音闸门：当前{'关闭（暂停上送中）' if gate.closed else '打开'} | "
                         f"本次闸住已拦截 {gate.gated_chunks} 块 | 累计回补 preroll {gate.replay_count} 次")
        ig = self._input_gate
        if ig.enabled and (ig.dropped_chunks or ig.opened):
            lines.append(f"[diag] 输入门限（loopback）：门限 {ig.threshold_db:g} dBFS | "
                         f"当前{'上送中' if ig.is_open else '拦截中'} | "
                         f"已拦截 {ig.dropped_chunks} 块 | 开闸 {ig.opened} 次"
                         f"（补发 preroll {ig.replay_count} 次）| "
                         f"最近一块电平 {ig.level_db:.1f} dBFS")
        if self._repeat_dropped:
            lines.append(f"[diag] 本地 repeat 抑制：已丢弃 {self._repeat_dropped} 条重复输出"
                         f"（重复文本：{self._repeat_text[:40]!r}）")
        lines.append("[diag] 断开前最近 12 条译文本（看是否在重复同一句）：")
        tail = list(self._text_hist)[-12:]
        if not tail:
            lines.append("[diag]   （一条都没有 —— 模型根本没出过文本）")
        for t, txt in tail:
            lines.append(f"[diag]   t-{now - t:6.1f}s  {txt[:88]}")
        evts: list[tuple[str, float]] = []
        try:
            evts = list(self._session.recent_events())     # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"[diag] 断开前最近 {len(evts)} 个服务端事件（括号内为距今秒数）：")
        lines.append("[diag]   " + (" → ".join(f"{n}(-{ts:.1f}s)" for n, ts in evts[-20:])
                                    or "（无记录）"))
        lines.append("─" * 64)
        print("\n".join(lines), flush=True)

    async def _reconnect_session(self) -> None:
        """关掉旧会话再建一个（失败会抛，交给重试循环）。"""
        try:
            if self._session is not None:
                await self._session.close()
        except Exception:  # noqa: BLE001
            pass
        self._session = None
        scfg = self._cfg.directions[self._direction].to_session_config(self._cfg.session_base)
        await self._create_session(scfg)

    async def _reconnect_loop(self, reason: str) -> None:
        backoff = [float(x) for x in self._cfg.session_base.get("reconnect_backoff", [2, 5, 10, 30])]
        attempt = 0
        while not self._stopping:
            wait = backoff[min(attempt, len(backoff) - 1)]
            attempt += 1
            msg = f"连接断开（{reason}）→ {wait:.0f}s 后重连（第 {attempt} 次）"
            self._events.on_status("warn", msg)
            print(f"[session] ⚠️ {msg}", flush=True)
            await asyncio.sleep(wait)
            if self._stopping:
                return
            try:
                await self._reconnect_session()
                print(f"[session] ✅ 已重连（第 {attempt} 次）", flush=True)
                self._events.on_status("info", f"已重连（第 {attempt} 次）")
                return
            except Exception as exc:  # noqa: BLE001
                print(f"[session] ❌ 第 {attempt} 次重连失败：{type(exc).__name__}: {exc}",
                      flush=True)


# ================================================================ 音频采集函数


def _stop_requested(stop_event: threading.Event | None) -> bool:
    """采集循环的退出判据：stop() 会置位这个事件（线程安全）。"""
    return stop_event is not None and stop_event.is_set()


async def run_mic(session, tele, seconds: float = 0.0, device_pattern: str | None = None,
                  device_name: str | None = None,
                  stop_event: threading.Event | None = None) -> None:
    """麦克风采集（16kHz 单声道）。seconds=0 → 一直跑到 Ctrl+C 或 stop_event 置位。"""
    dev_name = _resolve_mic_name(device_name, device_pattern)
    if dev_name:
        print(f"[mic] 使用设备：{dev_name!r}")
    else:
        print("[mic] 使用系统默认输入设备")
    print("[mic] 开始采集" + ("（Ctrl+C 结束）" if seconds <= 0 else f"（{seconds:.0f}s）"))
    await _pump_capture(session, tele, seconds, stop_event, "mic",
                        lambda: platform.capture_backend().open_mic(
                            dev_name, rate=16000, channels=None,
                            blocksize=CHUNK_BYTES // 2))
    print("[mic] 采集结束")


def _resolve_mic_name(device_name: str | None, device_pattern: str | None) -> str | None:
    """把用户配置（设备名 / 关键词）解析成一个**设备名**，交给平台层打开。

    这里只负责「名字」，不碰索引/句柄：设备名怎么变成底层句柄按平台定 ——
    Linux 把描述映射到 PipeWire `node.name` 后走 `pw-record --target=`；
    Windows 由 `vlt/platform/win.py: open_mic` 再解析成 PortAudio 索引
    （保持 v0.3.x 同名端点的打开口径，见那边的说明）。
    `device_pattern` 是界面「设备关键词」那条兼容路径：在我们的输入设备表里做子串匹配。
    """
    if device_name:
        return device_name
    if not device_pattern:
        return None
    want = device_pattern.lower()
    for info in enumerate_mic_devices():
        if want in info.name.lower():
            print(f"[mic] 关键词 {device_pattern!r} 命中：{info.name!r}")
            return info.name
    print(f"[mic] ⚠️ 没找到匹配 '{device_pattern}' 的输入设备，改用系统默认设备")
    return None


async def _send_gated(session, tele, label: str, pcm16: bytes,
                      gate: "_LevelGate | None") -> int:
    """按输入门限放行一块 16k 单声道 PCM 并送到会话。返回**真正上送**的字节数。

    两条 loopback 采集腿（Windows 的 `_pump_capture` / Linux 的 `_pump_vrchat_capture`）
    共用这一处口径：判据只认**混好并重采样之后的同一块**，门限放行 0..N 块
    （开闸时带回 preroll，所以不是「要么全给要么不给」）；`sent` 与 telemetry
    也只算真正上送的块，被拦掉的不计。

    为什么要抽出来共用：#12 就是「同一件事在两条腿上各写一遍」写漏了一半 ——
    Linux 腿当初压根没接门限，界面说开了、实际不生效。口径只留这一处。
    """
    out_chunks = ([pcm16] if gate is None else
                  gate.feed(pcm16, now=time.monotonic(),
                            dur_s=len(pcm16) / 2 / 16000.0))
    sent = 0
    for c in out_chunks:
        await session.send_audio(c)
        sent += len(c)                    # 只算真正上送的（门限拦掉的不计）
        if tele is not None:
            tele.add(f"{label}_chunk", bytes=len(c))
    return sent


def _gate_tail(gate: "_LevelGate | None") -> str:
    """「采集结束」行上的输入门限小结（两条腿共用一处口径，别各写一遍）。"""
    if gate is not None and gate.enabled and (gate.dropped_chunks or gate.opened):
        return (f"｜输入门限（{gate.threshold_db:g} dBFS）：拦截 {gate.dropped_chunks} 块、"
                f"开闸 {gate.opened} 次")
    return ""


async def _pump_capture(session, tele, seconds: float, stop_event, label: str, opener,
                       gate: "_LevelGate | None" = None) -> int:
    """共用的采集泵：开流 → 拉块 → 送会话 → 一定关流。返回送出的字节数。

    两个平台（Windows 的 PortAudio 回调 / Linux 的 pw-record 子进程）在这里被抹平，
    引擎只看到 `await source.read()`。关流顺序由 `AudioSource.close()` 内部保证。
    """
    source = opener()
    end = None if seconds <= 0 else time.perf_counter() + seconds
    sent = 0
    try:
        while (end is None or time.perf_counter() < end) and not _stop_requested(stop_event):
            chunk = await source.read(timeout=1.0)
            if chunk is None:
                continue                      # 超时：还活着但暂时没数据
            pcm16 = to_16k_mono(chunk, source.rate, source.channels)
            if not pcm16:
                continue
            sent += await _send_gated(session, tele, label, pcm16, gate)
    finally:
        source.close()
    return sent


def pick_input_device(pattern: str | None) -> int | None:
    """按名称子串匹配输入设备。"""
    if not pattern:
        return None

    want = pattern.lower()
    # 走平台层的设备表（Windows 上已收敛到 WASAPI 已启用那一套）：不然同名设备会在
    # MME/DirectSound/WASAPI 下各命中一次，而这里取**第一条**，命中的是 MME（44100Hz）。
    for i, d in enumerate(platform.device_backend().query_devices()):
        if d["max_input_channels"] > 0 and want in str(d["name"]).lower():
            return int(d.get("pa_index", i))    # 过滤后列表下标 ≠ PortAudio 索引
    return None


# 各语言 Windows 的「默认设备」名前缀（比较小写）：中 / 英 / 日 / 韩 / 俄
_DEFAULT_DEVICE_PREFIXES = ("默认", "default", "デフォルト", "기본", "по умолчанию")


def _strip_loopback_suffix(name: str) -> str:
    """pyaudiowpatch 的 loopback 设备名 = 输出设备名 + loopback 后缀（实测有 "[Loopback]"/"(loopback)" 两种）。"""
    n = str(name).strip()
    low = n.lower()
    for suffix in ("[loopback]", "(loopback)"):
        if low.endswith(suffix):
            return n[: -len(suffix)].strip()
    return n


def pick_default_loopback(loops: list[dict], default_out_index: int | None,
                          default_out_name: str | None = None) -> dict | None:
    """从 WASAPI loopback 列表里挑出「系统默认输出设备」对应的 loopback（纯函数，离线可测）。

    优先级：
      1) `index` 精确命中（最可靠）；
      2) 名字与**默认输出设备名**一致（去掉 loopback 后缀、忽略大小写）；
      3) 多语言「默认」前缀兜底：默认 / Default / デフォルト / 기본 / По умолчанию
         —— 旧代码写死 `startswith("默认")`，英/日/俄 Windows 永不命中，
         会退化成「拿枚举到的第一个 loopback」，译音回灌可能写到错声卡、对方听不到；
      4) 都不中 → None（由调用方回落到「第一个 loopback」并留痕）。
    """
    loops = list(loops or [])
    if not loops:
        return None
    if default_out_index is not None and int(default_out_index) >= 0:
        for d in loops:
            if d.get("index") == default_out_index:
                return d
    if default_out_name:
        want = _strip_loopback_suffix(default_out_name).lower()
        if want:
            for d in loops:
                if _strip_loopback_suffix(str(d.get("name") or "")).lower() == want:
                    return d
    for d in loops:
        name = str(d.get("name") or "").strip().lower()
        if any(name.startswith(p) for p in _DEFAULT_DEVICE_PREFIXES):
            return d
    return None


# 「同一失败理由只留一行」的节流状态（见 `_note_loopback_enum_error`）。
_last_enum_err: str | None = None


def _note_loopback_enum_error(exc: Exception) -> None:
    """枚举设备失败留痕：**同一理由只留一行**，理由变了立刻再打一行。

    为什么需要节流：这个函数被两个重试循环调用 —— 电平探针的低频自愈
    （`level_probe.RETRY_S = 5.0`）与引擎 loopback 腿的重开。不节流就是
    **十几行/分钟**，把真正的信息淹掉。与 `level_probe._enter_waiting` 同一取舍：
    降级**不静默**（第一行一定在），只是不刷屏。
    """
    global _last_enum_err
    msg = f"[loopback] ❌ 枚举设备失败：{type(exc).__name__}: {exc}"
    if msg != _last_enum_err:
        _last_enum_err = msg
        print(msg, flush=True)


def _note_loopback_enum_ok() -> None:
    """枚举恢复正常：从失败里恢复时补一行 ✅（与 `level_probe._note_open` 同一取舍）。"""
    global _last_enum_err
    if _last_enum_err is not None:
        _last_enum_err = None
        print("[loopback] ✅ 设备枚举已恢复", flush=True)


def pick_loopback_target(patterns: list[str] | None = None,
                         device_name: str | None = None) -> LoopbackTarget | None:
    """挑一个「系统声采集」目标（纯逻辑 + 后端数据，两个平台共用）。

    优先级：
      1) 用户在界面上按名选的（精确 / 不区分大小写 / 子串，见 resolve_device_name）；
      2) 回退链关键词命中（`LOOPBACK_FALLBACK`）；
      3) 系统**默认输出设备**对应的那一份（靠 pick_default_loopback 的多语言前缀兜底）；
      4) 第一个可用设备，并且**留痕说明用了回退**（绝不静默选错）。

    平台差异被压在 `query_loopback_devices()` / `default_output_index()` 里：
    Windows 返回 WASAPI loopback 设备，Linux 返回 PipeWire 输出节点。
    """
    backend = platform.device_backend()
    try:
        loops = backend.query_loopback_devices()
    except Exception as exc:  # noqa: BLE001
        _note_loopback_enum_error(exc)      # 同一理由只留一行（重试循环里不会刷屏）
        return None
    _note_loopback_enum_ok()                # 从失败里恢复：补一行 ✅
    if not loops:
        return None

    def _as_target(d: dict) -> LoopbackTarget:
        # Linux 侧真正的打开标识是 node.name；Windows 侧是设备 index（字符串化）
        ident = str(d.get("node_name") or d.get("index"))
        return LoopbackTarget(id=ident, name=str(d.get("name", "")),
                              sample_rate=int(d.get("defaultSampleRate", 48000)),
                              channels=int(d.get("maxInputChannels", 2)))

    # ① 用户按名指定
    if device_name:
        idx = resolve_device_name(device_name, "loopback")
        if idx is not None:
            for d in loops:
                if int(d.get("index", -1)) == idx:
                    print(f"[loopback] 按名称选中：{device_name!r} → {d.get('name')!r}")
                    return _as_target(d)
        print(f"[loopback] ⚠️ 未找到设备 '{device_name}'，回退自动检测")

    # ② 回退链关键词
    chain = [p.lower() for p in (patterns or LOOPBACK_FALLBACK)]
    for kw in chain:
        for d in loops:
            if kw in str(d.get("name", "")).lower():
                print(f"[loopback] 关键词 {kw!r} 命中：{d.get('name')!r}")
                return _as_target(d)

    # ③ 默认输出设备对应的那一份
    try:
        default_out = backend.default_output_index()
        default_name = str(backend.device_info_by_index(default_out).get("name") or "")
        d = pick_default_loopback(loops, default_out, default_name)
        if d is not None:
            print(f"[loopback] 选中默认输出设备对应的采集端点：{d.get('name')!r}")
            return _as_target(d)
        label = default_name or f"#{default_out}"
        print(f"[loopback] ⚠️ 默认输出设备「{label}」在采集端点列表里匹配不上"
              f"（index / 设备名 / 多语言「默认」前缀都不中）"
              f"→ 回退到第一个：{loops[0].get('name')}", flush=True)
    except Exception:  # noqa: BLE001 — 拿不到默认设备不该致命，下面还有兜底
        pass

    # ④ 第一个
    return _as_target(loops[0])


def pick_vrchat_targets() -> list[LoopbackTarget]:
    """Linux：定位 **VRChat 的全部音频输出流**（= 别人说话）。找不到返回空列表。

    ⚠️ 刻意**不回落**到系统默认输出：VRChat 的播放输出在 PipeWire 里是应用流节点
    （`Stream/Output/Audio`），默认 sink 上还混着浏览器、音乐、系统提示音 ——
    抓默认 sink 会把它们一起当成「游戏内语音」喂给模型。
    用户口径：VRChat 没跑就**等**它跑起来，绝不改抓别的东西。

    ⚠️ 返回**列表**：VRChat（Wine）实测会开 2 个播放流（`audio stream #1` / `#5`），
    可能各自承载一部分声音，所以要每一路各开一条 `pw-record` 再混音。
    `LoopbackTarget.id` 用 `object.serial` —— `pw-record --target=` 只认「序列号或名称」，
    而这些节点的 `node.name` 全是 `VRChat.exe`，按名字区分不了多个流。

    平台差异压在 `CaptureBackend.find_vrchat_output_streams()` 后面
    （Windows 侧没有这个概念，`getattr` 取不到就返回空列表，永远走不到这里）。
    """
    if not platform.IS_LINUX:
        return []
    finder = getattr(platform.device_backend(), "find_vrchat_output_streams", None)
    if finder is None:
        return []
    try:
        rows = finder()
    except Exception as exc:  # noqa: BLE001 — 查不到不该让这条腿崩掉，交由上层重试
        print(f"[loopback] ⚠️ 查询 VRChat 音频输出失败：{type(exc).__name__}: {exc}", flush=True)
        return []
    targets: list[LoopbackTarget] = []
    for d in rows or []:
        ident = str(d.get("serial") or "")
        if not ident:
            continue
        targets.append(LoopbackTarget(
            id=ident,
            name=str(d.get("name") or d.get("node_name") or ident),
            sample_rate=int(d.get("defaultSampleRate", 48000)),
            channels=int(d.get("maxInputChannels", 2)),
        ))
    return targets


async def _wait_for_vrchat(stop_event, on_status=None, *,
                           poll_s: float = 2.0) -> list[LoopbackTarget] | None:
    """Linux：轮询等 VRChat 的音频输出流出现。返回 None = 被 stop 打断。

    「等」而不是「回落」是刻意的：这条腿只该抓 VRChat。VRChat 没起来时，
    采集腿保持空转等待，启动 VRChat 后自动接上（用户口径）。
    """
    announced = False
    while not _stop_requested(stop_event):
        targets = pick_vrchat_targets()
        if targets:
            return targets
        if not announced:
            announced = True
            msg = "等待 VRChat 音频输出（VRChat 启动后会自动开始采集）"
            print(f"[loopback] {msg}", flush=True)
            if on_status is not None:
                on_status("info", msg)
        await asyncio.sleep(poll_s)
    return None


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
    # 非整数倍（44100 / 22050 / 88200 …）：在**原始**采样点的时间轴上插值取点。
    #
    # ⚠️ 这里曾经有个把 44.1kHz 用户语音彻底毁掉的 bug：旧写法先按 down 抽稀
    #    （44100 → 每 441 个样点取 1 个），再把这批抽稀结果当成「相邻样点」插值，
    #    等于把语音降成约 100Hz 的包络再拉长。实测（真实中文语音，44100Hz 声卡）：
    #    与原始语音相关性 0.005、高频能量只剩 0.5%（原声 34.6%），ASR 一个字都认不出。
    #    真机日志印证：loopback 腿连续 900s「收到译文本 0 条」后被服务端超时掐断，
    #    而走 PortAudio 重采样的麦克风腿同场次正常出字。
    if len(a) < 2:
        return b""
    x = _lowpass(a, rate)
    n_out = int(len(a) * 16000 / rate)
    if n_out < 2:
        return b""
    pos = np.arange(n_out) * (rate / 16000.0)
    y = np.interp(pos, np.arange(len(a)), x)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


async def run_loopback(session, tele, patterns: list[str] | None = None,
                       seconds: float = 0.0, device_name: str | None = None,
                       stop_event: threading.Event | None = None,
                       on_status: Callable[[str, str], None] | None = None,
                       gate: "_LevelGate | None" = None) -> None:
    """采集 VRChat 的播放输出（= 别人说话）→ 推给会话。
    `gate` 是输入门限（`_LevelGate`）：VRChat 输出混了所有人，远处玩家声音小、
    本来就听不清，达不到门限的块就不上送（开闸时会带回 preroll 补句首）。
    只作用于这条腿 —— 麦克风（自己说话）不走门限。

    ⚠️ 两个平台都必须把 `gate` 用上（Linux 走 `_run_loopback_linux` 转发进
    `_pump_vrchat_capture`）。#12 的教训：合并时漏了 Linux 侧，界面上门限
    「开着」、实际一条块都没判。

    平台差异全部压在 `CaptureBackend.open_loopback()` 后面：
      * Windows：WASAPI loopback（pyaudiowpatch），非阻塞轮询 + 读线程；
                 端点由 `pick_loopback_target()` 挑（界面手选 / 回退链 / 默认输出）。
      * Linux  ：`pw-record --target=<VRChat 输出流>`，**等 VRChat 出现再采**，
                 VRChat 退出（输出流消失）后回到等待，VRChat 重新启动自动续上。
    """
    if platform.IS_LINUX:
        await _run_loopback_linux(session, tele, seconds, stop_event, on_status,
                                  gate=gate)
        return

    target = pick_loopback_target(patterns, device_name)
    if target is None:
        # ⚠️ 文案按平台分叉：这条分支只可能是 Windows（Linux 已在上面 return），
        # 以前统一写「PipeWire/音频服务」，Windows 用户照着找 PipeWire 纯属误导。
        hint = "PipeWire/音频服务" if platform.IS_LINUX else "系统音频服务（WASAPI）"
        print("[loopback] ❌ 没找到任何可采集的系统输出"
              f"（VRChat 在跑吗？{hint}正常吗？）")
        return
    print(f"[loopback] 采集端点「{target.name}」{target.sample_rate}Hz ×{target.channels}ch"
          f" → 16kHz 单声道")
    print("[loopback] 开始采集" + ("（Ctrl+C 结束）" if seconds <= 0 else f"（{seconds:.0f}s）"))
    sent = await _pump_capture(session, tele, seconds, stop_event, "loopback",
                               lambda: platform.capture_backend().open_loopback(
                                   target, blocksize=CHUNK_BYTES),
                               gate=gate)
    print(f"[loopback] 采集结束，共 {sent} bytes ≈ {sent / 2 / 16000:.0f}s{_gate_tail(gate)}")


async def _run_loopback_linux(session, tele, seconds: float, stop_event,
                              on_status, gate: "_LevelGate | None" = None) -> None:
    """Linux 的 loopback 腿：**等 VRChat → 定向采集（多路混音）→ 流变化 → 再等**。

    为什么不是「挑一个 sink 就开始录」：VRChat 的播放输出是应用流节点，
    而默认 sink 上同时混着浏览器/音乐/系统提示音 —— 抓 sink 会把它们一起
    当成「游戏内语音」喂给模型。所以这条腿只在 VRChat 在线时采集；
    VRChat 没跑（或中途退出）就空转等待，回来时重开 `pw-record`。

    VRChat 会开**多个**播放流（实测 2 路），逐路各开一条 `pw-record` 后混音。

    `gate`（输入门限）原样转给 `_pump_vrchat_capture`，由它在**多路混音之后**
    按块判 —— 与 Windows 腿同一处口径（`_send_gated`）。门限对象跨「等待 →
    采集 → 流变化 → 再等」整段存活，hold / preroll / 拦截计数连续累积。
    """
    total = 0
    while not _stop_requested(stop_event):
        targets = await _wait_for_vrchat(stop_event, on_status)
        if targets is None:
            break                                   # 被「停止翻译」打断
        detail = "、".join(f"{t.name}[{t.id}]" for t in targets)
        print(f"[loopback] 检测到 VRChat 音频：{len(targets)} 路（{detail}）"
              f"{targets[0].sample_rate}Hz ×{targets[0].channels}ch → 16kHz 单声道", flush=True)
        if on_status is not None:
            on_status("info", f"检测到 VRChat 音频（{len(targets)} 路）→ 开始采集")
        sent, stopped = await _pump_vrchat_capture(session, tele, stop_event, targets,
                                                   gate=gate)
        total += sent
        if stopped or seconds > 0:
            break
        print("[loopback] VRChat 音频输出已变化（退出 / 增减播放流）→ 重新等待/接上", flush=True)
        if on_status is not None:
            on_status("warn", "VRChat 音频输出消失 → 等待 VRChat 重新启动")
    print(f"[loopback] 采集结束，共 {total} bytes ≈ {total / 2 / 16000:.0f}s"
          f"{_gate_tail(gate)}", flush=True)


async def _pump_vrchat_capture(session, tele, stop_event,
                               targets: list[LoopbackTarget],
                               gate: "_LevelGate | None" = None) -> tuple[int, bool]:
    """采集 VRChat 的若干路输出流并混音。返回 (送出字节数, 是否被停止)。

    与共享的 `_pump_capture` 的区别有两点，都是被实测逼出来的：

      1. **多路**：VRChat（Wine）会开多个播放流，每路一条 `pw-record`，用
         `MixedAudioSource` 相加限幅成一路再送会话。
      2. 每 ~3s 回查一次 PipeWire 图：VRChat 退出（流全消失）**或播放流增减**
         就主动收尾 —— 不能只指望 `pw-record` 子进程自己退出：目标节点消失后
         它是**不会**退的，而共享泵只看「队列超时」是发现不了这件事的。

    `gate`（输入门限）：判在**多路混音 + 重采样之后**的同一块上，与 Windows 腿
    同口径（走 `_send_gated`）——「别人说」这条腿远处的玩家小声就该被滤掉。
    """
    opened: list = []
    for t in targets:
        opened.append(platform.capture_backend().open_loopback(t, blocksize=CHUNK_BYTES))
    source = opened[0] if len(opened) == 1 else MixedAudioSource(opened)
    want = sorted(t.id for t in targets)
    sent = 0
    last_check = time.monotonic()
    try:
        while not _stop_requested(stop_event):
            chunk = await source.read(timeout=1.0)
            if chunk is not None:
                pcm16 = to_16k_mono(chunk, source.rate, source.channels)
                if pcm16:
                    sent += await _send_gated(session, tele, "loopback", pcm16, gate)
            now = time.monotonic()
            if now - last_check >= VRCHAT_RECHECK_S:
                last_check = now
                if sorted(t.id for t in pick_vrchat_targets()) != want:
                    break            # 流没了 / 增减了 → 收尾，回到外层重新等待或重开
    finally:
        source.close()
    return sent, _stop_requested(stop_event)


def list_devices() -> None:
    """打印设备表（`--list-devices`）。**列的是程序实际会用的那一套**。

    Windows 上平台层已把设备收敛到 WASAPI 已启用端点（见 `platform/win.py: query_devices`），
    所以这里不再出现 MME/DirectSound/WDM-KS 的重复项与未启用幽灵 —— 与界面下拉一致。
    设备号打的是**真实 PortAudio 索引**（`pa_index`），可直接用于排查「打开到哪一路」。
    """
    devs = platform.device_backend().query_devices()

    def idx_of(d: dict, i: int) -> int:
        return int(d.get("pa_index", i))

    print("=== 输入设备（麦克风）===")
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0:
            print(f"  {idx_of(d, i):3d} | in={d['max_input_channels']} | "
                  f"{int(d['default_samplerate'])}Hz | {d['name']}")
    print("\n=== 输出设备 ===")
    for i, d in enumerate(devs):
        if d["max_output_channels"] > 0:
            print(f"  {idx_of(d, i):3d} | out={d['max_output_channels']} | "
                  f"{int(d['default_samplerate'])}Hz | {d['name']}")
