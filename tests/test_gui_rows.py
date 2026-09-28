"""界面气泡回归测试：流式增量不能把每句话变成两条气泡。

背景（真实踩过的坑）：终版事件到来时，界面若"先把当前气泡封顶、再新插一条"，
每句话都会出现两条内容相同的相邻气泡。修法是让终版**就地更新当前气泡并封口**。

规则：气泡数 == 终版事件数；且不存在内容完全相同的相邻气泡。

需要 Tk（Windows 上标准库自带）。若在无显示环境跑会跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(events: list[tuple], expect_finals: int) -> bool:
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    try:
        for ev in events:
            src, txt, final = ev[:3]
            who = ev[3] if len(ev) > 3 else "mine"
            gui._add_text(src, txt, final, who=who)
        bubbles = gui._bubbles
        data = [(b.source, b.text) for b in bubbles]

        dupes = sum(1 for a, b in zip(data, data[1:])
                    if str(a[0]) == str(b[0]) and str(a[1]) == str(b[1]))
        ok = len(data) == expect_finals and dupes == 0
        print(f"  事件 {len(events)} 条（终版 {sum(1 for e in events if e[2])}）→ "
              f"气泡 {len(data)}（期望 {expect_finals}），相邻重复 {dupes}  "
              f"{'OK' if ok else '✗'}")
        if not ok:
            for i, v in enumerate(data, 1):
                print(f"    第{i}条: {v[0]!r} / {v[1]!r}")
        return ok
    finally:
        try:
            gui._root.destroy()
        except Exception:
            pass


def test_growing_bubble_shifts_later_bubbles() -> bool:
    """★ 回归：流式气泡长高时，下面的气泡必须真的下移（否则重叠）。

    真实事故（用户日志里抓到的 TclError）：

        File "vlt/gui.py", line 1281, in _redraw_current
            self._canvas.move(*other.items, 0, delta)
        _tkinter.TclError: wrong # args: should be
            ".!frame6.!canvas move tagOrId xAmount yAmount"

    `Canvas.move` 只接受**一个** tagOrId；双行气泡有 ≥2 个图元 → 必然抛错 →
    下面的气泡不移位 → 视觉上叠在一起。
    """
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    try:
        gui._root.geometry("920x620")
        gui._root.update()
        gui._add_text("", "第一句", False, who="mine")          # 未终版 → 之后还会增长
        gui._add_text("", "第二句", True, who="theirs")
        gui._root.update()

        later = gui._bubbles[1]
        assert later.items, "第二条气泡没有图元"
        assert len(later.items) >= 2, f"预期双行气泡有 ≥2 个图元，实际 {len(later.items)}"
        before = gui._canvas.coords(later.items[0])[1]

        # 把第一条撑长 → 触发 _redraw_current 里的 delta 位移路径
        gui._add_text("", "第一句变得很长很长" * 20, False, who="mine")
        gui._root.update()
        after = gui._canvas.coords(later.items[0])[1]

        assert after > before, f"下面的气泡没有下移：{before} → {after}（会重叠）"
        print(f"  ✓ 气泡长高时下面的气泡下移（y {before} → {after}，图元 {len(later.items)} 个）")
        return True
    finally:
        gui._root.destroy()


def test_room_row_present_and_not_clipped() -> bool:
    """★ 房间行（批次 2b）：独立成行、控件齐全，且没被窗口裁掉。

    真实坑（BRIEF 反复强调）：Tk 空间不足时**从最后打包的控件开始裁**，把新勾选框
    塞进已经拥挤的行 → 用户报「我没看到那个勾选框」。所以房间必须**独立成行**，
    并按既有方式断言 `winfo_reqwidth() <= winfo_width()`（装得下、没被裁）。
    """
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    try:
        gui._root.update_idletasks()
        gui._root.update()

        # ① 行与控件都在，且确实被 pack 进了布局
        row = getattr(gui, "_room_row", None)
        assert row is not None, "没有房间行（_room_row 不存在）"
        assert row.winfo_manager() == "pack", \
            f"房间行没有被 pack（manager={row.winfo_manager()!r}）"
        for attr in ("_room_var", "_room_code_var", "_room_nick_var", "_room_status"):
            assert hasattr(gui, attr), f"房间行缺控件/变量：{attr}"
        classes = {w.winfo_class() for w in row.winfo_children()}
        assert "Checkbutton" in classes, f"房间行里没有「房间」勾选框：{classes}"
        assert "TEntry" in classes, f"房间行里没有输入框（房间码/昵称）：{classes}"
        assert "TLabel" in classes, f"房间行里没有标签：{classes}"
        assert str(gui._room_status.cget("text")).strip(), "房间状态标签是空的"

        # ② 没被裁：需求宽度 <= 实际宽度（屏幕本身更窄时属物理限制，放行——与 test_i18n 同口径）
        need, have = row.winfo_reqwidth(), row.winfo_width()
        screen = int(gui._root.winfo_screenwidth() or 0)
        win_have = gui._root.winfo_width()
        fits = need <= have + 1 or (screen and win_have >= screen - 32)
        assert fits, (f"房间行被裁：需求 {need}px，实际只有 {have}px"
                      f"（窗口 {win_have}px / 屏宽 {screen}px）")
        print(f"  ✓ 房间行独立存在、控件齐全、未被裁（req {need} <= width {have}；"
              f"状态文案 {str(gui._room_status.cget('text'))!r}）")
        return True
    finally:
        try:
            gui._root.destroy()
        except Exception:
            pass


def test_room_first_toggle_after_section_created() -> bool:
    """★ 回归：老 config.yaml 没有 `room:` 段时，勾选房间必须**当场**就能连（不该要求重启）。

    真实事故（用户日志 2026-09-28）：

        22:56:02 [gui] 房间设置已保存：enabled=true room_code='' nickname=''
        22:56:02 [room] ❌ 房间链路没启动：没填 server_url（config.yaml 的 room.server_url）

    而那个 config.yaml 里**有** server_url：补建的只是**文件**，内存里的 `RoomConfig`
    还是「配置里没有 room 段」时的默认值（server_url 为空）→ 启动校验直接拒 →
    用户看到「勾了房间没反应」，重启一次才好。
    """
    import tempfile

    import vlt.gui as gui_mod
    from vlt.gui import TranslationGUI
    from vlt.room.model import RoomConfig

    gui = TranslationGUI()
    real_default = gui_mod.DEFAULT_CONFIG
    tmp = Path(tempfile.mkdtemp()) / "config.yaml"
    try:
        # 老用户的配置：完全没有 room 段
        tmp.write_text("session:\n  api_key: ''\n", encoding="utf-8")
        gui_mod.DEFAULT_CONFIG = tmp
        gui._room_cfg = RoomConfig.from_dict({})       # = 启动时读到「没有 room 段」的状态
        assert not gui._room_cfg.server_url, "前置条件：此时内存里的 server_url 应为空"

        gui._room_var.set(True)                        # 勾上「房间」
        gui._room_code_var.set("testtest")
        gui._room_nick_var.set("sand")
        gui._sync_room_cfg_from_fields()
        gui._save_room_cfg()

        text = tmp.read_text(encoding="utf-8")
        file_ok = "room:" in text and "vlt-room.kcm-nixi.cn" in text
        mem_ok = bool(gui._room_cfg.server_url)
        print(f"  补建后 server_url：文件={file_ok}，内存={gui._room_cfg.server_url!r}"
              f"（房间码 {gui._room_cfg.room_code!r}）  {'OK' if file_ok and mem_ok else '✗'}")
        assert file_ok, "room 段没有被补建进 config.yaml"
        assert mem_ok, ("内存里的 server_url 仍为空 → 首次勾选还是会报「没填 server_url」，"
                        "用户必须重启一次（这正是本用例防的回归）")
        return True
    finally:
        gui_mod.DEFAULT_CONFIG = real_default
        try:
            gui._root.destroy()
        except Exception:
            pass


def main() -> int:
    cases = [
        # 一句话：增量 → 增量 → 终版，应只占 1 条气泡
        ([("你好", "Hello", False),
          ("你好 世界", "Hello world", False),
          ("你好 世界", "Hello world", True)], 1),

        # 首条就是终版（极短句），也应只占 1 条气泡
        ([("谢谢", "Thanks", True)], 1),

        # 服务端分段：终版 → 继续增量 → 再终版，应占 2 条气泡（两条终版）
        ([("第一部分", "Part one", True),
          ("第二部分", "Part two", True)], 2),

        # 混合多句
        ([("甲", "A", False), ("甲", "A", True),
          ("乙", "B", False), ("乙 丙", "B C", False), ("乙 丙", "B C", True),
          ("丁", "D", True)], 3),

        # 双向同时：左右两路流各自就地更新，互不干扰
        ([("你好", "Hello", False, "mine"),
          ("Hi there", "嗨", False, "theirs"),
          ("你好 世界", "Hello world", True, "mine"),
          ("Hi there, friend", "嗨，朋友", True, "theirs")], 2),
    ]

    print("界面气泡回归测试：")
    all_ok = True
    try:
        all_ok &= test_growing_bubble_shifts_later_bubbles()
    except AssertionError as exc:
        print(f"  ❌ 气泡长高时下移失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_room_row_present_and_not_clipped()
    except AssertionError as exc:
        print(f"  ❌ 房间行检查失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_room_first_toggle_after_section_created()
    except AssertionError as exc:
        print(f"  ❌ 首次勾选房间失败：{exc}")
        all_ok = False
    for i, (events, expect) in enumerate(cases, 1):
        print(f"用例 {i}:")
        all_ok &= check(events, expect)

    print()
    if all_ok:
        print("✅ 全部通过（终版不重复插气泡）")
        return 0
    print("❌ 有失败用例")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
