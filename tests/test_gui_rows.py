"""界面回归测试：流式气泡不重复 + 房间行（连接按钮）的排布与守卫。

## 气泡（原始用例）

背景（真实踩过的坑）：终版事件到来时，界面若"先把当前气泡封顶、再新插一条"，
每句话都会出现两条内容相同的相邻气泡。修法是让终版**就地更新当前气泡并封口**。

规则：气泡数 == 终版事件数；且不存在内容完全相同的相邻气泡。

## 房间行

第三行只留**高频**动作：一个「连接房间 / 断开连接」按钮 + 状态文案；房间码、昵称
这类「填一次就不动」的输入搬进了「设置 → 房间」页。这里钉住三件事：行没被裁、
按钮文案随连接态变、空房间码不许静默起连接。

需要 Tk（Windows 上标准库自带）。若在无显示环境跑会跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 界面文案跟随系统语言，钉死成中文保证可复现（CI 是英文系统，不钉会红）。
# 必须在构造 TranslationGUI 之前打上 —— 界面文案在建窗时就按当时的语言取词。
import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

# 电平探针的「打开设备」这一环打桩：用例会打开设置窗，探针若真去开设备，
# 就会碰到**用户真实的 VRChat 播放流** —— 离线测试的硬规则是绝不碰真声卡。
# 打桩后探针走「打不开 → 低频重试」路径，线程照常起停，界面逻辑不受影响。
import vlt.level_probe as _level_probe  # noqa: E402


def _no_audio_probe(device_name: str | None = None) -> None:  # noqa: ANN001
    raise RuntimeError("测试进程：不打开真实电平采集（用例只验界面逻辑）")


_level_probe.open_level_source = _no_audio_probe


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


def test_peer_bubble_shows_nickname_and_source() -> bool:
    """★ 分玩家气泡：昵称 / 原文 / 译文三行都要在（主窗口不能只显示译文）。

    真实事故：分玩家条目复用了房间成员的渲染路径（`label` = 昵称），而气泡的
    小字槽只画 `label if label else source` —— 昵称永远非空（最少也是 actorN），
    原文就被静默吞掉，三块屏里只有主窗口少了原文。
    """
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    try:
        gui._root.geometry("920x620")
        gui._root.update()
        gui._add_text("こんにちは、元気？", "你好，最近怎么样？", True,
                      who="peer:actor1301", label="阿诺owo")
        gui._root.update()
        b = gui._bubbles[-1]
        texts = [str(gui._canvas.itemcget(iid, "text"))
                 for iid in b.items if gui._canvas.type(iid) == "text"]
        joined = "\n".join(texts)
        for want, what in (("阿诺owo", "昵称"),
                           ("こんにちは、元気？", "原文"),
                           ("你好，最近怎么样？", "译文")):
            assert want in joined, f"气泡里没有{what}（{want!r}）：{texts}"

        # 房间成员：只有昵称、没有原文 → 小字区仍是一行，不留空行
        gui._add_text("", "房间成员的译文", True,
                      who="peer:room1", label="某成员")
        gui._root.update()
        rb = gui._bubbles[-1]
        rtexts = [str(gui._canvas.itemcget(iid, "text"))
                  for iid in rb.items if gui._canvas.type(iid) == "text"]
        assert "某成员" in rtexts, f"房间气泡丢了昵称：{rtexts}"
        assert all("\n" not in t for t in rtexts if t != "某成员"), \
            f"房间气泡的小字区被塞了多余行：{rtexts}"
        print(f"  ✓ 分玩家气泡含昵称/原文/译文；房间气泡仍只有昵称：{rtexts}")
        return True
    finally:
        gui._root.destroy()


def test_room_row_present_and_not_clipped() -> bool:
    """★ 房间行：独立成行、只留「连接房间」按钮 + 状态，且没被窗口裁掉。

    真实坑（BRIEF 反复强调）：Tk 空间不足时**从最后打包的控件开始裁**，把它塞进
    已经拥挤的行 → 用户报「我没看到那个按钮」。所以房间必须**独立成行**，
    并按既有方式断言 `winfo_reqwidth() <= winfo_width()`（装得下、没被裁）。

    这一行**不该再有**勾选框和房间码/昵称输入框 —— 那些是「填一次就不动」的低频输入，
    已搬进「设置 → 房间」页（见 test_room_button_text_and_empty_code_guard）。
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
        for attr in ("_room_var", "_room_btn", "_room_status"):
            assert hasattr(gui, attr), f"房间行缺控件/变量：{attr}"
        classes = {w.winfo_class() for w in row.winfo_children()}
        assert "TButton" in classes, f"房间行里没有「连接房间」按钮：{classes}"
        assert "TLabel" in classes, f"房间行里没有状态标签：{classes}"
        assert str(gui._room_status.cget("text")).strip(), "房间状态标签是空的"

        # ② 没被裁：需求宽度 <= 实际宽度（屏幕本身更窄时属物理限制，放行——与 test_i18n 同口径）
        need, have = row.winfo_reqwidth(), row.winfo_width()
        screen = int(gui._root.winfo_screenwidth() or 0)
        win_have = gui._root.winfo_width()
        fits = need <= have + 1 or (screen and win_have >= screen - 32)
        assert fits, (f"房间行被裁：需求 {need}px，实际只有 {have}px"
                      f"（窗口 {win_have}px / 屏宽 {screen}px）")
        print(f"  ✓ 房间行独立存在、按钮+状态齐全、未被裁（req {need} <= width {have}；"
              f"按钮 {str(gui._room_btn.cget('text'))!r}，"
              f"状态文案 {str(gui._room_status.cget('text'))!r}）")
        return True
    finally:
        try:
            gui._root.destroy()
        except Exception:
            pass


