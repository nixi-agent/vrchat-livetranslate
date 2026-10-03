"""OSC 端口（设置 → 常规 → VRChat OSC）：回填 / 校验 / 就地写 / 补建 chatbox 段。

## 治什么

「OSC 端口」这条设置是用户要的：默认 9000 只在**没动过 VRChat** 时成立。用户在 VRChat
里改过 OSC 端口、或中间挂了 OSC 转发工具时，我们不跟着改就一条气泡都发不出去 ——
而症状只是「chatbox 没反应」，界面上完全看不出是端口不对。

本文件守住 `gui._on_save_osc_port` 的四条口径：

1. 打开设置时按 `chatbox.port` **回填**（脏值回落 9000，不把非数字塞进输入框）；
2. 保存走**就地写**（沿用 `_yaml_set_or_create`：注释与键顺序不许丢）；
3. 写完**同步内存** `self._cfg.chatbox["port"]` —— 引擎是「开始翻译」时按它建 Chatbox 的，
   不同步就是「改了没反应」；
4. 非法值（0 / 70000 / 非数字 / 空）**不写盘**并留痕（状态栏 + 就地红字 + 日志各一行）。

## 离线可跑

不连网络、不枚举设备；只走「取值 → 校验 → 写盘」这条真实路径。真个人 `config.yaml`
先备份、测完还原（`finally` 里还原，失败也不留脏文件）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 干净环境（CI）没有 API key，而 load_config 默认 require_key=True 会 SystemExit。
# 给一个拼接出来的假 key：本文件只验配置读写，与 key 真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "oscporttest0123456789abcdef")

# 界面语言钉死 zh：CI 与外国机器是英文系统（产品代码不依赖这个补丁）
import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

CONFIG = ROOT / "config.yaml"          # 源码运行的配置路径（与 gui.DEFAULT_CONFIG 同一个）


def _n_comments(t: str) -> int:
    return sum(1 for ln in t.splitlines() if ln.strip().startswith("#"))


def _port_line(t: str) -> str | None:
    """chatbox 段里的 port 行（原样返回整行，便于看缩进有没有被破坏）。"""
    in_cb = False
    for ln in t.splitlines():
        if ln.startswith("chatbox:"):
            in_cb = True
            continue
        if in_cb:
            if ln and not ln[0].isspace():
                return None                     # 出了 chatbox 段还没看到 port
            if ln.strip().startswith("port:"):
                return ln
    return None


def _port_value(line: str | None) -> str | None:
    """从 port 行里取「键: 值」部分（**忽略行尾注释** —— 模板里的 port 行就有注释，
    CI 上 config.yaml 是由模板生成的，拿整行做等值比较会假红，本文件第一版就踩了）。"""
    if line is None:
        return None
    return line.split("#", 1)[0].strip()


def _port_comment(line: str | None) -> str | None:
    """port 行尾注释（没有则 None）。"""
    if line is None or "#" not in line:
        return None
    return line.split("#", 1)[1].strip()


def _prepare() -> str:
    """确保有 config.yaml（CI / 新克隆上没有 → 照程序规矩从模板生成），返回原文。"""
    if not CONFIG.exists():
        CONFIG.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                          encoding="utf-8")
    return CONFIG.read_text(encoding="utf-8")


def _make_gui():
    import vlt.gui as G

    gui = G.TranslationGUI()
    gui._open_settings()          # 设置弹窗是懒建的：控件要建出来才谈得上回填/保存
    return gui


def _destroy(gui) -> None:  # noqa: ANN001
    try:
        gui._root.destroy()
    except Exception:  # noqa: BLE001
        pass


def test_backfill_and_save_keeps_comments() -> None:
    before = _prepare()
    n_before = _n_comments(before)
    comment_before = _port_comment(_port_line(before))
    gui = None
    try:
        gui = _make_gui()
        # 1) 回填：输入框初值 == 文件里的 port
        assert gui._osc_port_var.get() == "9000", \
            f"回填不对：{gui._osc_port_var.get()!r}"
        # 2) 保存
        gui._osc_port_var.set("9001")
        gui._on_save_osc_port()
        after = CONFIG.read_text(encoding="utf-8")
        assert _port_value(_port_line(after)) == "port: 9001", \
            f"没写对：{_port_line(after)!r}"
        # 模板里的 port 行本来就带行尾注释 → 写入后必须还在（就地写不许把它抹掉）
        if comment_before:
            assert _port_comment(_port_line(after)) == comment_before, "同行注释被抹掉"
        assert _n_comments(after) == n_before, "注释被破坏"
        # 3) 内存同步（否则本轮「开始翻译」还用旧端口）
        assert gui._cfg.chatbox["port"] == 9001, "内存没同步"
        assert gui._osc_err.cget("text") == "", "成功时不该留红字"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print("  回填 9000 → 保存 9001：写对、注释保住、内存同步 OK")


def test_invalid_values_not_written() -> None:
    before = _prepare()
    gui = None
    try:
        gui = _make_gui()
        gui._osc_port_var.set("9001")
        gui._on_save_osc_port()
        good = CONFIG.read_text(encoding="utf-8")
        for bad in ("0", "70000", "abc", "", " 9001 x"):
            gui._osc_port_var.set(bad)
            gui._on_save_osc_port()
            assert CONFIG.read_text(encoding="utf-8") == good, f"非法值 {bad!r} 竟然写盘了"
            assert gui._osc_err.cget("text"), f"非法值 {bad!r} 没留红字"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print("  非法值（0 / 70000 / 非数字 / 空 / 带空格）：不写盘 + 就地红字 OK")


def test_missing_chatbox_section_recreated() -> None:
    """老配置可能整段没有 `chatbox:` → 必须补建，不能静默 no-op。"""
    before = _prepare()
    gui = None
    try:
        # 造一份「没有 chatbox 段」的配置
        text = before.replace("chatbox:\n", "chatboxX:\n", 1)
        assert "chatbox:\n" not in text
        CONFIG.write_text(text, encoding="utf-8")
        gui = _make_gui()
        gui._osc_port_var.set("9010")
        gui._on_save_osc_port()
        after = CONFIG.read_text(encoding="utf-8")
        assert "chatbox:" in after, "没补建 chatbox 段"
        assert _port_value(_port_line(after)) == "port: 9010", \
            f"补建后没写对：{_port_line(after)!r}"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print("  chatbox 段缺失时：自动补建并写入 OK")


if __name__ == "__main__":
    print("test_osc_port_save:")
    test_backfill_and_save_keeps_comments()
    test_invalid_values_not_written()
    test_missing_chatbox_section_recreated()
    print("ALL PASSED")
