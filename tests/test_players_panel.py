"""界面回归测试：右栏「房间玩家」增量渲染 + 勾选语义。

## 背景（真实踩过的坑）

右栏最初每 tick 都按「签名」决定要不要重画，而签名里带**实时距离**——距离每秒都在变，
于是整栏控件被 `destroy()` 再重建；重排又用「全部 `pack_forget()` → 逐个 `pack()`」。
两个动作都会让控件 unmap 再 map，界面上就是**一直在闪**（用户实测报的）。改成
「控件按 `_row_key` 复用、只 configure 文本、换位只 `pack(before=…)`」之后才不闪，
但那之后没有测试兜着：谁改回「重建」都不会被发现，只会在实机上重新开始闪。

这里钉住四条（全部是**可观察的控件身份**，不看像素）：

1. 玩家没增删时，行控件对象必须是**同一批**（`id()` 不变）；
2. 距离/状态变化只改文本（`cget("text")` 变了，控件没换）；
3. 距离排序换位只重排，控件既不重建也不从 pack 表里消失；
4. 有人离开时只销毁他自己那一行，别人的行不动。

## 勾选语义

`select_mode=all` 时名单**不参与**判断。此时「全选」若照常写名单，配置会自相矛盾
（`all` + 非空名单 → 每次解析都警告「名单不生效」），所以必须是 no-op + 状态栏提示；
而「勾某个人」则相反，要顺手切到 `only` 并**明说**其他人会立刻停翻（不能静默改行为）。

`_save()` 会真的写 `config.yaml`（`gui_settings.save_players_setting`），所以交互用例
一律把 `gui_players_panel._save` 打桩——跑测试不能改用户的配置。

需要 Tk（Windows 上标准库自带）。根窗口全程 `withdraw()`：测试不该在用户屏幕上弹窗，
也因此这里**不能**用 `winfo_ismapped()` 判「控件还在」，改用 `pack_slaves()` 身份与次序。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 界面文案跟随系统语言，钉死成中文保证可复现（CI 是英文系统，不钉会红）。
# 必须在建栏之前打上 —— 控件文案是建的时候就按当时语言取的词。
import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

# 电平探针的「打开设备」这一环打桩：本用例不建设置窗，但 `vlt.gui` 一 import 就会
# 挂探针钩子，离线测试的硬规则是绝不碰真声卡（用户的 VRChat 播放流正在上面）。
import vlt.level_probe as _level_probe  # noqa: E402


def _no_audio_probe(device_name: str | None = None) -> None:  # noqa: ANN001
    raise RuntimeError("测试进程：不打开真实电平采集（用例只验界面逻辑）")


_level_probe.open_level_source = _no_audio_probe


# ── 假 gui ──
class _FakeWarmup:
    """只实现面板真正用到的那一个方法。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def status(self) -> dict:
        return {"player_rows": [dict(r) for r in self.rows]}


class _FakeGUI:
    """面板对 gui 的全部依赖都是**属性**，所以 duck typing 比真 GUI 更可控。"""

    def __init__(self, rows: list[dict], players: dict | None = None) -> None:
        import tkinter as tk

        self._root = tk.Tk()
        self._root.withdraw()
        self._dpi_scale = 1.0
        self._cfg = type("Cfg", (), {"players": players or {"mode": "decrypt"}})()
        self._warmup = _FakeWarmup(rows)
        self._engine_ctx = type("Ctx", (), {"players_warmup": self._warmup,
                                           "players_leg": None})()
        self.statuses: list[tuple[str, str]] = []
        self.set_status = lambda level, text: self.statuses.append((level, str(text)))

    def _set_status(self, level: str, text: str) -> None:  # 面板按这个方法名调用
        self.statuses.append((level, str(text)))

    def rows(self, rows: list[dict]) -> None:  # 换一批玩家行
        self._warmup.rows = rows

    def players(self, **changes) -> None:  # 改配置（模拟用户在设置页改过）
        self._cfg.players.update(changes)

    def close(self) -> None:
        try:
            self._root.destroy()
        except Exception:                        # noqa: BLE001
            pass


