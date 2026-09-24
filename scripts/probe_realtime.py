#!/usr/bin/env python
"""
probe_realtime.py —— P0.5 探针：验证阿里百炼 Realtime 同传的「握手 + 事件流 + 首增量延迟」。

它不依赖头显/VRChat/物理控制台会话，只做四件事：
  1. 用旧域名（wss://dashscope.aliyuncs.com/api-ws/v1/realtime）建 WebSocket
  2. 发 session.update（qwen3.8 字段：output_modalities / translation.language / turn_detection）
  3. 把一段 16kHz 单声道 PCM 按实时节奏推进去
  4. 记录每个服务端事件的到达时间，算「首增量延迟」，并落一份 JSONL 事件日志

用法：
  python scripts/probe_realtime.py --pcm testdata/zh_test_16k.pcm --target-lang en
  python scripts/probe_realtime.py --pcm testdata/zh_test_16k.pcm --target-lang en --output-modalities text
  python scripts/probe_realtime.py --pcm testdata/zh_test_16k.pcm --target-lang ja --output-modalities text,audio

API key 读取顺序：环境变量 DASHSCOPE_API_KEY → ~/.bailian/config.json 的 api_key。**永不打印明文 key**。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import websockets

MODEL_DEFAULT = "qwen3.8-livetranslate-flash-realtime"
LEGACY_BASE = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
MAAS_BASE = "wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

# 我们关心的事件（用于摘要；其余事件也会被记录）
TEXT_DELTA_EVENTS = ("response.text.delta", "response.audio_transcript.delta", "response.text.text")
AUDIO_DELTA_EVENTS = ("response.audio.delta",)


def load_api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if key:
        return key.strip()
    cfg = Path(os.path.expanduser("~/.bailian/config.json"))
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        for k in ("api_key", "apiKey", "DASHSCOPE_API_KEY"):
            if data.get(k):
                return str(data[k]).strip()
        # 允许嵌套
        for v in data.values():
            if isinstance(v, dict):
                for k in ("api_key", "apiKey"):
                    if v.get(k):
                        return str(v[k]).strip()
    raise SystemExit("找不到 API key：设 DASHSCOPE_API_KEY，或用 bl auth login --api-key 写入 ~/.bailian/config.json")


def build_url(base: str, model: str, workspace_id: str | None) -> str:
    if "{workspace_id}" in base:
        if not workspace_id:
            raise SystemExit("该域名需要 --workspace-id（百炼控制台「业务空间详情」）")
        base = base.replace("{workspace_id}", workspace_id)
    return f"{base}?model={model}"


def read_pcm(path: Path) -> bytes:
    if not path.exists():
        raise SystemExit(f"PCM 文件不存在：{path}")
    return path.read_bytes()


class Probe:
    def __init__(self, args: argparse.Namespace, api_key: str) -> None:
        self.args = args
        self.api_key = api_key
        self.t0 = time.perf_counter()
        self.events: list[dict] = []
        self.first_text_delta_at: float | None = None
        self.first_audio_delta_at: float | None = None
        self.done_at: float | None = None
        self.session_updated = asyncio.Event()
        self.text_parts: list[str] = []
        self.audio_bytes = 0
        self.audio_pcm = bytearray()

    def mark(self) -> float:
        return (time.perf_counter() - self.t0) * 1000

    def record(self, ev: dict) -> None:
        t = self.mark()
        self.events.append({"t_ms": round(t, 1), "type": ev.get("type"), "raw": ev})

    # ---------- 主流程 ----------
    async def run(self) -> int:
        url = build_url(self.args.base, self.args.model, self.args.workspace_id)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        pcm = read_pcm(Path(self.args.pcm))
        chunk_bytes = int(self.args.chunk_ms / 1000 * 16000 * 2)  # 16k * s16 * ms

        print(f"[probe] url           = {url}")
        print(f"[probe] model         = {self.args.model}")
        print(f"[probe] pcm           = {self.args.pcm}  ({len(pcm)} bytes = {len(pcm)/2/16000:.2f}s)")
        print(f"[probe] target_lang   = {self.args.target_lang}   source_lang = {self.args.source_lang}")
        print(f"[probe] output_modes  = {self.args.output_modalities}   turn_detection = {self.args.turn_detection}")
        print(f"[probe] chunk         = {self.args.chunk_ms}ms ({chunk_bytes} bytes)\n")

        try:
            async with websockets.connect(url, additional_headers=headers, open_timeout=15) as ws:
                print(f"[probe] WS 已连接  t=+{self.mark():.0f}ms")

                # 1) 收 session.created，然后发 session.update
                created = await self._wait_for(ws, "session.created", timeout=10)
                if created is None:
                    print("[probe] ⚠️ 未见 session.created（可能事件名不同），仍继续发 session.update")

                await ws.send(json.dumps({"event_id": "evt_update_1", "type": "session.update", "session": self._session_payload()}))
                print(f"[probe] session.update 已发送  t=+{self.mark():.0f}ms")

                ok = await self._wait_for(ws, "session.updated", timeout=10)
                print(f"[probe] session.updated {'✅ 收到' if ok else '❌ 未收到'}  t=+{self.mark():.0f}ms\n")
                if ok is None:
                    print("[probe] 继续跑（部分实现可能只在 error 时才发声）")

                # 2) 后台收事件
                recv_task = asyncio.create_task(self._recv_loop(ws, deadline_after_stream=self.args.settle_s))

                # 3) 按实时节奏推音频
                audio_start = self.mark()
                sent = 0
                for i in range(0, len(pcm), chunk_bytes):
                    chunk = pcm[i : i + chunk_bytes]
                    await ws.send(json.dumps({
                        "event_id": f"evt_audio_{i}",
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode(),
                    }))
                    sent += len(chunk)
                    if self.args.realtime:
                        await asyncio.sleep(self.args.chunk_ms / 1000)
                audio_end = self.mark()
                print(f"[probe] 音频推送完毕  {sent} bytes  用时 {audio_end - audio_start:.0f}ms（含节流）"
                      f"{'' if self.args.realtime else '  [非实时灌入]'}")

                # 4) 等响应收尾
                try:
                    await asyncio.wait_for(recv_task, timeout=self.args.settle_s + 5)
                except asyncio.TimeoutError:
                    recv_task.cancel()

                # 5) 优雅结束
                try:
                    await ws.send(json.dumps({"event_id": "evt_finish_1", "type": "session.finish"}))
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

        except Exception as exc:  # noqa: BLE001
            print(f"[probe] ❌ 连接阶段失败：{type(exc).__name__}: {exc}")
            self._dump_jsonl()   # 失败也留痕：否则拿不到 session.created/updated 的真实载荷
            return 2

        self._report(len(pcm))
        self._dump_jsonl()
        self._save_audio()
        return 0

    def _save_audio(self) -> None:
        if not self.args.save_audio:
            return
        if not self.audio_pcm:
            print("  ⚠️ 没有音频可保存（output_modalities 未含 audio？）")
            return
        import wave

        out = Path(self.args.save_audio)
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)   # 模型输出为 24kHz PCM
            w.writeframes(bytes(self.audio_pcm))
        print(f"  译音已保存: {out}  ({len(self.audio_pcm)} bytes = {len(self.audio_pcm)/2/24000:.2f}s @24kHz)")

    def _session_payload(self) -> dict:
        modes = [m.strip() for m in self.args.output_modalities.split(",") if m.strip()]
        # 实测结论：该模型要求每次 session.update 必须带 translation，否则报
        # "Invalid translation parameter."（服务端不做字段合并，是整体校验）。
        session: dict = {
            "output_modalities": modes,
            "translation": {"language": self.args.target_lang},
        }
        if self.args.turn_detection and self.args.turn_detection != "none":
            session["audio"] = {"input": {"turn_detection": {"type": self.args.turn_detection}}}
        if self.args.source_lang:
            session["input_audio_transcription"] = {"language": self.args.source_lang}
        if self.args.voice:
            session["voice"] = self.args.voice
        return session

    async def _wait_for(self, ws, event_type: str, timeout: float) -> dict | None:
        """读到指定事件（其余事件一并记录），超时返回 None。"""
        end = time.perf_counter() + timeout
        while True:
            remain = end - time.perf_counter()
            if remain <= 0:
                return None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remain)
            except asyncio.TimeoutError:
                return None
            ev = json.loads(raw)
            self.record(ev)
            if ev.get("type") == event_type:
                return ev

    async def _recv_loop(self, ws, deadline_after_stream: float) -> None:
        """持续收事件；音频推完后静默超过 deadline 秒就自行结束。"""
        stream_done_at: float | None = None
        while True:
            timeout = 1.0
            if stream_done_at is None and self.done_at is not None:
                stream_done_at = time.perf_counter()
            if stream_done_at is not None and (time.perf_counter() - stream_done_at) > deadline_after_stream:
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            except Exception:
                return
            ev = json.loads(raw)
            self.record(ev)
            t = ev.get("type", "")
            if t in TEXT_DELTA_EVENTS:
                if self.first_text_delta_at is None:
                    self.first_text_delta_at = self.mark()
                frag = ev.get("delta") or ev.get("text") or ev.get("transcript") or ""
                if frag:
                    self.text_parts.append(str(frag))
            elif t in AUDIO_DELTA_EVENTS:
                if self.first_audio_delta_at is None:
                    self.first_audio_delta_at = self.mark()
                chunk = base64.b64decode(ev.get("delta") or "")
                self.audio_bytes += len(chunk)
                self.audio_pcm.extend(chunk)
            elif t == "response.done":
                self.done_at = self.mark()
                return

    def _report(self, pcm_len: int) -> None:
        from collections import Counter

        hist = Counter(e["type"] for e in self.events)
        print("\n" + "=" * 68)
        print("事件直方图（按类型）")
        print("=" * 68)
        for name, n in hist.most_common():
            print(f"  {n:5d}  {name}")

        print("\n" + "=" * 68)
        print("延迟与产出")
        print("=" * 68)
        audio_seconds = pcm_len / 2 / 16000
        print(f"  输入音频时长            : {audio_seconds:.2f}s")
        if self.first_text_delta_at is not None:
            print(f"  首个文本增量到达        : +{self.first_text_delta_at:.0f}ms  "
                  f"（相当于音频开始后 {self.first_text_delta_at/1000:.2f}s）")
        else:
            print("  首个文本增量            : ❌ 未收到任何文本增量")
        if self.first_audio_delta_at is not None:
            print(f"  首个音频增量到达        : +{self.first_audio_delta_at:.0f}ms  "
                  f"（累计 {self.audio_bytes} bytes / 约 {self.audio_bytes/2/24000:.2f}s@24k）")
        if self.done_at is not None:
            print(f"  response.done           : +{self.done_at:.0f}ms")
        text = "".join(self.text_parts).strip()
        print(f"\n  译文（按事件累加）      : {text[:400] if text else '(空)'}")

        errs = [e for e in self.events if e["type"] == "error"]
        if errs:
            print("\n  ⚠️ 错误事件：")
            for e in errs[:3]:
                print(f"    {json.dumps(e['raw'], ensure_ascii=False)[:300]}")

    def _dump_jsonl(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"\n  事件日志: {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcm", required=True, help="16kHz 单声道 s16le PCM 输入")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--base", default=LEGACY_BASE, help="legacy 默认；也可传 maas 模板（需 --workspace-id）")
    ap.add_argument("--workspace-id", default=None)
    ap.add_argument("--target-lang", default="en")
    ap.add_argument("--source-lang", default=None, help="不填 = 自动识别")
    ap.add_argument("--output-modalities", default="text", help="text | text,audio")
    ap.add_argument("--turn-detection", default="none",
                    help="none（不传，用服务端默认）| server_vad | speaker_detection")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--chunk-ms", type=int, default=100)
    ap.add_argument("--realtime", action="store_true", default=True, help="按实时节奏推送（默认开）")
    ap.add_argument("--no-realtime", dest="realtime", action="store_false")
    ap.add_argument("--settle-s", type=float, default=8.0, help="最后一段音频后等待响应的秒数")
    ap.add_argument("--save-audio", default=None, help="把 response.audio.delta 拼成 WAV 存到此路径（24kHz 单声道）")
    args = ap.parse_args()
    return asyncio.run(Probe(args, load_api_key()).run())


if __name__ == "__main__":
    sys.exit(main())
