"""会话层抽象：把「不同代次模型的字段/事件差异」全部关在这一层里。

下游（节流合并器 / chatbox / overlay / 虚拟麦）只消费 TextDelta 与 audio bytes，
永远不知道背后是 qwen3.8 还是 qwen3.5。换模型 = 换一个实现类，下游零改动。
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import endpoints

# ---------------------------------------------------------------- 归一化事件


@dataclass
class TextDelta:
    """归一化后的译文文本事件。

    confirmed : 已确认的累计译文（本段内的完整已确认部分）
    pending   : 尚未确认的预测文本（qwen3.5 的 `stash`；qwen3.8 恒为空串）
    is_final  : 本段响应结束（response.done / response.text.done）
    source    : 源语言识别结果（3.8 默认就会返回，可做「原文+译文」双行）
    """

    confirmed: str = ""
    pending: str = ""
    is_final: bool = False
    source: str = ""

    @property
    def display(self) -> str:
        """给用户看的文本：已确认 + 预测（预测部分会被后续事件替换，不能累加）。"""
        return (self.confirmed + self.pending).strip()


# 静默兜底默认值（全仓库唯一真相）：
# 必须大于服务端「增量间隔」（实测连续说话时相邻 delta 最大 2.3s），
# 阈值太小会在句子中间抢跑，把半句当成最终版。
DEFAULT_FINAL_SILENCE_S = 3.0

# 「上游也没人说话了」的快封句（真链路实测 2026-10-01：省 ~1.8s）：
# 实测（真链路 2 句）：服务端在**用户停止说话后 8s 内一条事件都不发**
# （既无 response.text.done 也无 response.done）→ 没有语义完成信号可用，只能靠定时器；
# 而累计译文在「说完前 0.74~0.89s」就不再增长 → 之后再等 3s 全是白等。
# 所以：**上游已无「有人说话」的电平 ≥ fast_user_quiet_s**（用户确实说完了）时，
# 文字静默只要 fast_silence_s 就封句；还在说（< fast_user_quiet_s）则仍用保守的 3.0s
# —— 那 2.3s 的大间隔是**句子中间**的停顿，不能拿它当证据。
#
# ⚠️ 判据必须是**电平**（`_SessionProxy.send_audio` 里 `peak >= SILENCE_PEAK` → note_voice()），
#    不能拿「距上次上送音频的间隔」：麦克风腿**没有闸门**（`_SilenceGate` 只挂在环回腿上、
#    阈值同 silence_gate_after_s 默认 30s），真人说话时静音块照样每 ~0.1s 上送一次
#    → 那个间隔恒为 ~0.1s，快路径永远不触发（实测脚本 out/check_pr49_mic_signal.py）。
DEFAULT_FAST_FINAL_SILENCE_S = 1.1
DEFAULT_FAST_FINAL_USER_QUIET_S = 0.5


def should_finalize(*, text_quiet_s: float, user_quiet_s: float | None,
                    silence_s: float = DEFAULT_FINAL_SILENCE_S,
                    fast_silence_s: float | None = DEFAULT_FAST_FINAL_SILENCE_S,
                    fast_user_quiet_s: float = DEFAULT_FAST_FINAL_USER_QUIET_S) -> bool:
    """该不该把累计文本封成「最终版」（纯函数，便于离线测）。

    两条路径：
    - **慢**：文字静默 ≥ `silence_s`（3.0s）。任何时候都成立，但用户说完后要白等 3s。
    - **快**：`fast_silence_s` 非 None、**上游已无人在说话 ≥ `fast_user_quiet_s`**、
      且文字静默 ≥ `fast_silence_s`。都不说了，服务端的尾巴（实测最后一条分片在
      说完前 0.7~0.9s 就到齐）就不会再长 → 可以早封。

    `user_quiet_s=None` = 这条腿**从没收到过**「有人说话」的信号（没声音，或信号没接上）
    → **走慢路径**（不早封）。⚠️ 这里宁可保守：抢跑会把半句当最终版发出去，而多等 3s 只是慢；
    且「从没说过话」时 `tick()` 本来就没文本可封。信号来源见 `LiveTranslateSession.note_voice()`
    （由 `_SessionProxy.send_audio` 按**电平**上报）。
    """
    if text_quiet_s >= silence_s:
        return True
    if fast_silence_s is None or user_quiet_s is None:
        return False
    if user_quiet_s < fast_user_quiet_s:
        return False                      # 上游还在说话：服务端可能只是慢，别抢跑
    return text_quiet_s >= fast_silence_s


@dataclass
class SessionConfig:
    """一条会话的全部可配置项。模型与目标语言都在这里，故都可插拔。"""

    model: str = "qwen3.8-livetranslate-flash-realtime"
    target_lang: str = "en"
    source_lang: str | None = None      # None = 自动识别
    output_audio: bool = False          # True → ["text","audio"]，同时拿译音
    voice: str = "Tina"                 # 实测：不指定会抛 Voice 'Chelsie' is not supported
    hotwords: dict[str, str] = field(default_factory=dict)
    turn_detection: str | None = None   # None = 用服务端默认（3.8 为 speaker_detection）
    # base_url 是**唯一的地址真相源**（默认值也走 endpoints 派生，不再写第二份域名字面量）。
    base_url: str = endpoints.default_base_url(endpoints.PROVIDER_QIANWEN)
    # provider **仅供日志与诊断**：实际连到哪永远以 base_url 为准（见 endpoints
    # 模块的「宿主派生」口径）。切线路由界面同步改写 base_url，这里只是把选择带出来留痕。
    provider: str = endpoints.DEFAULT_PROVIDER
    api_key: str = ""
    # 连接预算（RPM 10：每次 WS 连接算一次请求）
    reconnect_backoff: tuple[int, ...] = (2, 5, 10, 30)
    max_new_sessions_per_minute: int = 4
    # 静默兜底：服务端在持续音频下可能长时间不发 response.done（实测），
    # 超过这个静默时长就把累计文本当最终版发出，保证「句末刷最终版」必达。
    # ⚠️ 必须大于服务端的「增量间隔」：实测连续说话时相邻 delta 可间隔 2.3s，
    #    阈值太小会在句子中间抢跑，把半句当成最终版。
    final_silence_s: float = DEFAULT_FINAL_SILENCE_S
    # 快封句（麦克风也静了 → 用户确实说完了）：文字静默到这个值就封，省 ~1.8s。
    # None = 关掉快路径（退回纯 final_silence_s）。
    fast_final_silence_s: float | None = DEFAULT_FAST_FINAL_SILENCE_S
    fast_final_user_quiet_s: float = DEFAULT_FAST_FINAL_USER_QUIET_S

    @property
    def url(self) -> str:
        # 两条线路的 base_url 都是可直接连接的公共地址（无占位符、无账号成分），
        # 这里只负责拼上 ?model=。
        return f"{self.base_url}?model={self.model}"


# ---------------------------------------------------------------- 会话接口

TextHandler = Callable[[TextDelta], None]
AudioHandler = Callable[[bytes], None]      # 24kHz 单声道 s16le PCM
UsageHandler = Callable[[dict], None]


class LiveTranslateSession(abc.ABC):
    """一条双工实时翻译会话。实现类负责该代次的事件名/字段分派。"""

    def __init__(self, cfg: SessionConfig) -> None:
        self.cfg = cfg
        self.on_text: TextHandler | None = None
        self.on_audio: AudioHandler | None = None
        self.on_usage: UsageHandler | None = None
        # 原始服务端事件钩子（调试/埋点用）：(event_type, full_event) -> None
        self.on_event = None
        # 最近一次「上游有人说话」的时刻（`note_voice()` 打点；0.0 = 这条腿全程没声音）
        self._last_voice_at: float = 0.0

    def note_voice(self) -> None:
        """上游告诉会话：**刚刚这一块音频是「有人在说话」**（电平过 `SILENCE_PEAK`）。

        只给静默兜底用（`tick()` 的快路径判据）。谁调用：`engine._SessionProxy.send_audio`
        —— 它本来就在算 `loud = peak >= SILENCE_PEAK`，顺手把「有人在说」上报给会话。

        ⚠️ 判据为什么不能用「距上次上送音频的间隔」：麦克风腿**没有闸门**（静音块照样
        每 ~0.1s 上送一次，`_SilenceGate` 只挂在环回腿、阈值默认 30s）→ 那个间隔恒为
        ~0.1s，快路径永不触发（实测脚本 `out/check_pr49_mic_signal.py`）。
        """
        self._last_voice_at = time.perf_counter()

    def user_quiet_s(self, now: float | None = None) -> float | None:
        """距上游最后一次「有人说话」过去多久；这条腿全程没声音 → `None`（按「已静」看待）。"""
        if not self._last_voice_at:
            return None
        return (now if now is not None else time.perf_counter()) - self._last_voice_at

    @abc.abstractmethod
    async def start(self, on_text: TextHandler, on_audio: AudioHandler | None = None,
                    on_usage: UsageHandler | None = None) -> None:
        """建立连接、下发 session.update、启动事件循环。"""

    @abc.abstractmethod
    async def send_audio(self, pcm16_16k: bytes) -> None:
        """推送一段 16kHz 单声道 s16le PCM。"""

    @abc.abstractmethod
    async def close(self) -> None:
        """优雅结束（发 session.finish 并断连）。"""


def create_session(cfg: SessionConfig) -> LiveTranslateSession:
    """按模型名前缀分发到对应代次的实现（可插拔点）。"""
    if cfg.provider == endpoints.PROVIDER_CHATGPT:
        from .chatgpt_live import ChatGPTLiveSession
        return ChatGPTLiveSession(cfg)
    model = cfg.model.lower()
    if model.startswith("qwen3.8-livetranslate") or model.startswith("qwen3.5-livetranslate"):
        from .qwen38 import QwenLiveTranslateSession  # 3.5/3.8 共用一套写出逻辑，差异在事件映射
        return QwenLiveTranslateSession(cfg)
    raise ValueError(f"暂不支持的模型：{cfg.model}（在 create_session 里登记新代次实现）")
