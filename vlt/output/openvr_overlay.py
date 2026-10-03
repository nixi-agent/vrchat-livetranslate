"""手腕 Overlay 的 **Windows / SteamVR 后端**（SteamVR 的 overlay 接口 + Pillow）。

⚠️ 本模块是 **Windows 独占**：
  * PyInstaller 打包 Windows 版时，它随 `vlt/platform/win.py` 一起进包；
  * 打 Linux 版（AppImage）时，构建脚本会把这份文件从 AppDir 里**删掉**；
  * `scripts/check_platform_purity.py --platform linux` 会断言它（及其引入的字样）
    不在 Linux 产物里 —— 这条是红灯门禁，不是注释里的君子协定。

Linux 侧的对等物是 `vlt/output/openxr_overlay.py`（自建 OpenXR overlay）。
两端公开接口一致（`start/update/update_entries/tick/close/available`），
所以 engine / gui 不需要分平台。

设计要点：
- 纯 Python，不需要 Unity / XSOverlay / OVR Toolkit
- SteamVR 没跑时**优雅降级**（禁用 overlay，不阻塞其他输出，且留痕说明原因）
"""
from __future__ import annotations

import ctypes
import math
import sys
import time
from pathlib import Path

from PIL import Image

from ..paths import APP_DIR as ROOT
from .overlay import OverlayConfig, _unpack_entry, render_conversation, render_panel

