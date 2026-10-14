#!/usr/bin/env python3
"""把一次发布的两个产物（Windows exe / Linux AppImage）发布到 Cloudflare R2，并生成 + 复核 `latest.json`。

## 为什么要有这个脚本（而不是把命令写进 workflow yaml）

「发布」这件事有三段 —— 算摘要 → 生成清单 → 上传并**走公开域名复核**。写成 yaml 胶水之后本机
复现不了（凭据一失效就只能靠猜），而这里本机同一条命令就能补发 / 复核。清单格式是**客户端契约**
（`vlt/update_check.py` 解析它），所以只留一处、由 `tests/test_publish_r2.py` 钉住。

```bat
:: 发布（CI 与本机同一条命令；本机凭据在 D:\\workspace\\.r2.env）
python scripts/publish_r2.py publish --dist dist --tag v0.11.2 --env-file D:/workspace/.r2.env
:: 只复核公开域名上现在是什么（不上传、不改任何东西）
python scripts/publish_r2.py verify --tag v0.11.2
:: 只看清单会被生成成什么样（不联网）
python scripts/publish_r2.py manifest --dist dist --tag v0.11.2
```

## 键与缓存：固定键 + `Cache-Control: no-cache`（实测结论，别改回默认）

四个对象都用**固定键**（客户端地址永不变化），每次都覆盖：

    latest.json
    VRChatLiveTranslate.exe
    VRChatLiveTranslate-x86_64.AppImage
    SHA256SUMS.txt

R2 公共桶的对象默认 `Cache-Control: max-age=14400`，Cloudflare 会缓存**整对象** —— 实测同一键
覆盖之后仍然回**旧内容**（`cf-cache-status: HIT`），客户端会拿到上一版的 exe（摘要对不上 → 更新失败）。
带 `no-cache` 后实测每次 `REVALIDATED`、覆盖后立刻拿到新内容（`no-store` 亦可，走 `BYPASS`）。
→ 也就是说：**「替换式发布」必须配 `no-cache`**，这不是可选优化。

⚠️ 代价（评审 §三 要求写明）：固定键 ⇒ 主源上**没有历史版本可回滚**，「退到上一版」只能去
GitHub 的 tag 页拿附件（Release 里每一个版本都留着）。所以别把 R2 当归档，它只是**当前版**的分发点。
⚠️ 另一个已知窗口：清单里的 `release_url`（GUI「看看这次更新了什么」跳的页）指向本次 tag 的
GitHub Release 页，而 `publish-r2` 跑在 `release` 之前 —— 那几分钟里点它会看到 404，随后自愈。

## 客户端契约（`latest.json`）

```json
{
  "schema": 1,
  "version": "0.11.2",
  "tag": "v0.11.2",
  "published_at": "2026-10-12T18:32:11Z",
  "release_url": "https://github.com/nixi-agent/vrchat-livetranslate/releases/tag/v0.11.2",
  "assets": {
    "VRChatLiveTranslate.exe":            {"url": "https://dl.kcm-nixi.cn/VRChatLiveTranslate.exe",
                                            "size": 34648043, "sha256": "…64hex…"},
    "VRChatLiveTranslate-x86_64.AppImage": {"url": "…", "size": 41524096, "sha256": "…"}
  }
}
```

`assets` 以**附件名**为键（与 Release 附件同名）：客户端按自己运行形态要哪一个（Windows 单文件
exe / Linux AppImage），键名对不上 = 这一版没有它要的包，客户端会明确报「附件不全」而不是悄悄
装错东西。`schema` 只在格式**不兼容**时递增 —— 老客户端遇到不认识的 schema 会退回 GitHub 源。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

# 仓库根入 sys.path：让本机 `python scripts/publish_r2.py` 直接可跑（CI 也是这么跑的）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACCOUNT_ID = "c76b212a7cefad8f62e4f338a5bbe9d1"
BUCKET = "vrclt"
ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"
#: 公开下载域（R2 自定义域，客户端把它写在白名单里）。改这里必须同步改
#: `vlt/update_check.py` 的 MANIFEST_URL / ALLOWED_HOSTS 与 GUIDE 里的下载指引。
BASE_URL = "https://dl.kcm-nixi.cn"
REPO_SLUG = "nixi-agent/vrchat-livetranslate"

MANIFEST_KEY = "latest.json"
MANIFEST_SCHEMA = 1
CACHE_CONTROL = "no-cache"          # 见模块 docstring：固定键覆盖必须配它
CHUNK = 256 * 1024

EXE_NAME = "VRChatLiveTranslate.exe"
APPIMAGE_NAME = "VRChatLiveTranslate-x86_64.AppImage"
SUMS_NAME = "SHA256SUMS.txt"
#: 必须有的产物（缺一个就整批失败 —— 客户端会按形态各要一个）
REQUIRED_ASSETS = (EXE_NAME, APPIMAGE_NAME)
#: 可选对象（SHA256SUMS.txt 只给人和老客户端看；release.yml 生成它在另一个 job，缺了不拦）
OPTIONAL_ASSETS = (SUMS_NAME,)
CONTENT_TYPES = {
    MANIFEST_KEY: "application/json; charset=utf-8",
    EXE_NAME: "application/octet-stream",
    APPIMAGE_NAME: "application/octet-stream",
    SUMS_NAME: "text/plain; charset=utf-8",
}
USER_AGENT = "vrchat-livetranslate-publish/1.0"


class PublishError(RuntimeError):
    """发布 / 复核失败。消息直接给人看，不带堆栈。"""


# ---------------------------------------------------------------- 摘要与清单（纯函数，离线可测）


def sha256_file(path: Path) -> str:
    """流式算文件 sha256（hex）—— 产物 30MB+，别一次读进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_url(key: str, base_url: str = BASE_URL) -> str:
    """对象键 → 公开直链（键里有空格/括号时也要能用）。"""
    return f"{base_url.rstrip('/')}/{quote(key)}"


