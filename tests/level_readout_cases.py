"""level gui regression cases; shared offline fixtures."""
from __future__ import annotations

from level_gui_fixtures import (
    _CFG_WITH_LEVEL,
    _FakeEngine,
    _FakeLevelMatcher,
    _FakeProxy,
    _gui,
    _pump,
    _quiet,
)

def test_readout_tick_only_while_settings_visible() -> None:
    """★ 500ms 读数 tick：窗口可见才跑（且自己续期），关窗就地停、不许自己爬起来。"""
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        status = {"mode": "fixed", "mic_db": -24.0, "tts_db": -18.0,
                  "gain_db": -6.0, "headroom_db": 1.2, "fallback": False}
        gui._engines = [_FakeEngine(level_match=_FakeLevelMatcher(status=status))]  # noqa: SLF001
        gui._proxy = _FakeProxy(mic_db=-22.5)                       # noqa: SLF001
        names = gui._level_mode_names()                             # noqa: SLF001
        gui._level_mode_var.set(names["fixed"])                     # noqa: SLF001

        # ① 设置窗还关着 → 不排 tick（窗口关着不许每 500ms 去读引擎/代理）
        gui._refresh_level_readout()                                # noqa: SLF001
        assert gui._level_readout_job is None, "窗口不可见时不该排 tick"   # noqa: SLF001

        # ② 打开设置窗 → 排上 tick，读数出数（读的是**已在跑**的东西，绝不新开采集）
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._settings_win_viewable(), "前提：设置窗此刻应可见"   # noqa: SLF001
        gui._refresh_level_readout()                                # noqa: SLF001
        assert gui._level_readout_job is not None, "窗口可见时必须自己续期"  # noqa: SLF001
        line = str(gui._level_readout_lbl.cget("text"))              # noqa: SLF001
        for want in ("-24.0", "-18.0", "-6.0"):
            assert want in line, f"读数行缺 {want}：{line!r}"
        assert gui._level_gain_lbl.cget("text") == "-6.0 dB"        # noqa: SLF001

        # ③ 真等一跳（>500ms）且**不手动调**：读数自己刷新 → 证明「自己再排一次」有效
        status["gain_db"] = -9.5
        with _quiet():
            _pump(gui, 0.9)
        assert gui._level_gain_lbl.cget("text") == "-9.5 dB", \
            f"tick 没有自己续期：{gui._level_gain_lbl.cget('text')!r}"  # noqa: SLF001

        # ④ 引擎没给麦克风电平 → 回落读代理的 mic_reference_db（读数不许新开设备）
        status["mic_db"] = None
        gui._refresh_level_readout()                                # noqa: SLF001
        assert "-22.5" in str(gui._level_readout_lbl.cget("text")), \
            f"该回落读代理的参考电平：{gui._level_readout_lbl.cget('text')!r}"  # noqa: SLF001

        # ⑤ 三个来源全没有 → 整行「—」（绝不显示半真半假的数）
        gui._engines = []                                           # noqa: SLF001
        gui._proxy = None                                           # noqa: SLF001
        gui._refresh_level_readout()                                # noqa: SLF001
        assert gui._level_readout_lbl.cget("text") == "—"           # noqa: SLF001
        assert gui._level_gain_lbl.cget("text") == "—"              # noqa: SLF001

        # ⑥ 关窗 → 取消挂起的 tick 且不再续期
        with _quiet():
            gui._close_settings()                                   # noqa: SLF001
        assert gui._level_readout_job is None, "关窗后必须取消 tick"   # noqa: SLF001
        with _quiet():
            _pump(gui, 0.3)
        assert gui._level_readout_job is None, "关窗后不许自己爬起来"   # noqa: SLF001
    print("  ✓ 读数 tick：窗口关着不排；打开→排上并出读数（-24.0/-18.0/-6.0）；"
          "等 0.9s 自己刷到 -9.5 dB；引擎无麦克风电平→回落代理 -22.5；三源全无→整行「—」；"
          "关窗→取消且不再爬起来")

def test_readout_tick_cancelled_on_window_destroy() -> None:
    """★ 设置窗 / 根窗被**销毁**（而不是走 `close_settings`）时，挂起的读数 tick 必须一起取消。

    末影猫的 L5：`open_settings` 无条件排 500ms tick，收尾若不走 `close_settings`，那个
    `after` 就带着一个已经死掉的命令名到点 —— Tk 收尾时打一行
    `invalid command name "..._refresh_level_readout"`。

    判据用**状态**（`_level_readout_job` 还挂不挂着）而不是去抓那行 stderr：本机（Windows）
    三种收尾方式都没复现出那行（见 PR 说明），所以能被稳定验证的是「挂起的 tick 有没有被
    取消」—— 取消了，那条噪音在原理上就不可能再出现。
    """
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        assert "<Destroy>" in gui._settings_win.bind(), \
            "build_settings_dialog 必须把 <Destroy> 绑到取消读数 tick 上"  # noqa: SLF001
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._level_readout_job is not None, "前提：打开设置窗后应挂起读数 tick"  # noqa: SLF001

        gui._settings_win.destroy()                                 # 不给 close_settings 机会
        assert gui._level_readout_job is None, \
            "设置窗被销毁后必须取消挂起的 tick（否则到点就是 invalid command name）"
        with _quiet():
            _pump(gui, 0.3)
        assert gui._level_readout_job is None, "销毁后不许自己爬起来"   # noqa: SLF001
        with _quiet():
            gui._refresh_level_readout()                            # noqa: SLF001
        assert gui._level_readout_job is None, "窗口已销毁时也不许再排 tick"   # noqa: SLF001

    # 反向：正常收尾（close_settings 后再销毁根窗）仍只留一行「取消」语义，且幂等
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._level_readout_job is not None                    # noqa: SLF001
        gui._cancel_level_tick()                                     # noqa: SLF001
        gui._cancel_level_tick()                                     # 幂等：第二次不许抛
        assert gui._level_readout_job is None                        # noqa: SLF001
        with _quiet():
            gui._close_settings()                                   # noqa: SLF001
    print("  ✓ 读数 tick：设置窗被销毁（不走 close_settings）→ 挂起的 tick 被取消且不再续期；"
          "取消本身幂等")

