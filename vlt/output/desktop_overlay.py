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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

# 调试帧是"跑完要看"的产物 → 可写目录（exe 旁），与手腕屏的 dry-run 同一个口径
from ..paths import APP_DIR
from ..platform.overlay_pixels import alpha_mask_bits
from .overlay import OverlayConfig, _unpack_entry, render_conversation, render_panel

# 色键：窗口底色与贴图底色必须是**同一个**颜色，Tk 才会把那一片抠成透明。
# 选 (1,2,3) 是因为它离面板配色足够远（不会被误抠），又不是纯黑（纯黑会与文字抗锯齿边打架）。
TRANSPARENT_KEY = "#010203"
KEY_RGB = (1, 2, 3)                     # ⚠️ 必须与 TRANSPARENT_KEY 一致

ANCHORS = ("top_left", "top_center", "top_right",
           "middle_left", "center", "middle_right",
           "bottom_left", "bottom_center", "bottom_right", "free")

MODES = ("conversation", "latest")

# 窗口后端：auto=有原生就用、没有则回落 Tk（Tk 是正常路径之一，不留警告）；
# native=显式要求原生（建不起来时留一行警告再回落）；tk=强制 Tk；
# wayland / x11=强制某一条原生后端（不可用回落 Tk 并留日志）。
# ⚠️ 启动时生效；运行中改这条需要重启桌面字幕那条腿。
BACKENDS = ("auto", "native", "tk", "wayland", "x11")

ALPHA_MIN, ALPHA_MAX, ALPHA_DEFAULT = 0.2, 1.0, 0.9

# 锚点 → (水平, 垂直) 的**分子**（分母恒为 2）：0=贴起始边、1=居中、2=贴结束边。
# 用整数分数而不是 0.5 浮点，是为了让结果与面板/窗口尺寸的奇偶无关（不会出现 .5 抖动）。
_ANCHOR_GRID: dict[str, tuple[int, int]] = {
    "top_left": (0, 0), "top_center": (1, 0), "top_right": (2, 0),
    "middle_left": (0, 1), "center": (1, 1), "middle_right": (2, 1),
    "bottom_left": (0, 2), "bottom_center": (1, 2), "bottom_right": (2, 2),
}

_MISSING = object()


# 桌面字幕**自带**的视觉键（配色 / 底板与边框透明度 / 分隔线 / 说话人竖条色）。
# 默认值取 OverlayConfig 的**代码默认值** —— 不是用户 config.yaml 里的 `overlay:` 段：
# 两个面板长得像，但配置完全独立（2026-10-04 维护者口径：两者区别太大，不能共享配置）。
_VISUAL_FIELDS = ("color_translation", "color_source", "source_alpha", "color_border",
                  "border_alpha", "color_bg", "bg_alpha", "color_mine", "color_theirs",
                  "separator", "fade_after_s")


def desktop_visual_defaults() -> dict:
    """桌面字幕的视觉默认值（配色等）—— 取自 OverlayConfig 的**代码**默认值。

    用户改 `overlay:` 段（手腕屏）不会牵动这里，反过来也一样。
    """
    base = OverlayConfig()
    return {k: getattr(base, k) for k in _VISUAL_FIELDS}