def _row(actor: int, user_id: str, name: str, distance: float | None, **extra) -> dict:
    row = {"actor": actor, "user_id": user_id, "name": name, "distance": distance,
           "self": False, "speaking": False, "translating": False}
    row.update(extra)
    return row


def _panel(rows: list[dict], players: dict | None = None):
    """建栏并返回 (gui, panel 模块)。"""
    import vlt.gui_players_panel as panel

    gui = _FakeGUI(rows, players)
    panel.build_panel(gui)
    return gui, panel


def _ids(gui) -> dict[str, int]:
    return {key: id(entry["frame"]) for key, entry in gui._ppl_rows.items()}


def _order(gui) -> list[str]:
    """pack 表里的控件次序 → 反查成行 key（证明重排真的发生在已有控件上）。"""
    by_frame = {id(entry["frame"]): key for key, entry in gui._ppl_rows.items()}
    return [by_frame.get(id(widget), "?") for widget in gui._ppl_inner.pack_slaves()]


# ── 用例 ──
def test_reuse_controls_and_update_text() -> bool:
    """★ 核心：没增删时控件不重建，距离变了只改文本、只重排。"""
    rows = [_row(1, "usr_a", "阿诺", 3.4), _row(2, "usr_b", "小明", 1.2),
            _row(3, "", "还没 id", 8.0)]
    gui, panel = _panel(rows)
    try:
        panel._sync(gui)
        before = _ids(gui)
        assert len(before) == 4, f"应有 1 行自己 + 3 行别人，实际 {before}"
        assert gui._ppl_title.cget("text") == "房间玩家 (3)", gui._ppl_title.cget("text")
        assert _order(gui) == ["\x00self", "usr_b#2", "usr_a#1", "\x00actor3"], _order(gui)
        dist_before = gui._ppl_rows["usr_a#1"]["dist"].cget("text")

        # 距离互换（排序会变）+ 一个人改名字，没有增删
        gui.rows([_row(1, "usr_a", "阿诺", 0.9), _row(2, "usr_b", "小明", 9.0),
                  _row(3, "", "还没 id", 8.0)])
        panel._sync(gui)

        after = _ids(gui)
        assert after == before, f"控件被重建了：{before} → {after}"
        assert _order(gui) == ["\x00self", "usr_a#1", "\x00actor3", "usr_b#2"], _order(gui)
        dist_after = gui._ppl_rows["usr_a#1"]["dist"].cget("text")
        assert dist_after != dist_before, f"距离文本没更新：{dist_before!r}"
        print(f"  ✓ 3 名玩家换位只重排：控件 {len(after)} 个全部复用，"
              f"距离 {dist_before!r} → {dist_after!r}")
        return True
    finally:
        gui.close()


def test_leave_destroys_only_own_row() -> bool:
    """有人离开：只销毁他自己那行，别人的控件对象不动。"""
    rows = [_row(1, "usr_a", "阿诺", 3.4), _row(2, "usr_b", "小明", 1.2)]
    gui, panel = _panel(rows)
    try:
        panel._sync(gui)
        keep = {k: id(v["frame"]) for k, v in gui._ppl_rows.items() if k != "usr_b#2"}
        gone_frame = gui._ppl_rows["usr_b#2"]["frame"]

        gui.rows([_row(1, "usr_a", "阿诺", 3.4)])
        panel._sync(gui)

        assert "usr_b#2" not in gui._ppl_rows, "离开的人还在缓存里"
        assert not gone_frame.winfo_exists(), "离开的人那行没被销毁"
        after = {k: id(v["frame"]) for k, v in gui._ppl_rows.items()}
        assert after == keep, f"别人的行被重建了：{keep} → {after}"
        assert gui._ppl_title.cget("text") == "房间玩家 (1)", gui._ppl_title.cget("text")
        print(f"  ✓ 离开者只销毁自己那行，其余 {len(after)} 行控件复用")
        return True
    finally:
        gui.close()


