"""Desktop window controls and configuration reload through the platform facade."""
from __future__ import annotations
import time
from typing import Any
from PIL import Image
from ..platform.overlay_pixels import alpha_mask_bits
from ..session.chatgpt_output import allowed
from .desktop_model import DesktopOverlayConfig, best_anchor, clamp_alpha, resolve_position, TRANSPARENT_KEY


class _DesktopWindowControls:
    def _apply_shape_mask(self, panel: Image.Image) -> None:
        """Tk 回落路径的 Linux 补丁：把面板 alpha 转 1 位形状蒙版，抠掉键色底。

        Windows 的 Tk 有 `-transparentcolor` 色键；Linux 的 Tk 没有 → 每帧用 X Shape
        抠形（面板圆角外那圈键色底不再是不透明矩形；圆角本身是 1 位的、会有锯齿）。
        **逐像素半透明做不到** —— 那是原生窗（Wayland layer-shell / X11 ARGB）的事。
        尺寸变了蒙版要跟着重设 —— 每帧都重设，天然覆盖（set_window_shape 口径见此）。
        """
        if not self._hwnd or self._win is None:
            return
        from ..platform import set_window_shape
        try:
            mask = alpha_mask_bits(panel)
            ok = set_window_shape(self._hwnd, mask, panel.size[0], panel.size[1])
        except Exception as exc:  # noqa: BLE001 — 抠形失败不许打断出图
            if not self._shape_warned:
                self._shape_warned = True
                print(f"[desktop] ⚠️ 形状蒙版失败（字幕仍可用，圆角外会是不透明底）："
                      f"{type(exc).__name__}: {exc}")
            return
        if not ok and not self._shape_warned:
            self._shape_warned = True
            print("[desktop] ⚠️ 形状蒙版没设上（本平台可能不支持）"
                  " → 圆角外会是不透明键色底")

    def _create_native_window(self) -> Any:
        """问门面要原生叠加窗（Linux：Wayland layer-shell / X11 ARGB 覆盖窗）；拿不到返回 None。

        建不起来的原因由后端自己打日志（`vlt/platform/linux.py`）；这里只兜异常。
        """
        from ..platform import create_desktop_window
        try:
            return create_desktop_window(
                size=tuple(self.cfg.size_px),
                alpha=self.cfg.alpha,
                click_through=self.cfg.click_through,
                on_drag_end=self._native_drag_end,
                backend=self.cfg.backend,
            )
        except Exception as exc:  # noqa: BLE001 — 原生窗失败不许带崩界面
            print(f"[desktop] ⚠️ 原生窗创建异常（回落 Tk）：{type(exc).__name__}: {exc}")
            return None


    def _native_drag_end(self, x: int, y: int) -> None:
        """原生窗拖动落点：与 Tk 的 `_on_drag_end` 走同一套「折算成锚点+偏移」逻辑。"""
        self.user_pos = (int(x), int(y))
        changed = self.snap_to_config(self.user_pos)
        if changed:
            print(f"[desktop] 拖动落点 {self.user_pos} → 折算成配置 {changed}"
                  f"（点「锁定位置」后写回 config.yaml）")


    def _build_window(self, tk: Any) -> None:
        """无边框 + 置顶 + 色键透明 + 鼠标穿透。每一步失败都留一行日志再降级。"""
        from PIL import ImageTk  # noqa: F401  — 早失败：出图口不在就现在说清楚

        win = tk.Toplevel(self._root)
        self._win = win
        win.overrideredirect(True)                     # 无边框（不要标题栏那条）
        win.configure(bg=TRANSPARENT_KEY)
        w, h = (int(v) for v in self.cfg.size_px)
        win.geometry(f"{w}x{h}+{int(self.cfg.pos[0])}+{int(self.cfg.pos[1])}")
        # 三个属性各自 try：某个平台不吃其中一个时，其余的仍然生效（不要一坏全坏）
        for name, value in (("-topmost", True),
                            ("-alpha", clamp_alpha(self.cfg.alpha)),
                            ("-transparentcolor", TRANSPARENT_KEY)):
            try:
                win.attributes(name, value)
            except tk.TclError as exc:
                if name == "-transparentcolor":
                    # Linux 的 Tk 没有色键 → 记下来，之后每帧用 1 位形状蒙版抠底
                    self._need_shape_mask = True
                    hint = "改用 1 位形状蒙版抠底（不行的话底板会是不透明的一块）"
                else:
                    hint = "这一项按系统默认"
                print(f"[desktop] ⚠️ 窗口属性 {name} 没吃下（{exc}）→ {hint}，字幕仍可用")
        label = tk.Label(win, bd=0, highlightthickness=0, bg=TRANSPARENT_KEY)
        label.pack(fill="both", expand=True)
        self._label = label
        # update_idletasks（不是 update）：让 HWND 真的建出来、几何真的落下去，
        # 但不处理事件队列 —— GUI 模式下主循环正在跑，抢事件会重入。
        win.update_idletasks()
        self._resolve_hwnd()
        self._apply_exstyles()
        self._bind_drag()
        # Tk 首次映射时还会再写一次扩展样式（把穿透冲掉）→ 第一跳 tick 补回来
        self._exstyles_dirty = True


    def _resolve_hwnd(self) -> int:
        """Tk 给的是子窗口句柄，顶层要再问一次平台门面。"""
        if self._win is None:
            return 0
        from ..platform import top_level_hwnd
        try:
            self._hwnd = int(top_level_hwnd(int(self._win.winfo_id())))
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 取窗口句柄失败（{type(exc).__name__}: {exc}）"
                  f" → 鼠标穿透/不抢焦点设不上")
            self._hwnd = 0
        return self._hwnd


    def _apply_exstyles(self) -> bool:
        """鼠标穿透 + 不抢焦点 + 不进 alt-tab（能力在平台门面上，缺就降级并留痕）。"""
        if self.dry_run:
            return True
        want = bool(self.cfg.click_through) and not self._dragging
        if self._native is not None:
            # 原生 Wayland 窗：键盘交互在建面时就是 none（不抢焦点）；穿透 = 输入区置空。
            self._native.set_click_through(want)
            return True
        if self._win is None:
            return False
        from ..platform import set_click_through, set_tool_window
        if not self._hwnd:
            self._resolve_hwnd()
        if not self._hwnd:
            return False
        if not set_tool_window(self._hwnd):
            print("[desktop] ⚠️ 「不抢焦点/不进任务栏」没设上（本平台可能不支持）"
                  " → 点字幕可能把焦点从游戏里抢走")
        if not set_click_through(self._hwnd, want):
            print(f"[desktop] ⚠️ 鼠标穿透（{'开' if want else '关'}）没设上"
                  f"（本平台可能不支持） → 字幕会挡住鼠标")
            return False
        return True


    def _work_area(self) -> tuple[int, int, int, int]:
        """夹取用的屏幕可用区域。

        **已贴到目标窗口**就用「那块显示器」的工作区：主屏工作区只有主屏那一份，
        拿它夹副屏上的字幕会把字幕拽回主屏（左侧副屏坐标是负的，拽得尤其明显）。
        没有目标窗口（自由模式 / 找不到窗口）才沿用主屏工作区。
        """
        from ..platform import monitor_work_area, screen_work_area
        try:
            raw = (monitor_work_area(self._game_hwnd) if self._game_hwnd
                   else screen_work_area())
            area = tuple(int(v) for v in raw)
            left, top, right, bottom = area            # type: ignore[misc]
            if right > left and bottom > top:
                return (left, top, right, bottom)      # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            if not self._area_fallback_logged:
                self._area_fallback_logged = True
                print(f"[desktop] ⚠️ 取屏幕工作区失败（{type(exc).__name__}: {exc}）"
                      f" → 按 1920x1080 兜底")
        return (0, 0, 1920, 1080)


    def _apply_position(self) -> None:
        w, h = (int(v) for v in self.cfg.size_px)
        x, y = resolve_position(self.cfg, self._game_rect, (w, h), self._work_area())
        if self._native is not None:
            try:
                self._native.move(x, y)
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] ⚠️ 移动原生窗失败：{type(exc).__name__}: {exc}")
            return
        if self._win is None:
            return
        try:
            # ⚠️ 偏移必须写成 "+x+y"：Tk 把 "-50" 当成"距右边 50"，只有 "+-50" 才是
            #    绝对坐标 -50（多屏时副屏在左侧，坐标是负的）。
            self._win.geometry(f"{w}x{h}+{x}+{y}")
            self._win.update_idletasks()
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 移动窗口失败：{type(exc).__name__}: {exc}")


    def _own_hwnds(self) -> tuple[int, ...]:
        """本程序**自己**的窗口句柄（主界面 + 字幕窗）：找游戏窗口时必须排除。

        主界面标题是「VRChat 实时同传」（英文界面是 vrchat-livetranslate），也含
        "VRChat" —— 不排除的话，VRChat 没起来时字幕会贴到我们自己的界面上
        （截图里看得一清二楚：字幕贴在程序窗口下沿）。实测踩过。
        """
        out: list[int] = []
        if self._hwnd:
            out.append(int(self._hwnd))
        if self._root is not None:
            try:
                from ..platform import top_level_hwnd
                out.append(int(top_level_hwnd(int(self._root.winfo_id()))))
            except Exception:  # noqa: BLE001 — 拿不到就少排一个，不影响主流程
                pass
        return tuple(out)


    def _find_game(self, *, quiet_missing: bool = False) -> bool:
        """找目标窗口并记下它的客户区。找不到不算错（退化成绝对定位），但要留一行。"""
        if not self.cfg.attach_to_game:
            self._game_hwnd = None
            self._game_rect = None
            return False
        from ..platform import find_game_window, window_client_rect
        hwnd = find_game_window(self.cfg.game_title, self._own_hwnds())
        if not hwnd:
            self._game_hwnd = None
            self._game_rect = None
            self._next_search = time.monotonic() + self.SEARCH_RETRY_S
            if not quiet_missing:
                print(f"[desktop] ⚠️ 没找到窗口 {self.cfg.game_title!r}"
                      f" → 退化成屏幕绝对定位 {tuple(self.cfg.pos)}"
                      f"（{self.SEARCH_RETRY_S:.0f}s 后重试）")
            return False
        self._game_hwnd = int(hwnd)
        self._game_rect = window_client_rect(self._game_hwnd)
        if self._game_rect is None:
            print(f"[desktop] ⚠️ 找到窗口 {self.cfg.game_title!r}"
                  f"（hwnd=0x{self._game_hwnd:X}）但取不到客户区（最小化？）"
                  f" → 先用屏幕绝对定位")
            return False
        print(f"[desktop] ✅ 已贴到 {self.cfg.game_title!r}"
              f"（hwnd=0x{self._game_hwnd:X}，客户区 {self._game_rect}）")
        return True


    def _follow_game(self) -> None:
        """目标窗口移动/缩放/关闭时跟上；句柄失效就重新找。"""
        if not self.cfg.attach_to_game:
            return
        if self._dragging or self._drag_from is not None:
            return                       # 用户正在拖：别跟他抢窗口位置
        from ..platform import is_window, window_client_rect
        if self._game_hwnd is not None and not is_window(self._game_hwnd):
            print(f"[desktop] ⚠️ 目标窗口已关闭（hwnd=0x{self._game_hwnd:X}）"
                  f" → 重新查找 {self.cfg.game_title!r}")
            self._game_hwnd = None
            self._game_rect = None
        if self._game_hwnd is None:
            if time.monotonic() >= self._next_search:
                if self._find_game(quiet_missing=True) and self.cfg.follow:
                    self._apply_position()
            return
        rect = window_client_rect(self._game_hwnd)
        if rect is None:
            if not self._rect_missing_logged:
                self._rect_missing_logged = True
                print("[desktop] ⚠️ 目标窗口取不到客户区（最小化/还在加载？）→ 字幕停在原位")
            return
        self._rect_missing_logged = False
        if tuple(rect) == self._game_rect:
            return
        self._game_rect = tuple(rect)
        if self.cfg.follow:
            self._apply_position()


    def set_draggable(self, on: bool) -> None:
        """True = 临时关掉鼠标穿透，好让用户把字幕拖到想要的位置。"""
        on = bool(on)
        self._dragging = on
        if self.dry_run:
            return
        try:
            if self._native is not None:
                self._native.set_draggable(on)
            elif self._win is not None:
                if not self._apply_exstyles():
                    raise RuntimeError('desktop click-through change failed')
            else:
                return
        except Exception:
            self.close()
            raise
        restored = "开" if (self.cfg.click_through and not on) else "关"
        print(f"[desktop] {'已解锁拖动（鼠标穿透临时关闭）' if on else '已锁定位置'}"
              f" → 鼠标穿透={restored}")


    def _bind_drag(self) -> None:
        if self._win is None:
            return
        for widget in (self._win, self._label):
            if widget is None:
                continue
            widget.bind("<Button-1>", self._on_drag_start, add="+")
            widget.bind("<B1-Motion>", self._on_drag_move, add="+")
            widget.bind("<ButtonRelease-1>", self._on_drag_end, add="+")


    def _on_drag_start(self, ev: Any) -> None:
        if not self._dragging or self._win is None:
            return
        try:
            self._drag_from = (int(ev.x_root) - int(self._win.winfo_x()),
                               int(ev.y_root) - int(self._win.winfo_y()))
        except Exception:  # noqa: BLE001
            self._drag_from = None


    def _on_drag_move(self, ev: Any) -> None:
        if self._drag_from is None or self._win is None:
            return
        x = int(ev.x_root) - self._drag_from[0]
        y = int(ev.y_root) - self._drag_from[1]
        try:
            # 「+x+y」两段都要带加号：Tk 把 "-50" 读成「距右边 50」，只有 "+-50"
            # 才是绝对坐标 -50（多屏时副屏在左侧，坐标真的是负的）。
            self._win.geometry(f"+{x}+{y}")
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 拖动窗口失败：{type(exc).__name__}: {exc}")


    def _on_drag_end(self, _ev: Any) -> None:
        if self._drag_from is None or self._win is None:
            return
        self._drag_from = None
        try:
            pos = (int(self._win.winfo_x()), int(self._win.winfo_y()))
        except Exception:  # noqa: BLE001
            return
        self.user_pos = pos
        changed = self.snap_to_config(pos)
        if changed:
            print(f"[desktop] 拖动落点 {pos} → 折算成配置 {changed}"
                  f"（点「锁定位置」后写回 config.yaml）")


    def snap_to_config(self, pos: tuple[int, int] | None = None) -> dict[str, Any]:
        """把用户拖到的位置折算成配置键（界面拿它写回 config.yaml）。

        贴窗时**必须**折算成 `(anchor, offset)`：存死坐标的话，VRChat 窗口一移动 /
        改分辨率，字幕就与它脱钩了。没贴窗（`attach_to_game=False` 或找不到窗口）
        才直接存 `pos` 的屏幕绝对坐标。折算结果同时写回 `self.cfg` —— 否则下一跳
        「跟随」会按旧锚点把窗口弹回原处。
        """
        target = pos or self.user_pos
        if target is None:
            return {}
        w, h = (int(v) for v in self.cfg.size_px)
        if self.cfg.attach_to_game and self._game_rect is not None:
            anchor, offset = best_anchor(target, self._game_rect, (w, h))
            self.cfg.anchor, self.cfg.offset = anchor, offset
            return {"anchor": anchor, "offset": [int(offset[0]), int(offset[1])]}
        self.cfg.pos = (int(target[0]), int(target[1]))
        return {"pos": [int(target[0]), int(target[1])]}


    def set_alpha(self, alpha: Any) -> None:
        """整窗透明度（用户要的「可改透明度」）。夹到 0.2~1.0。"""
        self.cfg.alpha = clamp_alpha(alpha)
        if self.dry_run:
            return
        if self._native is not None:
            self._apply_alpha_only()
            return
        if self._win is None:
            return
        try:
            self._win.attributes("-alpha", self._visible_alpha())
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 设置透明度 {self.cfg.alpha:.2f} 失败："
                  f"{type(exc).__name__}: {exc}")
            if not allowed(self._displayed_approval):
                self.close()
            return
        # Tk 改 -alpha 会重写窗口扩展样式 → 穿透/工具窗要补回去，否则拖一次滑块就漏鼠标。
        # 立刻补一次 + 下一跳再补一次（Tk 的重写是延迟到事件循环里的）。
        self._apply_exstyles()
        self._exstyles_dirty = True


    def _remember_cfg_mtime(self) -> None:
        if self.config_path is None:
            return
        try:
            self._last_cfg_mtime = self.config_path.stat().st_mtime
        except OSError:
            self._last_cfg_mtime = 0.0


    def _reload_config(self) -> None:
        """config.yaml 存盘即生效（位置/尺寸/透明度/字号改完不用重启）。"""
        epoch = self._epoch
        path = self.config_path
        if path is None:
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return                                   # 文件被删/暂时读不到：保留旧配置
        if mtime == self._last_cfg_mtime:
            return
        self._last_cfg_mtime = mtime
        try:
            import yaml

            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            # ⚠️ 只读本段：桌面字幕的配置与 `overlay:`（手腕屏）完全独立。
            new = DesktopOverlayConfig.from_dict(raw.get("desktop_overlay") or {})
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 热重载失败（保留旧配置）：{type(exc).__name__}: {exc}")
            return
        old, self.cfg = self.cfg, new
        if new.alpha != old.alpha:
            self._apply_alpha_only()
        if epoch != self._epoch:
            return
        if new.click_through != old.click_through:
            self._apply_exstyles()
        if epoch != self._epoch:
            return
        if new.attach_to_game and not old.attach_to_game:
            self._find_game()
        if not new.attach_to_game:
            self._game_hwnd = None
            self._game_rect = None
        if epoch != self._epoch:
            return
        if (new.size_px, new.anchor, new.offset, new.pos,
                new.attach_to_game) != (old.size_px, old.anchor, old.offset,
                                        old.pos, old.attach_to_game):
            if self._native is not None and new.size_px != old.size_px:
                try:
                    self._native.set_size(tuple(new.size_px))
                except Exception as exc:  # noqa: BLE001
                    print(f"[desktop] ⚠️ 原生窗改尺寸失败：{type(exc).__name__}: {exc}")
            if epoch != self._epoch:
                return
            self._apply_position()
        if epoch != self._epoch:
            return
        self._last_sig = None                        # 视觉参数可能变了 → 下一跳重新出图
        self._dirty = True
        print(f"[desktop] ♻️ 配置热重载：mode={new.mode} anchor={new.anchor} "
              f"offset={new.offset} 面板={new.size_px[0]}x{new.size_px[1]} "
              f"透明度={new.alpha:.2f} 字号={new.font_size}/{new.source_font_size} "
              f"行数={new.max_lines} 显示原文={'是' if new.show_source else '否'} "
              f"穿透={'开' if new.click_through else '关'} 跟随={'开' if new.follow else '关'}")


    def _visible_alpha(self) -> float:
        return self.cfg.alpha if allowed(self._displayed_approval) else 0.0

    def _apply_alpha_only(self) -> None:
        """热重载里改透明度用：与 set_alpha 的区别是**不动 cfg**（已经换过了）。"""
        if self._native is not None:
            try:
                self._native.set_alpha(self._visible_alpha())
            except Exception as exc:
                print(f"[desktop] ⚠️ 原生窗透明度更新失败：{type(exc).__name__} → 停用字幕窗")
                self.close()
            return
        if self._win is None:
            return
        try:
            self._win.attributes("-alpha", self._visible_alpha())
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 热重载设置透明度失败：{type(exc).__name__}: {exc}")
            if not allowed(self._displayed_approval):
                self.close()
            return
        self._apply_exstyles()
        self._exstyles_dirty = True


    @property
    def position(self) -> tuple[int, int]:
        """当前窗口的屏幕坐标；没起窗时返回按配置推算的位置。"""
        if self._native is not None:
            try:
                return (int(self._native.position[0]), int(self._native.position[1]))
            except Exception:  # noqa: BLE001 — 窗口正在销毁：退回推算值
                pass
        if self._win is not None:
            try:
                return (int(self._win.winfo_x()), int(self._win.winfo_y()))
            except Exception:  # noqa: BLE001 — 窗口正在销毁：退回推算值
                pass
        return resolve_position(self.cfg, self._game_rect,
                                tuple(int(v) for v in self.cfg.size_px), self._work_area())


    @property
    def game_rect(self) -> tuple[int, int, int, int] | None:
        """最近一次找到的目标窗口客户区；没有则 None。"""
        return self._game_rect
