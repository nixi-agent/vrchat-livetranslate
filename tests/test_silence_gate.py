"""长静音闸门 + preroll 验收（改动②）。

## 治什么

用户 4 小时实测日志：输入音频里**静音块占比 66%~81%**，模型被静音喂久了进入
repeat 状态，服务端报 `COMMON_ERROR: model repeat output happened` 后 1011 掐线，
两条腿各断 8 次。现在：连续静音超阈值 → 暂停上送；声音恢复 → 先补发闸住期间
保留的最后 N 秒（preroll）再上送当前块 —— **绝不丢句首**。

## 离线可跑

闸门判定用注入的 `now`（纯逻辑）；代理集成用假会话 + 自造 PCM 块，
不连网络、不需要音频设备。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHUNK = 0.1          # 每块时长（秒）：3200B @16kHz s16le mono


def _cfg(**over):
    from vlt.config import AppConfig, Direction

    base = {"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
            "workspace_id": "", "reconnect_backoff": [0.05],
            "max_new_sessions_per_minute": 100, "final_silence_s": 0.1}
    base.update(over)
    return AppConfig(
        session_base=base,
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=False)},
        chatbox={}, merger={}, overlay={}, output={},
    )


def _pcm(peak: int, marker: int) -> bytes:
    """自造 100ms PCM 块：峰值 = peak，第 2 个采样塞 marker 以便辨认是哪一块。"""
    import numpy as np

    arr = np.full(1600, peak, dtype="<i2")
    arr[1] = marker                       # marker 必须 < |peak| 才不改变峰值判定
    return arr.tobytes()


# ---------------------------------------------------------------- 纯闸门逻辑（注入时间）

def test_short_silence_passes_through() -> None:
    """静音没超阈值：每一块都原样上送，闸门不关。"""
    from vlt.engine import _SilenceGate

    g = _SilenceGate(enabled=True, after_s=30.0, preroll_s=1.0)
    for i in range(299):                  # 29.9s 静音 < 30s 阈值
        out = g.feed(b"s", loud=False, dur_s=CHUNK, now=1000.0 + i * CHUNK)
        assert out == [b"s"], f"第 {i} 块不应被闸"
    assert not g.closed, "没到阈值不该关闸"
    print("  短时静音全部透传 OK")


def test_gate_closes_and_replays_preroll_in_order() -> None:
    """★ 核心验收：关闸 → 拦截 → 声音恢复时按原顺序补发 preroll + 当前块（不丢句首）。"""
    from vlt.engine import _SilenceGate

    g = _SilenceGate(enabled=True, after_s=1.0, preroll_s=0.25)
    # 1.0s 静音（10 块，t=0.0~0.9）：全部透传
    for i in range(10):
        c = f"s{i:02d}".encode()
        out = g.feed(c, loud=False, dur_s=CHUNK, now=i * CHUNK)
        assert out == [c], f"阈值内第 {i} 块应透传"
        assert not g.closed
    # 第 11 块（t=1.0）跨过阈值 → 关闸，这块开始被拦
    out = g.feed(b"s10", loud=False, dur_s=CHUNK, now=1.0)
    assert out == [], "跨阈值的静音块应被闸住"
    assert g.closed, "连续静音 ≥ after_s 后闸门必须关闭"
    # 闸住期间再喂 10 块（t=1.1~2.0）：全拦；preroll 只留最后 0.25s ≈ 2 块
    for i in range(11, 21):
        out = g.feed(f"s{i:02d}".encode(), loud=False, dur_s=CHUNK, now=i * CHUNK)
        assert out == [], f"闸住期间第 {i} 块不应上送"
    assert g.gated_chunks == 11, f"拦截计数不对：{g.gated_chunks}"
    # 声音回来：先补 preroll（最近 2 块 s19/s20），再发当前块 —— 顺序不能反
    out = g.feed(b"VOICE", loud=True, dur_s=CHUNK, now=2.1)
    assert out == [b"s19", b"s20", b"VOICE"], f"补发顺序不对（句首丢了？）：{out}"
    assert not g.closed and g.replay_count == 1
    # 闸门已开：后续块**立即**上送（不等任何东西）
    assert g.feed(b"NEXT", loud=True, dur_s=CHUNK, now=2.2) == [b"NEXT"]
    print("  关闸→拦截→preroll 按序回补→恢复立即发送（不丢句首）OK")


def test_gate_open_during_speech_never_triggers() -> None:
    """说话中间的短暂停顿不该关闸：有声块会重置连续静音计时。"""
    from vlt.engine import _SilenceGate

    g = _SilenceGate(enabled=True, after_s=1.0, preroll_s=0.5)
    t = 0.0
    for round_ in range(5):
        for _ in range(8):                # 0.8s 静音
            out = g.feed(b"s", loud=False, dur_s=CHUNK, now=t)
            assert out == [b"s"]
            t += CHUNK
        out = g.feed(b"L", loud=True, dur_s=CHUNK, now=t)   # 一句有声重置计时
        assert out == [b"L"]
        t += CHUNK
    assert not g.closed, "有声块不断重置计时，闸门不该误关"
    print("  说话中的短停顿不误关闸 OK")


def test_gate_disabled_passes_everything() -> None:
    """silence_gate_enabled: false → 永不关闸（用户可彻底关掉这个行为）。"""
    from vlt.engine import _SilenceGate

    g = _SilenceGate(enabled=False, after_s=0.2, preroll_s=1.0)
    for i in range(50):                   # 5s 静音，远超阈值
        assert g.feed(b"s", loud=False, dur_s=CHUNK, now=i * CHUNK) == [b"s"]
    assert not g.closed
    print("  闸门禁用后全部透传 OK")


def test_preroll_respects_budget() -> None:
    """preroll 缓冲只保留最后 preroll_s 秒：闸再久也不多发旧静音。"""
    from vlt.engine import _SilenceGate

    g = _SilenceGate(enabled=True, after_s=0.2, preroll_s=0.3)
    for i in range(100):                  # 10s 静音：0.2s 后关闸，之后全拦
        g.feed(f"s{i:03d}".encode(), loud=False, dur_s=CHUNK, now=i * CHUNK)
    out = g.feed(b"V", loud=True, dur_s=CHUNK, now=10.0)
    # 缓冲 ≤ 0.3s → 恰好 3 块（0.3s），且是最近的 3 块
    assert out == [b"s097", b"s098", b"s099", b"V"], f"preroll 超长或顺序错：{out}"
    print("  preroll 缓冲严格按时长截断 OK")


def test_gate_logs_open_and_close() -> None:
    """开/合闸都必须留痕（不许静默改变发送行为）。"""
    from vlt.engine import _SilenceGate

    g = _SilenceGate(enabled=True, after_s=0.2, preroll_s=0.2)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(5):
            g.feed(b"s", loud=False, dur_s=CHUNK, now=i * CHUNK)
        g.feed(b"V", loud=True, dur_s=CHUNK, now=0.5)
    out = buf.getvalue()
    assert "静音闸门关闭" in out, f"关闸没留痕：{out!r}"
    assert "静音闸门打开" in out and "preroll" in out, f"开闸回补没留痕：{out!r}"
    print("  开/合闸均留痕 OK")


# ---------------------------------------------------------------- 配置校验（非法留痕 + 回落默认）

def test_settings_invalid_values_fall_back_with_warning() -> None:
    from vlt.engine import silence_gate_settings

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        en, after, pre = silence_gate_settings({
            "silence_gate_enabled": "yes",            # 类型不对
            "silence_gate_after_s": -5,               # 负数
            "silence_gate_preroll_s": 0,              # 0
        })
    assert (en, after, pre) == (True, 30.0, 1.0), (en, after, pre)
    out = buf.getvalue()
    for key in ("silence_gate_enabled", "silence_gate_after_s", "silence_gate_preroll_s"):
        assert key in out, f"{key} 非法却没留痕：{out!r}"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        en, after, pre = silence_gate_settings({
            "silence_gate_after_s": 10, "silence_gate_preroll_s": 60})   # preroll > after
    assert pre == 1.0 and after == 10.0, (after, pre)
    assert "大于" in buf.getvalue(), "preroll > after 没留痕"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert silence_gate_settings({}) == (True, 30.0, 1.0)
        assert silence_gate_settings(None) == (True, 30.0, 1.0)
    assert buf.getvalue() == "", "缺省/全合法时不该打印任何东西"
    print("  非法参数留痕 + 回落默认值 OK")


# ---------------------------------------------------------------- 代理集成（假会话 + 自造 PCM）

class _FakeSession:
    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return True

    async def send_audio(self, pcm: bytes) -> None:
        self.received.append(pcm)

    async def close(self) -> None:
        self.closed = True

    def tick(self) -> None:
        pass


def test_proxy_gates_long_silence_and_recovers() -> None:
    """★ 端到端（离线）：静音超阈值后假会话收不到块；声音恢复立刻收到 preroll+句首。"""
    from vlt.engine import Engine, EngineEvents

    eng = Engine(cfg=_cfg(silence_gate_after_s=0.25, silence_gate_preroll_s=0.25),
                 direction="mine", source="mic", sinks=set(), events=EngineEvents())
    fake = _FakeSession()
    eng._session = fake
    loud = _pcm(peak=1000, marker=1)                       # ≥220 = 有声
    silents = [_pcm(peak=0, marker=m) for m in range(2, 12)]  # 全零 + marker < 220 = 静音

    async def run() -> None:
        await eng._proxy.send_audio(loud)
        assert fake.received == [loud], "有声块必须立即上送"
        for c in silents:
            await eng._proxy.send_audio(c)
            await asyncio.sleep(0.05)                       # 10×≥0.05s ≥ 0.5s ≫ 0.25s 阈值
        assert eng._proxy._gate.closed, "0.5s 连续静音后闸门必须关闭"          # noqa: SLF001
        sent_before = len(fake.received)
        # 闸住期间的块没有发出去：已发送数 < 1 + 10
        assert sent_before < 11, f"闸住期间仍在往外发：已发 {sent_before} 块"
        voice = _pcm(peak=1000, marker=99)
        await eng._proxy.send_audio(voice)
        # ★ 不丢句首：当前块必须是最后一块，它前面是最近 2 块闸住的静音（preroll 0.25s）
        assert fake.received[-1] == voice, "声音恢复后当前块没有立即上送"
        assert fake.received[-3:-1] == silents[-2:], \
            f"preroll 不是最近 2 块闸住音频（句首可能丢）：{[b[2:4] for b in fake.received[-4:]]}"
        nxt = _pcm(peak=1000, marker=100)
        await eng._proxy.send_audio(nxt)
        assert fake.received[-1] == nxt, "开闸后后续块没有立即上送"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(run())
    out = buf.getvalue()
    assert "静音闸门关闭" in out and "静音闸门打开" in out, f"开关闸没留痕：{out!r}"
    # 静音统计不受影响（黑匣子口径不变）
    assert eng._silent_chunks == 10, f"静音块统计被闸门干扰：{eng._silent_chunks}"
    print("  代理集成：长静音被闸 + 恢复补 preroll 不丢句首 OK")


if __name__ == "__main__":
    print("test_silence_gate:")
    test_short_silence_passes_through()
    test_gate_closes_and_replays_preroll_in_order()
    test_gate_open_during_speech_never_triggers()
    test_gate_disabled_passes_everything()
    test_preroll_respects_budget()
    test_gate_logs_open_and_close()
    test_settings_invalid_values_fall_back_with_warning()
    test_proxy_gates_long_silence_and_recovers()
    print("ALL PASSED")
