"""检查更新：纯逻辑全部在这个模块，HTTP 层可注入，GUI 只负责接线。

设计约束（写实现时别破坏）：
- 不做任何 Tk import；所有函数可离线测试。
- 任何失败都抛 UpdateCheckError（消息给用户看），绝不抛裸异常给调用方。
- 网络访问走默认 opener（尊重 HTTPS_PROXY / 系统代理）——GitHub 国内直连不稳，
  与 textin.py 显式禁代理（国内端点）正好相反，别抄错。
- 每个结论都留一行 [update] 日志（成功/跳过/失败各一行），不许静默降级。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml

from . import __version__
from .config_io import _fmt_scalar, _write_config_text, _yaml_set_in_text
from .i18n import t
from .paths import is_frozen

RELEASES_LATEST_API = "https://api.github.com/repos/nixi-agent/vrchat-livetranslate/releases/latest"
# Release 页面基址（与上面的 API 同仓库）：拼 tag 页 = f"{RELEASES_HTML}/tag/vX.Y.Z"
RELEASES_HTML = "https://github.com/nixi-agent/vrchat-livetranslate/releases"
DEFAULT_TIMEOUT_S = 10.0
DOWNLOAD_TIMEOUT_S = 120.0
EXE_ASSET_NAME = "VRChatLiveTranslate.exe"
SUMS_ASSET_NAME = "SHA256SUMS.txt"
# exe 同目录：{"version": "...", "sha256": "..."}（【稍后】与异常恢复的凭据）
PENDING_JSON = "update_pending.json"
# APP_DIR：{"last_seen_version": "..."}（版本变化一次性提示用）
STATE_JSON = "update_state.json"

# 只允许这几个 host：查最新（api.github.com）、asset 跳转起点（github.com）、
# asset 实际下载（两个 CDN 域）。重定向目标与最终落地地址都必须过这道白名单。
ALLOWED_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com"}

_CHUNK = 256 * 1024


class UpdateCheckError(RuntimeError):
    """检查/下载更新失败。消息直接给用户看，不带堆栈。"""


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
    """一次「最新 Release」查询的结果。"""

    tag: str              # 原始 tag，如 "v0.1.2"
    version: str          # 规范化版本，如 "0.1.2"
    html_url: str         # Release 页面（「打开页面」/ 手动下载用）
    exe_url: str          # VRChatLiveTranslate.exe 的 browser_download_url
    sums_url: str         # SHA256SUMS.txt 的 browser_download_url
    exe_size: int | None = None   # assets[].size（字节）：进度条总量的兜底，Content-Length 优先


# 延迟创建的默认 opener（带重定向守卫；测试里整体替换这个模块级对象即可离线）
_opener = None


def _get_opener():
    """默认 opener：自动读 HTTPS_PROXY / 系统代理（GitHub 国内直连不稳），并挂重定向守卫。"""
    global _opener
    if _opener is None:
        _opener = build_opener(_GuardedRedirectHandler())
    return _opener


@contextmanager
def _mapped_errors():
    """把 urllib/网络层的杂七杂八异常统一翻成给用户看的 UpdateCheckError。"""
    try:
        yield
    except UpdateCheckError:
        raise
    except HTTPError as exc:
        if exc.code in (403, 429):
            # 未认证限流 60 次/小时/IP；手动按钮被连点时最容易撞
            raise UpdateCheckError(t("GitHub 限流（未认证 60 次/小时），稍后再试")) from exc
        raise UpdateCheckError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise UpdateCheckError(t("网络不可达：{msg}", msg=exc.reason)) from exc
    except TimeoutError as exc:
        raise UpdateCheckError(t("网络不可达：连接超时")) from exc
    except Exception as exc:  # noqa: BLE001
        raise UpdateCheckError(f"{type(exc).__name__}: {exc}") from exc


def fetch_latest_release(timeout: float = DEFAULT_TIMEOUT_S) -> ReleaseInfo:
    """查 GitHub 最新 Release。/releases/latest 天然排除 prerelease 和 draft（GitHub 语义）。

    失败一律 UpdateCheckError：403/429=限流、URLError/超时=网络不可达、
    附件不全 / tag 不是版本号 = Release 本身异常（调用方留痕跳过，不崩）。
    """
    req = Request(RELEASES_LATEST_API, headers={
        "Accept": "application/vnd.github+json",
        # GitHub 对无 User-Agent 的请求直接 403
        "User-Agent": f"vrchat-livetranslate/{__version__}",
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
    exe_url = sums_url = ""
    exe_size: int | None = None
    for a in data.get("assets") or []:
        if a.get("name") == EXE_ASSET_NAME:
            exe_url = str(a.get("browser_download_url") or "")
            size = a.get("size")
            exe_size = size if isinstance(size, int) and size > 0 else None
        elif a.get("name") == SUMS_ASSET_NAME:
            sums_url = str(a.get("browser_download_url") or "")
    if not exe_url or not sums_url:
        raise UpdateCheckError(t("Release 附件不全：需要 {exe} 和 {sums}",
                                 exe=EXE_ASSET_NAME, sums=SUMS_ASSET_NAME))
    return ReleaseInfo(tag=tag, version=f"{ver[0]}.{ver[1]}.{ver[2]}",
                       html_url=str(data.get("html_url") or ""),
                       exe_url=exe_url, sums_url=sums_url, exe_size=exe_size)


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
        info = fetch_latest_release(timeout=timeout)
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
    """https + 白名单 host 才放行；其余一律 UpdateCheckError（重定向守卫与落地复检共用）。"""
    parts = urlsplit(url or "")
    if parts.scheme != "https":
        raise UpdateCheckError(t("拒绝非 https 地址：{url}", url=url))
    if parts.hostname not in ALLOWED_HOSTS:
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


def _download_expected_sha256(sums_url: str, timeout: float) -> str:
    """拉 SHA256SUMS.txt，取 VRChatLiveTranslate.exe 那一行的 hash（小文件，直接读进内存）。"""
    req = Request(sums_url, headers={"User-Agent": f"vrchat-livetranslate/{__version__}"})
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
    req = Request(url, headers={"User-Agent": f"vrchat-livetranslate/{__version__}"})
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


def download_and_verify(info: ReleaseInfo, dest_dir: Path,
                        timeout: float = DOWNLOAD_TIMEOUT_S,
                        progress: Callable[[int, int | None], None] | None = None) -> Path:
    """下载 exe + SHA256SUMS.txt 到 dest_dir（调用方保证 = exe 同目录，同卷 move 才近原子）。

    校验：SHA256SUMS.txt 里 VRChatLiveTranslate.exe 行的值 == 实测 sha256，
    不一致/解析不出 → 删除已下载文件 + UpdateCheckError，绝不进入替换步骤。
    progress(done, total) 在下载线程里被调，total 取 Content-Length（缺省 None）——
    本模块不碰 Tk，GUI 负责把回调转进队列。
    返回校验通过的 exe 路径（<dest_dir>/VRChatLiveTranslate.exe.new）。
    """
    dest = Path(dest_dir) / (EXE_ASSET_NAME + ".new")
    try:
        expected = _download_expected_sha256(info.sums_url, timeout)
        actual = _stream_to_file(info.exe_url, dest, timeout, progress)
    except Exception as exc:  # noqa: BLE001 — 任何失败都要清理残留 + 留痕，再原样上抛
        dest.unlink(missing_ok=True)
        print(f"[update] 下载失败：{exc}（残留已清理）", flush=True)
        raise
    if actual != expected:
        dest.unlink(missing_ok=True)
        print("[update] 校验失败已删除", flush=True)
        raise UpdateCheckError(t("下载的文件与发布页的摘要对不上，已删除（请重试）"))
    print(f"[update] 下载完成 sha256 校验通过（v{info.version}）", flush=True)
    try:
        write_pending(Path(dest_dir), info.version, actual)
    except OSError as exc:
        # json 写不进去不挡本次更新（【重载】不依赖它）；但【稍后】/残留恢复会因此走不通，
        # 必须留痕 —— 禁静默降级
        print(f"[update] ⚠️ 待更新信息没写下来（{exc}）：「稍后更新」退出时将不会自动替换",
              flush=True)
    return dest


# ---------------------------------------------------------------- 运行形态判定


def update_mode() -> str:
    """'frozen' | 'source' —— 决定「立即更新」按钮行为（源码运行不自更新，只给指引）。"""
    return "frozen" if is_frozen() else "source"


# ---------------------------------------------------------------- 更新器 bat 生成


def build_updater_bat(*, pid: int, current_exe: Path, new_exe: Path,
                      relaunch: bool = True) -> str:
    """生成更新器批处理文本（CRLF 行尾！cmd 对 LF-only 的 label/goto 会抽风）。

    逻辑：等 PID 退出（最多 ~60s）→ 备份 current 为 .bak → move .new 顶替（同卷近原子，
    失败则用 .bak 还原）→（relaunch=True 才）start 拉起新版 → 自删；失败留一行到 exe 旁的
    update_failed.log 并 pause（用户能看到窗口，不会无声消失）。
    relaunch=False 用于【稍后】退出时替换：只换不拉，用户下次自己打开就是新版 ——
    与 True 形态的唯一差别就是没有 start "" 那一行，其余逐字节相同。
    所有路径双引号包裹（桌面/用户名常含空格）；不写注册表、不要管理员。
    bat 正文只用 ASCII：cmd 按系统 OEM 代码页解析，塞中文注释在代码页不符时会变乱码。
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
        f'move /y "{new}" "{cur}"',
        "if not errorlevel 1 goto start",
        f'copy /y "{bak}" "{cur}" >nul',
        "goto fail",
        "",
        ":start",
    ]
    if relaunch:
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


