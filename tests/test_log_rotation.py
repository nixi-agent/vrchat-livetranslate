"""日志滚动 + 导出压缩包的验收。

## 两个需求（用户提出）
1. **日志不能把磁盘吃爆**：总量超上限自动删最旧的；单文件超上限自动切段
   （切段而不是停写 —— 日志**末尾**才是出问题的地方）。
2. **GUI 能把日志打成 zip 存到用户指定位置**，方便朋友出问题时发过来排查。

## 关键安全点
导出包要经聊天工具发出去，所以**打包前逐文件脱敏**：扫到 `sk-xxxx` 形状就替换，
配置里的房间认证字段另按 YAML 结构处理，损坏配置只导出说明。
这条必须有测试盯着 —— 密钥泄露是不可逆的。
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAKE_KEY = "sk" + "-ws-" + "leakcheck0123456789abcdef"      # 拼接构造，避免被凭据扫描误判


def test_prune_keeps_newest_and_respects_cap() -> None:
    """总量超上限 → 删最旧的；当前正在写的那份**永不删**。"""
    from vlt import crashlog

    d = Path(tempfile.mkdtemp(prefix="vlt-log-"))
    files = []
    for i in range(6):
        p = d / f"gui_2026010{i}_000000.log"
        p.write_bytes(b"x" * 1000)
        files.append(p)
    cur = files[-1]
    # 上限压到 3KB（6 个文件共 6KB）→ 应删掉最旧的 3 个
    removed = crashlog._prune_logs(d, keep={cur}, max_total=3000)
    left = sorted(p.name for p in d.glob("*.log*"))
    assert cur.exists(), "★ 正在写的当前日志被删了 —— 这是最要命的"
    assert len(removed) >= 3, f"应删掉至少 3 个最旧的，实际 {len(removed)}"
    assert "gui_2026010_000000.log" not in left, f"最旧的没删：{left}"
    assert "gui_20260105_000000.log" in left, f"最新的不该删：{left}"
    assert sum(p.stat().st_size for p in d.glob("*.log*")) <= 3000, "删完仍超上限"
    print(f"  滚动删除 OK（删 {len(removed)} 个，当前日志保留，剩余 ≤ 3KB）")


def test_rotation_switches_segment_not_stops() -> None:
    """单文件写满 → 切段继续写，且新段里能明确看到"已切段"。"""
    from vlt import crashlog

    d = Path(tempfile.mkdtemp(prefix="vlt-rot-"))
    cur = d / "gui_test.log"
    crashlog.MAX_LOG_FILE_BYTES = 1          # 立刻触发切段
    try:
        f = open(cur, "w", encoding="utf-8", buffering=1)
        tee = crashlog._Tee(None, f, log_path=cur, log_dir=d)
        tee.write("第一行\n")                  # 触发 _rotate
        tee.write("切段之后的第二行\n")
        tee.flush()
        segs = sorted(d.glob("gui_test*.log"))
        assert len(segs) >= 2, f"没有切出新段：{sorted(p.name for p in d.glob('*'))}"
        joined = "".join(p.read_text(encoding="utf-8", errors="replace") for p in segs)
        assert "切段之后的第二行" in joined, "★ 切段后内容丢了 —— 等于把关键证据扔掉"
        assert "已切段" in joined, "切段这件事本身应当在日志里留痕"
        print(f"  单文件切段 OK（{len(segs)} 段，后续内容未丢）")
    finally:
        crashlog.MAX_LOG_FILE_BYTES = 2 * 1024 * 1024


def test_export_redacts_keys_and_includes_config() -> None:
    """导出的 zip：包含日志 + config.yaml，且**密钥被脱敏**。"""
    from vlt import crashlog

    d = Path(tempfile.mkdtemp(prefix="vlt-exp-"))
    (d / "gui_20260101_000000.log").write_text(
        f"09:00:00.000 [startup] API 密钥：{FAKE_KEY}（40 字符）\n"
        "09:00:01.000 [gui] 正常日志一行\n",
        encoding="utf-8")
    (d / "gui_20260101_000001.log").write_text("第二条日志，无敏感内容\n", encoding="utf-8")
    cfg = d / "config.yaml"
    cfg.write_text("session:\n  model: x\n", encoding="utf-8")

    out = Path(tempfile.mkdtemp(prefix="vlt-expout-")) / "logs.zip"
    with patch("vlt.paths.app_dir", return_value=d):
        path, n, size, redacted = crashlog.export_logs(out, log_dir=d, extra_files=[cfg])
    assert path.exists() and path.suffix == ".zip", "没生成 zip"
    assert n >= 3, f"应至少打包 2 个日志 + 1 个配置，实际 {n}"
    assert redacted, "含密钥的文件应当被标为已脱敏"

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        blob = "".join(zf.read(nm).decode("utf-8", errors="replace") for nm in names)
        assert "说明.txt" in names, "缺少说明文件"
        assert any(nm.endswith("config.yaml") for nm in names), "配置没被打包进去"
        assert FAKE_KEY not in blob, "★ zip 里出现了完整密钥 —— 这是不可逆的泄露"
        assert "sk-****" in blob, "密钥应当被替换成打码形式"
        assert "正常日志一行" in blob, "脱敏把正文也弄丢了"
    print(f"  导出 OK（{n} 个文件 / {size}B；脱敏 {redacted}）")


def _export_config(text: str, name: str = "config.yaml") -> tuple[str, list[str]]:
    """只用假配置调用真实导出入口，并确认源文件没有被改写。"""
    from vlt import crashlog

    with tempfile.TemporaryDirectory(prefix="vlt-redact-") as tmp:
        app = Path(tmp)
        logs = app / "logs"
        logs.mkdir()
        (logs / "gui_test.log").write_bytes("普通诊断日志\n".encode("utf-8"))
        config = app / name
        original = text.encode("utf-8")
        config.write_bytes(original)
        with patch("vlt.paths.app_dir", return_value=app):
            archive, n, _, redacted = crashlog.export_logs(app / "export.zip")
        assert n == 2, "测试只应导出沙箱内的日志与配置"
        assert config.read_bytes() == original, "导出不得改写原配置"
        with zipfile.ZipFile(archive) as zf:
            assert zf.read("logs/gui_test.log").decode("utf-8") == "普通诊断日志\n"
            exported = zf.read(name).decode("utf-8")
            if name in redacted:
                assert name in zf.read("说明.txt").decode("utf-8")
        return exported, redacted


def test_export_redacts_room_authentication() -> None:
    """令牌无固定前缀；查询参数名可能编码，且旧 URL 里可能有多个令牌。"""
    from urllib.parse import parse_qsl, urlsplit

    secret = "仅供测试-房间口令"
    url = ("wss://example.invalid/ws?mode=debug&room=TESTTEST"
           "&k=old-key&tok=old-tok&%74oken=old-token&token=second-token")
    text = yaml.safe_dump({
        "session": {"model": "test-model"},
        "room": {"token": secret, "server_url": url, "room_code": "TESTTEST"},
    }, allow_unicode=True)
    exported, redacted = _export_config(text)
    for value in (secret, "old-key", "old-tok", "old-token", "second-token"):
        assert value not in exported, "导出的配置仍包含房间认证凭据"
    data = yaml.safe_load(exported)
    assert data["session"]["model"] == "test-model"
    assert data["room"]["room_code"] == "TESTTEST", "保留房间码用于排查"
    parts = urlsplit(data["room"]["server_url"])
    assert parts.netloc == "example.invalid" and parts.path == "/ws"
    query = dict(parse_qsl(parts.query))
    assert query["mode"] == "debug" and query["room"] == "TESTTEST"
    assert redacted == ["config.yaml"]
    print("  房间令牌 / URL 查询认证脱敏 OK（保留非敏感配置）")


def test_export_redacts_yaml_variants() -> None:
    """流式映射、多行、共享别名、合并与重复键都不能绕过字段脱敏。"""
    secret = "synthetic-" + "room-credential"  # 假数据，避免凭据扫描误判
    cases = [
        f"'room': {{'token': '{secret}'}}\n",
        f"room:\n  token: |\n    {secret}\n    第二行测试口令\n",
        f"room: {{token: &key {secret}}}\ncopy: *key\n",
        f"room:\n  token: &field token\n  *field: {secret}\n",
        f"room: {{token: &field room}}\n*field: {{token: {secret}}}\n",
        f"auth: &auth {{token: {secret}}}\nroom: {{<<: *auth}}\n",
        f"auth: &auth {{token: {secret}}}\nroom: {{<<: [*auth, {{enabled: true}}]}}\n",
        f"<<: {{room: {{token: {secret}}}}}\n",
        f"room: {{token: {secret}, token: second-secret}}\nroom: {{token: third-secret}}\n",
        f"auth: &auth {{<<: *auth, token: {secret}}}\nroom: {{<<: *auth}}\n",
        ("endpoint: &url 'wss://example.invalid/ws?token=" + secret
         + "'\nroom: {server_url: *url}\n"),
    ]
    for text in cases:
        exported, redacted = _export_config(text)
        for value in (secret, "第二行测试口令", "second-secret", "third-secret"):
            assert value not in exported, "YAML 语法变体绕过了脱敏"
        assert redacted == ["config.yaml"]
        assert yaml.safe_load(exported) is not None
    print(f"  YAML 语法变体脱敏 OK（{len(cases)} 种）")


def test_export_omits_unsafe_config() -> None:
    """损坏 YAML / 非预期结构只能导出说明，不能回退原文或泄露解析异常。"""
    secret = "synthetic-" + "broken-credential"
    cases = [
        f"room:\n  token: {secret}\nbroken: [\n",
        f"room: {{token: ['{secret}']}}\n",
        f"room: {{server_url: {{token: {secret}}}}}\n",
        f"room: [{secret}]\n",
        f"- {secret}\n",
        f"room: {{server_url: 'wss://[invalid?token={secret}'}}\n",
    ]
    for name in ("config.yaml", "config.yaml.broken"):
        for text in cases:
            exported, redacted = _export_config(text, name)
            assert secret not in exported, "不安全配置的原文或解析异常被导出了"
            assert "已省略" in exported, "应明确说明配置未被导出"
            assert redacted == [name]
    print("  损坏配置 / 非预期结构安全省略 OK（包含 .broken）")


def test_export_preserves_non_sensitive_config() -> None:
    """没有认证信息时保留原文、注释与空值，避免无谓重写诊断配置。"""
    for text in (
        "# 设备诊断\nsession:\n  model: test-model\n",
        "room:\n  token: ''\n  server_url: wss://example.invalid/ws?mode=debug\n",
        "# 尚未配置\n",
    ):
        exported, redacted = _export_config(text)
        assert exported == text
        assert not redacted
    print("  非敏感配置原文保留 OK")


if __name__ == "__main__":
    print("test_log_rotation:")
    test_prune_keeps_newest_and_respects_cap()
    test_rotation_switches_segment_not_stops()
    test_export_redacts_keys_and_includes_config()
    test_export_redacts_room_authentication()
    test_export_redacts_yaml_variants()
    test_export_omits_unsafe_config()
    test_export_preserves_non_sensitive_config()
    print("ALL PASSED")
