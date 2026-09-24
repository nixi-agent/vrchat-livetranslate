"""配置加载：YAML + 环境变量。API key 不落配置文件，只从环境变量或 bl CLI 的配置读。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .session.base import SessionConfig

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def load_api_key(explicit: str | None = None) -> str:
    """API key 读取顺序：显式参数 → 环境变量 DASHSCOPE_API_KEY → ~/.bailian/config.json（bl CLI）。"""
    if explicit:
        return explicit.strip()
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env:
        return env.strip()
    cfg = Path(os.path.expanduser("~/.bailian/config.json"))
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        for key in ("api_key", "apiKey", "DASHSCOPE_API_KEY"):
            if data.get(key):
                return str(data[key]).strip()
    raise SystemExit("找不到 API key：设 DASHSCOPE_API_KEY，或先跑 `bl auth login --api-key <key>`")


@dataclass
class Direction:
    source_lang: str | None = None
    target_lang: str = "en"
    output_audio: bool = False
    hotwords: dict[str, str] = field(default_factory=dict)
    voice: str | None = None

    def to_session_config(self, base: dict[str, Any]) -> SessionConfig:
        return SessionConfig(
            model=base["model"],
            target_lang=self.target_lang,
            source_lang=self.source_lang,
            output_audio=self.output_audio,
            voice=self.voice or base.get("voice") or "Tina",
            hotwords=self.hotwords,
            turn_detection=base.get("turn_detection"),
            base_url=base["base_url"],
            workspace_id=base.get("workspace_id") or "",
            api_key=base["api_key"],
            reconnect_backoff=tuple(base.get("reconnect_backoff", (2, 5, 10, 30))),
            max_new_sessions_per_minute=int(base.get("max_new_sessions_per_minute", 4)),
            final_silence_s=float(base.get("final_silence_s", 1.2)),
        )


@dataclass
class AppConfig:
    session_base: dict[str, Any]
    directions: dict[str, Direction]
    chatbox: dict[str, Any]
    merger: dict[str, Any]
    overlay: dict[str, Any] = field(default_factory=dict)

    def direction(self, name: str) -> Direction:
        if name not in self.directions:
            raise SystemExit(f"配置里没有方向的 key：{name}（现有：{list(self.directions)}）")
        return self.directions[name]


def load_config(path: str | Path | None = None, api_key: str | None = None) -> AppConfig:
    p = Path(path) if path else DEFAULT_CONFIG
    raw: dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    s = raw.get("session", {}) or {}
    session_base = {
        "model": s.get("model", "qwen3.8-livetranslate-flash-realtime"),
        "base_url": s.get("base_url", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"),
        "voice": s.get("voice", "Tina"),
        "turn_detection": s.get("turn_detection"),
        "workspace_id": s.get("workspace_id") or "",
        "reconnect_backoff": s.get("reconnect_backoff", [2, 5, 10, 30]),
        "max_new_sessions_per_minute": s.get("max_new_sessions_per_minute", 4),
        "final_silence_s": float(s.get("final_silence_s", 1.2)),
        "api_key": load_api_key(api_key),
    }
    directions = {}
    for name, d in (raw.get("directions") or {}).items():
        directions[name] = Direction(
            source_lang=(d or {}).get("source_lang"),
            target_lang=(d or {}).get("target_lang", "en"),
            output_audio=bool((d or {}).get("output_audio", False)),
            hotwords=(d or {}).get("hotwords") or {},
            voice=(d or {}).get("voice"),
        )
    if not directions:
        directions = {"mine": Direction(source_lang="zh", target_lang="en")}
    return AppConfig(
        session_base=session_base,
        directions=directions,
        chatbox=raw.get("chatbox") or {},
        merger=raw.get("merger") or {},
        overlay=raw.get("overlay") or {},
    )
