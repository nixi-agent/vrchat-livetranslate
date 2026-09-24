#!/usr/bin/env python
"""probe_session_update2.py —— 组合载荷探测：找出最初触发 1007 'Workspace access denied' 的确切字段组合。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_realtime import load_api_key  # noqa: E402

URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-livetranslate-flash-realtime"

T = {"language": "en"}

CASES: list[tuple[str, dict]] = [
    ("A  translation + output_modalities=text",
     {"output_modalities": ["text"], "translation": T}),
    ("B  A + audio.turn_detection=server_vad",
     {"output_modalities": ["text"], "translation": T,
      "audio": {"input": {"turn_detection": {"type": "server_vad"}}}}),
    ("C  A + input_audio_transcription.language=zh",
     {"output_modalities": ["text"], "translation": T,
      "input_audio_transcription": {"language": "zh"}}),
    ("D  A + input_audio_transcription{model,language}",
     {"output_modalities": ["text"], "translation": T,
      "input_audio_transcription": {"model": "qwen3-asr-flash-realtime", "language": "zh"}}),
    ("E  原始失败组合（A + turn_detection + transcription.language）",
     {"output_modalities": ["text"], "translation": T,
      "audio": {"input": {"turn_detection": {"type": "server_vad"}}},
      "input_audio_transcription": {"language": "zh"}}),
    ("F  translation + audio.turn_detection=speaker_detection",
     {"translation": T, "audio": {"input": {"turn_detection": {"type": "speaker_detection"}}}}),
    ("G  translation + modalities（3.5 字段名）",
     {"modalities": ["text"], "translation": T}),
]


async def run_case(label: str, session_payload: dict, api_key: str) -> str:
    try:
        async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {api_key}"}, open_timeout=12) as ws:
            await asyncio.wait_for(ws.recv(), timeout=8)  # session.created
            await ws.send(json.dumps({"event_id": "evt_u", "type": "session.update", "session": session_payload}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                ev = json.loads(raw)
                t = ev.get("type")
                if t == "session.updated":
                    s = ev.get("session", {})
                    return (f"✅ session.updated  模态={s.get('modalities')} "
                            f"output_modalities={s.get('output_modalities')} "
                            f"translation={s.get('translation')}")
                if t == "error":
                    return f"❌ error: {json.dumps(ev.get('error') or ev, ensure_ascii=False)[:200]}"
    except asyncio.TimeoutError:
        return "⏱ 超时"
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        reason = getattr(exc, "reason", None) or str(exc)
        return f"❌ 连接被关 {type(exc).__name__} code={code}: {reason[:160]}"


async def main() -> None:
    api_key = load_api_key()
    print(f"URL: {URL}\n")
    for label, payload in CASES:
        res = await run_case(label, payload, api_key)
        print(f"{label:52s} → {res}")


if __name__ == "__main__":
    asyncio.run(main())
