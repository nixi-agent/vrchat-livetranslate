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
- **流式**（`synthesize_stream`，请求头 `X-DashScope-SSE: enable`）：同一句 20~30 字实测
  首包 **0.36~0.42s**、整段 1.6~1.7s；下游虚拟声卡是抖动缓冲（攒 300ms 起播），拿到前几个
  分片就能开口 —— 打字腿「开口」从 ~1.7s 降到 **~0.5s**。
  ⚠️ 服务端在流**末尾**还会补发一片「整段汇总」（实测与前面所有分片**逐字节相同**）：
  必须丢弃，否则整句会被念两遍。
"""
from __future__ import annotations

import base64
import json
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .tts_transport import _Response, _post_pooled, _close_pooled, _FramingError, _PostOutcomeUnknown  # noqa: F401

ENDPOINT = "https://maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen3-tts-flash"
DEFAULT_VOICE = "Cherry"
DEFAULT_TIMEOUT_S = 30.0

# 说话译音用的是 Qwen-Omni 系列音色（Tina/Cindy/…），qwen3-tts-flash **不认这些 id**
# （会 InvalidParameter）。要试听它们只能走非实时 Qwen-Omni —— 同一个 compatible-mode
# 端点（textin.py 已在用、同一把 key、无需 workspace），但音频输出**强制流式**。
OMNI_ENDPOINT = "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions"
DEFAULT_OMNI_MODEL = "qwen3.5-omni-flash"
DEFAULT_OMNI_VOICE = "Tina"

# 目标语言码 → DashScope 的 language_type（可选参数；拿不准就不传，服务端自己判）
LANG_NAMES = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
    "it": "Italian", "pt": "Portuguese", "th": "Thai",
}

_opener = None
_DEFAULT_OPENER = None        # 模块自建的那个 opener（被替换过就不走连接池，见 _pool_allowed）


def _get_opener():
    """直连 opener（禁用系统代理）——与 textin 同一取舍：国内端点走代理是纯负担。"""
    global _opener, _DEFAULT_OPENER
    if _opener is None:
        _opener = build_opener(ProxyHandler({}))
        _DEFAULT_OPENER = _opener
    return _opener


def _open_response(req, timeout: float, *, reuse_conn: bool):
    """取响应：优先池化复用；只有发送前连接失败或幂等下载才能回退直连。

    回退与重连都留痕（本仓库约定：禁静默降级）。`reuse_conn=False` 时与以前**完全一致**。
    """
    if reuse_conn and _pool_allowed():
        try:
            resp, conn, key = _post_pooled(req, timeout, _note)
            return _Response(resp, conn, key)
        except HTTPError:
            raise
        except (_FramingError, _PostOutcomeUnknown) as exc:
            raise TtsError(f"HTTP 请求失败，未自动重送：{exc}") from exc
        except Exception as exc:                              # noqa: BLE001
            _note(f"连接复用不可用（{type(exc).__name__}: {exc}）→ 本次回退直连")
    return _Response(_get_opener().open(req, timeout=timeout))


def _pool_allowed() -> bool:
    """是否允许走连接池。

    规则：**取 opener 的路径被替换过就不池化**。两种替换方式都要挡住：
      * `module._opener = fake`（直接塞 opener）；
      * `module._get_opener = lambda: fake`（换掉取 opener 的函数）。
    替换者（用例里的假 opener、或别的接管 HTTP 层的代码）期望自己看到每一个请求；
    池化会绕过它 —— 实测后果是打了假 opener 的用例直接打到真端点上去（401）。
    """
    return (_get_opener is _DEFAULT_GET_OPENER
            and (_opener is None or _opener is _DEFAULT_OPENER))


_DEFAULT_GET_OPENER = _get_opener      # 供上面的守卫比对（模块导入时就固定下来）


class TtsError(RuntimeError):
    """合成失败（缺 key / 网络 / 参数 / 空音频）。消息给用户看，带原因不带堆栈。"""


class TtsStreamTruncated(TtsError):
    """流式中途断了，但**已经 yield 出去的分片有效**（少半句，不整句丢）。

    调用方约定：不要把已经推给声卡的部分撤掉，也不要在状态栏报「合成失败」——
    改成一条 warn 级提示（用户听出「这句好像没说完」时，界面上得有个交代）。
    """


def _note(msg: str) -> None:
    """兜底/降级路径的留痕（本仓库约定：**禁静默降级**，每一处降级都要能查）。

    比 print 多一层保护：日志本身绝不能把主流程搞挂。
    """
    try:
        print(f"[tts] {msg}", flush=True)
    except Exception:  # noqa: BLE001
        pass


# 「协议不认流式」这类码：退回整段还有意义。其余（401/403/429/5xx）再发一次也是白搭。
_SSE_FALLBACK_CODES = frozenset({400, 406, 415})


def _cut_note(got: int, exc: BaseException) -> str:
    """流式中途断掉：留一行痕 + 生成抛给调用方的消息（同一份文案，不写两处）。"""
    msg = f"流式中途中断（{type(exc).__name__}: {exc}），已保留 {got} 个分片（少半句、不整句丢）"
    _note(msg)
    return msg


def _decode_to_24k_mono(raw: bytes) -> bytes:
    """任意容器（WAV/MP3/…）→ 24kHz 单声道 s16le PCM。"""
    try:
        import miniaudio

        dec = miniaudio.decode(raw, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=24000)
        return bytes(dec.samples)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"音频解码失败：{type(exc).__name__}: {exc}") from exc


def _fetch(url: str, timeout: float, *, reuse_conn: bool = True) -> bytes:
    req = Request(url, headers={"Accept": "*/*"})
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as r:
            return r.read()
    except (HTTPError, URLError) as exc:
        raise TtsError(f"下载音频失败：{exc}") from exc


def _extract_audio(obj: dict, timeout: float, *, reuse_conn: bool = True) -> bytes:
    """从响应体里取音频原始字节：优先 base64 的 `data`，退回 `url` 下载。"""
    audio = (obj.get("output") or {}).get("audio") or {}
    if audio.get("data"):
        try:
            return base64.b64decode(audio["data"])
        except Exception as exc:  # noqa: BLE001
            raise TtsError(f"base64 音频解析失败：{exc}") from exc
    if audio.get("url"):
        return _fetch(str(audio["url"]), timeout, reuse_conn=reuse_conn)   # URL 有有效期，能不用就不用
    return b""


def _raise_if_error(obj: dict) -> None:
    """服务端错误有两种外壳：`{"error": {...}}` 与带 code/message 的扁平形态。"""
    if isinstance(obj.get("error"), dict):
        raise TtsError(str((obj["error"] or {}).get("message") or obj["error"])[:300])
    if obj.get("code") and obj.get("message"):
        raise TtsError(str(obj["message"])[:300])


def _chunk_to_pcm(chunk: bytes) -> bytes:
    """SSE 分片 → 24k 单声道 s16le。文档说分片是裸 PCM；万一是容器（RIFF）就解一次。"""
    if chunk[:4] == b"RIFF":
        return _decode_to_24k_mono(chunk)
    return chunk[: len(chunk) - (len(chunk) % 2)]


def synthesize(
    text: str,
    *,
    reuse_conn: bool = True,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    endpoint: str | None = None,
) -> bytes:
    """把一段文本合成为 24kHz 单声道 s16le PCM（与实时模型译音同格式）。

    同步函数（调用方丢线程池里跑）；`language` 是目标语言码（zh/en/ja…），
    会映射成 service 的 `language_type`，拿不准就不传。

    `endpoint`：多模态地址由**调用方**按当前线路从 base_url 的 host 派生后传入
    （见 endpoints.multimodal_url）；不传则回落模块常量 ENDPOINT（千问云默认）。
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

    req = Request(endpoint or ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json"}, method="POST")
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as r:
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
    _raise_if_error(resp)
    raw = _extract_audio(resp, timeout, reuse_conn=reuse_conn)
    if not raw:
        raise TtsError("服务端没返回音频")
    return _decode_to_24k_mono(raw)


def synthesize_stream(
    text: str,
    *,
    reuse_conn: bool = True,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    endpoint: str | None = None,
) -> Iterator[bytes]:
    """流式合成（SSE）：边生成边 yield 24k 单声道 s16le 的 PCM 分片。

    为什么值得：实测同一句 20~30 字，首包 0.36~0.42s、整段 1.6~1.7s；而下游虚拟声卡
    是抖动缓冲（攒到 buffer_ms 起播 / 停更 0.35s 强制起播），拿到前几个分片就能开口 ——
    打字腿的「开口」从 ~1.7s 降到 ~0.5s。

    `endpoint`：同 `synthesize`（由调用方按线路派生传入，不传回落 ENDPOINT）。
    ⚠️ 退回整段时要把 endpoint **一路透传**给 `synthesize`，否则兜底那条腿会悄悄
    读回模块常量、连到默认线路去（切了海外线路时正是最难查的那种漂移）。

    退回策略（调用方不必写两套逻辑；**每一处降级都留痕，禁静默降级**）：
    - 服务端没给 `text/event-stream`（或一个分片都没拿到）→ 退回整段 `synthesize()`，yield 一整块；
    - 流式被**协议性**拒绝（HTTP 400/406/415）→ 退回整段 `synthesize()`；
    - 中途断了（网络/服务端）→ **保留已经 yield 的分片**，随后抛 `TtsStreamTruncated`
      （少半句，不整句丢；调用方据此给一条 warn 提示）。
    - 其余错误（401/403/429/5xx、网络不可达）直接抛：再发一次同样会失败，只会把
      失败延迟翻倍、白耗一次配额。
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
    req = Request(endpoint or ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json",
                           # 实测：这两个头一起给，服务端才按 SSE 分片推
                           "Accept": "text/event-stream",
                           "X-DashScope-SSE": "enable"}, method="POST")
    got = 0
    acc = bytearray()          # 已发出的音频（用来识别末尾那片「整段汇总」）
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as resp:
            ctype = str(resp.headers.get("Content-Type", "") or "")
            if "event-stream" not in ctype:            # 服务端降级成了整段响应
                _note(f"服务端没按 SSE 回（Content-Type={ctype!r}）→ 退回整段合成")
                try:
                    obj = json.loads(resp.read().decode("utf-8", "replace"))
                except Exception as exc:  # noqa: BLE001
                    raise TtsError(f"响应解析失败：{exc}") from exc
                _raise_if_error(obj)
                raw = _extract_audio(obj, timeout, reuse_conn=reuse_conn)
                if raw:
                    # ⚠️ 顺序不能反：**先解码成功、再算「已送出」**。曾经先 `got += 1` 再 yield，
                    # 解码一失败就报「已保留 1 个分片（少半句、不整句丢）」—— 可实际上一个字
                    # 都没送出去，调用方会把「这段音频根本解不开」误当成「流式只给了半句」，
                    # 也不会再走「一个分片都没拿到 → 整段兜底」（实测复现）。
                    pcm = _decode_to_24k_mono(raw)
                    got += 1
                    yield pcm
            else:
                for line in resp:
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue                            # 心跳 / 空行 / event: 行
                    data = line[5:].strip()
                    if not data or data == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:                       # noqa: BLE001
                        continue                            # 非 JSON 的分片直接跳过
                    _raise_if_error(obj)
                    raw = _extract_audio(obj, timeout, reuse_conn=reuse_conn)
                    if not raw:
                        continue
                    pcm = _chunk_to_pcm(raw)
                    if not pcm:
                        continue
                    # ⚠️ 服务端在流末尾会补发一片「整段汇总」（实测与前面所有分片逐字节相同）：
                    # 吃掉它，否则虚拟声卡会把整句念两遍。
                    # 判据是**经验性**的：实测中真分片不会与已累计音频等长且逐字节相同
                    # （要触发得正好等于此前所有分片之和 + 内容一致）—— 是经验保证，不是不变式。
                    if acc and len(pcm) == len(acc) and pcm == bytes(acc):
                        continue
                    got += 1
                    acc += pcm
                    yield pcm
    except TtsError as exc:
        if got:                                             # 已唱出去的部分不撤
            raise TtsStreamTruncated(_cut_note(got, exc)) from exc
        raise
    except HTTPError as exc:
        if got:
            raise TtsStreamTruncated(_cut_note(got, exc)) from exc
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        err = TtsError(f"HTTP {exc.code}：{detail or exc.reason}")
        # 只有「协议不认流式」的码才值得退回整段：401/403/429/5xx 再发一次同样会失败，
        # 只会把失败延迟翻倍、白耗一次配额 → 直接抛。
        if exc.code not in _SSE_FALLBACK_CODES:
            raise err from exc
        _note(f"服务端不认流式（HTTP {exc.code}）→ 退回整段合成")
        try:
            yield synthesize(text, voice=voice, model=model, api_key=api_key,
                             language=language, timeout=timeout, endpoint=endpoint, reuse_conn=reuse_conn)
        except TtsError:
            raise err from exc
        return
    except URLError as exc:
        if got:
            raise TtsStreamTruncated(_cut_note(got, exc)) from exc
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        if got:
            raise TtsStreamTruncated(_cut_note(got, exc)) from exc
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    if not got:                                             # 流式没给东西 → 整段兜底
        _note("流式一个分片都没拿到 → 退回整段合成")
        yield synthesize(text, voice=voice, model=model, api_key=api_key,
                         language=language, timeout=timeout, endpoint=endpoint, reuse_conn=reuse_conn)


def synthesize_omni(
    text: str,
    *,
    reuse_conn: bool = True,
    voice: str = DEFAULT_OMNI_VOICE,
    model: str = DEFAULT_OMNI_MODEL,
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
    endpoint: str | None = None,
) -> bytes:
    """用**非实时 Qwen-Omni** 合成一段文本 → 24kHz 单声道 s16le PCM（与 `synthesize` 同格式）。

    为何单独一条路：说话译音的音色（Tina/Cindy/Liora Mira…）属于 Qwen-Omni 系列，
    `qwen3-tts-flash` 不支持（跨模型混用会 InvalidParameter），要试听只能走 Omni。

    实现要点（均有官方文档依据）：
    - Omni 是对话模型，音频输出**必须** `stream=True`；自己解 SSE，把分片的
      `choices[0].delta.audio.data`（base64）**拼接后一次解码**（官方示例就是这么干的）。
    - 它不是逐字 TTS：下一条指令让它朗读样例句，个别措辞可能略有出入 —— 试听音色足够。
    - 回的是 24k 单声道音频（可能裸 PCM、也可能带 WAV 头），统一过一遍解码器，
      解不动就当裸 s16le PCM 直接用（本就是目标格式）。

    `endpoint`：音色试听走的是 chat/completions（与打字翻译同一条），由调用方按线路
    从 base_url 的 host 派生后传入（见 endpoints.chat_url）；不传回落 OMNI_ENDPOINT。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")

    payload = {
        "model": model or DEFAULT_OMNI_MODEL,
        "messages": [{"role": "user",
                      "content": f"请逐字朗读下面引号内的这句话，只朗读、不要回答或补充任何内容：「{text}」"}],
        "modalities": ["text", "audio"],
        "audio": {"voice": voice or DEFAULT_OMNI_VOICE, "format": "wav"},
        "stream": True,                             # ⚠️ Omni 音频输出必须流式
        "stream_options": {"include_usage": True},
    }
    req = Request(endpoint or OMNI_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json",
                           "Accept": "text/event-stream"}, method="POST")
    b64: list[str] = []
    try:
        with _open_response(req, timeout, reuse_conn=reuse_conn) as r:
            for raw_line in r:                        # 逐行读 SSE
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:  # noqa: BLE001 — 心跳/不完整帧直接略过
                    continue
                if isinstance(obj.get("error"), dict):
                    err = obj["error"]
                    raise TtsError(str(err.get("message") or err)[:300])
                choices = obj.get("choices") or []
                if not choices:
                    continue
                aud = (choices[0].get("delta") or {}).get("audio") or {}
                if aud.get("data"):
                    b64.append(aud["data"])
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise TtsError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except TtsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    if not b64:
        raise TtsError("服务端没返回音频")
    try:
        raw = base64.b64decode("".join(b64))
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"base64 音频解析失败：{exc}") from exc
    try:
        return _decode_to_24k_mono(raw)
    except TtsError:
        return raw                                  # 已是裸 24k 单声道 s16le PCM，直接用