def release_url(tag: str, slug: str = REPO_SLUG) -> str:
    return f"https://github.com/{slug}/releases/tag/{tag}"


def _check_sums_file(dist: Path, assets: dict[str, dict]) -> None:
    """`SHA256SUMS.txt` 若在，必须与真实产物**逐项一致**，且两个平台各一行。

    为什么值得在这里拦（评审二审 §2 引出的口径）：GUIDE 让用户拿这个文件核校验值，
    而 AppImage 的 sha256 以前只写在 `latest.json` 里 → Linux 用户照文档核不了。
    现在两个平台各一行（生成方 = `release.yml`），于是内容错了比没有更坏，必须门禁。
    解析口径与客户端一致：**按附件名逐行找**（多余行忽略）。
    """
    p = Path(dist) / SUMS_NAME
    if not p.is_file():
        return                       # 可选对象：Release 里没带它就不拦（见 OPTIONAL_ASSETS）
    # ⚠️ 必须按**字节**读再自己解码：`Path.read_text()` 会做通用换行归一化（\r\n → \n），
    # 那样下面这条「不许有 CR」的判据永远不会触发（实测踩到，注入用例直接假绿）。
    raw = p.read_bytes().decode("utf-8", "replace")
    if "\r" in raw:
        # 实测踩到：CI 里 `Out-File` 写的是 CRLF，Linux 侧 `sha256sum -c` 会把 "\r" 当成
        # 文件名的一部分 → 每一行都「找不到文件」。这份文件的读者正是 Linux 用户（GUIDE 让
        # 他们拿它核 AppImage），所以行尾必须是 LF —— 生成侧走 WriteAllText 显式写 `\n`。
        raise PublishError(f"{SUMS_NAME} 里有 CR（CRLF 行尾）：Linux 侧 `sha256sum -c` 会"
                           f"因此整份核不过，请用 LF 行尾重新生成")
    seen: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[1].lstrip("*")
            if name in seen:
                # 同名重复的取舍在两处会**相反**（客户端命中即取第一条，这里若取最后一条），
                # 评审三审 R4 点出的口径不一致。生成侧不会产生重复行，那就直接判错，
                # 别留一个「两边读法不同」的隐患。
                raise PublishError(f"{SUMS_NAME} 里 {name} 出现了不止一行："
                                   f"同名重复时客户端取第一条、这里若取最后一条就会对不上，"
                                   f"请让每个附件只占一行")
            seen[name] = parts[0].lower()
    for name in REQUIRED_ASSETS:
        got, want = seen.get(name), str(assets.get(name, {}).get("sha256") or "")
        if got != want:
            raise PublishError(
                f"{SUMS_NAME} 里 {name} 的校验值与产物不一致"
                f"（文件里：{got or '缺这一行'}；实测：{want[:12]}…）："
                f"用户会拿这个文件核校验值，内容错了比没有更坏")


