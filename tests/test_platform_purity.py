#!/usr/bin/env python
"""平台隔离的**源码级**守卫：不必打包就能发现「混进另一个平台实现」的漂移。

## 为什么还要这一层（`check_platform_purity.py` 不是已经查了吗）

`check_platform_purity.py` 查的是**产物**（exe / AppImage），只在打包时/CI 的
package 阶段跑。等它红的时候，往往是「已经构建完了才发现白干一场」。
这里用**同一套判据**（同一份 `FORBIDDEN`、同一个 `_string_blobs`，包括「docstring 不算」
的语义）直接扫仓库源码，把同一件事提前到离线测试里。

## 还钉住一件容易漂移的事：禁列清单 ↔ 构建排除清单必须同步

`FORBIDDEN` 里写了「Windows 不该有 `vlt.output.openxr_overlay`」，而
`scripts/build_exe.py` 的 `EXCLUDE_WIN` 才真正把它排掉 —— 两处**只改一边**就会出现
「断言了一条永远不为真的规则」（禁列写了但没排除 → 打包才炸）或者
「排除了但没人验证」。所以这里做**双向**一致性检查：只查「禁列 ⊆ 排除清单」的话，
「新模块进了排除清单、忘了登记禁列」会静默通过 —— 隔离事实上发生了，产物级断言
却对它视而不见（#94）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_platform_purity as C  # noqa: E402


def _source_entries(excluded: tuple[str, ...]):
    """把仓库里的 `.py` 当成「产物里的条目」：(模块名, 实现字样)。

    `excluded` 里的模块视为「产物里没有」，直接跳过（与被排除的产物一致）。
    """
    for p in sorted((ROOT / "vlt").rglob("*.py")):
        mod = C._appdir_module_name(p.relative_to(ROOT))
        if any(mod == m or mod.startswith(m + ".") for m in excluded):
            continue
        code = compile(p.read_text(encoding="utf-8"), str(p), "exec")
        for blob in C._string_blobs(code):
            yield mod, blob


def _check_platform(platform: str) -> None:
    rules = C.FORBIDDEN[platform]
    names = [C._appdir_module_name(p.relative_to(ROOT))
             for p in sorted((ROOT / "vlt").rglob("*.py"))]

    # 禁列里的 vlt.* 模块**必须真的存在**（否则规则是死的：写了个不存在的名字，
    # 排除清单跟着一起写错也没人发现）。
    missing = [m for m in rules["modules"] if m.startswith("vlt.") and m not in names]
    assert not missing, (f"{platform} 的禁列里有仓库中不存在的模块"
                         f"（规则形同虚设，八成是改名后没同步）：{missing}")

    hits: dict[bytes, list[str]] = {}
    for mod, blob in _source_entries(tuple(rules["modules"])):
        for needle in rules["strings"]:
            if needle in blob:
                hits.setdefault(needle, []).append(mod)
    assert not hits, (
        f"{platform} 侧出现不该有的**实现**字样（docstring 不计）：\n"
        + "\n".join(f"  {k.decode()} ← {sorted(set(v))}" for k, v in hits.items()))
    print(f"  ✓ {platform} 侧源码干净（{len(rules['modules'])} 条禁列模块 / "
          f"{len(rules['strings'])} 条禁列字样）")


def test_string_scope_excludes_third_party() -> None:
    """★ 回归钉：字样判据只扫我们自己的实现（vlt.* / 入口），第三方库不参与。

    反例就在包里：sounddevice（PyInstaller 收进 PYZ 的纯 Python 模块）里写了
    PortAudio 的 `paWASAPI` 枚举常量 —— 它是跨平台库的合法实现，不是
    「Linux 产物混进了 Windows 实现」。以前 AppImage 路线只扫 usr/app（我们自己的
    源码），所以没暴露；PyInstaller 路线要是不设扫描域，就会红一次假警。
    """
    rules = C.FORBIDDEN["linux"]

    # 第三方条目里出现禁列字样 → 不判红
    assert C._judge("linux", rules, ["somepkg"], iter([("sounddevice", b"paWASAPI")])) is True
    # 我们自己的模块里出现同样的字样 → 判红（扫描域内的正确行为）
    assert C._judge("linux", rules, ["somepkg"], iter([("vlt.platform", b"paWASAPI")])) is False
    # 入口脚本也在扫描域内
    assert C._judge("linux", rules, ["somepkg"], iter([("run_gui", b"paWASAPI")])) is False
    print("  ✓ 字样判据的扫描域（第三方不参与）OK")


def test_source_is_platform_clean() -> None:
    """两个方向都扫：Linux 侧不含 Windows 实现，Windows 侧不含 Linux 实现。"""
    _check_platform("linux")
    _check_platform("windows")


def test_forbidden_lists_match_build_excludes() -> None:
    """`FORBIDDEN` 的禁列模块必须**真的**被构建脚本排除/删除。"""
    # Windows：EXCLUDE_WIN 必须覆盖 FORBIDDEN["windows"]["modules"] 里 vlt.* 的项
    src = (ROOT / "scripts" / "build_exe.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    exclude_win: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "EXCLUDE_WIN" for t in node.targets):
            exclude_win = [e.value for e in node.value.elts]      # type: ignore[union-attr]
    assert exclude_win, "没解析出 scripts/build_exe.py 的 EXCLUDE_WIN"
    for mod in C.FORBIDDEN["windows"]["modules"]:
        if mod.startswith("vlt."):
            assert mod in exclude_win, (f"{mod} 在 FORBIDDEN[windows] 里，"
                                        f"却没进 build_exe.py 的 EXCLUDE_WIN：{exclude_win}")
    # ★ 反向也要查（#94）：EXCLUDE_WIN 里的 vlt.* 必须**都登记进** FORBIDDEN。
    #   只查一个方向会漏掉这条触发序列：新加 vlt/output/foo_linux.py → 进了 EXCLUDE_WIN
    #   → **忘了**进 FORBIDDEN ⇒ 隔离事实上发生了，但 check_platform_purity.py 的产物级
    #   断言对这个模块**视而不见** —— 用例全绿，却没有任何断言证明它真被排掉了
    #   （哪天有人把它从 EXCLUDE_WIN 里删掉，也没有网会红）。
    unregistered_win = sorted(m for m in exclude_win if m.startswith("vlt.")
                              and m not in C.FORBIDDEN["windows"]["modules"])
    assert not unregistered_win, (
        f"这些模块进了 build_exe.py 的 EXCLUDE_WIN，却没登记进 "
        f"check_platform_purity.py 的 FORBIDDEN[windows][modules]：{unregistered_win}\n"
        f"  后果：产物级断言会对它们**视而不见**（新模块忘登记）—— 排除生效了没人证明，"
        f"哪天被误删也没人会红。\n"
        f"  修法：把它们加进 FORBIDDEN[windows][modules]（并补上对应的禁列字样）。")

    # Linux：build_appimage.sh 的 --exclude-module 清单必须覆盖 FORBIDDEN["linux"]["modules"]。
    # （PyInstaller 路线是「分析期排除」，不再有旧路线那种「AppDir 里反向删 .py」，
    #   判据从「脚本里出现 xxx.py 路径」改成「EXCLUDES 清单里出现模块名」。）
    import re

    sh = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
    m = re.search(r"EXCLUDES=\(([^)]*)\)", sh)
    excludes = m.group(1).split() if m else []
    assert excludes, "没解析出 build_appimage.sh 的 EXCLUDES（构建排除清单）"
    assert "--exclude-module" in sh, "EXCLUDES 没被 --exclude-module 用上"
    for mod in C.FORBIDDEN["linux"]["modules"]:
        assert mod in excludes, (f"{mod} 在 FORBIDDEN[linux] 里，"
                                 f"却没进 build_appimage.sh 的 EXCLUDES：{excludes}")
    # ★ 反向同理（#94）：EXCLUDES 里的 vlt.* 必须都登记进 FORBIDDEN[linux]，
    #   否则「排除了但产物级断言不看它」—— 见上面 Windows 侧那段说明。
    #   只查 vlt.*：第三方（sounddevice 这类）是否登记是另一码事，不在本条断言的口径里。
    unregistered_linux = sorted(m for m in excludes if m.startswith("vlt.")
                                and m not in C.FORBIDDEN["linux"]["modules"])
    assert not unregistered_linux, (
        f"这些模块进了 build_appimage.sh 的 EXCLUDES，却没登记进 "
        f"check_platform_purity.py 的 FORBIDDEN[linux][modules]：{unregistered_linux}\n"
        f"  后果：产物级断言会对它们**视而不见**（新模块忘登记）—— 排除生效了没人证明，"
        f"哪天被误删也没人会红。\n"
        f"  修法：把它们加进 FORBIDDEN[linux][modules]（并补上对应的禁列字样）。")
    # ⚠️ build_appimage.sh 里还有**第二份**内联清单（heredoc 里的 forbidden = {...}），
    # 它参与「磁盘上有、包里没有」的模块对账。只查 EXCLUDES 会漏掉它 —— 实测踩过：
    # win_d3d11 加进了 EXCLUDES、漏了内联集 → 本机用例全绿、CI 的 appimage 腿红。
    m2 = re.search(r"forbidden = \{([^}]*)\}", sh)
    assert m2, "没解析出 build_appimage.sh 里 heredoc 的内联禁列集合（forbidden = {...}）"
    inline = set(re.findall(r'"([\w.]+)"', m2.group(1)))
    vlt_excl = {m for m in excludes if m.startswith("vlt.")}
    vlt_inline = {m for m in inline if m.startswith("vlt.")}
    assert vlt_excl == vlt_inline, (
        f"build_appimage.sh 里两份 vlt 禁列清单不一致（加平台独占模块时必须同时改两处）："
        f"EXCLUDES={sorted(vlt_excl)} / 内联={sorted(vlt_inline)}")
    print("  ✓ 禁列清单与两侧构建排除清单同步（含 heredoc 内联集合）")


if __name__ == "__main__":
    print("test_platform_purity:")
    test_string_scope_excludes_third_party()
    test_source_is_platform_clean()
    test_forbidden_lists_match_build_excludes()
    print("ALL PASSED")
