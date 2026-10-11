#!/usr/bin/env python
"""Windows 产物的第二道网：`scripts/build_exe.py` 的「磁盘 vs 包内」模块对账。

## 为什么要有这道网（平台纯度检查不够吗）

`scripts/check_platform_purity.py --platform windows` 只回答「包里**不该有**的有没有混进来」。
它答不了另一个方向：「磁盘上的 `vlt` 模块**该收的都收进去了吗**」。

后者才是 Windows 侧以前缺的那道网 —— 本项目的模块大量是**在函数里按需导入**的
（没有对应硬件/驱动的机器上也要能启动），PyInstaller 的静态分析看不到它们，
漏加 `build_exe.py` 的 `HIDDEN` 的后果是：**构建绿、CI 绿、用户机器上
`ModuleNotFoundError`**（或更隐蔽：`vlt/i18n.py` 的词表加载失败 → 界面静默回落中文）。

Linux 侧一直有这道网（`scripts/build_appimage.sh` 第 3 步的 `pkg_archive_contents`
对账）。这里把 Windows 侧的判据（`build_exe.missing_vlt_modules`）用**纯函数级**用例钉住：
不打包就能跑，因此 CI 的 tests job 也覆盖得到。

## 钉住的几件事

1. 全在 → 返回空；2. 漏收的模块被抓；3. `allowed_missing`（平台独占）生效；
4. **静态检查**：`build_exe.py` 里的 `allowed_missing` 确实是从 `EXCLUDE_WIN` **派生**的
   —— 严禁抄第二份清单（#93 的教训：两份必然漂移）；
5. `HIDDEN` 里的模块不允许被 `allowed_missing` 豁免（那是「必须收进去」的承诺）；
6. 对账调用点在 `--no-verify` 的短路**之前**（CI 的 package job 正是带 `--no-verify` 跑的）。
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_exe as B  # noqa: E402

BUILD_EXE = ROOT / "scripts" / "build_exe.py"


def _disk_modules(vlt_dir: Path) -> set[str]:
    """磁盘上的 `vlt` 模块名（`vlt/output/__init__.py` → `vlt.output`）。

    与被测函数同一套命名规则 —— 这里是**期望值**那一侧，故意各写一遍：
    两边共用一份实现的话，命名规则写错了也一起错，用例照样绿。
    """
    out: set[str] = set()
    for p in sorted(vlt_dir.rglob("*.py")):
        rel = p.relative_to(vlt_dir.parent).with_suffix("")
        if rel.name == "__init__":
            rel = rel.parent
        out.add(".".join(rel.parts))
    return out


def _fake_vlt(tmp: Path, modules: list[str]) -> Path:
    """按点分名造一棵假的 `vlt/` 源码树（各级包补 `__init__.py`）。"""
    vlt = tmp / "vlt"
    vlt.mkdir(parents=True, exist_ok=True)
    (vlt / "__init__.py").write_text("", encoding="utf-8")
    for mod in modules:
        parts = mod.split(".")
        if len(parts) == 1:
            continue
        leaf = vlt.joinpath(*parts[1:])
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.with_suffix(".py").write_text("", encoding="utf-8")
    for d in [p for p in vlt.rglob("*") if p.is_dir()]:
        (d / "__init__.py").write_text("", encoding="utf-8")
    return vlt


def _assigned_value(tree: ast.AST, name: str):
    """`name = <expr>` 的 `<expr>`（找不到就返回 None）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == name for t in node.targets):
            return node.value
    return None


def _calls_of(tree: ast.AST, func_name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == func_name]


def test_all_present_returns_empty() -> None:
    """① 磁盘上的模块全都收进产物了 → 一个都不报。"""
    with tempfile.TemporaryDirectory() as td:
        mods = ["vlt", "vlt.app", "vlt.output", "vlt.output.chatbox",
                "vlt.platform", "vlt.platform.win"]
        vlt = _fake_vlt(Path(td), mods)
        names = set(mods) | {"numpy", "run_gui", "PIL.Image"}   # 第三方/入口不影响判据
        got = B.missing_vlt_modules(names, vlt, set())
        assert got == [], f"模块全在包里却报缺失：{got}"
    print("  ✓ 全在 → 返回空")


