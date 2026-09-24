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
from .output.chatbox import Chatbox, TokenBucket
from .output.merger import Merger
from .output.overlay import OverlayConfig, WristOverlay
from .session.base import TextDelta, create_session

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
CHUNK_BYTES = 3200          # 100ms @16kHz s16le mono


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
    direction = cfg.direction(args.direction)
    scfg = direction.to_session_config(cfg.session_base)

    cb = cfg.chatbox or {}
    chatbox = Chatbox(
        host=cb.get("host", "127.0.0.1"),
        port=int(cb.get("port", 9000)),
        max_chars=int(cb.get("max_chars", 144)),
        max_lines=int(cb.get("max_lines", 9)),
        bucket=TokenBucket(
            capacity=int(cb.get("bucket_capacity", 5)),
            window_s=float(cb.get("bucket_window_s", 5.0)),
            min_gap_s=float(cb.get("min_gap_s", 0.4)),
        ),
        notification_sound=bool(cb.get("notification_sound", True)),
        dry_run=args.dry_run,
    )

    tele = Telemetry(LOG_DIR / f"p1_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")

    # ---- 手腕 overlay（可选）----
    use_overlay = args.sink in ("overlay", "both")
    overlay: WristOverlay | None = None
    if use_overlay:
        overlay = WristOverlay(
            OverlayConfig.from_dict(cfg.overlay),
            config_path=Path(args.config) if args.config else None,
            dry_run=args.overlay_dry_run,
        )
        overlay.start()

    last_source = {"text": ""}
    last_overlay_at = {"t": 0.0}
    overlay_interval = float(cfg.overlay.get("interval_s", 0) or 0)

    def sink_chatbox(text: str, is_final: bool) -> None:
        chatbox.send(text, is_final)

    def feed_overlay(text: str, is_final: bool) -> None:
        """overlay 是本地显示，不受 chatbox 的 2 秒节流约束：有更新就立刻上屏。

        interval_s 默认 0 = 每个增量都刷；设成 >0 才做节流（怕刷太快看着晃时才用）。
        is_final 永远无视节流立即刷。
        """
        if overlay is None:
            return
        now = time.monotonic()
        if (overlay_interval > 0 and not is_final
                and (now - last_overlay_at["t"]) < overlay_interval):
            return
        last_overlay_at["t"] = now
        overlay.update(text, last_source["text"])

    merger = Merger(
        sink=sink_chatbox,
        interval_s=float((cfg.merger or {}).get("interval_s", cb.get("interval_s", 2.0))),
        carry_over=bool((cfg.merger or {}).get("carry_over", False)),
        max_chars=int(cb.get("max_chars", 144)),
    )

    audio_out: list[bytes] = []
    usage_totals: dict[str, int] = {}

    def on_text(d: TextDelta) -> None:
        if d.source:
            tele.add("source", text=d.source[:80])
            last_source["text"] = d.source
        text = d.display
        if not text:
            return
        tele.add("text", final=d.is_final, text=text[:120])
        # overlay：立刻上屏（本地显示，不受 chatbox 限流与节流影响）
        if args.sink in ("overlay", "both"):
            feed_overlay(text, d.is_final)
        # chatbox：走 2 秒节流 + 终版必刷（受漏桶 5 条/5 秒约束）
        if args.sink in ("chatbox", "both"):
            merger.push(d)

    def on_audio(pcm: bytes) -> None:
        audio_out.append(pcm)
        tele.add("audio", bytes=len(pcm))

    def on_usage(u: dict) -> None:
        for k, v in u.items():
            if isinstance(v, int):
                usage_totals[k] = usage_totals.get(k, 0) + v
        tele.add("usage", **{k: v for k, v in u.items() if isinstance(v, int)})

    session = create_session(scfg)
    if args.log_events:
        session.on_event = lambda t, ev: tele.add("event", type=t,
                                                  keys=sorted(k for k in ev.keys() if k != "type")[:8])
    print(f"[app] 方向={args.direction} 模型={scfg.model} {direction.source_lang or 'auto'}→{direction.target_lang} "
          f"audio={direction.output_audio} voice={scfg.voice}")
    t_connect = time.perf_counter()
    await session.start(on_text=on_text, on_audio=on_audio if direction.output_audio else None, on_usage=on_usage)
    tele.add("session_started", connect_ms=round((time.perf_counter() - t_connect) * 1000, 1))
    print(f"[app] 会话已建立（{(time.perf_counter()-t_connect)*1000:.0f}ms）")

    # 最终版补发泵：被限流暂缓的最终版在这里等窗口到了再发（最终版必达）
    async def pump() -> None:
        while True:
            await asyncio.sleep(0.3)
            n = chatbox.flush_pending()
            if n:
                tele.add("final_resend", count=n)
            if overlay is not None:
                overlay.tick()
            session.tick()      # 静默兜底：服务端不发 done 时也能刷最终版

    pump_task = asyncio.create_task(pump())

    try:
        if args.pcm:
            pcm = Path(args.pcm).read_bytes()
            print(f"[app] 推送 PCM：{len(pcm)} bytes ≈ {len(pcm)/2/16000:.2f}s（按实时节奏）")
            t_audio = time.perf_counter()
            for i in range(0, len(pcm), CHUNK_BYTES):
                await session.send_audio(pcm[i:i + CHUNK_BYTES])
                if not args.no_realtime:
                    await asyncio.sleep(CHUNK_BYTES / 2 / 16000)
            print(f"[app] 音频推送完毕（{time.perf_counter()-t_audio:.2f}s）")
        elif args.mic:
            await run_mic(session, tele, seconds=args.seconds, device_pattern=args.mic_device)
        elif args.loopback:
            await run_loopback(session, tele, patterns=args.loopback_device, seconds=args.seconds)
        else:
            print("[app] 没有音频源：用 --pcm <file> 或 --mic")

        # 等待收尾
        await asyncio.sleep(args.settle_s)
    finally:
        # 收尾：给补发泵一点时间把暂存的最终版发完
        for _ in range(12):
            if chatbox.pending_count == 0:
                break
            chatbox.flush_pending()
            await asyncio.sleep(0.5)
        pump_task.cancel()
        if overlay is not None:
            overlay.close()
        await session.close()
        chatbox.close()
        tele.dump()

    print("\n" + "=" * 60)
    print(f"chatbox 发送成功 {chatbox.sent_ok} 条 / 增量限流丢弃 {chatbox.sent_dropped} 条 "
          f"/ 最终版待补发 {chatbox.pending_count} 条 / 内容未变跳过 {merger.skipped_identical} 次")
    if usage_totals:
        print(f"token 用量：{usage_totals}")
    if audio_out:
        total = sum(len(x) for x in audio_out)
        print(f"译音累计 {total} bytes ≈ {total/2/24000:.2f}s @24kHz（写虚拟声卡前需 24k→48k 重采样）")
    if session.first_delta_ms:
        print(f"首个文本增量（自 speech_started）：{session.first_delta_ms:.0f}ms")
    print("=" * 60)
    return 0


