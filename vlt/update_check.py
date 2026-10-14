"""检查更新：纯逻辑全部在这个模块，HTTP 层可注入，GUI 只负责接线。

设计约束（写实现时别破坏）：
- 不做任何 Tk import；所有函数可离线测试。
- 任何失败都抛 UpdateCheckError（消息给用户看），绝不抛裸异常给调用方。
- 网络访问走默认 opener（尊重 HTTPS_PROXY / 系统代理）——GitHub 国内直连不稳，
  与 textin.py 显式禁代理（国内端点）正好相反，别抄错。
- 每个结论都留一行 [update] 日志（成功/跳过/失败各一行），不许静默降级。

## 两个更新源：Cloudflare R2（主）→ GitHub Release（兜底）

大陆用户访问 GitHub 不稳定（API 限流 60 次/小时、下载域名常被墙），所以**主源**是自家
Cloudflare R2 上的静态清单 `MANIFEST_URL`：

  * 清单由 `scripts/publish_r2.py` 在发版时生成（**那边是格式的唯一真相**，含字段说明）；
  * 产物地址与 sha256 都在清单里 → 下载不必再问 GitHub，也不受 GitHub 附件 CDN 的影响；
  * R2 上全是**固定键**、对象带 `no-cache` → 覆盖发布后立刻生效（理由见 publish_r2.py 头部）。

主源任何一步失败（连不上 / 清单不合契约 / 那一版没有本形态的包）都**回落**到原来的 GitHub
API 源，并各留一行 `[update]` 日志：R2 挂掉或格式升级时老路径还在，不至于让所有人收不到更新。
反过来，GitHub 挂了不影响主路径 —— 这就是这次改动的全部目的。

**下载阶段也有兜底**（评审 §3）：`download_and_verify(..., fallback=…)` 允许在主源产物下载
失败时用**同一版本**的 GitHub 附件重试一次 —— 大陆最常见的失败形态不是「清单拿不到」（843 B，
秒级），而是「清单拿到了、37 MB 下到一半断了」，那时重试同一个 CF 地址往往还是断。

⚠️ **信任边界（读一遍再改这个文件）**：`dl.kcm-nixi.cn` 是**更新链路的信任锚** —— 版本号、
产物地址与 sha256 都由它提供，而 sha256 是**清单自证**（同一源给的摘要只能证明「字节没坏」，
**不等于「这份内容是我发的」**）。所以：域名 / DNS / CF 账号 / R2 桶写权限，任一失守都等于
可以给所有客户端下发任意程序。HTTPS 挡不住这一类（攻击者持有该域名的**有效证书**）。
- 真正的解决办法是**清单签名**（公钥编译进客户端、私钥不进 CDN）—— 本次评审后决定**不做**，
  记为待办（`CONTRIBUTING.md` §七）；在此之前靠运维收敛暴露面（域名自动续费 / registrar lock /
  CF 2FA / CAA / 桶级最小权限 token）。
- 应急手段：环境变量 `VLT_UPDATE_SOURCE=github` 可**强制跳过主源**（见 `_forced_source()`）。

⚠️ 主源失败时抛的消息只进日志（用户看不到），所以那些文案**不走 `t()`**；
   最终能到用户眼前的是兜底源（GitHub）的错误，仍按原样走词表。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml

from .version import display_version
from .config_io import _fmt_scalar, _write_config_text, _yaml_set_in_text
from .i18n import t
from .paths import is_frozen

# 更新源（主）：自家 Cloudflare R2 上的静态清单。生成方 = scripts/publish_r2.py（格式的唯一真相）；
# 域名写在客户端白名单里，改域要两边同时改（publish_r2.BASE_URL / MANIFEST_URL / ALLOWED_HOSTS）。
MANIFEST_URL = "https://dl.kcm-nixi.cn/latest.json"
MANIFEST_HOST = "dl.kcm-nixi.cn"
MANIFEST_SCHEMA = 1
# 更新源（兜底）：GitHub API。大陆不稳，只在主源失败时走。
RELEASES_LATEST_API = "https://api.github.com/repos/nixi-agent/vrchat-livetranslate/releases/latest"
# Release 页面基址（与上面的 API 同仓库）：拼 tag 页 = f"{RELEASES_HTML}/tag/vX.Y.Z"
RELEASES_HTML = "https://github.com/nixi-agent/vrchat-livetranslate/releases"
DEFAULT_TIMEOUT_S = 10.0
DOWNLOAD_TIMEOUT_S = 120.0
EXE_ASSET_NAME = "VRChatLiveTranslate.exe"
# Linux 侧要换的那个附件（scripts/build_appimage.sh 产出的名字，release.yml 原样传上去）。
# 「换哪个文件」由运行形态定，见 asset_name_for()：Windows 单文件 exe ↔ Linux AppImage。
APPIMAGE_ASSET_NAME = "VRChatLiveTranslate-x86_64.AppImage"
SUMS_ASSET_NAME = "SHA256SUMS.txt"
# 待替换文件的落点后缀：下载物放在**安装文件同目录**（同卷 rename 才原子）。
# exe → VRChatLiveTranslate.exe.new；AppImage → VRChatLiveTranslate-x86_64.AppImage.new。
NEW_SUFFIX = ".new"
# exe 同目录：{"version": "...", "sha256": "..."}（【稍后】与异常恢复的凭据）
PENDING_JSON = "update_pending.json"
# APP_DIR：{"last_seen_version": "..."}（版本变化一次性提示用）
STATE_JSON = "update_state.json"

#: 紧急开关的**环境变量名**：值置 `github` ⇒ `fetch_latest_release()` 跳过主源、直接走 GitHub 兜底。
#: 用途只有一个：主源被判定不可信（域名/桶失守、清单异常）时，让人能立刻绕开它 ——
#: 这是本次评审要求的「可吊销手段」。它不是日常开关（除了排障没人该设它），所以**不写进 config.yaml**
#: （配置会随用户数据长期留着，而这是运维侧的临时决定）。
#: ⚠️ 边界（评审二审 §6）：它只管**检查**这一步（拿版本/地址）。已经打开的更新窗口里那次下载
#: 用的是当时取到的那份清单，**不受这个开关影响** —— 要彻底断掉主源，得在域名/DNS 侧动手
#: （或让桶里那份清单失效），别指望一个环境变量包打天下。
ENV_UPDATE_SOURCE = "VLT_UPDATE_SOURCE"

# 只允许这几个 host：清单与产物（dl.kcm-nixi.cn）、查最新（api.github.com）、
# asset 跳转起点（github.com）、asset 实际下载（两个 CDN 域）。重定向目标与最终落地地址都必须过这道白名单。
ALLOWED_HOSTS = {MANIFEST_HOST, "api.github.com", "github.com",
                 "objects.githubusercontent.com", "release-assets.githubusercontent.com"}

_CHUNK = 256 * 1024


class UpdateCheckError(RuntimeError):
    """检查/下载更新失败。消息直接给用户看，不带堆栈。"""


class ChecksumMismatch(UpdateCheckError):
    """下载下来的字节与清单/Release 声明的 sha256 不一致。

    单独一类是为了**不触发备用源重试**（评审二审 §3）：摘要不符是完整性问题，
    换个源再下一遍修不好它（同一个版本在两个源上本该是同一份字节），只会拖时间；
    而网络类失败（超时 / 连接重置）换个源确实常常能好。
    """


# ---------------------------------------------------------------- 版本解析与比较

# 只认严格 X.Y.Z 三段非负整数、不带前导零 —— release.yml 的对账正则就是这个形态，
# 出现 0.1 / 0.1.2-beta / 1.02.3 说明 tag 异常，调用方留痕跳过，绝不硬猜。
_VERSION_RE = re.compile(r"^[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """'v0.1.2' / '0.1.2' / 'V1.2.3' → (0,1,2)；不是严格 X.Y.Z 返回 None。"""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(remote_tag: str, local_version: str) -> bool:
    """remote 比 local 新 → True；任一边非法 → False（防御：绝不崩）。"""
    r, l = parse_version(remote_tag), parse_version(local_version)
    if r is None or l is None:
        return False
    return r > l


# ---------------------------------------------------------------- 拉取最新 Release


@dataclass(frozen=True)
class ReleaseInfo:
    """一次「最新 Release」查询的结果。

    ⚠️ 字段叫 `asset_*` 而不是 `exe_*`：这套更新器现在服务两种产物 ——
    Windows 单文件 exe 与 Linux AppImage（`asset_name` 记着这次查的是哪一个）。
    """

    tag: str              # 原始 tag，如 "v0.1.2"
    version: str          # 规范化版本，如 "0.1.2"
    html_url: str         # Release 页面（「打开页面」/ 手动下载用）
    asset_url: str        # 本次要下载的那个附件的 browser_download_url
    asset_name: str = EXE_ASSET_NAME   # 附件名（决定下载物落点 `<name>.new`）
    asset_digest: str = ""  # GitHub 算好的 sha256（assets[].digest，剥掉 "sha256:" 前缀）——
                            # 校验的**首选**来源：服务端对收到的字节算的，不必再依赖我们自己传的摘要文件
    sums_url: str = ""    # SHA256SUMS.txt 的 browser_download_url（**只对 exe 有意义**的兜底：
                          # 那个文件里只有 exe 那一行，2026-09 起官方 Release 也不再上传它）
    asset_size: int | None = None   # assets[].size（字节）：进度条总量的兜底，Content-Length 优先


# 延迟创建的默认 opener（带重定向守卫；测试里整体替换这个模块级对象即可离线）
_opener = None


def _get_opener():
    """默认 opener：自动读 HTTPS_PROXY / 系统代理（GitHub 国内直连不稳），并挂重定向守卫。"""
    global _opener
    if _opener is None:
        _opener = build_opener(_GuardedRedirectHandler())
    return _opener


@contextmanager
def _mapped_errors(rate_limit: str | None = None):
    """把 urllib/网络层的杂七杂八异常统一翻成给用户看的 UpdateCheckError。

    `rate_limit`：403/429 的文案。默认那句是 GitHub 的实情（未认证 60 次/小时），只有走 GitHub
    时才对；主源（自家 CDN）撞到 403/429 是另一回事（对象/域名不对、被拦），传自己的文案进来。
    """
    try:
        yield
    except UpdateCheckError:
        raise
    except HTTPError as exc:
        if exc.code in (403, 429):
            # 未认证限流 60 次/小时/IP；手动按钮被连点时最容易撞
            raise UpdateCheckError(rate_limit or t("GitHub 限流（未认证 60 次/小时），稍后再试")) from exc
        raise UpdateCheckError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise UpdateCheckError(t("网络不可达：{msg}", msg=exc.reason)) from exc
    except TimeoutError as exc:
        raise UpdateCheckError(t("网络不可达：连接超时")) from exc
    except Exception as exc:  # noqa: BLE001
        if _is_download_cancel(exc):
            # 用户取消**不是错误**：原样抛出去，让 GUI 走它自己的取消分支
            # （以前这里会把它包成 UpdateCheckError —— 于是「取消」被当成失败：
            #  日志记的是失败，本版还会因此触发一次 37 MB 的备用源重试。实测逮到过。）
            raise
        raise UpdateCheckError(f"{type(exc).__name__}: {exc}") from exc


def _is_allowed_url(url: str) -> bool:
    """URL 是否通过「https + host 白名单（+ 端口）」这道门（不抛异常，只回答是/否）。"""
    try:
        _check_url(url)
    except UpdateCheckError:
        return False
    return True


def parse_manifest(text: str, asset_name: str = EXE_ASSET_NAME) -> ReleaseInfo:
    """把 CF 清单（`latest.json`）解析成 ReleaseInfo；不合契约的地方一律 UpdateCheckError。

    清单是**发布侧与客户端之间唯一的契约**（生成方 = `scripts/publish_r2.py`）。这里严格校验的
    理由：这里放过的每一个「奇怪但能用」的形态，最后都会变成用户机器上一次装了一半的更新 ——
    宁可不更新（让调用方回落 GitHub），也不猜。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UpdateCheckError(t("响应不是合法 JSON：{msg}", msg=exc)) from exc
    if not isinstance(data, dict):
        raise UpdateCheckError("清单顶层不是对象")
    schema = data.get("schema")
    if schema != MANIFEST_SCHEMA:
        # 出现不认识的格式版本：不猜、不兼容处理，直接回落 GitHub（将来升级格式时老客户端有救）
        raise UpdateCheckError(f"清单的格式版本不认识：{schema!r}")
    version = str(data.get("version") or "")
    if parse_version(version) is None:
        # version 会被当版本号比较，形态不对 = 永远收不到更新（或永远提示有更新）
        raise UpdateCheckError(f"清单里的版本号不是严格 X.Y.Z：{version!r}")
    assets = data.get("assets")
    entry = assets.get(asset_name) if isinstance(assets, dict) else None
    if not isinstance(entry, dict):
        # 与 GitHub 源同一句话：那一版没有本形态要的包（Windows 单文件 exe / Linux AppImage）
        raise UpdateCheckError(t("Release 附件不全：缺少 {name} 或校验值", name=asset_name))
    url = str(entry.get("url") or "")
    _check_url(url)                      # scheme + host 白名单：清单被改写/域名写错都要拦住
    digest = str(entry.get("sha256") or "").lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise UpdateCheckError(f"{asset_name} 的校验值格式不对")
    size = entry.get("size")
    html = str(data.get("release_url") or "").strip()
    if not _is_allowed_url(html):
        # 这个地址会经 open_release_page() **直接交给浏览器打开**（与产物地址同级的东西），
        # 所以和别的 URL 一样过白名单；不合规就用已知安全的 GitHub Release 页顶上（它只用于
        # 「看看这次更新了什么」）。评审 §5 指出的就是这处口径不统一。
        html = f"{RELEASES_HTML}/tag/v{version}"
    return ReleaseInfo(tag=f"v{version}", version=version, html_url=html,
                       asset_url=url, asset_name=asset_name, asset_digest=digest,
                       sums_url="", asset_size=size if isinstance(size, int) and size > 0 else None)


