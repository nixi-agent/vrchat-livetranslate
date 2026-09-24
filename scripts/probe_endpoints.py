#!/usr/bin/env python
"""probe_endpoints.py —— 握手矩阵探测：判断「Workspace access denied」是域名问题、路径问题还是模型问题。

只做 connect + 等 session.created，不做音频推送；每个组合打印握手结果与首个事件/错误原文。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_realtime import load_api_key  # noqa: E402

CANDIDATES = [
    ("legacy  /api-ws/v1/realtime  + qwen3.8-livetranslate", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime", "qwen3.8-livetranslate-flash-realtime"),
    ("legacy  /api-ws/v1/realtime  + qwen3.5-livetranslate", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime", "qwen3.5-livetranslate-flash-realtime"),
    ("legacy  /api-ws/v1/realtime  + 无 model 参数",          "wss://dashscope.aliyuncs.com/api-ws/v1/realtime", None),
    ("legacy  /api-ws/v1/inference + qwen3.8-livetranslate", "wss://dashscope.aliyuncs.com/api-ws/v1/inference", "qwen3.8-livetranslate-flash-realtime"),
    ("legacy  /api-ws/v1/inference + qwen3.8-omni-realtime", "wss://dashscope.aliyuncs.com/api-ws/v1/inference", "qwen3.8-omni-flash-realtime"),
    ("intl    dashscope-intl /v1/realtime + qwen3.8",        "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime", "qwen3.8-livetranslate-flash-realtime"),
]


async def probe_one(label: str, base: str, model: str | None, api_key: str) -> None:
    url = base if model is None else f"{base}?model={model}"
    print(f"\n── {label}\n   {url}")
    try:
        async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {api_key}"}, open_timeout=12) as ws:
            print("   握手: ✅ WebSocket 已建立")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                ev = json.loads(raw)
                print(f"   首事件: {ev.get('type')}")
                if ev.get("type") == "error":
                    print(f"   错误详情: {json.dumps(ev, ensure_ascii=False)[:300]}")
                else:
                    print(f"   详情: {json.dumps(ev, ensure_ascii=False)[:200]}")
            except asyncio.TimeoutError:
                print("   首事件: (8s 内无事件；连接建立但服务端沉默)")
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        reason = getattr(exc, "reason", None) or str(exc)
        print(f"   握手: ❌ {type(exc).__name__} code={code} reason={reason[:200]}")


async def main() -> None:
    api_key = load_api_key()
    print(f"API key: {api_key[:6]}***{api_key[-4:]}  (len={len(api_key)})")
    for label, base, model in CANDIDATES:
        await probe_one(label, base, model, api_key)
    print("\n注：所有组合若都是 'Workspace access denied'，说明该 key 为工作空间作用域，必须使用 "
          "wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com 域名。")


if __name__ == "__main__":
    asyncio.run(main())