def build_matrix(pos: tuple[float, float, float], rot_deg: tuple[float, float, float]):
    """构造 OpenVR 的 3x4 位姿矩阵（行主序）。需要 openvr 才能返回其 ctypes 类型。"""
    import openvr

    rx, ry, rz = (math.radians(a) for a in rot_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    # Rz * Ry * Rx
    r = [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]
    m = openvr.HmdMatrix34_t()
    for i in range(3):
        for j in range(3):
            m.m[i][j] = r[i][j]
        m.m[i][3] = pos[i]
    return m


# ------------------------------------------------- 初始化失败：把「为什么」写进日志
# ⚠️ openvr 的异常**文本恒为空字符串**（原因只写在**异常类名**上，例如
#    `InitError_Init_NoServerForBackgroundApp`）。这里曾经打的是 `{exc}`，
#    结果日志里只剩一个冒号 —— 排查时等于没有信息（实测为此白跑两轮推理）。
#    所以一律打 `type(exc).__name__`，并给常见类名配一句中文。
_INIT_ERROR_HINTS = {
    "InitError_Init_NoServerForBackgroundApp":
        "没找到能接收「后台应用」的 SteamVR 服务：SteamVR 没启动（后台应用**不会**替用户启动它）；"
        "或程序所在路径含非 ASCII 字符（中文、全角括号等），OpenVR 认不出这个应用",
    "InitError_Init_InstallationNotFound":
        "本机找不到 SteamVR 安装（没装，或安装信息读不到）",
    "InitError_Init_VRClientDLLNotFound":
        "找不到 SteamVR 的 vrclient 运行库（安装不完整，可在 Steam 里校验文件完整性）",
    "InitError_Init_HmdNotFound":
        "SteamVR 在跑，但它没检测到头显：确认头显已连上、SteamVR 窗口里能看到设备",
    "InitError_Init_HmdNotFoundPresenceFailed":
        "SteamVR 在跑，但头显连接中断（USB/驱动掉了）：重插头显或重启 SteamVR",
    "InitError_Init_VRMonitorNotFound":
        "SteamVR 的 vrmonitor 没起来：从 Steam 里正常启动一次 SteamVR",
    "InitError_Init_VRMonitorStartupFailed":
        "SteamVR 的 vrmonitor 启动失败：重启 SteamVR，必要时重启电脑",
    "InitError_Init_AnotherAppLaunching":
        "SteamVR 正在启动中：等它起完（头显里能看到 SteamVR 大厅）再勾一次手腕屏",
    "InitError_Init_ShuttingDown":
        "SteamVR 正在退出：等它退干净，重新启动 SteamVR 再勾一次",
    "InitError_Init_UserConfigDirectoryInvalid":
        "SteamVR 的用户配置目录不可用（权限问题，或被清理软件删了）",
    "InitError_Init_PathRegistryNotFound":
        "读不到 SteamVR 的安装路径（注册表项缺失）",
    "InitError_Init_PathRegistryNotWritable":
        "SteamVR 的路径/日志目录不可写（权限问题）",
    "InitError_Init_NoLogPath":
        "SteamVR 的日志目录不可用",
    "InitError_Init_Retry":
        "SteamVR 让稍后重试（多半正在启动/切换场景）：过几秒再勾一次",
}


def _no_server_hint() -> str:
    """121 的**两种形态**只能从 SteamVR 自己的客户端日志里分辨，日志里说清去哪看。"""
    return ("排查：SteamVR 日志目录里的 `vrclient_<程序名>.txt` 会写明是哪种 —— "
            "「no one is listening」= SteamVR 没在跑；"
            "「信号灯超时 / semaphore timeout」= SteamVR 在跑但命名管道不应答"
            "（重启 SteamVR；不行就重启电脑）")


# ---------------------------------------------------------------- overlay 本体
class WristOverlay:
    """SteamVR overlay 生命周期管理 + 文本刷新 + 配置热重载。"""

    # 健康心跳间隔（秒）：日志里留一条「面板还活着 / 已经多久没成功上传」的定期证据。
    # 用户报的「隔一阵手腕屏就消失」没有这行时，事后完全看不出是哪个时刻掉的。
    HEARTBEAT_S = 30.0

    def __init__(self, cfg: OverlayConfig, config_path: Path | None = None, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.dry_run = dry_run
        self._frames_dir = ROOT / "out" / "overlay_frames"
        self._vr = None
        self._overlay = None
        self._handle = None
        self._device = None
        self._last_render: tuple[str, str] | None = None
        self._last_entries: tuple | None = None
        self._upload_fails = 0             # 贴图连续上传失败次数（用于降噪 + 触发重建）
        self._last_cfg_mtime = 0.0
        self._last_text_at = 0.0
        # ---- 自愈状态机 + 诊断（用户实测「手腕屏隔一阵就消失」，见 _upload 的注释）----
        self._last_ok_at = 0.0             # 最后一次成功上传贴图的时刻（monotonic）
        self._recover_stage = "none"       # none / rebuild / reinit
        self._fails_in_stage = 0           # 本阶段内连续失败次数（到 3 就升级到下一阶段）
        self._rebuilds_since_ok = 0        # 自上次成功以来重建几次
        self._reinits_since_ok = 0         # 自上次成功以来硬重启几次
        self._rebuilds = 0                 # 累计
        self._reinits = 0
        self._last_heartbeat = 0.0
        self.available = False
        self.frames_updated = 0

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """尝试接管 SteamVR；没跑就返回 False 并禁用（不抛异常、不阻塞）。"""
        if not self.cfg.enabled:
            print("[overlay] 配置里已禁用")
            return False
        if self.dry_run:
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            print(f"[overlay][dry-run] 不接管 SteamVR，渲染结果写到 {self._frames_dir}")
            return False
        try:
            import openvr
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] openvr 不可用：{exc}")
            return False
        try:
            self._vr = openvr.init(openvr.VRApplication_Background)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 打**类名**：openvr 异常的 str() 恒为空，只打 `{exc}` 等于没有信息
            name = type(exc).__name__
            print(f"[overlay] ⚠️ SteamVR 未运行或不可用，overlay 已禁用（其他输出不受影响）："
                  f"{name}: {exc}", flush=True)
            print(f"[overlay]   原因："
                  f"{_INIT_ERROR_HINTS.get(name, '未知原因（请把这一行连同上下文发给我们）')}",
                  flush=True)
            if name == "InitError_Init_NoServerForBackgroundApp":
                print(f"[overlay]   {_no_server_hint()}", flush=True)
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
            self._apply_transform()
            self._overlay.showOverlay(self._handle)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 创建 overlay 失败，已禁用（其他输出不受影响）："
                  f"{type(exc).__name__}: {exc}")
            self.available = False
            return False
        print(f"[overlay] ✅ 已挂到 {self.cfg.anchor}（device={self._device}），"
              f"宽 {self.cfg.width_m}m，位置 {self.cfg.pos}")
        return True

    def _resolve_anchor(self):
        import openvr

        sys_ = self._vr
        a = self.cfg.anchor
        try:
            if a == "left_hand":
                return sys_.getTrackedDeviceIndexForControllerRole(openvr.TrackedControllerRole_LeftHand)
            if a == "right_hand":
                return sys_.getTrackedDeviceIndexForControllerRole(openvr.TrackedControllerRole_RightHand)
            if a == "hmd":
                return openvr.k_unTrackedDeviceIndex_Hmd
            if a == "tracker":
                idx = 0
                for i in range(openvr.k_unMaxTrackedDeviceCount):
                    if sys_.getTrackedDeviceClass(i) == openvr.TrackedDeviceClass_GenericTracker:
                        if idx == self.cfg.tracker_index:
                            print(f"[overlay] tracker #{self.cfg.tracker_index} → device {i}")
                            return i
                        idx += 1
                print(f"[overlay] ⚠️ 没找到 tracker #{self.cfg.tracker_index}（驱动注册 ≠ 已配对在线），回退右手")
                return sys_.getTrackedDeviceIndexForControllerRole(openvr.TrackedControllerRole_RightHand)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 解析锚点失败：{exc}")
        return openvr.k_unTrackedDeviceIndex_Hmd

    def _apply_transform(self) -> None:
        if not self.available:
            return
        m = build_matrix(self.cfg.pos, self.cfg.rot)
        self._overlay.setOverlayTransformTrackedDeviceRelative(self._handle, self._device, m)
        self._overlay.setOverlayWidthInMeters(self._handle, self.cfg.width_m)
        self._overlay.setOverlayAlpha(self._handle, self.cfg.alpha)
        # 弯曲每次都应用：只在 >0 时设置的话，把它调回 0 就永远回不去了
        self._overlay.setOverlayCurvature(self._handle, max(0.0, min(1.0, self.cfg.curvature)))

    # ---------- 文本 ----------
    def update(self, text: str, source: str = "", force: bool = False) -> None:
        """刷新面板内容（相同内容不重复上传贴图）。"""
        text, source = text.strip(), source.strip()
        if not text:
            return
        key = (text, source if self.cfg.show_source else "")
        if key == self._last_render and not force:
            return
        self._last_render = key
        self._last_text_at = time.monotonic()
        img = render_panel(text, source, self.cfg)
        if self.dry_run:
            self.frames_updated += 1
            path = self._frames_dir / f"{self.frames_updated:03d}.png"
            img.save(path)
            print(f"[overlay][dry-run] 第 {self.frames_updated} 帧 → {path.name} | {text[:50]}")
            return
        if not self.available:
            return
        self._upload(img)

    def update_entries(self, entries, force: bool = False) -> None:
        """刷新成**对话视图**（GUI 用这个）：entries = [(who, source, translation), ...]
        或 4 元组 [(who, source, translation, label), ...]（label = 说话人昵称）。

        与 `update()` 的区别：`update()` 是"当前这一句"（CLI 单腿场景够用），
        `update_entries()` 是"最近几句对话"——手腕上只有一块屏，内容应该像 GUI 的聊天区。
        """
        # ⚠️ 必须走 `_unpack_entry`：房间链路会传 4 元组，直接解包 3 个会 ValueError，
        # 把整条手腕屏刷新打死（合并 main 时这行曾随 overlay.py 拆分而丢失过一次）。
        key = tuple((w, (s or "").strip(), (t or "").strip(), (lab or "").strip())
                    for w, s, t, lab in (_unpack_entry(e) for e in (entries or [])))
        if key == self._last_entries and not force:
            return
        self._last_entries = key
        self._last_text_at = time.monotonic()
        img = render_conversation(entries, self.cfg)
        if self.dry_run:
            self.frames_updated += 1
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            path = self._frames_dir / f"{self.frames_updated:03d}.png"
            img.save(path)
            print(f"[overlay][dry-run] 第 {self.frames_updated} 帧（对话视图，{len(entries or [])} 条）"
                  f" → {path.name}")
            return
        if not self.available:
            return
        self._upload(img)

    def _upload(self, img: Image.Image) -> None:
        w, h = img.size
        data = img.tobytes()               # RGBA
        buf = ctypes.create_string_buffer(data, len(data))
        try:
            self._overlay.setOverlayRaw(self._handle, buf, w, h, 4)
        except Exception as exc:  # noqa: BLE001
            self._upload_fails += 1
            f = self._upload_fails
            self._fails_in_stage += 1
            # 降噪：连失败 86 次时每帧打一行会把日志彻底淹没（用户实测就是这样），
            # 只报第一次 + 每隔 50 次汇总一条；但**第一次**要带上现场，
            # 否则事后只有一句 RequestFailed，谁也判断不出是「面板卡住」还是「面板被丢掉」。
            if f == 1:
                print(f"[overlay] ❌ setOverlayRaw 失败：{type(exc).__name__}: {exc}")
                print(f"[overlay][diag] 失败现场：{self._state_brief()}")
            if f % 50 == 0:
                print(f"[overlay] ❌ 贴图上传已连续失败 {f} 次（面板会停在最后一帧）")
                print(f"[overlay][diag] {self._state_brief()}")
            self._escalate()
            return
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

    def _recreate_overlay(self) -> bool:
        """销毁重建 overlay —— 贴图上传连续失败时用来恢复。

        SteamVR 的 `setOverlayRaw` 每次调用都会新建一张贴图，长时间高频更新后
        可能开始返回 `OverlayError_RequestFailed`（用户实测：连续失败 86 次，
        手腕屏停在最后一帧不动，之后又自己恢复）。重建能拿到一张干净的贴图；
        重建失败也不致命，下一次失败还会再试。
        """
        if self.dry_run or self._overlay is None:
            return False
        entries = list(self._last_entries or [])
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
            self._rebuilds += 1
            self._rebuilds_since_ok += 1
            self._recover_stage = "rebuild"
            self._fails_in_stage = 0
            print(f"[overlay] ♻️ 已重建 overlay 并重新贴到 {self.cfg.anchor}"
                  f"（device={self._device}，自上次成功以来第 {self._rebuilds_since_ok} 次）")
            if entries:                        # 新 overlay 是空的，立刻把内容贴回去
                self._last_entries = None
                self.update_entries(entries, force=True)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 重建 overlay 失败：{type(exc).__name__}: {exc}")
            return False

    # ---------- 自愈升级 + 诊断 ----------
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
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 硬重启 openvr 失败（下轮失败时再试）："
                  f"{type(exc).__name__}: {exc}")
            self.available = False
            return False
        self._reinits += 1
        self._reinits_since_ok += 1
        self._recover_stage = "reinit"
        self._fails_in_stage = 0
        print(f"[overlay] ♻️♻️ 已硬重启 openvr 连接并重建 overlay"
              f"（累计第 {self._reinits} 次，device={self._device}）")
        entries = list(self._last_entries or [])
        if entries:                            # 新 overlay 是空的，立刻把内容贴回去
            self._last_entries = None
            self.update_entries(entries, force=True)
        return True

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

    def _hmd_present(self) -> str:
        try:
            import openvr

            fn = getattr(openvr, "VR_IsHmdPresent", None)
            if fn is None:
                return "?"
            return "是" if fn() else "否"
        except Exception:  # noqa: BLE001
            return "?"

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
        bits = [
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
        elif self._visible_str() == "否":
            # 对象还在、只是被藏起来了（SteamVR 在某些界面切换里会这么做）——
            # 重新 show 一次就好，不必重建。
            print("[overlay] ⚠️ 面板存在但**不可见**（被 SteamVR 藏起来了）→ 重新 showOverlay")
            try:
                self._overlay.showOverlay(self._handle)
            except Exception as exc:  # noqa: BLE001
                print(f"[overlay]   showOverlay 失败：{type(exc).__name__}: {exc}")

    # ---------- 周期任务：热重载 + 淡出 ----------
    def tick(self) -> None:
        # 配置热重载（位置/尺寸改完存盘即生效，无需重启）
        if self.dry_run:
            return
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
                    if geo_changed or anchor_changed or render_changed or fade_changed:
                        last_entries = list(self._last_entries or [])
                        last_render = self._last_render
                        self.cfg = new_cfg
                        if self.available:
                            if anchor_changed:
                                self._device = self._resolve_anchor()
                            if geo_changed or anchor_changed:
                                self._apply_transform()
                            if render_changed:
                                if last_entries:
                                    self._last_entries = None      # 强制重渲
                                    self.update_entries(last_entries, force=True)
                                elif last_render:
                                    self._last_render = None
                                    self.update(last_render[0], last_render[1], force=True)
                        print(f"[overlay] ♻️ 配置热重载：anchor={new_cfg.anchor} pos={new_cfg.pos} "
                              f"rot={new_cfg.rot} width={new_cfg.width_m}m "
                              f"curvature={new_cfg.curvature} alpha={new_cfg.alpha} "
                              f"字号={new_cfg.font_size}/{new_cfg.source_font_size} "
                              f"面板={new_cfg.size_px[0]}x{new_cfg.size_px[1]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[overlay] ⚠️ 热重载失败（保留旧配置）：{exc}")

        # 健康心跳：定期把「面板还活着吗」写进日志，并在 overlay 已被 SteamVR 丢掉时重建。
        # 放在热重载与淡出之前——它是最该先出结果的一条诊断。
        if self.available:
            now = time.monotonic()
            if now - self._last_heartbeat >= self.HEARTBEAT_S:
                self._last_heartbeat = now
                try:
                    self._log_heartbeat()
                except Exception as exc:  # noqa: BLE001
                    print(f"[overlay] ⚠️ 心跳失败（不影响翻译）：{type(exc).__name__}: {exc}")

        # 可选淡出
        if self.available and self.cfg.fade_after_s > 0 and self._last_text_at:
            idle = time.monotonic() - self._last_text_at
            alpha = 0.0 if idle > self.cfg.fade_after_s else self.cfg.alpha
            try:
                self._overlay.setOverlayAlpha(self._handle, alpha)
            except Exception:
                pass

    def close(self) -> None:
        if self._overlay is not None and self._handle is not None:
            try:
                self._overlay.destroyOverlay(self._handle)
            except Exception:
                pass
        if self._vr is not None:
            try:
                import openvr

                openvr.shutdown()
            except Exception:
                pass
        self.available = False

