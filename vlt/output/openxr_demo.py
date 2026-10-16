"""OpenXR visual diagnostics and explicit runtime smoke entry point."""
from __future__ import annotations
import logging
import time
from pathlib import Path
from typing import Any
from .overlay import OverlayConfig, resolve_font_path

log = logging.getLogger("vlt.output.openxr_overlay")

def render_alpha_test(cfg: OverlayConfig | None = None) -> Any:
    """画一张「一眼就能看出 alpha / 颜色空间对不对」的判定图（`--smoke --alpha-test` 用）。

    底板/边框沿用真实面板的配色与留白，所以看它 ≈ 看真面板：

      * **12px 透明边距 + 圆角**：外面一旦出现黑边，就是图层的 alpha 没生效
        （缺 `BLEND_TEXTURE_SOURCE_ALPHA_BIT`）—— 正是「蓝框外一圈黑」的病根；
      * **四块 25/50/75/100% 不透明度的灰块**：应当由淡到实。整体偏亮、半透明的块
        发白，就说明未预乘 alpha 被当成预乘了（缺 `UNPREMULTIPLIED_ALPHA_BIT`）；
      * **一行 100% 不透明的白字**：底板半透明不该把文字一起变淡
        （整层乘子只乘 alpha 通道，RGB 不动）；
      * **一排纯灰阶（0 / 12 / 32 / 64 / 128 / 192 / 255，全部 100% 不透明）**：
        盯**颜色空间**（transfer function）。数字应逐级变亮且与标注值对得上；
        若 12 看着像 60、32 像 99（暗端整体被抬起、255 却仍是白），就是层纹理被
        当成线性值再编码一次 = **双重 gamma** —— 用户实测的「面板黑不下去」，
        病根见 `format_needs_linearize()`。这一排是纯色不透明块，没有 alpha 参与，
        所以它**只**反映颜色空间，不会和上面那排 alpha 判定混在一起。
    """
    from PIL import Image, ImageDraw, ImageFont

    cfg = cfg or OverlayConfig()
    w, h = cfg.size_px
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 12                              # 与 render_panel 一致的留白：这一圈必须全透明
    d.rounded_rectangle([pad, pad, w - pad, h - pad], radius=28,
                        fill=(*cfg.color_bg, cfg.bg_alpha),
                        outline=(*cfg.color_border, cfg.border_alpha), width=3)

    def _font(size: int) -> ImageFont.FreeTypeFont:
        path = resolve_font_path(cfg.font)
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 — 字体坏了也别让判定图画不出来
                pass
        return ImageFont.load_default()

    f_title = _font(max(18, cfg.font_size - 4))
    f_label = _font(max(14, cfg.source_font_size))
    x0 = pad * 2 + 6
    d.text((x0, pad * 2), "ALPHA TEST", font=f_title, fill=(*cfg.color_translation, 255))

    levels = (64, 128, 191, 255)          # 25% / 50% / 75% / 100%
    gap = 20
    bar = max(24, (w - 2 * x0 - gap * (len(levels) - 1)) // len(levels))
    top = pad * 3 + cfg.font_size
    bottom = top + max(60, h // 4)
    for i, a in enumerate(levels):
        x = x0 + i * (bar + gap)
        d.rectangle([x, top, x + bar, bottom], fill=(235, 235, 235, a))
        d.text((x, bottom + 10), f"{round(a / 255 * 100)}%",
               font=f_label, fill=(*cfg.color_translation, 255))
    d.text((x0, bottom + 22 + cfg.source_font_size), "文字必须实心 / text stays solid",
           font=f_label, fill=(*cfg.color_translation, 255))

    # ★ 灰阶带（颜色空间判定）：值写在标签上，观感应与标签一致。
    ramp_top = bottom + 22 + cfg.source_font_size + 18
    ramp_h = min(70, (h - pad) - ramp_top - (cfg.source_font_size + 12))
    if ramp_h >= 28:                     # 面板太矮就不画（判定图优先保持不越界/不遮底板）
        grays = (0, 12, 32, 64, 128, 192, 255)
        gw = max(18, (w - 2 * x0 - gap * (len(grays) - 1)) // len(grays))
        for i, v in enumerate(grays):
            x = x0 + i * (gw + gap)
            d.rectangle([x, ramp_top, x + gw, ramp_top + ramp_h], fill=(v, v, v, 255))
            d.text((x, ramp_top + ramp_h + 6), str(v), font=f_label,
                   fill=(*cfg.color_translation, 255))
    return img


def _smoke(seconds: float = 15.0, alpha_test: bool = False) -> int:
    """建会话 → 持续提交一帧面板 → 到点退出。不需要 API key、不连网。

    这是「真后端能不能用」的端到端验证入口（等价于 Windows 侧的 `--demo`，
    但会**真的把面板贴到你眼前**，因为要验的正是 XR 那一段）：

        python3 -m vlt.output.openxr_overlay --smoke 15
        python3 -m vlt.output.openxr_overlay --smoke 20 --alpha-test

    ⚠️ 会读**你自己的 config.yaml**（存在的话）：透明度和贴手腕的位置/角度都是
    真机要看的参数，用默认值测等于没测。想边看边调：改 config.yaml 存盘即热重载。
    """
    from .openxr_overlay import OpenXrOverlay
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from ..paths import APP_DIR
    cfg_path = Path(APP_DIR) / "config.yaml"
    cfg = OverlayConfig()
    if cfg_path.exists():
        try:
            import yaml
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            cfg = OverlayConfig.from_dict(raw.get("overlay") or {})
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 读 {cfg_path} 失败，这次用默认参数：{exc}", flush=True)
    ov = OpenXrOverlay(cfg, config_path=cfg_path if cfg_path.exists() else None)
    if not ov.start():
        print("❌ 启动失败（上面有原因）。手腕屏需要：Wayland 会话 + 已连接的 OpenXR 运行时"
              "（WiVRn/Monado 且头显已连）", flush=True)
        return 1
    if alpha_test:
        ov._queue_frame(render_alpha_test(cfg))
        print("✅ 会话已建立，正在提交 **alpha + 颜色空间判定图**。要看四点：\n"
              "   ① 蓝色边框外面是否**全透明**（有黑边 = 图层 alpha 没生效）\n"
              "   ② 四块灰是否由淡到实（发白/发光 = 未预乘 alpha 被当成预乘）\n"
              "   ③ 白字是否实心（跟着底板一起变淡 = 乘子乘到了 RGB）\n"
              "   ④ 最下面一排灰阶（标着 0/12/32/64/128/192/255）是否**黑得下去**：\n"
              "      若 12 看着像 60、32 像 99，而 255 仍是白 → 双重 gamma（层纹理被当线性值），\n"
              "      见 format_needs_linearize()；两侧观感不一致时先看这一排", flush=True)
    else:
        ov.update("你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。",
                  "Hello! I'm Nixi. This sentence is being translated in real time.")
        print(f"✅ 会话已建立，接下来 {seconds:.0f}s 内会持续重提交这一帧（你会看到这块面板）。"
              f"\n   想调位置/角度：改 config.yaml 的 overlay.anchor / offsets（每个锚点各一份），"
              f"存盘即热重载。",
              flush=True)
    t0 = time.monotonic()
    seen_states: list[str] = []
    next_report = t0 + 1.0
    try:
        while time.monotonic() - t0 < seconds:
            # 面板由后台帧循环每帧重提 —— 这里只报状态（原来那句 ov._submit()
            # 早就不存在了：提交已搬进帧循环线程，见 _frame_loop）
            ov.tick()
            st = str(getattr(ov._sess, "state", None)).rsplit(".", 1)[-1]
            if not seen_states or seen_states[-1] != st:
                seen_states.append(st)
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + 3.0
                print(f"    [{time.monotonic()-t0:4.1f}s] 会话状态={st} "
                      f"已提交={ov.frames_updated} 帧 "
                      f"整层 alpha={ov._layer_alpha():.2f} "
                      f"锚点追踪={ov.anchor_tracked}", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    print(f"共提交 {ov.frames_updated} 帧；会话状态序列 {'→'.join(seen_states)}；"
          f"锚点 {cfg.anchor} 追踪="
          f"{ov.anchor_tracked}",
          flush=True)
    if ov.frames_updated == 0:
        print("⚠️ 一帧都没提交成功。看上面的失败原因；若是会话一直没到 FOCUSED，"
              "试试**开着 VRChat（或任意 OpenXR 应用）**再跑 —— overlay 会话的 visible/focused "
              "依赖合成器上报。", flush=True)
    ov.close()
    print("已关闭。", flush=True)
    return 0
