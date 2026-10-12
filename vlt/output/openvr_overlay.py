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
from pathlib import Path

from PIL import Image

from ..paths import APP_DIR as ROOT
from .overlay import (OverlayConfig, _unpack_entry, render_conversation, render_panel,
                      OVERLAY_ERR_ANCHOR, OVERLAY_ERR_COMPOSITOR, OVERLAY_ERR_HMD,
                      OVERLAY_ERR_OTHER, OVERLAY_ERR_RUNTIME)
from .win_d3d11 import D3D11Texture

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
class WristOverlay:
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
        # ---- D3D11 持久贴图 + SetOverlayTexture 上传路径（见 _init_texture_path）----
        self._d3d = None                   # win_d3d11.D3D11Texture；None = 走旧的 setOverlayRaw
        self._tex_disabled = False         # setOverlayTexture 报「不支持」后置真 → 永久回退旧路
        self._tex_fallback_logged = False  # 永久回退只留痕一次（别每帧刷）
        self._tex_retried = False          # 「D3D 路不可用」后**定点重试**一次的机会（整个会话只给一次）
        self._tex_retry_at = None          # 定点重试的时间点（monotonic；由 _schedule_tex_retry 设置）
        self._last_cfg_mtime = 0.0
        self._last_text_at = 0.0
        # ---- 兼容模式（旧路 setOverlayRaw）下的主动重建 + 降级留痕 ----
        self._raw_frames = 0               # 兼容模式下自上次重建以来成功刷新的帧数（见 RAW_REBUILD_EVERY_FRAMES）
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
            self.last_error = OVERLAY_ERR_OTHER
            return False
        if self.dry_run:
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            print(f"[overlay][dry-run] 不接管 SteamVR，渲染结果写到 {self._frames_dir}")
            return False
        try:
            import openvr
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] openvr 不可用：{exc}")
            self.last_error = OVERLAY_ERR_OTHER
            return False
        try:
            self._vr = openvr.init(openvr.VRApplication_Background)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 打**类名**：openvr 异常的 str() 恒为空，只打 `{exc}` 等于没有信息
            name = type(exc).__name__
            self.last_error = _reason_code(name)
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
                if _reason_code(f"{type(exc).__name__}{exc}") != OVERLAY_ERR_ANCHOR:
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
            self.last_error = _reason_code(f"{name}{exc}")
            print(f"[overlay] ⚠️ 创建 overlay 失败，已禁用（其他输出不受影响）："
                  f"{name}: {exc}")
            # 真因要能行动：锚点设备没被追踪 ≠ SteamVR 没开（用户照「先把 SteamVR 打开」
            # 重试是白费力气，实测就是这么报的：OverlayError_InvalidTrackedDevice）
            if self.last_error == OVERLAY_ERR_ANCHOR:
                print(f"[overlay]   原因：{self._anchor_hint()}", flush=True)
            else:
                print(f"[overlay]   原因："
                      f"{_INIT_ERROR_HINTS.get(name, '未知原因（请把这一行连同上下文发给我们）')}",
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
        """把一帧送上手腕屏。有 D3D 层走 SetOverlayTexture，否则走旧的 setOverlayRaw。

        两条路**共用同一套失败计数与自愈阶梯**（`_upload_fails` / `_fails_in_stage` /
        `_escalate`）——不新造一套，否则「连续失败 → 重建 → 硬重启」的行为会在两条路上漂移。
        """
        if self._d3d is not None:
            self._upload_texture(img)
        else:
            self._upload_raw(img)

    def _upload_raw(self, img: Image.Image) -> None:
        """旧路（降级兜底）：`setOverlayRaw` 每帧新建一张贴图，高频刷新约 200 次后会撞墙。

        ⚠️ 这条路的性质是**已知缺陷**（见模块 docstring / openvr#772）：我们不用「等它挂了
        再自愈」，而是按帧数**主动**重建（`RAW_REBUILD_EVERY_FRAMES`），把「僵住数秒」换
        成「一次极短的刷新」。
        """
        w, h = img.size
        data = _rgba_bytes(img)            # 与 D3D 路**同一口径**（见 _rgba_bytes）
        buf = ctypes.create_string_buffer(data, len(data))
        try:
            self._overlay.setOverlayRaw(self._handle, buf, w, h, 4)
        except Exception as exc:  # noqa: BLE001
            self._on_upload_failure(exc, "setOverlayRaw")
            return
        self._on_upload_success(w, h)
        self._raw_frames += 1
        if self._raw_frames >= RAW_REBUILD_EVERY_FRAMES:
            # 先清零再重建：重建内部会重贴一帧（force=True）→ 不清零就会立刻再触发一次
            self._raw_frames = 0
            print(f"[overlay] ♻️ 兼容模式：已连续刷新 {RAW_REBUILD_EVERY_FRAMES} 帧 → "
                  f"主动重建面板（旧路约 200 次上传必挂，提前换掉以免僵住数秒）")
            self._recreate_overlay()

    def _upload_texture(self, img: Image.Image) -> None:
        """新路：只更新持久 D3D11 纹理的内容，再把它交给 `setOverlayTexture`（不新建任何贴图）。"""
        w, h = img.size
        # D3D11 上传与 overlay 提交是**两类**问题，必须分开 try：合并成一段时，D3D 侧的
        # `Map(源纹理) 失败 hr=0x887A0005` 会被报成「setOverlayTexture 失败」，排障方向被带偏
        # （实测踩过）。op 名进日志，`_on_upload_failure` 才能报出真实那一步。
        steps = (("D3D11 upload", lambda: self._d3d.upload(_rgba_bytes(img))),
                 ("setOverlayTexture", self._set_overlay_texture))
        for op, call in steps:
            try:
                call()
            except Exception as exc:  # noqa: BLE001
                # 「不支持 / InvalidTexture」类错误 = 这条路在这台机器/这个 SteamVR 上根本走不通，
                # 重试没意义 → **永久回退** setOverlayRaw（只留痕一次），并立刻用旧路补上这一帧。
                if self._is_texture_unsupported(exc):
                    self._fallback_to_raw_permanently(exc)
                    self._upload_raw(img)
                    return
                # 其它错误（设备丢失 / RequestFailed 等）按现有自愈阶梯走。
                self._on_upload_failure(exc, op)
                return
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

    # ---------- D3D11 持久贴图路径：启用 / 绑定 / 重建 / 回退 ----------
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
          * 非 Windows：`D3D11Texture(...)` 构造即因 `WinDLL` 缺失抛异常 → 落到下面的降级分支；
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
            self._d3d = D3D11Texture(w, h)
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
              f"色空间={OVERLAY_COLOR_SPACE}）")

    def _set_overlay_texture(self) -> None:
        """把当前 D3D 纹理交给 SteamVR。重建/硬重启后**新 handle 上没有任何贴图**，必须重调。"""
        if self._d3d is None:
            return
        self._overlay.setOverlayTexture(self._handle, _make_texture_t(self._d3d.shared_handle))

    def _schedule_tex_retry(self) -> None:
        """记下「隔一会儿再试一次 D3D 路」的时间点（`_maybe_retry_tex_path` 会在 tick 里兑现）。

        两种入口都会调它：① `start()` 时 `_init_texture_path()` 就失败；② 设备丢失后那次重建失败。
        真死 GPU 也不会反复建设备 —— 兑现代价是 `_tex_retried`，整个会话只放行一次。
        """
        if not self._tex_retried and not self._tex_disabled:
            self._tex_retry_at = time.monotonic() + TEX_RETRY_AFTER_S

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

    # ---------- 「面板整个不可用」（available=False）的退避自愈 ----------
    def _schedule_reinit(self, reason: str) -> None:
        """排下一次「重新接管 SteamVR」的时间点（退避翻倍、封顶 `REINIT_BACKOFF_MAX_S`）。

        ⚠️ 为什么必须有它：`available=False` 时上传/心跳/热重载**全部**早退，再也没有一次
        上传失败去驱动 `_escalate()` —— `_hard_reinit` 里那句「下轮失败时再试」永远等不到，
        面板冻结在最后一帧。重试因此由 `tick()` 驱动（见 `_maybe_reinit`）。
        退避保证不会变成重建风暴（真死 GPU / SteamVR 没起来时，5s→10s→20s→40s→60s…）。
        """
        if self.dry_run:
            return
        self._reinit_fails += 1
        delay = min(REINIT_BACKOFF_MAX_S, REINIT_BACKOFF_S * (2 ** (self._reinit_fails - 1)))
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
            self._d3d = D3D11Texture(w, h)
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
                  f"{RAW_REBUILD_EVERY_FRAMES} 帧主动重建来避开，仍可能看到一次极短的刷新")
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
            self._rebind_or_rebuild("已重建 overlay")   # 新 handle 上没有任何贴图，必须重绑/重建
            self._rebuilds += 1
            self._rebuilds_since_ok += 1
            self._recover_stage = "rebuild"
            self._fails_in_stage = 0
            self._raw_frames = 0          # 兼容模式的主动重建计数跟着清零（新 overlay = 新贴图配额）
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
            self.last_error = _reason_code(f"{type(exc).__name__}{exc}")
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
        mode = "持久贴图" if self._d3d is not None else ("兼容旧路" if not self._tex_disabled
                                                       else "兼容旧路(永久)")
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
        elif self._visible_str() == "否":
            # 对象还在、只是被藏起来了（SteamVR 在某些界面切换里会这么做）——
            # 重新 show 一次就好，不必重建。
            print("[overlay] ⚠️ 面板存在但**不可见**（被 SteamVR 藏起来了）→ 重新 showOverlay")
            try:
                self._overlay.showOverlay(self._handle)
            except Exception as exc:  # noqa: BLE001
                print(f"[overlay]   showOverlay 失败：{type(exc).__name__}: {exc}")

    # ---------- 周期任务：热重载 + 淡出 ----------
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

    def tick(self) -> None:
        # 配置热重载（位置/尺寸改完存盘即生效，无需重启）
        if self.dry_run:
            return
        self._maybe_apply_anchor()         # 锚点设备还没被追踪 → 定期重试贴上去（面板先留着）
        self._maybe_retry_tex_path()       # D3D 路曾不可用 → 到点再试一次（不依赖上传失败）
        self._maybe_reinit()               # 面板整个不可用 → 退避重试接管（同上，别挂在失败路径上）
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
                    # 面板像素尺寸单独拎出来：D3D 路径的持久纹理是按 size_px 建的，尺寸一变
                    # 就得**重建纹理**（否则 upload 的字节数对不上会 ValueError）。必须在
                    # self.cfg 被换掉之前算，且重建要排在重渲那一帧之前。
                    size_changed = new_cfg.size_px != self.cfg.size_px
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
                                # 尺寸变了且走 D3D 路径 → 先重建纹理，再重渲那一帧
                                if size_changed and self._d3d is not None:
                                    self._rebuild_d3d("size_px 变更")
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
        # ⚠️ 心跳**不看 available**：面板不可用时最需要它来留痕（`available=False` 之后
        #    其余路径全早退，日志会彻底安静，事后完全看不出是什么时候死的）。
        # ⚠️ 但**要看 `_closed`**：已取消勾选的实例再打心跳只会刷日志、还多调一次
        #    `findOverlay`（复审第 2 条：与 `_maybe_*` 的 `_closed` 纪律不对称）。
        now = time.monotonic()
        if not self._closed and now - self._last_heartbeat >= self.HEARTBEAT_S:
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