def test_self_and_idless_rows_have_no_checkbox() -> bool:
    """自己和「还没学到 user_id」的人不能勾：名单按 user_id 存，没 id 无从持久化。"""
    rows = [_row(1, "usr_a", "阿诺", 3.4), _row(3, "", "还没 id", 8.0)]
    gui, panel = _panel(rows)
    try:
        panel._sync(gui)
        assert gui._ppl_rows["\x00self"]["check"] is None, "自己那行出现了勾选框"
        assert gui._ppl_rows["\x00actor3"]["check"] is None, "没 id 的行出现了勾选框"
        assert gui._ppl_rows["usr_a#1"]["check"] is not None, "有 id 的行没有勾选框"
        print("  ✓ 自己 / 无 user_id 的行没有勾选框，有 id 的有")
        return True
    finally:
        gui.close()


def test_checkbox_mirrors_selection() -> bool:
    """名单里的 user_id 必须让勾选框与底色同步（勾选状态来自配置，不是控件自己记）。"""
    rows = [_row(1, "usr_a", "阿诺", 3.4), _row(2, "usr_b", "小明", 1.2)]
    gui, panel = _panel(rows, {"mode": "decrypt", "select_mode": "only",
                               "select_users": ["usr_a"]})
    try:
        panel._sync(gui)
        assert bool(gui._ppl_rows["usr_a#1"]["var"].get()) is True, "名单里的人没勾上"
        assert bool(gui._ppl_rows["usr_b#2"]["var"].get()) is False, "名单外的人被勾了"

        # 改名单后必须跟着变（同一批控件，只改状态）
        entry = gui._ppl_rows["usr_b#2"]
        gui.players(select_users=["usr_b"])
        panel._sync(gui)
        assert gui._ppl_rows["usr_b#2"] is entry, "改名单把控件重建了"
        assert bool(entry["var"].get()) is True, "新进名单的人没勾上"
        assert bool(gui._ppl_rows["usr_a#1"]["var"].get()) is False, "被移出名单还勾着"
        print("  ✓ 勾选状态跟随 select_users，且不重建控件")
        return True
    finally:
        gui.close()


def test_set_visible_follows_mode() -> bool:
    """`players.mode` 关了右栏要收起来，开了要回来（并且不重复 pack）。"""
    gui, panel = _panel([_row(1, "usr_a", "阿诺", 3.4)])
    try:
        panel._sync(gui)
        assert gui._ppl_packed is True, "decrypt 下右栏没显示"
        assert gui._players_panel in gui._root.pack_slaves(), "右栏没 pack 进窗口"

        gui.players(mode="off")
        panel._sync(gui)
        assert gui._ppl_packed is False, "off 下右栏没收起来"
        assert gui._players_panel not in gui._root.pack_slaves(), "off 下右栏还占着位置"

        gui.players(mode="decrypt")
        panel._sync(gui)
        assert gui._ppl_packed is True, "改回 decrypt 后右栏没回来"
        assert gui._root.pack_slaves().count(gui._players_panel) == 1, "右栏被重复 pack"
        print("  ✓ 右栏随 players.mode 显隐，且只 pack 一次")
        return True
    finally:
        gui.close()


def test_select_all_is_noop_under_all_mode() -> bool:
    """`all` 下「全选」不写配置（否则 all + 非空名单 = 自相矛盾），只提示。"""
    rows = [_row(1, "usr_a", "阿诺", 3.4)]
    gui, panel = _panel(rows, {"mode": "decrypt", "select_mode": "all"})
    saved: list[tuple] = []
    original = panel._save
    panel._save = lambda g, key, value, **kw: saved.append((key, value, kw))
    try:
        panel._sync(gui)
        panel._select_all(gui)
        assert not saved, f"all 下仍然写了配置：{saved}"
        assert gui.statuses and "勾选不生效" in gui.statuses[-1][1], gui.statuses

        gui.players(select_mode="only")
        panel._sync(gui)
        panel._select_all(gui)
        assert saved == [("select_users", ["usr_a"], {})], saved
        print("  ✓ all 下全选是 no-op + 提示；only 下才真写名单")
        return True
    finally:
        panel._save = original
        gui.close()


