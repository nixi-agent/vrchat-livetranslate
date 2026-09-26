"""打字输入：把一段文本翻译好，再当作「我说的一句话」送进既有输出管线。

**为什么不复用实时会话**（踩过的坑，别再试）：
    实测 `qwen3.8-livetranslate-flash-realtime` **不接受文本入口** ——
    发 `conversation.item.create`（input_text）之后再发 `response.create`，
    服务端直接回 `invalid_request_error / invalid_value: 'response.create'`。
    所以打字走百炼的**文本翻译**接口（compatible-mode /chat/completions）。

**为什么默认 `qwen-mt-flash` 而不是 `qwen3-livetranslate-flash`**：
    后者是实时模型的文本兄弟，但实测它的文本接口**会原样回吐**（中文进、中文出；
    同一模型换个句子又正常，行为不可依赖）。`qwen-mt-flash` 在
    zh→en / ja / ru / ko、en→zh、ja→zh 上都稳定，且**不填 source_lang 时自动识别**
    源语言（实测），所以「源语言=自动检测」的方向也能用。

本模块只负责「文本 → 译文」，不碰输出面：发到哪里由 Engine.send_text 决定，
这样打字和说话走的是**同一条**下游（界面气泡 / 手腕屏 / chatbox 节流器）。
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen-mt-flash"
DEFAULT_TIMEOUT_S = 20.0

# 延迟创建的直连 opener（测试里直接替换这个模块级对象即可离线验证请求体）
_opener = None


def _get_opener():
    """返回一个**显式禁用代理**的 opener。

    国内端点直连：注册表里的系统代理（翻墙客户端）在国内域名上是纯负担，
    代理挂掉时会把本可直连的百炼请求一起拖死 —— 这个项目已经在代理上吃过亏。
    """
    global _opener
    if _opener is None:
        _opener = build_opener(ProxyHandler({}))
    return _opener


class TextTranslateError(RuntimeError):
    """打字翻译失败（缺 key / 网络 / 参数 / 空结果）。

    消息是给用户看的（会进状态栏与日志），不带原始堆栈。
    """


def _extract(resp: dict) -> str:
    """从 compatible-mode 响应里取译文；顺便识别 DashScope 的错误外壳。"""
    if isinstance(resp.get("error"), dict):
        err = resp["error"]
        raise TextTranslateError(str(err.get("message") or err)[:300])
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TextTranslateError(f"响应里没有译文（{type(exc).__name__}）：{str(resp)[:200]}") from exc
    text = (content or "").strip() if isinstance(content, str) else ""
    if not text:
        raise TextTranslateError("服务端返回了空译文")
    return text


def translate_text(
    text: str,
    *,
    target_lang: str,
    source_lang: str | None = None,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str:
    """同步翻译一段文本（调用方放在线程里跑，别阻塞事件循环）。

    source_lang 为 None/空 → 不传该字段，服务端自动识别（实测 mt 系列支持）。
    """
    text = (text or "").strip()
    if not text:
        raise TextTranslateError("内容为空")
    if not (target_lang or "").strip():
        raise TextTranslateError("目标语言未设置")
    if not (api_key or "").strip():
        raise TextTranslateError("还没配置 API key（见界面右上角「设置」）")

    options: dict[str, str] = {}
    if source_lang:
        options["source_lang"] = str(source_lang)
    options["target_lang"] = str(target_lang)

    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": text}],
        # ⚠️ 必须有：缺 translation_options 时服务端直接 400
        #    （InvalidParameter: The input translation_options is not supported）
        "translation_options": options,
    }
    req = Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise TextTranslateError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except URLError as exc:
        raise TextTranslateError(f"网络不可达：{exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise TextTranslateError(f"{type(exc).__name__}: {exc}") from exc

    try:
        return _extract(json.loads(body))
    except TextTranslateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TextTranslateError(f"响应解析失败：{exc}") from exc


# 断句优先字符：中英标点 + 空格（CJK 没有空格，所以要认标点）
_BREAK_CHARS = " \n\t，。！？；：、,.!?;:…—)\"」』）"


def split_for_chatbox(text: str, limit: int = 144) -> list[str]:
    """把一段文本按 chatbox 的单条上限切分（默认 144 字符）。

    为什么不直接交给 `Chatbox.sanitize`：那是给**流式字幕**写的 —— 超长时保留
    **最后** 144 字（字幕越新越重要，这个取舍对字幕是对的）。打字输入是一整句，
    截尾会把开头吃掉、看着像翻译坏了，所以改成切成多条依次发；被漏桶挡下的
    「最终版」会进 Chatbox 的补发队列，不会丢。
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = max(rest.rfind(ch, 0, limit + 1) for ch in _BREAK_CHARS)
        # 断点太靠前（不足半条）或整段没有断点 → 硬切，否则会切出很短的一条
        cut = cut + 1 if cut >= limit // 2 else limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return [c for c in out if c]