# ---------------------------------------------------------------- 配置
@dataclass
class DesktopOverlayConfig:
    """桌面字幕窗的配置（**完全独立于 `overlay:` 段**）。

    ⚠️ 桌面面板与头显里的手腕屏长得像，但**两者不是一回事**（尺寸单位、使用场景、
    屏幕分辨率都不同），所以配置不许共享：本段自带一整套视觉键（字体 / 字号 / 配色 /
    行数 / 是否显示原文 / 面板尺寸），缺省时回落 **OverlayConfig 的代码默认值**。
    维护者口径（2026-10-04）：原来的「视觉参数默认继承 `overlay:` 段」打回重做。
    """

    enabled: bool = False
    mode: str = "conversation"            # conversation=镜像聊天区 | latest=只显示最新一句（歌词式）
    backend: str = "auto"                 # 窗口后端，见 BACKENDS（启动时生效）
    attach_to_game: bool = True           # True=贴在目标窗口上并跟随
    anchor: str = "bottom_center"         # 见 ANCHORS；free=用 pos 绝对定位
    offset: tuple[int, int] = (0, -48)    # 相对锚点的像素偏移
    pos: tuple[int, int] = (80, 80)       # attach_to_game=False / anchor="free" 时的屏幕坐标
    size_px: tuple[int, int] = (1024, 360)
    alpha: float = ALPHA_DEFAULT          # 整窗透明度（用户要的「可改透明度」），0.2~1.0
    click_through: bool = True            # 鼠标穿透（拖动时临时关掉）
    mode_pill: bool = True                # 面板右上角外侧的「原声/译音」胶囊（独立小窗、不抢焦点）
    follow: bool = True                   # 游戏窗口移动/缩放时跟随
    game_title: str = "VRChat"
    # ---- 视觉参数：**全在本段**，不从 overlay 段继承 ----
    font: str = ""                        # 空 = 按平台自动探测（见 overlay.resolve_font_path）
    font_size: int = 36
    source_font_size: int = 29
    max_lines: int = 3
    show_source: bool = True
    # 其余视觉键（配色 / 底板与边框透明度 / 分隔线 / 说话人竖条色）也全在本段：
    # 这里只装**本段显式写出的**那部分，渲染时缺的用 desktop_visual_defaults() 补。
    visual: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> "DesktopOverlayConfig":
        """解析 `desktop_overlay:` 段。

        ⚠️ **不接收也不读 `overlay:` 段**：两个面板的配置完全独立（见类文档）。
        每个键缺省时回落本 dataclass 的默认值（视觉键回落 OverlayConfig 的代码默认值）。
        """
        d = dict(d or {})
        base = DesktopOverlayConfig()

        def own(key: str, default: Any, cast: Any = None) -> Any:
            """只认本段（`desktop_overlay:`）的键。"""
            return _pick(d, key, default, cast)

        anchor = str(own("anchor", base.anchor, _as_str) or base.anchor)
        if anchor not in ANCHORS:
            print(f"[desktop] ⚠️ 配置里的 anchor={anchor!r} 不认识（可选 {ANCHORS}）"
                  f" → 用 {base.anchor}")
            anchor = base.anchor
        mode = str(own("mode", base.mode, _as_str) or base.mode)
        if mode not in MODES:
            print(f"[desktop] ⚠️ 配置里的 mode={mode!r} 不认识（可选 {MODES}）"
                  f" → 用 {base.mode}")
            mode = base.mode
        # backend 只看本段：`overlay.backend`（auto/null）是手腕屏那套，语义完全不同，
        # 读过来会让桌面字幕跟着手腕屏的开关走，属于配置语义漂移。
        backend = str(own("backend", base.backend, _as_str) or base.backend)
        if backend not in BACKENDS:
            print(f"[desktop] ⚠️ 配置里的 backend={backend!r} 不认识（可选 {BACKENDS}）"
                  f" → 用 {base.backend}")
            backend = base.backend

        cfg = DesktopOverlayConfig(
            enabled=bool(own("enabled", base.enabled, _as_bool)),
            mode=mode,
            backend=backend,
            attach_to_game=bool(own("attach_to_game", base.attach_to_game, _as_bool)),
            anchor=anchor,
            offset=own("offset", base.offset, _as_pair),
            pos=own("pos", base.pos, _as_pair),
            size_px=own("size_px", base.size_px, _as_pair),
            alpha=clamp_alpha(own("alpha", base.alpha)),
            click_through=bool(own("click_through", base.click_through, _as_bool)),
            mode_pill=bool(own("mode_pill", base.mode_pill, _as_bool)),
            follow=bool(own("follow", base.follow, _as_bool)),
            game_title=str(own("game_title", base.game_title, _as_str) or base.game_title),
            font=str(own("font", base.font, _as_str) or ""),
            font_size=own("font_size", base.font_size, _as_int),
            source_font_size=own("source_font_size", base.source_font_size, _as_int),
            max_lines=own("max_lines", base.max_lines, _as_int),
            show_source=bool(own("show_source", base.show_source, _as_bool)),
        )
        # 视觉键（配色等）同样只认本段：整段原样收下，渲染时用代码默认值补缺。
        cfg.visual = {k: v for k, v in d.items() if k in _VISUAL_FIELDS}
        return cfg

    def visual_config(self) -> OverlayConfig:
        """渲染用的视觉配置：**本段自己的**配色/底板/字体 + 尺寸/字号/行数。

        ⚠️ 不从 `overlay:` 段取任何东西 —— 两个面板的观感各自独立。
        """
        d = desktop_visual_defaults()
        d.update(self.visual or {})
        d.update({
            "enabled": True,
            "size_px": list(self.size_px),
            "alpha": self.alpha,
            "font": self.font,
            "font_size": self.font_size,
            "source_font_size": self.source_font_size,
            "max_lines": self.max_lines,
            "show_source": self.show_source,
        })
        return OverlayConfig.from_dict(d)


