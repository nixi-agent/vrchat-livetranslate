"""qwen3.8 / qwen3.5-livetranslate-flash-realtime 的会话实现。

**本文件是「跨代差异」的唯一收敛点**：事件名、字段名、断句配置的代次差异都在这里映射，
下游只拿到归一化的 TextDelta / audio bytes。

实测记录（2026-09-24，详见 docs/P0.5-实测结果.md）：
- 旧域名 wss://dashscope.aliyuncs.com/api-ws/v1/realtime 可用（无需 WorkspaceId）
- session.update **必须带 translation**，否则 `Invalid translation parameter.`（服务端不做字段合并）
- voice 必须显式给，否则抛 `Voice 'Chelsie' is not supported.`（即使纯文本模式也校验）
- 首文本增量约 +1.0s，首音频增量约 +1.2~1.5s
- RPM 10，且**每次 WS 连接算一次请求**
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import deque
from typing import Any

import websockets

from .base import AudioHandler, LiveTranslateSession, SessionConfig, TextDelta, TextHandler, UsageHandler


class ConnectionBudget:
    """RPM 预算：滑动窗口内限制新建连接数（实测 RPM 10 会被快速重连撞掉）。"""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(1, max_per_minute)
        self._stamps: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] > 60.0:
                self._stamps.popleft()
            if len(self._stamps) < self.max_per_minute:
                self._stamps.append(now)
                return
            wait = 60.0 - (now - self._stamps[0]) + 0.05
            print(f"[session] 连接预算已满（{self.max_per_minute}/min），等待 {wait:.1f}s")
            await asyncio.sleep(wait)


class QwenLiveTranslateSession(LiveTranslateSession):
    """3.8 / 3.5 共用；差异通过 _map_event() 按代次分派。"""

    def __init__(self, cfg: SessionConfig) -> None:
        super().__init__(cfg)
        self._gen = "38" if cfg.model.lower().startswith("qwen3.8") else "35"
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._closing = False
        self._buf: list[str] = []          # 本段已确认文本的增量累加
        self._src_buf: list[str] = []
        self._last_text_at: float = 0.0    # 最近一次文本增量时间（静默兜底用）
        self._last_final_text = ""         # 最近一次已发出的最终版文本（允许再次终结）
        self._budget = ConnectionBudget(cfg.max_new_sessions_per_minute)
        # 延迟埋点
        self._t_speech_start: float | None = None
        self._first_delta_ms: float | None = None
        self.first_delta_ms: float | None = None   # 对外暴露

    # ------------------------------------------------------------ 生命周期
    async def start(self, on_text: TextHandler, on_audio: AudioHandler | None = None,
                    on_usage: UsageHandler | None = None) -> None:
        self.on_text, self.on_audio, self.on_usage = on_text, on_audio, on_usage
        await self._budget.acquire()
        self._ws = await websockets.connect(
            self.cfg.url,
            additional_headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
        )
        # 3.8 连上后会先给 session.created
        try:
            first = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=10))
            if first.get("type") == "error":
                raise RuntimeError(f"服务端拒绝会话：{json.dumps(first.get('error') or first, ensure_ascii=False)}")
        except asyncio.TimeoutError:
            pass
        await self._ws.send(json.dumps({"event_id": "evt_update", "type": "session.update",
                                        "session": self._session_payload()}))
        self._recv_task = asyncio.create_task(self._recv_loop())

    def _session_payload(self) -> dict:
        """⚠️ 必须整体下发且必须含 translation（实测：缺任一必要字段会被整体拒绝）。"""
        session: dict = {
            "translation": {"language": self.cfg.target_lang},
        }
        if self._gen == "38":
            session["output_modalities"] = ["text", "audio"] if self.cfg.output_audio else ["text"]
        else:
            session["modalities"] = ["text", "audio"] if self.cfg.output_audio else ["text"]
        if self.cfg.voice:
            session["voice"] = self.cfg.voice
        if self.cfg.source_lang:
            session["input_audio_transcription"] = {"language": self.cfg.source_lang}
        if self.cfg.hotwords:
            session["translation"]["corpus"] = {"phrases": self.cfg.hotwords}
        if self.cfg.turn_detection:
            if self._gen == "38":
                session["audio"] = {"input": {"turn_detection": {"type": self.cfg.turn_detection}}}
            else:
                session["turn_detection"] = {"type": self.cfg.turn_detection}
        return session

    async def send_audio(self, pcm16_16k: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("会话尚未 start()")
        await self._ws.send(json.dumps({
            "event_id": "evt_audio",
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16_16k).decode(),
        }))

    @property
    def is_alive(self) -> bool:
        """会话是否还活着（接收循环没结束、且不是正常关闭）。"""
        if self._closing or self._ws is None or self._recv_task is None:
            return False
        return not self._recv_task.done()

    @property
    def fail_reason(self) -> str:
        """接收循环异常退出时的原因（用于日志与重连提示）。"""
        if self._recv_task is None or not self._recv_task.done():
            return ""
        try:
            exc = self._recv_task.exception()
        except Exception:  # noqa: BLE001  （cancelled 时取 exception 会抛）
            return "接收循环被取消"
        if exc is None:
            return "接收循环已结束"
        return f"{type(exc).__name__}: {exc}"[:200]

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"event_id": "evt_finish", "type": "session.finish"}))
                await asyncio.sleep(0.3)
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()

    # ------------------------------------------------------------ 事件循环
    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._closing:
                    return
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                self._handle_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._closing:
                print(f"[session] 事件循环中断：{type(exc).__name__}: {exc}")

    def _handle_event(self, ev: dict) -> None:
        etype = ev.get("type", "")
        if self.on_event is not None:
            try:
                self.on_event(etype, ev)
            except Exception:
                pass

        # --- 生命周期 ---
        if etype == "error":
            print(f"[session] ⚠️ 服务端 error: {json.dumps(ev.get('error') or ev, ensure_ascii=False)[:300]}")
            return
        if etype == "input_audio_buffer.speech_started":
            self._t_speech_start = time.perf_counter()
            return
        if etype == "response.created":
            self._buf.clear()
            return

        # --- 归一化文本（按代次分派）---
        textish = self._map_text_event(etype, ev)
        if textish is not None:
            confirmed_delta, pending, final_text = textish
            if confirmed_delta:
                self._buf.append(confirmed_delta)
            if final_text is not None:
                self._emit(confirmed=final_text, pending="", is_final=True)
            else:
                self._emit(confirmed="".join(self._buf), pending=pending, is_final=False)
            return

        # --- 音频增量 ---
        if etype == "response.audio.delta":
            data = ev.get("delta") or ""
            if data and self.on_audio is not None:
                self.on_audio(base64.b64decode(data))
            return

        # --- 源语言识别（3.8 默认就会返回，无需显式配置）---
        if etype in ("conversation.item.input_audio_transcription.delta",
                     "conversation.item.input_audio_transcription.text"):
            frag = ev.get("delta") or ev.get("text") or ""
            if frag:
                self._src_buf.append(frag)
            return
        if etype == "conversation.item.input_audio_transcription.completed":
            self._src_buf = [ev.get("transcript") or ev.get("text") or "".join(self._src_buf)]
            return

        # --- 响应结束 ---
        if etype == "response.done":
            usage = (ev.get("response") or {}).get("usage") or {}
            # 某些情况下没有 done 事件带全文，则用累加值兜底（重复终结由 _emit 去重）
            self._emit(confirmed="".join(self._buf), pending="", is_final=True)
            if self.on_usage is not None and usage:
                self.on_usage(usage)
            self._src_buf = []
            return

    def tick(self) -> None:
        """静默兜底（由调用方周期触发）。

        实测发现：持续推送音频时，服务端可能长时间不发 `response.text.done` /
        `response.done`（它靠检测静音来收尾一段话）。若只等 done 事件，
        「句末刷最终版」就永远不会触发 —— 所以按静默时长兜底。

        注意两点（都是实测踩出来的）：
        1) 阈值必须大于服务端的增量间隔（实测最大 2.3s），否则会在句子中间抢跑；
        2) **不能一发就永久封死**——长句后续还会有增量，文本变了就应再次终结。
        """
        if self._closing or not self._buf:
            return
        cur = "".join(self._buf)
        if cur == self._last_final_text:
            return
        if self._last_text_at and (time.perf_counter() - self._last_text_at) >= self.cfg.final_silence_s:
            self._emit(confirmed=cur, pending="", is_final=True)

    def _map_text_event(self, etype: str, ev: dict) -> tuple[str, str, str | None] | None:
        """返回 (confirmed_delta, pending, final_text)；final_text 非 None 表示本段结束。

        代次差异（官方文档 + 实测）：
          3.8 纯文本   : response.text.delta（累加）/ response.text.done（全文）
          3.8 文本+音频: response.audio_transcript.delta / response.audio_transcript.done
          3.5 纯文本   : response.text.text（text + stash 预测）/ response.text.done
          3.5 文本+音频: response.audio_transcript.text / response.audio_transcript.done
        """
        if self._gen == "38":
            if etype == "response.text.delta":
                return ev.get("delta", ""), "", None
            if etype == "response.text.done":
                return "", "", ev.get("text", "")
            if etype == "response.audio_transcript.delta":
                return ev.get("delta", ""), "", None
            if etype == "response.audio_transcript.done":
                return "", "", ev.get("transcript") or ev.get("text") or ""
        else:
            if etype == "response.text.text":
                return ev.get("text", ""), ev.get("stash", ""), None
            if etype == "response.text.done":
                return "", "", ev.get("text", "")
            if etype == "response.audio_transcript.text":
                return ev.get("text", ""), ev.get("stash", ""), None
            if etype == "response.audio_transcript.done":
                return "", "", ev.get("transcript") or ev.get("text") or ""
        return None

    def _emit(self, confirmed: str, pending: str, is_final: bool) -> None:
        if self.on_text is None:
            return
        if is_final:
            # 终版去重集中在这里：静默兜底 / response.done / response.text.done 三条路径
            # 可能对同一段文本都触发终结，重复下发会让下游（chatbox）白发一次。
            if confirmed == self._last_final_text:
                return
            self._last_final_text = confirmed
        else:
            self._last_text_at = time.perf_counter()   # 静默兜底计时
        # 首个增量延迟埋点（以 speech_started 为基准）
        if self._first_delta_ms is None and (confirmed or pending):
            if self._t_speech_start is not None:
                self._first_delta_ms = (time.perf_counter() - self._t_speech_start) * 1000
                self.first_delta_ms = self._first_delta_ms
        self.on_text(TextDelta(confirmed=confirmed, pending=pending, is_final=is_final,
                               source="".join(self._src_buf)))