def test_uncollected_module_is_caught() -> None:
    """② 漏收的模块必须被抓出来（这正是「构建绿、用户机器上炸」的那一类）。"""
    with tempfile.TemporaryDirectory() as td:
        mods = ["vlt", "vlt.app", "vlt.output", "vlt.output.chatbox", "vlt.i18n",
                "vlt.locales", "vlt.locales.en"]
        vlt = _fake_vlt(Path(td), mods)
        names = set(mods) - {"vlt.output.chatbox", "vlt.locales", "vlt.locales.en"}
        got = B.missing_vlt_modules(names, vlt, set())
        assert got == ["vlt.locales", "vlt.locales.en", "vlt.output.chatbox"], (
            f"漏收的模块没被完整抓出来：{got}")
    print("  ✓ 漏收模块被抓（含按需加载的词表包）")


def test_allowed_missing_exempts() -> None:
    """③ `allowed_missing` 生效：故意排除的平台独占模块不报缺失（含其子模块）。"""
    with tempfile.TemporaryDirectory() as td:
        mods = ["vlt", "vlt.output", "vlt.output.chatbox", "vlt.platform",
                "vlt.platform.linux", "vlt.platform.linux.pw"]
        vlt = _fake_vlt(Path(td), mods)
        names = {"vlt", "vlt.output", "vlt.output.chatbox", "vlt.platform"}
        allowed = {"vlt.platform.linux"}
        assert B.missing_vlt_modules(names, vlt, allowed) == [], (
            "allowed_missing 没生效：被 EXCLUDE_WIN 排掉的模块仍被报成缺失")
        # 但豁免是**按名字**的，不能顺手把别的漏收模块一起放过去
        partial = names - {"vlt.output.chatbox"}
        assert B.missing_vlt_modules(partial, vlt, allowed) == ["vlt.output.chatbox"], (
            "allowed_missing 越权豁免了清单外的模块")
    print("  ✓ allowed_missing 生效（前缀语义，且不越权）")


def test_allowed_missing_is_derived_from_exclude_win() -> None:
    """④ 静态检查：`build_exe.py` 传给对账的 `allowed_missing` 必须从 `EXCLUDE_WIN` 派生。

    两种写法都要判红：
      * **抄第二份清单**（`allowed_missing = {"vlt.platform.linux", ...}`）—— #93 的教训：
        两份清单必然漂移，漂了就是「排除了但对账不看它」；
      * **写死成空集**（`set()` / `{}`）—— 所有平台独占模块会被误报成缺失，
        于是有人为了让构建变绿把它们塞进 `HIDDEN` ⇒ Linux 实现混进 Windows 产物。
    """
    tree = ast.parse(BUILD_EXE.read_text(encoding="utf-8"))
    calls = _calls_of(tree, "missing_vlt_modules")
    assert calls, "build_exe.py 里没调用 missing_vlt_modules(...) —— 打包后的对账没接上？"
    for call in calls:
        arg = next((kw.value for kw in call.keywords if kw.arg == "allowed_missing"), None)
        if arg is None:
            assert len(call.args) >= 3, (
                f"missing_vlt_modules 的调用没传 allowed_missing（第 3 个参数）：{ast.unparse(call)}")
            arg = call.args[2]
        if isinstance(arg, ast.Name):                    # 局部变量 → 看它的赋值表达式
            arg = _assigned_value(tree, arg.id) or arg
        assert not isinstance(arg, (ast.Set, ast.List, ast.Tuple, ast.Dict, ast.Constant)), (
            f"allowed_missing 写死成了字面量清单（第二份清单必然与 EXCLUDE_WIN 漂移）："
            f"{ast.unparse(arg)}")
        used = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
        assert "EXCLUDE_WIN" in used, (
            f"allowed_missing 不是从 EXCLUDE_WIN 派生的（写死成空集 / 抄第二份清单都会到这）："
            f"{ast.unparse(arg)}\n"
            f"  正确写法：allowed_missing = {{m for m in EXCLUDE_WIN if m.startswith('vlt.')}}")
    print(f"  ✓ allowed_missing 从 EXCLUDE_WIN 派生（{len(calls)} 处调用）")


def test_hidden_is_never_exempted() -> None:
    """⑤ `HIDDEN` 里的模块**不允许**缺失 —— 也就不能被 `allowed_missing` 豁免掉。

    同一个模块既在 `HIDDEN`（要求收进去）又在 `EXCLUDE_WIN`（要求排掉）是配置自相矛盾：
    PyInstaller 那边各让一步，结果是「排掉了、对账也过了」，`HIDDEN` 的承诺被静默吞掉。
    `reconcile_modules()` 里有一条运行时断言，这里在**不打包**的情况下钉住同一件事。
    """
    allowed = {m for m in B.EXCLUDE_WIN if m.startswith("vlt.")}
    hidden = {h for h in B.HIDDEN if h.startswith("vlt")}
    swallowed = sorted(h for h in hidden
                       if any(h == a or h.startswith(a + ".") for a in allowed))
    assert not swallowed, (
        f"这些模块在 HIDDEN 里被要求收进产物，却被 EXCLUDE_WIN 的排除豁免掉了：{swallowed}\n"
        f"  按需导入的留 HIDDEN；平台独占的留 EXCLUDE_WIN —— 二选一。")
    print(f"  ✓ HIDDEN 的 {len(hidden)} 个 vlt 模块一个都没被 allowed_missing 豁免")


