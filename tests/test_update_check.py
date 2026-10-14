"""vlt/update_check.py 的离线测试（覆盖计划 Task 1~7 的全部纯逻辑）。

跑法：.venv/Scripts/python.exe tests/test_update_check.py

全程离线：HTTP 层一律用假 opener 注入（整体替换模块级 `uc._opener`），
绝不真连 GitHub / Cloudflare —— CI 机器不一定能连外网。

两个更新源都要覆盖：主源（Cloudflare 清单，`manifest_payload()` 造）与兜底源
（GitHub Release，`payload()` 造），外加「主源失败才回落」的顺序与留痕（`test_manifest_source`）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

from vlt import update_check as uc                  # noqa: E402

OUT = ROOT / "out" / "update_check"
EXE_NAME = "VRChatLiveTranslate.exe"
APPIMAGE_NAME = "VRChatLiveTranslate-x86_64.AppImage"
SUMS_NAME = "SHA256SUMS.txt"
DL_BASE = "https://github.com/nixi-agent/vrchat-livetranslate/releases/download"

# 附件名是**与 release.yml 的发布契约**（那边 `files:` 里写的名字）：写死在这里，
# 常量被改名而工作流没跟着改的话，这里立刻红。
assert EXE_NAME == uc.EXE_ASSET_NAME, "exe 附件名与发布契约不一致"
assert APPIMAGE_NAME == uc.APPIMAGE_ASSET_NAME, "AppImage 附件名与发布契约不一致"


# ---------------------------------------------------------------- 测试替身


class FakeResp:
    """冒充 urllib 的响应：read 可分块、有 url / headers、能当上下文管理器。"""

    def __init__(self, body: bytes, url: str = uc.RELEASES_LATEST_API,
                 headers: dict | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.url = url
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """按预设脚本顺序回应：条目是 FakeResp（原样返回）或 Exception（原样抛出）。"""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[str] = []
        self.reqs: list = []

    def open(self, req, timeout=None):  # noqa: ANN001, ANN201
        self.calls.append(req.full_url if hasattr(req, "full_url") else str(req))
        self.reqs.append(req)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class MapOpener:
    """按 URL 分发响应（下载测试用：exe 与 sums 两个地址各回各的）。"""

    def __init__(self, mapping: dict) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def open(self, req, timeout=None):  # noqa: ANN001, ANN201
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        item = self.mapping[url]
        if isinstance(item, Exception):
            raise item
        return item


def payload(tag: str = "v9.9.9", *, asset: str = EXE_NAME, asset_size: int | None = 12345678,
            drop: str = "", digest: str | None = "sha256:" + "a" * 64) -> bytes:
    """造一份 GitHub /releases/latest 的 JSON。

    `asset` 决定「我们要的那份附件」叫什么（exe / AppImage，两者只有一个在）；
    drop='asset'/'sums' 让附件缺一个；digest=None 模拟「GitHub 没给 digest」的老形态
    （2026-09 起官方 Release 不再上传 SHA256SUMS.txt，校验值改为取 assets[].digest）。
    """
    assets = []
    if drop != "asset":
        a = {"name": asset, "browser_download_url": f"{DL_BASE}/{tag}/{asset}"}
        if asset_size is not None:
            a["size"] = asset_size
        if digest:
            a["digest"] = digest
        assets.append(a)
    if drop != "sums":
        assets.append({"name": SUMS_NAME,
                       "browser_download_url": f"{DL_BASE}/{tag}/{SUMS_NAME}"})
    return json.dumps({
        "tag_name": tag,
        "html_url": f"https://github.com/nixi-agent/vrchat-livetranslate/releases/tag/{tag}",
        "assets": assets,
    }).encode()


def manifest_payload(version: str = "9.9.9", *, asset: str = EXE_NAME, sha: str = "a" * 64,
                     size: int | None = 12345678, url: str | None = None, schema: int = 1,
                     release_url: str | None = None, assets=None) -> bytes:
    """造一份 **Cloudflare 清单**（`scripts/publish_r2.py` 发出来的那个形态）。

    字段含义见该脚本头部；这里的手写形态是**故意的**：客户端对清单的容忍度由它钉住，
    发布侧改格式会在 tests/test_publish_r2.py 的「发布 → 解析」往返里红掉。
    """
    if assets is None:
        if url is None:
            url = f"https://dl.kcm-nixi.cn/{asset}"
        assets = {asset: {"url": url, "size": size, "sha256": sha}}
        other = APPIMAGE_NAME if asset == EXE_NAME else EXE_NAME
        assets[other] = {"url": f"https://dl.kcm-nixi.cn/{other}", "size": 1, "sha256": "b" * 64}
    return json.dumps({
        "schema": schema,
        "version": version,
        "tag": f"v{version}",
        "published_at": "2026-10-14T00:00:00Z",
        "release_url": release_url if release_url is not None else
        f"https://github.com/nixi-agent/vrchat-livetranslate/releases/tag/v{version}",
        "assets": assets,
    }).encode()


# ---------------------------------------------------------------- Task 1：版本解析与比较


def test_parse_version() -> None:
    assert uc.parse_version("0.1.1") == (0, 1, 1)
    assert uc.parse_version("v0.1.2") == (0, 1, 2)
    assert uc.parse_version(" V1.2.3 ") == (1, 2, 3)
    assert uc.parse_version("10.20.30") == (10, 20, 30)
    for bad in ("", "0.1", "0.1.2.3", "v", "abc", "0.1.2-beta", "0.1.x", "1.02.3"):
        # 1.02.3 这种带前导零的也拒掉：与 release.yml 对账正则的产物不一致就是异常
        assert uc.parse_version(bad) is None, f"应当拒绝：{bad!r}"
    print("  parse_version OK")


def test_is_newer() -> None:
    assert uc.is_newer("v0.1.2", "0.1.1")
    assert not uc.is_newer("v0.1.1", "0.1.1")
    assert not uc.is_newer("v0.1.0", "0.1.1")
    assert uc.is_newer("v1.0.0", "0.9.9")
    # 非法 tag → 不认为有更新（防御，绝不崩）
    assert not uc.is_newer("not-a-version", "0.1.1")
    print("  is_newer OK")


# ---------------------------------------------------------------- Task 2：拉取最新 Release


def test_fetch_latest_release_github() -> None:
    old = uc._opener
    try:
        op = FakeOpener([FakeResp(payload("v9.9.9"))])
        uc._opener = op
        info = uc.fetch_latest_release_github()
        assert info.version == "9.9.9" and info.tag == "v9.9.9"
        assert info.asset_name == EXE_NAME, f"默认要的应该是 exe：{info.asset_name}"
        assert info.asset_url.endswith("/" + EXE_NAME)
        assert info.sums_url.endswith("/" + SUMS_NAME)
        assert info.asset_size == 12345678, f"asset_size 没带上：{info.asset_size}"
        assert info.asset_digest == "a" * 64, f"没取到 GitHub 的 digest：{info.asset_digest!r}"
        # GitHub 对无 User-Agent 的请求直接 403 —— UA 必须带
        ua = op.reqs[0].headers.get("User-agent", "")
        assert ua.startswith("vrchat-livetranslate/"), f"缺 User-Agent：{ua!r}"

        # ★ AppImage 形态：要的就是 AppImage 那个附件（Windows 的 exe 在不在都无所谓）
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", asset=APPIMAGE_NAME))])
        img = uc.fetch_latest_release_github(asset_name=APPIMAGE_NAME)
        assert img.asset_name == APPIMAGE_NAME and img.asset_url.endswith("/" + APPIMAGE_NAME), \
            f"没按 asset_name 取附件：{img}"
        # ★ 缺 AppImage 附件（比如只带了 exe 的 Release）→ 报错，绝不悄悄拿别的附件顶上
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9"))])
        try:
            uc.fetch_latest_release_github(asset_name=APPIMAGE_NAME)
            raise AssertionError("Release 里没有 AppImage 居然没报错")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e) and APPIMAGE_NAME in str(e), f"提示不对：{e}"
        # ★ AppImage 没有 SHA256SUMS.txt 兜底（那个文件里只有 exe 一行）→ 必须自带 digest
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", asset=APPIMAGE_NAME, digest=None))])
        try:
            uc.fetch_latest_release_github(asset_name=APPIMAGE_NAME)
            raise AssertionError("AppImage 没有 digest 居然放行（没法校验）")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e)

        # assets[].size 缺省 → asset_size 为 None（进度条改走 Content-Length / indeterminate）
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", asset_size=None))])
        assert uc.fetch_latest_release_github().asset_size is None

        # ★ 没有 SHA256SUMS.txt（2026-09 起官方 Release 就不带这个文件）→ 靠 digest 照样能更新。
        #   这就是实测踩到的那个 bug：旧逻辑硬要求两个附件，删掉 sums 后更新检查直接静默失败。
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", drop="sums"))])
        _nosum = uc.fetch_latest_release_github()
        assert _nosum.asset_digest and not _nosum.sums_url, \
            f"缺 sums 时应当用 digest 顶上：digest={_nosum.asset_digest!r} sums={_nosum.sums_url!r}"

        # exe 缺了 → UpdateCheckError
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", drop="asset"))])
        try:
            uc.fetch_latest_release_github()
            raise AssertionError("缺 exe 居然没报错")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e)

        # 既没 digest 也没 sums（没有任何可校验的凭据）→ 也必须报错，绝不装一个没法验的包
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", digest=None, drop="sums"))])
        try:
            uc.fetch_latest_release_github()
            raise AssertionError("没有任何校验凭据居然没报错")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e)

        # 限流 403 / 429 → 明确提示（未认证 60 次/小时，手动按钮被连点时最容易撞）
        for code in (403, 429):
            uc._opener = FakeOpener([HTTPError(uc.RELEASES_LATEST_API, code, "x",
                                               None, io.BytesIO(b""))])
            try:
                uc.fetch_latest_release_github()
                raise AssertionError(f"HTTP {code} 居然没报错")
            except uc.UpdateCheckError as e:
                assert "限流" in str(e), f"HTTP {code} 提示不对：{e}"

        # 断网 → 网络不可达
        uc._opener = FakeOpener([URLError("timed out")])
        try:
            uc.fetch_latest_release_github()
            raise AssertionError("断网居然没报错")
        except uc.UpdateCheckError as e:
            assert "网络不可达" in str(e)

        # 非 semver tag → 报错不崩
        uc._opener = FakeOpener([FakeResp(payload("release-2024"))])
        try:
            uc.fetch_latest_release_github()
            raise AssertionError("坏 tag 居然没报错")
        except uc.UpdateCheckError:
            pass
        print("  fetch_latest_release_github OK")
    finally:
        uc._opener = old


# ---------------------------------------------------------------- Task 2b：主源（Cloudflare 清单）与回落


def test_manifest_source() -> None:
    """主源 = 自家 Cloudflare 清单：解析契约 + 「主源失败才回落 GitHub」的行为。

    这条守的是本次改动的**全部目的**：大陆用户不再需要连 GitHub 才能收到更新。
    """
    old = uc._opener
    try:
        # ① 清单正常 → **一次网络就够**（不该再顺带去问 GitHub）
        op = FakeOpener([FakeResp(manifest_payload("9.9.9"), url=uc.MANIFEST_URL)])
        uc._opener = op
        info = uc.fetch_latest_release()
        assert info.tag == "v9.9.9" and info.version == "9.9.9", info
        assert info.asset_name == EXE_NAME and info.asset_url == f"https://dl.kcm-nixi.cn/{EXE_NAME}"
        assert info.asset_digest == "a" * 64 and info.asset_size == 12345678, info
        assert info.sums_url == "", "主源的清单自带 sha256，不该再去要 SHA256SUMS.txt"
        # 「看看这次更新了什么」的落点仍来自清单的 release_url（按钮行为不变）
        assert info.html_url.endswith("/tag/v9.9.9") and "github.com" in info.html_url, info.html_url
        assert op.calls == [uc.MANIFEST_URL], f"主源正常时不该再问别处：{op.calls}"
        # UA 必须带（与 GitHub 那条同口径）：R2 访问日志里要能看出是哪一版在查
        assert op.reqs[0].headers.get("User-agent", "").startswith("vrchat-livetranslate/")

        # ② AppImage 形态：清单里两份都有 → 取 AppImage 那份（落点/校验值都跟附件名走）
        uc._opener = FakeOpener([FakeResp(manifest_payload("9.9.9"), url=uc.MANIFEST_URL)])
        img = uc.fetch_latest_release(asset_name=APPIMAGE_NAME)
        assert img.asset_name == APPIMAGE_NAME and img.asset_url.endswith(APPIMAGE_NAME), img
        assert img.asset_digest == "b" * 64, img

        # ③ 清单里没有本形态的包 → 报错（与 GitHub 源同一句话，别悄悄拿另一个平台的包顶上）
        only_exe = {"assets": {EXE_NAME: {"url": f"https://dl.kcm-nixi.cn/{EXE_NAME}",
                                          "size": 1, "sha256": "a" * 64}}}
        try:
            uc.parse_manifest(manifest_payload("9.9.9", **only_exe).decode(), APPIMAGE_NAME)
            raise AssertionError("清单里没有 AppImage 居然放行")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e) and APPIMAGE_NAME in str(e), f"提示不对：{e}"

        # ④ 不合契约的清单逐条必须报错（这里放过的形态，最后都会变成一次装了一半的更新）
        bad_cases = [
            ("坏 JSON", b"{not json"),
            ("顶层不是对象", b"[1,2,3]"),
            ("schema 不认识", manifest_payload("9.9.9", schema=99)),
            ("version 不是严格 X.Y.Z", manifest_payload("9.9.9-beta")),
            ("url 不是 https", manifest_payload("9.9.9", url="http://dl.kcm-nixi.cn/x.exe")),
            ("url 在白名单外", manifest_payload("9.9.9", url="https://evil.example.com/x.exe")),
            ("sha256 形态不对", manifest_payload("9.9.9", sha="deadbeef")),
            ("assets 不是对象", manifest_payload("9.9.9", assets=[])),
        ]
        for name, body in bad_cases:
            try:
                uc.parse_manifest(body.decode(), EXE_NAME)
                raise AssertionError(f"{name} 居然解析通过")
            except uc.UpdateCheckError:
                pass

        # size 缺省 → asset_size 为 None（进度条走 Content-Length / indeterminate），不算错
        m = json.loads(manifest_payload("9.9.9").decode())
        del m["assets"][EXE_NAME]["size"]
        assert uc.parse_manifest(json.dumps(m), EXE_NAME).asset_size is None

        # ⑤ 主源失败 → **回落** GitHub：两个请求、顺序对、日志留痕（禁静默降级）
        buf = io.StringIO()
        op = FakeOpener([URLError("cf 连不上"), FakeResp(payload("v9.9.9"))])
        uc._opener = op
        with redirect_stdout(buf):
            info = uc.fetch_latest_release()
        assert info.version == "9.9.9" and info.asset_url.endswith("/" + EXE_NAME), info
        assert op.calls == [uc.MANIFEST_URL, uc.RELEASES_LATEST_API], f"回落顺序不对：{op.calls}"
        logs = buf.getvalue()
        for needle in ("主源", "回落", "GitHub"):
            assert needle in logs, f"回落没留痕（缺 {needle}）：{logs!r}"
        # 清单**不合契约**也算主源失败 → 同样回落（不是让所有人都收不到更新）
        uc._opener = FakeOpener([FakeResp(b"{not json", url=uc.MANIFEST_URL),
                                 FakeResp(payload("v9.9.9"))])
        assert uc.fetch_latest_release().version == "9.9.9"

        # ⑥ 两边都挂 → 抛兜底源（GitHub）的错：用户看到的话术与改动前一致
        uc._opener = FakeOpener([URLError("cf 连不上"), URLError("timed out")])
        try:
            uc.fetch_latest_release()
            raise AssertionError("两个源都挂了居然没报错")
        except uc.UpdateCheckError as e:
            assert "网络不可达" in str(e), f"提示不对：{e}"

        # ⑦ 域名与白名单是**两处**：改清单域名时漏改白名单 → 主源会被自己的守卫拦掉（这里红）
        assert urlsplit(uc.MANIFEST_URL).hostname in uc.ALLOWED_HOSTS, "清单域名不在白名单里"
        print("  manifest source OK（主源解析 + 回落 GitHub）")
    finally:
        uc._opener = old


# ---------------------------------------------------------------- Task 2c：下载回落与各处守卫（评审 §3~§5）


def test_download_fallback_and_guards() -> None:
    """评审 §3（下载阶段回落）/§4（只回落主源、不吞自己的 bug）/§5（release_url 白名单）
    以及端口口径与紧急开关。全部离线（假 opener 按顺序回应）。"""
    old = uc._opener
    tmp = OUT / "dl_fallback"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        # ① 主源产物下不动 → 用**同一版本**的 GitHub 附件重试一次
        alt_bytes = b"fallback-" * 100
        alt_sha = hashlib.sha256(alt_bytes).hexdigest()
        prim = uc.parse_manifest(manifest_payload("9.9.9").decode(), EXE_NAME)
        cf_url = prim.asset_url
        gh_url = f"{DL_BASE}/v9.9.9/{EXE_NAME}"
        op = FakeOpener([
            URLError("主源下到一半断了"),          # ① 主源产物
            # ② github_fallback 查 API：校验值就是备用源那份（同一版本的两个落点，字节一致）
            FakeResp(payload("v9.9.9", digest="sha256:" + alt_sha)),
            FakeResp(alt_bytes, url=gh_url),       # ③ GitHub 附件
        ])
        uc._opener = op
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = uc.download_and_verify(prim, tmp, fallback=lambda: uc.github_fallback(prim))
        assert op.calls == [cf_url, uc.RELEASES_LATEST_API, gh_url], f"请求序列不对：{op.calls}"
        assert out.read_bytes() == alt_bytes, "换源之后落盘的不是备用源那份字节"
        meta = json.loads((tmp / uc.PENDING_JSON).read_text(encoding="utf-8"))
        assert meta == {"version": "9.9.9", "sha256": alt_sha}, meta
        logs = buf.getvalue()
        for needle in ("主源下载失败", "换备用源重试", "备用源（GitHub）"):
            assert needle in logs, f"回落没留痕（缺 {needle}）：{logs!r}"
        out.unlink()
        (tmp / uc.PENDING_JSON).unlink(missing_ok=True)

        # ② 备用源版本对不上 → 宁可失败，绝不跨版本装
        uc._opener = FakeOpener([FakeResp(payload("v8.8.8"))])
        try:
            uc.github_fallback(prim)
            raise AssertionError("版本不一致居然放行")
        except uc.UpdateCheckError as e:
            assert "v8.8.8" in str(e) and "9.9.9" in str(e), f"提示没点名两个版本：{e}"

        # ③ 用户取消 ≠ 失败：不许再发起第二次传输（否则点了关闭还去下一遍 37MB）
        class _DownloadCancelled(Exception):
            pass

        assert uc._is_download_cancel(_DownloadCancelled()), "取消异常的类名契约变了"
        from vlt.ui_text import _DownloadCancelled as RealCancel
        assert uc._is_download_cancel(RealCancel()), \
            "ui_text 里的 _DownloadCancelled 改名/改结构了：取消会被当成失败去重试"

        called: list[int] = []

        def boom_fallback():
            called.append(1)
            raise AssertionError("用户取消了还去回退")

        class _CancelResp:
            url = cf_url
            headers: dict = {}

            def read(self, n: int = -1) -> bytes:
                raise _DownloadCancelled()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        uc._opener = MapOpener({cf_url: _CancelResp()})
        try:
            uc.download_and_verify(prim, tmp, fallback=boom_fallback)
            raise AssertionError("取消居然没抛出去")
        except _DownloadCancelled:
            pass
        assert not called, "用户取消后仍然发起了第二次传输"
        assert not (tmp / (EXE_NAME + uc.NEW_SUFFIX)).exists(), "取消后残留没清"

        # ④ 我们**自己的代码**坏了（TypeError）→ 必须原样冒出来 + 不许去向 GitHub 回落
        real_parse = uc.parse_manifest

        def boom(text, asset_name=EXE_NAME):  # noqa: ANN001, ANN202
            raise TypeError("注入的代码 bug")

        try:
            uc.parse_manifest = boom
            op = FakeOpener([FakeResp(manifest_payload("9.9.9"), url=uc.MANIFEST_URL)])
            uc._opener = op
            try:
                uc.fetch_latest_release()
                raise AssertionError("解析里的 TypeError 被吞成「回落 GitHub」了")
            except TypeError:
                pass
            assert op.calls == [uc.MANIFEST_URL], f"代码 bug 不该触发回落：{op.calls}"
        finally:
            uc.parse_manifest = real_parse

        # ⑤ release_url 会直接交给浏览器打开 → 与别的 URL 同口径（白名单外的换掉）
        evil = json.loads(manifest_payload("9.9.9", release_url="https://evil.example.com/x"))
        assert uc.parse_manifest(json.dumps(evil), EXE_NAME).html_url == \
            f"{uc.RELEASES_HTML}/tag/v9.9.9"
        good_url = f"{uc.RELEASES_HTML}/tag/v9.9.9"
        keep = json.loads(manifest_payload("9.9.9", release_url=good_url))
        assert uc.parse_manifest(json.dumps(keep), EXE_NAME).html_url == good_url

        # ⑥ 端口/协议也算白名单口径（`host:8443` 不是我们发布东西的地方）
        for bad in ("https://dl.kcm-nixi.cn:8443/x.exe", "http://dl.kcm-nixi.cn/x.exe"):
            try:
                uc.parse_manifest(manifest_payload("9.9.9", url=bad).decode(), EXE_NAME)
                raise AssertionError(f"非默认端口 / 非 https 居然放行：{bad}")
            except uc.UpdateCheckError:
                pass
        assert uc._is_allowed_url(uc.MANIFEST_URL)
        assert not uc._is_allowed_url("https://dl.kcm-nixi.cn:8443/x")

        # ⑦ 紧急开关：VLT_UPDATE_SOURCE=github → 直接跳过主源（评审要求「可吊销手段」）
        buf = io.StringIO()
        op = FakeOpener([FakeResp(payload("v9.9.9"))])
        uc._opener = op
        os.environ[uc.ENV_UPDATE_SOURCE] = "github"
        try:
            with redirect_stdout(buf):
                forced = uc.fetch_latest_release()
        finally:
            os.environ.pop(uc.ENV_UPDATE_SOURCE, None)
        assert forced.version == "9.9.9"
        assert op.calls == [uc.RELEASES_LATEST_API], f"紧急开关没跳过主源：{op.calls}"
        assert f"{uc.ENV_UPDATE_SOURCE}=github" in buf.getvalue(), "紧急开关没留痕"

        # ⑧ 摘要不符 ≠ 网络失败 → **本次不自动换源**（评审二审 §3：换源救不了「源头对象就是错的」）
        mismatch = uc.parse_manifest(manifest_payload("9.9.9", sha="f" * 64).decode(), EXE_NAME)
        tampered = b"tampered-" * 1000
        uc._opener = FakeOpener([FakeResp(tampered, url=mismatch.asset_url)])
        tried: list[int] = []

        def must_not_run() -> uc.ReleaseInfo:
            tried.append(1)
            raise AssertionError("摘要不符还去换源重试了")

        uc._MISMATCH_SEEN.discard("9.9.9")          # 让这条用例从干净状态开始
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                uc.download_and_verify(mismatch, tmp, fallback=must_not_run)
            raise AssertionError("摘要不符居然装上了")
        except uc.ChecksumMismatch as e:
            assert type(e) is uc.ChecksumMismatch, "摘要不符应当抛 ChecksumMismatch（子类）"
        assert not tried, "摘要不符触发了备用源重试"
        assert "摘要不符：本次不自动换源" in buf.getvalue(), f"没说明为什么不重试：{buf.getvalue()!r}"
        assert not (tmp / (EXE_NAME + uc.NEW_SUFFIX)).exists(), "摘要不符的残留没清"

        # ⑨ 用户再点一次「重试」→ **直接走备用源**（评审三审 R3：给这类失败留一条用户可达的出口）
        alt_bytes = b"fallback-after-mismatch-" * 100
        alt_sha = hashlib.sha256(alt_bytes).hexdigest()
        gh_url = f"{DL_BASE}/v9.9.9/{EXE_NAME}"
        op = FakeOpener([
            FakeResp(payload("v9.9.9", digest="sha256:" + alt_sha)),   # ① github_fallback 查 API
            FakeResp(alt_bytes, url=gh_url),                            # ② GitHub 附件
        ])
        uc._opener = op
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = uc.download_and_verify(mismatch, tmp, fallback=lambda: uc.github_fallback(mismatch))
        assert op.calls == [uc.RELEASES_LATEST_API, gh_url], \
            f"重试时又去下了主源（应当直接换源）：{op.calls}"
        assert out.read_bytes() == alt_bytes, "换源后落的不是备用源那份字节"
        assert "上一次是摘要不符 → 这次直接走备用源" in buf.getvalue(), \
            f"这次重试没留痕：{buf.getvalue()!r}"
        out.unlink()
        (tmp / uc.PENDING_JSON).unlink(missing_ok=True)
        uc._MISMATCH_SEEN.clear()

        # ⑩ 备用源也失败（大陆常见的 403 限流）→ 两因都留痕 + 异常链指向主因（评审二审 §1）
        prim2 = uc.parse_manifest(manifest_payload("9.9.9").decode(), EXE_NAME)
        uc._opener = FakeOpener([URLError("主源连不上：connection reset")])
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                uc.download_and_verify(prim2, tmp,
                                       fallback=lambda: uc.github_fallback(prim2))  # API 被限流 → 抛
            raise AssertionError("备用源也失败时居然返回了文件")
        except uc.UpdateCheckError as e:
            logs = buf.getvalue()
            assert "主源下载失败（" in logs, f"主源那次没留痕：{logs!r}"
            assert "备用源也失败" in logs, f"备用源那次没留痕：{logs!r}"
            assert "connection reset" in logs, f"主因被埋掉了：{logs!r}"
            assert isinstance(e.__cause__, uc.UpdateCheckError), \
                f"没有把主因串进异常链（__cause__={e.__cause__!r}）"
            assert "connection reset" in str(e.__cause__), f"链上的主因不对：{e.__cause__}"
        assert "9.9.9" not in uc._MISMATCH_SEEN, "网络类失败不该记进「摘要不符」名单"

        # ⑪ 非 Exception 的 BaseException（如 asyncio.CancelledError）**不该**被当成网络失败去兜底
        #    （评审三审 R1：`except BaseException` 会把这类信号误判成下载失败，白跑一次 37MB）
        class _NotAnException(BaseException):
            pass

        called2: list[int] = []

        def boom_alt() -> uc.ReleaseInfo:
            called2.append(1)
            raise AssertionError("非 Exception 的 BaseException 触发了备用源")

        class _WeirdResp:
            url = mismatch_prim_url = cf_url
            headers: dict = {}

            def read(self, n: int = -1) -> bytes:
                raise _NotAnException()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        uc._MISMATCH_SEEN.clear()
        uc._opener = MapOpener({cf_url: _WeirdResp()})
        try:
            uc.download_and_verify(prim, tmp, fallback=boom_alt)
            raise AssertionError("BaseException 被吞了")
        except _NotAnException:
            pass
        assert not called2, "非 Exception 的 BaseException 不该触发备用源重试"
        print("  下载回落 / 收窄异常 / release_url / 端口 / 紧急开关 / 摘要不重试 / 重试换源 / 两因留痕 / BaseException 不兜底 OK")
    finally:
        uc._opener = old
        uc._MISMATCH_SEEN.clear()       # 模块级状态：别泄给后面的用例


# ---------------------------------------------------------------- Task 3：忽略列表读写


def test_fmt_scalar() -> None:
    # 既有标量行为必须与 gui.py 里的同源函数一致（配置写入的回归线）
    assert uc._fmt_scalar(None) == "null"
    assert uc._fmt_scalar(True) == "true"
    assert uc._fmt_scalar(0.24) == "0.24"
    assert uc._fmt_scalar(3) == "3"
    # 本轮新增：list → 行内流式（忽略列表写 config.yaml 要用）
    assert uc._fmt_scalar(["0.1.2", "0.1.3"]) == "[0.1.2, 0.1.3]"
    assert uc._fmt_scalar([]) == "[]"
    print("  _fmt_scalar OK")


def test_ignore_list_roundtrip() -> None:
    tmp = OUT / "ignore"
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config.yaml"
    cfg.write_text("# 顶部注释\nsession:\n  model: m\n", encoding="utf-8")

    assert uc.load_ignored_versions(cfg) == []
    uc.add_ignored_version(cfg, "0.1.2")
    uc.add_ignored_version(cfg, "v0.1.3")           # 带 v 也规范化存放
    uc.add_ignored_version(cfg, "0.1.2")            # 重复加 → 不重复
    assert uc.load_ignored_versions(cfg) == ["0.1.2", "0.1.3"]

    text = cfg.read_text(encoding="utf-8")
    assert "session:" in text and "model: m" in text, "其它段被破坏"
    assert "# 顶部注释" in text, "注释被吃掉"
    assert "update_ignored: [0.1.2, 0.1.3]" in text, "不是行内流式列表写法"

    # 版本号不合法 → 明确报错，文件不动
    before = cfg.read_text(encoding="utf-8")
    try:
        uc.add_ignored_version(cfg, "not-a-version")
        raise AssertionError("坏版本号居然没报错")
    except uc.UpdateCheckError:
        pass
    assert cfg.read_text(encoding="utf-8") == before

    # 坏文件 / 缺失文件 → 空列表不崩
    assert uc.load_ignored_versions(tmp / "nope.yaml") == []
    cfg.write_text("ui: [\n", encoding="utf-8")
    assert uc.load_ignored_versions(cfg) == []
    # 坏 YAML 上 add → UpdateCheckError（绝不能把坏文件写得更坏）
    try:
        uc.add_ignored_version(cfg, "0.1.2")
        raise AssertionError("坏 YAML 居然能往里写")
    except uc.UpdateCheckError:
        pass
    print("  ignore list OK")


# ---------------------------------------------------------------- Task 4：决策 + 一次性检查入口


def test_should_prompt_and_check() -> None:
    info = uc.ReleaseInfo(tag="v0.2.0", version="0.2.0", html_url="h",
                          asset_url="e", sums_url="s")
    assert uc.should_prompt(info, "0.1.1", [])
    assert not uc.should_prompt(info, "0.1.1", ["0.2.0"])
    assert not uc.should_prompt(info, "0.2.0", [])

    old = uc._opener
    tmp = OUT / "check"
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config.yaml"
    cfg.unlink(missing_ok=True)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            # 1) 有更新 —— 走真实生产路径：主源（Cloudflare 清单）
            uc._opener = FakeOpener([FakeResp(manifest_payload("9.9.9"), url=uc.MANIFEST_URL)])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "update" and got is not None and got.version == "9.9.9"
            # 2) 已是最新
            uc._opener = FakeOpener([FakeResp(manifest_payload("0.1.1"), url=uc.MANIFEST_URL)])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "latest" and got is not None
            # 3) 在忽略列表
            uc.add_ignored_version(cfg, "9.9.9")
            uc._opener = FakeOpener([FakeResp(manifest_payload("9.9.9"), url=uc.MANIFEST_URL)])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "ignored" and got is not None
            # 4) 主源不通 → 回落 GitHub 依然能得出同样的结论（兜底路径也要端到端能用）
            uc._opener = FakeOpener([URLError("cf 连不上"), FakeResp(payload("v9.9.9"))])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "ignored" and got is not None, "主源挂了之后没回落到 GitHub"
            # 5) 两个源都挂：只留日志不抛、info=None
            uc._opener = FakeOpener([URLError("cf 连不上"), URLError("timed out")])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "error" and got is None
        logs = buf.getvalue()
        # 留痕是硬要求：成功/跳过/失败每个分支都要有一行 [update] 日志；用哪个源也要看得见
        for needle in ("检查中", "发现新版本 v9.9.9（当前 v0.1.1）", "已是最新",
                       "忽略列表，跳过", "检查失败", "更新源：Cloudflare", "回落 GitHub"):
            assert needle in logs, f"[update] 日志缺：{needle}"
        print("  should_prompt / check_for_updates OK")
    finally:
        uc._opener = old


# ---------------------------------------------------------------- Task 5：下载 + SHA256 + 白名单


def test_download_and_verify() -> None:
    exe_bytes = b"fake-exe-" * 1000
    good = hashlib.sha256(exe_bytes).hexdigest()
    sums = f"{good}  {EXE_NAME}\n".encode()
    exe_url = f"{DL_BASE}/v9.9.9/{EXE_NAME}"
    sums_url = f"{DL_BASE}/v9.9.9/{SUMS_NAME}"
    info = uc.ReleaseInfo(tag="v9.9.9", version="9.9.9", html_url="h",
                          asset_url=exe_url, sums_url=sums_url, asset_size=len(exe_bytes))
    tmp = OUT / "download"
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / (EXE_NAME + ".new")
    dest.unlink(missing_ok=True)

    old = uc._opener
    try:
        # 通过路径：内容一致、progress 按块回报、总量取 Content-Length
        uc._opener = MapOpener({
            sums_url: FakeResp(sums, url=sums_url),
            exe_url: FakeResp(exe_bytes,
                              url="https://objects.githubusercontent.com/real-exe",
                              headers={"Content-Length": str(len(exe_bytes))}),
        })
        calls: list[tuple[int, int | None]] = []
        out = uc.download_and_verify(info, tmp, progress=lambda d, t: calls.append((d, t)))
        assert out == dest and out.read_bytes() == exe_bytes
        assert calls and calls[-1] == (len(exe_bytes), len(exe_bytes)), f"progress 不对：{calls[-1:]}"
        assert all(t == len(exe_bytes) for _, t in calls)
        # 校验通过 → 写下 update_pending.json（【稍后】退出时替换 / 残留恢复的复验凭据）
        meta = json.loads((tmp / uc.PENDING_JSON).read_text(encoding="utf-8"))
        assert meta == {"version": "9.9.9", "sha256": good}, f"pending json 不对：{meta}"
        out.unlink()

        # sums 里多一行（AppImage；2026-10 起 `release.yml` 就是这么写的）→ 仍按附件名找到 exe 那行。
        # 这条钉的是**解析口径**：客户端是按附件名逐行找的，多出来的行不该影响取值
        # （维护者 2026-10 明确：< v0.2.0 的老客户端不在考虑范围内，这里只保自己这套口径不歪）
        two_line = (f"{good}  {EXE_NAME}\n"
                    f"{'0' * 64}  {APPIMAGE_NAME}\n").encode()
        uc._opener = MapOpener({sums_url: FakeResp(two_line, url=sums_url),
                                exe_url: FakeResp(exe_bytes, url=exe_url)})
        out = uc.download_and_verify(info, tmp)
        assert out.read_bytes() == exe_bytes, "两行 sums 里没取到 exe 的校验值"
        out.unlink()
        (tmp / uc.PENDING_JSON).unlink(missing_ok=True)

        # 没有 Content-Length → total=None（进度条降级显示，绝不除零）
        uc._opener = MapOpener({sums_url: FakeResp(sums, url=sums_url),
                                exe_url: FakeResp(exe_bytes, url=exe_url)})
        calls.clear()
        out = uc.download_and_verify(info, tmp, progress=lambda d, t: calls.append((d, t)))
        assert calls and all(t is None for _, t in calls) and calls[-1][0] == len(exe_bytes)
        out.unlink()

        # 校验值对不上 → UpdateCheckError 且下载物被删
        bad_good = good[:-1] + ("0" if good[-1] != "0" else "1")
        uc._opener = MapOpener({sums_url: FakeResp(f"{bad_good}  {EXE_NAME}\n".encode(),
                                                   url=sums_url),
                                exe_url: FakeResp(exe_bytes, url=exe_url)})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("校验不一致居然没报错")
        except uc.UpdateCheckError:
            pass
        assert not dest.exists(), "校验失败后下载物没被删除"

        # sums 里没有 exe 那一行 → 报错
        uc._opener = MapOpener({sums_url: FakeResp(b"deadbeef  other.bin\n", url=sums_url)})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("sums 缺行居然没报错")
        except uc.UpdateCheckError:
            pass

        # 最终落地地址跑出白名单 → 拦（响应里的 url 模拟被改写后的落地地址）
        uc._opener = MapOpener({sums_url: FakeResp(sums, url=sums_url),
                                exe_url: FakeResp(exe_bytes, url="https://evil.example.com/x")})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("白名单外的落地地址居然放行")
        except uc.UpdateCheckError:
            pass
        assert not dest.exists()

        # 下载中途断网 → UpdateCheckError + 无残留
        class _Boom:
            url = exe_url
            headers: dict = {}

            def read(self, n: int = -1) -> bytes:
                raise URLError("connection reset")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        uc._opener = MapOpener({sums_url: FakeResp(sums, url=sums_url), exe_url: _Boom()})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("中途断网居然没报错")
        except uc.UpdateCheckError:
            pass
        assert not dest.exists(), "中途失败后残留没被清理"

        # ★ AppImage：同一个下载器，落点跟着 asset_name 走（不写死 exe 名字）
        img_bytes = b"fake-appimage-" * 1000
        img_sha = hashlib.sha256(img_bytes).hexdigest()
        img_url = f"{DL_BASE}/v9.9.9/{APPIMAGE_NAME}"
        img_info = uc.ReleaseInfo(tag="v9.9.9", version="9.9.9", html_url="h",
                                  asset_url=img_url, asset_name=APPIMAGE_NAME,
                                  asset_digest=img_sha, asset_size=len(img_bytes))
        # 映射里只有 AppImage 一个地址：校验值取 digest 就够，**一次网络都不该多走**
        uc._opener = MapOpener({img_url: FakeResp(img_bytes, url=img_url)})
        img_dest = tmp / (APPIMAGE_NAME + ".new")
        img_dest.unlink(missing_ok=True)
        out = uc.download_and_verify(img_info, tmp)
        assert out == img_dest and out.read_bytes() == img_bytes, \
            f"AppImage 没下到 {img_dest}：{out}"
        meta = json.loads((tmp / uc.PENDING_JSON).read_text(encoding="utf-8"))
        assert meta == {"version": "9.9.9", "sha256": img_sha}, f"pending json 不对：{meta}"
        out.unlink()
        print("  download_and_verify OK")
    finally:
        uc._opener = old


def test_redirect_guard() -> None:
    h = uc._GuardedRedirectHandler()
    req = Request(f"{DL_BASE}/v9.9.9/{EXE_NAME}")

    # 白名单内的 https 跳转 → 放行（exe 下载正常就会 302 到 CDN）
    new = h.redirect_request(req, None, 302, "Found", {},
                             "https://objects.githubusercontent.com/the-real-asset")
    assert new is not None and new.full_url.startswith("https://objects.githubusercontent.com/")

    # 白名单外 host → 拦
    try:
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")
        raise AssertionError("跳到白名单外居然放行")
    except uc.UpdateCheckError:
        pass

    # 降级成 http → 拦（哪怕 host 在白名单里）
    try:
        h.redirect_request(req, None, 302, "Found", {}, "http://github.com/x")
        raise AssertionError("http 降级居然放行")
    except uc.UpdateCheckError:
        pass
    print("  redirect guard OK")


# ---------------------------------------------------------------- Task 6：运行形态判定


def test_update_mode_and_target() -> None:
    """运行形态判定：源码 / 单文件 exe / AppImage —— 以及「该换哪个文件」。"""
    assert uc.update_mode() in ("frozen", "appimage", "source")
    # 本测试在源码 checkout 里跑 → 必然是 source
    assert uc.update_mode() == "source", f"源码运行却判定成 {uc.update_mode()}"
    assert uc.update_target_path() is None, "源码运行没有可替换的单文件"
    assert not uc.can_self_update(), "源码运行不该被判成能自更新"
    assert uc.asset_name_for() == EXE_NAME

    tmp = OUT / "mode"
    tmp.mkdir(parents=True, exist_ok=True)
    img = tmp / APPIMAGE_NAME
    img.write_bytes(b"appimage")
    old_env = os.environ.get("APPIMAGE")
    try:
        # ★ 有 $APPIMAGE 且文件在 → appimage（能自更新）。以前这里被判成 source，
        #   界面于是告诉 AppImage 用户「在仓库目录跑 git pull 就是最新版」——他没有仓库。
        os.environ["APPIMAGE"] = str(img)
        assert uc.update_mode() == "appimage", "AppImage 形态没认出来"
        assert uc.can_self_update(), "AppImage 应当能自更新"
        assert uc.update_target_path() == img, f"该换的文件不是那个 AppImage：{uc.update_target_path()}"
        assert uc.asset_name_for() == APPIMAGE_NAME, "没按形态选附件"

        # 只有 APPDIR（--appimage-extract-and-run）：没有单文件可换 → 仍是 source
        os.environ.pop("APPIMAGE")
        os.environ["APPDIR"] = str(tmp)
        assert uc.update_mode() == "source", "解包运行不该被当成能自更新的 AppImage"

        # APPIMAGE 指向一个已经不存在的文件（用户挪走/删了）→ 也不能假装能替换
        os.environ["APPIMAGE"] = str(tmp / "gone.AppImage")
        assert uc.update_mode() == "source"
        assert uc.update_target_path() is None
    finally:
        os.environ.pop("APPDIR", None)
        if old_env is None:
            os.environ.pop("APPIMAGE", None)
        else:
            os.environ["APPIMAGE"] = old_env
    print("  update_mode / update_target_path / asset_name_for OK")


def test_update_mode_frozen_appimage() -> None:
    """★ 回归钉：PyInstaller 版 AppImage（frozen + $APPIMAGE）必须判成 appimage。

    只判 frozen 的话会被当成 Windows 形态：查更新去下 Windows 的 exe，
    替换目标指向只读挂载里的可执行文件。真机上就这么踩过一次。
    """
    tmp = OUT / "mode-frozen"
    tmp.mkdir(parents=True, exist_ok=True)
    img = tmp / APPIMAGE_NAME
    img.write_bytes(b"appimage")

    real_frozen = uc.is_frozen
    old_env = os.environ.get("APPIMAGE")
    try:
        uc.is_frozen = lambda: True                      # 假装是 PyInstaller 产物
        os.environ.pop("APPIMAGE", None)
        if sys.platform == "win32":
            # Windows 的 frozen 本来就等于发行形态（单文件 exe）→ 仍是 frozen
            assert uc.update_mode() == "frozen", f"Windows frozen 被判成 {uc.update_mode()}"
            assert uc.can_self_update()
        else:
            # Linux 的 frozen 目录形态（onedir 直接跑）：没有单文件可换 → source（绝不能是 frozen！）
            assert uc.update_mode() == "source", \
                f"Linux frozen 目录形态被判成 {uc.update_mode()}（会去下 Windows exe）"
            assert uc.update_target_path() is None
            assert not uc.can_self_update()

        # frozen + APPIMAGE（Linux 的真实发行形态）→ appimage，且目标就是那个文件
        os.environ["APPIMAGE"] = str(img)
        assert uc.update_mode() == "appimage", \
            f"frozen AppImage 被判成 {uc.update_mode()}（先看 frozen 看的？）"
        assert uc.update_target_path() == img
        assert uc.asset_name_for() == APPIMAGE_NAME, "没按形态选对附件"
    finally:
        uc.is_frozen = real_frozen
        if old_env is None:
            os.environ.pop("APPIMAGE", None)
        else:
            os.environ["APPIMAGE"] = old_env
    print("  frozen(AppImage/目录) 形态判定 OK")


def test_install_appimage() -> None:
    """AppImage 就地替换：补可执行位、内容换成新版，**运行中的旧文件不受影响**。

    「运行中能换」是 Linux 自更新的根据：`os.replace` 只换目录项，旧 inode 仍被运行中的
    进程（和它的 squashfs 挂载）引用着 —— 这里用「打开着的旧文件句柄仍读到旧内容」
    直接把它钉住。Windows 恰好相反（目标被打开时 `os.replace` 直接 Access denied），
    所以那边才需要那个「等 PID 退出的更新器 bat」——这条差异也一并钉在这里，
    免得哪天有人把 Windows 也改成「直接换」还以为两端一样。
    """
    tmp = OUT / "install"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    current = tmp / APPIMAGE_NAME
    new = tmp / (APPIMAGE_NAME + ".new")
    current.write_bytes(b"old-version")
    new.write_bytes(b"new-version")
    new.chmod(0o644)                        # 下载来的文件没有可执行位

    if os.name == "posix":
        with open(current, "rb") as running:    # 模拟「正在运行的 AppImage」
            uc.install_appimage(new, current)
            assert running.read() == b"old-version", \
                "运行中的旧文件内容被改了 —— 旧 inode 必须还活着（不然更新会砸掉当前会话）"
    else:
        # Windows：打开着的文件换不动。必须**失败得干干净净**（旧文件原样、下载物还在），
        # 而不是换一半 —— 这正是 Windows 侧要写 .bat 等 PID 退出的原因。
        with open(current, "rb") as running:
            try:
                uc.install_appimage(new, current)
                raise AssertionError("Windows 上居然把打开着的文件换掉了 —— 语义变了？")
            except uc.UpdateCheckError as e:
                assert "替换" in str(e), f"错误提示没提替换：{e}"
            assert running.read() == b"old-version"
            assert current.read_bytes() == b"old-version", "失败时旧文件必须原样不动"
            assert new.exists(), "失败时下载物该留着（用户还能重试）"
        # 文件关掉之后就能正常换（Windows 的「等 PID 退出」就是在等这个时刻）
        uc.install_appimage(new, current)

    assert not new.exists(), "新版文件应当被 rename 顶替（不再存在）"
    assert current.read_bytes() == b"new-version", "当前文件没换成新版"
    if os.name == "posix":
        assert current.stat().st_mode & 0o111, "换过去的 AppImage 必须可执行"

    # 目录不可写（比如装在 /opt、或放在只读挂载上）→ 明确的错，界面据此转手动下载。
    # ⚠️ Windows 上 chmod 不产生只读目录、root 又能无视权限位，所以只在「posix 非 root」下判。
    if os.name == "posix" and os.geteuid() != 0:
        ro = tmp / "ro"
        ro.mkdir()
        target = ro / APPIMAGE_NAME
        target.write_bytes(b"old")
        src = tmp / "src.new"
        src.write_bytes(b"new")
        ro.chmod(0o555)
        try:
            try:
                uc.install_appimage(src, target)
                raise AssertionError("只读目录居然替换成功了")
            except uc.UpdateCheckError as e:
                assert "替换" in str(e), f"错误提示没提替换：{e}"
            assert target.read_bytes() == b"old", "失败时旧文件必须原样不动"
            assert src.exists(), "失败时下载物不该被吃掉（用户还能重试）"
        finally:
            ro.chmod(0o755)
    else:
        print("  （跳过只读目录用例：Windows / root 下权限位不生效）")
    print("  install_appimage OK")


# ---------------------------------------------------------------- Task 7：更新器 bat 生成


def test_build_updater_bat() -> None:
    bat = uc.build_updater_bat(
        pid=4321,
        current_exe=Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe"),
        new_exe=Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe.new"))
    assert "\r\n" in bat, "bat 必须 CRLF"
    assert "\n" not in bat.replace("\r\n", ""), "混进了裸 LF（cmd 对 LF-only 的 label/goto 会抽风）"
    for needle in ("4321", r'"C:\Users\A B\Desktop\VRChatLiveTranslate.exe"',
                   r'"C:\Users\A B\Desktop\VRChatLiveTranslate.exe.new"',
                   "tasklist", "move /y", 'start ""', 'del "%~f0"', ".bak",
                   "update_failed.log", ":fail", "pause"):
        assert needle in bat, f"bat 缺 {needle}"
    # ★ 替换必须带重试（真机实测）：PyInstaller 单文件的**父进程**比子进程晚一步释放 exe 句柄，
    #   子进程一消失就 move 会撞共享冲突 → 替换失败回滚、用户升了个寂寞。没有这个循环就是回归。
    assert ":trymove" in bat and "goto trymove" in bat, "替换没有重试循环"
    assert ":restore" in bat, "重试用尽后应先从 .bak 还原再报失败"
    assert ">nul 2>&1" in bat, "重试时要把 move 的报错也吞掉（否则黑窗刷错误）"
    print("  build_updater_bat OK")


# ---------------------------------------------------------------- Task 11：阶段二纯逻辑


def test_build_updater_bat_relaunch() -> None:
    cur = Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe")
    new = Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe.new")
    bat_on = uc.build_updater_bat(pid=4321, current_exe=cur, new_exe=new, relaunch=True)
    bat_off = uc.build_updater_bat(pid=4321, current_exe=cur, new_exe=new, relaunch=False)

    # 【重载】形态：替换后 start "" 拉起新版；【稍后】退出时替换：只换不拉
    assert f'start "" "{cur}"' in bat_on, "relaunch=True 缺拉起行"
    assert 'start ""' not in bat_off, "relaunch=False 不该拉起新进程"
    # ★ 拉起新实例前必须清掉 PyInstaller 的内部变量（真机实测的启动失败根因）：
    #   新版本会带着 _PYI_PARENT_PROCESS_LEVEL=1 启动 → 引导器以为自己是已解包的子进程
    #   → 跳过自解包 → 起不来（不写日志、无窗口）。用户看到的就是「更新完没再打开」。
    for var in ("_MEIPASS", "_MEIPASS2", "_PYI_ARCHIVE_FILE",
                "_PYI_PARENT_PROCESS_LEVEL", "_PYI_APPLICATION_HOME_DIR"):
        assert f'set "{var}="' in bat_on, f"拉起前没有清 {var} —— 新实例会起不来"
        assert f'set "{var}="' not in bat_off, f"relaunch=False 不该出现清理 {var} 的行"
    # 两形态除「拉起块（清理 + start 行）」外其余逐字节相同
    extra = [ln for ln in bat_on.splitlines(keepends=True) if ln not in bat_off]
    assert extra, "两形态居然完全一样"
    assert all(ln.startswith(('start ""', 'set "_', "rem drop"))
               for ln in extra), f"多出来不只是拉起块：{extra}"
    stripped = bat_on
    for ln in extra:
        stripped = stripped.replace(ln, "", 1)
    assert stripped == bat_off, "两形态差异不止拉起块"
    for needle in ("tasklist", "move /y", 'del "%~f0"', ".bak", ":fail"):
        assert needle in bat_off, f"relaunch=False 缺 {needle}"
    # 默认参数必须保持 True（既有行为不变）
    assert uc.build_updater_bat(pid=1, current_exe=cur, new_exe=new) == bat_on.replace(
        "4321", "1"), "默认值不是 relaunch=True"
    print("  build_updater_bat relaunch 两形态 OK")


def test_pending_download() -> None:
    tmp = OUT / "pending"
    tmp.mkdir(parents=True, exist_ok=True)
    exe = tmp / EXE_NAME
    exe.write_bytes(b"old")                          # 当前 exe（内容无所谓）
    new_exe = tmp / (EXE_NAME + ".new")
    pending = tmp / uc.PENDING_JSON
    new_exe.unlink(missing_ok=True)
    pending.unlink(missing_ok=True)

    # 什么都没有 → None，且一行日志都不打（每次启动都走的正常路）
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe) is None
    assert buf.getvalue() == "", f"无残留不该刷日志：{buf.getvalue()!r}"

    # 完好：写真 bytes + write_pending → 复核通过，返回 (version, sha256)
    body = b"new-exe-" * 1000
    new_exe.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    p = uc.write_pending(tmp, "9.9.9", sha)
    assert p == pending and json.loads(p.read_text(encoding="utf-8")) == {
        "version": "9.9.9", "sha256": sha}
    buf = io.StringIO()
    with redirect_stdout(buf):
        hit = uc.check_pending_download(exe, current_version="0.1.1")
    assert hit == ("9.9.9", sha), f"完好残留没命中：{hit}"
    assert "复核通过" in buf.getvalue() and "9.9.9" in buf.getvalue(), "命中没留痕"
    assert new_exe.exists() and pending.exists(), "命中后文件不该被动"

    # 损坏：改一个字节 → None，两个文件都被清理，留痕
    new_exe.write_bytes(body + b"x")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe, current_version="0.1.1") is None
    assert not new_exe.exists() and not pending.exists(), "损坏残留没被清理"
    assert "复核不通过" in buf.getvalue(), "损坏分支没留痕"

    # json 损坏 / 缺字段 → None + 清理 + 留痕
    for bad_json in ("{not json", json.dumps({"version": "9.9.9"}),  # 缺 sha256
                     json.dumps({"version": "not-a-version", "sha256": sha}),
                     json.dumps({"version": "9.9.9", "sha256": "abc"})):
        new_exe.write_bytes(body)
        pending.write_text(bad_json, encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert uc.check_pending_download(exe) is None, f"坏 json 居然放行：{bad_json!r}"
        assert not new_exe.exists() and not pending.exists()
        assert "记录文件损坏" in buf.getvalue(), f"坏 json 分支没留痕：{bad_json!r}"

    # 只剩 json / 只剩 .new → None + 清理 + 留痕（两种残缺都不能放行）
    pending.write_text(json.dumps({"version": "9.9.9", "sha256": sha}), encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe) is None
    assert not pending.exists() and "下载物缺失" in buf.getvalue()

    new_exe.write_bytes(body)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe) is None
    assert not new_exe.exists() and "缺少记录文件" in buf.getvalue()

    # 待替换版本不比当前新（上次其实换成功了，只是残留没清）→ 清理 + 留痕
    new_exe.write_bytes(body)
    uc.write_pending(tmp, "9.9.9", sha)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe, current_version="9.9.9") is None
    assert not new_exe.exists() and not pending.exists()
    assert "不比当前" in buf.getvalue(), "换完残留分支没留痕"
    # 当前版本号不是严格 X.Y.Z（测试版/预发布版，如 0.4.0-beta.1）→ 新旧无法比较，
    # ⚠️ 绝不能当成「换完剩下的残留」删掉（is_newer 对不可解析版本一律返回 False，
    #    那是防御性默认值，不是「不比当前新」的结论）。
    new_exe.write_bytes(body)
    uc.write_pending(tmp, "9.9.9", sha)
    buf = io.StringIO()
    with redirect_stdout(buf):
        hit = uc.check_pending_download(exe, current_version="0.4.0-beta.1")
    assert hit is not None, "当前版本号不可解析时，校验完好的待更新文件被误判成残留丢弃"
    assert new_exe.exists() and pending.exists(), "文件不该被清掉"
    assert "无法比较" in buf.getvalue(), "无法比较分支没留痕"
    new_exe.unlink()
    pending.unlink()

    # ★ 同一套残留逻辑对 AppImage 同样成立：.new 的名字从**安装文件**推出来
    #   （VRChatLiveTranslate-x86_64.AppImage.new），不写死 exe 那一套。
    img = tmp / APPIMAGE_NAME
    img.write_bytes(b"old-img")
    img_new = tmp / (APPIMAGE_NAME + ".new")
    img_new.write_bytes(body)
    uc.write_pending(tmp, "9.9.9", sha)
    buf = io.StringIO()
    with redirect_stdout(buf):
        hit = uc.check_pending_download(img, current_version="0.1.1")
    assert hit == ("9.9.9", sha), f"AppImage 残留没命中：{hit}"
    assert "复核通过" in buf.getvalue()
    img_new.unlink()
    pending.unlink()

    print("  write_pending / check_pending_download OK")


def test_last_seen_version() -> None:
    tmp = OUT / "state"
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / uc.STATE_JSON
    state.unlink(missing_ok=True)

    assert uc.load_last_seen_version(tmp) is None, "缺失文件应视为首次"
    uc.save_last_seen_version(tmp, "0.1.1")
    assert uc.load_last_seen_version(tmp) == "0.1.1"
    uc.save_last_seen_version(tmp, "0.1.2")            # 覆盖写（提示后写回当前版本）
    assert uc.load_last_seen_version(tmp) == "0.1.2"
    state.write_text("{broken", encoding="utf-8")
    assert uc.load_last_seen_version(tmp) is None, "损坏文件应视为首次"
    state.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert uc.load_last_seen_version(tmp) is None, "缺字段应视为首次"
    print("  load/save_last_seen_version OK")


def test_expected_sha256_prefers_github_digest() -> None:
    """校验值优先取 GitHub 的 `assets[].digest`；只有老 Release 没 digest 才回退拉 SHA256SUMS.txt。

    这条守的是「删掉 SHA256SUMS.txt 之后更新链路还能用」——真机实测就是这么坏的：
    旧逻辑硬要求两个附件，官方 Release 不再上传 sums 后，更新检查在真 exe 上直接静默失败
    （用户什么提示都看不到，等于永远收不到新版本）。
    """
    old = uc._opener
    try:
        info = uc.ReleaseInfo(tag="v1.0.0", version="1.0.0", html_url="h",
                              asset_url=f"{DL_BASE}/v1.0.0/{EXE_NAME}", asset_digest="b" * 64)
        uc._opener = MapOpener({})          # 空映射：有 digest 时**一次网络都不该走**
        assert uc._expected_sha256(info, 1.0) == "b" * 64, "有 digest 时不该再去拉文件"

        legacy = uc.ReleaseInfo(tag="v1.0.0", version="1.0.0", html_url="h",
                                asset_url=f"{DL_BASE}/v1.0.0/{EXE_NAME}",
                                sums_url=f"{DL_BASE}/v1.0.0/{SUMS_NAME}")
        uc._opener = MapOpener({f"{DL_BASE}/v1.0.0/{SUMS_NAME}":
                                FakeResp(f"{'c' * 64}  {EXE_NAME}\n".encode(),
                                         url=f"{DL_BASE}/v1.0.0/{SUMS_NAME}")})
        assert uc._expected_sha256(legacy, 1.0) == "c" * 64, "没 digest 时应回退到 sums 文件"
        print("  _expected_sha256 OK（GitHub digest 优先，sums 仅老版本兜底）")
    finally:
        uc._opener = old


def main() -> int:
    print("test_update_check:")
    tests = [
        test_parse_version,
        test_is_newer,
        test_fetch_latest_release_github,
        test_manifest_source,
        test_download_fallback_and_guards,
        test_fmt_scalar,
        test_ignore_list_roundtrip,
        test_should_prompt_and_check,
        test_download_and_verify,
        test_expected_sha256_prefers_github_digest,
        test_redirect_guard,
        test_update_mode_and_target,
        test_update_mode_frozen_appimage,
        test_install_appimage,
        test_build_updater_bat,
        test_build_updater_bat_relaunch,
        test_pending_download,
        test_last_seen_version,
    ]
    bad = []
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001 — 任何一个断言挂都要退出码非 0，但其余用例照跑
            bad.append(fn.__name__)
            print(f"  ✗ {fn.__name__} 失败：")
            traceback.print_exc()
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
