"""桌面模式的字幕叠加窗（issue #11）：无边框 / 置顶 / 鼠标穿透 / 跟随游戏窗口。

PC 端（不戴头显）用户的诉求原话是「译文像歌词一样贴在屏幕任意角落，最好能改透明度」——
以前只有手腕屏（VR 贴图）一条腿，桌面玩家必须切回本程序窗口才看得到别人在说什么。

本模块是**共享**的（两个平台的产物都带它），三条边界要守住：

* **渲染复用** `vlt/output/overlay.py` 的 `render_conversation` / `render_panel`：
  桌面字幕与手腕屏长一个样，这里不另写一套画字逻辑；
* **出图口有两条腿**（`desktop_overlay.backend` 选择，见 :data:`BACKENDS`）：
  * **原生窗**（Linux：Wayland 走 layer-shell + wl_shm；X11 走 32 位 ARGB 覆盖窗 +
    XPutImage —— 都是逐像素透明 / 协议级鼠标穿透 / 置顶）——经门面
    `create_desktop_window` 拿窗口对象，此后只按窗口契约调用
    （`vlt/platform/base.py:DesktopWindow`）；
  * **Tk 回落**（Windows 色键 / 原生窗用不了的会话）：
    `overrideredirect` 无边框 + `-topmost` + `-alpha`；Windows 用 `-transparentcolor`
    色键、Linux 用 1 位**形状蒙版**（`alpha_mask_bits()` → `set_window_shape`）把
    RGBA 面板外的键色底抠掉（圆角是 1 位的、会有锯齿；逐像素半透明做不到 ——
    那是原生窗的事）；
* **平台能力一律走 `vlt.platform` 门面**（找窗口 / 客户区 / 鼠标穿透 / 工作区 / 原生窗）：
  本文件里不许出现 Win32 调用，也不许直接 import 平台独占模块 —— 那是红灯门禁
  （`tests/test_desktop_overlay.py::test_shared_module_stays_platform_clean`；
  产物级的「不混进另一个平台实现」另由 `scripts/check_platform_purity.py` 把关）。
  门面上没有对等实现的平台会拿到安全默认值 —— 降级由调用方/后端打日志说明。

离线可验证：`python -m vlt.output.desktop_overlay --demo --out <dir>` 只渲染 PNG，不建窗口。
"""
from __future__ import annotations

import time as time
import threading
from pathlib import Path
from typing import Any

from PIL import Image

# 调试帧是"跑完要看"的产物 → 可写目录（exe 旁），与手腕屏的 dry-run 同一个口径
from ..paths import APP_DIR
from ..platform.overlay_pixels import alpha_mask_bits as alpha_mask_bits
from ..session.chatgpt_output import allowed, current, same, scope, select_rows
from .overlay import OverlayConfig as OverlayConfig, _unpack_entry, render_conversation, render_panel

# 色键：窗口底色与贴图底色必须是**同一个**颜色，Tk 才会把那一片抠成透明。
# 选 (1,2,3) 是因为它离面板配色足够远（不会被误抠），又不是纯黑（纯黑会与文字抗锯齿边打架）。
from .desktop_model import (  # noqa: F401 — preserve existing import locations
    TRANSPARENT_KEY, KEY_RGB, ANCHORS, MODES,
    BACKENDS, ALPHA_MIN, ALPHA_MAX, ALPHA_DEFAULT,
    _ANCHOR_GRID, _MISSING, _VISUAL_FIELDS, desktop_visual_defaults,
    DesktopOverlayConfig, _pick, _as_pair, _as_int,
    _as_str, _as_bool, clamp_alpha, _clamp_into_area,
    compute_position, best_anchor, resolve_position,
)
from .desktop_controls import _DesktopWindowControls


