"""Windows wrist startup lifecycle; native calls keep their approval owner."""
from __future__ import annotations

import time
from pathlib import Path
from .overlay import OverlayConfig


def _m():
    from . import openvr_overlay
    return openvr_overlay


class _WristStartup:
    def __init__(self, cfg: OverlayConfig, config_path: Path | None = None, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.dry_run = dry_run
        self._frames_dir = _m().ROOT / "out" / "overlay_frames"
        self._vr = None
        self._overlay = None
        self._handle = None
        self._device = None
        self._last_render: tuple[str, str] | None = None
        self._last_entries: tuple | None = None
        self._content = None
        self._uploaded_approval = None
        self._alpha_retry = False
        self._epoch = 0
        self._upload_fails = 0             # 贴图连续上传失败次数（用于降噪 + 触发重建）
        # ---- D3D11 持久贴图 + SetOverlayTexture 上传路径（见 _init_texture_path）----
        self._d3d = None                   # win_d3d11.D3D11Texture；None = 走旧的 setOverlayRaw
        self._tex_disabled = False         # setOverlayTexture 报「不支持」后置真 → 永久回退旧路
        self._tex_fallback_logged = False  # 永久回退只留痕一次（别每帧刷）
        self._tex_retried = False          # 「D3D 路不可用」后**定点重试**一次的机会（整个会话只给一次）
        self._tex_retry_at = None          # 定点重试的时间点（monotonic；由 _schedule_tex_retry 设置）
        self._last_cfg_mtime = 0.0
        self._last_text_at = 0.0
        # ---- 兼容模式（旧路 setOverlayRaw）下的主动重建 + 降级留痕 ----
        self._raw_frames = 0               # 兼容模式下自上次重建以来成功刷新的帧数（见 _m().RAW_REBUILD_EVERY_FRAMES）
        self.degraded_note = None          # 面向用户的降级说明（None = 正常走持久贴图路）；见 _note_degraded
        # ---- 自愈状态机 + 诊断（用户实测「手腕屏隔一阵就消失」，见 _upload 的注释）----
        self._last_ok_at = 0.0             # 最后一次成功上传贴图的时刻（monotonic）
        self._recover_stage = "none"       # none / rebuild / reinit
        self._fails_in_stage = 0           # 本阶段内连续失败次数（到 3 就升级到下一阶段）
        self._rebuilds_since_ok = 0        # 自上次成功以来重建几次
        self._reinits_since_ok = 0         # 自上次成功以来硬重启几次
        self._rebuilds = 0                 # 累计
        self._reinits = 0
        self._reinit_at = None             # 「面板不可用」后的下一次重试时间点（monotonic）
        self._reinit_fails = 0             # 不可用状态下的重试次数（退避用）
        self._last_heartbeat = 0.0
        self.available = False
        #: 起不来时的**原因码**（界面按码翻文案；见 `output/overlay.py` 的 OVERLAY_ERR_*）。
        #: 空串 = 最近一次没有失败。`start()` 的每条失败路径都必须写它 ——
        #: 界面过去无从知道真因，把什么都写成「先把 SteamVR 打开」。
        self.last_error = ""
        self._closed = False               # close() 过：绝不参与自愈（否则取消勾选后屏会自己回来）
        #: 锚点设备（手柄 / tracker）还没被追踪 → **面板留着**，由 tick 里的
        #: `_maybe_apply_anchor` 定期重试应用变换。见 `start()` 里那段说明：
        #: 旧写法会让「不戴设备时勾选手腕屏」直接失败并把勾选退回（用户实测报的就是它）。
        self.pending_anchor = False
        self._anchor_retry_at = 0.0        # 下一次重试应用变换的时间点（monotonic）
        self._anchor_tries = 0             # 已重试次数（留痕降噪用）
        self.frames_updated = 0


    def start(self) -> bool:
        """尝试接管 SteamVR；没跑就返回 False 并禁用（不抛异常、不阻塞）。"""
        self._closed = False               # 显式 start() 过的实例重新参与自愈
        self.last_error = ""
        # ⚠️ 会话级自愈状态必须归零：`start()` 之后就是**新的一轮会话**，上一轮的值留着会
        #    让新会话按上一轮的档位跑（复审 #99 第 3 条，逐条实测过）：
        #      * `_reinit_fails` 从 3 接着走 → 下一次「面板不可用」排定 **40s** 而不是 5s；
        #      * `_tex_retried` 仍是 True → 「整个会话只给一次」的 D3D 定点重试名额已被用掉
        #        （`_schedule_tex_retry` 直接成了 no-op）；
        #      * `_anchor_tries` 留着 → 新会话的锚点重试从「第 9 次」开始，前 30 次的留痕
        #        规则（`ANCHOR_RETRY_LOG_EVERY`）跟着错位；
        #      * `_upload_fails` / `_fails_in_stage` 留着 → 新会话第一次失败就凑够 3 次、
        #        立刻升级成「重建面板 / 硬重启 openvr」。
        #    `_tex_disabled` / `_tex_fallback_logged` **不重置**：它们是「这套 SteamVR 不支持
        #    贴图直传」的判定（与 openvr 版本绑定，重启同一实例也不会变），重置只会让同一条
        #    不支持重复踩一遍。`degraded_note` 同理（重置会在 `_init_texture_path` 因
        #    `_tex_disabled` 早退时把「仍在降级」这件事从状态栏抹掉）。
        self._reinit_at = None
        self._reinit_fails = 0
        self._tex_retry_at = None
        self._tex_retried = False
        self._upload_fails = 0
        self._fails_in_stage = 0
        self._recover_stage = "none"
        self._rebuilds_since_ok = 0
        self._reinits_since_ok = 0
        self._raw_frames = 0
        self._last_ok_at = 0.0
        self.pending_anchor = False
        self._anchor_retry_at = 0.0
        self._anchor_tries = 0
        if not self.cfg.enabled:
            print("[overlay] 配置里已禁用")
            self.last_error = _m().OVERLAY_ERR_OTHER
            return False
        if self.dry_run:
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            print(f"[overlay][dry-run] 不接管 SteamVR，渲染结果写到 {self._frames_dir}")
            return False
        try:
            import openvr
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] openvr 不可用：{exc}")
            self.last_error = _m().OVERLAY_ERR_OTHER
            return False
        try:
            self._vr = openvr.init(openvr.VRApplication_Background)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 打**类名**：openvr 异常的 str() 恒为空，只打 `{exc}` 等于没有信息
            name = type(exc).__name__
            self.last_error = _m()._reason_code(name)
            print(f"[overlay] ⚠️ SteamVR 未运行或不可用，overlay 已禁用（其他输出不受影响）："
                  f"{name}: {exc}", flush=True)
            print(f"[overlay]   原因："
                  f"{_m()._INIT_ERROR_HINTS.get(name, '未知原因（请把这一行连同上下文发给我们）')}",
                  flush=True)
            if name == "InitError_Init_NoServerForBackgroundApp":
                print(f"[overlay]   {_m()._no_server_hint()}", flush=True)
            return False
        try:
            # ⚠️ overlay 接口是**模块级工厂函数** `openvr.IVROverlay()`，
            # 不是 IVRSystem 的属性 —— 写 `self._vr.overlay` 会抛
            # AttributeError: 'IVRSystem' object has no attribute 'overlay'，
            # 而且必须放在 try 里：否则异常会冒到引擎，把整条翻译腿一起打死。
            self._overlay = openvr.IVROverlay()
            try:
                self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if "KeyInUse" not in name and "KeyInUse" not in str(exc):
                    raise
                # 同 key 的**残留 overlay**（上次进程异常退出没清干净）→ 清掉再建一次。
                # 用户实测过这个报错：双引擎同时挂同一个 key 会撞
                # `OverlayError_KeyInUse`（GUI 侧已改成只给 owner 那条腿挂）；
                # 这里兜的是「上一次没清干净」的情况，否则手腕屏会一直被卡住。
                print(f"[overlay] ⚠️ key '{self.cfg.overlay_key}' 已被占用（{name}），"
                      f"尝试清理残留 overlay 后重建", flush=True)
                try:
                    stale = self._overlay.findOverlay(self.cfg.overlay_key)
                    self._overlay.destroyOverlay(stale)
                    print("[overlay]   已清理残留 overlay", flush=True)
                except Exception as exc2:  # noqa: BLE001
                    print(f"[overlay]   清理残留 overlay 未成功（继续尝试重建）："
                          f"{type(exc2).__name__}: {exc2}", flush=True)
                self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            self._device = self._resolve_anchor()
            # ⚠️ available 必须在 _apply_transform 之前置位：它开头有
            # `if not self.available: return`，否则「创建时应用变换」这一步等于没做
            # （面板会出现但停在默认位置）。
            self.available = True
            # ⚠️ 锚点设备（手柄 / tracker）没被追踪时，`setOverlayTransformTrackedDeviceRelative`
            #    会抛 `OverlayError_InvalidTrackedDevice`（本机真 SteamVR 实测：SteamVR 明明在跑，
            #    但没头显 / 手柄没醒）。旧写法让整个 start() 失败 → 界面把勾选退回、还只给一句
            #    「先把 SteamVR 打开」——用户照它重试纯属白费力气（人在桌面前、手柄就是没醒，
            #    这正是他报的「非翻译模式下强制打开手腕屏不行」最可能的形态）。
            #    现在：**面板留着**，进「等锚点」状态，由 `tick()` 的 `_maybe_apply_anchor`
            #    每 ANCHOR_RETRY_S 重试一次，设备一被追踪面板就自己出现（不用再勾一次）。
            try:
                self._apply_transform()
            except Exception as exc:  # noqa: BLE001
                if _m()._reason_code(f"{type(exc).__name__}{exc}") != _m().OVERLAY_ERR_ANCHOR:
                    raise                       # 其它错（句柄/贴图类）照旧判失败，别把真问题掩盖掉
                self.pending_anchor = True
                self._anchor_retry_at = time.monotonic() + self.ANCHOR_RETRY_S
                print(f"[overlay] ⚠️ 锚点设备（{self.cfg.anchor}）还没被 SteamVR 追踪："
                      f"面板先留着，每 {self.ANCHOR_RETRY_S:.0f}s 自动重试 —— 唤醒手柄 / tracker"
                      f"（或戴上头显）后它会自己出现，不需要重新勾选", flush=True)
            self._overlay.showOverlay(self._handle)
            # overlay 建好、变换应用完之后，尝试切到「持久 D3D11 贴图 + SetOverlayTexture」路。
            # 这一步**绝不许抛异常**（内部全 try 住）：建不起来就留一行痕、继续用旧的 setOverlayRaw。
            self._init_texture_path()
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            self.last_error = _m()._reason_code(f"{name}{exc}")
            print(f"[overlay] ⚠️ 创建 overlay 失败，已禁用（其他输出不受影响）："
                  f"{name}: {exc}")
            # 真因要能行动：锚点设备没被追踪 ≠ SteamVR 没开（用户照「先把 SteamVR 打开」
            # 重试是白费力气，实测就是这么报的：OverlayError_InvalidTrackedDevice）
            if self.last_error == _m().OVERLAY_ERR_ANCHOR:
                print(f"[overlay]   原因：{self._anchor_hint()}", flush=True)
            else:
                print(f"[overlay]   原因："
                      f"{_m()._INIT_ERROR_HINTS.get(name, '未知原因（请把这一行连同上下文发给我们）')}",
                      flush=True)
            # ⚠️ `createOverlay()` 可能在**之前**就已经成功（锚点解析 / 应用变换都在它之后）：
            #    不销毁的话，SteamVR 里就留着一个我们不再持有的同名 overlay → 下次勾选直接撞
            #    `OverlayError_KeyInUse`（真机日志里那串「key 已被占用 → 清理残留」正是它，
            #    而且每失败一次多留一个）。
            self._release_handle()
            self.available = False
            return False
        if self.pending_anchor:
            # 不许打「✅ 已挂到 …」：这会儿锚点还没就绪，面板并没有真的贴到设备上
            print(f"[overlay] 面板已建好，但锚点 {self.cfg.anchor} 还没被追踪（见上一行）"
                  f"—— 设备就绪后会自动贴上去", flush=True)
        else:
            print(f"[overlay] ✅ 已挂到 {self.cfg.anchor}（device={self._device}），"
                  f"宽 {self.cfg.width_m}m，位置 {self.cfg.pos}")
        return True
