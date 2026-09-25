"""界面 API key 输入行验收 + 「明文绝不外泄」回归守卫。

## 为什么要有这个测试

用户报「GUI 上现在没有地方输入 key」。查下来是**两个缺口**，不只是少个输入框：

1. 界面里确实没有输入控件；
2. 更隐蔽的：`config.load_api_key()` 的优先级是
   `显式参数 → 环境变量 → ~/.bailian/config.json` —— **压根没查界面保存的 key**。
   所以就算加了输入框、存下了 key，也不会被使用。

第 2 条正是「看起来做了但没生效」的典型，必须有测试钉住。

安全约束（用户明确要求过「别把我key推上去」）：
明文 key 不得出现在日志、状态栏、异常信息里；界面上保存后立即清空。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 假 key 刻意用拼接构造：一是别被 pre-commit 的凭据扫描误判（它按 API key 前缀匹配），
# 二是明确告诉后来者这不是真凭据。
FAKE_KEY = "sk" + "-ws-" + "testonly0123456789abcdef0123456789"
ENV_KEY = "sk" + "-ws-" + "envshouldlose0123456789abcdef"


def _use_temp_storage() -> Path:
    """把 key 存储目录重定向到临时目录 —— 绝不能碰到用户真实的 api_key.txt。"""
    from vlt import credentials

    tmp = Path(tempfile.mkdtemp(prefix="vlt-keytest-"))
    credentials._storage_dir_override = lambda: tmp
    return tmp


def _reset_storage() -> None:
    from vlt import credentials

    credentials._storage_dir_override = None


def test_roundtrip_and_mask() -> None:
    _use_temp_storage()
    try:
        from vlt.credentials import clear_saved_key, load_saved_key, mask_key, save_api_key

        p = save_api_key(FAKE_KEY)
        assert p.exists(), "保存后文件不存在"
        assert load_saved_key() == FAKE_KEY, "读回的值不一致"
        masked = mask_key(FAKE_KEY)
        assert FAKE_KEY not in masked, f"打码后仍含完整 key：{masked}"
        assert masked.startswith(FAKE_KEY[:3]) and "****" in masked, f"打码格式不对：{masked}"
        assert clear_saved_key() is True and not p.exists(), "清除失败"
        assert clear_saved_key() is False, "重复清除应返回 False"
        print(f"  存取/打码/清除 OK（{masked}）")
    finally:
        _reset_storage()


def test_invalid_key_rejected() -> None:
    tmp = _use_temp_storage()
    try:
        from vlt.credentials import save_api_key

        for bad in ["", "   ", "short", "has space in it 1234567890"]:
            try:
                save_api_key(bad)
            except ValueError:
                continue
            raise AssertionError(f"非法 key 竟然被接受了：{bad!r}")
        assert not (tmp / "api_key.txt").exists(), "非法 key 不该写出文件"
        print("  非法 key 被拒绝 OK（空 / 过短 / 含空白）")
    finally:
        _reset_storage()


def test_load_api_key_prefers_gui_saved() -> None:
    """★ 界面存的 key 必须优先于环境变量和 bl CLI 配置 —— 这是真正修的那个缺口。"""
    _use_temp_storage()
    old_env = os.environ.get("DASHSCOPE_API_KEY")
    buf = io.StringIO()
    try:
        from vlt.config import load_api_key
        from vlt.credentials import save_api_key

        os.environ["DASHSCOPE_API_KEY"] = ENV_KEY
        save_api_key(FAKE_KEY)
        with contextlib.redirect_stdout(buf):
            got = load_api_key()
        assert got == FAKE_KEY, f"没优先用界面保存的 key（拿到 {got[:8]}…）"
        out = buf.getvalue()
        assert "界面保存" in out, f"应说明 key 来源，实际：{out!r}"
        assert FAKE_KEY not in out, "★ 日志里出现了完整 key！"
        assert ENV_KEY not in out, "★ 日志里出现了环境变量里的 key！"
        print("  界面 key 优先于环境变量 OK（且日志只打码）")
    finally:
        if old_env is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = old_env
        _reset_storage()


def test_gui_key_row_saves_without_leaking() -> None:
    """★ 界面上：能存、存完立刻清空输入框、日志/状态栏都不出现明文。"""
    _use_temp_storage()
    buf = io.StringIO()
    gui = None
    try:
        from vlt.credentials import load_saved_key
        from vlt.gui import TranslationGUI
        from vlt import credentials as cred_mod

        assert cred_mod._storage_dir_override is not None, "测试必须先把存储重定向到临时目录"

        with contextlib.redirect_stdout(buf):
            gui = TranslationGUI()
        assert hasattr(gui, "_key_entry"), "界面没有 API key 输入框"
        assert gui._key_entry.cget("show") != "", "输入框没有掩码显示，明文会暴露在屏幕上"

        with contextlib.redirect_stdout(buf):
            gui._key_var.set(FAKE_KEY)
            gui._on_save_key()
        assert load_saved_key() == FAKE_KEY, "界面保存没有落盘"
        assert gui._key_var.get() == "", "保存后输入框没有清空，明文还留在界面上"
        label = gui._key_status.cget("text")
        assert "****" in label, f"状态标签没有显示打码值：{label}"
        assert FAKE_KEY not in label, f"★ 状态标签出现明文：{label}"

        # 清除按钮
        with contextlib.redirect_stdout(buf):
            gui._on_clear_key()
        assert load_saved_key() is None, "清除没有生效"

        out = buf.getvalue()
        assert FAKE_KEY not in out, f"★ 界面日志里出现了完整 key！\n{out}"
        print(f"  界面存取 OK，状态标签：{label}（日志无明文）")
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:
                pass
        _reset_storage()


if __name__ == "__main__":
    print("test_api_key_gui:")
    test_roundtrip_and_mask()
    test_invalid_key_rejected()
    test_load_api_key_prefers_gui_saved()
    test_gui_key_row_saves_without_leaking()
    print("ALL PASSED")