def pending_new_exe(exe_path: Path) -> Path:
    """exe 旁待替换的新版文件位置（与 download_and_verify 的落地路径同一规则）。"""
    return Path(exe_path).parent / (EXE_ASSET_NAME + ".new")


def write_pending(dest_dir: Path, version: str, sha256: str) -> Path:
    """下载校验通过后写 update_pending.json（供【稍后更新】与异常恢复的复验凭据）。"""
    p = Path(dest_dir) / PENDING_JSON
    p.write_text(json.dumps({"version": version, "sha256": sha256},
                            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def check_pending_download(exe_path: Path,
                           current_version: str = "") -> tuple[str, str] | None:
    """残留 .new 的安全网：用之前必须重新校验，损坏/半截绝不放行。

    exe 旁存在 <EXE_ASSET_NAME>.new 且 update_pending.json 完好 → 重新计算 .new 的
    SHA256 与 json 比对：一致 → 返回 (version, sha256)；
    不一致 / json 损坏缺字段 / .new 缺失 / 版本不比 current_version 新（换过的残留）→
    清理残留（.new 与 json 都删）+ 逐分支 [update] 留痕，返回 None。
    完全没有残留 → 静默返回 None（每次启动都会走这条路，不刷日志）。
    """
    exe_path = Path(exe_path)
    new_exe = pending_new_exe(exe_path)
    pending = exe_path.parent / PENDING_JSON

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
