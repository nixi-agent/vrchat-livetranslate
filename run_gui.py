"""PyInstaller 打包入口（带界面的那个）。

**为什么不直接拿 `vlt/gui.py` 当入口**：它用的是包内相对导入（`from .config import ...`），
被 PyInstaller 当脚本直接执行时不在包上下文里，导入会失败。用一个顶层脚本
`from vlt.gui import main` 最省事，源码运行照样能用。

**为什么开头要补 stdout/stderr**：`--noconsole` 打包出来的 exe 里 `sys.stdout` 是 `None`，
此时任何 `print(...)` 都会抛 `AttributeError: 'NoneType' object has no attribute 'write'`。
项目里 print 遍地都是，所以必须在**导入任何项目代码之前**把它们兜住
（崩溃日志随后会接管 stdout，把输出落到 exe 旁边的 logs/ 里）。

**为什么还要拦 `--verify-*`**：验收脚本（scripts/verify_appimage.py）要用**冻结产物本身**
跑包内检查 —— 见 vlt/selfcheck.py 的说明。这些开关必须在导入 GUI 之前拦下来，
免得拉起窗口/崩溃日志那一套。
"""
from __future__ import annotations

import sys

if sys.stdout is None or sys.stderr is None:
    import os

    _fallback = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = _fallback
    if sys.stderr is None:
        sys.stderr = _fallback

if "--vlt-keyd" in sys.argv[1:]:
    # Windows 一次性提权取钥子进程：**同一个 exe** 用 `--vlt-keyd` 再启动一次，
    # 由 UAC 提权后跑 `vlt.players.keyd.main`（见 `vlt/players/keyd.py`）。
    # 必须在 `--verify-*` 之前拦：既不能进 GUI，也不能被验收脚本吃掉。
    from vlt.players import keyd as _keyd  # noqa: E402

    raise SystemExit(_keyd.main(sys.argv[sys.argv.index("--vlt-keyd") + 1:]))

if any(a.startswith("--verify-") for a in sys.argv[1:]):
    from vlt.selfcheck import main as _verify_main  # noqa: E402

    raise SystemExit(_verify_main(sys.argv[1:]))

from vlt.gui import main  # noqa: E402  （必须在 stdout 兜底之后导入）

if __name__ == "__main__":
    raise SystemExit(main())
