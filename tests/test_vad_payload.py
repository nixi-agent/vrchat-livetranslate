"""服务端断句（turn_detection）下发口径验收。

## 真实问题（2026-10-21 实测）

用户报「说完话要过好一会儿才出字」。真机日志 + 直接打 API 的量化结果：

| 客户端下发 | 说完 → 最终译文 |
|---|---|
| 什么都不发（v0.12.0 的现状，服务端默认 speaker_detection） | 4.0~4.2s |
| `speaker_detection + silence_duration_ms=1000` | 2.5s |
| `server_vad`（默认 800ms） | 0.96s |
| `server_vad + silence_duration_ms=500` | **0.53s** |
| `server_vad + silence_duration_ms=300` | 0.39s，但会吞句首（10 句里 4 句丢开头） |

断句之后模型自己只要 0.15~0.2s ⇒ 慢在「断句」，不在模型/网络。

## 这里钉住什么

1. 默认配置（`turn_detection=None`）**必须**下发 `server_vad + 500ms` ——
   这正是「不发 → 4s」的回归口子，缺了它用例必红；
2. 本测试分支固定 server_vad：老配置里的 `speaker_detection` 不能把快档吃掉；
3. `silence_duration_ms` 可配、可关（None → 不下发该字段，退回服务端默认）；
4. 3.8 放在 `audio.input.turn_detection`、3.5 放在顶层 `turn_detection`（两代字段不同）；
5. 下发内容仍必须带 `translation` / `voice`（缺任一会被服务端整体拒绝）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.session.base import SessionConfig, DEFAULT_VAD_SILENCE_MS  # noqa: E402
from vlt.session.qwen38 import QwenLiveTranslateSession  # noqa: E402


def _td_of(payload: dict) -> dict:
    return ((payload.get("audio") or {}).get("input") or {}).get("turn_detection") or {}


def test_default_payload_uses_server_vad_500() -> None:
    """不填 turn_detection 时必须走 server_vad + 500ms（老版本这里是「什么都不发」）。"""
    s = QwenLiveTranslateSession(SessionConfig(api_key="x"))
    payload = s._session_payload()
    td = _td_of(payload)
    assert td.get("type") == "server_vad", f"默认必须 server_vad，实际 {td!r}"
    assert td.get("silence_duration_ms") == DEFAULT_VAD_SILENCE_MS == 500, td
    # 必要字段仍在（服务端会整体校验）
    assert payload.get("translation") == {"language": "en"}, payload
    assert payload.get("voice") == "Tina", payload
    print("  默认 → server_vad + 500ms OK")


def test_legacy_speaker_detection_does_not_win() -> None:
    """老 config.yaml 里的 speaker_detection（旧默认/旧诊断包）不能吃掉快档。"""
    s = QwenLiveTranslateSession(SessionConfig(api_key="x", turn_detection="speaker_detection"))
    td = _td_of(s._session_payload())
    assert td.get("type") == "server_vad", f"应固定 server_vad，实际 {td!r}"
    print("  配置 speaker_detection → 仍按 server_vad OK")


def test_silence_duration_is_configurable() -> None:
    s = QwenLiveTranslateSession(SessionConfig(api_key="x", silence_duration_ms=1000))
    td = _td_of(s._session_payload())
    assert td.get("silence_duration_ms") == 1000, td
    print("  silence_duration_ms 可配 OK")


def test_silence_duration_none_omits_field() -> None:
    """None = 不下发该字段，交给服务端默认（回退口子要能表达）。"""
    s = QwenLiveTranslateSession(SessionConfig(api_key="x", silence_duration_ms=None))
    td = _td_of(s._session_payload())
    assert td.get("type") == "server_vad", td
    assert "silence_duration_ms" not in td, td
    print("  silence_duration_ms=None → 不下发 OK")


def test_gen35_uses_top_level_turn_detection() -> None:
    """3.5 的字段位置在顶层（不是 audio.input），别把两代写混。"""
    s = QwenLiveTranslateSession(SessionConfig(
        api_key="x", model="qwen3.5-livetranslate-flash-realtime"))
    payload = s._session_payload()
    assert "audio" not in payload, payload
    td = payload.get("turn_detection") or {}
    assert td.get("type") == "server_vad" and td.get("silence_duration_ms") == 500, payload
    print("  3.5 → 顶层 turn_detection OK")


def test_config_clamps_out_of_range() -> None:
    """手写配置越界 → 夹到 [200, 6000] 并留痕（不许带病下发）。"""
    from vlt.config import _int_clamped
    from vlt.session.base import VAD_SILENCE_MS_MIN, VAD_SILENCE_MS_MAX

    lo = _int_clamped(10, DEFAULT_VAD_SILENCE_MS, key="t", lo=VAD_SILENCE_MS_MIN, hi=VAD_SILENCE_MS_MAX)
    hi = _int_clamped(99999, DEFAULT_VAD_SILENCE_MS, key="t", lo=VAD_SILENCE_MS_MIN, hi=VAD_SILENCE_MS_MAX)
    bad = _int_clamped("abc", DEFAULT_VAD_SILENCE_MS, key="t", lo=VAD_SILENCE_MS_MIN, hi=VAD_SILENCE_MS_MAX)
    assert lo == VAD_SILENCE_MS_MIN and hi == VAD_SILENCE_MS_MAX and bad == DEFAULT_VAD_SILENCE_MS
    print("  越界/非法值夹取 OK")


if __name__ == "__main__":
    print("test_vad_payload:")
    test_default_payload_uses_server_vad_500()
    test_legacy_speaker_detection_does_not_win()
    test_silence_duration_is_configurable()
    test_silence_duration_none_omits_field()
    test_gen35_uses_top_level_turn_detection()
    test_config_clamps_out_of_range()
    print("ALL PASSED")
