"""「译音音量」界面层的离线验收：脏值不炸、三处同步、热更新留痕、读数 tick 的开关。

## 治什么

评审（PR #87）指出界面这条腿上有几处**只有真点过界面才看得见**的坑，纯逻辑测试
（`tests/test_level_match.py`）覆盖不到：

1. `_level_read_db` 漏接 `tk.TclError` —— 用户在 Spinbox 里手打 ``abc``、或清空后失焦，
   `DoubleVar.get()` 抛的是 TclError 而不是 ValueError。异常逃出 Tk 回调 → crashlog 记一条
   「Tk 回调异常」、设置**静默不保存**、控件还留着脏值（与「看到的==生效的」正好相反）。
2. `_apply_level_config` 里那条 `hasattr(p, "set_level_config")` 恒真（`LinuxMicProxy`
   继承基类），else 分支永远走不到，注释还是错的（写着「Linux 本期没接」）。
3. 改一次参数要同时落到**三处**：磁盘 config.yaml、内存真源 `self._cfg`（设置窗可反复重建，
   只落盘不同步就会关窗再开看见旧值）、正在跑的代理/引擎（热更新）。漏一处都是「我改了没生效」。
4. 500ms 读数 tick 必须**只在设置窗可见时跑**，关窗就地停下且不许自己爬起来。

## 离线保证

配置用**临时目录里的临时 config.yaml**（绝不碰仓库/用户真配置）；界面语言钉死 zh；
独立电平探针打桩成假类（`_NoProbe`），**绝不开真 loopback 设备**；麦克风代理因
`_is_test_process()` 守卫本来就不会被构造。跑法：

    .venv/Scripts/python.exe tests/test_level_gui.py
"""

from level_controls_cases import (
    test_level_read_db_survives_dirty_spinbox,
    test_on_level_change_persists_syncs_and_hot_updates,
    test_level_write_preserves_comments_and_siblings,
    test_apply_level_config_leaves_trace_and_never_raises,
    test_sync_level_controls_state_follows_mode,
)

from level_readout_cases import (
    test_readout_tick_only_while_settings_visible,
    test_readout_tick_cancelled_on_window_destroy,
    test_child_widget_destroy_does_not_cancel_readout_tick,
    test_readout_tick_cancelled_when_root_destroyed,
)

def main() -> int:
    tests = [
        test_level_read_db_survives_dirty_spinbox,
        test_on_level_change_persists_syncs_and_hot_updates,
        test_level_write_preserves_comments_and_siblings,
        test_apply_level_config_leaves_trace_and_never_raises,
        test_sync_level_controls_state_follows_mode,
        test_readout_tick_only_while_settings_visible,
        test_readout_tick_cancelled_on_window_destroy,
        test_child_widget_destroy_does_not_cancel_readout_tick,
        test_readout_tick_cancelled_when_root_destroyed,
    ]
    print("test_level_gui:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
