"""手腕 Overlay 输出：把译文渲染成贴图，挂到手腕/前臂 tracker/HMD 前的 SteamVR overlay 上。

设计要点（见 notes/VRChat实时同传-架构设计-端到端路线.md §4.4）：
- 纯 Python（pyopenvr + Pillow），不需要 Unity / XSOverlay / OVR Toolkit
- **中文/日文用 Pillow 画成图**，绕开 chatbox 与 avatar 头顶字的字符集限制
- 锚点：左手 / 右手 / 前臂 VIVE Ultimate Tracker / HMD 前固定偏移；位置·旋转·尺寸·透明度可配 + 热重载
- SteamVR 没跑时**优雅降级**（禁用 overlay，不阻塞其他输出）
- 离线可验证：`--demo` 只渲染 PNG，不碰 SteamVR
"""
from __future__ import annotations

import ctypes
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------- 配置
@dataclass
class OverlayConfig:
    enabled: bool = True
    anchor: str = "right_hand"          # left_hand | right_hand | tracker | hmd
    tracker_index: int = 0              # anchor=tracker 时用第几个 tracker
    pos: tuple[float, float, float] = (0.0, 0.06, 0.02)      # 相对锚点，米
    rot: tuple[float, float, float] = (0.0, 0.0, 0.0)        # 欧拉角，度
    width_m: float = 0.24
    curvature: float = 0.0
    alpha: float = 0.9
    size_px: tuple[int, int] = (1024, 320)
    font: str = "C:/Windows/Fonts/msyh.ttc"
    font_size: int = 48
    source_font_size: int = 36
    # 配色：统一白色系，层级只靠字号 + 透明度区分（避免出现"蓝原文 + 白译文"这种像残留色的观感）
    color_translation: tuple[int, int, int] = (255, 255, 255)
    color_source: tuple[int, int, int] = (255, 255, 255)
    source_alpha: int = 205                  # 原文透明度（0-255）；150 在白底上会显灰像另一个颜色，取 205 更"同色系"
    color_border: tuple[int, int, int] = (110, 170, 205)
    border_alpha: int = 120
    color_bg: tuple[int, int, int] = (12, 14, 20)
    bg_alpha: int = 205
    separator: bool = True                   # 原文与译文之间的细分隔线（同色系，低透明度）
    max_lines: int = 3
    show_source: bool = True
    fade_after_s: float = 0.0           # >0 则最后一条文本显示 fade_after_s 秒后淡出
    overlay_key: str = "vlt.wrist.panel"

    @staticmethod
    def from_dict(d: dict) -> "OverlayConfig":
        d = d or {}
        off = d.get("offset") or {}
        return OverlayConfig(
            enabled=bool(d.get("enabled", True)),
            anchor=d.get("anchor", "right_hand"),
            tracker_index=int(d.get("tracker_index", 0)),
            pos=tuple(off.get("pos", (0.0, 0.06, 0.02))),          # type: ignore[arg-type]
            rot=tuple(off.get("rot", (0.0, 0.0, 0.0))),            # type: ignore[arg-type]
            width_m=float(off.get("width_m", 0.24)),
            curvature=float(off.get("curvature", 0.0)),
            alpha=float(off.get("alpha", d.get("alpha", 0.9))),
            size_px=tuple(d.get("size_px", (1024, 320))),          # type: ignore[arg-type]
            font=d.get("font", "C:/Windows/Fonts/msyh.ttc"),
            font_size=int(d.get("font_size", 48)),
            source_font_size=int(d.get("source_font_size", 36)),
            color_translation=tuple(d.get("color_translation", (255, 255, 255))),   # type: ignore[arg-type]
            color_source=tuple(d.get("color_source", (255, 255, 255))),             # type: ignore[arg-type]
            source_alpha=int(d.get("source_alpha", 205)),
            color_border=tuple(d.get("color_border", (110, 170, 205))),             # type: ignore[arg-type]
            border_alpha=int(d.get("border_alpha", 120)),
            color_bg=tuple(d.get("color_bg", (12, 14, 20))),                        # type: ignore[arg-type]
            bg_alpha=int(d.get("bg_alpha", 205)),
            separator=bool(d.get("separator", True)),
            max_lines=int(d.get("max_lines", 3)),
            show_source=bool(d.get("show_source", True)),
            fade_after_s=float(d.get("fade_after_s", 0.0)),
            overlay_key=d.get("overlay_key", "vlt.wrist.panel"),
        )


# ---------------------------------------------------------------- 渲染（离线可测）
# 中文禁则处理：这些标点不允许出现在行首（行尾溢出一点也比行首标点好读）
CLOSING_PUNCT = "。，、；：！？）」』】》〉·…—.,;:!?)]}\"'"
OPENING_PUNCT = "（「『【《〈([{"


