"""设置页滚轮守卫：滚轮滑过下拉框 / 数字框只滚页面，绝不改设置值。

## 真实事故（2026-10）

设置页是**整页可滚**的，而 Tk 给 `TCombobox` / `TSpinbox` 自带滚轮绑定
（`ttk::combobox::Scroll` / `ttk::spinbox::MouseWheel`）：指针滑过就改值，并且
直接触发 `<<ComboboxSelected>>` / `-command` —— 也就是**当场写 config.yaml**。

用户只是滚了一页：
- `players.distance_mode` 从 `order` 变成 `off`（名字后面的距离没了）；
- `players.max_sessions` 从 4 变成 5；
- 界面上没有任何提示，日志只有「设置已保存」。

本文件钉住：控件级守卫接住滚轮后，值不变、配置文件一个字节都不变。

跑法：xvfb-run -a .venv/bin/python tests/test_settings_wheel.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

# 沙箱 config：用例绝不碰开发者真实配置（其中可能有 base_url / ui.lang）
from _cfgbox import sandbox_config  # noqa: E402

CONFIG = sandbox_config(reset=True)

# 构造 TranslationGUI 之前钉死界面语言（CI 是英文系统，不钉会按 en 取词条）
import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

# 电平探针会真开音频设备 —— 离线测试硬规则：绝不碰真声卡
import vlt.level_probe as _level_probe  # noqa: E402

_level_probe.open_level_source = (  # type: ignore[assignment]
    lambda device_name=None: (_ for _ in ()).throw(
        RuntimeError("测试进程：不打开真实电平采集")))


def _wheel(widget, seq: str = "<Button-5>") -> None:
    """同步派发一次滚轮事件（`when="now"`：不传的话 Tk 会排队，断言就跑在派发前）。"""
    widget.event_generate(seq, x=6, y=6, when="now")


def _close(gui) -> None:  # noqa: ANN001
    """收尾：先取消挂起的 after 任务，再销毁 root（否则 Tk 会打 invalid command name）。"""
    from vlt import gui_settings

    try:
        gui_settings.cancel_players_status(gui)
    except Exception:
        pass
    try:
        gui._cancel_level_tick()
    except Exception:
        pass
    try:
        gui._root.destroy()
    except Exception:
        pass


def test_settings_combo_wheel_does_not_change_value() -> bool:
    """语言下拉：滚轮不许改值，更不许落盘（以前会直接写 ui.lang）。"""
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    try:
        gui._open_settings(page="general")
        gui._root.update()
        combo = gui._ui_lang_combo
        before_value = combo.get()
        before_cfg = CONFIG.read_bytes()

        _wheel(combo)
        gui._root.update()

        assert combo.get() == before_value, (
            f"滚轮改了下拉框的值：{before_value!r} → {combo.get()!r}")
        assert CONFIG.read_bytes() == before_cfg, "滚轮滑过下拉框竟然写了配置文件"
        print(f"  ✓ 下拉框滚轮不改值/不落盘（保持 {before_value!r}）")
        return True
    finally:
        _close(gui)


def test_settings_spinbox_wheel_does_not_change_value() -> bool:
    """数字框（译音缓冲）：滚轮不许改值，也不许触发 command 落盘。"""
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    try:
        gui._open_settings(page="audio")
        gui._root.update()
        spin = gui._translated_spin
        var = gui._translated_buf_var
        before_value = var.get()
        before_cfg = CONFIG.read_bytes()

        _wheel(spin)
        gui._root.update()

        assert var.get() == before_value, (
            f"滚轮改了数字框的值：{before_value!r} → {var.get()!r}")
        assert CONFIG.read_bytes() == before_cfg, "滚轮滑过数字框竟然写了配置文件"
        print(f"  ✓ 数字框滚轮不改值/不落盘（保持 {before_value!r}）")
        return True
    finally:
        _close(gui)


def main() -> int:
    tests = [
        test_settings_combo_wheel_does_not_change_value,
        test_settings_spinbox_wheel_does_not_change_value,
    ]
    print("test_settings_wheel:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
