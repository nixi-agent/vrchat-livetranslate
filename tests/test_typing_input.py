"""打字输入接线验收（issue #110）。

## 真实问题

v0.11.0–v0.12.0 里界面底部的「打字:」行 **100% 不可用**：回车抛
`TypeError: 'NoneType' object is not callable`，发送按钮抛
`TypeError: send_typed() missing 3 required positional arguments`。

根因是 gui 模块化拆分（6083052）后两个调用点各写一遍参数，都漏了
`engines` / `engine_dirs` / `set_status_fn`，而现有用例没有覆盖「界面回调 →
send_typed」这一层，所以 45/45 全绿而功能是死的。

## 这里钉住什么

1. 回车与「发送」按钮**走同一个入口**（`_submit_typed`）→ 会把文字交给 `mine` 腿；
2. 发送成功后清空输入框、状态栏收到 info；
3. 没有 `mine` 腿时给 warn 而不是崩；
4. `set_status_fn=None`（headless / 测试）也**不许崩** —— 这是回车那条路的原始故障面。

故障注入：把 `_submit_typed` 里改成 `send_typed(ctx)`（拆分后的原始写法），
第 1 条必须变红。
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.gui_chat import (  # noqa: E402
    ChatCtx, _submit_typed, build_input_row, send_typed, set_text_input_enabled,
)


class _FakeEngine:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return True


def _make_ctx(root: tk.Tk):
    ctx = ChatCtx()
    eng = _FakeEngine()
    ctx.engines_ref = [eng]
    ctx.engine_dirs_ref = ["mine"]
    statuses: list[tuple[str, str]] = []
    ctx.set_status_fn = lambda level, msg: statuses.append((level, msg))
    build_input_row(root, ctx, SimpleNamespace(text_input={"enabled": True}), lambda _w: None)
    set_text_input_enabled(ctx, True)          # build 完默认置灰，测试要能点
    return ctx, eng, statuses


def test_button_and_return_share_one_entry() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        ctx, eng, statuses = _make_ctx(root)

        ctx.text_var.set("你好 打字")
        ctx.send_btn.invoke()                  # 「发送」按钮
        assert eng.sent == ["你好 打字"], f"按钮没把文字交给 mine 腿：{eng.sent}"
        assert ctx.text_var.get() == "", "发送成功后要清空输入框"
        assert statuses and statuses[-1][0] == "info", statuses

        ctx.text_var.set("second")
        assert _submit_typed(ctx) == "break", "回车回调必须吃掉事件"
        assert eng.sent == ["你好 打字", "second"], eng.sent
        print("  回车与按钮同入口、交给 mine 腿、清空 + info OK")
    finally:
        root.destroy()


def test_no_mine_leg_warns_without_crash() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        ctx, eng, statuses = _make_ctx(root)
        ctx.engines_ref = []
        ctx.engine_dirs_ref = []
        ctx.text_var.set("hi")
        ctx.send_btn.invoke()
        assert eng.sent == [], "没有 mine 腿时不该发出去"
        assert statuses and statuses[-1][0] == "warn", statuses
        print("  无 mine 腿 → warn、不崩 OK")
    finally:
        root.destroy()


class _Var:
    """最小 StringVar 替身：只要 get/set，不需要 Tk 根。"""

    def __init__(self, value: str = "") -> None:
        self._v = value

    def get(self) -> str:
        return self._v

    def set(self, value: str) -> None:
        self._v = value


def test_status_fn_none_does_not_crash() -> None:
    """headless / 没接状态栏时，回车那条路以前就是死在这里（NoneType 不可调用）。"""
    ctx = ChatCtx()
    eng = _FakeEngine()
    ctx.engines_ref = [eng]
    ctx.engine_dirs_ref = ["mine"]
    ctx.text_var = _Var("hi")
    ctx.text_entry = object()                  # 非 None 才能进发送逻辑
    send_typed(ctx, [eng], ["mine"], None)    # 成功路径会回报状态 → 以前这里炸
    assert eng.sent == ["hi"], eng.sent
    send_typed(ctx, [], [], None)             # 无 mine 腿的 warn 路径同样不许炸
    print("  set_status_fn=None → 两条路径都不崩 OK")


if __name__ == "__main__":
    print("test_typing_input:")
    test_button_and_return_share_one_entry()
    test_no_mine_leg_warns_without_crash()
    test_status_fn_none_does_not_crash()
    print("ALL PASSED")