def test_room_first_toggle_after_section_created() -> bool:
    """★ 回归：老 config.yaml 没有 `room:` 段时，点「连接房间」必须**当场**就能连（不该要求重启）。

    真实事故（用户日志 2026-09-28）：

        22:56:02 [gui] 房间设置已保存：enabled=true room_code='' nickname=''
        22:56:02 [room] ❌ 房间链路没启动：没填 server_url（config.yaml 的 room.server_url）

    而那个 config.yaml 里**有** server_url：补建的只是**文件**，内存里的 `RoomConfig`
    还是「配置里没有 room 段」时的默认值（server_url 为空）→ 启动校验直接拒 →
    用户看到「点了连接房间没反应」，重启一次才好。
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

        # 打桩：本用例要验的是「补建 room 段 + 回读内存」，不是真去连线上房间服务器
        started: list[str] = []
        gui._start_room = lambda: started.append(gui._room_cfg.server_url)

        gui._room_code_var.set("testtest")
        gui._room_nick_var.set("sand")
        gui._on_room_button()                          # = 原来的「勾上房间」，现在走按钮路径
        gui._sync_room_cfg_from_fields()
        gui._save_room_cfg()

        text = tmp.read_text(encoding="utf-8")
        file_ok = "room:" in text and "vlt-room.kcm-nixi.cn" in text
        mem_ok = bool(gui._room_cfg.server_url)
        print(f"  补建后 server_url：文件={file_ok}，内存={gui._room_cfg.server_url!r}"
              f"（房间码 {gui._room_cfg.room_code!r}，_start_room 被调 {len(started)} 次）  "
              f"{'OK' if file_ok and mem_ok else '✗'}")
        assert file_ok, "room 段没有被补建进 config.yaml"
        assert mem_ok, ("内存里的 server_url 仍为空 → 首次点「连接房间」还是会报「没填 server_url」，"
                        "用户必须重启一次（这正是本用例防的回归）")
        assert started and started[0], ("按钮路径没走到 _start_room，或走到时 server_url 还是空的："
                                        f"{started!r}")
        return True
    finally:
        gui_mod.DEFAULT_CONFIG = real_default
        try:
            gui._root.destroy()
        except Exception:
            pass


def test_room_button_text_and_empty_code_guard() -> bool:
    """★ 房间按钮：文案随连接态变；空房间码不许静默起连接。

    改造把勾选框换成了按钮，于是「按钮上写的是什么」成了唯一的操作提示 ——
    写错就等于让用户点一个会做反事情的按钮。三件事：
      ① 没连上 → 「连接房间」（可点）；
      ② 房间码为空 → **不建连接** + 状态栏明确提示 + 弹窗直接停在「房间」页
         （「点了什么都没发生」是最坏的失败模式，禁静默）；
      ③ 已连上 → 「断开连接」。用最小替身，绝不真联网。
    """
    from types import SimpleNamespace

    from vlt.gui import TranslationGUI
    from vlt.room.model import ConnectionState

    t = _i18n.t
    gui = TranslationGUI()
    try:
        # ① 初始没有 client → 「连接房间」，且可点
        assert gui._room is None, "前置条件：初始不该有房间连接"
        text = str(gui._room_btn.cget("text"))
        assert text == t("连接房间"), f"初始按钮文案应是「连接房间」，实际 {text!r}"
        assert str(gui._room_btn.cget("state")) == "normal", "初始按钮不该是禁用态"
        assert str(gui._room_btn.cget("style")) == "Accent.TButton", \
            f"未连接时该用蓝色主按钮样式，实际 {gui._room_btn.cget('style')!r}"
        print(f"  ✓ ① 未连接 → 按钮 {text!r}（可点，蓝色 Accent）")

        # ② 房间码为空 → 不起连接 + 状态栏提示 + 弹窗停在「房间」页
        gui._room_code_var.set("")
        gui._on_room_button()
        assert gui._room is None, "★ 房间码为空却建了连接（应该先拦住）"
        warn = t("先在「设置 → 房间」里填房间码")
        got = str(gui._status_label.cget("text"))
        assert warn in got, f"状态栏没出现空房间码提示：{got!r}"
        nb = gui._settings_nb
        assert nb.index(nb.select()) == nb.index(gui._settings_tabs[t("房间")]), \
            "设置弹窗没有停在「房间」页（提示了却没把人送到该填的地方）"
        print(f"  ✓ ② 空房间码 → 没建连接，状态栏 {got!r}，弹窗已停在「房间」页")
        gui._close_settings()

        # ③ 最小替身模拟「已连接」：_refresh_room_btn 只调 room.state()
        gui._room = SimpleNamespace(
            stop=lambda timeout=5.0: None,     # _stop_room 只会这么调它
            state=lambda: SimpleNamespace(conn=ConnectionState.ONLINE, peer_count=2))
        gui._refresh_room_btn()
        text = str(gui._room_btn.cget("text"))
        assert text == t("断开连接"), f"已连接时按钮文案应是「断开连接」，实际 {text!r}"
        assert str(gui._room_btn.cget("state")) == "normal", "已连接时按钮该可点（点了就断开）"
        assert str(gui._room_btn.cget("style")) == "Danger.TButton", \
            f"已连接时该换成反向动作样式（与「连接房间」不同色），实际 {gui._room_btn.cget('style')!r}"
        print(f"  ✓ ③ 已连接（替身 ONLINE · 2 人）→ 按钮 {text!r}（红棕 Danger，与连接态不同色）")
        return True
    finally:
        gui._room = None      # 替身不是真 client：别让任何收尾路径去 stop 它
        try:
            gui._root.destroy()
        except Exception:
            pass


def test_room_generate_button() -> bool:
    """★ 设置「房间」页的「随机生成」按钮：在位、能生成合法且不重复的房间码、并落盘。

    防四类回归：
      ① 按钮根本没被 grid 进布局（用户「看不到那个按钮」——Tk 空间不足会裁最后打包的控件）；
      ② 点了不换码，或换出来的码不合法（对方照着念也进不了同一个房间）；
      ③ 连点得到的是同一个码（等价于按钮没接上生成逻辑）；
      ④ 生成的码没写回 config.yaml（重连/重启后丢失）。
    """
    import tempfile

    import vlt.gui as gui_mod
    from vlt.gui import TranslationGUI
    from vlt.room.protocol import ROOM_CODE_LEN, is_valid_room_code

    t = _i18n.t
    gui = TranslationGUI()
    real_default = gui_mod.DEFAULT_CONFIG
    tmp = Path(tempfile.mkdtemp()) / "config.yaml"
    try:
        # 落盘目标先挪到临时文件：②③④ 都会经 _on_room_generate → _save_room_cfg，
        # 不先把 DEFAULT_CONFIG 指走就会写真用户的 config.yaml。
        tmp.write_text("session:\n  api_key: ''\n", encoding="utf-8")
        gui_mod.DEFAULT_CONFIG = tmp

        # ① 按钮在「房间」页里、文案对、且确实被 grid 进了布局
        btn = getattr(gui, "_room_gen_btn", None)
        assert btn is not None, "没有「随机生成」按钮（_room_gen_btn 不存在）"
        got_text = str(btn.cget("text"))
        assert got_text == t("随机生成"), f"按钮文案应是 {t('随机生成')!r}，实际 {got_text!r}"
        assert btn.winfo_manager() == "grid", \
            f"「随机生成」按钮没被 grid 进布局（manager={btn.winfo_manager()!r}）"
        print(f"  ✓ ① 按钮在位、文案 {got_text!r}、已 grid 进布局")

        # ② 空房间码 → 点一下生成一个 8 位合法码
        gui._room_code_var.set("")
        gui._on_room_generate()
        code = gui._room_code_var.get()
        assert len(code) == ROOM_CODE_LEN, f"房间码应是 {ROOM_CODE_LEN} 位，实际 {code!r}"
        assert is_valid_room_code(code), f"生成的房间码不合法：{code!r}"
        print(f"  ✓ ② 生成合法 {ROOM_CODE_LEN} 位房间码 {code!r}")

        # ③ 连点 5 次，收集到的码两两不同（32^8≈1.1e12，重复概率可忽略；防「按钮没换码」）
        codes: list[str] = []
        for _ in range(5):
            gui._on_room_generate()
            c = gui._room_code_var.get()
            assert is_valid_room_code(c), f"连点生成的房间码不合法：{c!r}"
            codes.append(c)
        assert len(set(codes)) == len(codes), f"连点 5 次出现了重复码：{codes}"
        print(f"  ✓ ③ 连点 5 次得到 5 个互不相同的码：{codes}")

        # ④ 落盘：最后一次生成的码应写进了 config.yaml（room 段被补建）
        last = codes[-1]
        text = tmp.read_text(encoding="utf-8")
        assert "room:" in text, "config.yaml 里没有 room 段（没被补建）"
        assert last in text, f"最后生成的房间码 {last!r} 没写进 config.yaml"
        print(f"  ✓ ④ 房间码 {last!r} 已落盘到 config.yaml")
        return True
    finally:
        gui_mod.DEFAULT_CONFIG = real_default
        try:
            gui._root.destroy()
        except Exception:
            pass


def test_entry_right_click_menu() -> bool:
    """★ 文本输入框的右键菜单（剪切/复制/粘贴/全选）。

    背景：Tk 的 `ttk.Entry` / `tk.Text` 在 Windows 上**天生没有右键菜单** —— Ctrl+C/V 能用
    只是因为 Tk 绑了虚拟事件，鼠标用户根本没有入口（用户实测报「右键没有复制粘贴，
    只能键盘 C+V」）。这里钉住三件事：每个文本框都挂了菜单、菜单四个条目的文案对、
    **粘贴/全选/复制真的能改到内容**（只验「绑上了」等于没验）。
    """
    from vlt.gui import TranslationGUI

    t = _i18n.t
    gui = TranslationGUI()
    try:
        widgets = {
            "API key 输入框": gui._key_entry,
            "房间码输入框": gui._room_code_entry,
            "昵称输入框": gui._room_nick_entry,
            "词库文本框": gui._glossary_text,
            "打字输入框": gui._text_entry,
        }
        expected = [t("剪切"), t("复制"), t("粘贴"), t("全选")]

        # ① 每个控件都挂了 <Button-3>，且菜单在、四个条目文案对
        for name, w in widgets.items():
            assert w.bind("<Button-3>"), f"{name} 没有右键绑定"
            menu = getattr(w, "_edit_menu", None)
            assert menu is not None, f"{name} 没有 _edit_menu（Tk 会把它回收掉 → 菜单点不动）"
            labels = [menu.entrycget(i, "label") for i in range(4)]
            assert labels == expected, f"{name} 菜单条目不对：{labels!r}（期望 {expected!r}）"
        print(f"  ✓ ① 五个文本框都有右键菜单，条目 = {expected}")

        # ② 功能真验：真实剪贴板 → 粘贴，改的是房间码本身
        root = gui._root
        root.clipboard_clear()
        root.clipboard_append("AB12CD34")
        root.update()                    # 剪贴板在 Tk 里要过一轮事件循环才生效
        gui._room_code_var.set("")
        code_menu = gui._room_code_entry._edit_menu
        code_menu.invoke(2)              # 0 剪切 / 1 复制 / 2 粘贴 / 3 全选
        assert gui._room_code_var.get() == "AB12CD34", \
            f"右键「粘贴」没把内容写进房间码：{gui._room_code_var.get()!r}"
        print(f"  ✓ ② 右键「粘贴」生效 → 房间码 {gui._room_code_var.get()!r}")

        # ③ 全选 + 复制 + 粘贴 一圈（证明另外两个条目也真通，而不只是绑定在）
        code_menu.invoke(3)              # 全选
        code_menu.invoke(1)              # 复制
        gui._room_code_var.set("")
        gui._room_code_entry.update()
        code_menu.invoke(2)              # 粘贴
        assert gui._room_code_var.get() == "AB12CD34", \
            f"全选→复制→粘贴 之后内容不对：{gui._room_code_var.get()!r}"
        print(f"  ✓ ③ 全选→复制→粘贴 一圈通过 → {gui._room_code_var.get()!r}")

        # ④ add=\"+\" 回归：右键绑定不得顶掉「打字:」框原有的 <Return>/<Escape>
        assert gui._text_entry.bind("<Return>"), "右键绑定把 <Return> 顶掉了"
        assert gui._text_entry.bind("<Escape>"), "右键绑定把 <Escape> 顶掉了"
        print("  ✓ ④ 右键绑定没有覆盖原有的 <Return>/<Escape>")
        return True
    finally:
        try:
            gui._root.destroy()
        except Exception:
            pass


def test_chatbox_text_toggle_button() -> bool:
    """★ 第二行 chatbox 右边那颗「气泡: 译文 / 原文」按钮：文案随模式、未勾 chatbox 时置灰、
    点一下**改内存 cfg（引擎每条现读 → 同一次会话内立即生效）**并落盘。

    用**临时 config.yaml**（绝不碰用户真实配置），并把 `vlt.config/gui.DEFAULT_CONFIG`
    一起指过去 —— `_save_ui_state` 走的是 `vlt.gui.DEFAULT_CONFIG` 这个名字。
    """
    import tempfile

    import yaml

    import vlt.config as cfg_mod
    import vlt.gui as gui_mod

    tmp = Path(tempfile.mkdtemp(prefix="vlt-chatbox-text-"))
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text("ui:\n  chatbox: true\n  chatbox_text: translated\n", encoding="utf-8")
    saved_paths = (cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG)
    cfg_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui = None
    try:
        from vlt.engine import chatbox_text_mode
        from vlt.gui import TranslationGUI

        gui = TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        btn = gui._chatbox_text_btn
        assert btn.winfo_manager() == "pack", "切换按钮没被 pack 进输出行"
        assert gui._chatbox_var.get() is True, "前提：chatbox 勾着"

        # ① 默认态：显示当前模式（译文）+ 可点
        assert btn.cget("text") == "🌐 气泡: 译文", f"默认文案不对：{btn.cget('text')!r}"
        assert str(btn.cget("state")) == "normal", f"勾着 chatbox 时按钮不可点：{btn.cget('state')!r}"

        # ② 点一下 → 切到原文：内存 cfg 立刻变（引擎每条现读，无需重启）+ 落盘 + 状态栏回执
        gui._on_chatbox_text_toggle()
        assert btn.cget("text") == "📝 气泡: 原文", f"切换后文案不对：{btn.cget('text')!r}"
        assert gui._cfg.ui.get("chatbox_text") == "source", \
            f"内存 cfg 没跟着变（引擎读的还是旧的）：{gui._cfg.ui!r}"
        assert chatbox_text_mode(gui._cfg) == "source", "同一份 cfg 给引擎，模式必须已是 source"
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["ui"]["chatbox_text"] == "source", \
            "没写回 config.yaml"
        assert "气泡改为显示原文" in str(gui._status_label.cget("text")), \
            f"状态栏没回执：{gui._status_label.cget('text')!r}"

        # ③ 再点一下 → 切回译文
        gui._on_chatbox_text_toggle()
        assert gui._cfg.ui.get("chatbox_text") == "translated"
        assert btn.cget("text") == "🌐 气泡: 译文"
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["ui"]["chatbox_text"] == "translated"

        # ④ 取消勾选 chatbox → 置灰（改了也不生效，放着能点等于骗人）；勾回来立刻可点
        gui._chatbox_var.set(False)
        gui._on_chatbox_toggle()
        assert str(btn.cget("state")) == "disabled", \
            f"没勾 chatbox 时按钮应置灰，实际 {btn.cget('state')!r}"
        gui._chatbox_var.set(True)
        gui._on_chatbox_toggle()
        assert str(btn.cget("state")) == "normal", f"勾回 chatbox 后按钮应可点：{btn.cget('state')!r}"

        print("  ✓ 气泡文本切换按钮：文案随模式 · 点击改内存 cfg（同会话即时生效）+ 落盘 + 状态栏回执 · "
              "未勾 chatbox 时置灰（全程用临时 config.yaml）")
        return True
    finally:
        cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG = saved_paths
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
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

    print("界面回归测试（流式气泡 + 房间行）：")
    all_ok = True
    try:
        all_ok &= test_growing_bubble_shifts_later_bubbles()
    except AssertionError as exc:
        print(f"  ❌ 气泡长高时下移失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_peer_bubble_shows_nickname_and_source()
    except AssertionError as exc:
        print(f"  ❌ 分玩家气泡昵称/原文/译文检查失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_room_row_present_and_not_clipped()
    except AssertionError as exc:
        print(f"  ❌ 房间行检查失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_room_first_toggle_after_section_created()
    except AssertionError as exc:
        print(f"  ❌ 首次点「连接房间」失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_room_button_text_and_empty_code_guard()
    except AssertionError as exc:
        print(f"  ❌ 房间按钮文案/空房间码守卫失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_room_generate_button()
    except AssertionError as exc:
        print(f"  ❌ 「随机生成」房间码按钮失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_entry_right_click_menu()
    except AssertionError as exc:
        print(f"  ❌ 输入框右键菜单失败：{exc}")
        all_ok = False
    try:
        all_ok &= test_chatbox_text_toggle_button()
    except AssertionError as exc:
        print(f"  ❌ 气泡文本切换按钮失败：{exc}")
        all_ok = False
    for i, (events, expect) in enumerate(cases, 1):
        print(f"用例 {i}:")
        all_ok &= check(events, expect)

    print()
    if all_ok:
        print("✅ 全部通过（终版不重复插气泡 · 房间行按钮化）")
        print("ALL PASSED")
        return 0
    print("❌ 有失败用例")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
