"""vlt/update_check.py 的离线测试（覆盖计划 Task 1~7 的全部纯逻辑）。

跑法：.venv/Scripts/python.exe tests/test_update_check.py

全程离线：HTTP 层一律用假 opener 注入（整体替换模块级 `uc._opener`），
绝不真连 GitHub —— CI 机器不一定能连外网。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

from vlt import update_check as uc                  # noqa: E402

OUT = ROOT / "out" / "update_check"
EXE_NAME = "VRChatLiveTranslate.exe"
SUMS_NAME = "SHA256SUMS.txt"
DL_BASE = "https://github.com/nixi-agent/vrchat-livetranslate/releases/download"


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


def payload(tag: str = "v9.9.9", *, exe_size: int | None = 12345678, drop: str = "") -> bytes:
    """造一份 GitHub /releases/latest 的 JSON。drop='exe'/'sums' 可让附件缺一个。"""
    assets = []
    if drop != "exe":
        a = {"name": EXE_NAME, "browser_download_url": f"{DL_BASE}/{tag}/{EXE_NAME}"}
        if exe_size is not None:
            a["size"] = exe_size
        assets.append(a)
    if drop != "sums":
        assets.append({"name": SUMS_NAME,
                       "browser_download_url": f"{DL_BASE}/{tag}/{SUMS_NAME}"})
    return json.dumps({
        "tag_name": tag,
        "html_url": f"https://github.com/nixi-agent/vrchat-livetranslate/releases/tag/{tag}",
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


def test_fetch_latest_release() -> None:
    old = uc._opener
    try:
        op = FakeOpener([FakeResp(payload("v9.9.9"))])
        uc._opener = op
        info = uc.fetch_latest_release()
        assert info.version == "9.9.9" and info.tag == "v9.9.9"
        assert info.exe_url.endswith("/" + EXE_NAME)
        assert info.sums_url.endswith("/" + SUMS_NAME)
        assert info.exe_size == 12345678, f"exe_size 没带上：{info.exe_size}"
        # GitHub 对无 User-Agent 的请求直接 403 —— UA 必须带
        ua = op.reqs[0].headers.get("User-agent", "")
        assert ua.startswith("vrchat-livetranslate/"), f"缺 User-Agent：{ua!r}"

        # assets[].size 缺省 → exe_size 为 None（进度条改走 Content-Length / indeterminate）
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", exe_size=None))])
        assert uc.fetch_latest_release().exe_size is None

        # 附件缺一个 → UpdateCheckError
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", drop="sums"))])
        try:
            uc.fetch_latest_release()
            raise AssertionError("附件不全居然没报错")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e)

        # 限流 403 / 429 → 明确提示（未认证 60 次/小时，手动按钮被连点时最容易撞）
        for code in (403, 429):
            uc._opener = FakeOpener([HTTPError(uc.RELEASES_LATEST_API, code, "x",
                                               None, io.BytesIO(b""))])
            try:
                uc.fetch_latest_release()
                raise AssertionError(f"HTTP {code} 居然没报错")
            except uc.UpdateCheckError as e:
                assert "限流" in str(e), f"HTTP {code} 提示不对：{e}"

        # 断网 → 网络不可达
        uc._opener = FakeOpener([URLError("timed out")])
        try:
            uc.fetch_latest_release()
            raise AssertionError("断网居然没报错")
        except uc.UpdateCheckError as e:
            assert "网络不可达" in str(e)

        # 非 semver tag → 报错不崩
        uc._opener = FakeOpener([FakeResp(payload("release-2024"))])
        try:
            uc.fetch_latest_release()
            raise AssertionError("坏 tag 居然没报错")
        except uc.UpdateCheckError:
            pass
        print("  fetch_latest_release OK")
    finally:
        uc._opener = old


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
    info = uc.ReleaseInfo(tag="v0.2.0", version="0.2.0", html_url="h", exe_url="e", sums_url="s")
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
            # 1) 有更新
            uc._opener = FakeOpener([FakeResp(payload("v9.9.9"))])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "update" and got is not None and got.version == "9.9.9"
            # 2) 已是最新
            uc._opener = FakeOpener([FakeResp(payload("v0.1.1"))])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "latest" and got is not None
            # 3) 在忽略列表
            uc.add_ignored_version(cfg, "9.9.9")
            uc._opener = FakeOpener([FakeResp(payload("v9.9.9"))])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "ignored" and got is not None
            # 4) 失败：只留日志不抛、info=None
            uc._opener = FakeOpener([URLError("timed out")])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "error" and got is None
        logs = buf.getvalue()
        # 留痕是硬要求：成功/跳过/失败每个分支都要有一行 [update] 日志
        for needle in ("检查中", "发现新版本 v9.9.9（当前 v0.1.1）", "已是最新",
                       "忽略列表，跳过", "检查失败"):
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
                          exe_url=exe_url, sums_url=sums_url, exe_size=len(exe_bytes))
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
        out.unlink()

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


def test_update_mode() -> None:
    mode = uc.update_mode()
    assert mode in ("frozen", "source")
    # 本测试在源码 checkout 里跑 → 必然是 source
    assert mode == "source", f"源码运行却判定成 {mode}"
    print("  update_mode OK")


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
    print("  build_updater_bat OK")


def main() -> int:
    print("test_update_check:")
    tests = [
        test_parse_version,
        test_is_newer,
        test_fetch_latest_release,
        test_fmt_scalar,
        test_ignore_list_roundtrip,
        test_should_prompt_and_check,
        test_download_and_verify,
        test_redirect_guard,
        test_update_mode,
        test_build_updater_bat,
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