def test_real_tree_reconciles_with_derived_allowance() -> None:
    """⑥ 拿**仓库真实的** `vlt/` 源码树跑一遍对账（不打包，产物条目按排除清单模拟）。

    顺带钉住故障注入的那个方向：`allowed_missing` 一旦是空集，`EXCLUDE_WIN` 里的
    平台独占模块立刻全部被误报成「缺失」—— 也就是说这道网**不允许**被写死成空集。
    """
    vlt_dir = ROOT / "vlt"
    disk = _disk_modules(vlt_dir)
    allowed = {m for m in B.EXCLUDE_WIN if m.startswith("vlt.")}
    assert allowed, "EXCLUDE_WIN 里一个 vlt.* 都没有 —— 派生出的 allowed_missing 会是空集？"
    on_disk_allowed = sorted(m for m in allowed
                             if any(m == d or d.startswith(m + ".") for d in disk))
    assert on_disk_allowed, (
        f"EXCLUDE_WIN 里的 vlt.* 在磁盘上一个都找不到（模块改名了？）：{sorted(allowed)}")

    names = disk - set(on_disk_allowed)      # 模拟「除排除项以外全收进产物」
    assert B.missing_vlt_modules(names, vlt_dir, allowed) == [], (
        "真实源码树 + 派生的 allowed_missing 本该对平，却报了缺失")
    # 故障注入等价物：allowed_missing 写死成空集 → 平台独占模块全被误报
    false_positives = B.missing_vlt_modules(names, vlt_dir, set())
    assert set(on_disk_allowed) <= set(false_positives), (
        f"allowed_missing 写死成空集时，本该被误报的平台独占模块没全报出来："
        f"{false_positives}")
    print(f"  ✓ 真实 vlt/ 树对平（{len(disk)} 个模块，其中 {len(on_disk_allowed)} 个按 "
          f"EXCLUDE_WIN 排除；空 allowed_missing 会误报 {len(false_positives)} 个）")


def test_reconcile_runs_before_no_verify_shortcut() -> None:
    """⑦ 对账调用点必须在 `if args.no_verify: return 0` **之前**。

    `--no-verify` 只该跳「真跑一次 `--self-test`」；模块对账是静态的，必须一直跑 ——
    CI 的 package job 与 release job 都是带 `--no-verify` 调 build_exe.py 的，
    放到短路后面等于这道网在 CI 里从来不跑（正是它要防的那种「假绿」）。
    """
    tree = ast.parse(BUILD_EXE.read_text(encoding="utf-8"))
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main_fn is not None, "build_exe.py 里没有 main()"
    calls = _calls_of(main_fn, "reconcile_modules")
    assert calls, "main() 里没调用 reconcile_modules(exe) —— 打完包没做模块对账"
    shortcuts = [n.lineno for n in ast.walk(main_fn)
                 if isinstance(n, ast.If) and "no_verify" in ast.dump(n.test)]
    assert shortcuts, ("main() 里找不到 `if args.no_verify` 短路 —— 结构变了？"
                       "请人工确认模块对账仍然不受 --no-verify 影响，再改这条用例")
    assert calls[0].lineno < min(shortcuts), (
        f"reconcile_modules(exe)（第 {calls[0].lineno} 行）在 `if args.no_verify`"
        f"（第 {min(shortcuts)} 行）之后 —— CI 带 --no-verify 跑时这道网根本不会执行")
    print(f"  ✓ 对账在 --no-verify 短路之前（第 {calls[0].lineno} 行 < 第 {min(shortcuts)} 行）")


if __name__ == "__main__":
    print("test_build_exe_modules:")
    test_all_present_returns_empty()
    test_uncollected_module_is_caught()
    test_allowed_missing_exempts()
    test_allowed_missing_is_derived_from_exclude_win()
    test_hidden_is_never_exempted()
    test_real_tree_reconciles_with_derived_allowance()
    test_reconcile_runs_before_no_verify_shortcut()
    print("ALL PASSED")
