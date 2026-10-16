"""Windows wrist texture lifecycle; native calls keep their approval owner."""
from __future__ import annotations

import ctypes
import time
from PIL import Image
from ..session.chatgpt_output import allowed, current


def _m():
    from . import openvr_overlay
    return openvr_overlay


class _WristTexture:
    def _can_upload(self, approval, epoch) -> bool:
        return not self._closed and epoch == self._epoch and allowed(approval)


    def _upload(self, img: Image.Image, epoch: int | None = None) -> None:
        """把一帧送上手腕屏。有 D3D 层走 SetOverlayTexture，否则走旧的 setOverlayRaw。

        两条路**共用同一套失败计数与自愈阶梯**（`_upload_fails` / `_fails_in_stage` /
        `_escalate`）——不新造一套，否则「连续失败 → 重建 → 硬重启」的行为会在两条路上漂移。
        """
        approval = current()
        epoch = self._epoch if epoch is None else epoch
        if not self._can_upload(approval, epoch):
            return
        if self._d3d is not None:
            self._upload_texture(img, approval, epoch)
        else:
            self._upload_raw(img, approval, epoch)


    def _upload_raw(self, img: Image.Image, approval=None, epoch=None) -> None:
        """旧路（降级兜底）：`setOverlayRaw` 每帧新建一张贴图，高频刷新约 200 次后会撞墙。

        ⚠️ 这条路的性质是**已知缺陷**（见模块 docstring / openvr#772）：我们不用「等它挂了
        再自愈」，而是按帧数**主动**重建（`_m().RAW_REBUILD_EVERY_FRAMES`），把「僵住数秒」换
        成「一次极短的刷新」。
        """
        if epoch is None:
            approval, epoch = current(), self._epoch
        if not self._can_upload(approval, epoch):
            return
        w, h = img.size
        data = _m()._rgba_bytes(img)            # 与 D3D 路**同一口径**（见 _rgba_bytes）
        buf = ctypes.create_string_buffer(data, len(data))
        if not self._can_upload(approval, epoch):
            return
        self._uploaded_approval = approval
        try:
            self._overlay.setOverlayRaw(self._handle, buf, w, h, 4)
        except Exception as exc:  # noqa: BLE001
            if not self._can_upload(approval, epoch):
                return
            self._last_entries = self._last_render = None
            self._on_upload_failure(exc, "setOverlayRaw")
            return
        if epoch != self._epoch or self._closed:
            return
        try:
            self._set_alpha(self.cfg.alpha)
        except Exception:
            pass
        if not self._can_upload(approval, epoch):
            return
        self._on_upload_success(w, h)
        self._raw_frames += 1
        if self._raw_frames >= _m().RAW_REBUILD_EVERY_FRAMES:
            # 先清零再重建：重建内部会重贴一帧（force=True）→ 不清零就会立刻再触发一次
            self._raw_frames = 0
            print(f"[overlay] ♻️ 兼容模式：已连续刷新 {_m().RAW_REBUILD_EVERY_FRAMES} 帧 → "
                  f"主动重建面板（旧路约 200 次上传必挂，提前换掉以免僵住数秒）")
            self._recreate_overlay()


    def _upload_texture(self, img: Image.Image, approval=None, epoch=None) -> None:
        """新路：只更新持久 D3D11 纹理的内容，再把它交给 `setOverlayTexture`（不新建任何贴图）。"""
        if epoch is None:
            approval, epoch = current(), self._epoch
        if not self._can_upload(approval, epoch):
            return
        w, h = img.size
        data = _m()._rgba_bytes(img)
        if not self._can_upload(approval, epoch):
            return
        # D3D11 上传与 overlay 提交是**两类**问题，必须分开 try：合并成一段时，D3D 侧的
        # `Map(源纹理) 失败 hr=0x887A0005` 会被报成「setOverlayTexture 失败」，排障方向被带偏
        # （实测踩过）。op 名进日志，`_on_upload_failure` 才能报出真实那一步。
        self._uploaded_approval = approval
        steps = (("D3D11 upload", lambda: self._d3d.upload(data)),
                 ("setOverlayTexture", self._set_overlay_texture))
        for op, call in steps:
            if epoch != self._epoch or self._closed:
                return
            if not allowed(approval):
                try:
                    self._set_alpha(0.0)
                except Exception:
                    pass
                return
            try:
                call()
            except Exception as exc:  # noqa: BLE001
                if not self._can_upload(approval, epoch):
                    return
                self._last_entries = self._last_render = None
                # 「不支持 / InvalidTexture」类错误 = 这条路在这台机器/这个 SteamVR 上根本走不通，
                # 重试没意义 → **永久回退** setOverlayRaw（只留痕一次），并立刻用旧路补上这一帧。
                if self._is_texture_unsupported(exc):
                    self._fallback_to_raw_permanently(exc)
                    self._upload_raw(img, approval, epoch)
                    return
                # 其它错误（设备丢失 / RequestFailed 等）按现有自愈阶梯走。
                self._on_upload_failure(exc, op)
                return
        if not self._can_upload(approval, epoch):
            return
        try:
            self._set_alpha(self.cfg.alpha)
        except Exception:
            pass
        if self._can_upload(approval, epoch):
            self._on_upload_success(w, h)


    def _on_upload_failure(self, exc: Exception, op: str) -> None:
        self._upload_fails += 1
        f = self._upload_fails
        self._fails_in_stage += 1
        # 降噪：连失败 86 次时每帧打一行会把日志彻底淹没（用户实测就是这样），
        # 只报第一次 + 每隔 50 次汇总一条；但**第一次**要带上现场，
        # 否则事后只有一句 RequestFailed，谁也判断不出是「面板卡住」还是「面板被丢掉」。
        if f == 1:
            print(f"[overlay] ❌ {op} 失败：{type(exc).__name__}: {exc}")
            print(f"[overlay][diag] 失败现场：{self._state_brief()}")
        if f % 50 == 0:
            print(f"[overlay] ❌ 贴图上传已连续失败 {f} 次（面板会停在最后一帧）")
            print(f"[overlay][diag] {self._state_brief()}")
        self._escalate()


    def _on_upload_success(self, w: int, h: int) -> None:
        self.frames_updated += 1
        self._last_ok_at = time.monotonic()
        if self._upload_fails:
            print(f"[overlay] ✅ 贴图恢复上传（连续失败 {self._upload_fails} 次；"
                  f"期间重建 {self._rebuilds_since_ok} 次、硬重启 {self._reinits_since_ok} 次）")
            self._upload_fails = 0
            self._recover_stage = "none"
            self._fails_in_stage = 0
            self._rebuilds_since_ok = 0
            self._reinits_since_ok = 0
        # 也别每帧都打：第 1 帧 + 每 50 帧一条（保留"还在刷"的证据，但不淹日志）
        if self.frames_updated == 1 or self.frames_updated % 50 == 0:
            print(f"[overlay] ← 贴图已更新（{w}x{h}, 第 {self.frames_updated} 帧）")


    def _note_degraded(self, note: str | None) -> None:
        """记下**面向用户**的降级说明（`None` = 已在正常路径上）。

        用途：`engine` 每帧把它与上一帧比对，变了就经 `on_status` 推到状态栏（禁静默降级）。
        日志那几行仍然照打 —— 状态栏会被后续消息覆盖，日志才是事后可查的那一份。
        """
        if note != self.degraded_note:
            self.degraded_note = note
            print(f"[overlay] ⚠️ 手腕屏降级状态变化：{note or '已恢复正常（持久贴图路）'}", flush=True)


    def _init_texture_path(self) -> None:
        """尝试启用「持久 D3D11 贴图 + SetOverlayTexture」；任何一步失败都留一行痕并降级到旧路。

        **绝不抛异常**（引擎不能被它打死）。降级判据：
          * dry_run / 已永久回退 → 直接不启用；
          * overlay 接口没有 `setOverlayTexture`（例如离线测试的假 openvr）→ 走旧路；
          * 非 Windows：`_m().D3D11Texture(...)` 构造即因 `WinDLL` 缺失抛异常 → 落到下面的降级分支；
          * D3D 设备/纹理建不起来（无 GPU、驱动异常等）→ 同样降级。
        """
        if self.dry_run or self._tex_disabled:
            return
        if self._overlay is None or not hasattr(self._overlay, "setOverlayTexture"):
            print("[overlay] overlay 接口无 setOverlayTexture：走 setOverlayRaw 旧路")
            self._note_degraded("手腕屏走的是兼容刷新方式（这套 SteamVR 不支持贴图直传）："
                                "内容偶尔会短暂不更新，属已知限制")
            return
        w, h = self.cfg.size_px
        try:
            self._d3d = _m().D3D11Texture(w, h)
            self._set_overlay_texture()      # 立刻绑一次：新 overlay 上此刻还没有任何贴图
        except Exception as exc:  # noqa: BLE001
            self._close_d3d()
            print(f"[overlay] ⚠️ D3D11 贴图路径不可用，降级用 setOverlayRaw（稍后会再试一次）："
                  f"{type(exc).__name__}: {exc}")
            self._note_degraded("手腕屏暂时退回兼容刷新方式（图形加速路径不可用，稍后会自动重试）")
            self._schedule_tex_retry()      # 可能是瞬时失败（TDR 复位窗口）→ 到点再试一次
            return
        self._note_degraded(None)
        print(f"[overlay] ✅ D3D11 持久贴图路径已启用（{w}x{h}，SetOverlayTexture，"
              f"色空间={_m().OVERLAY_COLOR_SPACE}）")


    def _set_overlay_texture(self) -> None:
        """把当前 D3D 纹理交给 SteamVR。重建/硬重启后**新 handle 上没有任何贴图**，必须重调。"""
        if self._closed or self._d3d is None or not allowed(self._uploaded_approval):
            return
        approval, epoch = self._uploaded_approval, self._epoch
        texture = _m()._make_texture_t(self._d3d.shared_handle)
        if self._can_upload(approval, epoch):
            self._overlay.setOverlayTexture(self._handle, texture)


    def _schedule_tex_retry(self) -> None:
        """记下「隔一会儿再试一次 D3D 路」的时间点（`_maybe_retry_tex_path` 会在 tick 里兑现）。

        两种入口都会调它：① `start()` 时 `_init_texture_path()` 就失败；② 设备丢失后那次重建失败。
        真死 GPU 也不会反复建设备 —— 兑现代价是 `_tex_retried`，整个会话只放行一次。
        """
        if not self._tex_retried and not self._tex_disabled:
            self._tex_retry_at = time.monotonic() + _m().TEX_RETRY_AFTER_S


    def _maybe_retry_tex_path(self) -> None:
        """到点了就再试一次启用 D3D 路（跑在 `tick()` 里，所以不依赖「有上传失败」）。

        ⚠️ 与 `_maybe_reinit` / `_maybe_apply_anchor` **同一套纪律**：只跑在 `tick()` 里，且
        `close()` 过的实例绝不复活。少了 `_closed` 守卫时的实测后果（复审 #99 第 2 条）：
        对一个已 `close()` 的实例调 `tick()`，会真的**新建一个 D3D11 设备/纹理**
        （真机上泄漏到进程退出），再拿 `handle=None` 去调 `setOverlayTexture`。
        `close()` 里那条 `_tex_retry_at = None` 是第二道护栏（把已排定的时间点直接作废）——
        两道都要，缺一道就有一半路径能复活（同 `_maybe_reinit` 那对护栏）。
        """
        if (self._closed or self._d3d is not None or self._tex_disabled or self._tex_retried
                or self._tex_retry_at is None or time.monotonic() < self._tex_retry_at):
            return
        self._tex_retried = True
        self._tex_retry_at = None
        print("[overlay] ♻️ 再试一次启用 D3D11 持久贴图路径（上次不可用）")
        self._init_texture_path()
        if self._d3d is not None:
            # 换路成功：把计数与阶段都清干净，别让新旧两条路的失败历史互相干扰
            self._upload_fails = 0
            self._fails_in_stage = 0
            self._recover_stage = "none"
            self._raw_frames = 0
        else:
            # 定点重试也失败 → 本会话就停在兼容模式了：必须**响亮**说清代价（用户可感知）
            self._note_degraded("手腕屏本次运行一直用兼容刷新方式（图形加速路径不可用）："
                                "面板会每约 2 分钟短暂重刷一次；重启程序可再试")


    def _rebind_or_rebuild(self, where: str) -> None:
        """把贴图绑到当前 handle；若 D3D 设备已丢失则先重建纹理。

        只重建 SteamVR 一侧救不回坏设备（把一张死纹理绑到新 handle 上，得等下一轮破坏性的
        硬重启才有机会修）。`_recreate_overlay()` / `_hard_reinit()` 共用这一条判据。
        """
        if self._d3d is not None and not self._d3d.healthy():
            print(f"[overlay]   {where}：D3D11 设备已丢失 → 一并重建持久纹理")
            self._rebuild_d3d("设备已丢失")
        else:
            self._set_overlay_texture()


    def _close_d3d(self) -> None:
        if self._d3d is not None:
            try:
                self._d3d.close()
            except Exception:  # noqa: BLE001
                pass
            self._d3d = None


    def _rebuild_d3d(self, reason: str = "尺寸变更") -> None:
        """重建持久纹理 —— **两种**触发（`size_px` 变更 / D3D11 设备丢失），所以日志要带原因。

        ⚠️ 文案别写死成「size_px → …」：排 TDR 时看到那行会误以为是配置热重载（复审提过）。
        """
        w, h = self.cfg.size_px
        self._close_d3d()
        try:
            self._d3d = _m().D3D11Texture(w, h)
            self._set_overlay_texture()
        except Exception as exc:  # noqa: BLE001
            self._close_d3d()
            print(f"[overlay] ⚠️ 重建 D3D11 纹理失败（{reason}），降级用 setOverlayRaw（稍后会再试一次）："
                  f"{type(exc).__name__}: {exc}")
            self._schedule_tex_retry()      # 同上：瞬时失败给一次定点重试的机会
            return
        print(f"[overlay] ♻️ D3D11 纹理已重建（{reason}）：{w}x{h}")


    def _fallback_to_raw_permanently(self, exc: Exception) -> None:
        """setOverlayTexture 报「不支持」→ 释放 D3D、永久回退旧路，只留痕一次。"""
        self._close_d3d()
        self._tex_disabled = True
        self._note_degraded("手腕屏本次运行一直用兼容刷新方式（这套 SteamVR 不支持贴图直传）："
                            "面板会每约 2 分钟短暂重刷一次；重启程序可再试")
        if not self._tex_fallback_logged:
            self._tex_fallback_logged = True
            print(f"[overlay] ⚠️ setOverlayTexture 不被支持（{type(exc).__name__}: {exc}）"
                  f"→ 永久回退 setOverlayRaw")
            # 降级要**响亮**：这条路的已知代价写清楚，别让用户拿「面板偶尔不更新」去猜原因
            print("[overlay] ⚠️ 兼容模式的已知代价：旧路每约 200 次上传会失败一次，"
                  "面板会僵住数秒 —— 已改为每 "
                  f"{_m().RAW_REBUILD_EVERY_FRAMES} 帧主动重建来避开，仍可能看到一次极短的刷新")
            print("[overlay]    排查方向：这套 SteamVR 版本是否支持 setOverlayTexture、"
                  "显卡/驱动是否异常（把本段日志发给我们）", flush=True)


    @staticmethod
    def _is_texture_unsupported(exc: Exception) -> bool:
        """这条错误是不是「SetOverlayTexture 根本不支持 / 纹理被判非法」这类**重试无用**的错。

        pyopenvr 把原因写在**异常类名**上（如 `OverlayError_InvalidTexture`），str() 常为空，
        所以类名与文本都扫一遍。
        """
        blob = f"{type(exc).__name__}{exc}".lower()
        return any(m in blob for m in ("invalidtexture", "unsupported", "notsupported",
                                       "not supported"))
