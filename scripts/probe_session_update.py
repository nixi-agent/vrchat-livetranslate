#!/usr/bin/env python
"""probe_session_update.py —— 逐字段二分定位 session.update 的「Workspace access denied」触发器。

每个候选载荷在新连接里独立测试：connect → 等 session.created → 发 session.update → 判定结果。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_realtime import load_api_key  # noqa: E402

URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-livetranslate-flash-realtime"

CASES: list[tuple[str, dict]] = [
    ("① 空 session 对象", {"session": {}}),
    ("② 只 output_modalities=['text']", {"session": {"output_modalities": ["text"]}}),
    ("③ 只 output_modalities=['text','audio']", {"session": {"output_modalities": ["text", "audio"]}}),
    ("④ 只 translation.language=en", {"session": {"translation": {"language": "en"}}}),
    ("⑤ 只 audio.input.turn_detection=server_vad",
     {"session": {"audio": {"input": {"turn_detection": {"type": "server_vad"}}}}}),
    ("⑥ 只 audio.input.turn_detection=speaker_detection",
     {"session": {"audio": {"input": {"turn_detection": {"type": "speaker_detection"}}}}}),
    ("⑦ 只 input_audio_transcription.language=zh", {"session": {"input_audio_transcription": {"language": "zh"}}}),
    ("⑧ 只 voice=Tina", {"session": {"voice": "Tina"}}),
    ("⑨ 只 modalities=['text']（3.5 字段）", {"session": {"modalities": ["text"]}}),
    ("⑩ 最小组合：output_modalities+translation",
     {"session": {"output_modalities": ["text"], "translation": {"language": "en"}}}),
]


async def run_case(label: str, session_payload: dict, api_key: str) -> str:
    try:
        async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {api_key}"}, open_timeout=12) as ws:
            await asyncio.wait_for(ws.recv(), timeout=8)  # session.created
            await ws.send(json.dumps({"event_id": "evt_u", "type": "session.update", "session": session_payload["session"]}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                ev = json.loads(raw)
                t = ev.get("type")
                if t == "session.updated":
                    mods = ev.get("session", {}).get("modalities") or ev.get("session", {}).get("output_modalities")
                    return f"✅ session.updated  (响应模态={mods})"
                if t == "error":
                    return f"❌ error: {json.dumps(ev.get('error') or ev, ensure_ascii=False)[:220]}"
    except asyncio.TimeoutError:
        return "⏱ 超时（8s 内无 session.updated / error）"
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        reason = getattr(exc, "reason", None) or str(exc)
        return f"❌ 连接异常 {type(exc).__name__} code={code}: {reason[:180]}"


async def main() -> None:
    api_key = load_api_key()
    print(f"URL: {URL}\n")
    results = []
    for label, payload in CASES:
        res = await run_case(label, payload, api_key)
        results.append((label, res))
        print(f"{label:44s} → {res}")

    print("\n" + "=" * 70)
    ok = [l for l, r in results if r.startswith("✅")]
    bad = [(l, r) for l, r in results if not r.startswith("✅")]
    print(f"成功 {len(ok)} / 失败 {len(bad)}")
    for l, r in bad:
        print(f"  失败: {l} → {r[:120]}")


if __name__ == "__main__":
    asyncio.run(main())
