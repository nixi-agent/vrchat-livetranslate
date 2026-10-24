"""OpenXR overlay maintenance, configuration and recovery."""
from __future__ import annotations
import logging
import time
from .overlay import OverlayConfig
from ..session.chatgpt_output import allowed, current, same
from .openxr_model import (
    CYLINDER_EXT,
    TRACKER_EXT,
    should_rebuild,
)

log = logging.getLogger("vlt.output.openxr_overlay")

class _XrMaintenance:
    def _update_content(self, kind, data, renderer, force, epoch=None):
        approval = current()
        epoch = self._epoch if epoch is None else epoch
        if not allowed(approval):
            return
        with self._img_lock:
            if epoch != self._epoch or self._frame_stop.is_set():
                return
            key = self._last_render if kind == 'latest' else self._last_entries
            if not force and key == data and same(approval, self._last_approval):
                return
            content = (kind, data, approval, epoch)
            self._content = content
            self._last_render = data if kind == 'latest' else None
            self._last_entries = data if kind == 'conversation' else None
            self._last_render_cached, self._last_entries_cached = self._last_render, self._last_entries
            self._last_approval = approval
        try:
            img = renderer(*data, self.cfg) if kind == 'latest' else renderer(list(data), self.cfg)
            if epoch != self._epoch or not allowed(approval):
                return
            if self.dry_run:
                self._write_demo(img)
            else:
                self._queue_frame(img, content)
        except Exception:
            with self._img_lock:
                if self._content is content:
                    self._last_render = self._last_entries = None
            raise

    def _layer_alpha(self) -> float:
        """当前该用的**整层** alpha（Linux 侧的 `setOverlayAlpha()`）。

        `fade_after_s > 0` 且静默超过它 → 直接归零（与 Windows 侧一致：是**消失**，
        不是渐变 —— 那边就是 `setOverlayAlpha(0.0)`）。
        """
        cfg = self.cfg
        if cfg.fade_after_s > 0 and self._last_content_at:
            if time.monotonic() - self._last_content_at > cfg.fade_after_s:
                return 0.0
        return max(0.0, min(1.0, float(cfg.alpha)))

    @staticmethod
    def _extensions(required: tuple[str, ...] = (
            "XR_EXTX_overlay", "XR_KHR_opengl_enable", "XR_MNDX_egl_enable")) -> list[str]:
        """挑出可用的扩展（运行时不支持的就不启用，免得 create_instance 直接失败）。

        `required` 由 GL 后端声明（`_GlBackend.REQUIRED_EXTENSIONS`）：Wayland 的
        EGL_MNDX 要 `XR_MNDX_egl_enable`，X11 的 XLIB 绑定只要 `XR_KHR_opengl_enable`。
        默认值保持 Wayland 那套（旧调用口径不变）。

        ⚠️ 枚举失败时**不猜**：只请求「没有它这条腿根本起不来」的必需项，不把可选的
        柱面扩展（`CYLINDER_EXT`）算进去。请求一个运行时没有的扩展会让 `create_instance`
        直接失败 —— 那是**整条手腕屏消失**，比「不弯」严重得多。柱面的能力判定改由
        `submit()` 按实际启用的扩展做，拿不到就退回平面（`effective_curvature()`）。

        `TRACKER_EXT` 同理：有就启用（tracker 锚点才有可能可用），没有就不请求 ——
        反正那族路径不存在，`_ensure_actions()` 会按启用列表把 tracker 整族跳过。
        """
        import xr
        required = list(required)
        try:
            have = set()
            for e in xr.enumerate_instance_extension_properties():
                n = e.extension_name
                have.add(n.decode() if isinstance(n, bytes) else str(n))
        except Exception:  # noqa: BLE001
            return list(required)            # 枚举不出来就只赌必需项，不赌柱面/tracker
        want = required + [CYLINDER_EXT, TRACKER_EXT]
        return [e for e in want if e in have]

    def _escalate(self) -> None:
        """分级自愈：连续失败到阈值就升一级（swapchain → session → instance）。

        ⚠️ 刻意限频：失败一次就重启 instance 等于自己把自己拖死。
        ⚠️ 本方法**不许**在动作后把 `_stage` 放回 "none"：那等于每轮都从第一级重来、
        永远升不上第二级。用户实测日志：面板尺寸拖大后连刷 232 次「已重建 swapchain」、
        0 次重建会话 —— 面板永久卡死，只能重启应用。梯子的回落只在帧循环的
        「真的恢复上传」分支里做（`_fails = 0` 那处）。
        """
        if self._fails_in_stage < 3:
            return
        self._fails_in_stage = 0
        if self._stage == "none":
            self._stage = "swapchain"
        elif self._stage == "swapchain":
            self._stage = "session"
        else:
            self._stage = "instance"
        try:
            if self._stage == "swapchain":
                self._sess.rebuild_swapchain()
                log.warning("[overlay:xr] ♻️ 已重建 swapchain")
            elif self._stage == "session":
                self._rebuilds += 1
                self._teardown(keep_gl=True)
                self._bring_up(rebuild_gl=False)
                log.warning("[overlay:xr] ♻️♻️ 已重建 overlay 会话（第 %d 次）", self._rebuilds)
            else:
                self._reinits += 1
                self._teardown(keep_gl=True)
                self._bring_up(rebuild_gl=True)
                log.warning("[overlay:xr] ♻️♻️♻️ 已重建 GL context + 会话（第 %d 次）", self._reinits)
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] ⚠️ 自愈（%s）失败，下轮再试：%s: %s",
                        self._stage, type(exc).__name__, exc)

    def tick(self) -> None:
        """定期调用（engine / GUI 侧）：只留心跳。

        ⚠️ 这里**不做任何 XR 调用**（不泵事件、不热重载、不 locate）—— 整条 XR 生命线由
        `_xr_main` 线程独占；跨线程调 XR 会搅乱状态机（实测：帧线程里 session 停在
        READY、提交报 SessionNotRunning）。热重载也搬进了那条线程。
        """
        if self.dry_run or not self.available or self._sess is None:
            return
        now = time.monotonic()
        if now - self._last_heartbeat >= self.HEARTBEAT_S:
            self._last_heartbeat = now
            age = now - self._last_ok_at if self._last_ok_at else -1.0
            log.info("[overlay:xr][diag] 面板活着：已上传 %d 帧，上次成功 %.1fs 前，"
                     "会话状态=%s，锚点 %s", self.frames_updated, age,
                     self._sess.state, self.cfg.anchor)

    def _reload_config_if_changed(self) -> None:
        """配置热重载：位置/尺寸/字号改完存盘即生效，无需重启。"""
        if not (self.config_path and self.config_path.exists()):
            return
        mtime = self.config_path.stat().st_mtime
        if mtime == self._last_cfg_mtime:
            return
        self._last_cfg_mtime = mtime
        try:
            import yaml
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            new_cfg = OverlayConfig.from_dict(raw.get("overlay") or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] 读配置失败（本次不重载）：%s", exc)
            return
        geo, render = should_rebuild(self.cfg, new_cfg)
        # 淡出阈值既不改几何也不改贴图 —— 它只影响帧循环里逐帧算的整层 alpha。
        # 但配置对象**必须**换掉，否则界面上改了 fade_after_s 存盘后不生效。
        fade_only = new_cfg.fade_after_s != self.cfg.fade_after_s
        if not (geo or render or fade_only):
            return
        self.cfg = new_cfg
        try:
            # ★ 尺寸变化必须让**交换链**跟着换（见 `XrOverlaySession.set_size` 的实测记录）：
            #   只重渲染不换交换链的话，新图比交换链大 → 上传越界 + image_rect 越界 →
            #   每帧提交失败，而自愈重建用的还是旧尺寸 → 面板永久卡死。
            if self._sess is not None and tuple(new_cfg.size_px) != tuple(self._sess.size_px):
                old = self._sess.size_px
                self._sess.set_size(tuple(new_cfg.size_px))
                log.info("[overlay:xr] 面板尺寸 %sx%s → %sx%s（交换链已跟随重建）",
                         old[0], old[1], new_cfg.size_px[0], new_cfg.size_px[1])
            if geo:
                self._sess.ensure_anchor(new_cfg.anchor, new_cfg.tracker_index)
            if render:
                self._repaint()
            elif geo:
                # 只改了几何：用当前内容重提一帧，让变换生效
                if self._content is not None:
                    self._last_entries = None
                    self._last_render = None
                    self._repaint()
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] ⚠️ 热重载应用失败：%s: %s", type(exc).__name__, exc)