def fetch_latest_manifest(timeout: float = DEFAULT_TIMEOUT_S,
                          asset_name: str = EXE_ASSET_NAME) -> ReleaseInfo:
    """取主源（Cloudflare R2）的清单，返回**我要的那份产物**的描述。

    下载器只需要 `asset_url` + `asset_digest`，两者清单里都有 —— 所以主源路径下一次网络走完，
    不再像 GitHub 那条要先问 API 再 302 到 CDN。
    """
    req = Request(MANIFEST_URL, headers={
        # UA 带上版本号：R2 侧访问日志里能看出是哪一版在查（与 GitHub 那条同口径）
        "User-Agent": f"vrchat-livetranslate/{display_version()}",
    })
    with _mapped_errors(rate_limit=f"HTTP 403/429（对象不存在、被拦或限流）：{MANIFEST_HOST}"):
        with _get_opener().open(req, timeout=timeout) as resp:
            _check_url(_final_url(resp))     # 落地复检：跳转终点也必须在白名单
            body = resp.read().decode("utf-8", "replace")
    return parse_manifest(body, asset_name)


def _forced_source() -> str:
    """紧急开关的值（规范化）：`github` ⇒ 跳过主源。见 ENV_UPDATE_SOURCE 的注释。"""
    return (os.environ.get(ENV_UPDATE_SOURCE) or "").strip().lower()


