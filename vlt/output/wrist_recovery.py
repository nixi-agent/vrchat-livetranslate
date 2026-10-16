"""Windows wrist recovery lifecycle; native calls keep their approval owner."""
from __future__ import annotations

import time


def _m():
    from . import openvr_overlay
    return openvr_overlay


class _WristRecovery:
    def _schedule_reinit(self, reason: str) -> None:
        """排下一次「重新接管 SteamVR」的时间点（退避翻倍、封顶 `_m().REINIT_BACKOFF_MAX_S`）。

        ⚠️ 为什么必须有它：`available=False` 时上传/心跳/热重载**全部**早退，再也没有一次
        上传失败去驱动 `_escalate()` —— `_hard_reinit` 里那句「下轮失败时再试」永远等不到，
        面板冻结在最后一帧。重试因此由 `tick()` 驱动（见 `_maybe_reinit`）。
        退避保证不会变成重建风暴（真死 GPU / SteamVR 没起来时，5s→10s→20s→40s→60s…）。
        """
        if self.dry_run:
            return
        self._reinit_fails += 1
        delay = min(_m().REINIT_BACKOFF_MAX_S, _m().REINIT_BACKOFF_S * (2 ** (self._reinit_fails - 1)))
        self._reinit_at = time.monotonic() + delay
        print(f"[overlay] ⏳ 面板当前不可用（{reason}）：{delay:.0f}s 后自动重试接管 SteamVR"
              f"（第 {self._reinit_fails} 次尝试）", flush=True)


    def _maybe_reinit(self) -> None:
        """到点了就再试一次 `_hard_reinit()`（跑在 `tick()` 里，不依赖「有上传失败」）。

        ⚠️ 必须显式看 `_closed`：`close()` 恰好把 `available` 置 False，而这正是本函数
        「可以重试」的条件 —— 少了这个守卫，「已排定重试 + 用户取消勾选」会让下一次 tick
        直接 `openvr.init()` 把面板**复活**（正是本 PR 要消灭的「取消勾选后屏自己回来」）。
        复审实测指出（`_maybe_apply_anchor` 那边本来就有这条守卫，这里漏了）。
        """
        if (self._closed or self.available or self.dry_run or self._reinit_at is None
                or time.monotonic() < self._reinit_at):
            return
        self._reinit_at = None
        print(f"[overlay] ♻️ 面板不可用 → 重试接管 SteamVR（第 {self._reinit_fails} 次）", flush=True)
        if self._hard_reinit():              # 失败时它会自己 `_schedule_reinit` 排下一次
            self._reinit_fails = 0
            self._note_degraded(None)


    def _recreate_overlay(self) -> bool:
        """销毁重建 overlay —— 贴图上传连续失败时用来恢复。

        SteamVR 的 `setOverlayRaw` 每次调用都会新建一张贴图，长时间高频更新后
        可能开始返回 `OverlayError_RequestFailed`（用户实测：连续失败 86 次，
        手腕屏停在最后一帧不动，之后又自己恢复）。重建能拿到一张干净的贴图；
        重建失败也不致命，下一次失败还会再试。
        """
        if self.dry_run or self._overlay is None:
            return False
        try:
            try:
                if self._handle is not None:
                    self._overlay.destroyOverlay(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            self._device = self._resolve_anchor()
            self.available = True
            self._apply_transform()
            self._overlay.showOverlay(self._handle)
            self._rebind_or_rebuild("已重建 overlay")   # 新 handle 上没有任何贴图，必须重绑/重建
            self._rebuilds += 1
            self._rebuilds_since_ok += 1
            self._recover_stage = "rebuild"
            self._fails_in_stage = 0
            self._raw_frames = 0          # 兼容模式的主动重建计数跟着清零（新 overlay = 新贴图配额）
            print(f"[overlay] ♻️ 已重建 overlay 并重新贴到 {self.cfg.anchor}"
                  f"（device={self._device}，自上次成功以来第 {self._rebuilds_since_ok} 次）")
            self._repaint()
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 重建 overlay 失败：{type(exc).__name__}: {exc}")
            return False


    def _escalate(self) -> None:
        """贴图连续失败时的自愈升级：重建 handle → 硬重启 openvr 上下文。

        用户实测（2026-09-25 20:32，家里那台机器）证明「重建 handle」这一级不够用：

            20:32:07  ← 贴图已更新（第 200 帧）              # 一切正常
            20:32:09  ❌ setOverlayRaw 失败：OverlayError_RequestFailed
                      ♻️ 连续失败 50/100/…/350 次 → 每次都重建，**仍然全程失败**
            20:35:09  会话结束

        面板一旦进入这个状态，**换 handle 救不回来**：换 handle 换不掉已经死掉的
        openvr 上下文（SteamVR 合成器重启 / 头显待机回来 / vrserver 换了一代之后，
        手里这份接口指针会永远返回失败）。所以再加一级：shutdown + init + 重建。
        """
        if self.dry_run or self._overlay is None:
            return
        if self._fails_in_stage < 3:
            return
        # D3D11 侧先自检：**设备丢失**（TDR / 驱动重置 / 适配器移除）只有重建「持久纹理」救得回来，
        # 而下面两级都只修 SteamVR 一侧 —— 不补这道，坏设备会一直挂着：连失败只让
        # `_recreate_overlay`/`_hard_reinit` 反复重建 SteamVR 侧（还会 shutdown+init openvr），
        # 面板永远回不来（实测：20 帧后 _d3d 还是那个坏设备，硬重启计数却在涨）。
        # 只在 none 阶段试一次（成功则计数器由下一次上传结果重置；失败则交给下面两级），避免重建风暴。
        # 设备丢失（TDR / 驱动重置 / 适配器移除）：重建持久纹理。**只在建成功时**才把阶段推到
        # "rebuild"；建失败（复位窗口内的瞬时失败 → `_d3d` 变 None）时保持 "none"，
        # 交给 `tick()` 的定点重试（`_maybe_retry_tex_path`）—— 重试挂在这里是等不到的：
        # raw 路随后会成功，`_escalate` 要等 raw 再攒够 3 次连续失败才被调用（约 200 帧/2 分钟）。
        # ① 见复审第 1 条（这一条我第一版就是这么错的）。
        if (self._d3d is not None and self._recover_stage == "none"
                and not self._d3d.healthy()):
            print("[overlay] ♻️ D3D11 设备已丢失 → 重建持久纹理")
            self._rebuild_d3d("设备已丢失")
            if self._d3d is not None:
                self._recover_stage = "rebuild"   # 再失败就走硬重启那级
                self._fails_in_stage = 0
            return
        if self._recover_stage == "none":
            print(f"[overlay] ♻️ 贴图连续失败 {self._upload_fails} 次 → 重建 overlay")
            self._recreate_overlay()
        elif self._recover_stage == "rebuild":
            print(f"[overlay] ♻️ 重建之后仍然连续失败 {self._fails_in_stage} 次"
                  f"（换 handle 没用）→ 硬重启 openvr 连接")
            self._hard_reinit()
        elif self._upload_fails % 50 == 0:
            # 已经硬重启过还在失败：别每 3 帧就重启一次 openvr（那等于自己把自己拖死），
            # 每 50 次失败再试一次。
            print(f"[overlay] ♻️ 硬重启之后仍然连续失败 {self._upload_fails} 次 → 再硬重启一次")
            self._hard_reinit()


    def _hard_reinit(self) -> bool:
        """整条 openvr 连接重启：shutdown → init → 重建 overlay → 把内容贴回去。

        ⚠️ 进程里只有**手腕屏这一处**在用 openvr（GUI 双引擎模式下也只给 owner 那条腿挂），
        所以 shutdown 不会波及别的组件；调用点都在同一个引擎线程（init 与 shutdown 同线程）。
        """
        if self.dry_run:
            return False
        try:
            import openvr
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 硬重启失败（openvr 不可用）：{exc}")
            return False
        try:
            openvr.shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay]   硬重启：shutdown 报错（忽略，继续 init）：{type(exc).__name__}: {exc}")
        try:
            self._vr = openvr.init(openvr.VRApplication_Background)
            self._overlay = openvr.IVROverlay()
            self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            self._device = self._resolve_anchor()
            self.available = True
            self._apply_transform()
            self._overlay.showOverlay(self._handle)
            self._rebind_or_rebuild("已硬重启 openvr")  # 新 handle 上没有任何贴图，必须重绑/重建
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 硬重启 openvr 失败（已排入退避重试）："
                  f"{type(exc).__name__}: {exc}")
            # ⚠️ 这里置 available=False 之后，上传 / 心跳 / 热重载会**全部**早退 ——
            #    必须有 `_maybe_reinit()` 接手重试（退避 5s→60s，见 _schedule_reinit），
            #    否则「下轮失败时再试」永远等不到（面板冻结在最后一帧、日志也安静了
            #    = 用户看到的「内容不更新」）。
            #    ⚠️ 两个手腕屏修复分支在这里各写了一套「不可用后重试」的机制（固定 15s vs
            #    退避 5→60s）；合并时**只保留退避这一套**（有界、不会长期每 15s 敲一次坏掉的
            #    SteamVR），另一套连同 HEAL_RETRY_S/_heal_at/_maybe_heal 一起删掉 ——
            #    两套并存会各自重试一次（实测合并后 tick 里同时调 `_maybe_heal` 与
            #    `_maybe_reinit`），那是同一件事的两份实现，必然漂移。
            #    `last_error` 保留：界面要靠它说清真因（见 gui_sinks.overlay_error_text）。
            self.last_error = _m()._reason_code(f"{type(exc).__name__}{exc}")
            self.available = False
            self._schedule_reinit("硬重启失败")
            return False
        self._reinits += 1
        self._reinits_since_ok += 1
        self._recover_stage = "reinit"
        self._fails_in_stage = 0
        self._reinit_at = None
        self._reinit_fails = 0            # 接管成功 → 「不可用」的退避计数清零
        print(f"[overlay] ♻️♻️ 已硬重启 openvr 连接并重建 overlay"
              f"（累计第 {self._reinits} 次，device={self._device}）")
        self._repaint()
        return True


    def _hmd_present(self) -> str:
        try:
            import openvr

            fn = getattr(openvr, "VR_IsHmdPresent", None)
            if fn is None:
                return "?"
            return "是" if fn() else "否"
        except Exception:  # noqa: BLE001
            return "?"


    def _maybe_apply_anchor(self) -> None:
        """锚点设备还没被追踪 → 定期重试「应用变换」；面板一直留着，设备一就绪就出现。

        为什么必须有它（而不是让 `start()` 直接失败、让用户重勾）：`start()` 那一刻设备往往
        还没醒（人在桌面前、手柄没拿起来 / 头显没戴），而「设备什么时候被追踪」只有 SteamVR
        知道。旧写法让勾选被退回，用户按「先把 SteamVR 打开」重试永远是白费力气 —— 面板本身
        其实建得出来，差的只是「贴到哪台设备上」。见 `start()` 里那段说明。

        ⚠️ 与 `_maybe_reinit` 同一套纪律：只跑在 `tick()` 里，`close()` 过的实例绝不复活
        （否则用户取消勾选后屏会自己回来）；留痕降噪但**绝不静默**。
        """
        if not self.pending_anchor or self._closed or not self.available:
            return
        now = time.monotonic()
        if now < self._anchor_retry_at:
            return
        self._anchor_tries += 1
        self._anchor_retry_at = now + self.ANCHOR_RETRY_S
        try:
            self._apply_transform()
        except Exception as exc:  # noqa: BLE001
            if self._anchor_tries == 1 or self._anchor_tries % self.ANCHOR_RETRY_LOG_EVERY == 0:
                print(f"[overlay] 锚点 {self.cfg.anchor} 仍未就绪（第 {self._anchor_tries} 次重试）："
                      f"{type(exc).__name__}（面板留着，不会退回勾选）", flush=True)
            return
        self.pending_anchor = False
        print(f"[overlay] ✅ 锚点 {self.cfg.anchor} 已就绪（重试 {self._anchor_tries} 次）"
              f"→ 手腕屏面板已出现在该设备上", flush=True)
