"""首次启用麦克风代理时给一次说明（它会常驻占用虚拟声卡的输出流）；只提示一次。

## 为什么要有这个弹窗

代理默认是**开**的，而且程序一启动就打开虚拟声卡的输出流（见 ``micproxy`` 模块头「生命周期」）。
用户看到的现象却是「装完这软件，我声卡就一直响/有回声」，却不知道是谁占的 ——
尤其**默认播放设备也指向那块虚拟声卡**的人，会一直听到自己的声音。

所以第一次真正把代理跑起来时，明说一次：占用了什么、回声从哪来、去哪儿关掉。
提示过就落盘（``output.audio.proxy.hint_shown``），以后不再打扰。

## 纪律

- ``headless``（跑用例 / ``--self-test`` / 打包自检）**既不弹窗、也不写标记** ——
  否则自检会把用户配置里的标记写成「已提示」，用户永远看不到这条说明。
- 弹窗是**非模态**（``transient`` + ``lift``，**不** ``grab_set``）：翻译正在跑时也不许卡住界面，
  与 ``gui_update.show_update_dialog`` 同一口径。
- 本模块**不许** ``import vlt.gui``（防循环引用），控件与回调都从 ``gui`` 属性上取。
  ⚠️ 但配置路径例外地要拿：走 `gui.__class__.__module__` 取那个模块的 `DEFAULT_CONFIG`
  （与 `gui_proxy._m(self)` 同一个理由 —— 用例把那套打桩指到临时文件时才生效）。
- 「已提示过」这件事**不能留不下痕迹**：config.yaml 写不进去时（只读目录 / 文件被占用 /
  配置损坏）落一个同目录的**兜底标记文件**（见 `write_fallback_marker`）。否则用户每次启动
  都会被同一条说明再打扰一次，而它按设计只该弹一次 —— 这条与「禁静默降级」是同一件事的
  两个面：降级必须留痕，留痕之后就不该再重复打扰。
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .i18n import t

#: 兜底标记文件名（与 config.yaml 同目录、点号开头 = 内部状态）。
_FALLBACK_NAME = ".proxy-hint-shown"


def _gui_module(gui):
    """定义 *gui* 那个类的模块（= `vlt.gui`）—— 配置路径要从它上面取，见模块说明。"""
    return sys.modules.get(gui.__class__.__module__)


def fallback_marker_path(gui) -> Path | None:
    """兜底标记文件的路径（config.yaml 同目录）；取不到配置路径就返回 None。"""
    cfg = getattr(_gui_module(gui), "DEFAULT_CONFIG", None)
    return Path(cfg).with_name(_FALLBACK_NAME) if cfg else None


def already_hinted(gui) -> bool:
    """说明是否已经弹过：配置里的 ``hint_shown`` **或**兜底标记文件，有一个就算。

    用兜底文件时每次启动留一行痕（禁静默降级）—— 它意味着 config.yaml 一直写不进去，
    用户该知道这件事，也该知道删掉这个文件就能再看一次说明。
    """
    proxy_cfg = (gui._proxy_audio_cfg().get("proxy") or {}) if hasattr(gui, "_proxy_audio_cfg") else {}
    if proxy_cfg.get("hint_shown"):
        return True
    p = fallback_marker_path(gui)
    if p is None or not p.exists():
        return False
    try:
        why = p.read_text(encoding="utf-8").strip()
    except OSError:
        why = ""
    print(f"[proxy] 首次启用说明的「已提示」标记来自兜底文件 {p}"
          f"{f'（config.yaml 写不进去：{why}）' if why else ''}"
          " —— 删除它可以让这条说明再弹一次", flush=True)
    return True


def write_fallback_marker(gui) -> bool:
    """config.yaml 写不进去时的兜底：把「已提示过」落成同目录的一个小文件。

    只在真正需要时才被调（`gui_proxy._mark_proxy_hint_shown` 发现标记没落盘）。
    成功/失败都留一行 ``[proxy]``（禁静默降级）：成功要让人知道多了个文件、怎么撤销；
    失败要明确说出后果 —— 下次启动还会再弹一次。
    """
    p = fallback_marker_path(gui)
    if p is None:
        print("[proxy] ⚠️ 连兜底标记也写不了：取不到 config.yaml 的路径 —— "
              "下次启动还会再弹一次这条说明", flush=True)
        return False
    try:
        p.write_text("代理首次启用说明已弹过（config.yaml 写不进去时的兜底标记）\n",
                     encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[proxy] ⚠️ 连兜底标记 {p} 也写不了（{exc}）—— 下次启动还会再弹一次这条说明",
              flush=True)
        return False
    print(f"[proxy] ⚠️ config.yaml 改不动，已改用兜底标记 {p} 记住「说明已弹过」"
          "（它保证以后不再打扰；想让说明再弹一次就删掉它）", flush=True)
    return True


def maybe_show(gui, *, on_shown=None, opener=None) -> bool:
    """首次成功启用代理时弹一次说明，返回是否真的弹了。

    - ``on_shown``：弹窗**已显示**后调用（用来落盘「已提示」标记）；不传就只弹。
    - ``opener``：开窗函数替身（用例里传假的，免得在 CI 上真开 Tk 窗）。

    三条早退：``headless`` / 已经提示过（配置里的标记或兜底标记）/ 窗口被销毁后重建——都不弹。
    """
    if getattr(gui, "_headless", False):
        return False
    if already_hinted(gui):
        return False
    (opener or _open_window)(gui)
    print("[proxy] 首次启用说明已弹出（占用虚拟声卡输出流 / 默认播放设备指向它会听到回声）",
          flush=True)
    if on_shown is not None:
        on_shown()
    return True


def _open_window(gui) -> None:
    """懒建非模态 Toplevel：标题 + 正文 + 「知道了」。已存在就抬起来。"""
    win = getattr(gui, "_proxy_hint_win", None)
    if win is not None:
        try:
            if win.winfo_exists():
                win.lift()
                return
        except Exception:                                  # noqa: BLE001
            pass

    from .ui_theme import PANEL
    from .ui_tk import FONT_BOLD_MD

    win = tk.Toplevel(gui._root)
    win.title(t("麦克风代理已启用"))
    win.configure(bg=PANEL)
    win.transient(gui._root)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: close(gui))
    win.bind("<Escape>", lambda _e: close(gui))
    gui._proxy_hint_win = win

    body = ttk.Frame(win, padding=(20, 16, 20, 14))
    body.pack(fill=tk.BOTH, expand=True)
    ttk.Label(body, text=t("麦克风代理已启用"), font=FONT_BOLD_MD).pack(anchor=tk.W)
    # ⚠️ 正文必须是**字面量**（相邻字符串会被编译器拼成一个常量）：`t(BODY)` 这种动态 key
    #    不受 i18n 守卫的覆盖检查 —— 常量改了而词表没跟上时，界面会静默回落成中文原文（踩过）。
    ttk.Label(body, text=t(
        "程序会把真实麦克风直通到虚拟声卡（VRChat 里麦克风固定选它），并常驻占用它的输出流。\n\n"
        "如果你的「默认播放设备」也是这块虚拟声卡，会听到自己的声音（回声）。\n"
        "不需要的话：设置 → 麦克风代理 → 取消勾选（立即释放声卡）。"),
        wraplength=420, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))

    btns = ttk.Frame(body)
    btns.pack(fill=tk.X, pady=(16, 0))
    ok = ttk.Button(btns, text=t("知道了"), style="Accent.TButton",
                    command=lambda: close(gui))
    ok.pack(side=tk.LEFT)
    ok.focus_set()

    win.update_idletasks()
    rx, ry = gui._root.winfo_x(), gui._root.winfo_y()
    rw = gui._root.winfo_width()
    win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 60}")
    try:
        gui._apply_dark_titlebar(win)
    except Exception:                                      # noqa: BLE001
        pass
    win.lift()


def close(gui) -> None:
    """关掉弹窗（幂等）：退出程序、按 Esc、点「知道了」都走这里。"""
    win, gui._proxy_hint_win = getattr(gui, "_proxy_hint_win", None), None
    if win is None:
        return
    try:
        win.destroy()
    except Exception:                                      # noqa: BLE001
        pass