def build_manifest(dist: Path, tag: str, published_at: str | None = None,
                   base_url: str = BASE_URL) -> dict:
    """扫 dist/ 里的产物 → `latest.json` 的内容（不联网、不写盘）。

    `tag` 必须是 `vX.Y.Z`（与 `release.yml` 的 tag / `__version__` 对账同口径）：清单里的
    `version` 会被客户端当版本号比较，形态不对 = 这一版永远收不到更新（或永远提示有更新）。
    ⚠️ 前缀只在这里规范化一次（`v`/`V` 都认）—— 别在 `version` 与 `tag` 两处各判一次，
    评审指出过 `V9.9.9` 会生成 `tag: "vV9.9.9"` 的旧写法。
    """
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() and (p == "0" or not p.startswith("0")) for p in parts):
        raise PublishError(f"tag 不是严格 vX.Y.Z（客户端只认这个形态）：{tag!r}")
    tag_field = f"v{version}"
    dist = Path(dist)
    assets: dict[str, dict] = {}
    for name in REQUIRED_ASSETS:
        p = dist / name
        if not p.is_file():
            raise PublishError(f"缺少产物 {name}（dist 目录里没有；发布不接受少一半）")
        assets[name] = {"url": asset_url(name, base_url), "size": p.stat().st_size,
                        "sha256": sha256_file(p)}
        if assets[name]["size"] <= 0:
            raise PublishError(f"产物 {name} 是空文件")
    for name in OPTIONAL_ASSETS:
        p = dist / name
        if p.is_file():
            assets[name] = {"url": asset_url(name, base_url), "size": p.stat().st_size,
                            "sha256": sha256_file(p)}
    if published_at is None:
        published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _check_sums_file(dist, assets)
    return {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "tag": tag_field,
        "published_at": published_at,
        "release_url": release_url(tag_field),
        "assets": assets,
    }


def manifest_bytes(manifest: dict) -> bytes:
    """清单的字节形态：UTF-8、无 BOM、末尾一个换行（人看也顺眼）。"""
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------- 上传


def load_env_file(path: Path) -> None:
    """把 KEY=VALUE 文件里的 R2 凭据灌进环境变量（本机用；只认这两个键）。"""
    wanted = {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in wanted and v:
            os.environ[k] = v


def make_uploader(endpoint: str = ENDPOINT, bucket: str = BUCKET):
    """默认上传器：boto3 + S3 API（R2 的 S3 端点只认 R2 的 Access Key/Secret）。"""
    import boto3                                    # 延迟 import：manifest/verify 不需要 boto3
    from botocore.config import Config

    ak, sk = os.environ.get("R2_ACCESS_KEY_ID"), os.environ.get("R2_SECRET_ACCESS_KEY")
    if not ak or not sk:
        raise PublishError("缺 R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY"
                           "（CI 走 secrets，本机可用 --env-file 指一个 KEY=VALUE 文件）")
    client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ak,
                          aws_secret_access_key=sk, region_name="auto",
                          config=Config(signature_version="s3v4",
                                        retries={"max_attempts": 5, "mode": "standard"}))

    def upload(local: Path, key: str, content_type: str) -> None:
        try:
            client.upload_file(str(local), bucket, key,
                               ExtraArgs={"ContentType": content_type, "CacheControl": CACHE_CONTROL})
        except Exception as exc:  # noqa: BLE001 — 凭据/桶名/网络错都要变成一句 [r2] ❌，
            # 而不是让 boto3 的 ClientError 抛一屏栈（评审 §三）
            raise PublishError(f"上传 {key} 失败：{type(exc).__name__}: {exc}") from exc

    return upload