def _pick(d: dict, key: str, default: Any, cast: Any) -> Any:
    """取值：本段写了（且不是 None）就转成目标类型，否则用 `default`。

    ⚠️ 这里**没有**「继承另一个段」的口子 —— 桌面字幕与 `overlay:`（手腕屏）
    的配置完全独立（2026-10-04 维护者口径）。别再加回来。
    """
    raw = d.get(key, _MISSING)
    if raw is _MISSING or raw is None:
        return default
    if cast is None:
        return raw
    try:
        return cast(raw)
    except (TypeError, ValueError):
        # 配置是用户手改的：值不合法要**说出来**再回落，不能悄悄换个值让人猜
        print(f"[desktop] ⚠️ 配置项 {key}={raw!r} 不合法 → 用默认值 {default!r}")
        return default


def _as_pair(raw: Any) -> tuple[int, int]:
    vals = [int(v) for v in raw]
    if len(vals) != 2:
        raise ValueError(f"需要两个整数，实际 {len(vals)} 个")
    return (vals[0], vals[1])


def _as_int(raw: Any) -> int:
    return int(raw)


def _as_str(raw: Any) -> str:
    return str(raw)


def _as_bool(raw: Any) -> bool:
    return bool(raw)


# ---------------------------------------------------------------- 纯函数（离线可测）
def clamp_alpha(a: Any) -> float:
    """整窗透明度夹到 0.2~1.0；非法值（None / "abc" / NaN）回落 0.9。

    下限不是 0：全透明的字幕窗用户会以为"功能坏了"，0.2 至少还看得见个影子。
    """
    try:
        v = float(a)
    except (TypeError, ValueError):
        return ALPHA_DEFAULT
    if v != v:                                  # NaN
        return ALPHA_DEFAULT
    return max(ALPHA_MIN, min(ALPHA_MAX, v))


def _clamp_into_area(x: int, y: int, w: int, h: int,
                     area: tuple[int, int, int, int]) -> tuple[int, int]:
    """把 (x, y) 夹进可用屏幕区域。面板比区域还大时贴区域左上角（至少看得见）。"""
    left, top, right, bottom = (int(v) for v in area)
    if w >= right - left:
        x = left
    else:
        x = max(left, min(int(x), right - w))
    if h >= bottom - top:
        y = top
    else:
        y = max(top, min(int(y), bottom - h))
    return (int(x), int(y))


def compute_position(anchor: str, offset: tuple[int, int],
                     rect: tuple[int, int, int, int],
                     size: tuple[int, int],
                     area: tuple[int, int, int, int]) -> tuple[int, int]:
    """按锚点把面板贴到目标窗口客户区上，加偏移，最后夹进屏幕可用区域。

    `rect`=(left, top, right, bottom) 目标窗口客户区（屏幕坐标）；
    `size`=(w, h) 面板像素尺寸；`area`=(l, t, r, b) 可用屏幕区域（工作区，不含任务栏）。

    不认识的锚点按 `center` 处理（面板不会因此跑到屏幕外）。
    """
    left, top, right, bottom = (int(v) for v in rect)
    w, h = (int(v) for v in size)
    fx, fy = _ANCHOR_GRID.get(str(anchor or ""), _ANCHOR_GRID["center"])
    ox, oy = _as_pair(offset)
    x = left + ((right - left - w) * fx) // 2 + ox
    y = top + ((bottom - top - h) * fy) // 2 + oy
    return _clamp_into_area(x, y, w, h, area)


def best_anchor(pos: tuple[int, int], rect: tuple[int, int, int, int],
                size: tuple[int, int]) -> tuple[str, tuple[int, int]]:
    """把「拖到某处的绝对坐标」反算成 `(锚点, 偏移)`。

    拖动后要落盘的必须是「贴着窗口的哪个锚点 + 多大的偏移」，**不能存死坐标** ——
    否则 VRChat 窗口一移动/改分辨率，字幕就与它脱钩了（用户拖动时并不知道自己在
    「贴窗」）。取**离落点最近**的那个九宫格点（偏移向量最短），这样窗口缩放时字幕
    仍然贴着同一条边。
    """
    left, top, right, bottom = (int(v) for v in rect)
    w, h = (int(v) for v in size)
    px, py = (int(v) for v in pos)
    best: tuple[int, str, int, int] = (1 << 62, "center", 0, 0)
    for name, (fx, fy) in _ANCHOR_GRID.items():
        bx = left + ((right - left - w) * fx) // 2
        by = top + ((bottom - top - h) * fy) // 2
        ox, oy = px - bx, py - by
        d = ox * ox + oy * oy
        if d < best[0]:
            best = (d, name, ox, oy)
    return best[1], (best[2], best[3])