async def run_mic(session, tele, seconds: float = 0.0, device_pattern: str | None = None) -> None:
    """麦克风采集（16kHz 单声道）。

    seconds=0 → 一直跑到 Ctrl+C（实时同传的正常用法）。
    ⚠️ WASAPI 端点按 Windows 会话隔离：必须在**物理控制台会话**里跑，否则采不到声音。
    """
    import sounddevice as sd

    dev_index = pick_input_device(device_pattern)
    if device_pattern and dev_index is None:
        print(f"[mic] ⚠️ 没找到匹配 '{device_pattern}' 的输入设备，改用系统默认设备")
    info = sd.query_devices(dev_index) if dev_index is not None else sd.query_devices(kind="input")
    print(f"[mic] 使用设备 #{dev_index if dev_index is not None else '(默认)'} : {info['name']}  "
          f"{int(info['default_samplerate'])}Hz")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def callback(indata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"[mic] ⚠️ {status}")
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

    print("[mic] 开始采集" + ("（Ctrl+C 结束）" if seconds <= 0 else f"（{seconds:.0f}s）"))
    with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                           blocksize=CHUNK_BYTES // 2, callback=callback, device=dev_index):
        end = None if seconds <= 0 else time.perf_counter() + seconds
        while end is None or time.perf_counter() < end:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await session.send_audio(chunk)
            tele.add("mic_chunk", bytes=len(chunk))
    print("[mic] 采集结束")