def fetch_latest_release(timeout: float = DEFAULT_TIMEOUT_S,
                         asset_name: str = EXE_ASSET_NAME) -> ReleaseInfo:
    """查最新版本：**先主源（Cloudflare R2），失败回落 GitHub**。两条路都留一行 [update] 日志。

    这是 GUI 唯一该调的入口（`check_for_updates` 用的就是它）。回落是**故意**的：
    主源是自家对象存储、形态由我们控制，但「它挂了」不该等于「所有人都收不到更新」；
    而 GitHub 那条老路已经跑了十几个版本、有全套用例守着，留着当兜底几乎零成本。

    ⚠️ 只回落**主源坏了**，不回落**我们自己的代码坏了**：捕获面收窄到
    `UpdateCheckError / OSError / TimeoutError`（网络与契约问题），解析里的 TypeError /
    AttributeError 那类是 bug，必须原样冒到 `check_for_updates` 的日志里
    —— 否则它会伪装成一句「回落到 GitHub」，把 bug 藏进正常路径（评审 §4）。
    """
    if _forced_source() == "github":
        # 紧急开关：主源被判定不可信时绕开它（不留痕可不行，这行必须显眼）
        print(f"[update] {ENV_UPDATE_SOURCE}=github：按紧急开关跳过主源（{MANIFEST_HOST}）",
              flush=True)
        return fetch_latest_release_github(timeout=timeout, asset_name=asset_name)
    try:
        info = fetch_latest_manifest(timeout=timeout, asset_name=asset_name)
    except (UpdateCheckError, OSError, TimeoutError) as exc:
        print(f"[update] ⚠️ 主源（{MANIFEST_HOST}）失败：{exc} → 回落 GitHub Release", flush=True)
    else:
        print(f"[update] 更新源：Cloudflare（{MANIFEST_HOST}）v{info.version}", flush=True)
        return info
    info = fetch_latest_release_github(timeout=timeout, asset_name=asset_name)
    print(f"[update] 更新源：GitHub Release v{info.version}（主源不可用时的兜底）", flush=True)
    return info


def fetch_latest_release_github(timeout: float = DEFAULT_TIMEOUT_S,
                                asset_name: str = EXE_ASSET_NAME) -> ReleaseInfo:
    """兜底源：查 GitHub 最新 Release 里**我要的那份附件**（默认 exe；AppImage 传 APPIMAGE_ASSET_NAME）。
    /releases/latest 天然排除 prerelease 和 draft（GitHub 语义）。

    失败一律 UpdateCheckError：403/429=限流、URLError/超时=网络不可达、
    附件不全 / tag 不是版本号 = Release 本身异常（调用方留痕跳过，不崩）。
    """
    req = Request(RELEASES_LATEST_API, headers={
        "Accept": "application/vnd.github+json",
        # GitHub 对无 User-Agent 的请求直接 403
        "User-Agent": f"vrchat-livetranslate/{display_version()}",
    })
    with _mapped_errors():
        with _get_opener().open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise UpdateCheckError(t("响应不是合法 JSON：{msg}", msg=exc)) from exc
    tag = str(data.get("tag_name") or "")
    ver = parse_version(tag)
    if ver is None:
        # tag 与 __version__ 的一致性由 release.yml 对账保证；走到这说明 Release 本身异常
        raise UpdateCheckError(t("最新 Release 的 tag 不是版本号：{tag}", tag=f"{tag!r}"))
    asset_url = asset_digest = sums_url = ""
    asset_size: int | None = None
    for a in data.get("assets") or []:
        if a.get("name") == asset_name:
            asset_url = str(a.get("browser_download_url") or "")
            size = a.get("size")
            asset_size = size if isinstance(size, int) and size > 0 else None
            # GitHub 为每个附件算好并在 API 里返回 digest（形如 "sha256:abcd…"）。
            # 用它做校验就不必再自己上传 SHA256SUMS.txt（2026-09 已停止上传）。
            digest = str(a.get("digest") or "").lower()
            if digest.startswith("sha256:"):
                asset_digest = digest.split(":", 1)[1]
        elif a.get("name") == SUMS_ASSET_NAME:
            sums_url = str(a.get("browser_download_url") or "")
    if not asset_url or (not asset_digest and not (sums_url and asset_name == EXE_ASSET_NAME)):
        # 附件不在 / 没有可用校验来源 → 宁可不更新，也不装一个没法验的包（调用方按「检查失败」留痕）
        # ⚠️ SHA256SUMS.txt 只兜底 exe：那个文件里只有 exe 一行，AppImage 必须自带 digest。
        raise UpdateCheckError(t("Release 附件不全：缺少 {name} 或校验值", name=asset_name))
    return ReleaseInfo(tag=tag, version=f"{ver[0]}.{ver[1]}.{ver[2]}",
                       html_url=str(data.get("html_url") or ""),
                       asset_url=asset_url, asset_name=asset_name,
                       asset_digest=asset_digest,
                       sums_url=sums_url, asset_size=asset_size)


