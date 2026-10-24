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
import time

from PIL import Image

from ..paths import APP_DIR
ROOT = APP_DIR
from .overlay import (OverlayConfig as OverlayConfig, _unpack_entry, render_conversation, render_panel,
                      OVERLAY_ERR_ANCHOR, OVERLAY_ERR_COMPOSITOR, OVERLAY_ERR_HMD,
                      OVERLAY_ERR_OTHER, OVERLAY_ERR_RUNTIME)
from .win_d3d11 import D3D11Texture as D3D11Texture

# 交给 SteamVR 的贴图色空间（OpenVR 的 EColorSpace）。Auto(=0) 实测可用；Linear 会被 SteamVR
# 拒（OverlayError_InvalidTexture），**别用**。Auto vs Gamma 的外观取舍仍等用户肉眼结论 ——
# 想试 Gamma 就把这里改成 1（openvr.ColorSpace_Gamma），改这一处即可。
# ⚠️ 前提：内容必须按 sRGB 编码进纹理（我们的渲染就是），且这块不做「线性化再输出」，
#    否则 50% 灰会明显发灰。
OVERLAY_COLOR_SPACE = 0

# 「D3D 路当前不可用」之后隔多久再试一次（TDR 复位窗口通常是百毫秒级；2s 足够稳）。
# ⚠️ 触发点在 `tick()` 而**不是** `_escalate`：设备丢失重建成败后 raw 路会成功，
#    `_escalate` 要等 raw 再攒够 3 次连续失败才被调用（按旧路特性约 200 帧/2 分钟）——
#    重试挂在 `_escalate` 上等于永远等不到（复审第 1 条就是这么抓出来的）。
TEX_RETRY_AFTER_S = 2.0

# —— 「面板整个不可用」之后的重试节奏（`available=False` 的自愈，见 `_maybe_reinit`）——
# 背景：`_hard_reinit()` 失败会置 `available=False`，而上传 / 心跳 / 热重载**全部**有
# `if not self.available` 早退 → 从此再没有任何一次上传失败去驱动 `_escalate`，
# 那句「下轮失败时再试」永远等不到：面板冻结在最后一帧、日志也安静了。
# 现在按退避重试（5s 起步、翻倍、封顶 60s），既不静默死掉，也不会变成重建风暴。
REINIT_BACKOFF_S = 5.0
REINIT_BACKOFF_MAX_S = 60.0

# 兼容模式（走 `setOverlayRaw` 旧路）下**主动**重建面板的间隔（帧）。
# 旧路每约 200 次上传必挂（`OverlayError_RequestFailed`，openvr#772 / #1565），用户可感知
# 症状是「每 ~2 分钟僵住数秒」。等它挂掉再靠自愈阶梯救 = 症状已经发生；所以改成**提前**
# 重建（帧数到点就重建，此刻屏幕上是干净的最后一帧，用户只会看到一次极短的刷新）。
RAW_REBUILD_EVERY_FRAMES = 150


def _make_texture_t(shared_handle: int):
    """构造 OpenVR 的 `Texture_t`：handle = **DXGI 共享句柄**，eType = DXGISharedHandle。

    ⚠️ 为什么不是 `TextureType_DirectX(0)` + 纹理指针：本机真 SteamVR 逐项实测（矩阵脚本
    `out/spike_openvr_texture.py --probe-matrix`）—— `eType=0` 配纹理指针 / SRV / 共享纹理
    **一律** `OverlayError_InvalidTexture`；**只有** `MISC_SHARED` 纹理的 DXGI 共享句柄配
    `eType=TextureType_DXGISharedHandle(5)` 被接受（openvr.h：该类型 "only supported for
    **Overlay** render targets"）。用户机器上 `eType=0` 表现为「不报错但屏幕上什么都不画」，
    所以**绝不能**把「调用没抛异常」当成「用户看得见」。
    """
    import openvr

    t = openvr.Texture_t()
    t.handle = ctypes.c_void_p(shared_handle)
    t.eType = openvr.TextureType_DXGISharedHandle   # =5（handle 是 IDXGIResource 的共享句柄）
    t.eColorSpace = OVERLAY_COLOR_SPACE
    return t


def _rgba_bytes(img: Image.Image) -> bytes:
    """两条上传路**共用**的字节口径：一律 RGBA。

    （原来 D3D 路 `img.convert("RGBA").tobytes()`、raw 路 `img.tobytes()` —— 渲染恒为 RGBA 时
    逐字节等价，但口径不对称：哪天进来非 RGBA 图，炸的是 raw 路。这里统一，且已是 RGBA 时
    不多做一次整图拷贝。）
    """
    return (img if img.mode == "RGBA" else img.convert("RGBA")).tobytes()

