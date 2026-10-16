"""出网端点的**单一真相源**：两条服务线路（千问云 / 千问云·海外版）的地址派生。

## 两条线路是什么

- `qianwen`   —— 千问云（国内，默认），host `maas.qianwenaiapi.com`；
- `qwencloud` —— **千问云·海外版**（Qwen Cloud，`https://www.qwencloud.com/`），
  host `maas.qwencloudapi.com`。海外用户走这条：注册只要 Google / GitHub / 邮箱，
  不绑卡也能拿到免费额度，比阿里云国际站那套（注册 + 风控 + 业务空间 + 地域）简单得多。

⚠️ **两条线路的 API key 不互通**，各存各的槽（见 `key_slot` / credentials.py）。
⚠️ **2026-10 起海外线路不再走「阿里云百炼·国际版」**：那条线路要业务空间 ID + 地域，
且海外注册会被风控要信用卡流水。老配置里的 `provider: bailian_intl` 与
`*.maas.aliyuncs.com` 地址会被 `is_retired_base_url` 识别，由 config 做一次性迁移并留痕——
**绝不静默**（用户必须知道 key 要重填）。

## 为什么要「宿主派生」

本项目有 4 条出网调用（实时 WS、打字翻译 HTTP、打字译音 TTS、音色试听 Omni），
过去每条腿各写一份国内域名常量（`maas.qianwenaiapi.com`），海外用户根本用不了。
支持两条**互斥**线路后，如果继续让每条腿各存一份域名，就会出现「切了线路、
但某条腿还连着老域名」这类最难查的漂移 —— 这正是 #12「同一件事写两遍漏一半」的翻版。

所以这里定一条铁律：**`session.base_url` 是唯一的地址真相源**，另外两条 HTTP
端点（chat / multimodal）一律从它的 **host 派生**（`chat_url` / `multimodal_url`），
禁止在别处再写第二份域名常量。`provider` 只负责界面默认值 / key 槽 / 校验 /
日志摘要，**实际连接地址永远以 base_url 为准**。

## 关键事实：两条线路的三条路径**同形**（已按官方文档逐条核实）

    WebSocket 实时：  /api-ws/v1/realtime
    OpenAI 兼容：     /compatible-mode/v1/chat/completions
    多模态（TTS）：    /api/v1/services/aigc/multimodal-generation/generation

千问云·海外版（docs.qwencloud.com）与国内千问云只有 **host 不同**，三条路径后缀完全一致
（实时：`wss://maas.qwencloudapi.com/api-ws/v1/realtime`；OpenAI 兼容：
`https://maas.qwencloudapi.com/compatible-mode/v1`；TTS 走
`POST /api/v1/services/aigc/multimodal-generation/generation`）。
正因同形，才可以「拿 base_url 的 host + 固定路径」拼出另外两条腿，
换线路时用户只需要改一个 base_url，三条腿自动跟着走。

海外线路**不需要**业务空间 ID、也没有地域段 —— 地址里没有可变的账号成分，
所以 host 就是公共域名（这也是它比国际站简单的地方）。

## 依赖纪律

只依赖标准库（`urllib.parse`）。**绝不 import `vlt.engine`**：engine 反向依赖
本模块（接线时调 `chat_url` / `multimodal_url` / `describe`），import 回去会成环。
"""
from __future__ import annotations

from urllib.parse import urlsplit

# ---------------------------------------------------------------- 线路（provider）

PROVIDER_QIANWEN = "qianwen"
PROVIDER_QWENCLOUD = "qwencloud"
PROVIDER_CHATGPT = "chatgpt"
PROVIDERS = (PROVIDER_QIANWEN, PROVIDER_QWENCLOUD, PROVIDER_CHATGPT)
DEFAULT_PROVIDER = PROVIDER_QIANWEN

# 线路中文名（**只进日志**，不是界面文案；界面文案走 t()）。
_PROVIDER_CN = {
    PROVIDER_QIANWEN: "千问云",
    PROVIDER_QWENCLOUD: "千问云·海外版",
    PROVIDER_CHATGPT: "ChatGPT 订阅语音",
}

# 已下线线路的别名 → 现行线路。老 config.yaml 里可能写着 `bailian_intl`：
# 归一化时**照常留痕**（见 normalize_provider），并由 config.load_config 顺手把
# 指向旧域名的 base_url 迁移掉。
RETIRED_PROVIDERS = {
    "bailian_intl": PROVIDER_QWENCLOUD,
    "bailian": PROVIDER_QWENCLOUD,
    "dashscope_intl": PROVIDER_QWENCLOUD,
}

# ---------------------------------------------------------------- 路径（两条线路同形）

WS_PATH = "/api-ws/v1/realtime"
CHAT_PATH = "/compatible-mode/v1/chat/completions"
MULTIMODAL_PATH = "/api/v1/services/aigc/multimodal-generation/generation"

# ---------------------------------------------------------------- host

HOSTS = {
    PROVIDER_QIANWEN: "maas.qianwenaiapi.com",
    PROVIDER_QWENCLOUD: "maas.qwencloudapi.com",
}

# 已下线线路的 host 特征（阿里云百炼·国际版当年生成的地址）。命中即由 config 迁移到海外版。
# ⚠️ 刻意**只列当年的地域**（`{workspace_id}.{region}.maas.aliyuncs.com`，region 取自旧的
#    地域表），不写成「所有 *.maas.aliyuncs.com」：国内百炼的业务空间地址是
#    `{workspace_id}.cn-beijing.maas.aliyuncs.com`，那种属于国内线路，绝不能被挪到海外版。
RETIRED_HOST_SUFFIXES = tuple(
    f".{rid}.maas.aliyuncs.com" for rid in (
        "ap-southeast-1", "ap-northeast-1", "us-east-1", "eu-central-1", "cn-hongkong",
    )
) + ("dashscope-intl.aliyuncs.com",)   # 国际站 DashScope 兼容地址（当年文档里的另一种写法）

