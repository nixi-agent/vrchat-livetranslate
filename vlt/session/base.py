"""会话层抽象：把「不同代次模型的字段/事件差异」全部关在这一层里。

下游（节流合并器 / chatbox / overlay / 虚拟麦）只消费 TextDelta 与 audio bytes，
永远不知道背后是 qwen3.8 还是 qwen3.5。换模型 = 换一个实现类，下游零改动。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Callable

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
    base_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    workspace_id: str = ""              # 非空则用 maas 域名
    api_key: str = ""
    # 连接预算（RPM 10：每次 WS 连接算一次请求）
    reconnect_backoff: tuple[int, ...] = (2, 5, 10, 30)
    max_new_sessions_per_minute: int = 4
    # 静默兜底：服务端在持续音频下可能长时间不发 response.done（实测），
    # 超过这个静默时长就把累计文本当最终版发出，保证「句末刷最终版」必达。
    # ⚠️ 必须大于服务端的「增量间隔」：实测连续说话时相邻 delta 可间隔 2.3s，
    #    阈值太小会在句子中间抢跑，把半句当成最终版。
    final_silence_s: float = DEFAULT_FINAL_SILENCE_S

    @property
    def url(self) -> str:
        base = self.base_url
        if "{workspace_id}" in base:
            if not self.workspace_id:
                raise ValueError("该 base_url 需要 workspace_id（百炼控制台「业务空间详情」）")
            base = base.replace("{workspace_id}", self.workspace_id)
        return f"{base}?model={self.model}"


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
    model = cfg.model.lower()
    if model.startswith("qwen3.8-livetranslate") or model.startswith("qwen3.5-livetranslate"):
        from .qwen38 import QwenLiveTranslateSession  # 3.5/3.8 共用一套写出逻辑，差异在事件映射
        return QwenLiveTranslateSession(cfg)
    raise ValueError(f"暂不支持的模型：{cfg.model}（在 create_session 里登记新代次实现）")