# ---------------------------------------------------------------- 公开域名复核


def _http_get(url: str, timeout: float = 60.0, opener=None) -> tuple[bytes | None, dict]:
    """GET 一个 URL：返回 (前若干字节 None=流式交给调用方, 响应头)。流式版见 _http_stream。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        body = resp.read(64 * 1024)
    return body, headers


def _http_stream_sha256(url: str, timeout: float = 300.0, opener=None) -> tuple[str, int, dict]:
    """流式下载并算 sha256：返回 (hex, 字节数, 响应头)。复核「用户拿到的是不是这堆字节」用。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    n = 0
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n, headers


def same_host(url: str, base_url: str) -> bool:
    """清单里的产物地址是否落在公开域下。

    ⚠️ 两处口径（评审 §三 提到第一处，本次一并收紧）：

    * 用**主机名逐字相等**而不是 `startswith`：`https://dl.kcm-nixi.cn.evil.com/x` 也能通过
      startswith 那一关（前缀匹配的经典坑），而主机名相等没有这个缝；
    * 比的是**完整 origin**（scheme + host + 有效端口）：`https://dl.kcm-nixi.cn:8443/x` 与
      `http://dl.kcm-nixi.cn/x` 都不该算「在公开域下」。
    """
    return _origin(url) == _origin(base_url)


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, hostname, 端口) —— 端口缺省按 scheme 补 80/443；畸形值统一给 None（判不相等）。"""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (parts.scheme, parts.hostname or "", port)


def verify_public(tag: str, *, base_url: str = BASE_URL, with_assets: bool = True,
                  timeout: float = 300.0, opener=None) -> dict:
    """走**公开域名**复核这次发布：清单是不是这一版、两个产物是不是发布的那些字节。

    为什么必须复核：上传成功 ≠ 用户拿到的是新东西（CDN 缓存、键写错、Content-Type 装错都可能
    让下载站看起来「有」，实际是旧的）。这条命令在 CI 里是发布门禁，在本机是事后体检。
    """
    want_version = tag[1:] if tag.startswith(("v", "V")) else tag
    problems: list[str] = []
    m_url = asset_url(MANIFEST_KEY, base_url)
    try:
        raw, m_headers = _http_get(m_url, timeout=60.0, opener=opener)
    except urllib.error.HTTPError as exc:
        raise PublishError(f"取不到公开清单 {m_url}：HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishError(f"取不到公开清单 {m_url}：{type(exc).__name__}: {exc}") from exc
    cache_note = (f"cf-cache-status={m_headers.get('cf-cache-status', '?')} "
                  f"age={m_headers.get('age', '-')} etag={m_headers.get('etag', '-')}")
    try:
        data = json.loads((raw or b"").decode("utf-8", "replace"))
    except ValueError as exc:
        raise PublishError(f"公开清单不是合法 JSON（{exc}）：{m_url} [{cache_note}]") from exc
    if not isinstance(data, dict):
        raise PublishError(f"公开清单不是对象：{m_url} [{cache_note}]")
    got_version = str(data.get("version") or "")
    if got_version != want_version:
        problems.append(f"清单版本是 {got_version!r}，期望 {want_version!r}")
    print(f"[r2] 清单 {m_url}：version={got_version} schema={data.get('schema')} [{cache_note}]")
    assets = data.get("assets") if isinstance(data.get("assets"), dict) else {}
    for name in REQUIRED_ASSETS:
        entry = assets.get(name) if isinstance(assets.get(name), dict) else None
        if entry is None:
            problems.append(f"清单里没有 {name}")
            continue
        url = str(entry.get("url") or "")
        if not same_host(url, base_url):
            # 域名不对就**不再去取**：清单是从网上读来的，指哪儿打哪儿等于把「下载任意地址」的能力
            # 交出去（即使只是复核脚本）。复核的判据是「我们自己的域名上是不是这些字节」。
            problems.append(f"{name} 的 url 不在公开域下：{url!r}")
            continue
        sha = str(entry.get("sha256") or "").lower()
        if len(sha) != 64:
            problems.append(f"{name} 的 sha256 形态不对：{sha!r}")
        if not with_assets:
            print(f"[r2] {name}：size={entry.get('size')} sha256={sha[:16]}…（--no-assets：不下载核对）")
            continue
        try:
            actual, size, headers = _http_stream_sha256(url, timeout=timeout, opener=opener)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"下载 {name} 失败：{type(exc).__name__}: {exc}")
            continue
        # 逐字节核对：这是「用户真正会拿到这些字节」的唯一硬证据
        if actual != sha:
            problems.append(f"{name} 的 sha256 对不上（清单 {sha[:12]}… 实测 {actual[:12]}…，"
                            f"cf-cache-status={headers.get('cf-cache-status', '?')}"
                            f" age={headers.get('age', '-')}）")
        elif isinstance(entry.get("size"), int) and entry["size"] != size:
            problems.append(f"{name} 的 size 对不上（清单 {entry['size']} 实测 {size}）")
        else:
            cs = headers.get("cf-cache-status", "?")
            print(f"[r2] {name}：{size} 字节 sha256 校验通过（cf-cache-status={cs}）")
    if problems:
        for p in problems:
            print(f"[r2] ❌ {p}", flush=True)
        # 报错本身也要带上具体原因：CI 上「失败在哪一项」比一个计数有用得多
        detail = "；".join(problems[:4]) + ("；…" if len(problems) > 4 else "")
        raise PublishError(f"公开域名复核不通过（{len(problems)} 项）：{detail}。{manifests_hint(m_url)}")
    print(f"[r2] ✅ 公开域名复核通过：v{want_version} 的清单与产物逐字节一致", flush=True)
    return {"version": got_version, "manifest_url": m_url, "assets": sorted(assets)}


def manifests_hint(url: str) -> str:
    return f"{url}（若怀疑 CDN 缓存，看响应头 cf-cache-status / age；对象必须带 Cache-Control: {CACHE_CONTROL}）"


# ---------------------------------------------------------------- 子命令


def cmd_manifest(args: argparse.Namespace) -> int:
    m = build_manifest(Path(args.dist), args.tag, base_url=args.base_url)
    text = manifest_bytes(m).decode("utf-8")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"[r2] 清单已写入 {args.out}")
    else:
        print(text, end="")
    for name, entry in m["assets"].items():
        print(f"[r2] {name}: {entry['size']} 字节 sha256={entry['sha256']}", flush=True)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    if args.env_file:
        load_env_file(Path(args.env_file))
    dist = Path(args.dist)
    m = build_manifest(dist, args.tag, base_url=args.base_url)
    print(f"[r2] 发布 v{m['version']} → {args.base_url}（bucket={BUCKET}）")
    for name, entry in m["assets"].items():
        print(f"[r2] {name}: {entry['size']} 字节 sha256={entry['sha256']}")
    # 清单也写进 dist/：CI 里顺手当构建产物留存，本机则是「刚才发了什么」的凭据
    (dist / MANIFEST_KEY).write_bytes(manifest_bytes(m))
    if args.dry_run:
        print("[r2] --dry-run：不联网、不上传")
        return 0
    upload = make_uploader(args.endpoint, args.bucket)
    # 顺序有讲究：**先传产物、最后传清单**。清单是客户端唯一的入口 —— 先挂清单的话，产物还没
    # 上去用户就已经看到「有新版本」，点下去只会下到 404 或半截文件（这类「一半的更新」在慢网络
    # 下才偶发，事后极难复现，所以顺序固定在这里，不要图省事改成并行/反过来）。
    for name in list(REQUIRED_ASSETS) + list(OPTIONAL_ASSETS):
        p = dist / name
        if not p.is_file():
            print(f"[r2] ⚠️ 可选对象 {name} 不在 dist/，跳过（Release 附件里没有它，通常无害）",
                  flush=True)
            continue
        key = name
        upload(p, key, CONTENT_TYPES.get(key, "application/octet-stream"))
        print(f"[r2] 已上传 {key}（Cache-Control: {CACHE_CONTROL}）", flush=True)
    upload(dist / MANIFEST_KEY, MANIFEST_KEY, CONTENT_TYPES[MANIFEST_KEY])
    print(f"[r2] 已上传 {MANIFEST_KEY}", flush=True)
    # 上传完立刻走公开域名自重（CI 的发布门禁就靠这一步）
    if args.verify:
        time.sleep(args.verify_delay)
        verify_public(args.tag, base_url=args.base_url, with_assets=args.verify_assets)
    else:
        print("[r2] ⚠️ --no-verify：跳过公开域名复核（发布门禁就靠它，别在 CI 里关）", flush=True)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    verify_public(args.tag, base_url=args.base_url, with_assets=not args.no_assets)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="发布产物到 Cloudflare R2 并复核公开下载域名")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--tag", required=True, help="发布 tag，如 v0.11.2（必须严格 vX.Y.Z）")
        sp.add_argument("--base-url", default=BASE_URL, help=f"公开下载域（默认 {BASE_URL}）")

    mp = sub.add_parser("manifest", help="只生成 latest.json（不联网）")
    common(mp)
    mp.add_argument("--dist", default="dist", help="产物目录（默认 dist）")
    mp.add_argument("--out", default="", help="写到文件；缺省打到 stdout")
    mp.set_defaults(func=cmd_manifest)

    pp = sub.add_parser("publish", help="生成清单 + 上传 + 复核（CI 用的就是这条）")
    common(pp)
    pp.add_argument("--dist", default="dist", help="产物目录（默认 dist）")
    pp.add_argument("--endpoint", default=ENDPOINT, help="R2 的 S3 端点")
    pp.add_argument("--bucket", default=BUCKET, help="R2 桶名")
    pp.add_argument("--env-file", default="", help="KEY=VALUE 凭据文件（本机用；CI 走环境变量）")
    pp.add_argument("--dry-run", action="store_true", help="只算摘要 + 生成清单，不上传")
    pp.add_argument("--verify", dest="verify", action="store_true", default=True,
                    help="上传后走公开域名复核（默认开）")
    pp.add_argument("--no-verify", dest="verify", action="store_false",
                    help="跳过复核（只在明确知道自己在干什么时用）")
    pp.add_argument("--verify-assets", dest="verify_assets", action="store_true", default=True,
                    help="复核时把两个产物整份下回来对摘要（默认开）")
    pp.add_argument("--no-verify-assets", dest="verify_assets", action="store_false")
    pp.add_argument("--verify-delay", type=float, default=2.0, help="上传完等几秒再复核（默认 2s）")
    pp.set_defaults(func=cmd_publish)

    vp = sub.add_parser("verify", help="只复核公开域名上现在是什么")
    common(vp)
    vp.add_argument("--no-assets", action="store_true", help="只核清单，不下载产物")
    vp.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PublishError as exc:
        print(f"[r2] ❌ {exc}", flush=True)
        return 1
    except OSError as exc:
        # 网络/磁盘层的错（DNS、超时、写不进去）同样给一句人话，别抛栈
        print(f"[r2] ❌ {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