def test_toggle_under_all_switches_to_only() -> bool:
    """`all` 下勾一个人 = 「我要只翻这几个」：写名单 + 切 only，两次都带明确提示。"""
    rows = [_row(1, "usr_a", "阿诺", 3.4)]
    gui, panel = _panel(rows, {"mode": "decrypt", "select_mode": "all"})
    saved: list[tuple] = []
    original = panel._save

    def _fake_save(g, key, value, *, note: str = "") -> None:
        saved.append((key, value, note))
        g.players(**{key: value})           # 让后续 `_settings` 读到新值

    panel._save = _fake_save
    try:
        panel._sync(gui)
        var = gui._ppl_rows["usr_a#1"]["var"]
        var.set(True)
        panel._on_toggle(gui, "usr_a", var)
        assert [s[0] for s in saved] == ["select_users", "select_mode"], saved
        assert saved[0][1] == ["usr_a"], saved
        assert saved[1][1] == "only", saved
        assert all("立刻停止翻译" in s[2] for s in saved), saved
        print("  ✓ all 下勾人 → 写名单 + 切 only，两步都写明「立刻停止翻译」")
        return True
    finally:
        panel._save = original
        gui.close()


def test_row_key_separates_actors_of_same_account() -> bool:
    """漏收 Leave 时同账号会留两条（新旧 actor）：不能互相顶掉，要各占一行。"""
    rows = [_row(1, "usr_a", "阿诺", 3.4), _row(7, "usr_a", "阿诺", 3.4)]
    gui, panel = _panel(rows)
    try:
        panel._sync(gui)
        assert set(gui._ppl_rows) == {"\x00self", "usr_a#1", "usr_a#7"}, gui._ppl_rows
        assert gui._ppl_title.cget("text") == "房间玩家 (2)", gui._ppl_title.cget("text")
        print("  ✓ 同账号两个 actor 各占一行（key 带 actor）")
        return True
    finally:
        gui.close()


def test_no_position_rows_sort_last() -> bool:
    """没有距离的人垫底，且距离列不留「None」这种脏文本。"""
    rows = [_row(1, "usr_a", "阿诺", None), _row(2, "usr_b", "小明", 2.0)]
    gui, panel = _panel(rows)
    try:
        panel._sync(gui)
        assert _order(gui) == ["\x00self", "usr_b#2", "usr_a#1"], _order(gui)
        text = gui._ppl_rows["usr_a#1"]["dist"].cget("text")
        assert "None" not in text, f"距离列出现了 None：{text!r}"
        print(f"  ✓ 无位置的人垫底，距离列显示 {text!r}（不是 None）")
        return True
    finally:
        gui.close()


def main() -> int:
    tests = (
        test_reuse_controls_and_update_text,
        test_leave_destroys_only_own_row,
        test_self_and_idless_rows_have_no_checkbox,
        test_checkbox_mirrors_selection,
        test_set_visible_follows_mode,
        test_select_all_is_noop_under_all_mode,
        test_toggle_under_all_switches_to_only,
        test_row_key_separates_actors_of_same_account,
        test_no_position_rows_sort_last,
    )
    all_ok = True
    for fn in tests:
        try:
            all_ok &= bool(fn())
        except AssertionError as exc:
            print(f"  ❌ {fn.__name__}：{exc}")
            all_ok = False
        except Exception as exc:                 # noqa: BLE001
            print(f"  ❌ {fn.__name__} 抛异常：{type(exc).__name__}: {exc}")
            all_ok = False

    print()
    if all_ok:
        print("✅ 全部通过（右栏增量渲染 · 勾选语义）")
        print("ALL PASSED")
        return 0
    print("❌ 有失败用例")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
