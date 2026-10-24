"""Wrist overlay health checks and configuration refresh; no native imports."""
import time
from .overlay import OverlayConfig
from ..session.chatgpt_output import allowed, scope


class _WristMaintenance:
    def _repaint(self) -> None:
        content = self._content
        if content is not None and content[3] == self._epoch and allowed(content[2]):
            with scope(content[2]):
                if content[0] == 'update':
                    self.update(*content[1], force=True)
                else:
                    self.update_entries(*content[1], force=True)

    def _set_alpha(self, alpha: float) -> None:
        self._alpha_retry = True
        if not allowed(self._uploaded_approval):
            alpha = 0.0
        self._overlay.setOverlayAlpha(self._handle, alpha)
        self._alpha_retry = False

    def _overlay_exists(self) -> bool | None:
        """我们那把 key 还在 SteamVR 里吗？None = 判断不了。

        这行决定「面板消失」的两种可能里到底是哪种：
        - 还在   → 面板只是**卡在最后一帧**（上传被拒，对象本身没死）
        - 已不在 → 面板是**真被 SteamVR 丢掉了**（合成器重启/被清理），必须重建
        """
        if self._overlay is None or self._handle is None:
            return None
        try:
            self._overlay.findOverlay(self.cfg.overlay_key)
            return True
        except Exception as exc:  # noqa: BLE001
            name = f"{type(exc).__name__}{exc}"
            if "UnknownOverlay" in name:
                return False
            return None


    def _visible_str(self) -> str:
        if self._overlay is None or self._handle is None:
            return "?"
        try:
            return "是" if self._overlay.isOverlayVisible(self._handle) else "否"
        except Exception:  # noqa: BLE001
            return "?"


    def _state_brief(self) -> str:
        """一行现场信息，故障时决定下一步查什么（永不抛异常）。"""
        age = time.monotonic() - self._last_ok_at if self._last_ok_at else -1.0
        exists = self._overlay_exists()
        mode = '持久贴图' if self._d3d is not None else ('兼容旧路' if not self._tex_disabled
                                                       else '兼容旧路(永久)')
        bits = [
            f"面板={'可用' if self.available else '不可用'}",
            f"上传路={mode}",
            f"最后成功上传 {age:.0f}s 前" if age >= 0 else "还没成功上传过",
            f"帧数={self.frames_updated}",
            {True: "overlay在", False: "overlay已被丢弃", None: "overlay状态未知"}[exists],
            f"可见={self._visible_str()}",
            f"HMD在线={self._hmd_present()}",
            f"重建={self._rebuilds}/硬重启={self._reinits}",
        ]
        return " | ".join(bits)


    def _log_heartbeat(self) -> None:
        """每 HEARTBEAT_S 秒一行状态 —— 「面板隔一阵就消失」全靠这行定位。"""
        print(f"[overlay][diag] 心跳：{self._state_brief()}")
        if self._overlay_exists() is False:
            print("[overlay] ⚠️ SteamVR 里已经没有这把 key 了 —— 面板是**真消失**"
                  "（不是卡在最后一帧）→ 重建 overlay")
            self._recreate_overlay()
        elif self._visible_str() == "否" and allowed(self._uploaded_approval):
            # 对象还在、只是被藏起来了（SteamVR 在某些界面切换里会这么做）——
            # 重新 show 一次就好，不必重建。
            print("[overlay] ⚠️ 面板存在但**不可见**（被 SteamVR 藏起来了）→ 重新 showOverlay")
            try:
                self._overlay.showOverlay(self._handle)
            except Exception as exc:  # noqa: BLE001
                print(f"[overlay]   showOverlay 失败：{type(exc).__name__}: {exc}")


    def tick(self) -> None:
        # 配置热重载（位置/尺寸改完存盘即生效，无需重启）
        if self.dry_run or self._closed:
            return
        self._maybe_apply_anchor()
        self._maybe_retry_tex_path()
        self._maybe_reinit()
        if self.config_path and self.config_path.exists():
            mtime = self.config_path.stat().st_mtime
            if mtime != self._last_cfg_mtime:
                self._last_cfg_mtime = mtime
                try:
                    import yaml

                    raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                    new_cfg = OverlayConfig.from_dict(raw.get("overlay") or {})
                    # 弯曲与锚点也要参与比对：少了它们，界面上拖动弯曲滑块、
                    # 或切换锚点（左手/右手/tracker）时热重载会「静默不生效」。
                    geo_changed = (new_cfg.pos, new_cfg.rot, new_cfg.width_m, new_cfg.alpha,
                                   new_cfg.curvature) != (self.cfg.pos, self.cfg.rot,
                                                          self.cfg.width_m, self.cfg.alpha,
                                                          self.cfg.curvature)
                    anchor_changed = (new_cfg.anchor != self.cfg.anchor
                                      or new_cfg.tracker_index != self.cfg.tracker_index)
                    # 影响**贴图内容**的参数（字号 / 面板像素尺寸 / 行数 / 是否显示原文 /
                    # 底板与原文的不透明度 / 配色 / 分隔线）：这些改了只重应用变换是不够的，
                    # 必须重新渲染一帧，否则界面上拖字号或透明度滑块会「看着生效、屏上没变」。
                    # ⚠️ 口径要与 Linux 侧 `openxr_overlay.should_rebuild()` 一致 ——
                    #    同一个 config.yaml 两个平台的热重载行为必须一样。
                    render_changed = (new_cfg.font_size != self.cfg.font_size
                                      or new_cfg.source_font_size != self.cfg.source_font_size
                                      or new_cfg.size_px != self.cfg.size_px
                                      or new_cfg.max_lines != self.cfg.max_lines
                                      or new_cfg.show_source != self.cfg.show_source
                                      or new_cfg.bg_alpha != self.cfg.bg_alpha
                                      or new_cfg.source_alpha != self.cfg.source_alpha
                                      or new_cfg.border_alpha != self.cfg.border_alpha
                                      or new_cfg.separator != self.cfg.separator
                                      or new_cfg.color_bg != self.cfg.color_bg
                                      or new_cfg.color_border != self.cfg.color_border
                                      or new_cfg.color_source != self.cfg.color_source
                                      or new_cfg.color_translation != self.cfg.color_translation
                                      or new_cfg.color_mine != self.cfg.color_mine
                                      or new_cfg.color_theirs != self.cfg.color_theirs)
                    # 淡出阈值只影响每帧算出来的 overlay alpha，不改几何也不改贴图 ——
                    # 但配置对象要换掉，否则改 fade_after_s 存盘不生效。
                    fade_changed = new_cfg.fade_after_s != self.cfg.fade_after_s
                    size_changed = new_cfg.size_px != self.cfg.size_px
                    if geo_changed or anchor_changed or render_changed or fade_changed:
                        self.cfg = new_cfg
                        if self.available:
                            if anchor_changed:
                                self._device = self._resolve_anchor()
                            if geo_changed or anchor_changed:
                                self._apply_transform()
                            if render_changed:
                                if size_changed and self._d3d is not None:
                                    self._rebuild_d3d('size_px 变更')
                                self._repaint()
                        print(f"[overlay] ♻️ 配置热重载：anchor={new_cfg.anchor} pos={new_cfg.pos} "
                              f"rot={new_cfg.rot} width={new_cfg.width_m}m "
                              f"curvature={new_cfg.curvature} alpha={new_cfg.alpha} "
                              f"字号={new_cfg.font_size}/{new_cfg.source_font_size} "
                              f"面板={new_cfg.size_px[0]}x{new_cfg.size_px[1]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[overlay] ⚠️ 热重载失败（保留旧配置）：{exc}")

        # 健康心跳：定期把「面板还活着吗」写进日志，并在 overlay 已被 SteamVR 丢掉时重建。
        # 放在热重载与淡出之前——它是最该先出结果的一条诊断。
        if not self._closed:
            now = time.monotonic()
            if now - self._last_heartbeat >= self.HEARTBEAT_S:
                self._last_heartbeat = now
                try:
                    self._log_heartbeat()
                except Exception as exc:  # noqa: BLE001
                    print(f"[overlay] ⚠️ 心跳失败（不影响翻译）：{type(exc).__name__}: {exc}")

        # 已提交的旧贴图也不能在健康检查或淡出刷新时重新显现。
        if self.available and not allowed(self._uploaded_approval):
            try:
                self._set_alpha(0.0)
            except Exception:
                pass
            return

        # 可选淡出
        if self.available and (self._alpha_retry or (self.cfg.fade_after_s > 0 and self._last_text_at)):
            faded = (self.cfg.fade_after_s > 0 and self._last_text_at
                     and time.monotonic() - self._last_text_at > self.cfg.fade_after_s)
            alpha = 0.0 if faded else self.cfg.alpha
            try:
                self._set_alpha(alpha)
            except Exception:
                pass
