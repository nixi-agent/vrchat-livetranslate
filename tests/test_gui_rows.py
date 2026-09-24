"""界面行数回归测试：流式增量不能把每句话变成两行。

背景（真实踩过的坑）：终版事件到来时，界面若"先把当前行封顶、再新插一行"，
每句话都会出现两条内容相同的相邻行。修法是让终版**就地更新当前行并封行**。

规则：界面行数 == 终版事件数；且不存在内容完全相同的相邻行。

需要 Tk（Windows 上标准库自带）。若在无显示环境跑会跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(events: list[tuple[str, str, bool]], expect_finals: int) -> bool:
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    try:
        for src, txt, final in events:
            gui._add_text(src, txt, final)
        rows = gui._tree.get_children()
        data = [gui._tree.item(r)["values"] for r in rows]

        dupes = sum(1 for a, b in zip(data, data[1:])
                    if str(a[1]) == str(b[1]) and str(a[2]) == str(b[2]))
        ok = len(data) == expect_finals and dupes == 0
        print(f"  事件 {len(events)} 条（终版 {sum(1 for e in events if e[2])}）→ "
              f"行数 {len(data)}（期望 {expect_finals}），相邻重复 {dupes}  "
              f"{'OK' if ok else '✗'}")
        if not ok:
            for i, v in enumerate(data, 1):
                print(f"    第{i}行: {v[1]!r} / {v[2]!r}")
        return ok
    finally:
        try:
            gui._root.destroy()
        except Exception:
            pass


def main() -> int:
    cases = [
        # 一句话：增量 → 增量 → 终版，应只占 1 行
        ([("你好", "Hello", False),
          ("你好 世界", "Hello world", False),
          ("你好 世界", "Hello world", True)], 1),

        # 首条就是终版（极短句），也应只占 1 行
        ([("谢谢", "Thanks", True)], 1),

        # 服务端分段：终版 → 继续增量 → 再终版，应占 2 行（两条终版）
        ([("第一部分", "Part one", True),
          ("第二部分", "Part two", True)], 2),

        # 混合多句
        ([("甲", "A", False), ("甲", "A", True),
          ("乙", "B", False), ("乙 丙", "B C", False), ("乙 丙", "B C", True),
          ("丁", "D", True)], 3),
    ]

    print("界面行数回归测试：")
    all_ok = True
    for i, (events, expect) in enumerate(cases, 1):
        print(f"用例 {i}:")
        all_ok &= check(events, expect)

    print()
    if all_ok:
        print("✅ 全部通过（终版不重复插行）")
        return 0
    print("❌ 有失败用例")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