# ---------------------------------------------------------------- 忽略列表读写
#
# 就地改写保注释的三个工具函数（_yaml_set_in_text / _write_config_text / _fmt_scalar）
# 住在 vlt/config_io.py —— gui.py 与本模块的单一真相，别在这儿再抄一份。


def load_ignored_versions(config_path: Path) -> list[str]:
    """读 config.yaml 的 ui.update_ignored；文件不存在/段缺失/值非法 → 空列表（不报错）。

    读取走 yaml.safe_load（只读不破坏）；写入才需要上面的就地改写保注释。
    """
    try:
        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 坏 YAML / 文件不存在 / 编码问题都按「没忽略过」处理
        return []
    ui = data.get("ui") if isinstance(data, dict) else None
    val = ui.get("update_ignored") if isinstance(ui, dict) else None
    if not isinstance(val, list):
        return []
    return [str(v) for v in val]


def add_ignored_version(config_path: Path, version: str) -> None:
    """把版本加进 ui.update_ignored（规范化、不带 v、不重复），就地改写保注释与键序。

    ui 段不存在时补建 —— 老 config.yaml 没有 ui 段（与 gui._save_ui_state 同一套路）。
    """
    ver = parse_version(version)
    if ver is None:
        raise UpdateCheckError(t("不是合法版本号，无法写入忽略列表：{version}",
                                 version=f"{version!r}"))
    version = f"{ver[0]}.{ver[1]}.{ver[2]}"
    ignored = load_ignored_versions(config_path)
    if version in ignored:
        return
    ignored.append(version)

    config_path = Path(config_path)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if text:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            # 原文件已经坏了就别再往上写：写一半好不了，还可能把坏文件「合法化」成更怪的形态
            raise UpdateCheckError(t("config.yaml 不是合法 YAML，已放弃写入：{msg}",
                                     msg=exc)) from exc
    if not re.search(r"(?m)^ui:", text):
        block = "# 点过「不再提示这个版本」的记录（不用手改）\nui:\n"
        text = text.rstrip("\n") + ("\n\n" + block if text.strip() else block)
    text = _yaml_set_in_text(text, ["ui", "update_ignored"], _fmt_scalar(ignored))
    _write_config_text(config_path, text)


# ---------------------------------------------------------------- 决策 + 一次性检查入口


def should_prompt(remote: ReleaseInfo, local_version: str, ignored: list[str]) -> bool:
    """更高版本 且 不在忽略列表 → True。remote.version 已在 fetch 里规范化。"""
    if remote.version in ignored:
        return False
    return is_newer(remote.version, local_version)


def check_for_updates(local_version: str, config_path: Path,
                      timeout: float = DEFAULT_TIMEOUT_S) -> tuple[str, ReleaseInfo | None]:
    """GUI 后台线程调的唯一入口。返回 (status, info)：
    status ∈ {"update", "latest", "ignored", "error"}；error 时 info=None。

    硬要求：每个分支都留一行 [update] 日志（成功/跳过/失败各一行），绝不静默 ——
    检查失败对用户完全无感（不弹窗），但日志里必须查得到。
    """
    print("[update] 检查中…", flush=True)
    try:
        # 要哪份附件按**当前运行形态**定（Windows 单文件 exe / Linux AppImage）——
        # 拿错附件的话后面下载校验都对不上，所以选附件这一步只在这里做一次。
        info = fetch_latest_release(timeout=timeout, asset_name=asset_name_for())
    except Exception as exc:  # noqa: BLE001 — 失败只留痕，绝不把异常甩给启动流程
        print(f"[update] 检查失败：{exc}", flush=True)
        return "error", None
    ignored = load_ignored_versions(config_path)
    if not should_prompt(info, local_version, ignored):
        if info.version in ignored:
            print(f"[update] v{info.version} 在忽略列表，跳过", flush=True)
            return "ignored", info
        print(f"[update] 已是最新 v{info.version}", flush=True)
        return "latest", info
    print(f"[update] 发现新版本 v{info.version}（当前 v{local_version}）", flush=True)
    return "update", info


# ---------------------------------------------------------------- 下载 + SHA256 校验 + 域白名单


def _check_url(url: str) -> None:
    """https + 白名单 host（+ 非默认端口）才放行；其余一律 UpdateCheckError
    （重定向守卫与落地复检共用）。"""
    parts = urlsplit(url or "")
    if parts.scheme != "https":
        raise UpdateCheckError(t("拒绝非 https 地址：{url}", url=url))
    if parts.hostname not in ALLOWED_HOSTS:
        raise UpdateCheckError(t("拒绝白名单外的地址：{url}", url=url))
    try:
        port = parts.port
    except ValueError:              # 畸形端口（https://host:abc/）→ 不当成合法地址
        port = -1
    if port not in (None, 443):
        # 白名单是「域名 + 默认端口」：`dl.kcm-nixi.cn:8443` 不是我们发布东西的地方
        # （评审 §三 指出的口径不一致）。复用同一句话术，不新增词表 key。
        raise UpdateCheckError(t("拒绝白名单外的地址：{url}", url=url))