def test_child_widget_destroy_does_not_cancel_readout_tick() -> None:
    """★ 设置窗的**子控件**被销毁，不许取消读数 tick（issue #89）。

    Tk 的默认 bindtags 是 `(widget, class, toplevel, "all")`，所以绑在设置窗 Toplevel 上的
    `<Destroy>` **也会被子控件销毁触发**（实测 `child.destroy()` → 回调收到
    `e.widget == '.!toplevel.!frame'`）。不加 `e.widget is win` 过滤时：一旦以后有人动态
    重建设置页，窗口**仍可见**的读数 tick 就会被悄悄取消 —— 读数从此不再刷新、且没有任何提示。

    判据用**状态**（`_level_readout_job` 还挂不挂着），不是去抓某行 stderr：过滤正确时
    子控件销毁前后 job 都在；把过滤去掉（不再按 `e.widget` 判），这条立刻变红。
    """
    import tkinter as tk
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._level_readout_job is not None, "前提：打开设置窗后应挂起读数 tick"  # noqa: SLF001

        # 造一个子控件再销毁它（模拟动态重建设置页 / 某控件内部销毁子件）
        child = tk.Frame(gui._settings_win)                         # noqa: SLF001
        child.pack()
        gui._root.update()                                          # noqa: SLF001
        child.destroy()
        gui._root.update()                                          # noqa: SLF001
        assert gui._level_readout_job is not None, \
            "销毁设置窗的**子控件**不该取消读数 tick（<Destroy> 须按 e.widget is win 过滤）"  # noqa: SLF001
        with _quiet():
            _pump(gui, 0.3)
        assert gui._level_readout_job is not None, "子控件销毁后读数 tick 仍应活着"   # noqa: SLF001

        with _quiet():
            gui._close_settings()
    print("  ✓ 读数 tick：销毁设置窗的**子控件**（非窗口本身）→ 挂起的 tick 不被取消"
          "（<Destroy> 已按 e.widget is win 过滤）")

def test_readout_tick_cancelled_when_root_destroyed() -> None:
    """★ 关程序那条路径：销毁**根窗**时，设置窗上的 `<Destroy>` 仍必须取消挂起的读数 tick。

    这是那个绑定的**原始目的**（末影猫 L5：收尾时那个 `after` 带着已经死掉的命令名到点，
    Tk 打一行 `invalid command name "..._refresh_level_readout"`），但 #91 的两条用例
    只覆盖了「销毁设置窗本身」与「销毁子控件」——「关程序 → 根窗连带销毁设置窗」这条
    **没人守**。#91 给绑定加了 `e.widget is win` 过滤：这条路径上 `e.widget` 必须仍是
    设置窗自己，否则过滤会把收尾保障一起丢掉，而现象上完全看不出来（本机三种收尾
    都不打那行噪音，见上一个用例的注脚）。

    判据只能是**状态**（`_level_readout_job`）：根窗销毁后 tick 必须被取消、且不许再爬起来。
    去掉 `build_settings_dialog` 里那条 `<Destroy>` 绑定、或把过滤写成永不命中，这条都变红。
    """
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._settings_win_viewable(), "前提：设置窗此刻应可见"   # noqa: SLF001
        assert gui._level_readout_job is not None, "前提：打开设置窗后应挂起读数 tick"  # noqa: SLF001

        gui._root.destroy()          # 关程序：根窗销毁连带销毁设置窗（**不走** close_settings）
        assert gui._level_readout_job is None, \
            "销毁根窗时设置窗仍须取消挂起的读数 tick（<Destroy> 的 e.widget 过滤不许把它挡掉）"  # noqa: SLF001

        gui._refresh_level_readout()                            # 根窗已死 → 不许再排 tick  # noqa: SLF001
        assert gui._level_readout_job is None, "根窗已销毁时不许再排 tick"   # noqa: SLF001
    print("  ✓ 读数 tick：销毁**根窗**（关程序路径，不走 close_settings）→ 设置窗上的 "
          "<Destroy> 仍取消挂起的 tick，且不再续期")