def resolve_position(cfg: DesktopOverlayConfig,
                     rect: tuple[int, int, int, int] | None,
                     size: tuple[int, int],
                     area: tuple[int, int, int, int]) -> tuple[int, int]:
    """定位的统一入口。

    没有目标窗口（`rect is None`）、`anchor == "free"`、或 `attach_to_game=False`
    → 用 `cfg.pos` 的屏幕绝对坐标（同样夹进 area）；否则按锚点贴到目标窗口上。
    """
    if rect is None or cfg.anchor == "free" or not cfg.attach_to_game:
        x, y = _as_pair(cfg.pos)
        w, h = (int(v) for v in size)
        return _clamp_into_area(x, y, w, h, area)
    return compute_position(cfg.anchor, cfg.offset, rect, size, area)


# 「原声/译音」胶囊与面板之间的缝隙（外置时在面板上沿之上，内置时从面板上沿往里缩）
PILL_GAP = 8


def pill_position(panel_pos: tuple[int, int], panel_size: tuple[int, int],
                  pill_size: tuple[int, int],
                  area: tuple[int, int, int, int]) -> tuple[int, int]:
    """胶囊贴在**面板右上角外侧**；顶部放不下时退回面板**内部**右上角。

    `panel_pos`=(x, y) 面板当前屏幕坐标；`panel_size`=(w, h) 面板像素尺寸；
    `pill_size`=(w, h) 胶囊像素尺寸；`area`=(l, t, r, b) 屏幕可用区域。

    外侧优先：x 与面板右边对齐，y 在面板上沿再往上一个 :data:`PILL_GAP` —— 不挡字幕。
    算出来的 y 顶出工作区（面板本身就贴着屏幕顶）→ 挪到面板内部右上角（内缩 PILL_GAP），
    否则胶囊会被屏幕边缘吃掉一半。最后统一夹进工作区。
    """
    px, py = (int(v) for v in panel_pos)
    pw, _ph = (int(v) for v in panel_size)
    w, h = (int(v) for v in pill_size)
    top = int(area[1])
    x = px + pw - w
    y = py - h - PILL_GAP
    if y < top:                                   # 上面没地方了 → 放进面板内部右上角
        x, y = x - PILL_GAP, py + PILL_GAP
    return _clamp_into_area(x, y, w, h, area)