class _GuardedRedirectHandler(HTTPRedirectHandler):
    """每次重定向前校验目标：scheme 必须 https 且 host ∈ ALLOWED_HOSTS，否则 UpdateCheckError。

    为什么需要它：exe 下载会 302 到 CDN，urllib 默认无脑跟随跳转 ——
    元数据被篡改或代理被劫持时，默认行为会把恶意地址也照下不误。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _final_url(resp) -> str:  # noqa: ANN001
    """响应最终落地的 URL（真响应走 geturl()，测试替身只有 .url）。"""
    geturl = getattr(resp, "geturl", None)
    if callable(geturl):
        return geturl()
    return str(getattr(resp, "url", "") or "")


def _content_length(resp) -> int | None:  # noqa: ANN001
    """响应头里的 Content-Length；没有/不是数字 → None（进度条降级显示，绝不除零）。"""
    headers = getattr(resp, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _expected_sha256(info: ReleaseInfo, timeout: float) -> str:
    """这次下载该期望什么 sha256。

    首选 GitHub 在 API 里给的 `assets[].digest`（服务端对上传字节算的，最省事也最独立）；
    只有当老 Release 没带 digest 时，才回去拉 SHA256SUMS.txt 兜底（只对 exe 有意义）。
    """
    if info.asset_digest:
        return info.asset_digest
    return _download_expected_sha256(info.sums_url, timeout)


def _download_expected_sha256(sums_url: str, timeout: float) -> str:
    """拉 SHA256SUMS.txt，取 VRChatLiveTranslate.exe 那一行的 hash（小文件，直接读进内存）。"""
    req = Request(sums_url, headers={"User-Agent": f"vrchat-livetranslate/{display_version()}"})
    with _mapped_errors():
        with _get_opener().open(req, timeout=timeout) as resp:
            _check_url(_final_url(resp))        # 落地复检：跳转终点也必须在白名单
            text = resp.read().decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == EXE_ASSET_NAME:
            h = parts[0].lower()
            if len(h) == 64 and all(c in "0123456789abcdef" for c in h):
                return h
            raise UpdateCheckError(t("{sums} 里 {exe} 的校验值格式不对",
                                     sums=SUMS_ASSET_NAME, exe=EXE_ASSET_NAME))
    raise UpdateCheckError(t("{sums} 里没有 {exe} 的校验值",
                             sums=SUMS_ASSET_NAME, exe=EXE_ASSET_NAME))


def _stream_to_file(url: str, dest: Path, timeout: float,
                    progress: Callable[[int, int | None], None] | None) -> str:
    """边下边算 sha256，每块调一次 progress(done, total)。返回 hex digest。"""
    req = Request(url, headers={"User-Agent": f"vrchat-livetranslate/{display_version()}"})
    h = hashlib.sha256()
    with _mapped_errors():
        with _get_opener().open(req, timeout=timeout) as resp:
            _check_url(_final_url(resp))        # 落地复检
            total = _content_length(resp)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    return h.hexdigest()


def github_fallback(info: ReleaseInfo, timeout: float = DEFAULT_TIMEOUT_S) -> ReleaseInfo:
    """主源上的产物下载失败时的备用取件：**同一版本**在 GitHub Release 上的那份附件。

    为什么值得多这一跳（评审 §3）：大陆最常见的失败形态不是「清单拿不到」（843 B，秒级），
    而是「清单拿得到、37 MB 产物下到一半断了」—— 那时重试同一个 CF 地址往往还是断，
    而 GitHub 附件走的是另一条 CDN 通路（未必更快，但实测可用）。

    只接受**版本号一致**的回落：对不上宁可失败，也绝不跨版本装（那才是最坏的失败）。
    """
    alt = fetch_latest_release_github(timeout=timeout, asset_name=info.asset_name)
    if alt.version != info.version:
        raise UpdateCheckError(t("备用源上的版本是 v{got}，与要装的 v{want} 不同：已放弃回退",
                                 got=alt.version, want=info.version))
    print(f"[update] 备用源（GitHub）上有同一版 v{alt.version} 的 {alt.asset_name}，准备重试",
          flush=True)
    return alt


#: 出现过「摘要不符」的版本号（进程内记着）。用途只有一个（评审三审 R3）：给这类失败留一条
#: **用户可达**的出口 —— 第一次摘要不符**不**自动换源（换源救不了「源头对象就是错的」），
#: 但用户在同一版本上再点一次「重试」时就**直接走备用源**（能救「中间缓存/传输把它改坏了」）。
#: 不做持久化：重启程序后这个版本会重新走主源一次，那本来就是合理的默认。
_MISMATCH_SEEN: set[str] = set()


def _is_download_cancel(exc: BaseException) -> bool:
    """是不是「用户取消了下载」——`vlt/ui_text.py::_DownloadCancelled` 由 GUI 的 progress 回调抛出。

    ⚠️ 本模块不许 import ui_text（要能脱离 Tk 单测），所以**按类名判**：那个名字是它与本模块
    之间的契约，`tests/test_update_check.py` 会钉住它。判错的后果是「用户点了关闭，程序却
    又去 GitHub 下一遍 37 MB」（真机测试逮到过）。

    也看 `__cause__` / `__context__`：别处（如 `_mapped_errors`）万一把它包了一层，
    链上还能认出来。
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if any(cls.__name__ == "_DownloadCancelled" for cls in type(cur).__mro__):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def download_and_verify(info: ReleaseInfo, dest_dir: Path,
                        timeout: float = DOWNLOAD_TIMEOUT_S,
                        progress: Callable[[int, int | None], None] | None = None,
                        fallback: Callable[[], ReleaseInfo] | None = None) -> Path:
    """下载本次要换的那个附件（exe / AppImage，由 `info.asset_name` 定）到 dest_dir。

    调用方保证 dest_dir = **安装文件同目录**（同卷 rename 才近原子）：Windows 是 exe 旁边，
    Linux 是那个 `.AppImage` 旁边。

    校验：期望值取清单 / Release 里的 sha256（老 GitHub Release 回落 SHA256SUMS.txt 的 exe 行），
    与实测 sha256 不一致/解析不出 → 删除已下载文件 + UpdateCheckError，绝不进入替换步骤。
    progress(done, total) 在下载线程里被调，total 取 Content-Length（缺省 None）——
    本模块不碰 Tk，GUI 负责把回调转进队列。

    `fallback`：这个附件在**主源上下不动时的第二次机会**（GUI 传 `lambda: github_fallback(info)`）。
    只在「下载/校验」失败时用一次；**用户取消不算失败**（点了关闭就别再发起第二次传输）；
    **摘要不符也不算"网络失败"**（`ChecksumMismatch` → 不重试，见该类 docstring）；
    它自己抛的异常原样上抛，但会把主源那次的原因串进异常链 + 日志（否则最后一行只剩备用源的错）。
    返回校验通过的文件路径（<dest_dir>/<asset_name>.new）。

    ⚠️ 超时口径：这里的 `timeout` 是**每次 socket 操作的超时**，不是整条链的总时限 ——
    一个「慢但在推进」的连接可以合法地跑很久（大陆到 CF 实测过 100~700 KB/s 的波动，
    硬加总时限会把这种用户直接卡死在下不动）。真正的兜底是：一次失败后**只**重试一次，
    且摘要不符不重试（那类问题换源修不好）。
    """
    dest = Path(dest_dir) / (info.asset_name + NEW_SUFFIX)

    def _once(src: ReleaseInfo) -> Path:
        """一次完整的「下载 + 校验 + 落盘」；失败时清残留并原样上抛。"""
        try:
            expected = _expected_sha256(src, timeout)
            actual = _stream_to_file(src.asset_url, dest, timeout, progress)
        except BaseException as exc:  # noqa: BLE001 — 任何失败（含 Ctrl+C / 取消）都要清残留 + 留痕
            dest.unlink(missing_ok=True)
            if _is_download_cancel(exc):
                # 取消不是失败，别和网络错误混在一行（评审二审 §4：日志里要能一眼分清）
                print("[update] 下载已取消（残留已清理）", flush=True)
            else:
                print(f"[update] 下载失败：{exc}（残留已清理）", flush=True)
            raise
        if actual != expected:
            dest.unlink(missing_ok=True)
            print("[update] 校验失败已删除", flush=True)
            raise ChecksumMismatch(t("下载的文件与发布页的摘要对不上，已删除（请重试）"))
        print(f"[update] 下载完成 sha256 校验通过（v{src.version}）", flush=True)
        try:
            write_pending(Path(dest_dir), src.version, actual)
        except OSError as exc:
            # json 写不进去不挡本次更新（【重载】不依赖它）；但【稍后】/残留恢复会因此走不通，
            # 必须留痕 —— 禁静默降级
            print(f"[update] ⚠️ 待更新信息没写下来（{exc}）：「稍后更新」退出时将不会自动替换",
                  flush=True)
        return dest

    # 走备用源之前先把「这次是从主源起步的」这件事定下来：上一次同一版本是摘要不符时
    # （用户又点了一次重试），直接跳过主源走备用源 —— 见 `_MISMATCH_SEEN` 的说明。
    _from_fallback = fallback is not None and info.version in _MISMATCH_SEEN
    primary: BaseException | None = None
    if _from_fallback:
        print(f"[update] ⚠️ v{info.version} 上一次是摘要不符 → 这次直接走备用源（用户重试）",
              flush=True)
    try:
        if not _from_fallback:
            return _once(info)
    except Exception as exc:
        # 只接 Exception：取消（`ui_text._DownloadCancelled`）**是** Exception 的子类，靠
        # `_is_download_cancel` 认它；而 KeyboardInterrupt / SystemExit / asyncio.CancelledError
        # 这类非 Exception 的 BaseException 不该被当成「网络失败」去白跑一次 37MB 兜底
        # （评审三审 R1：原先写 `except BaseException` + 手动排除三个，行为等价但理由写反了）。
        if fallback is None or _is_download_cancel(exc):
            raise
        if isinstance(exc, ChecksumMismatch):
            # 摘要不符**本次不自动换源**（评审二审 §3，理由见 ChecksumMismatch docstring），
            # 但要留一条**用户可达**的出口（评审三审 R3）：记下这个版本，用户再点一次「重试」
            # 就直接走备用源 —— 换源确实救不了「源头对象就是错的」，但**能救**「中间缓存/传输
            # 把它改坏了」，而后者是更常见的形态；每次点重试都白下一遍 37MB 才是要避免的。
            _MISMATCH_SEEN.add(info.version)
            print("[update] ⚠️ 摘要不符：本次不自动换源；再点一次「重试」会直接走备用源"
                  "（换源救不了源头就错的对象，但能救中间缓存/传输改坏的）", flush=True)
            raise
        primary = exc
        print(f"[update] ⚠️ 主源下载失败（{exc}）→ 换备用源重试一次", flush=True)
    try:
        return _once(fallback())
    except Exception as exc:  # noqa: BLE001 — 备用源也失败：把两因都留下，再原样上抛
        if not isinstance(exc, UpdateCheckError):
            raise
        if primary is None:
            raise                       # 这一轮是从备用源起步的（用户重试那个分支），没有主因可串
        # 只报备用源那个错会把真正的原因埋掉（大陆场景下备用源经常正是那个 403 限流），
        # 所以：日志里先主因后备用源，异常链 `from primary` 也串上（评审二审 §1）。
        print(f"[update] ⚠️ 备用源也失败：{exc}（主因仍是主源那次：{primary}）", flush=True)
        raise type(exc)(str(exc)) from primary



