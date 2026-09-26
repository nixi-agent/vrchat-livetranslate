"""配置加载：YAML + 环境变量。API key 不落配置文件，只从环境变量或 bl CLI 的配置读。"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .session.base import SessionConfig, DEFAULT_FINAL_SILENCE_S

from .paths import APP_DIR, BUNDLE_DIR

ROOT = APP_DIR                                    # 保留别名，兼容既有引用
DEFAULT_CONFIG = APP_DIR / "config.yaml"           # 可写：用户配置
# 入库的是模板；config.yaml 是用户自己的配置（设备名/语言偏好），已被 gitignore。
EXAMPLE_CONFIG = BUNDLE_DIR / "config.example.yaml"  # 只读：随程序分发的模板


def ensure_config(path: Path | None = None) -> Path:
    """确保配置文件存在：不存在就从 config.example.yaml 复制一份。

    这样新克隆的仓库（以及给朋友用的时候）开箱即用，而用户的实际配置
    不会进版本库——设备名这类东西因机器而异，跟着仓库走只会互相污染。
    """
    p = Path(path) if path else DEFAULT_CONFIG
    if p.exists() or not EXAMPLE_CONFIG.exists():
        return p
    try:
        p.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[config] 已从 {EXAMPLE_CONFIG.name} 生成 {p.name}")
    except OSError as exc:
        print(f"[config] 生成 {p.name} 失败（将使用内置默认值）：{exc}")
    return p


def load_api_key(explicit: str | None = None) -> str:
    """API key 读取顺序：显式参数 → 界面保存的 → 环境变量 → ~/.bailian/config.json（bl CLI）。

    界面保存的排第二（仅次于显式传参）：用户在界面上填了 key，就是最明确的意图，
    不该被环境变量或 CLI 配置盖掉。来源会打一行日志（**只打码、绝不打明文**），
    否则"为什么连的还是旧 key"根本查不出来。
    """
    if explicit:
        return explicit.strip()
    try:
        from .credentials import load_saved_key, mask_key

        saved = load_saved_key()
        if saved:
            print(f"[config] API key 来源：界面保存（{mask_key(saved)}）", flush=True)
            return saved
    except Exception as exc:  # noqa: BLE001
        print(f"[config] ⚠️ 读取界面保存的 key 失败，继续走其它来源："
              f"{type(exc).__name__}: {exc}", flush=True)
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
            final_silence_s=float(base.get("final_silence_s", DEFAULT_FINAL_SILENCE_S)),  # 默认值必须 > 服务端增量间隔（实测最大 2.3s），改小会让最终版在句子中间抢跑
        )


@dataclass
class AppConfig:
    session_base: dict[str, Any]
    directions: dict[str, Direction]
    chatbox: dict[str, Any]
    merger: dict[str, Any]
    overlay: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)      # 界面上次的选择（方向/输出勾选），启动时恢复
    text_input: dict[str, Any] = field(default_factory=dict)   # 打字输入（替代说话）

    def direction(self, name: str) -> Direction:
        if name not in self.directions:
            raise SystemExit(f"配置里没有方向的 key：{name}（现有：{list(self.directions)}）")
        return self.directions[name]


def _resolve_api_key(api_key: str | None, require_key: bool) -> str:
    """取 key；`require_key=False` 时"还没有 key"不抛错，而是返回空串。

    界面用得上：启动时**不能**因为没填 key 就起不来 —— 那样用户连"去哪填 key"
    的入口都看不到（首次使用、或换台电脑给朋友用，就是死局）。
    调用方（`_start`）会检查空 key 并给出明确指引。
    """
    try:
        return load_api_key(api_key)
    except SystemExit:
        if require_key:
            raise
        print("[config] ⚠️ 尚未配置 API key —— 界面照常启动；"
              "开始翻译前请点主界面右上角的 API key 入口填一个", file=sys.stderr, flush=True)
        return ""


def load_config(path: str | Path | None = None, api_key: str | None = None,
                require_key: bool = True) -> AppConfig:
    p = ensure_config(Path(path) if path else None)   # 缺文件时从 config.example.yaml 生成
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            # 配置文件被写坏了（实测：残留的孤立序列项会让整个文件非法，
            # 比如 `pos: [...]` 下面还留着旧版的 `- 0.0`）→ 备份 + 从模板重建，
            # 否则程序下次连启动都起不来。**绝不静默**：打印清楚，并保留坏文件供排查。
            broken = p.with_name(p.name + ".broken")
            try:
                shutil.copyfile(p, broken)
            except OSError as exc2:  # noqa: BLE001
                broken = None
                print(f"[config] 备份损坏的配置也失败了：{exc2}", file=sys.stderr)
            print(f"[config] ❌ 配置文件解析失败：{exc}\n"
                  f"[config]    （这是「配置被写坏」的表现，不是你的错）\n"
                  f"[config]    已备份到 {broken}，并从 {EXAMPLE_CONFIG.name} 重新生成默认配置 ——"
                  f"设备/语言等选择需要重设一次",
                  file=sys.stderr)
            if EXAMPLE_CONFIG.exists():
                shutil.copyfile(EXAMPLE_CONFIG, p)
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
        "final_silence_s": float(s.get("final_silence_s", DEFAULT_FINAL_SILENCE_S)),  # 默认值必须 > 服务端增量间隔（实测最大 2.3s），改小会让最终版在句子中间抢跑
        # 长静音闸门 + 本地 repeat 抑制：原样透传，取值校验在 engine 里做
        # （非法值会**留痕**并回落默认值，见 engine.silence_gate_settings / repeat_guard_settings）
        "silence_gate_enabled": s.get("silence_gate_enabled", True),
        "silence_gate_after_s": s.get("silence_gate_after_s", 30),
        "silence_gate_preroll_s": s.get("silence_gate_preroll_s", 1.0),
        "repeat_guard_enabled": s.get("repeat_guard_enabled", True),
        "repeat_guard_hits": s.get("repeat_guard_hits", 3),
        "repeat_guard_ratio": s.get("repeat_guard_ratio", 0.9),
        "api_key": _resolve_api_key(api_key, require_key),
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
    raw_output = raw.get("output") or {}
    raw_audio = raw_output.get("audio") or {}
    raw_capture = raw.get("capture") or {}
    raw_textin = raw.get("text_input") or {}
    output = {
        "audio": {
            "enabled": bool(raw_audio.get("enabled", False)),
            "device": raw_audio.get("device") or ["voicemeeter input", "voicemeeter aux input", "cable input", "vb-audio"],
            "device_name": str(raw_audio.get("device_name") or ""),
            "sample_rate": int(raw_audio.get("sample_rate", 48000)),
            "buffer_ms": int(raw_audio.get("buffer_ms", 300)),
            "max_buffer_ms": int(raw_audio.get("max_buffer_ms", 2000)),
        },
        "capture": {
            "mic_device": str(raw_capture.get("mic_device") or ""),
            "loopback_device": str(raw_capture.get("loopback_device") or ""),
        },
    }
    return AppConfig(
        session_base=session_base,
        directions=directions,
        chatbox=raw.get("chatbox") or {},
        merger=raw.get("merger") or {},
        overlay=raw.get("overlay") or {},
        output=output,
        ui=raw.get("ui") or {},
        text_input={
            "enabled": bool(raw_textin.get("enabled", True)),
            # 默认 qwen-mt-flash：实测 qwen3-livetranslate-flash 的**文本**接口会原样回吐
            # （中文进中文出，换个句子又正常），不能依赖；mt 系列稳定且能自动识别源语言。
            "model": str(raw_textin.get("model") or "qwen-mt-flash"),
            "timeout_s": float(raw_textin.get("timeout_s", 20.0)),
            # 打字也要出声：文本翻译不回音频，这一步单独用 TTS 合成后喂虚拟声卡
            "tts": {
                "enabled": bool((raw_textin.get("tts") or {}).get("enabled", True)),
                "model": str((raw_textin.get("tts") or {}).get("model") or "qwen3-tts-flash"),
                "voice": str((raw_textin.get("tts") or {}).get("voice") or "Cherry"),
                "timeout_s": float((raw_textin.get("tts") or {}).get("timeout_s", 30.0)),
            },
        },
    )