# ---------------------------------------------------------------- 窗口本体
class DesktopOverlay:
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
        self._last_cfg_mtime = 0.0
        self._next_search = 0.0
        self._area_fallback_logged = False
        self._rect_missing_logged = False
        self._exstyles_dirty = False
        self._need_shape_mask = False     # Tk 回落 + 没色键（Linux）→ 每帧用形状蒙版抠底
        self._shape_warned = False
        # ---- 「原声/译音」胶囊（独立的小 Toplevel：字幕窗是穿透的，里面画按钮点不着）----
        self._pill: Any = None            # tk.Toplevel；None = 还没建 / 已销毁
        self._pill_label: Any = None
        self._pill_hwnd = 0
        self._pill_text = ""              # 当前胶囊文字（dry-run 也记，供诊断/测试）
        self._pill_cb: Any = None         # 点胶囊时调的回调（界面层注入）
        self._pill_exstyles_dirty = False  # Tk 映射后扩展样式会被重写 → 下一跳补一次
        self._pill_disabled_logged = False
        self._pill_unavailable_logged = False

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

    def _apply_exstyles(self) -> None:
        """鼠标穿透 + 不抢焦点 + 不进 alt-tab（能力在平台门面上，缺就降级并留痕）。"""
        if self.dry_run:
            return
        want = bool(self.cfg.click_through) and not self._dragging
        if self._native is not None:
            # 原生 Wayland 窗：键盘交互在建面时就是 none（不抢焦点）；穿透 = 输入区置空。
            self._native.set_click_through(want)
            return
        if self._win is None:
            return
        from ..platform import set_click_through, set_tool_window
        if not self._hwnd:
            self._resolve_hwnd()
        if not self._hwnd:
            return
        if not set_tool_window(self._hwnd):
            print("[desktop] ⚠️ 「不抢焦点/不进任务栏」没设上（本平台可能不支持）"
                  " → 点字幕可能把焦点从游戏里抢走")
        if not set_click_through(self._hwnd, want):
            print(f"[desktop] ⚠️ 鼠标穿透（{'开' if want else '关'}）没设上"
                  f"（本平台可能不支持） → 字幕会挡住鼠标")

    def close(self) -> None:
        """销毁自己的窗口；自建的 root 一并销毁。重复调用不报错。"""
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
        self._destroy_pill()               # 胶囊是独立小窗：必须跟字幕一起收掉
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
        text = (text or "").strip()
        source = (source or "").strip()
        if not text:
            return                                   # 空内容不清屏：宁可留着上一句
        self._latest = (text, source)
        self._entries = [("theirs", source, text)]
        self._touch(force)

    def update_entries(self, entries: Any, force: bool = False) -> None:
        """刷新成对话视图：entries = [(who, source, translation[, label]), ...]，**最后一条最新**。

        `label` = 说话人昵称（房间里的远端成员才有），小字画在译文上方、与手腕屏一致。
        ⚠️ 必须走 `_unpack_entry`：**只解 3 个会把 4 元组整条丢掉**（原来那个 `try: who, source,
        text = item` 就是这么写的）—— 表现为「房间里别人说的话在桌面字幕上凭空消失」，
        而手腕屏那条腿好好的（它一直收 4 元组）。两条腿共用同一份 `render_conversation`，
        形状也必须共用同一套口径。
        """
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
        self._entries = rows
        self._latest = (rows[-1][2], rows[-1][1])
        self._touch(force)

    def _touch(self, force: bool) -> None:
        sig = self._signature()
        if sig == self._last_sig and not force:
            return                                   # 内容没变：不重复出图（省 CPU）
        self._last_sig = sig
        self._dirty = True
        if self.dry_run:
            self._redraw()                           # dry-run 没有 tick 驱动 → 立刻落盘

    def _signature(self) -> tuple:
        if self.cfg.mode == "latest":
            return ("latest", self._latest)
        return ("conversation", tuple(self._entries))

    def _render(self) -> Image.Image:
        """复用**手腕屏那套**渲染（不另写画字逻辑）。"""
        vcfg = self.cfg.visual_config()
        if self.cfg.mode == "latest":
            text, source = self._latest
            return render_panel(text, source, vcfg)
        return render_conversation(list(self._entries), vcfg)

    def _composite(self, panel: Image.Image) -> Image.Image:
        """把 RGBA 面板贴到**色键同色**的 RGB 底上。

        Tk 的 `-transparentcolor` 只认 RGB 底色：圆角外那一圈 (1,2,3) 才是真透明的地方。
        半透明底板 (12,14,20,205) 叠上去得到 (10,12,17)，肉眼与深色底板一致（主控实测）。
        """
        base = Image.new("RGB", panel.size, KEY_RGB)
        base.paste(panel, (0, 0), panel)
        return base

    def _redraw(self) -> None:
        self._dirty = False
        try:
            panel = self._render()
        except Exception as exc:  # noqa: BLE001 — 出图失败不许打断翻译腿，保留上一帧
            print(f"[desktop] ⚠️ 出图失败（保留上一帧）：{type(exc).__name__}: {exc}")
            return
        if self._native is not None:
            try:
                self._native.set_panel(panel, self.cfg.alpha)
                self._frames += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] ⚠️ 原生窗贴图失败（保留上一帧）："
                      f"{type(exc).__name__}: {exc}")
            return
        frame = self._composite(panel)
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
        if self._blit(frame):
            self._frames += 1
            if self._need_shape_mask:
                self._apply_shape_mask(panel)

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

    def _blit(self, frame: Image.Image) -> bool:
        if self._win is None or self._label is None:
            return False
        try:
            from PIL import ImageTk

            # ⚠️ master 要给：进程里可能同时存在 GUI 的 root 与我们自建的 root，
            #    不指定的话 PhotoImage 会挂到"默认 root"上（可能已经被销毁）。
            self._photo = ImageTk.PhotoImage(frame, master=self._win)
            self._label.configure(image=self._photo)
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 贴图失败（保留上一帧）：{type(exc).__name__}: {exc}")
            return False
        return True

    # ---------- 周期任务 ----------
    def tick(self) -> None:
        """跟随目标窗口 + 配置热重载 + 处理待重绘。没起窗时直接 return。"""
        if not self._started:
            return
        if self.dry_run:
            if self._dirty:
                self._redraw()
            return
        if self._native is not None:
            self._reload_config()
            self._follow_game()
            self._native.tick()
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
        if self._pill_exstyles_dirty and self._pill is not None:
            # 胶囊首次映射后 Tk 会重写扩展样式（把「不抢焦点」冲掉）→ 下一跳补回来
            self._pill_exstyles_dirty = False
            self._apply_pill_exstyles()
        if self._dirty:
            self._redraw()
        if self._pump and self._root is not None:
            try:
                self._root.update()          # 自建 root：没人跑主循环，这里泵一下
            except Exception as exc:  # noqa: BLE001
                print(f"[desktop] ⚠️ 泵 Tk 事件失败：{type(exc).__name__}: {exc}")

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
            self._place_pill()             # 原生窗那条腿建不出胶囊，这里是空操作
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
            return
        self._place_pill()                 # 面板动了，胶囊跟着走（右上角外侧）

    def _own_hwnds(self) -> tuple[int, ...]:
        """本程序**自己**的窗口句柄（主界面 + 字幕窗）：找游戏窗口时必须排除。

        主界面标题是「VRChat 实时同传」（英文界面是 vrchat-livetranslate），也含
        "VRChat" —— 不排除的话，VRChat 没起来时字幕会贴到我们自己的界面上
        （截图里看得一清二楚：字幕贴在程序窗口下沿）。实测踩过。
        """
        out: list[int] = []
        if self._hwnd:
            out.append(int(self._hwnd))
        if self._pill_hwnd:
            out.append(int(self._pill_hwnd))
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

    # ---------- 交互 ----------
    def set_draggable(self, on: bool) -> None:
        """True = 临时关掉鼠标穿透，好让用户把字幕拖到想要的位置。"""
        on = bool(on)
        self._dragging = on
        if self.dry_run:
            return
        if self._native is not None:
            self._native.set_draggable(on)
        elif self._win is not None:
            self._apply_exstyles()
        else:
            return
        restored = "开" if (self.cfg.click_through and not on) else "关"
        print(f"[desktop] {'已解锁拖动（鼠标穿透临时关闭）' if on else '已锁定位置'}"
              f" → 鼠标穿透={restored}")

    # ---------- 拖动 ----------
    # `overrideredirect(True)` 的窗口**没有标题栏**，系统不给拖 —— 只能自己绑鼠标事件。
    # 解不解锁是用户的事：鼠标穿透开着时窗口根本收不到这些事件（这正是穿透的意义）。
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
            return
        self._place_pill()          # 拖动时面板每动一下，胶囊跟着走

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
            self._native.set_alpha(self.cfg.alpha)
            return
        if self._win is None:
            return
        try:
            self._win.attributes("-alpha", self.cfg.alpha)
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 设置透明度 {self.cfg.alpha:.2f} 失败："
                  f"{type(exc).__name__}: {exc} → 保持原值")
            return
        # Tk 改 -alpha 会重写窗口扩展样式 → 穿透/工具窗要补回去，否则拖一次滑块就漏鼠标。
        # 立刻补一次 + 下一跳再补一次（Tk 的重写是延迟到事件循环里的）。
        self._apply_exstyles()
        self._exstyles_dirty = True

    # ---------- 模式胶囊（原声 / 译音）----------
    def set_mode_pill(self, text: str | None, cb: Any = None) -> None:
        """在面板右上角外侧显示/更新「原声/译音」胶囊；`text=None` → 隐藏。

        为什么是**独立小窗**而不是面板里画一个按钮：字幕窗默认鼠标穿透
        （Windows 是 `WS_EX_TRANSPARENT`），整个窗口收不到鼠标事件 —— 面板里的
        按钮永远点不着。这个 Toplevel 反过来：**不穿透**（要能被点），但用
        `set_tool_window` 标成 `WS_EX_NOACTIVATE`（**不抢焦点**）—— 用户在 VRChat
        里，焦点被抢走会中断操作（视角/移动会卡住），这正是本功能的全部意义。

        `cb` 由界面层**注入**（就是主界面那颗按钮的同一条 `toggle_voice_mode`），
        本模块（vlt/output/**）不 import vlt.gui —— 分层纪律。

        `text=None` / 没配 `mode_pill` / dry-run 都不建窗；dry-run 只记
        `self._pill_text`（现有离线用例依赖「绝不建窗」）。
        """
        text = (text or "").strip()
        if not self.cfg.mode_pill:
            if not self._pill_disabled_logged:
                self._pill_disabled_logged = True
                print("[desktop] mode_pill=false → 不显示「原声/译音」胶囊"
                      "（主界面按钮照常可切）")
            self._pill_cb = None
            self._hide_pill()
            return
        if not text:
            self._pill_cb = None
            self._hide_pill()
            return
        self._pill_cb = cb
        self._pill_text = text
        if self.dry_run:
            return                                     # 离线：只记文字，绝不建窗
        if self._pill is None and not self._build_pill():
            return
        try:
            self._pill_label.configure(text=text)
            self._pill.deiconify()
            self._pill.update_idletasks()
        except Exception as exc:  # noqa: BLE001 — 胶囊坏了不许带崩字幕/翻译
            print(f"[desktop] ⚠️ 更新胶囊文字失败（本次忽略）：{type(exc).__name__}: {exc}")
            return
        self._place_pill()
        if self._pill_exstyles_dirty:
            # 先设一次；**不清脏标志** —— Tk 映射后会再重写一遍，留给下一跳 tick 补
            self._apply_pill_exstyles()

    def _build_pill(self) -> bool:
        """惰性建胶囊窗（第一次拿到文字时才建）。返回是否建成。"""
        master = self._win.master if self._win is not None else self._root
        if master is None:
            # 原生叠加窗那条腿（Linux layer-shell / X11 ARGB）没有 Tk 主窗 → 建不出 Tk 胶囊
            if not self._pill_unavailable_logged:
                self._pill_unavailable_logged = True
                print("[desktop] ⚠️ 没有 Tk 主窗（原生叠加窗后端）→「原声/译音」胶囊跳过"
                      "（主界面按钮仍可切换）")
            return False
        try:
            import tkinter as tk
        except Exception as exc:  # noqa: BLE001 — 没有 Tk 就没有胶囊，字幕本身照常
            print(f"[desktop] ⚠️ 建胶囊需要 Tk（不可用）：{type(exc).__name__}: {exc}")
            return False
        # 纯数据常量（ui_theme 不 import tkinter）；字体在 ui_tk 里，必须惰性导入 ——
        # 否则无 Tk 的平台连 desktop_overlay 都 import 不了。
        from ..ui_theme import ACCENT, PANEL, TEXT
        from ..ui_tk import FONT_UI
        try:
            top = tk.Toplevel(master)
            top.overrideredirect(True)                 # 无边框（不要标题栏）
            top.attributes("-topmost", True)
            top.configure(bg=ACCENT)                   # 1px 高亮描边（Label 四周露出来）
            lbl = tk.Label(top, text=self._pill_text, bg=PANEL, fg=TEXT, font=FONT_UI,
                           padx=8, pady=8, cursor="hand2")   # 内边距 8px：够大才好点
            lbl.pack(padx=1, pady=1)                   # 圆角靠字面留白，不做真圆角
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 建胶囊窗失败（字幕照常，只是不能从字幕上切档位）："
                  f"{type(exc).__name__}: {exc}")
            return False
        self._pill, self._pill_label = top, lbl
        # 只绑 Toplevel：Tk 的 bindtags 里 widget → class → **toplevel** → all，
        # 点 Label 时事件会冒到 Toplevel 上的这个绑定；两处都绑会**触发两次**（实测）。
        top.bind("<Button-1>", self._on_pill_click, add="+")
        top.update_idletasks()                         # 让 HWND/几何真的落下去
        self._resolve_pill_hwnd()
        self._apply_pill_exstyles()
        # Tk 首次映射会**再写一次**扩展样式（把「不抢焦点」冲掉）→ 下一跳补回来
        self._pill_exstyles_dirty = True
        print(f"[desktop] 已建「原声/译音」胶囊（点它切档位，不抢焦点）"
              f" 文字={self._pill_text!r}")
        return True

    def _resolve_pill_hwnd(self) -> int:
        """胶囊的顶层句柄（Tk 给的是子窗口）。失败留痕：否则用户会被莫名抢焦点。"""
        if self._pill is None:
            return 0
        from ..platform import top_level_hwnd
        try:
            self._pill_hwnd = int(top_level_hwnd(int(self._pill.winfo_id())))
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 取胶囊句柄失败（{type(exc).__name__}: {exc}）"
                  f" → 点胶囊可能把焦点从游戏里抢走")
            self._pill_hwnd = 0
        return self._pill_hwnd

    def _apply_pill_exstyles(self) -> None:
        """胶囊 = 工具窗（`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`）：不抢焦点、不进 alt-tab。

        ⚠️ **不设**鼠标穿透 —— 它就是要被点的；穿透是字幕面板的事。
        """
        if self.dry_run or self._pill is None:
            return
        from ..platform import set_tool_window
        if not self._pill_hwnd and not self._resolve_pill_hwnd():
            return
        if not set_tool_window(self._pill_hwnd):
            print("[desktop] ⚠️ 胶囊「不抢焦点」没设上（本平台可能不支持）"
                  " → 点它会从 VRChat 抢走焦点")

    def _place_pill(self) -> None:
        """按面板当前位置重算胶囊位置 —— 面板移动/缩放/拖动/换文字全走这里。"""
        if self._pill is None:
            return
        try:
            self._pill.update_idletasks()
            px, py = self.position
            pw, ph = int(self.cfg.size_px[0]), int(self.cfg.size_px[1])
            size = (int(self._pill.winfo_reqwidth()), int(self._pill.winfo_reqheight()))
            x, y = pill_position((px, py), (pw, ph), size, self._work_area())
            # 「+x+y」两段都带加号：Tk 把 "-50" 读成「距右边 50」（多屏副屏坐标是负的）
            self._pill.geometry(f"+{x}+{y}")
        except Exception as exc:  # noqa: BLE001 — 摆错位置不值得打断字幕
            print(f"[desktop] ⚠️ 摆放胶囊失败：{type(exc).__name__}: {exc}")

    def _on_pill_click(self, _ev: Any = None) -> None:
        cb = self._pill_cb
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # noqa: BLE001 — 切档位失败不许带崩字幕
            print(f"[desktop] ⚠️ 胶囊回调失败：{type(exc).__name__}: {exc}")

    def _hide_pill(self) -> None:
        """隐藏胶囊（保留窗口，下次显示更快）。"""
        self._pill_text = ""
        if self._pill is None:
            return
        try:
            self._pill.withdraw()
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 隐藏胶囊失败（忽略）：{type(exc).__name__}: {exc}")

    def _destroy_pill(self) -> None:
        """销毁胶囊窗并清引用（close / 热重载关掉 mode_pill 时用）。"""
        pill, self._pill = self._pill, None
        self._pill_label = None
        self._pill_hwnd = 0
        self._pill_text = ""
        self._pill_cb = None
        self._pill_exstyles_dirty = False
        if pill is None:
            return
        try:
            pill.destroy()
        except Exception as exc:  # noqa: BLE001 — 关窗失败不值得打断退出流程
            print(f"[desktop] ⚠️ 销毁胶囊窗时报错（已忽略）：{type(exc).__name__}: {exc}")

    # ---------- 热重载 ----------
    def _remember_cfg_mtime(self) -> None:
        if self.config_path is None:
            return
        try:
            self._last_cfg_mtime = self.config_path.stat().st_mtime
        except OSError:
            self._last_cfg_mtime = 0.0

    def _reload_config(self) -> None:
        """config.yaml 存盘即生效（位置/尺寸/透明度/字号改完不用重启）。"""
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
        if new.click_through != old.click_through:
            self._apply_exstyles()
        if old.mode_pill and not new.mode_pill:
            self._destroy_pill()          # 热重载关掉胶囊：立刻收掉，别留一个孤儿小窗
            print("[desktop] ♻️ mode_pill 已关（「原声/译音」胶囊窗口已销毁）")
        if new.attach_to_game and not old.attach_to_game:
            self._find_game()
        if not new.attach_to_game:
            self._game_hwnd = None
            self._game_rect = None
        if (new.size_px, new.anchor, new.offset, new.pos,
                new.attach_to_game) != (old.size_px, old.anchor, old.offset,
                                        old.pos, old.attach_to_game):
            if self._native is not None and new.size_px != old.size_px:
                try:
                    self._native.set_size(tuple(new.size_px))
                except Exception as exc:  # noqa: BLE001
                    print(f"[desktop] ⚠️ 原生窗改尺寸失败：{type(exc).__name__}: {exc}")
            self._apply_position()
        self._last_sig = None                        # 视觉参数可能变了 → 下一跳重新出图
        self._dirty = True
        print(f"[desktop] ♻️ 配置热重载：mode={new.mode} anchor={new.anchor} "
              f"offset={new.offset} 面板={new.size_px[0]}x{new.size_px[1]} "
              f"透明度={new.alpha:.2f} 字号={new.font_size}/{new.source_font_size} "
              f"行数={new.max_lines} 显示原文={'是' if new.show_source else '否'} "
              f"穿透={'开' if new.click_through else '关'} 跟随={'开' if new.follow else '关'}")

    def _apply_alpha_only(self) -> None:
        """热重载里改透明度用：与 set_alpha 的区别是**不动 cfg**（已经换过了）。"""
        if self._native is not None:
            self._native.set_alpha(self.cfg.alpha)
            return
        if self._win is None:
            return
        try:
            self._win.attributes("-alpha", self.cfg.alpha)
        except Exception as exc:  # noqa: BLE001
            print(f"[desktop] ⚠️ 热重载设置透明度失败：{type(exc).__name__}: {exc}")
            return
        self._apply_exstyles()
        self._exstyles_dirty = True

    # ---------- 诊断 ----------
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