def _tokens(text: str) -> list[str]:
    """切分：拉丁词整词为一个 token，CJK 逐字，标点单独成 token。

    纯按字符切会把英文单词劈开（实测出现 transla/ted、y/our），
    纯按词切又对中文无效（中文没有空格）——所以必须混合切。
    """
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_'/"):
            buf += ch
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
    if buf:
        tokens.append(buf)
    return tokens


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """按像素宽度折行，带拉丁整词保护 + 中文避头尾（行首禁则标点）。"""
    lines: list[str] = []
    cur = ""
    for tk in _tokens(text):
        if tk == "\n":
            lines.append(cur)
            cur = ""
            continue
        if tk == " " and not cur:          # 行首不留空格
            continue
        if draw.textlength(cur + tk, font=font) <= max_w:
            cur += tk
            continue
        # 放不下：若该 token 是禁则标点 → 悬挂在本行末尾（避免行首标点）
        if tk in CLOSING_PUNCT and cur:
            cur += tk
            lines.append(cur)
            cur = ""
            continue
        if cur:
            lines.append(cur.rstrip())
        cur = "" if tk == " " else tk
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def render_panel(text: str, source: str = "", cfg: OverlayConfig | None = None) -> Image.Image:
    """把（原文, 译文）渲染成 RGBA 面板。纯函数，便于离线预览与单测。"""
    cfg = cfg or OverlayConfig()
    w, h = cfg.size_px
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角半透明底板
    pad = 12
    d.rounded_rectangle([pad, pad, w - pad, h - pad], radius=28,
                        fill=(*cfg.color_bg, cfg.bg_alpha),
                        outline=(*cfg.color_border, cfg.border_alpha), width=3)

    def _font(size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(cfg.font, size)
        except Exception:
            return ImageFont.load_default()

    inner_w = w - 4 * pad
    y = pad * 2

    # 原文（小字、灰蓝）—— 3.8 默认就会返回源语言识别结果，双行显示零额外成本
    if cfg.show_source and source:
        sf = _font(cfg.source_font_size)
        src_lines = wrap_text(d, source, sf, inner_w)[-2:]
        for ln in src_lines:
            d.text((pad * 2, y), ln, font=sf, fill=(*cfg.color_source, cfg.source_alpha))
            y += cfg.source_font_size + 8
        y += 6
        if cfg.separator:                    # 同色系细分隔线，替代"靠换色分层"的做法
            d.line([(pad * 2, y), (w - pad * 2, y)],
                   fill=(*cfg.color_source, max(60, cfg.source_alpha // 2)), width=2)
            y += 12

    # 译文（大字、白）
    tf = _font(cfg.font_size)
    text_lines = wrap_text(d, text, tf, inner_w)
    if len(text_lines) > cfg.max_lines:      # 同传场景保留最新内容
        text_lines = text_lines[-cfg.max_lines:]
    for ln in text_lines:
        d.text((pad * 2, y), ln, font=tf, fill=(*cfg.color_translation, 255))
        y += cfg.font_size + 10
    return img


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


# ---------------------------------------------------------------- overlay 本体
class WristOverlay:
    """SteamVR overlay 生命周期管理 + 文本刷新 + 配置热重载。"""

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
        self._last_cfg_mtime = 0.0
        self._last_text_at = 0.0
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
            print(f"[overlay] ⚠️ SteamVR 未运行或不可用，overlay 已禁用（其他输出不受影响）：{exc}")
            return False
        self._overlay = self._vr.overlay
        try:
            self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            self._overlay.setOverlayWidthInMeters(self._handle, self.cfg.width_m)
            if self.cfg.curvature:
                self._overlay.setOverlayCurvature(self._handle, self.cfg.curvature)
            self._overlay.setOverlayAlpha(self._handle, self.cfg.alpha)
            self._device = self._resolve_anchor()
            self._apply_transform()
            self._overlay.showOverlay(self._handle)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 创建 overlay 失败：{exc}")
            return False
        self.available = True
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

    def _upload(self, img: Image.Image) -> None:
        w, h = img.size
        data = img.tobytes()               # RGBA
        buf = ctypes.create_string_buffer(data, len(data))
        try:
            self._overlay.setOverlayRaw(self._handle, buf, w, h, 4)
            self.frames_updated += 1
            print(f"[overlay] ← 贴图已更新（{w}x{h}, 第 {self.frames_updated} 帧）")
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ❌ setOverlayRaw 失败：{type(exc).__name__}: {exc}")

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
                    if geo_changed or anchor_changed:
                        self.cfg = new_cfg
                        if self.available:
                            if anchor_changed:
                                self._device = self._resolve_anchor()
                            self._apply_transform()
                        print(f"[overlay] ♻️ 配置热重载：anchor={new_cfg.anchor} pos={new_cfg.pos} "
                              f"rot={new_cfg.rot} width={new_cfg.width_m}m "
                              f"curvature={new_cfg.curvature} alpha={new_cfg.alpha}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[overlay] ⚠️ 热重载失败（保留旧配置）：{exc}")

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


# ---------------------------------------------------------------- 离线预览
def _demo() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="离线渲染手腕面板（不碰 SteamVR）")
    ap.add_argument("--out", default=str(ROOT / "out" / "wrist_panel.png"))
    ap.add_argument("--text", default="Hello! I'm Nixi. This sentence is being translated in real time, let's see how it looks on your wrist.")
    ap.add_argument("--source", default="你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。")
    ap.add_argument("--font", default=None)
    ap.add_argument("--width-m", type=float, default=0.24)
    args = ap.parse_args()

    cfg = OverlayConfig()
    if args.font:
        cfg.font = args.font
    img = render_panel(args.text, args.source, cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"渲染完成：{out}  ({img.size[0]}x{img.size[1]}, 面板宽 {args.width_m}m)")
    print(f"字体：{cfg.font}")


if __name__ == "__main__":
    _demo()
