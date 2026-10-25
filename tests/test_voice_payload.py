"""说话译音的音色下发口径（`session.update` 的 voice 字段位置）。

## 真实问题（2026-10-25 直连两条线路实测）

用户报「换哪个音色听起来都一样」。把 `session.update` 打成探针（顶层 voice 填一个
**不存在的**音色，看服务端报不报错）：

| 线路 / 模型 | 顶层 `voice` | `audio.output.voice` |
|---|---|---|
| 千问云 maas（`maas.qianwenaiapi.com`），3.8 | 静默接受、照常出音，**音色仍是默认 Tina** | 非法 id → 1007 `Voice ... is not supported.`；`Ethan` → F0 149Hz（Tina 276Hz） |
| 老 dashscope 域名，3.8 | 生效 | 也认（并镜像回顶层） |
| 千问云 maas，3.5 | 生效（非法 id → 1007） | —（3.5 是旧字段形态，不走 audio.output） |

即：**3.8 在默认线路上只认 `audio.output.voice`**，只写顶层 `voice` 等于没写。
修复口径 = 两处都写（两条线路的公共安全交集），3.5 保持只写顶层。

## 这里钉住什么

1. 3.8 的 payload 必须带 `audio.output.voice`，且等于 `SessionConfig.voice`；
2. 顶层 `voice` 仍保留（老线路 / 3.5 的兼容面，缺了会被服务端整体拒绝）；
3. 3.5 的 payload 不许出现 `audio.output`（字段形态不同，别顺手写进去）；
4. 改 `cfg.voice` → payload 跟着变（防「写死 Tina」这类回归）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.session.base import SessionConfig  # noqa: E402
from vlt.session.qwen38 import QwenLiveTranslateSession  # noqa: E402

MODEL_38 = "qwen3.8-livetranslate-flash-realtime"
MODEL_35 = "qwen3.5-livetranslate-flash-realtime"


def _payload(model: str, voice: str) -> dict:
    return QwenLiveTranslateSession(SessionConfig(api_key="x", model=model, voice=voice))._session_payload()


def test_38_voice_goes_to_audio_output() -> None:
    """3.8：音色必须落在 `audio.output.voice`（默认线路唯一生效位置）。"""
    for voice in ("Tina", "Ethan", "longanfengyue_v3.1"):
        p = _payload(MODEL_38, voice)
        out = ((p.get("audio") or {}).get("output") or {}).get("voice")
        assert out == voice, f"3.8 的 audio.output.voice 必须是 {voice!r}，实际 {out!r}（payload={p!r}）"
        print(f"  ✓ 3.8 voice={voice} → audio.output.voice")


def test_38_keeps_top_level_voice_for_legacy_line() -> None:
    """3.8：顶层 voice 也留着 —— 老 dashscope 域名靠它，缺了会被服务端整体拒绝。"""
    p = _payload(MODEL_38, "Ethan")
    assert p.get("voice") == "Ethan", p
    print("  ✓ 3.8 同时保留顶层 voice（老线路兼容）")


def test_35_has_no_audio_output_block() -> None:
    """3.5：只认顶层 voice，不许出现 audio.output（字段形态不同）。"""
    p = _payload(MODEL_35, "Ethan")
    assert p.get("voice") == "Ethan", p
    assert "output" not in (p.get("audio") or {}), f"3.5 不该有 audio.output：{p.get('audio')!r}"
    print("  ✓ 3.5 只写顶层 voice")


def test_empty_voice_omits_fields() -> None:
    """voice 为空 = 交给服务端默认（3.8 也不该凭空造一个 audio.output）。"""
    s = QwenLiveTranslateSession(SessionConfig(api_key="x", model=MODEL_38, voice=""))
    p = s._session_payload()
    assert "voice" not in p, p
    assert "output" not in (p.get("audio") or {}), p
    print("  ✓ 空音色不下发")


def test_voice_change_is_reflected() -> None:
    """改 config 里的音色 → 下发内容跟着变（防写死 Tina 的回归）。"""
    a = _payload(MODEL_38, "Tina")
    b = _payload(MODEL_38, "Ethan")
    assert a["audio"]["output"]["voice"] != b["audio"]["output"]["voice"], (a, b)
    print("  ✓ 换音色 → 下发内容变化")


if __name__ == "__main__":
    for fn in (test_38_voice_goes_to_audio_output, test_38_keeps_top_level_voice_for_legacy_line,
               test_35_has_no_audio_output_block, test_empty_voice_omits_fields,
               test_voice_change_is_reflected):
        fn()
    print("ALL VOICE PAYLOAD TESTS PASSED")
