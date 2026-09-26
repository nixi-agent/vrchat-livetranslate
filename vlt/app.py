"""P1 主程序：音频源 → 会话 → 节流合并 → chatbox。

用法：
  # 用测试 PCM 跑通链路（不依赖麦克风/VRChat）
  python -m vlt.app --direction mine --pcm testdata/zh_test_16k.pcm --dry-run

  # 真发到 VRChat chatbox（需 VRChat 在跑、OSC 已开）
  python -m vlt.app --direction mine --pcm testdata/zh_test_16k.pcm

  # 麦克风实时（需在物理控制台会话里跑，且选对设备）
  python -m vlt.app --direction mine --mic

  # 观察 OSC 报文（另开一个终端）：python scripts/osc_listen.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .config import load_config
from .engine import (
    Engine, EngineEvents,
    run_mic, run_loopback, to_16k_mono,
    pick_input_device, pick_loopback_device, list_devices,
)

from .paths import APP_DIR

ROOT = APP_DIR
LOG_DIR = APP_DIR / "logs"


class Telemetry:
    def __init__(self, path: Path | None = None) -> None:
        self.t0 = time.perf_counter()
        self.records: list[dict] = []
        self.path = path

    def add(self, kind: str, **kw) -> None:
        rec = {"t_ms": round((time.perf_counter() - self.t0) * 1000, 1), "kind": kind, **kw}
        self.records.append(rec)

    def dump(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            for r in self.records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[telemetry] {self.path}")


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    direction = args.direction
    if direction not in cfg.directions:
        raise SystemExit(f"配置里没有方向的 key：{direction}（现有：{list(cfg.directions)}）")

    d = cfg.directions[direction]
    print(f"[app] 方向={direction} 模型={cfg.session_base['model']} "
          f"{d.source_lang or 'auto'}→{d.target_lang} "
          f"audio={d.output_audio} voice={cfg.session_base.get('voice', 'Tina')}")

    tele = Telemetry(LOG_DIR / f"p1_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    usage_totals: dict[str, int] = {}

    if args.pcm:
        source = f"pcm:{args.pcm}"
    elif args.mic:
        source = "mic"
    elif args.loopback:
        source = "loopback"
    else:
        print("[app] 没有音频源：用 --pcm <file> 或 --mic")
        return 0

    sinks: set[str] = set()
    if args.sink in ("chatbox", "both"):
        sinks.add("chatbox")
    if args.sink in ("overlay", "both"):
        sinks.add("overlay")

    events = EngineEvents(
        on_text=lambda src, txt, final: tele.add("text", final=final, text=txt[:120]),
        on_status=lambda level, msg: print(f"[app][{level}] {msg}"),
        on_stats=lambda s: usage_totals.update(
            {k: usage_totals.get(k, 0) + v for k, v in s.items() if isinstance(v, int)}),
    )

    engine = Engine(
        cfg=cfg,
        direction=direction,
        source=source,
        sinks=sinks,
        events=events,
        settle_s=args.settle_s,
        dry_run=args.dry_run,
        overlay_dry_run=args.overlay_dry_run,
        no_realtime=args.no_realtime,
        config_path=args.config,
        audio_out=args.audio_out,
        audio_device=args.audio_device or None,
    )

    engine.start()

    try:
        engine.join()
    except KeyboardInterrupt:
        print("\n[app] 已停止（Ctrl+C）")

    engine.stop()
    tele.dump()

    cb = engine.chatbox
    m = engine.merger
    print("\n" + "=" * 60)
    if cb is not None:
        print(f"chatbox 发送成功 {cb.sent_ok} 条 / 增量限流丢弃 {cb.sent_dropped} 条 "
              f"/ 最终版待补发 {cb.pending_count} 条 "
              f"/ 内容未变跳过 {m.skipped_identical if m else 0} 次")
    if usage_totals:
        print(f"token 用量：{usage_totals}")
    sess = engine.session
    if sess is not None and sess.first_delta_ms:
        print(f"首个文本增量（自 speech_started）：{sess.first_delta_ms:.0f}ms")
    print("=" * 60)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 P1：文本路（我说 → chatbox）")
    ap.add_argument("--direction", default="mine", help="config.yaml 里 directions 的 key")
    ap.add_argument("--pcm", default=None, help="16kHz 单声道 s16le PCM 文件（测试用）")
    ap.add_argument("--mic", action="store_true", help="用麦克风实时采集")
    ap.add_argument("--list-devices", action="store_true", help="列出音频输入/输出设备后退出")
    ap.add_argument("--loopback", action="store_true",
                    help="采集 VRChat 播放输出（= 听别人说话）；配合 --sink overlay 就是手腕屏那条腿")
    ap.add_argument("--no-realtime", action="store_true", help="尽快灌入 PCM（不做实时节流）")
    ap.add_argument("--settle-s", type=float, default=8.0, help="音频推完后等待响应的秒数")
    ap.add_argument("--dry-run", action="store_true", help="不真发 OSC，只打印与落报文")
    ap.add_argument("--sink", default="chatbox", choices=["chatbox", "overlay", "both"],
                    help="输出去向：chatbox（我说→气泡）/ overlay（别人说→手腕屏）/ both")
    ap.add_argument("--overlay-dry-run", action="store_true",
                    help="overlay 不接管 SteamVR，把每帧渲染成 PNG 存 out/overlay_frames/")
    ap.add_argument("--audio-out", action="store_true", default=None,
                    help="开启译音输出（模型译音 → 虚拟声卡）。覆盖 config.yaml 的 output.audio.enabled")
    ap.add_argument("--audio-device", default=None, nargs="*",
                    help="虚拟声卡设备名称回退链；不填用 config.yaml 里的默认值")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    if args.list_devices:
        list_devices()
        return 0
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[app] 已停止（Ctrl+C）")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