# ---------------------------------------------------------------- 运行形态判定


def update_mode() -> str:
    """'frozen' | 'appimage' | 'source' —— 决定「立即更新」怎么换、甚至能不能换。

    - `frozen`  ：**Windows** 单文件 exe（PyInstaller）→ 换 exe 自己（换不了就靠更新器 bat）；
    - `appimage`：Linux AppImage（`$APPIMAGE` 指向那个文件）→ 换那个 `.AppImage`；
    - `source`  ：仓库里直接跑（`run_gui.sh`）、`--appimage-extract-and-run`、或
      **Linux 的 PyInstaller 目录产物**（没有单文件可换）→ 只给指引，绝不动任何东西。

    ⚠️ AppImage 必须单独一类，别并进 source：它的**源码目录**确实是只读挂载（不能改），
    但 AppImage **文件本身**通常可写，而且 Linux 允许「运行中的可执行文件被 rename 顶替」
    （旧 inode 继续活着，见 `install_appimage()`）—— 所以它和 exe 一样能自更新。
    以前这里只有 frozen/source，AppImage 用户会被当成源码：界面弹「在仓库目录跑 git pull」，
    而 AppImage 用户根本没有仓库。

    ⚠️ 判定顺序：**先 APPIMAGE 再看 frozen**。现在 Linux 侧的 AppImage 是「PyInstaller
    打包的产物再包一层 AppImage」——进程里 `sys.frozen=True` 且 `$APPIMAGE` 有值，
    如果先判 frozen 会被当成 Windows 形态：查更新**去下 Windows 的 exe**，
    替换目标还指向只读挂载里的可执行文件（实测踩过，见 tests 的对应用例）。
    """
    if appimage_path() is not None:
        return "appimage"
    if is_frozen():
        # Linux 的 frozen 目录形态（onedir 直接运行）：没有可替换的单文件，
        # 归 source（只给指引）；只有 Windows 单文件 exe 才是真正的 frozen。
        return "frozen" if sys.platform == "win32" else "source"
    return "source"