# 注册/开通入口（界面「去哪申请 key」按钮用；未知 provider 回落千问云那条）。
# 千问云·海外版给的是 **Qwen Cloud 首页**（注册入口 / 免费额度 / API key 都在里面）；
# 别换回 modelstudio.console.alibabacloud.com —— 那条路要绑卡 + 风控。
SIGNUP_URLS = {
    PROVIDER_QIANWEN: "https://www.qianwenai.com/",
    PROVIDER_QWENCLOUD: "https://www.qwencloud.com/",
}


# ---------------------------------------------------------------- 归一化（非法值回落 + 留痕，绝不静默）


def normalize_provider(raw: object) -> str:
    """把任意取值规范成一个合法线路 id；非法值**留痕后**回落默认（qianwen）。

    为什么要处理已下线线路：老 config.yaml 里写着 `bailian_intl` 时，
    静默当成「未识别」会让用户以为配置丢了；这里明确告诉 TA「那条线路已下线，
    按千问云·海外版处理，key 要重填」——配置是手写的，但**绝不静默**。
    """
    val = str(raw).strip().lower()
    if val in PROVIDERS:
        return val
    retired = RETIRED_PROVIDERS.get(val)
    if retired is not None:
        print(f"[endpoints] ⚠️ 服务线路 {raw!r} 已下线（阿里云百炼·国际版），"
              f"改按 {_PROVIDER_CN[retired]} 处理 —— 该线路的 API key 与国内版不互通，"
              f"请在「设置 → 常规」里重新填写并保存一次", flush=True)
        return retired
    print(f"[endpoints] ⚠️ 未识别的服务线路 {raw!r}，按默认 {DEFAULT_PROVIDER} 处理",
          flush=True)
    return DEFAULT_PROVIDER


def provider_name(provider: str) -> str:
    """线路的中文名（日志/诊断用）。未知 provider 先归一化再取名，绝不 KeyError。"""
    return _PROVIDER_CN[normalize_provider(provider)]


def signup_url(provider: str) -> str:
    """该线路的注册入口 URL；未知 provider 回落千问云那条（绝不 KeyError）。"""
    return SIGNUP_URLS.get(normalize_provider(provider), SIGNUP_URLS[DEFAULT_PROVIDER])


# ---------------------------------------------------------------- 已下线线路的识别


def is_retired_base_url(base_url: object) -> bool:
    """base_url 是否指向**已下线**的阿里云国际站/百炼域名（config 据此做迁移）。

    只认 host 后缀，不做任何字符串替换：认不出来（手填的第三方地址）就返回 False，
    交给调用方按「以 base_url 为准」处理。
    """
    try:
        host = urlsplit(str(base_url or "")).netloc.lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == suf or host.endswith(suf) for suf in RETIRED_HOST_SUFFIXES)


# ---------------------------------------------------------------- 地址派生


def default_base_url(provider: str) -> str:
    """某条线路的**默认** base_url（实时 WS 端点，唯一真相源）。

    - qianwen   → `wss://maas.qianwenaiapi.com/api-ws/v1/realtime`
    - qwencloud → `wss://maas.qwencloudapi.com/api-ws/v1/realtime`

    两条线路都不带占位符 / 账号成分：host 是公共域名，直接可连。
    """
    prov = normalize_provider(provider)
    if prov == PROVIDER_CHATGPT:
        return "codex://app-server"
    return f"wss://{HOSTS[prov]}{WS_PATH}"


def host_of(base_url: str) -> str:
    """从 base_url 取出 host（netloc）——另外两条 HTTP 端点都靠它派生。

    解析不出 host（没写 scheme、空串等）→ 抛 ValueError，绝不返回空串让调用方
    拼出 `https:///compatible-mode/...` 这种残废地址。

    ⚠️ 异常消息**刻意不回显 base_url**：地址由用户手写在 config.yaml 里，
    回显进异常等于把它抄进日志（config / engine 都会打印捕获到的异常）。
    故这里只讲原因、不带值。
    """
    netloc = urlsplit(str(base_url or "")).netloc
    if not netloc:
        raise ValueError("无法从 base_url 解析出 host（scheme 缺失或地址为空）")
    return netloc


def chat_url(base_url: str) -> str:
    """打字翻译 / 音色试听（OpenAI 兼容 chat/completions）端点：从 base_url 的 host 派生。"""
    return f"https://{host_of(base_url)}{CHAT_PATH}"


def multimodal_url(base_url: str) -> str:
    """打字译音 TTS（多模态生成）端点：从 base_url 的 host 派生。"""
    return f"https://{host_of(base_url)}{MULTIMODAL_PATH}"


def key_slot(provider: str) -> str:
    """该线路对应的**密钥槽名**（就是线路 id 本身）。

    千问云与千问云·海外版的 key **不通用**，故分槽各存一份（见 credentials.py），
    切线路不用重填。
    """
    return normalize_provider(provider)


def describe(provider: str, base_url: str) -> str:
    """一行日志摘要：线路 + host。换线路排查时第一眼要看它。

    形如：`线路=千问云·海外版 host=maas.qwencloudapi.com`

    **绝不抛**：host 解析不出时降级成一个占位串，日志本身不能把启动搞挂；
    **API key 一律不出现**。
    """
    prov = normalize_provider(provider)
    try:
        host = host_of(base_url)
    except ValueError:
        host = "<host 解析失败>"
    return f"线路={_PROVIDER_CN[prov]} host={host}"
