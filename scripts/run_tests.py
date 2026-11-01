#!/usr/bin/env python3
"""仓库内**唯一**的测试运行器：逐文件跑 `tests/test_*.py`，本地与 CI 共用同一套口径。

为什么有它：本仓库的用例是**单文件脚本**（`python tests/xxx.py` 直接跑，不是 pytest）。
以前本机用这个脚本、CI 却各写一段内联 `for` 循环 —— 三四处拷贝必然漂移（虚拟屏分辨率、
跳过语义都各改各的，实测已经出现过「本机 640x480 / CI 1280x1024」的口径分裂）。现在
**CI 的两个 job 也直接调它**，口径只留这一处。

用法（务必用仓库自己的 venv 解释器，本机没有 python3）：

    .venv/Scripts/python.exe scripts/run_tests.py                # 全部（默认跳过 test_engine）
    .venv/Scripts/python.exe scripts/run_tests.py --only device  # 只跑文件名含 device 的
    .venv/Scripts/python.exe scripts/run_tests.py --only 'test_room_*'   # 也支持通配
    .venv/Scripts/python.exe scripts/run_tests.py --coverage     # 顺带累积覆盖率摘要
    .venv/Scripts/python.exe scripts/run_tests.py --require-tk   # 缺 tkinter 直接判红（CI 用）
    .venv/Scripts/python.exe scripts/run_tests.py --require-xvfb  # Linux 缺 xvfb-run 直接判红（CI 用）
    .venv/Scripts/python.exe scripts/run_tests.py --with-engine  # 连需真 key 的也跑
    .venv/Scripts/python.exe scripts/run_tests.py --no-xvfb      # 明确裸跑（不挂 xvfb）

约定（即 CI 的口径）：
  * 默认**跳过** `tests/test_engine.py`（要真实 API key 打真会话），并打印原因，**不静默**；
  * **Linux 上逐用例自动挂 `xvfb-run -a`**（每个用例一个干净、无窗口管理器的虚拟 X），
    否则平铺 WM（Hyprland / niri / sway）会把窗口重排成满屏，`test_desktop_overlay*` /
    `test_i18n` 这类实测几何的用例会**假红**。虚拟屏**显式钉死**成
    `-screen 0 <VLT_XVFB_SCREEN，默认 1920x1080x24>` —— 不再依赖发行版默认
    （Arch 是 640x480、Debian/Ubuntu 是 1280x1024，不钉就各跑各的）；
    Windows / macOS 自带桌面会话，不套（同 CI 的 Windows job）。找不到 `xvfb-run` 时本机
    只打印提示（不静默），可用 `--no-xvfb` 明确接受裸跑；**CI 传 `--require-xvfb`，缺了
    直接判红**（裸跑下 GUI 用例会自跳过 → 假绿）；
  * **无 tkinter**：本机默认跳过「传递依赖 tkinter」的用例（名单由
    `scripts/list_display_tests.py` 按 import 图**自动发现**，逐条打印理由）；CI 传
    `--require-tk`，此时缺 tkinter **直接判红**，绝不静默少跑一批 GUI 用例；
  * 在 `GITHUB_ACTIONS=true` 下输出 `::group:: / ::error / ::notice` 注解，本地保持纯文本；
  * 一个用例都没**真正通过**（`PASS=0`，全是跳过）→ 判非 0，拒绝把「一个都没跑」当绿；
  * 任一用例退出码非 0 → 整体退出码非 0；
  * 覆盖率**不设阈值、不阻断**：只多打印一张 `--show-missing` 摘要；测试结果才是门禁。

⚠️ 已知陷阱：若环境里带着 `DASHSCOPE_API_KEY`（尤其是被别的用例注入过的假 key），
会影响部分用例。跑之前先 `unset DASHSCOPE_API_KEY`（见技能 `vrchat-livetranslate-dev`）。
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
LIST_DISPLAY = ROOT / "scripts" / "list_display_tests.py"

#: 需要真实 API key / 真会话的用例：默认跳过（CI 同样跳过，理由一致）
ENGINE_TEST = "test_engine.py"
ENGINE_SKIP_REASON = "需要真实 API key（打真会话，属本机实测项）"

#: Linux 上给**每个**用例套的虚拟 X 包装（与 CI 逐用例口径一致，见 `xvfb_decision`）。
XVFB_RUN = "xvfb-run"
#: 虚拟屏分辨率**显式钉死**：不依赖发行版默认（Arch 640x480 / Ubuntu 1280x1024）。
#: 可用 `VLT_XVFB_SCREEN` 覆盖（与 tests/test_x11_window.py 同名，改一处全变）。
DEFAULT_XVFB_SCREEN = "1920x1080x24"


class Annotator:
    """GitHub Actions 注解 + 分组。非 GHA 环境全部退化为空操作（本地保持纯文本）。"""

    def __init__(self, enabled: bool = False, out=None) -> None:
        self.enabled = enabled
        self.out = out if out is not None else sys.stdout

    def _emit(self, line: str) -> None:
        if self.enabled:
            print(line, file=self.out, flush=True)

    def group(self, title: str) -> None:
        self._emit(f"::group::{title}")

    def endgroup(self) -> None:
        self._emit("::endgroup::")

    def error(self, msg: str, file: str | None = None) -> None:
        self._emit(f"::error file={file}::{msg}" if file else f"::error::{msg}")

    def notice(self, msg: str) -> None:
        self._emit(f"::notice::{msg}")


def gha_enabled() -> bool:
    """是否处于 GitHub Actions（决定要不要发 `::group::` 这类注解）。"""
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def xvfb_screen() -> str:
    """虚拟屏分辨率串（`WxHxD`）：`VLT_XVFB_SCREEN` 优先，否则 `DEFAULT_XVFB_SCREEN`。"""
    return os.environ.get("VLT_XVFB_SCREEN", "").strip() or DEFAULT_XVFB_SCREEN


def tkinter_available() -> bool:
    """当前解释器能否 import tkinter（与真正跑用例的是同一个解释器）。"""
    try:
        import tkinter  # noqa: F401
    except Exception:  # noqa: BLE001 — 缺 tkinter 就是缺，别挑异常类型
        return False
    return True


def child_env() -> dict[str, str]:
    """给用例进程的环境：默认禁掉自动提权。

    Windows 端取钥要读裸盘，正常路径会 `ShellExecuteW("runas", ...)` 弹 UAC。跑测试时
    没人点那个弹窗（用户原话：「很多时候在测试的时候我是点不了的因为我不在电脑面前」），
    一旦有用例走到取钥就会吊满 90 s 才失败。所以这里默认注入 `VLT_NO_ELEVATE=1`，
    让 `vlt/players/keyd.py` 直接短路报「需要管理员」；真需要提权验证时由调用者显式
    覆盖这个变量，本函数不覆盖已有的值。
    """
    env = dict(os.environ)
    env.setdefault("VLT_NO_ELEVATE", "1")
    return env


def tk_gui_tests(exe: str | None = None) -> set[str]:
    """「传递依赖 tkinter」的用例名单（POSIX 相对路径），供无 tkinter 时跳过。

    复用 `scripts/list_display_tests.py`（**含它自带的自检**）：脚本退出码非 0 一律当
    硬错抛出 —— 否则「解析器某天坏了」会静默变成「名单为空、全部跳过」，比手抄更危险。
    """
    proc = subprocess.run(
        [exe or sys.executable, str(LIST_DISPLAY)],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "scripts/list_display_tests.py 自检失败（依赖图异常？）：\n"
            + (proc.stderr or "").strip()
        )
    return set(proc.stdout.split())


def xvfb_decision(disabled: bool, require: bool = False) -> bool:
    """是否给用例挂 `xvfb-run -a`。返回决定，并在需要时打印一行说明。

    平台差异（对齐 CI 的两个 job）：
      * **Linux** —— GitHub runner 无显示器、且本机常见平铺窗口管理器（Hyprland / niri /
        sway）会把窗口重排成满屏，于是 `test_desktop_overlay*` / `test_i18n` 这类**实测几何**
        的用例会假红（本机实测：不挂 xvfb 时报 `(1960, 40) != (40, 40)`，挂上就绿）。
        CI 的 `linux-tests` 就是 `xvfb-run -a` **逐用例**跑 —— 这里照做。
      * **Windows / macOS** —— 自带真实桌面会话（CI 的 Windows job 也不挂），不套。
      * Linux 上**找不到** `xvfb-run`：本机不硬红，但明确打印提示（别让裸跑变成静默），
        确要接受裸跑就加 `--no-xvfb`；**CI 传 `--require-xvfb` 时直接抛错判红** ——
        否则裸跑下 GUI 用例会走 `_try_tk()` / `没有 DISPLAY` **自跳过并返回 0**，整轮假绿。
    """
    if disabled:
        return False
    if not sys.platform.startswith("linux"):
        return False
    if shutil.which(XVFB_RUN) is None:
        if require:
            raise RuntimeError(
                f"Linux 上找不到 {XVFB_RUN}，但传了 --require-xvfb（CI 不允许裸跑：GUI 用例"
                "会自跳过 → 假绿）—— 装 `xorg-server-xvfb` 后重跑；确要接受裸跑请去掉 "
                "--require-xvfb 并加 --no-xvfb")
        print("⚠️ 没找到 xvfb-run：界面（Tk）用例可能因窗口管理器重排而假红。"
              "装 `xorg-server-xvfb` 后重跑；确要裸跑请加 --no-xvfb。\n")
        return False
    return True


def discover() -> list[Path]:
    """全部待跑用例，按文件名排序（顺序固定，报告可复现）。"""
    return sorted(TESTS_DIR.glob("test_*.py"))


def select(tests: list[Path], only: str | None) -> list[Path]:
    if not only:
        return tests
    return [t for t in tests if only in t.name or fnmatch.fnmatch(t.name, only)]


def build_cmd(test: Path, coverage: bool, xvfb: bool, screen: str) -> list[str]:
    """构造单个用例的命令行。

    `xvfb` 为真时在最前面挂 `xvfb-run -a -s "-screen 0 <screen>"`，**每个用例一个干净、
    无窗口管理器的虚拟 X**，且分辨率**显式钉死**（不依赖发行版默认）—— 与 CI 的
    `linux-tests` 逐用例口径完全一致；覆盖率模式下顺序也相同。Windows / macOS 不套
    （自带桌面会话）。

    覆盖率走 `coverage run -a`（append）而不是 `--parallel-mode` + combine：
    本运行器**严格逐个**起子进程（从不开并行 —— 见技能里「全量测试一次只能跑一份」
    的踩坑），单个 append 目标就够，省掉一批 .coverage.* 与 combine 的簿记。
    """
    if coverage:
        base = [sys.executable, "-m", "coverage", "run", "-a",
                "--source=vlt", str(test)]
    else:
        base = [sys.executable, str(test)]
    if xvfb:
        return [XVFB_RUN, "-a", "-s", f"-screen 0 {screen}", *base]
    return base


def erase_coverage_data() -> None:
    """清掉上一次的覆盖率数据，避免把不同代码版本的结果混进来。"""
    (ROOT / ".coverage").unlink(missing_ok=True)
    for old in ROOT.glob(".coverage.*"):
        old.unlink(missing_ok=True)


def coverage_summary() -> int:
    """打印覆盖率摘要（`--show-missing`）。**只打印**，结论不影响退出码。"""
    print("\n" + "=" * 62)
    print("覆盖率摘要（非阻断；未设阈值）")
    print("=" * 62)
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--show-missing"],
        cwd=ROOT,
    )
    if proc.returncode != 0:
        print(f"（coverage report 退出码 {proc.returncode}：没有任何数据或 coverage 未安装？）")
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    # 逐行刷新：本运行器把子进程输出直接透传到同一个 stdout，而 Python 的 print 在
    # 重定向到文件/CI 日志时是**块缓冲**的 —— 不刷新的话「PASS/SKIP 行 + 覆盖率表」
    # 会晚于（甚至排到）子进程输出之后，日志顺序看着是乱的。
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(
        description="逐文件跑 tests/test_*.py（单文件脚本用例），可选覆盖率摘要。")
    ap.add_argument("--only", metavar="PATTERN",
                    help="只跑文件名匹配 PATTERN 的用例（子串或通配，如 device / 'test_room_*'）")
    ap.add_argument("--with-engine", action="store_true",
                    help=f"连 {ENGINE_TEST} 一起跑（需要真实 API key）")
    ap.add_argument("--coverage", action="store_true",
                    help="累积覆盖率并打印摘要（不设阈值、不阻断）")
    ap.add_argument("--no-xvfb", action="store_true",
                    help="不要自动挂 xvfb-run（默认 Linux 上逐用例挂，与 CI 一致）")
    ap.add_argument("--require-tk", action="store_true",
                    help="缺 tkinter 时直接判红（CI 用，避免静默跳过一批 GUI 用例）")
    ap.add_argument("--require-xvfb", action="store_true",
                    help="Linux 上缺 xvfb-run 时直接判红（CI 用，避免裸跑下 GUI 用例自跳过）")
    ap.add_argument("--xvfb-screen", metavar="WxHxD",
                    help=f"xvfb 虚拟屏分辨率（默认 {DEFAULT_XVFB_SCREEN}，或 env VLT_XVFB_SCREEN）")
    ap.add_argument("--no-gha", action="store_true",
                    help="即使处于 GITHUB_ACTIONS 也不发 ::group:: / ::error 注解")
    args = ap.parse_args(argv)

    ann = Annotator(enabled=gha_enabled() and not args.no_gha)
    screen = args.xvfb_screen or xvfb_screen()

    # ---- tkinter 门禁：CI 缺了就红；本机缺了就跳过依赖它的用例（逐条打印理由）----
    tk_skip: set[str] = set()
    if not tkinter_available():
        if args.require_tk:
            msg = ("解释器没有 tkinter，但传了 --require-tk（CI 不允许静默少跑一批 "
                   "GUI 用例）—— 请安装 tk / 换带 tkinter 的解释器")
            print(f"❌ {msg}")
            ann.error(msg)
            return 3
        try:
            tk_skip = tk_gui_tests()
        except RuntimeError as exc:
            print(f"❌ {exc}")
            return 3
        if not tk_skip:
            print("❌ 解释器没有 tkinter，但 list_display_tests.py 说没有任何用例依赖"
                  " tkinter —— 疑似依赖图解析器坏了，拒绝静默裸跑")
            return 3
        print(f"⚠️ 解释器没有 tkinter：将跳过 {len(tk_skip)} 个依赖 tkinter 的用例"
              "（逐条打印理由，不静默）\n")

    try:
        use_xvfb = xvfb_decision(args.no_xvfb, args.require_xvfb)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        ann.error(str(exc))
        return 3

    tests = select(discover(), args.only)
    if not tests:
        print(f"没有匹配的用例（tests 目录：{TESTS_DIR}，--only={args.only!r}）")
        return 1

    if args.coverage:
        if subprocess.run([sys.executable, "-m", "coverage", "--version"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            print("--coverage 需要 coverage：先 `pip install -r requirements-dev.txt`")
            return 2
        erase_coverage_data()

    if os.environ.get("DASHSCOPE_API_KEY"):
        print("⚠️ 环境里有 DASHSCOPE_API_KEY —— 仓库已知陷阱：它可能顶掉真凭据、"
              "或让用例拿假 key 假红。建议先 `unset DASHSCOPE_API_KEY` 再跑。\n")

    print(f"解释器：{sys.executable}")
    print(f"用例目录：{TESTS_DIR}")
    print(f"共 {len(tests)} 个文件" + (f"（--only={args.only!r}）" if args.only else ""))
    if sys.platform.startswith("linux"):
        print("界面隔离：" + (f"xvfb-run -a -s '-screen 0 {screen}'"
                              "（逐用例一个干净虚拟 X，与 CI 一致）"
                              if use_xvfb else "裸跑（未用 xvfb）"))
    print("-" * 62)

    n_pass = n_fail = n_skip = 0
    failed: list[str] = []
    for test in tests:
        rel = test.relative_to(ROOT).as_posix()
        if test.name == ENGINE_TEST and not args.with_engine:
            n_skip += 1
            print(f"SKIP {test.name} —— {ENGINE_SKIP_REASON}")
            ann.notice(f"{rel} 已跳过（{ENGINE_SKIP_REASON}）")
            continue
        if rel in tk_skip:
            n_skip += 1
            print(f"SKIP {test.name} —— 当前解释器无 tkinter")
            ann.notice(f"{rel} 已跳过（当前解释器无 tkinter）")
            continue
        ann.group(rel)
        proc = subprocess.run(build_cmd(test, args.coverage, use_xvfb, screen),
                              cwd=ROOT, env=child_env())
        ann.endgroup()
        if proc.returncode == 0:
            n_pass += 1
            print(f"PASS {test.name}")
        else:
            n_fail += 1
            failed.append(test.name)
            print(f"FAIL {test.name}（退出码 {proc.returncode}）")
            ann.error("测试失败（见本组日志）", file=rel)

    if args.coverage:
        coverage_summary()

    print("-" * 62)
    print(f"合计：PASS={n_pass} FAIL={n_fail} SKIP={n_skip}")
    if failed:
        print("失败用例：" + ", ".join(failed))
        return 1
    if n_pass == 0:
        msg = (f"没有任何用例真正通过（全是跳过？）：PASS=0 FAIL=0 SKIP={n_skip}"
               " —— 拒绝把「一个都没跑」当成绿。")
        print(f"❌ {msg}")
        ann.error(msg)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