class DesktopOverlay(_DesktopWindowControls):
    """桌面字幕窗：生命周期 + 出图 + 跟随 + 配置热重载。

    接口与手腕屏后端对齐（`start/update/update_entries/tick/close/available`），
    所以界面/引擎那边不需要为"桌面还是 VR"分叉。

    线程约定：**只有 `dry_run` 才会在 `update*()` 里立刻出图**。真窗口模式下一律置脏、
    由 `tick()` 出图 —— `tick()` 在 Tk 主循环里跑，而 `update*()` 可能来自采集/翻译线程，
    跨线程碰 Tk 是会崩的。GUI 每 50ms 一跳，最坏延迟一跳，用户看不出来。
    """

    available = True                      # 平台不支持（没有 Tk）时 start() 会置 False

    SEARCH_RETRY_S = 1.0                  # 找不到目标窗口时的重找间隔（别每 50ms 扫一遍窗口表）

    def __init__(self, cfg: DesktopOverlayConfig, config_path: Any = None,
                 dry_run: bool = False, root: Any = None) -> None:
        self.cfg = cfg
        self.config_path = Path(config_path) if config_path else None
        self.dry_run = bool(dry_run)
        # dry-run 的帧目录：与手腕屏的 out/overlay_frames 并列，跑完能直接翻图
        self._frames_dir = APP_DIR / "out" / "desktop_overlay_frames"
        self._root = root
        self._own_root = root is None     # 自建的 root 由我们负责 destroy
        self._pump = root is None         # 自建 root → 没有主循环，tick() 里自己泵事件
        self._win: Any = None
        self._native: Any = None          # 原生窗（Wayland layer-shell / X11 ARGB）；None = Tk 那条腿
        self._label: Any = None
        self._photo: Any = None           # ⚠️ 必须留引用：PhotoImage 被 GC = 窗口变空白
        self._hwnd = 0
        self._game_hwnd: int | None = None
        self._game_rect: tuple[int, int, int, int] | None = None
        self._frames = 0
        self._dirty = False
        self._started = False
        self._dragging = False
        self._drag_from: tuple[int, int] | None = None   # 按下时鼠标相对窗口左上角的位移
        self.user_pos: tuple[int, int] | None = None     # 用户拖完的落点（界面拿它写回配置）
        self._entries: list[tuple[str, str, str]] = []
        self._latest: tuple[str, str] = ("", "")
        self._last_sig: Any = None
        self._content: Any = None
        self._displayed_approval: Any = None
        self._epoch = 0
        self._lock = threading.Lock()
        self._last_cfg_mtime = 0.0
        self._next_search = 0.0
        self._area_fallback_logged = False
        self._rect_missing_logged = False
        self._exstyles_dirty = False
        self._need_shape_mask = False     # Tk 回落 + 没色键（Linux）→ 每帧用形状蒙版抠底
        self._shape_warned = False

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """建窗 → 找目标窗口 → 贴上去。

        找不到目标窗口**也算成功**（退化成 `cfg.pos` 绝对定位，桌面玩家没开游戏时
        照样能用来练手/摆位置）；只有窗口根本建不起来这类硬失败才返回 False。

        窗口后端的选择：`backend=native/wayland/x11` 或 `auto` 且门面能给出原生窗
        （Wayland layer-shell / X11 ARGB 覆盖窗，逐像素透明）→ 用它；否则回落 Tk
        （Windows 色键 / 没有 layer-shell 且没有 X11 的会话）。
        """
        if self._started:
            return True
        if self.dry_run:
            try:
                self._frames_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"[desktop] ❌ [dry-run] 建不出帧目录 {self._frames_dir}：{exc}")
                self.available = False
                return False
            print(f"[desktop][dry-run] 不建窗口，出图写到 {self._frames_dir}")
            self._started = True
            self.available = True
            return True

        if self.cfg.backend != "tk":
            self._native = self._create_native_window()
            if self._native is None and self.cfg.backend in ("native", "wayland", "x11"):
                print(f"[desktop] ⚠️ backend={self.cfg.backend} 但原生窗建不起来"
                      f" → 自动回落到 Tk")

        if self._native is None:
            try:
                import tkinter as tk
            except Exception as exc:  # noqa: BLE001 — 没有 Tk 就没有桌面字幕这条腿
                print(f"[desktop] ❌ 本机没有 Tk（tkinter 不可用）：{type(exc).__name__}: {exc}")
                self.available = False
                return False
            try:
                if self._root is None:
                    self._root = tk.Tk()
                    self._own_root = True
                    self._pump = True
                    self._root.withdraw()      # 只要字幕窗，不要那个空白主窗
                self._build_window(tk)
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] ❌ 建窗失败：{type(exc).__name__}: {exc}")
                self.available = False
                self.close()
                return False

        self._started = True
        self.available = True
        self._remember_cfg_mtime()
        self._find_game()
        self._apply_position()
        self._dirty = True
        self._redraw()                     # 先出一帧空面板：用户立刻能看见窗在哪、好不好拖
        print(f"[desktop] ✅ 桌面字幕已起来：mode={self.cfg.mode} anchor={self.cfg.anchor} "
              f"后端={'原生' if self._native is not None else 'Tk'} "
              f"面板={self.cfg.size_px[0]}x{self.cfg.size_px[1]} 透明度={self.cfg.alpha:.2f} "
              f"鼠标穿透={'开' if self.cfg.click_through else '关'} "
              f"跟随={'开' if self.cfg.follow else '关'} 位置={self.position}")
        return True


    def close(self) -> None:
        """销毁自己的窗口；自建的 root 一并销毁。重复调用不报错。"""
        with self._lock:
            self.available = False
            self._dragging, self._drag_from = False, None
            self._entries, self._latest = [], ("", "")
            self._epoch += 1
            self._content = self._last_sig = self._displayed_approval = None
            self._dirty = False
        native, self._native = self._native, None
        if native is not None:
            try:
                native.close()
            except Exception as exc:  # noqa: BLE001 — 关窗失败不值得打断退出流程
                print(f"[desktop] ⚠️ 销毁原生叠加窗时报错（已忽略）："
                      f"{type(exc).__name__}: {exc}")
        win, self._win = self._win, None
        self._label = None
        self._photo = None
        self._hwnd = 0
        was_started = self._started
        self._started = False
        self._pump = False
        if win is not None:
            try:
                win.destroy()
            except Exception as exc:  # noqa: BLE001 — 关窗失败不值得打断退出流程
                print(f"[desktop] ⚠️ 销毁字幕窗时报错（已忽略）：{type(exc).__name__}: {exc}")
        if self._own_root and self._root is not None:
            root, self._root = self._root, None
            try:
                root.destroy()
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] ⚠️ 销毁自建 Tk 主窗时报错（已忽略）：{type(exc).__name__}: {exc}")
        self._own_root = False
        if was_started:
            print(f"[desktop] 已关闭桌面字幕窗（共出图 {self._frames} 帧）")

    # ---------- 内容 ----------
    def update(self, text: str, source: str = "", force: bool = False) -> None:
        """只显示最新一句（歌词式）。`mode="conversation"` 时当作"别人刚说的一句"。"""
        epoch = self._epoch
        text = (text or "").strip()
        source = (source or "").strip()
        if not text or not allowed(current()):
            return                                   # 空内容不清屏：宁可留着上一句
        self._touch(force, (text, source), (("theirs", source, text),), epoch)

    def update_entries(self, entries: Any, force: bool = False) -> None:
        """刷新成对话视图：entries = [(who, source, translation[, label]), ...]，**最后一条最新**。

        `label` = 说话人昵称（房间里的远端成员才有），小字画在译文上方、与手腕屏一致。
        ⚠️ 必须走 `_unpack_entry`：**只解 3 个会把 4 元组整条丢掉**（原来那个 `try: who, source,
        text = item` 就是这么写的）—— 表现为「房间里别人说的话在桌面字幕上凭空消失」，
        而手腕屏那条腿好好的（它一直收 4 元组）。两条腿共用同一份 `render_conversation`，
        形状也必须共用同一套口径。
        """
        epoch = self._epoch
        rows: list[tuple[str, str, str, str]] = []
        for item in (entries or []):
            who, source, text, label = _unpack_entry(item)
            text = (text or "").strip()
            if not text:
                continue                             # 空内容丢掉（但不清屏）
            rows.append((str(who or "theirs"), (source or "").strip(), text,
                         (label or "").strip()))
        if not rows:
            return                                   # 空内容不清屏
        self._touch(force, (rows[-1][2], rows[-1][1]), tuple(rows), epoch)

    def _touch(self, force: bool, latest: tuple, entries: tuple, epoch: int) -> None:
        approval = current()
        content = (latest, entries, approval)
        view = self._view(content)
        if not view[1]:
            return
        with self._lock:
            if epoch != self._epoch or not self.available:
                return
            self._content = content
            self._latest, self._entries = latest, list(entries)
            sig, last = self._signature(view), self._last_sig
            if last is not None and sig == last[0] and same(view[2], last[1]) and not force:
                return                               # 同一段内容没变：不重复出图
            self._last_sig = (sig, view[2])
            self._dirty = True
        if self.dry_run:
            self._redraw()                           # dry-run 没有 tick 驱动 → 立刻落盘

    def _signature(self, content: Any = None) -> tuple:
        latest, entries = content[:2] if content is not None else (self._latest, self._entries)
        if self.cfg.mode == "latest":
            return ("latest", latest)
        return ("conversation", tuple(entries))

    def _view(self, content):
        if content is None:
            return self._latest, tuple(self._entries), None
        rows, approval = select_rows(content[1], content[2], self.cfg.mode == "latest")
        latest = (rows[-1][2], rows[-1][1]) if rows else ("", "")
        return latest, rows, approval

    def _retry(self, key, epoch):
        with self._lock:
            if epoch == self._epoch and self._last_sig is key:
                self._last_sig = None

    def _render(self, content: Any = None) -> Image.Image:
        """复用**手腕屏那套**渲染（不另写画字逻辑）。"""
        vcfg = self.cfg.visual_config()
        latest, entries = content[:2] if content is not None else (self._latest, self._entries)
        if self.cfg.mode == "latest":
            text, source = latest
            return render_panel(text, source, vcfg)
        return render_conversation(list(entries), vcfg)

    def _composite(self, panel: Image.Image) -> Image.Image:
        """把 RGBA 面板贴到**色键同色**的 RGB 底上。

        Tk 的 `-transparentcolor` 只认 RGB 底色：圆角外那一圈 (1,2,3) 才是真透明的地方。
        半透明底板 (12,14,20,205) 叠上去得到 (10,12,17)，肉眼与深色底板一致（主控实测）。
        """
        base = Image.new("RGB", panel.size, KEY_RGB)
        base.paste(panel, (0, 0), panel)
        return base

    def _redraw(self) -> None:
        with self._lock:
            self._dirty = False
            epoch, content = self._epoch, self._content
            key, mode = self._last_sig, self.cfg.mode
        view = self._view(content)
        approval = view[2]
        if content is not None and not view[1]:
            return
        if epoch != self._epoch or not allowed(approval):
            return
        try:
            panel = self._render(view)
        except Exception as exc:  # noqa: BLE001 — 出图失败不许打断翻译腿，保留上一帧
            print(f"[desktop] ⚠️ 出图失败（保留上一帧）：{type(exc).__name__}: {exc}")
            self._retry(key, epoch)
            return
        if epoch != self._epoch or mode != self.cfg.mode or not allowed(approval):
            return
        native = self._native
        if native is not None:
            try:
                with scope(approval):
                    native.set_panel(panel, self.cfg.alpha)
                with self._lock:
                    if epoch != self._epoch or self._native is not native:
                        return
                    self._displayed_approval = approval
                    self._frames += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] ⚠️ 原生窗贴图失败（保留上一帧）："
                      f"{type(exc).__name__}: {exc}")
                self._retry(key, epoch)
            return
        frame = self._composite(panel)
        if epoch != self._epoch or mode != self.cfg.mode or not allowed(approval):
            return
        if self.dry_run:
            path = self._frames_dir / f"frame_{self._frames + 1:03d}.png"
            try:
                self._frames_dir.mkdir(parents=True, exist_ok=True)
                frame.save(path)
            except OSError as exc:
                print(f"[desktop] ❌ [dry-run] 写帧失败 {path}：{exc}")
                return
            self._frames += 1
            print(f"[desktop][dry-run] 第 {self._frames} 帧 → {path.name}"
                  f"（{frame.size[0]}x{frame.size[1]}）")
            return
        with scope(approval):
            drawn = self._blit(frame)
        if drawn:
            with self._lock:
                if epoch != self._epoch:
                    return
                self._frames += 1
            if self._need_shape_mask:
                self._apply_shape_mask(panel)
        else:
            self._retry(key, epoch)


    def _blit(self, frame: Image.Image) -> bool:
        approval = current()
        epoch = self._epoch
        win, label = self._win, self._label
        if self._win is None or self._label is None or not allowed(approval):
            return False
        try:
            from PIL import ImageTk

            # ⚠️ master 要给：进程里可能同时存在 GUI 的 root 与我们自建的 root，
            #    不指定的话 PhotoImage 会挂到"默认 root"上（可能已经被销毁）。
            photo = ImageTk.PhotoImage(frame, master=win)
            if epoch != self._epoch or not allowed(approval):
                return False
            label.configure(image=photo)
            with self._lock:
                if epoch != self._epoch or self._win is not win or self._label is not label:
                    return False
                self._photo = photo
                self._displayed_approval = approval
            self._apply_alpha_only()
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 贴图失败（保留上一帧）：{type(exc).__name__}: {exc}")
            return False
        return epoch == self._epoch and self._win is win and self._label is label

    # ---------- 周期任务 ----------
    def tick(self) -> None:
        """跟随目标窗口 + 配置热重载 + 处理待重绘。没起窗时直接 return。"""
        if not self._started:
            return
        if self.dry_run:
            if self._dirty:
                self._redraw()
            return
        if not allowed(self._displayed_approval):
            self._apply_alpha_only()
            if not self._started:
                return
            self._dirty = True
        native = self._native
        if native is not None:
            try:
                self._reload_config()
                if not self._started or self._native is not native:
                    return
                self._follow_game()
                if not self._started or self._native is not native:
                    return
                native.tick()
                if getattr(native, 'available', True) is False:
                    self.close()
                    return
            except Exception as exc:
                print(f"[desktop] ⚠️ 原生窗轮询失败：{type(exc).__name__} → 停用字幕窗")
                self.close()
                return
            if not self._started or self._native is not native:
                return
            if self._dirty:
                self._redraw()
            return
        if self._win is None:
            return
        self._reload_config()
        self._follow_game()
        if self._exstyles_dirty:
            # Tk 改 -alpha / 首次映射都会**延迟**重写扩展样式（把鼠标穿透冲掉）：
            # 改完立刻补一次会被它盖掉，所以留个脏标志，下一跳（GUI 50ms 一跳）再补。
            self._exstyles_dirty = False
            self._apply_exstyles()
        if self._dirty:
            self._redraw()
        if self._pump and self._root is not None:
            try:
                self._root.update()          # 自建 root：没人跑主循环，这里泵一下
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] ⚠️ 泵 Tk 事件失败：{type(exc).__name__}: {exc}")


    # ---------- 诊断 ----------


    @property
    def frame_count(self) -> int:
        """成功出图的帧数（单测/诊断用）。"""
        return self._frames


