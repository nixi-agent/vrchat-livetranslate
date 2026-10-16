"""Shared desktop overlay configuration and pure positioning calculations."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from .overlay import OverlayConfig

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


# ---------------------------------------------------------------- 窗口本体
