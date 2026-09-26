"""打字输入的**译音**：把译文合成成音频，喂给虚拟声卡那条腿。

为什么打字要单独一步 TTS：
    打字走的是**文本翻译**接口（实时模型不接受文本入口，见 `textin.py` 模块注释），
    它只回文本、不回音频 —— 不加这一步，打字内容就永远进不了虚拟声卡、对面听不到。
    语音那条腿的音频是实时模型直出的，这里补的是同格式的替代品。

实测（2026-09）：
- 模型 `qwen3-tts-flash`（也可用 `qwen3-tts-instruct-flash`），返回 `output.audio`
  同时带 `data`(base64) 与 `url`；**优先用 data**，省一次下载且不受 URL 过期影响。
- 音频是 24kHz 单声道 WAV，用 `miniaudio` 解成 **24k 单声道 s16le PCM** ——
  与实时模型译音**同格式**，所以下游可以直接复用 `resample_24k_mono_to_48k_stereo`
  和 `VirtualMic`，不需要任何新管线。
- 音色 `Cherry` 中/英/日都能读（实测），故默认一个音色就够；要换按 config 改。
"""
from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen3-tts-flash"
DEFAULT_VOICE = "Cherry"
DEFAULT_TIMEOUT_S = 30.0

# 目标语言码 → DashScope 的 language_type（可选参数；拿不准就不传，服务端自己判）
LANG_NAMES = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
    "it": "Italian", "pt": "Portuguese",
}

_opener = None


def _get_opener():
    """直连 opener（禁用系统代理）——与 textin 同一取舍：国内端点走代理是纯负担。"""
    global _opener
    if _opener is None:
        _opener = build_opener(ProxyHandler({}))
    return _opener


class TtsError(RuntimeError):
    """合成失败（缺 key / 网络 / 参数 / 空音频）。消息给用户看，带原因不带堆栈。"""


def _decode_to_24k_mono(raw: bytes) -> bytes:
    """任意容器（WAV/MP3/…）→ 24kHz 单声道 s16le PCM。"""
    try:
        import miniaudio

        dec = miniaudio.decode(raw, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=24000)
        return bytes(dec.samples)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"音频解码失败：{type(exc).__name__}: {exc}") from exc


def _fetch(url: str, timeout: float) -> bytes:
    req = Request(url, headers={"Accept": "*/*"})
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            return r.read()
    except (HTTPError, URLError) as exc:
        raise TtsError(f"下载音频失败：{exc}") from exc


def synthesize(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """把一段文本合成为 24kHz 单声道 s16le PCM（与实时模型译音同格式）。

    同步函数（调用方丢线程池里跑）；`language` 是目标语言码（zh/en/ja…），
    会映射成 service 的 `language_type`，拿不准就不传。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")

    payload: dict = {"model": model or DEFAULT_MODEL,
                     "input": {"text": text, "voice": voice or DEFAULT_VOICE}}
    lang_name = LANG_NAMES.get((language or "").lower())
    if lang_name:
        payload["input"]["language_type"] = lang_name

    req = Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json"}, method="POST")
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise TtsError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    try:
        resp = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"响应解析失败：{exc}") from exc
    if isinstance(resp.get("error"), dict):
        raise TtsError(str((resp["error"] or {}).get("message") or resp["error"])[:300])

    audio = ((resp.get("output") or {}).get("audio") or {})
    raw: bytes | None = None
    if audio.get("data"):
        try:
            raw = base64.b64decode(audio["data"])
        except Exception as exc:  # noqa: BLE001
            raise TtsError(f"base64 音频解析失败：{exc}") from exc
    elif audio.get("url"):
        raw = _fetch(str(audio["url"]), timeout)      # URL 有有效期，能不用就不用
    if not raw:
        raise TtsError("服务端没返回音频")
    return _decode_to_24k_mono(raw)