def pick_input_device(pattern: str | None) -> int | None:
    """按名称子串匹配输入设备（不写死设备 ID：设备索引会随插拔/会话切换变化）。"""
    if not pattern:
        return None
    import sounddevice as sd

    want = pattern.lower()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and want in str(d["name"]).lower():
            return i
    return None


# ---------------------------------------------------------------- 环回采集（听别人说话）
# VRChat 的游戏音频是混音，没有逐说话人通道 → 只能对**播放端点**做 WASAPI loopback。
# 设备按名称子串匹配 + 回退链，绝不写死设备 ID（会话切换/插拔会让索引整个变）。
LOOPBACK_FALLBACK = ["steam streaming speakers", "vive virtual", "cable input", "voicemeeter"]


def pick_loopback_device(patterns: list[str] | None = None):
    """返回 (index, name, rate, channels)。找不到返回 None。"""
    import pyaudiowpatch as pyaudio

    chain = [p.lower() for p in (patterns or LOOPBACK_FALLBACK)]
    p = pyaudio.PyAudio()
    try:
        loops = list(p.get_loopback_device_info_generator())
        if not loops:
            return None, p
        for kw in chain:
            for d in loops:
                if kw in str(d["name"]).lower():
                    return (d["index"], d["name"], int(d["defaultSampleRate"]),
                            int(d["maxInputChannels"])), p
        # 回退：默认播放设备对应的 loopback
        try:
            default_out = p.get_host_api_info_by_type(pyaudio.paWASAPI).get("defaultOutputDevice", -1)
            for d in loops:
                if d.get("index") == default_out or str(d["name"]).startswith("默认"):
                    return (d["index"], d["name"], int(d["defaultSampleRate"]),
                            int(d["maxInputChannels"])), p
        except Exception:
            pass
        d = loops[0]
        return (d["index"], d["name"], int(d["defaultSampleRate"]), int(d["maxInputChannels"])), p
    except Exception:
        p.terminate()
        raise