def appimage_path() -> Path | None:
    """当前运行的 AppImage 文件（`$APPIMAGE`）；没这变量 / 文件已经不在了 → None。

    只认 `APPIMAGE`：那是 AppImage 运行时自己设的、指向那个 `.AppImage` 文件。
    `APPDIR` 是挂载点（`--appimage-extract-and-run` 下也有），但那种形态没有可替换的
    单文件，所以不能拿它当「能自更新」的依据。
    """
    raw = (os.environ.get("APPIMAGE") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    try:
        return p if p.is_file() else None
    except OSError:          # 路径里有非法字符 / 权限问题：当没有，别让检查流程崩
        return None


def can_self_update() -> bool:
    """当前形态能不能**就地换掉自己**（Windows 单文件 exe / Linux AppImage）。

    界面所有「自更新」相关的入口（退出时替换、启动残留恢复、版本变化提示）都该按它判，
    而不是各写一遍 `update_mode() == "frozen"` —— 后者就是 AppImage 被漏掉的原因。
    """
    return update_mode() in ("frozen", "appimage")


def asset_name_for(mode: str | None = None) -> str:
    """这次更新该下载哪个附件名（按运行形态定）。"""
    return APPIMAGE_ASSET_NAME if (mode or update_mode()) == "appimage" else EXE_ASSET_NAME


def update_target_path() -> Path | None:
    """当前形态「该被换掉的那个文件」：frozen → exe 自己；appimage → 那个 AppImage。

    ⚠️ 别用 `sys.executable` 代替它：AppImage 下那是挂载点里的
    `usr/python/bin/python3.11`，换它既没意义（重挂载就没了）也做不到（只读挂载）。
    """
    mode = update_mode()
    if mode == "frozen":
        return Path(sys.executable).resolve()
    if mode == "appimage":
        return appimage_path()
    return None


def install_appimage(new_path: Path, current_path: Path) -> None:
    """把下载好的新版 AppImage 就地顶替旧版（**运行中也能做**）。

    为什么这里不需要 Windows 那套「等 PID 退出的更新器」：Linux 的 `os.replace` 是原子的，
    只换目录项 —— 运行中的进程和它的 squashfs 挂载仍指向**旧 inode**，继续正常工作，
    新进程打开的就是新文件。所以没有「父进程还攥着文件句柄」这类窗口期，也就不需要外部脚本。

    没有留 .bak：`os.replace` 要么换成、要么异常（旧文件原样不动），不存在「换了一半」的
    状态，而 Windows 那边留 .bak 正是为了兜住 `move /y` 的失败重试。新版文件本身坏了的话，
    回下载页拿上一版即可（发布产物都带 GitHub 服务端的 digest，可以自己核）。
    """
    new_path, current_path = Path(new_path), Path(current_path)
    try:
        new_path.chmod(0o755)          # 下载来的文件没有可执行位，顶替过去必须补上
        os.replace(new_path, current_path)
    except OSError as exc:
        raise UpdateCheckError(t("替换 AppImage 失败：{msg}", msg=exc)) from exc
    print(f"[update] AppImage 已就地替换：{current_path}（下次启动就是新版）", flush=True)


# ---------------------------------------------------------------- 更新器 bat 生成


def build_updater_bat(*, pid: int, current_exe: Path, new_exe: Path,
                      relaunch: bool = True) -> str:
    """生成更新器批处理文本（CRLF 行尾！cmd 对 LF-only 的 label/goto 会抽风）。

    逻辑：等 PID 退出（最多 ~60s）→ 备份 current 为 .bak → move .new 顶替（**带重试**，
    失败则用 .bak 还原）→（relaunch=True 才）start 拉起新版 → 自删；失败留一行到 exe 旁的
    update_failed.log 并 pause（用户能看到窗口，不会无声消失）。
    relaunch=False 用于【稍后】退出时替换：只换不拉，用户下次自己打开就是新版 ——
    与 True 形态的唯一差别就是没有 start "" 那一行，其余逐字节相同。
    所有路径双引号包裹（桌面/用户名常含空格）；不写注册表、不要管理员。
    bat 正文只用 ASCII：cmd 按系统 OEM 代码页解析，塞中文注释在代码页不符时会变乱码。

    ⚠️ **为什么 move 要重试**（真机实测，2026-09）：PyInstaller 单文件 exe 是「父进程
    bootloader + 子进程跑 Python」两级结构，我们等的是**自己那个 PID（子进程）**，
    而 `VRChatLiveTranslate.exe` 的文件句柄握在**父进程**手里、它会晚一步退出。
    子进程一消失就立刻 `move /y` 会撞上共享冲突 → 替换失败 → 回滚 + update_failed.log：
    用户看到的是「点了立即更新、程序重启了、还是旧版本」。重试 30 次（每次约 1s）覆盖这个窗口。
    """
    cur = str(current_exe)
    new = str(new_exe)
    bak = cur + ".bak"
    log = str(Path(current_exe).parent / "update_failed.log")
    lines = [
        "@echo off",
        "setlocal",
        f"rem wait for main program (PID {pid}) to exit, up to ~60s",
        "set /a TRIES=0",
        ":wait",
        f'tasklist /fi "PID eq {pid}" | find "{pid}" >nul',
        "if errorlevel 1 goto replace",
        "set /a TRIES+=1",
        "if %TRIES% geq 60 goto fail",
        "ping 127.0.0.1 -n 2 >nul",
        "goto wait",
        "",
        ":replace",
        f'copy /y "{cur}" "{bak}" >nul',
        "if errorlevel 1 goto fail",
        "rem the parent bootloader may still hold the exe (see docstring): retry the move",
        "set /a MTRIES=0",
        ":trymove",
        f'move /y "{new}" "{cur}" >nul 2>&1',
        "if not errorlevel 1 goto start",
        "set /a MTRIES+=1",
        "if %MTRIES% geq 30 goto restore",
        "ping 127.0.0.1 -n 2 >nul",
        "goto trymove",
        "",
        ":restore",
        f'copy /y "{bak}" "{cur}" >nul',
        "goto fail",
        "",
        ":start",
    ]
    if relaunch:
        # 新实例必须是「干净环境」启动：PyInstaller 的内部变量会让它以为自己是已被解包的
        # 子进程而跳过自解包 → 起不来（不写日志、无窗口）。详见 vlt/gui.updater_env()。
        lines += [
            'rem drop PyInstaller internal vars so the new instance extracts itself',
            'set "_MEIPASS="',
            'set "_MEIPASS2="',
            'set "_PYI_ARCHIVE_FILE="',
            'set "_PYI_PARENT_PROCESS_LEVEL="',
            'set "_PYI_APPLICATION_HOME_DIR="',
            'set "_PYI_SPLASH_IPC="',
        ]
        lines.append(f'start "" "{cur}"')
    lines += [
        'del "%~f0"',
        "exit /b 0",
        "",
        ":fail",
        f'echo %date% %time% update failed >> "{log}"',
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------- 待更新状态与版本记录


def file_sha256(path: Path) -> str:
    """流式算文件的 sha256（hex）—— 残留 .new 复验用，别把 30MB+ 一次读进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def pending_new_asset(asset_path: Path) -> Path:
    """安装文件旁待替换的新版位置（与 download_and_verify 的落地规则同一个后缀）。

    exe → `VRChatLiveTranslate.exe.new`；AppImage → `VRChatLiveTranslate-x86_64.AppImage.new`。
    从路径本身推名字（而不是拼 `EXE_ASSET_NAME`）：两种产物共用一套残留复核逻辑。
    """
    p = Path(asset_path)
    return p.parent / (p.name + NEW_SUFFIX)


def write_pending(dest_dir: Path, version: str, sha256: str) -> Path:
    """下载校验通过后写 update_pending.json（供【稍后更新】与异常恢复的复验凭据）。"""
    p = Path(dest_dir) / PENDING_JSON
    p.write_text(json.dumps({"version": version, "sha256": sha256},
                            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def check_pending_download(asset_path: Path,
                           current_version: str = "") -> tuple[str, str] | None:
    """残留 .new 的安全网：用之前必须重新校验，损坏/半截绝不放行。

    安装文件（exe / AppImage）旁存在 `<安装文件名>.new` 且 update_pending.json 完好 →
    重新计算 .new 的 SHA256 与 json 比对：一致 → 返回 (version, sha256)；
    不一致 / json 损坏缺字段 / .new 缺失 / 版本不比 current_version 新（换过的残留）→
    清理残留（.new 与 json 都删）+ 逐分支 [update] 留痕，返回 None。
    完全没有残留 → 静默返回 None（每次启动都会走这条路，不刷日志）。
    """
    asset_path = Path(asset_path)
    new_exe = pending_new_asset(asset_path)
    pending = asset_path.parent / PENDING_JSON

    def _cleanup(reason: str) -> None:
        new_exe.unlink(missing_ok=True)
        pending.unlink(missing_ok=True)
        print(f"[update] 待更新文件{reason}：残留已清理，按正常检查流程走", flush=True)

    has_new, has_json = new_exe.exists(), pending.exists()
    if not has_new and not has_json:
        return None
    if not has_new:
        _cleanup("只剩记录文件、下载物缺失")
        return None
    if not has_json:
        _cleanup("缺少记录文件，无法确认完整性")
        return None
    try:
        meta = json.loads(pending.read_text(encoding="utf-8"))
        version, expected = str(meta["version"]), str(meta["sha256"]).lower()
        if parse_version(version) is None or len(expected) != 64:
            raise ValueError("字段不合法")
    except Exception:
        _cleanup("记录文件损坏")
        return None
    if current_version and not is_newer(version, current_version):
        # ⚠️ is_newer() 在**任一边版本号不合法**时也返回 False（它的注释写着「防御：绝不崩」），
        #    但「没法比」和「不比当前新」是两回事 —— 把前者当成残留，就会把一个校验完好的
        #    更新包直接删掉。真机触发条件：当前版本是 0.4.0-beta.1 这类非严格 X.Y.Z
        #    （测试版/预发布版），于是每次退出都把下载好的更新当垃圾清理。
        if parse_version(current_version) is None:
            print(f"[update] 当前版本 v{current_version} 不是严格 X.Y.Z，与待更新文件 "
                  f"v{version} 无法比较新旧：保留待复核，不按残留清理", flush=True)
        else:
            # 待替换版本不新于当前版本 = 上次其实换成功了、只是残留没清掉
            _cleanup(f"（v{version}）不比当前 v{current_version} 新，是换完剩下的")
            return None
    actual = file_sha256(new_exe)
    if actual != expected:
        _cleanup(f"完整性复核不通过（期望 {expected[:12]}… 实测 {actual[:12]}…）")
        return None
    print(f"[update] 检测到已下载好的新版本 v{version}，完整性复核通过（不重复下载）",
          flush=True)
    return version, expected


def load_last_seen_version(app_dir: Path) -> str | None:
    """上次运行记录的版本号；文件缺失/损坏 → None（当作首次运行看待，不报错）。"""
    try:
        data = json.loads((Path(app_dir) / STATE_JSON).read_text(encoding="utf-8"))
        v = data.get("last_seen_version") if isinstance(data, dict) else None
        return str(v) if isinstance(v, str) and v else None
    except Exception:  # noqa: BLE001 — 坏 JSON / 文件不存在 / 缺字段都按「首次」处理
        return None


def save_last_seen_version(app_dir: Path, version: str) -> None:
    """写回本次运行版本（版本变化提示「同一版本只弹一次」的凭据）。失败只留痕。"""
    try:
        (Path(app_dir) / STATE_JSON).write_text(
            json.dumps({"last_seen_version": version}, ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError as exc:
        print(f"[update] ⚠️ 版本提示状态写不进去：{exc}", flush=True)