def _demo() -> None:
    """离线渲染对话样例（不建窗口、不碰 VRChat）。

    `python -m vlt.output.desktop_overlay --demo --out out/desktop_frames`
    """
    import argparse

    ap = argparse.ArgumentParser(description="离线渲染桌面字幕（不建窗口）")
    ap.add_argument("--demo", action="store_true", help="渲染样例帧（本入口的唯一用途）")
    ap.add_argument("--out", default=str(APP_DIR / "out" / "desktop_frames"),
                    help="PNG 落盘目录")
    ap.add_argument("--mode", default="conversation", choices=MODES,
                    help="conversation=镜像聊天区；latest=只显示最新一句")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=360)
    args = ap.parse_args()

    entries = [
        ("theirs", "Hey! Are you heading to the mirror tonight?",
         "嘿！你今晚要去镜子那边吗？"),
        ("mine", "当然，八点见，我先把字幕窗摆好。",
         "Sure, see you at eight — let me put my subtitle window in place first."),
        ("theirs", "こんにちは、はじめまして！日本語でも大丈夫？",
         "你好，初次见面！用日语也没问题吗？"),
    ]
    cfg = DesktopOverlayConfig(
        enabled=True, mode=args.mode,
        size_px=(max(64, int(args.width)), max(64, int(args.height))))
    ov = DesktopOverlay(cfg, dry_run=True)
    ov._frames_dir = Path(args.out)        # --out 指定落盘目录（默认在可写目录的 out/ 下）
    if not ov.start():
        raise SystemExit(1)
    for i in range(1, len(entries) + 1):   # 1 条 / 2 条 / 3 条：顺便看看塞满时的排版
        ov.update_entries(entries[:i], force=True)
    print(f"[desktop] demo 完成：{ov.frame_count} 帧 → {ov._frames_dir}"
          f"（mode={args.mode}，面板 {cfg.size_px[0]}x{cfg.size_px[1]}，色键底 {TRANSPARENT_KEY}）")


if __name__ == "__main__":
    _demo()