def to_16k_mono(pcm: bytes, rate: int, channels: int) -> bytes:
    """任意采样率/声道 → 16kHz 单声道 s16le（会话要求 16k）。"""
    import numpy as np
    from math import gcd

    a = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        a = a[: len(a) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate == 16000:
        return a.astype(np.int16).tobytes()
    if rate % 16000 == 0:                      # 整数倍直接分箱平均（带简单抗混叠）
        f = rate // 16000
        n = len(a) // f * f
        return a[:n].reshape(-1, f).mean(axis=1).astype(np.int16).tobytes()
    g = gcd(rate, 16000)                       # 非整数倍走线性插值
    up, down = 16000 // g, rate // g
    n = len(a) // down * down
    if n == 0:
        return b""
    x = np.arange(0, n, down)
    xi = np.arange(0, len(x) * up) / up
    y = np.interp(xi, np.arange(len(x)), a[:n:down])
    return y.astype(np.int16).tobytes()


async def run_loopback(session, tele, patterns: list[str] | None = None, seconds: float = 0.0) -> None:
    """采集 VRChat 的播放输出（= 别人说话）→ 推给会话。

    ⚠️ 必须在物理控制台会话里跑（WASAPI 端点按会话隔离）。
    """
    import asyncio as _asyncio
    import pyaudiowpatch as pyaudio

    loop = asyncio.get_running_loop()
    picked, p = pick_loopback_device(patterns)
    if picked is None:
        print("[loopback] ❌ 没找到任何 loopback 设备（VRChat 在跑吗？在物理控制台会话里吗？）")
        p.terminate()
        return
    idx, name, rate, channels = picked
    print(f"[loopback] 采集端点 #{idx}「{name}」{rate}Hz ×{channels}ch → 16kHz 单声道")

    stream = p.open(format=pyaudio.paInt16, channels=min(2, channels or 2), rate=rate,
                    frames_per_buffer=int(rate * 0.1), input=True, input_device_index=idx)
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def reader():
        while True:
            try:
                data = stream.read(int(rate * 0.1), exception_on_overflow=False)
            except Exception:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)

    import threading

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    print("[loopback] 开始采集" + ("（Ctrl+C 结束）" if seconds <= 0 else f"（{seconds:.0f}s）"))
    end = None if seconds <= 0 else time.perf_counter() + seconds
    sent_bytes = 0
    try:
        while end is None or time.perf_counter() < end:
            try:
                raw = await _asyncio.wait_for(queue.get(), timeout=1.0)
            except _asyncio.TimeoutError:
                continue
            pcm16 = to_16k_mono(raw, rate, min(2, channels or 2))
            if pcm16:
                await session.send_audio(pcm16)
                sent_bytes += len(pcm16)
                tele.add("loopback_chunk", bytes=len(pcm16))
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        p.terminate()
    print(f"[loopback] 采集结束，共 {sent_bytes} bytes ≈ {sent_bytes/2/16000:.0f}s")


def list_devices() -> None:
    import sounddevice as sd

    print("=== 输入设备（麦克风）===")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i:3d} | in={d['max_input_channels']} | {int(d['default_samplerate'])}Hz | {d['name']}")
    print("\n=== 输出设备 ===")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            print(f"  {i:3d} | out={d['max_output_channels']} | {int(d['default_samplerate'])}Hz | {d['name']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 P1：文本路（我说 → chatbox）")
    ap.add_argument("--direction", default="mine", help="config.yaml 里 directions 的 key")
    ap.add_argument("--pcm", default=None, help="16kHz 单声道 s16le PCM 文件（测试用）")
    ap.add_argument("--mic", action="store_true", help="用麦克风实时采集")
    ap.add_argument("--mic-device", default=None,
                    help="输入设备名称子串（如 'realtek' / 'usb'）；不填用系统默认。用 --list-devices 查")
    ap.add_argument("--list-devices", action="store_true", help="列出音频输入/输出设备后退出")
    ap.add_argument("--loopback", action="store_true",
                    help="采集 VRChat 播放输出（= 听别人说话）；配合 --sink overlay 就是手腕屏那条腿")
    ap.add_argument("--loopback-device", default=None, nargs="*",
                    help="loopback 设备名称子串回退链，默认 steam streaming speakers → vive virtual → cable input")
    ap.add_argument("--seconds", type=float, default=0.0, help="麦克风采集时长；0 = 一直跑到 Ctrl+C（默认）")
    ap.add_argument("--no-realtime", action="store_true", help="尽快灌入 PCM（不做实时节流）")
    ap.add_argument("--settle-s", type=float, default=8.0, help="音频推完后等待响应的秒数")
    ap.add_argument("--dry-run", action="store_true", help="不真发 OSC，只打印与落报文")
    ap.add_argument("--sink", default="chatbox", choices=["chatbox", "overlay", "both"],
                    help="输出去向：chatbox（我说→气泡）/ overlay（别人说→手腕屏）/ both")
    ap.add_argument("--overlay-dry-run", action="store_true",
                    help="overlay 不接管 SteamVR，把每帧渲染成 PNG 存 out/overlay_frames/")
    ap.add_argument("--log-events", action="store_true", help="把每个服务端事件类型写进埋点（调试用）")
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