from .wrist_maintenance import _WristMaintenance
from ..session.chatgpt_output import allowed, current, same

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
    "InitError_IPC_CompositorConnectFailed":
        "SteamVR 在跑，但它的合成器（vrcompositor）没起来 —— 手腕屏面板必须由合成器显示。"
        "把 SteamVR 完全退出后重新启动一次；不行就重启电脑",
}


def _no_server_hint() -> str:
    """121 的**两种形态**只能从 SteamVR 自己的客户端日志里分辨，日志里说清去哪看。"""
    return ("排查：SteamVR 日志目录里的 `vrclient_<程序名>.txt` 会写明是哪种 —— "
            "「no one is listening」= SteamVR 没在跑；"
            "「信号灯超时 / semaphore timeout」= SteamVR 在跑但命名管道不应答"
            "（重启 SteamVR；不行就重启电脑）")


# ------------------------------------------------- 失败原因：给界面一个**可翻译的码**
# 界面（状态栏）只认码；完整原因由下面的中文提示负责（日志保持中文，诊断用）。
_RUNTIME_ERRORS = (
    "InitError_Init_NoServerForBackgroundApp", "InitError_Init_InstallationNotFound",
    "InitError_Init_VRClientDLLNotFound", "InitError_Init_VRMonitorNotFound",
    "InitError_Init_VRMonitorStartupFailed", "InitError_Init_AnotherAppLaunching",
    "InitError_Init_ShuttingDown", "InitError_Init_UserConfigDirectoryInvalid",
    "InitError_Init_PathRegistryNotFound", "InitError_Init_PathRegistryNotWritable",
    "InitError_Init_NoLogPath", "InitError_Init_Retry",
)
_HMD_ERRORS = ("InitError_Init_HmdNotFound", "InitError_Init_HmdNotFoundPresenceFailed")
# 合成器（vrcompositor）没起来：overlay 接口连不上它 → 面板根本建不出来。
# 实测形态（本机 null 驱动、SteamVR 明明在跑）：
#   [overlay] ⚠️ 创建 overlay 失败…：InitError_IPC_CompositorConnectFailed:
# 与「SteamVR 没跑」解药不同（那句「先启动 SteamVR」在这条上纯误导）。
_COMPOSITOR_ERRORS = ("InitError_IPC_CompositorConnectFailed", "Compositor")


def _reason_code(text: str) -> str:
    """把 openvr 的错误类名 / 异常文本归成界面能翻译的原因码（码定义见 `output/overlay.py`）。"""
    blob = str(text or "")
    if "invalidtrackeddevice" in blob.lower():
        return OVERLAY_ERR_ANCHOR
    if any(name in blob for name in _HMD_ERRORS):
        return OVERLAY_ERR_HMD
    if any(name in blob for name in _COMPOSITOR_ERRORS):
        return OVERLAY_ERR_COMPOSITOR
    if any(name in blob for name in _RUNTIME_ERRORS):
        return OVERLAY_ERR_RUNTIME
    return OVERLAY_ERR_OTHER


# ---------------------------------------------------------------- overlay 本体
from .wrist_startup import _WristStartup
from .wrist_texture import _WristTexture
from .wrist_recovery import _WristRecovery


