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

from .base import (AudioHandler, LiveTranslateSession, SessionConfig, TextDelta, TextHandler,
                   UsageHandler, should_finalize)

# 关闭握手的上限（秒）。**真链路实测（2026-09-30）：百炼服务端不回 close 帧** ——
# `ws.close()` 稳吃 10.01s（两次测量一致），也就是 websockets 的默认 close_timeout，
# 于是 `Engine._cleanup() → Session.close()` 让引擎线程在「停止翻译」后还活 10.3s，
# 而 `Engine.stop()` 又在调用方（Tk 主线程）上等这个线程 → 界面「未响应」。
# 对端正常回 close 帧时这个值不起作用（握手一完成就返回），只有对端装死才走兜底。
CLOSE_TIMEOUT_S = 1.5
# `ws.close()` 自身的硬上限（比 close_timeout 略宽一点，只用来兜底/留痕）。
CLOSE_WAIT_S = 2.0


def is_fatal_server_error(payload: dict) -> bool:
    """服务端 `error` 事件是否属于**已判定的致命类**（纯函数，离线可测）。

    用户实测日志（4 小时）：`{"code": "COMMON_ERROR", "message": "model repeat
    output happened"}` 出现后服务端**必定**紧跟着 1011 掐断连接 —— 中间那几秒
    干等没有意义，应主动重建。判定宁保守：只有这两类明确信号才算致命，
    其余 error（限流、参数等）维持「只打印、不断线」的既有行为。
    """
    if not isinstance(payload, dict):
        return False
    err = payload.get("error") or payload
    if not isinstance(err, dict):
        return False
    msg = str(err.get("message") or "").lower()
    code = str(err.get("code") or "").upper()
    if "model repeat output happened" in msg or "repeat output" in msg:
        return True
    return code == "COMMON_ERROR"


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
        # 黑匣子：最近的服务端事件类型（断线诊断用）
        self._evt_hist: deque[tuple[str, float]] = deque(maxlen=80)
        self._evt_counts: dict[str, int] = {}
        self._closing = False
        # 服务端判定致命错误的原因（如 model repeat output happened）。
        # 一旦设置，说明本会话已被我们主动放弃，看门狗会直接走重连路径。
        self.fatal_reason = ""
        self._buf: list[str] = []          # 本段已确认文本的增量累加
        self._src_buf: list[str] = []
        self._last_text_at: float = 0.0    # 最近一次文本增量时间（静默兜底用）
        self._last_audio_at: float = 0.0   # 最近一次**上送音频**时刻（快封句的「麦克风静音」依据）
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
            close_timeout=CLOSE_TIMEOUT_S,
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
        # 快封句的「麦克风静音」依据：注意引擎的 _SilenceGate 静音时**只暂停上送**，
        # 所以「距上次上送的间隔」就是用户真实的停顿长度。
        self._last_audio_at = time.perf_counter()
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

    def recent_events(self) -> list[tuple[str, float]]:
        """最近的服务端事件（类型, 相对现在的秒数）。

        黑匣子用：断线时把这些打出来，能看出服务端在掐断前最后在做什么
        （例如一直在重复发同一个 delta）。
        """
        now = time.monotonic()
        return [(name, now - ts) for name, ts in self._evt_hist]

    def event_counts(self) -> dict[str, int]:
        return dict(self._evt_counts)

    @property
    def fail_reason(self) -> str:
        """接收循环异常退出时的原因（用于日志与重连提示）。"""
        if self.fatal_reason:
            return self.fatal_reason
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
        """关会话：正常路径（对端回 close 帧）秒回；对端装死时有界 + **留痕**。

        为什么要有界：实测百炼服务端不回 close 帧 → `ws.close()` 走 websockets 的
        `close_timeout` 默认 10s，白等会让「停止翻译」多花 10 秒（界面线程同步等它 =
        窗口「未响应」，用户真机复现）。这里两道闸：建连时给 `close_timeout`，
        外层再套一层 `wait_for` —— 前者管协议层握手，后者保证任何实现都不会卡住收尾。
        """
        self._closing = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"event_id": "evt_finish", "type": "session.finish"}))
                await asyncio.sleep(0.3)
            except Exception:
                pass
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(self._ws.close(), timeout=CLOSE_WAIT_S)
            except asyncio.TimeoutError:
                # 降级路径必须留痕（本仓库禁静默降级）：否则「停止怎么要好几秒」无从查起。
                print(f"[session] 服务端 {CLOSE_WAIT_S:.1f}s 内没回 close 帧 → 放弃等待"
                      f"（已发 session.finish，不影响停止）", flush=True)
            except Exception:
                pass
            else:
                ms = (time.perf_counter() - t0) * 1000
                if ms >= CLOSE_TIMEOUT_S * 1000 * 0.9:
                    # 没到硬上限、但已贴到 close_timeout：握手不完整，同样要留痕。
                    print(f"[session] close 握手用了 {ms:.0f}ms"
                          f"（服务端未回 close 帧，已按 close_timeout={CLOSE_TIMEOUT_S}s 收尾）",
                          flush=True)
        if self._recv_task is not None:
            self._recv_task.cancel()

    def _abort_on_fatal(self, reason: str) -> None:
        """服务端判定致命错误 → 主动放弃本会话（不许静默：必须留痕）。

        只做三件幂等的事：记原因 → 置 _closing（is_alive=False）→ 关 ws。
        不发 session.finish（服务端已经坏了，发了也是白等超时）；
        重连交给引擎看门狗的既有路径（带退避 + RPM 预算，不会更凶）。
        """
        if not self.fatal_reason:
            self.fatal_reason = reason
            print(f"[session] ⚠️ 服务端判定致命错误 → 主动重建会话（不再干等 1011）：{reason}",
                  flush=True)
        self._closing = True
        if self._ws is None:
            return
        try:
            asyncio.get_running_loop().create_task(self._close_ws(self._ws))
        except RuntimeError:
            pass            # 不在事件循环里（离线单测直接调）：标志位已足够让看门狗接手

    async def _close_ws(self, ws: Any) -> None:
        try:
            await ws.close()
        except Exception:   # noqa: BLE001
            pass

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
                # 黑匣子：记下事件类型序列（断线时用来还原"服务端最后在做什么"）
                etype = str(ev.get("type", ""))
                self._evt_hist.append((etype, time.monotonic()))
                self._evt_counts[etype] = self._evt_counts.get(etype, 0) + 1
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
            if is_fatal_server_error(ev):
                # 实测：这类 error 之后服务端必跟 1011 掐线。与其干等几秒，
                # 不如立刻主动断连 —— is_alive=False → 看门狗 0.3s 内走既有重连路径。
                self._abort_on_fatal(
                    f"服务端致命错误：{json.dumps(ev.get('error') or ev, ensure_ascii=False)[:200]}")
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

        2026-10-01 实测补充（真链路 2 句）：**停止上送后的 8s 内服务端一条事件都不发**
        （既无 text.done 也无 response.done）→ 没有语义信号可用，只能靠定时器；
        而累计译文在「说完前 0.74~0.89s」就不再增长 → 之后再等 3s 全是白等。
        故加上**双条件快封句**：麦克风也静了（用户确实说完）时，文字静默 1.1s 就封，
        实测把终版从「说完后 +2.1s」提到 **+0.3s**（省 ~1.8s）。判据在
        `base.should_finalize()`（纯函数，离线可测）。

        注意三点（都是实测踩出来的）：
        1) 慢阈值必须大于服务端的增量间隔（实测最大 2.3s），否则会在句子中间抢跑；
        2) **不能一发就永久封死**——长句后续还会有增量，文本变了就应再次终结；
        3) 那 2.3s 的大间隔是**句子中间**的停顿 → 快路径必须有「麦克风已静」这一条，
           否则照样会抢跑。
        """
        if self._closing or not self._buf:
            return
        cur = "".join(self._buf)
        if cur == self._last_final_text:
            return
        if not self._last_text_at:
            return
        now = time.perf_counter()
        text_quiet = now - self._last_text_at
        mic_quiet = (now - self._last_audio_at) if self._last_audio_at else None
        if should_finalize(text_quiet_s=text_quiet, mic_quiet_s=mic_quiet,
                           silence_s=self.cfg.final_silence_s,
                           fast_silence_s=self.cfg.fast_final_silence_s,
                           fast_mic_quiet_s=self.cfg.fast_final_mic_quiet_s):
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
