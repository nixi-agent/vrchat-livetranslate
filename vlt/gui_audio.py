"""设备扫描 / 输入门限控制的 UI 构建与纯逻辑。

从 ``vlt.gui.TranslationGUI`` 的设备选择区与输入门限区提取而来。
所有 Tk 控件与可变状态通过 :class:`AudioCtx` 传入，函数本身不持有
``self`` 引用。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Optional

import tkinter as tk

from . import config as _config_mod
from .config_io import (
    _fmt_scalar,
    _write_config_text,
    _yaml_scalar,
    _yaml_set_or_create,
)
from .devices import (
    enumerate_audio_out_devices,
    enumerate_loopback_devices,
    enumerate_mic_devices,
    format_device_display,
)
from .engine import INPUT_GATE_MIN_DB, LEVEL_FLOOR_DB
from .i18n import t
from .level_probe import LevelProbe
from .ui_theme import ACCENT, SURFACE_HOVER, TEXT
from .ui_tk import combo_values


# ================================================================ 上下文

@dataclass
class AudioCtx:
    """设备 / 门限功能所需的全部 **控件引用** 与 **回调**。

    可变状态（``gate_probe`` / ``*_names`` 等）由 gui.py 的薄壳方法
    管理，本模块的函数通过参数接收，不直接持有。
    """
    # ── 设备控件 ──
    mic_combo: Any = None                   # ttk.Combobox
    loopback_combo: Any = None              # ttk.Combobox
    audio_out_combo: Any = None             # ttk.Combobox
    linux_fixed_audio: bool = False

    # ── 门限控件 ──
    gate_var: Any = None                    # tk.DoubleVar（阈值 dB）
    gate_enabled_var: Any = None            # tk.BooleanVar
    gate_val_lbl: Any = None                # ttk.Label
    gate_level_canvas: Any = None           # tk.Canvas
    gate_level_lbl: Any = None              # ttk.Label

    # ── 回调 ──
    set_status_fn: Optional[Callable] = None           # (level, msg) -> None
    is_closing_fn: Optional[Callable] = None           # () -> bool
    settings_win_fn: Optional[Callable] = None         # () -> tk.Toplevel | None


# ================================================================ 设备扫描


def sync_from_gui(ctx: AudioCtx, gui) -> None:
    """从 gui 实例同步控件引用和回调到 AudioCtx。"""
    c = ctx; g = getattr
    for n in ("mic_combo", "loopback_combo", "audio_out_combo", "gate_var", "gate_enabled_var",
              "gate_val_lbl", "gate_level_canvas", "gate_level_lbl"):
        setattr(c, n, g(gui, f"_{n}", None))
    c.linux_fixed_audio = g(gui, '_linux_fixed_audio', False)
    c.set_status_fn = gui._set_status; c.is_closing_fn = lambda: gui._closing
    c.settings_win_fn = lambda: gui._settings_win
    gui._gate_holder.update(probe=gui._gate_probe, save_job=gui._gate_save_job,
        level_hold=gui._gate_level_hold, hold_ms=gui._gate_hold_ms,
        preroll_ms=gui._gate_preroll_ms, engines=gui._engines)


def start_device_scan(ctx: AudioCtx, root: tk.Misc, headless: bool,
                      scan_holder: dict, cfg=None) -> None:
    """扫描设备（**主线程同步执行**）。

    ⚠️ `cfg` **必须**透传：`on_device_scan_result` 的「恢复上次选择」完全依赖它
    （读 `cfg.output.capture.mic_device`）。曾经这里写死 `cfg=None`，于是启动/刷新后
    麦克风下拉**永远显示「自动检测」**，配置里选好的设备在 UI 上凭空消失
    （实际采集不受影响 —— 代理/引擎直接读配置）。回归见 `tests/test_device_save.py`。

    为什么不用后台线程（**Windows 侧的理由**）：PortAudio 的初始化 / 销毁是**线程绑定**
    的（WASAPI 用 COM 单元）。sounddevice 在首次调用的那个线程里 Pa_Initialize，而它的
    atexit 钩子在主线程 Pa_Terminate —— 跨线程销毁会抛 Tcl_AsyncDelete。
    （Linux 枚举走 `pw-dump` 子进程、不碰 PortAudio；同步做只为两平台口径一致。）
    """
    if headless:
        return
    scan_holder["pending"] = True
    try:
        root.update_idletasks()
        mics = enumerate_mic_devices()
        loops = [] if ctx.linux_fixed_audio else enumerate_loopback_devices()
        outs = [] if ctx.linux_fixed_audio else enumerate_audio_out_devices()
        on_device_scan_result(ctx, cfg=cfg, mics=mics, loops=loops, outs=outs,
                              names_holder=scan_holder.get("names", {}),
                              scan_holder=scan_holder)
    except Exception as exc:                         # noqa: BLE001
        scan_holder["pending"] = False
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", t("设备扫描失败：{msg}", msg=exc))


def on_refresh_devices(ctx: AudioCtx, root: tk.Misc, engines: list,
                       headless: bool, scan_holder: dict, cfg=None) -> None:
    """用户点「刷新设备」：翻译在跑时先提醒，再扫描。"""
    if any(e.running for e in engines):
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", t("建议停止翻译后再刷新设备列表"))
    start_device_scan(ctx, root, headless, scan_holder, cfg=cfg)
    if ctx.set_status_fn:
        ctx.set_status_fn("info", t("正在扫描设备…"))


def on_device_scan_result(ctx: AudioCtx, cfg, mics, loops, outs,
                          names_holder: dict,
                          scan_holder: dict | None = None) -> None:
    """扫描完成：填充下拉、恢复配置里的选择、更新状态栏。"""
    auto = t("自动检测")
    names_holder["mic"] = []
    mic_display = [auto]
    for info in mics:
        names_holder["mic"].append(
            (info.node_name or info.name) if ctx.linux_fixed_audio else info.name)
        mic_display.append(format_device_display(info))

    if ctx.linux_fixed_audio:
        counts = Counter(mic_display[1:])
        for i, info in enumerate(mics, 1):
            if counts[mic_display[i]] > 1 and info.node_name:
                mic_display[i] += f" [{info.node_name}]"

    names_holder["loop"] = []
    loop_display = [auto]
    for info in loops:
        names_holder["loop"].append(info.name)
        loop_display.append(format_device_display(info))

    names_holder["out"] = []
    out_display = [auto]
    for info in outs:
        names_holder["out"].append(info.name)
        out_display.append(format_device_display(info))

    ctx.mic_combo.configure(values=mic_display)
    if not ctx.linux_fixed_audio:
        ctx.loopback_combo.configure(values=loop_display)
        ctx.audio_out_combo.configure(values=out_display)

    # 恢复配置里的选择（如果设备在列表里）
    capture_cfg = {}
    audio_cfg = {}
    if cfg is not None:
        capture_cfg = (cfg.output or {}).get("capture") or {}
        audio_cfg = (cfg.output or {}).get("audio") or {}
    mic_name = capture_cfg.get("mic_device") or ""
    loop_name = capture_cfg.get("loopback_device") or ""
    out_name = audio_cfg.get("device_name") or ""

    # Linux 旧配置仍存描述：仅在唯一命中时迁移，之后重连改描述也保留同一节点。
    if ctx.linux_fixed_audio and mic_name and mic_name not in names_holder["mic"]:
        matches = [i for i, info in enumerate(mics) if info.name == mic_name]
        if len(matches) == 1:
            mic_name = names_holder["mic"][matches[0]]
            save_device_config(ctx, cfg, mic_name, loop_name, out_name)

    if mic_name and mic_name in names_holder["mic"]:
        ctx.mic_combo.set(mic_display[names_holder["mic"].index(mic_name) + 1])
    else:
        ctx.mic_combo.set(auto)

    if not ctx.linux_fixed_audio:
        if loop_name and loop_name in names_holder["loop"]:
            ctx.loopback_combo.set(
                loop_display[names_holder["loop"].index(loop_name) + 1])
        else:
            ctx.loopback_combo.set(auto)

        if out_name and out_name in names_holder["out"]:
            ctx.audio_out_combo.set(
                out_display[names_holder["out"].index(out_name) + 1])
        else:
            ctx.audio_out_combo.set(auto)

    if not mics and not loops and not outs:
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", t("未扫描到设备（远程会话下枚举为空是正常的）"))
    elif ctx.linux_fixed_audio:
        if ctx.set_status_fn:
            ctx.set_status_fn("info",
                t("已扫描到 {m} 个麦克风（VRChat 音频与译音输出自动处理）", m=len(mics)))
    else:
        if ctx.set_status_fn:
            ctx.set_status_fn("info",
                t("已扫描到 {m} 个麦克风 / {l} 个 loopback / {o} 个输出",
                  m=len(mics), l=len(loops), o=len(outs)))
    if scan_holder is not None:
        scan_holder["pending"] = False


def on_device_change(ctx: AudioCtx, cfg, names_holder: dict) -> None:
    """设备下拉变更：解析选择 → 保存 → 状态栏回显。"""
    auto = t("自动检测")
    mic_text = ctx.mic_combo.get()

    mic_name = ""
    if mic_text != auto and mic_text:
        display_list = combo_values(ctx.mic_combo)
        idx = display_list.index(mic_text) if mic_text in display_list else -1
        if idx > 0 and idx - 1 < len(names_holder["mic"]):
            mic_name = names_holder["mic"][idx - 1]

    loop_name = ""
    out_name = ""
    if not ctx.linux_fixed_audio:
        loop_text = ctx.loopback_combo.get()
        if loop_text != auto and loop_text:
            display_list = combo_values(ctx.loopback_combo)
            idx = display_list.index(loop_text) if loop_text in display_list else -1
            if idx > 0 and idx - 1 < len(names_holder["loop"]):
                loop_name = names_holder["loop"][idx - 1]

        out_text = ctx.audio_out_combo.get()
        if out_text != auto and out_text:
            display_list = combo_values(ctx.audio_out_combo)
            idx = display_list.index(out_text) if out_text in display_list else -1
            if idx > 0 and idx - 1 < len(names_holder["out"]):
                out_name = names_holder["out"][idx - 1]

    save_device_config(ctx, cfg, mic_name, loop_name, out_name)
    picked = [n for n in (mic_name, loop_name, out_name) if n]
    if ctx.set_status_fn:
        if picked:
            ctx.set_status_fn("info", t("已选设备：{names}", names=" | ".join(picked)))
        else:
            ctx.set_status_fn("info", t("设备：全部自动检测"))


def save_device_config(ctx: AudioCtx, cfg,
                       mic_name: str, loop_name: str, out_name: str) -> None:
    """把设备选择写回 config.yaml —— **就地改那几行**，不整份重写。

    Linux：只写麦克风。``loopback_device`` / ``output.audio.device_name`` 原样保留。
    """
    write_fixed = not ctx.linux_fixed_audio
    p = _config_mod.DEFAULT_CONFIG
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
        updates: list[tuple[list[str], str]] = [
            (["capture", "mic_device"], _yaml_scalar(mic_name)),
        ]
        if write_fixed:
            updates.append((["capture", "loopback_device"], _yaml_scalar(loop_name)))
            updates.append((["output", "audio", "device_name"], _yaml_scalar(out_name)))
        for key_path, val in updates:
            text = _yaml_set_or_create(text, key_path, val)
        _write_config_text(p, text)
    except Exception as exc:                         # noqa: BLE001
        print(f"[gui] 保存设备配置失败：{exc}", flush=True)
        return
    if cfg is not None:
        cfg.output.setdefault("capture", {})["mic_device"] = mic_name
        if write_fixed:
            cfg.output["capture"]["loopback_device"] = loop_name
            cfg.output.setdefault("audio", {})["device_name"] = out_name


# ================================================================ 输入门限

def on_gate_change(ctx: AudioCtx, cfg, root: tk.Misc,
                   engines: list, gate_holder: dict) -> None:
    """门限开关 / 滑块变化：立刻更新显示与正在跑的引擎，落盘延后 300ms。"""
    db = float(ctx.gate_var.get())
    ctx.gate_val_lbl.configure(text=f"{db:g} dB")
    apply_gate_live(ctx, engines)
    sync_gate_level_probe(ctx, cfg, gate_holder)
    job = gate_holder.get("save_job")
    if job is not None:
        try:
            root.after_cancel(job)
        except Exception:                            # noqa: BLE001
            pass
    gate_holder["save_job"] = root.after(300, lambda: save_gate_cfg(ctx, cfg, gate_holder))


def apply_gate_live(ctx: AudioCtx, engines: list) -> None:
    """把界面上的门限热更新到**正在跑**的引擎（不必等下次「开始翻译」）。"""
    en = bool(ctx.gate_enabled_var.get())
    db = float(ctx.gate_var.get())
    n = 0
    for e in engines:
        g = getattr(e, "input_gate", None)
        if g is None:
            continue
        g.enabled = en
        g.threshold_db = db
        n += 1
    if n:
        print(f"[gui] 输入门限已热更新（{n} 条腿）：{'开' if en else '关'} "
              f"{db:g} dBFS", flush=True)


def save_gate_cfg(ctx: AudioCtx, cfg, gate_holder: dict) -> None:
    """把门限写回 config.yaml 的 capture 段（就地改，保住注释与键顺序）。"""
    gate_holder["save_job"] = None
    p = _config_mod.DEFAULT_CONFIG
    if not p.exists():
        return
    enabled = bool(ctx.gate_enabled_var.get())
    db = round(float(ctx.gate_var.get()), 1)
    try:
        import yaml as _yaml
        text = p.read_text(encoding="utf-8")
        text = _yaml_set_or_create(text, ["capture", "gate_enabled"], _fmt_scalar(enabled))
        text = _yaml_set_or_create(text, ["capture", "gate_db"], _fmt_scalar(db))
        try:
            cap_now = (_yaml.safe_load(text) or {}).get("capture") or {}
        except Exception:                            # noqa: BLE001
            cap_now = {}
        for key, val in (("gate_hold_ms", gate_holder.get("hold_ms", 500.0)),
                         ("gate_preroll_ms", gate_holder.get("preroll_ms", 250))):
            if key not in cap_now:
                text = _yaml_set_or_create(text, ["capture", key], _fmt_scalar(int(val)))
        _write_config_text(p, text)
    except Exception as exc:                         # noqa: BLE001
        print(f"[gui] 保存输入门限失败：{exc}", flush=True)
        return
    if cfg is not None:
        cap = cfg.output.setdefault("capture", {})
        cap["gate_enabled"] = enabled
        cap["gate_db"] = db
    print(f"[gui] 输入门限已写入 config.yaml：enabled={enabled} gate_db={db:g}"
          f"（hold {gate_holder.get('hold_ms', 500):g}ms / "
          f"preroll {gate_holder.get('preroll_ms', 250)}ms 沿用配置）",
          flush=True)
    if ctx.set_status_fn:
        ctx.set_status_fn("info", t("输入门限已保存：{db} dB", db=f"{db:g}"))


def gate_probe_wanted(ctx: AudioCtx, engines: list) -> bool:
    """独立电平探针**该不该在跑**（四个条件同时成立）。

    ① 没在退出；② 没有引擎在跑；③ 勾了「启用」；④ 设置窗**可见**。
    """
    if ctx.is_closing_fn and ctx.is_closing_fn():
        return False
    if engines:
        return False
    var = ctx.gate_enabled_var
    if var is None or not bool(var.get()):
        return False
    win = ctx.settings_win_fn() if ctx.settings_win_fn else None
    if win is None:
        return False
    try:
        return bool(win.winfo_viewable())
    except Exception:                                # noqa: BLE001
        return False


def sync_gate_level_probe(ctx: AudioCtx, cfg, gate_holder: dict) -> None:
    """把探针对齐到 ``gate_probe_wanted()`` 的判定（幂等）。"""
    if not gate_probe_wanted(ctx, gate_holder.get("engines", [])):
        stop_gate_probe(ctx, gate_holder)
        return
    if gate_holder.get("probe") is not None:
        return
    capture_cfg = (cfg.output or {}).get("capture") or {}
    name = capture_cfg.get("loopback_device") or None
    probe = LevelProbe(device_name=name)
    gate_holder["probe"] = probe
    probe.start()
    print(f"[level] 设置窗已打开且勾了「启用」→ 开始独立电平采集"
          f"（设备：{name or '自动检测'}；没在翻译，不占用引擎那条腿）", flush=True)


def stop_gate_probe(ctx: AudioCtx, gate_holder: dict) -> None:
    """停掉探针并释放设备。"""
    probe = gate_holder.get("probe")
    if probe is None:
        return
    gate_holder["probe"] = None
    was_running = probe.running
    probe.stop()
    if was_running:
        print(f"[level] 已停止独立电平采集并释放设备（本次共采 {probe.chunks} 块）",
              flush=True)


def gate_level_db(ctx: AudioCtx, engines: list,
                  gate_holder: dict) -> float | None:
    """当前该画出来的电平（dBFS）；没有可用来源时返回 None。"""
    if engines:
        lvl = LEVEL_FLOOR_DB
        for e in engines:
            g = getattr(e, "input_gate", None)
            if g is not None:
                lvl = max(lvl, float(g.level_db))
        return lvl
    probe = gate_holder.get("probe")
    if probe is not None and probe.running and probe.has_data:
        return float(probe.level_db)
    return None


def refresh_gate_level(ctx: AudioCtx, engines: list,
                       gate_holder: dict) -> None:
    """刷新设置窗里的实时电平条（每 100ms 一次）。"""
    cv = ctx.gate_level_canvas
    gvar = ctx.gate_var
    if cv is None or gvar is None:
        return
    try:
        if not cv.winfo_exists():
            return
        w = max(10, int(cv.winfo_width()))
        h = max(6, int(cv.winfo_height()))
    except Exception:                                # noqa: BLE001
        return
    active = bool(ctx.gate_enabled_var.get())
    thr = max(INPUT_GATE_MIN_DB, min(0.0, float(gvar.get())))
    lo, hi = INPUT_GATE_MIN_DB, 0.0
    x_thr = (thr - lo) / (hi - lo) * w
    cv.delete("all")
    cv.create_line(x_thr, 0, x_thr, h, fill=TEXT, width=2)
    lvl = gate_level_db(ctx, engines, gate_holder)
    if lvl is None:
        gate_holder["level_hold"] = LEVEL_FLOOR_DB
        if ctx.gate_level_lbl is not None:
            try:
                ctx.gate_level_lbl.configure(text="—")
            except Exception:                        # noqa: BLE001
                pass
        return
    hold = gate_holder.get("level_hold", LEVEL_FLOOR_DB)
    hold = max(lvl, hold - 1.5, LEVEL_FLOOR_DB)
    gate_holder["level_hold"] = hold
    db = max(lo, min(hi, hold))
    x_lvl = (db - lo) / (hi - lo) * w
    cv.create_rectangle(0, 0, x_lvl, h, outline="",
                        fill=(ACCENT if (active and db >= thr) else SURFACE_HOVER))
    if ctx.gate_level_lbl is not None:
        ctx.gate_level_lbl.configure(text=f"{db:.0f} dB")