class WristOverlay(_WristStartup, _WristTexture, _WristRecovery, _WristMaintenance):
    """SteamVR overlay 生命周期管理 + 文本刷新 + 配置热重载。"""

    # 健康心跳间隔（秒）：日志里留一条「面板还活着 / 已经多久没成功上传」的定期证据。
    # 用户报的「隔一阵手腕屏就消失」没有这行时，事后完全看不出是哪个时刻掉的。
    HEARTBEAT_S = 30.0

    #: 锚点设备（手柄 / tracker）还没被追踪时，每隔这么久重试一次「应用变换」。
    #: 只跑在 `tick()` 里（见 `_maybe_apply_anchor`）：面板已经建好、内容也照常刷，
    #: 差的只是「贴到哪台设备上」—— 设备一就绪面板自己出现，不需要用户再勾一次。
    ANCHOR_RETRY_S = 1.0
    #: 每多少次重试才留一行痕（禁静默 + 不淹日志）。
    ANCHOR_RETRY_LOG_EVERY = 30


    @property
    def alive(self) -> bool:
        """现在还真的在接管 SteamVR 吗？

        ⚠️ `available` 单独不够：`close()` 之后 handle 已经销毁，而界面可能仍持有这个对象
        （勾选与停止是两条路），那种「已关但还在」的实例必须算不可用。
        界面据此决定「勾选时要不要重建」（见 `vlt/gui_sinks.py::start_overlay`）——
        旧写法只看 `overlay_out is not None`，于是已死的实例让勾选框「点了什么都不做」。
        """
        return bool(self.available) and not self._closed

    # ---------- 生命周期 ----------

    def _anchor_hint(self) -> str:
        """锚点设备无效时的可行动解释（真机实测：手柄/tracker 不在线就会这样）。"""
        return (f"锚点设备（{self.cfg.anchor}）没被 SteamVR 追踪：手柄 / tracker 需要在追踪中"
                f"（戴上头显、唤醒手柄后再勾一次；也可在「设置 → 手腕屏」里改锚点试）")

    def _release_handle(self) -> None:
        """销毁我们这把 key 的 overlay（幂等、绝不抛异常）。失败路径专用，防残留 KeyInUse。"""
        if self._overlay is None or self._handle is None:
            return
        try:
            self._overlay.destroyOverlay(self._handle)
            print("[overlay]   已销毁本次创建的 overlay（不留残留 key）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay]   清理本次 overlay 失败（忽略）：{type(exc).__name__}: {exc}",
                  flush=True)
        self._handle = None

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
        self._set_alpha(self.cfg.alpha)
        # 弯曲每次都应用：只在 >0 时设置的话，把它调回 0 就永远回不去了
        self._overlay.setOverlayCurvature(self._handle, max(0.0, min(1.0, self.cfg.curvature)))

    # ---------- 文本 ----------
    def update(self, text: str, source: str = "", force: bool = False) -> None:
        """刷新面板内容（相同内容不重复上传贴图）。"""
        epoch = self._epoch
        text, source = text.strip(), source.strip()
        approval = current()
        if epoch != self._epoch or not text or not allowed(approval):
            return
        key = (text, source if self.cfg.show_source else "")
        if (key == self._last_render and not force
                and self._content is not None and self._content[3] == epoch
                and same(self._content[2], approval)):
            return
        self._content = ('update', (text, source), approval, epoch)
        self._last_render = key
        self._last_entries = None
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
        self._upload(img, epoch)

    def update_entries(self, entries, force: bool = False) -> None:
        """刷新成**对话视图**（GUI 用这个）：entries = [(who, source, translation), ...]
        或 4 元组 [(who, source, translation, label), ...]（label = 说话人昵称）。

        与 `update()` 的区别：`update()` 是"当前这一句"（CLI 单腿场景够用），
        `update_entries()` 是"最近几句对话"——手腕上只有一块屏，内容应该像 GUI 的聊天区。
        """
        # ⚠️ 必须走 `_unpack_entry`：房间链路会传 4 元组，直接解包 3 个会 ValueError，
        # 把整条手腕屏刷新打死（合并 main 时这行曾随 overlay.py 拆分而丢失过一次）。
        epoch, approval = self._epoch, current()
        if not allowed(approval):
            return
        key = tuple((w, (s or "").strip(), (t or "").strip(), (lab or "").strip())
                    for w, s, t, lab in (_unpack_entry(e) for e in (entries or [])))
        if (key == self._last_entries and not force
                and self._content is not None and self._content[3] == epoch
                and same(self._content[2], approval)):
            return
        self._content = ('update_entries', (key,), approval, epoch)
        self._last_entries = key
        self._last_render = None
        self._last_text_at = time.monotonic()
        img = render_conversation(key, self.cfg)
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
        self._upload(img, epoch)


    # ---------- D3D11 持久贴图路径：启用 / 绑定 / 重建 / 回退 ----------


    # ---------- 「面板整个不可用」（available=False）的退避自愈 ----------


    # ---------- 自愈升级 + 诊断 ----------


    def close(self) -> None:
        self._epoch += 1
        self._content = None
        self._uploaded_approval = None
        self._alpha_retry = False
        self._last_entries = self._last_render = None
        self._closed = True                # 之后 `alive` 恒为假 + 不再自愈（见 _maybe_reinit）
        self.pending_anchor = False        # 取消勾选后不许「自己出现」（见 _maybe_apply_anchor）
        self._reinit_at = None             # 已排定的「重试接管」也一并作废（见 _maybe_reinit）
        # ⚠️ D3D 路的定点重试同样要作废：它的守卫星是 `_closed` + 这个时间点**两道**，
        #    只留一道时「已排定重试 + 取消勾选」仍能让下一次 tick 建出一个新 D3D 设备
        #    （见 _maybe_retry_tex_path；复审 #99 第 2 条）。
        self._tex_retry_at = None
        if self._overlay is not None and self._handle is not None:
            try:
                self._overlay.destroyOverlay(self._handle)
            except Exception:
                pass
            self._handle = None
        if self._vr is not None:
            try:
                import openvr

                openvr.shutdown()
            except Exception:
                pass
            self._vr = None
        self._close_d3d()          # 释放持久 D3D11 设备/纹理（幂等，raw 路径下是 no-op）
        self.available = False

