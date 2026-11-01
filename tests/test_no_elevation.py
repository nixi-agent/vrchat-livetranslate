"""无人值守门控验收：设了 `VLT_NO_ELEVATE` 就绝不弹 UAC。

## 为什么必须加

用户实测反馈：「你刚刚在测试的时候有一大堆要我管理员权限点击，这些东西都从
自动化测试中摘出去，很多时候在测试的时候我是点不了的因为我不在电脑面前」。

Windows 端取钥要读 `\\\\.\\PhysicalDrive0` 的原始扇区，只有管理员能开，所以正常
路径会用 `ShellExecuteW("runas", ...)` 拉一次性提权子进程（`vlt/players/keyd.py`
的 `run_scan`）。这条路径**产品行为正确**，但一旦落进自动化（测试、CI、验证脚本），
UAC 弹窗没人点，就会一直吊到 `ACCEPT_TIMEOUT = 90 s` 才失败，整条流水线跟着卡住。

## 门控放在哪

放在 `run_scan()` 的**最开头**、`_shell_execute` 之前 —— GUI、warmup、验证脚本、
未来任何测试都走这同一条入口，一道闸全盖住。抛 `KeyDenied` 而不是新异常类型，是因为
`vlt/players/win_capture.py` 的 `_scan_via_keyd` 已经 `except keyd.KeyDenied` 并转成
非 fatal 的 `KeyUnavailable`（上层按 `RETRY_BACKOFF` 退避重试，不崩、不弹窗）。

关键细节：**只有在未提权时才拦**。已经跑在管理员终端里就直接扫，不该被这个开关挡住
—— 那时根本没有 UAC 可弹。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.players import keyd  # noqa: E402


class _Env:
    """临时改环境变量，退出时还原（连"原本没有"也还原成没有）。"""

    def __init__(self, **kw: str | None) -> None:
        self._kw = kw
        self._old: dict[str, str | None] = {}

    def __enter__(self) -> "_Env":
        for k, v in self._kw.items():
            self._old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc: object) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _AdminPatch:
    """把 `is_admin()` 钉成指定值，退出时还原。"""

    def __init__(self, value: bool) -> None:
        self.value = value
        self._old = keyd.is_admin

    def __enter__(self) -> "_AdminPatch":
        keyd.is_admin = lambda: self.value      # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        keyd.is_admin = self._old               # type: ignore[assignment]


class _ShellSpy:
    """替换 `_shell_execute`：记下有没有被调用，并按需返回一个假句柄。"""

    def __init__(self, ret: int = 0) -> None:
        self.ret = ret
        self.calls: list[tuple] = []
        self._old = keyd._shell_execute

    def __call__(self, *a, **kw) -> int:
        self.calls.append((a, kw))
        return self.ret

    def __enter__(self) -> "_ShellSpy":
        keyd._shell_execute = self         # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        keyd._shell_execute = self._old    # type: ignore[assignment]


def test_default_is_zero_config() -> None:
    """不设环境变量时门控是关的 —— 产品默认行为不能变。"""
    with _Env(**{keyd.NO_ELEVATE_ENV: None}):
        assert keyd.elevation_blocked() is False, "默认不该禁止提权"
    print(f"  默认放行 OK（{keyd.NO_ELEVATE_ENV} 未设置）")


def test_env_values() -> None:
    """真值表：只有明确的"是"才拦，其它（含写错）一律当没设。"""
    yes = ("1", "true", "TRUE", "yes", "on", " On ")
    no = ("", "0", "false", "no", "off", "vm", "2")
    for v in yes:
        with _Env(**{keyd.NO_ELEVATE_ENV: v}):
            assert keyd.elevation_blocked() is True, f"{v!r} 应该判定为禁止提权"
    for v in no:
        with _Env(**{keyd.NO_ELEVATE_ENV: v}):
            assert keyd.elevation_blocked() is False, f"{v!r} 不该判定为禁止提权"
    print(f"  真值表 OK（{len(yes)} 真 / {len(no)} 假）")


def test_blocked_never_calls_shell_execute() -> None:
    """核心断言：门控生效时 `ShellExecuteW` 一次都不能被调用。"""
    with _Env(**{keyd.NO_ELEVATE_ENV: "1"}):
        with _AdminPatch(False):
            with _ShellSpy() as spy:
                try:
                    keyd.run_scan(pid=1234, created=5678, samples={})
                except keyd.KeyDenied as exc:
                    assert keyd.NO_ELEVATE_ENV in str(exc), \
                        f"报错里该点明是哪个环境变量：{exc}"
                else:
                    raise AssertionError("置位时 run_scan 必须抛 KeyDenied")
                assert spy.calls == [], f"居然调了提权：{spy.calls!r}"
    print("  置位 → KeyDenied 且 ShellExecuteW 未被调用 OK")


def test_admin_is_not_affected() -> None:
    """已经在管理员里就不拦（没有 UAC 可弹），仍走原来的拉起流程。"""
    with _Env(**{keyd.NO_ELEVATE_ENV: "1"}):
        with _AdminPatch(True):
            with _ShellSpy(ret=0) as spy:
                try:
                    keyd.run_scan(pid=1234, created=5678, samples={})
                except keyd.KeyDenied:
                    pass        # 假句柄 0 → 拉起失败，这是预期
                assert spy.calls, "管理员下不该被门控挡住"
    print("  已提权 → 不被门控挡住 OK")


def test_win_capture_degrades_softly() -> None:
    """落到采集层要变成非 fatal 的 `KeyUnavailable`，不能抛到上层崩掉。"""
    from vlt.players import key_win, win_capture

    seen: list[str] = []

    class _Stub:
        def _status(self, level: str, msg: str) -> None:
            seen.append(msg)

    with _Env(**{keyd.NO_ELEVATE_ENV: "1"}):
        with _AdminPatch(False):
            try:
                win_capture._WinCapture._scan_via_keyd(
                    _Stub(), key_win.Identity(pid=1234, created=5678), {}, {})
            except win_capture.KeyUnavailable as exc:
                assert getattr(exc, "fatal", False) is False, \
                    f"这个失败是可重试的，不该标 fatal：{exc}"
            else:
                raise AssertionError("门控生效时采集层该抛 KeyUnavailable")
            assert any("UAC" in m for m in seen), f"该先提示一次要提权：{seen!r}"
    print("  采集层降级为可重试 KeyUnavailable OK")


def test_no_real_elevation_in_tests() -> None:
    """`tests/` 里不该出现提权调用 —— 防止以后有人又往测试里塞。"""
    tests_dir = ROOT / "tests"
    banned = ("ShellExecuteW", '"runas"', "'runas'")
    offenders: list[str] = []
    for path in sorted(tests_dir.glob("*.py")):
        if path.name == Path(__file__).name:
            continue        # 本文件要断言这些字符串，自然含它们
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in banned:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"测试目录里出现了提权调用：{offenders}"
    print("  tests/ 无提权调用 OK")


if __name__ == "__main__":
    print("test_no_elevation:")
    test_default_is_zero_config()
    test_env_values()
    test_blocked_never_calls_shell_execute()
    test_admin_is_not_affected()
    test_win_capture_degrades_softly()
    test_no_real_elevation_in_tests()
    print("ALL PASSED")
