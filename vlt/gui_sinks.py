"""手腕屏與桌面字幕的 GUI 生命週期。"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from . import platform
from . import config as _cfg_mod
from .config_io import _fmt_scalar, _write_config_text, _yaml_set_or_create
from .output.overlay import OverlayConfig
from .i18n import t
from .gui_engine_context import EngineCtx

def start_overlay(ctx: EngineCtx, *, force: bool = False) -> bool:
    """按勾选状态接管 SteamVR 的手腕屏。失败只禁用这一项，绝不影响翻译。

    手腕屏由 **界面** 持有而不是某个引擎：手腕上只该有一块屏，内容是最近几句对话
    （镜像聊天区）。交给两个引擎各自创建会撞 ``OverlayError_KeyInUse``（用户实测）。

    *force* =True 用于「勾上就起」那条路（此时还没点开始翻译，``ctx.sinks`` 里没有 overlay）。
    返回是否 **真的起来了** —— 调用方要靠它决定失败时是否自动退回勾选。
    """
    if ctx.overlay_out is not None:
        return True                       # 已经起来了，别重复建（会撞 KeyInUse）
    if not force and "overlay" not in ctx.sinks:
        return False
    try:
        ctx.overlay_out = platform.create_wrist_overlay(
            OverlayConfig.from_dict(ctx.cfg.overlay), config_path=_cfg_mod.DEFAULT_CONFIG)
        if not ctx.overlay_out.start():
            ctx.overlay_out = None        # start() 内部已打印原因
            return False
        push_overlay(ctx, force=True)
        return True
    except Exception as exc:              # noqa: BLE001
        ctx.overlay_out = None
        print(f"[gui] ⚠️ 手腕屏初始化异常，已禁用（翻译不受影响）："
              f"{type(exc).__name__}: {exc}", flush=True)
        return False


def push_overlay(ctx: EngineCtx, force: bool = False) -> None:
    """把聊天区最近几条推给手腕屏（同一块屏显示两个方向的对话）。"""
    if ctx.overlay_out is None:
        return
    try:
        entries = [(b.who, b.source, b.text, b.label) for b in ctx.bubbles[-8:]]
        ctx.overlay_out.update_entries(entries, force=force)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 手腕屏刷新失败：{type(exc).__name__}: {exc}", flush=True)


def desktop_cfg(cfg) -> dict:
    """安全取 ``cfg.desktop_overlay`` 段（可能是 dict 或 None）。"""
    d = cfg.desktop_overlay
    return d if isinstance(d, dict) else {}


def start_desktop(ctx: EngineCtx, *, force: bool = False) -> bool:
    """把桌面字幕窗拉起来（PC 桌面模式：贴在 VRChat 窗口上的叠加窗）。

    与手腕屏同一套约定：由 **界面** 持有（内容是聊天区的镜像），
    失败只禁用这一项，绝不影响翻译。
    """
    if ctx.desktop_out is not None:
        return True
    if not force and "desktop" not in ctx.sinks:
        return False
    try:
        from .output.desktop_overlay import DesktopOverlay, DesktopOverlayConfig
        cfg = DesktopOverlayConfig.from_dict(desktop_cfg(ctx.cfg))
        out = DesktopOverlay(cfg, config_path=_cfg_mod.DEFAULT_CONFIG, root=ctx.root)
        if not out.start():
            return False                  # start() 内部已打印原因
        ctx.desktop_out = out
        push_desktop(ctx, force=True)
        return True
    except Exception as exc:              # noqa: BLE001
        ctx.desktop_out = None
        print(f"[gui] ⚠️ 桌面字幕初始化异常，已禁用（翻译不受影响）："
              f"{type(exc).__name__}: {exc}", flush=True)
        return False


def stop_desktop(ctx: EngineCtx) -> None:
    """关闭桌面字幕窗并复位拖拽状态。"""
    if ctx.desktop_out is None:
        return
    try:
        ctx.desktop_out.close()
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 关闭桌面字幕时出错（忽略）：{exc}", flush=True)
    ctx.desktop_out = None
    ctx.desktop_dragging = False
    # 文案跟着复位：字幕窗都关掉了还写着「锁定位置」，与真实状态不符
    btn = ctx.desktop_drag_btn
    if btn is not None:
        try:
            btn.configure(text=t("解锁拖动"))
        except Exception:                 # noqa: BLE001
            pass


def push_desktop(ctx: EngineCtx, force: bool = False) -> None:
    """把聊天区最近几条推给桌面字幕（与手腕屏同一份内容）。

    ⚠️ 条目形状必须与 ``push_overlay`` 一致（4 元组，带说话人昵称）：桌面字幕与手腕屏
    共用 ``overlay.render_conversation``，房间里的成员靠这个 label 才显示得出昵称。
    """
    if ctx.desktop_out is None:
        return
    try:
        entries = [(b.who, b.source, b.text, b.label) for b in ctx.bubbles[-8:]]
        ctx.desktop_out.update_entries(entries, force=force)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 桌面字幕刷新失败：{type(exc).__name__}: {exc}", flush=True)


def toggle_desktop_drag(ctx: EngineCtx) -> None:
    """解锁 / 锁定桌面字幕拖动。

    字幕窗默认 **鼠标穿透**（不挡着点 VRChat），穿透开着时窗口收不到鼠标事件，
    所以要拖必须先解锁；锁定 = 把落点折算成锚点+偏移写回配置并恢复穿透。
    """
    if ctx.desktop_out is None:
        ctx.desktop_dragging = False
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", t("桌面字幕还没开启，先勾上「桌面字幕」再解锁拖动"))
        return
    ctx.desktop_dragging = not ctx.desktop_dragging
    try:
        ctx.desktop_out.set_draggable(ctx.desktop_dragging)
    except Exception as exc:              # noqa: BLE001
        ctx.desktop_dragging = False
        print(f"[gui] 切换桌面字幕拖动失败：{type(exc).__name__}: {exc}", flush=True)
        return
    btn = ctx.desktop_drag_btn
    if btn is not None:
        try:
            btn.configure(text=t("锁定位置") if ctx.desktop_dragging else t("解锁拖动"))
        except Exception:                 # noqa: BLE001
            pass
    if ctx.desktop_dragging:
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("桌面字幕已解锁：拖动字幕窗到想要的位置，放好后点「锁定位置」"))
    else:
        save_desktop_cfg(ctx)
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("桌面字幕位置已记住"))


def schedule_desktop_save(ctx: EngineCtx) -> None:
    """防抖 300ms 后把桌面字幕参数写回 config.yaml。"""
    if ctx.desktop_save_job is not None:
        try:
            ctx.root.after_cancel(ctx.desktop_save_job)
        except Exception:
            pass
    ctx.desktop_save_job = ctx.root.after(300, lambda: save_desktop_cfg(ctx))


def save_desktop_cfg(ctx: EngineCtx, *, config_path: Path | None = None) -> None:
    """把桌面字幕的参数写回 config.yaml 的 ``desktop_overlay:`` 段。

    写什么：用户 **真动过** 的滑块（字号 / 尺寸 / 透明度）+ 拖动折算出的锚点/偏移。

    ⚠️ 滑块值只在 **用户真的动过** 时才写：无条件写的话，用户只是把字幕拖了个位置，
    滑块上那些（默认）值就被写进配置并热重载生效 —— 表现为「拖一下位置，字号/透明度突然变了」。
    """
    ctx.desktop_save_job = None
    p = config_path if config_path is not None else _cfg_mod.DEFAULT_CONFIG
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
        updates: list[tuple[list[str], str]] = []
        mem: dict[str, Any] = {}
        tuned = ctx.desktop_tuned
        if ctx.desktop_alpha_touched and ctx.desktop_alpha_var is not None:
            a = float(ctx.desktop_alpha_var.get())
            updates.append((["desktop_overlay", "alpha"], _fmt_scalar(a)))
            mem["alpha"] = a
        if "font_size" in tuned and ctx.desktop_font_var is not None:
            v = int(float(ctx.desktop_font_var.get()))
            updates.append((["desktop_overlay", "font_size"], str(v)))
            mem["font_size"] = v
        if "source_font_size" in tuned and ctx.desktop_srcfont_var is not None:
            v = int(float(ctx.desktop_srcfont_var.get()))
            updates.append((["desktop_overlay", "source_font_size"], str(v)))
            mem["source_font_size"] = v
        if tuned & {"panel_width", "panel_height"} and ctx.desktop_w_var is not None and ctx.desktop_h_var is not None:
            w = int(float(ctx.desktop_w_var.get()))
            h = int(float(ctx.desktop_h_var.get()))
            updates.append((["desktop_overlay", "size_px"], f"[{w}, {h}]"))
            mem["size_px"] = [w, h]
        if ctx.desktop_out is not None:
            for key, val in (ctx.desktop_out.snap_to_config() or {}).items():
                if key in ("offset", "pos"):
                    updates.append((["desktop_overlay", key], f"[{val[0]}, {val[1]}]"))
                else:
                    updates.append((["desktop_overlay", key], str(val)))
        if not updates:
            return
        for key_path, value in updates:
            text = _yaml_set_or_create(text, key_path, value)
        _write_config_text(p, text)
        # 同步内存快照：start_desktop() 是按 ctx.cfg 建窗的，不同步的话
        # 「关掉桌面字幕再勾上」会用旧值重建窗口。
        if mem:
            if not isinstance(ctx.cfg.desktop_overlay, dict):
                ctx.cfg.desktop_overlay = {}
            ctx.cfg.desktop_overlay.update(mem)
        print("[gui] 桌面字幕参数已写入 config.yaml："
              + " ".join(f"{'/'.join(k)}={v}" for k, v in updates), flush=True)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 保存桌面字幕参数失败：{exc}", flush=True)


def _stop_overlay(ctx: EngineCtx) -> None:
    """关闭手腕屏。"""
    if ctx.overlay_out is None:
        return
    try:
        ctx.overlay_out.close()
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 关闭手腕屏时出错（忽略）：{exc}", flush=True)
    ctx.overlay_out = None
