"""scripts/publish_r2.py 的离线测试。

为什么这些用例值得存在：

  * `latest.json` 是**发布侧与客户端之间唯一的契约** —— 这里做「发布 → 客户端解析」的往返对账
    （`build_manifest` → `manifest_bytes` → `vlt.update_check.parse_manifest`），
    任何一边单方面改字段，这里立刻红；
  * `verify_public()` 是发布门禁的最后一道（回答「用户拿到的到底是不是我们发的那些字节」）。
    用假 opener 注入「清单版本不对 / 摘要不对 / size 不对 / 产物下不来 / 清单取不到 / 域名被改写」
    六种形态，断言它**必须报错** —— 否则它就成了那种打印一行 ✅ 却什么都没验的装饰；
  * 缓存那条（固定键 + `Cache-Control: no-cache`）是实测结论（同键覆盖后 CDN 仍回旧内容），
    写死断言，免得后人把它「优化」回默认值。

跑法：.venv/Scripts/python.exe tests/test_publish_r2.py
全程离线：不连 R2、不起网络（上传器与 HTTP 层都可注入）。
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import re
import shutil
import sys
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

from vlt import update_check as uc                  # noqa: E402

pr = importlib.import_module("scripts.publish_r2")

OUT = ROOT / "out" / "publish_r2"


# ---------------------------------------------------------------- 测试替身


class FakeResp:
    """冒充 urllib 的响应：read 可分块、有 headers、能当上下文管理器。"""

    def __init__(self, body: bytes, url: str, headers: dict | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.url = url
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """按 URL 分发的假 opener：值是 FakeResp 就回响应，是 Exception 就抛。

    签名与 `urllib.request.urlopen(req, timeout=...)` 一致（publish_r2 就是这么调的）。
    """

    def __init__(self, mapping: dict) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001, ANN201
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        item = self.mapping.get(url)
        if item is None:
            raise AssertionError(f"假网络没准备这个地址：{url}")
        if isinstance(item, Exception):
            raise item
        return item


def make_dist(tmp: Path, *, exe: bytes = b"fake-exe-" * 100, img: bytes = b"fake-image-" * 100,
              sums: bytes | None = None, sums_mode: str = "both") -> Path:
    """铺一个 dist/：两个必需产物 + `SHA256SUMS.txt`。

    `sums_mode` 决定那个文件长什么样（本仓库 2026-10 起的口径是**两个平台各一行**）：
      * `both`（默认）：exe + AppImage 各一行，值都对；
      * `exe-only`    ：老口径（只有 exe 一行）→ 现在的门禁应当判红；
      * `wrong`       ：exe 那行的值写错 → 判红；
      * `none`        ：不带这个文件（它是可选对象，不该拦住发布）。
    """
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / pr.EXE_NAME).write_bytes(exe)
    (tmp / pr.APPIMAGE_NAME).write_bytes(img)
    if sums_mode == "none":
        (tmp / pr.SUMS_NAME).unlink(missing_ok=True)
        return tmp
    if sums is None:
        h_exe = hashlib.sha256(exe).hexdigest()
        h_img = hashlib.sha256(img).hexdigest()
        if sums_mode == "wrong":
            h_exe = "0" * 64
        lines = [f"{h_exe}  {pr.EXE_NAME}"]
        if sums_mode in ("both", "crlf"):
            lines.append(f"{h_img}  {pr.APPIMAGE_NAME}")
        sep = "\r\n" if sums_mode == "crlf" else "\n"
        sums = (sep.join(lines) + sep).encode()
    (tmp / pr.SUMS_NAME).write_bytes(sums)
    return tmp


def good_manifest(tag: str = "v9.9.9", dist: Path | None = None) -> dict:
    """直接把产物读成清单（复核用例的输入）。"""
    assert dist is not None
    return pr.build_manifest(dist, tag)


def bad_size_manifest(dist: Path) -> dict:
    """清单里 size 被写错、字节其实没变（sha 仍对得上）—— 只靠 sha 判不出来时 size 这条要兜住。"""
    m = pr.build_manifest(dist, "v9.9.9")
    m["assets"][pr.EXE_NAME]["size"] += 1
    return m


# ---------------------------------------------------------------- 契约对账


def test_contract_with_client() -> None:
    """发布侧与客户端的两份常量必须逐项对得上（改一边漏另一边 → 这里红）。"""
    assert f"{pr.BASE_URL}/{pr.MANIFEST_KEY}" == uc.MANIFEST_URL, \
        f"清单地址对不上：{pr.BASE_URL}/{pr.MANIFEST_KEY} vs {uc.MANIFEST_URL}"
    assert urlsplit(pr.BASE_URL).hostname == uc.MANIFEST_HOST
    assert uc.MANIFEST_HOST in uc.ALLOWED_HOSTS, "客户端的下载白名单里没有这个域 → 主源会被自己拦掉"
    assert pr.MANIFEST_SCHEMA == uc.MANIFEST_SCHEMA, "清单格式版本两边不一致"
    assert (pr.EXE_NAME, pr.APPIMAGE_NAME, pr.SUMS_NAME) == \
        (uc.EXE_ASSET_NAME, uc.APPIMAGE_ASSET_NAME, uc.SUMS_ASSET_NAME), "产物名两边不一致"
    # 固定键覆盖式发布必须配 no-cache：默认的 max-age=14400 实测会让 CDN 回上一版
    assert pr.CACHE_CONTROL == "no-cache", \
        "Cache-Control 变了：固定键覆盖后 CDN 会回旧内容（实测 max-age=14400 → HIT 旧字节）"
    # release.yml 里那条发布步骤：产物名 / 脚本 / 凭据键都要在（改错了没人会知道，除了这里）
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for needle in ("scripts/publish_r2.py", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                   pr.EXE_NAME, pr.APPIMAGE_NAME, pr.SUMS_NAME, pr.MANIFEST_KEY):
        assert needle in wf, f"release.yml 里没有 {needle}（发布链路的契约）"
    # ★ 发布门禁：release 必须**依赖** publish-r2。去掉它 = R2 传不上去也照发 Release，
    #   而客户端主要从 R2 收更新 → 「发了版但用户收不到」这种失败会静默通过。
    assert re.search(r"\brelease:\s*\n\s*needs:\s*\[[^\]]*publish-r2[^\]]*\]", wf), \
        "release job 没有依赖 publish-r2（发布门禁被摘了）"
    print("  contract（客户端 ↔ 发布脚本 ↔ release.yml）OK")


# ---------------------------------------------------------------- 清单生成


def test_build_manifest() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    dist = make_dist(OUT / "dist")
    m = pr.build_manifest(dist, "v9.9.9")
    assert m["schema"] == pr.MANIFEST_SCHEMA and m["version"] == "9.9.9" and m["tag"] == "v9.9.9"
    assert m["release_url"] == f"https://github.com/{pr.REPO_SLUG}/releases/tag/v9.9.9"
    assert set(m["assets"]) == {pr.EXE_NAME, pr.APPIMAGE_NAME, pr.SUMS_NAME}, m["assets"]
    exe = m["assets"][pr.EXE_NAME]
    assert exe["url"] == f"{pr.BASE_URL}/{pr.EXE_NAME}"
    assert exe["size"] == (dist / pr.EXE_NAME).stat().st_size
    assert exe["sha256"] == hashlib.sha256((dist / pr.EXE_NAME).read_bytes()).hexdigest()
    assert m["published_at"].endswith("Z") and len(m["published_at"]) == 20, m["published_at"]

    # 产物缺一个 → 整批失败（客户端按形态各要一个，少一半等于一半用户收不到更新）
    (dist / pr.APPIMAGE_NAME).unlink()
    try:
        pr.build_manifest(dist, "v9.9.9")
        raise AssertionError("缺 AppImage 居然还能生成清单")
    except pr.PublishError as e:
        assert pr.APPIMAGE_NAME in str(e), f"提示没点名缺哪个产物：{e}"
    make_dist(dist)                             # 还原

    # 空文件 → 失败（半截上传的典型形态）
    (dist / pr.EXE_NAME).write_bytes(b"")
    try:
        pr.build_manifest(dist, "v9.9.9")
        raise AssertionError("空产物居然还能生成清单")
    except pr.PublishError:
        pass
    make_dist(dist)

    # tag 形态：客户端只认严格 X.Y.Z 的 version，清单里写别的形态 = 这一版永远收不到更新
    for bad in ("9.9", "v9.9.9-beta", "v09.9.9", "v9.9.9.9", ""):
        try:
            pr.build_manifest(dist, bad)
            raise AssertionError(f"坏 tag {bad!r} 居然通过了")
        except pr.PublishError:
            pass

    # 大小写前缀都认，且**只规范化一次**：`V9.9.9` 不能生成 `tag: "vV9.9.9"`（评审 §三）
    up = pr.build_manifest(dist, "V9.9.9")
    assert up["tag"] == "v9.9.9" and up["version"] == "9.9.9", up
    assert up["release_url"].endswith("/tag/v9.9.9"), up["release_url"]
    assert pr.build_manifest(dist, "9.9.9")["tag"] == "v9.9.9"

    # 域名/端口口径（评审 §三）：`same_host` 比的是完整 origin，不是前缀、也不放过非默认端口
    assert pr.same_host(f"{pr.BASE_URL}/{pr.EXE_NAME}", pr.BASE_URL)
    assert not pr.same_host("https://dl.kcm-nixi.cn.evil.com/x", pr.BASE_URL)
    assert not pr.same_host("https://dl.kcm-nixi.cn:8443/x", pr.BASE_URL)
    assert not pr.same_host("http://dl.kcm-nixi.cn/x", pr.BASE_URL)

    # SHA256SUMS.txt 是可选的：Release 里没带它也不该拦住发布
    make_dist(dist, sums_mode="none")
    assert set(pr.build_manifest(dist, "v9.9.9")["assets"]) == {pr.EXE_NAME, pr.APPIMAGE_NAME}
    print("  build_manifest OK（含缺产物 / 空文件 / 坏 tag / 可选 sums）")


def test_sums_file_gate() -> None:
    """`SHA256SUMS.txt` 的门禁（评审二审 §2）：两个平台各一行、值必须与产物一致。

    背景：GUIDE 让用户拿这个文件核校验值，而 AppImage 的 sha256 以前只在 `latest.json` 里 ——
    Linux 用户照文档核不了。现在追加了这一行，于是**内容错了比没有更坏**，必须卡门禁。
    """
    tmp = OUT / "sums_gate"

    # 两行都对 → 通过，且清单里也带上了这个可选对象
    dist = make_dist(tmp, sums_mode="both")
    m = pr.build_manifest(dist, "v9.9.9")
    assert pr.SUMS_NAME in m["assets"], "两行齐全时这个文件仍应作为可选对象进清单"

    # 只有 exe 一行（老口径）→ 判红，且点名缺的是哪一行
    dist = make_dist(tmp, sums_mode="exe-only")
    try:
        pr.build_manifest(dist, "v9.9.9")
        raise AssertionError("只有 exe 一行的 sums 居然通过了")
    except pr.PublishError as e:
        assert pr.APPIMAGE_NAME in str(e) and "缺这一行" in str(e), f"提示没点名缺哪行：{e}"

    # 值写错 → 判红
    dist = make_dist(tmp, sums_mode="wrong")
    try:
        pr.build_manifest(dist, "v9.9.9")
        raise AssertionError("校验值写错的 sums 居然通过了")
    except pr.PublishError as e:
        assert "不一致" in str(e), f"提示没说清是对不上：{e}"

    # 不带这个文件 → 不拦（可选对象）
    dist = make_dist(tmp, sums_mode="none")
    pr.build_manifest(dist, "v9.9.9")

    # CRLF 行尾 → 判红：Linux 侧 `sha256sum -c` 会把 "\r" 当成文件名的一部分，整份核不过 ——
    # 而这份文件的读者正是 Linux 用户（GUIDE 让他们拿它核 AppImage）。CI 里 `Out-File`
    # 写的恰好就是 CRLF（实测踩到），所以生成侧改成显式写 LF，这里钉住不许退回去。
    dist = make_dist(tmp, sums_mode="crlf")
    try:
        pr.build_manifest(dist, "v9.9.9")
        raise AssertionError("CRLF 行尾的 sums 居然通过了")
    except pr.PublishError as e:
        assert "CRLF" in str(e), f"提示没说清是行尾问题：{e}"

    # 同名重复一行 → 判红：客户端命中即取**第一条**，门禁若取最后一条就会「两边读法不同」
    # （评审三审 R4）。生成侧不会产生重复行，所以直接判错，不留隐患。
    dist = make_dist(tmp, sums_mode="both")
    with (dist / pr.SUMS_NAME).open("ab") as f:
        f.write(f"{'f' * 64}  {pr.EXE_NAME}\n".encode())
    try:
        pr.build_manifest(dist, "v9.9.9")
        raise AssertionError("同名重复行的 sums 居然通过了")
    except pr.PublishError as e:
        assert "不止一行" in str(e), f"提示没说清是重复行：{e}"

    # 多余的行（如将来加第三个产物）→ 忽略，不影响判据（与老客户端的解析口径一致）
    dist = make_dist(tmp, sums_mode="both")
    with (dist / pr.SUMS_NAME).open("ab") as f:
        f.write(b"deadbeef  something-else.bin\n")
    pr.build_manifest(dist, "v9.9.9")
    print("  SHA256SUMS.txt 门禁 OK（两行齐全 / 缺行 / 值错 / 缺失 / CRLF / 同名重复 / 多余行）")


def test_published_manifest_parses_by_client() -> None:
    """发布侧生成的清单，客户端必须能原样解析出**同一份**产物信息（往返对账）。"""
    dist = make_dist(OUT / "roundtrip")
    text = pr.manifest_bytes(pr.build_manifest(dist, "v9.9.9")).decode("utf-8")
    info = uc.parse_manifest(text, uc.EXE_ASSET_NAME)
    assert info.version == "9.9.9" and info.tag == "v9.9.9"
    assert info.asset_name == uc.EXE_ASSET_NAME
    assert info.asset_url == f"{pr.BASE_URL}/{pr.EXE_NAME}"
    assert info.asset_size == (dist / pr.EXE_NAME).stat().st_size
    assert info.asset_digest == hashlib.sha256((dist / pr.EXE_NAME).read_bytes()).hexdigest()
    # Linux 形态走同一个清单取另一份附件
    img = uc.parse_manifest(text, uc.APPIMAGE_ASSET_NAME)
    assert img.asset_name == uc.APPIMAGE_ASSET_NAME and img.asset_digest != info.asset_digest
    print("  build_manifest → parse_manifest 往返 OK")


# ---------------------------------------------------------------- 公开域复核（发布门禁）


def test_verify_public_catches_bad_publishes() -> None:
    """六种「上传成功但用户拿到的不是这个」的形态，验伪器都必须报错。"""
    dist = make_dist(OUT / "verify")
    m = good_manifest("v9.9.9", dist)
    exe_bytes = (dist / pr.EXE_NAME).read_bytes()
    img_bytes = (dist / pr.APPIMAGE_NAME).read_bytes()
    m_url = f"{pr.BASE_URL}/{pr.MANIFEST_KEY}"
    exe_url = f"{pr.BASE_URL}/{pr.EXE_NAME}"
    img_url = f"{pr.BASE_URL}/{pr.APPIMAGE_NAME}"

    def net(manifest, *, exe=exe_bytes, img=img_bytes):
        if isinstance(manifest, Exception):
            m_entry = manifest
        else:
            mbody = manifest if isinstance(manifest, bytes) else pr.manifest_bytes(manifest)
            m_entry = FakeResp(mbody, m_url, {"cf-cache-status": "REVALIDATED", "age": "0"})
        return FakeNet({m_url: m_entry,
                        exe_url: FakeResp(exe, exe_url) if not isinstance(exe, Exception) else exe,
                        img_url: FakeResp(img, img_url) if not isinstance(img, Exception) else img})

    # ① 全对 → 通过（顺带把清单响应头里的缓存状态打出来，便于判断「是不是缓存给的旧版本」）
    buf = io.StringIO()
    from contextlib import redirect_stdout
    with redirect_stdout(buf):
        got = pr.verify_public("v9.9.9", opener=net(m))
    assert got["version"] == "9.9.9" and set(got["assets"]) >= {pr.EXE_NAME, pr.APPIMAGE_NAME}
    assert "sdk" not in buf.getvalue()             # 只确认没崩；具体文案不钉死
    assert "REVALIDATED" in buf.getvalue(), "复核输出里应当带上缓存状态"

    cases = [
        ("清单还是上一版（覆盖没生效 / CDN 回旧）", net(pr.build_manifest(dist, "v9.9.8")),
         "版本"),
        ("产物字节被换掉", net(m, exe=b"tampered" * 100), "sha256"),
        ("产物被截断（size 对不上）", net(m, img=img_bytes[: len(img_bytes) // 2]), "sha256"),
        ("清单里的 size 与实测不符", net(bad_size_manifest(dist)), "size"),
        ("产物取不到（404）",
         net(m, exe=HTTPError(exe_url, 404, "Not Found", None, io.BytesIO(b""))), "下载"),
        ("清单取不到", net(HTTPError(m_url, 404, "Not Found", None, io.BytesIO(b"")), exe=exe_bytes,
                          img=img_bytes), "清单"),
        ("清单不是 JSON", net(b"not json"), "JSON"),
    ]
    for name, opener, needle in cases:
        try:
            with redirect_stdout(io.StringIO()):
                pr.verify_public("v9.9.9", opener=opener)
            raise AssertionError(f"{name} 居然复核通过")
        except pr.PublishError as e:
            assert needle in str(e), f"{name} 的报错没说到点子上（期望含 {needle!r}）：{e}"

    # ⑦ 清单里的产物地址被改到别的域 → 必须在**下载之前**就拦（用主机名逐字比，别用 startswith：
    #    https://dl.kcm-nixi.cn.evil.com/x 能骗过 startswith）。两条判据缺一不可：
    #    ① 报错要说到点上（"不在公开域下"；别拿外层那句 "公开域名复核不通过" 当 needle —— 它
    #       连「下载失败」这种别的问题也会带上，会假绿，实测踩过）；
    #    ② **一次下载都不许发起**（假网络的 calls 就是物证）。
    evil = good_manifest("v9.9.9", dist)
    evil["assets"][pr.EXE_NAME]["url"] = "https://dl.kcm-nixi.cn.evil.com/VRChatLiveTranslate.exe"
    evil_net = net(evil)
    try:
        with redirect_stdout(io.StringIO()):
            pr.verify_public("v9.9.9", opener=evil_net)
        raise AssertionError("产物地址被改到别的域居然复核通过")
    except pr.PublishError as e:
        assert "不在公开域下" in str(e), f"报错没点到域名这一项：{e}"
    assert all("evil.com" not in c for c in evil_net.calls), \
        f"域名不对却已经去取了：{evil_net.calls}"
    assert not pr.same_host("https://dl.kcm-nixi.cn.evil.com/x", pr.BASE_URL)
    assert pr.same_host(f"{pr.BASE_URL}/{pr.EXE_NAME}", pr.BASE_URL)

    # ⑧ --no-assets：只核清单（快），不该去下载产物
    fast = net(m)
    with redirect_stdout(io.StringIO()):
        pr.verify_public("v9.9.9", with_assets=False, opener=fast)
    assert fast.calls == [m_url], f"--no-assets 居然还下载了产物：{fast.calls}"

    # ⑨ 清单下发不下来（网络层异常）→ 也要报错，不能静默当成功
    try:
        with redirect_stdout(io.StringIO()):
            pr.verify_public("v9.9.9", opener=FakeNet({m_url: URLError("dns 挂了")}))
        raise AssertionError("清单请求失败居然复核通过")
    except pr.PublishError:
        pass
    print("  verify_public OK（版本 / 摘要 / size / 下载失败 / 域名改写 / --no-assets）")


def test_manifest_cli_writes_file() -> None:
    """CLI 形态：`manifest --out` 落盘的内容与 stdout 一致（CI 与本机都靠它）。"""
    dist = make_dist(OUT / "cli")
    out = OUT / "cli" / "latest.json"
    args = type("A", (), {"dist": str(dist), "tag": "v9.9.9", "base_url": pr.BASE_URL,
                          "out": str(out)})()
    buf = io.StringIO()
    from contextlib import redirect_stdout
    with redirect_stdout(buf):
        assert pr.cmd_manifest(args) == 0
    text = out.read_text(encoding="utf-8")
    assert json.loads(text)["version"] == "9.9.9"
    assert text.endswith("\n") and "\r\n" not in text, "清单必须是 UTF-8/LF（CRLF 会让某些客户端多带一个 \\r）"
    assert uc.parse_manifest(text, uc.EXE_ASSET_NAME).version == "9.9.9"
    print("  cmd_manifest OK")


def main() -> int:
    print("test_publish_r2:")
    tests = [
        test_contract_with_client,
        test_build_manifest,
        test_published_manifest_parses_by_client,
        test_verify_public_catches_bad_publishes,
        test_manifest_cli_writes_file,
        test_sums_file_gate,
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
