#!/usr/bin/env python
"""分玩家设置：默认值 / 范围校验 / 设置页回填与保存。

## 这里钉住的事

1. `parse_players` 的默认值与 `config.example.yaml` 一致（4 人 / -55 / 滞回 6 …）；
2. 脏值**回落并留痕**（不静默变成别的行为），边界值夹紧；
3. `gate` 模式缺 `max_distance_m` 时自动降级（与运行期判定同口径）；
4. 设置页：有「分玩家」这一页、控件按配置回填、改一项就写回 config.yaml 并同步内存；
5. 模式改动会启停预热器（`ensure_players_warmup` 被调用）；默认关不建预热器。

跑法：xvfb-run -a .venv/bin/python tests/test_players_settings.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "playerssettings0123456789")

import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

from _cfgbox import sandbox_config  # noqa: E402

CONFIG = sandbox_config(reset=True)

from vlt.players_settings import (  # noqa: E402
    MODE_DECRYPT,
    PlayersSettings,
    parse_players,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def test_defaults() -> None:
    print("[players-settings] 默认值")
    ps = parse_players({})
    check("模式默认 off", ps.mode == "off", ps.mode)
    check("没有预热开关（分玩家必预热）", "prewarm" not in PlayersSettings.__dataclass_fields__,
          str(list(PlayersSettings.__dataclass_fields__)))
    check("默认 4 人", ps.max_sessions == 4, str(ps.max_sessions))
    check("默认门限 -55", ps.gate_db == -55.0, str(ps.gate_db))
    check("默认滞回 6", ps.gate_hysteresis_db == 6.0, str(ps.gate_hysteresis_db))
    check("默认距离 off", ps.distance_mode == "off", ps.distance_mode)
    check("默认等流量 120s", ps.pre_seconds == 120.0, str(ps.pre_seconds))
    check("非字典回落默认", parse_players("垃圾") == PlayersSettings())


def test_validation() -> None:
    print("[players-settings] 脏值 / 边界")
    logs: list[str] = []
    ps = parse_players({"max_sessions": 99, "gate_db": -100, "gate_hysteresis_db": 99,
                        "pre_seconds": 1, "prewarm_buffer_mb": 0,
                        "mode": "??", "prewarm": "yes", "distance_mode": "xx"},
                       log=logs.append)
    check("人数夹到 8", ps.max_sessions == 8, str(ps.max_sessions))
    check("门限夹到 -70", ps.gate_db == -70.0, str(ps.gate_db))
    check("滞回夹到 12", ps.gate_hysteresis_db == 12.0, str(ps.gate_hysteresis_db))
    check("等流量夹到 10", ps.pre_seconds == 10.0, str(ps.pre_seconds))
    check("缓冲夹到 4", ps.prewarm_buffer_mb == 4, str(ps.prewarm_buffer_mb))
    check("未知模式回落 off", ps.mode == "off", ps.mode)
    parse_players({"mode": "decrypt", "prewarm": "auto"}, log=logs.append)
    check("废弃的 prewarm 键被忽略且只提示一次",
          sum("prewarm 已废弃" in line for line in logs) == 1, str(logs))
    check("未知距离回落 off", ps.distance_mode == "off", ps.distance_mode)
    check("每处都留了痕", len(logs) >= 8, str(len(logs)))

    gate = parse_players({"distance_mode": "gate", "max_distance_m": 0})
    check("gate 缺距离 → 降级", gate.distance_mode == "off", gate.distance_mode)
    gate2 = parse_players({"distance_mode": "both", "max_distance_m": 8})
    check("both + 距离正常", gate2.distance_mode == "both" and gate2.max_distance_m == 8)

    ok = parse_players({"mode": "decrypt", "max_sessions": "6"})
    check("字符串数字也收", ok.max_sessions == 6, str(ok.max_sessions))
    check("模式合法", ok.mode == MODE_DECRYPT, ok.mode)


def test_page() -> None:
    print("[players-settings] 设置页回填 / 保存")
    import vlt.gui as G
    from vlt import gui_engine
    from vlt import gui_settings as gs

    # 写一份「已开分玩家」的配置，验证回填
    text = CONFIG.read_text(encoding="utf-8")
    text = gs._yaml_set_or_create(text, ["players", "mode"], "off")
    CONFIG.write_text(text, encoding="utf-8")

    gui = G.TranslationGUI()
    try:
        gui._open_settings()
        check("有「分玩家」页", "分玩家" in (gui._settings_tabs or {}),
              str(list((gui._settings_tabs or {}).keys())))
        check("模式下拉回填", gui._players_mode_var.get() in ("关闭", "Off"),
              gui._players_mode_var.get())
        check("人数回填为 4", gui._players_spin_vars["max_sessions"].get() == "4",
              gui._players_spin_vars["max_sessions"].get())
        check("门限回填 -55", gui._players_spin_vars["gate_db"].get() == "-55",
              gui._players_spin_vars["gate_db"].get())

        gs.save_players_setting(gui, "gate_db", -58.0)
        saved = CONFIG.read_text(encoding="utf-8")
        check("YAML 写回", "gate_db: -58" in saved,
              [ln for ln in saved.splitlines() if "gate_db" in ln][:3])
        check("内存同步", (gui._cfg.players or {}).get("gate_db") == -58.0,
              str(gui._cfg.players))

        # 保存会把新值同步给预热器。**这里只断言「被调用」**：真建预热器会起线程，
        # 而 Tk 变量 + 后台线程 + GC 在收尾时会把进程打崩（tests/test_osc_port_save.py
        # 的模块注释记过这个坑）。真正的预热器装配由下面 headless 用例覆盖。
        calls: list[int] = []
        real_ensure = gui_engine.ensure_players_warmup
        gui_engine.ensure_players_warmup = lambda ctx: calls.append(1)  # noqa: ARG005
        try:
            gs.save_players_setting(gui, "mode", "decrypt")
            check("内存里的模式已改", (gui._cfg.players or {}).get("mode") == "decrypt",
                  str(gui._cfg.players))
            check("保存后会同步给预热器", len(calls) >= 1, str(calls))
        finally:
            gui_engine.ensure_players_warmup = real_ensure
        check("设置页没有预热下拉", not hasattr(gui, "_players_prewarm_var"))
        check("预热状态文案有内容", bool(gs._players_status_line(gui)),
              gs._players_status_line(gui))
    finally:
        try:
            gui._close_settings()
        except Exception:  # noqa: BLE001
            pass
        try:
            gui._root.destroy()
        except Exception:  # noqa: BLE001
            pass
        import gc
        gc.collect()


def test_warmup_wiring() -> None:
    """`ensure_players_warmup`：按配置建/停预热器（不碰 Tk）。"""
    print("[players-settings] 预热器装配（headless）")
    from types import SimpleNamespace

    from vlt import gui_engine

    ctx = SimpleNamespace(
        cfg=SimpleNamespace(players={}),
        players_warmup=None,
        set_status_fn=None,
    )
    try:
        gui_engine.ensure_players_warmup(ctx)
        check("默认关：不建预热器", ctx.players_warmup is None)
        ctx.cfg.players = {"mode": "decrypt"}
        gui_engine.ensure_players_warmup(ctx)
        warmup = ctx.players_warmup
        check("开分玩家就建了预热器", warmup is not None)
        if warmup is not None:
            check("状态可读", "phase" in warmup.status(), str(warmup.status()))
        # 关掉模式 → 预热器必须被收掉（不能留着 socket 继续跑）
        ctx.cfg.players = {"mode": "off"}
        gui_engine.ensure_players_warmup(ctx)
        check("模式改成 off 后预热器被停", ctx.players_warmup is None)
    finally:
        gui_engine.stop_players_warmup(ctx)

    # 队列没接上时**绝不能**在后台线程调用状态栏回调（Tk 只能主线程碰）——
    # 真机症状就是 AppImage 启动瞬间 segfault。
    touched: list[int] = []
    ctx2 = SimpleNamespace(
        cfg=SimpleNamespace(players={"mode": "decrypt"}),
        players_warmup=None,
        set_status_fn=lambda *_a: touched.append(1),
        q=None,
    )
    try:
        gui_engine.ensure_players_warmup(ctx2)
        import time as _time
        _time.sleep(0.4)          # 给预热线程一次状态变化的机会
        check("没有队列时不碰状态栏（防 Tk 跨线程崩溃）", touched == [], str(touched))
    finally:
        gui_engine.stop_players_warmup(ctx2)


def main() -> int:
    test_defaults()
    test_validation()
    test_page()
    test_warmup_wiring()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（分玩家设置）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
