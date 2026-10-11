"""译音音量匹配（vlt/output/level_match.py）纯逻辑测试：**全程离线，绝不碰音频设备**。

本模块只做数字信号上的算术（RMS / 峰值 / 增益 / 限幅 / 斜坡），所以每条用例都用
`struct` 合成的 PCM 直接喂进去，不需要虚拟声卡、不需要 sounddevice、不需要 pytest。
和 `tests/test_micproxy.py` 一样是**单文件脚本**：全绿打印 `ALL PASSED` 并退出 0。

覆盖 .qoder-brief.md §3 的五组，外加评审后补的四组回归：
  1. 块电平 `block_db`（已知幅度正弦 / 全零静音；engine.chunk_level_db 委托到这里）
  2. `apply_gain_db`（0 dB 逐字节相等 / −6 dB / 正增益的天花板限幅）
  3. `MicReference`（中位数、样本门槛、有效期、抗尖峰、静音不得出结论）
  4. 增益计算（四档麦克风参考电平、峰值限幅把 +4 压到 +1.2、限速收敛）
  5. 参考取不到（保住上一次增益 / 从头到尾没拿到 → 回落固定增益）
  6. off→on：进 off 清斜坡状态、切回来**立刻**重算到新目标（不被限速）；fixed 清过期 fallback
  7. 单一实现：「增益+限幅→字节」/ 块电平 / dB 读数格式各只有一份（两条路逐字节相同）
  8. 配置层一致性：`vlt/config.py` 的默认值/区间 == `level_match` 的 `DEFAULT_*`/`RANGES`
     （靠测试钉，而不是让 config.py 反向 import output 包），以及 `_float_in_range` / `_str_choice`
  9. 跨线程（泵/音频线程 vs Tk 线程）：`MicReference` 无异常不丢块、`LevelMatcher` 互斥成立
 10. 句首斜坡在**短块**（< 20ms/块）下必须接着走，不许每个块边界重铺一遍
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402

from vlt.output import level_match as LM                    # noqa: E402
from vlt.output.level_match import (                        # noqa: E402
    LEVEL_FLOOR_DB,
    MODE_FIXED,
    MODE_FOLLOW_MIC,
    MODE_OFF,
    LevelConfig,
    LevelMatcher,
    MicReference,
    TranslatedLevel,
    apply_gain_db,
)


# ------------------------------------------------------------------ 合成工具

def _sine(amp: float, n: int, *, freq: float = 1000.0, rate: int = 16000,
          channels: int = 1) -> bytes:
    """幅度 `amp`（满刻度计数）的正弦，s16le；`channels` > 1 时各声道相同。"""
    t = np.arange(n) / rate
    s = np.rint(amp * np.sin(2.0 * math.pi * freq * t))
    s = np.clip(s, -32768, 32767)
    if channels > 1:
        s = np.repeat(s, channels)
    return s.astype("<i2").tobytes()


def _silence(n: int, *, channels: int = 1) -> bytes:
    return struct.pack(f"<{n * channels}h", *([0] * (n * channels)))


def _amp_for_rms_db(db: float) -> float:
    """要让正弦的 RMS 落在 `db` dBFS，需要的幅度（正弦 RMS = amp/√2）。"""
    return 32768.0 * (10.0 ** (db / 20.0)) * math.sqrt(2.0)


def _speech_like(rms_db: float, peak_db: float, seconds: float, *,
                 rate: int = 48000, channels: int = 2) -> bytes:
    """RMS ≈ `rms_db`、峰值 ≈ `peak_db` 的**双声道 48k** 合成块（高波峰因数，像语音）。

    做法：一段目标 RMS 的正弦打底，再把稀疏几处样本抬到目标峰值 —— 抬的那几处对整体
    RMS 的贡献 < 0.01 dB，于是「RMS 与峰值可以分别钉住」，正好对上实测的
    「译音中位 −18 dBFS、峰值贴 −2.2 dBFS」。
    """
    n = int(rate * seconds)
    base = np.rint(_amp_for_rms_db(rms_db)
                   * np.sin(2.0 * math.pi * 233.0 * np.arange(n) / rate))
    peak = round(32768.0 * (10.0 ** (peak_db / 20.0)))
    for i in range(0, n, max(1, n // 8)):
        base[i] = peak
    base = np.clip(base, -32768, 32767)
    if channels > 1:
        base = np.repeat(base, channels)
    return base.astype("<i2").tobytes()


# ------------------------------------------------------- 1. 块电平 block_db

def test_block_db_known_sine():
    """已知幅度正弦 → `block_db` 与解析值差 < 0.5 dB；全零 → LEVEL_FLOOR_DB。"""
    amp = 10000
    n = 1600                                            # 100ms @16k = 整数个 1kHz 周期
    pcm = _sine(amp, n)
    got = LM.block_db(pcm)
    want = 20.0 * math.log10(amp / 32768.0 / math.sqrt(2.0))
    assert abs(got - want) < 0.5, f"块电平 {got:.2f} 与解析值 {want:.2f} 差太远"

    assert LM.block_db(_silence(n)) == LEVEL_FLOOR_DB, "纯数字静音必须回落 LEVEL_FLOOR_DB"
    assert LM.block_db(b"") == LEVEL_FLOOR_DB, "空块必须回落 LEVEL_FLOOR_DB"

    # 半幅度 → 正好低 6 dB（顺带钉住「dB 是 20·log10 而不是 10·log10」）
    half = LM.block_db(_sine(amp / 2.0, n))
    assert abs((got - half) - 6.02) < 0.5, f"半幅度应低约 6 dB，实测差 {got - half:.2f}"

    peak = LM._peak_db(_sine(amp, n))
    want_peak = 20.0 * math.log10(amp / 32768.0)
    assert abs(peak - want_peak) < 0.5, f"峰值 {peak:.2f} 与解析值 {want_peak:.2f} 差太远"
    assert LM._peak_db(_silence(n)) == LEVEL_FLOOR_DB
    print(f"  block_db：正弦 {got:.2f} dBFS（解析 {want:.2f}）、静音回落 "
          f"{LEVEL_FLOOR_DB:g}、峰值 {peak:.2f} dBFS OK")


# ------------------------------------------------------ 2. apply_gain_db

def test_apply_gain_db_zero_is_byte_identical():
    """db=0.0 → **逐字节相等**（P0：关掉时必须与升级前行为一模一样）。"""
    pcm = _sine(12345, 960) + _silence(120)
    out = apply_gain_db(pcm, 0.0)
    assert out == pcm, "0 dB 必须逐字节相等"
    assert out is pcm, "0 dB 应走短路，原对象返回（不付 numpy 开销）"
    assert apply_gain_db(b"", -6.0) == b"", "空输入必须安全返回空"
    print("  apply_gain_db(0 dB)：逐字节相等 + 短路返回原对象 OK")


def test_apply_gain_db_minus_six():
    """db=−6 → RMS 正好掉 6.0 ± 0.1 dB；长度不变。"""
    n = 4800
    pcm = _sine(10000, n)
    out = apply_gain_db(pcm, -6.0)
    assert len(out) == len(pcm), f"输出字节数必须与输入一致：{len(out)} != {len(pcm)}"
    before = LM.block_db(pcm)
    after = LM.block_db(out)
    delta = before - after
    assert abs(delta - 6.0) < 0.1, f"−6 dB 应让 RMS 掉 6.0 dB，实测掉 {delta:.3f}"
    print(f"  apply_gain_db(−6 dB)：RMS {before:.2f} → {after:.2f}（差 {delta:.3f} dB）OK")


def test_apply_gain_db_positive_hits_ceiling_not_int16_rail():
    """最大正增益：峰值被压到 ceiling 以内，**且不出现 int16 两端（削波）样本**。"""
    ceiling = -1.0
    pcm = _sine(20000, 4800)
    out = apply_gain_db(pcm, 12.0, ceiling_dbfs=ceiling)
    assert len(out) == len(pcm), "输出字节数必须与输入一致"
    arr = np.frombuffer(out, dtype="<i2")
    limit = 32768.0 * (10.0 ** (ceiling / 20.0))
    peak = int(np.max(np.abs(arr.astype(np.int64))))
    assert peak <= math.ceil(limit), f"峰值 {peak} 超过天花板 {limit:.0f}"
    assert not bool(np.any(arr == -32768)), "出现 −32768：说明限幅没生效（削波/绕回）"
    assert not bool(np.any(arr == 32767)), "出现 32767：说明限幅没生效（削波/绕回）"
    got_peak_db = 20.0 * math.log10(peak / 32768.0)
    assert abs(got_peak_db - ceiling) < 0.2, \
        f"限幅后峰值应贴在 {ceiling} dBFS，实测 {got_peak_db:.3f}"
    print(f"  apply_gain_db(+12 dB)：峰值被限到 {peak}（{got_peak_db:.2f} dBFS ≈ 天花板 "
          f"{ceiling:g}），无 −32768/32767 削波样本 OK")


# ------------------------------------------------------- 3. MicReference

def _feed_voiced(ref: MicReference, *, rms_db: float, seconds: float = 3.0,
                 rate: int = 16000, channels: int = 1, t0: float = 0.0) -> float:
    """按 100ms 一块喂满 `seconds` 秒的有声语音，返回**最后一块的时间戳**。"""
    amp = _amp_for_rms_db(rms_db)
    per = int(rate * LM.BLOCK_S)
    now = t0
    last = t0
    for _ in range(int(round(seconds / LM.BLOCK_S))):
        ref.feed(_sine(amp, per, rate=rate, channels=channels), rate, channels, now=now)
        last = now
        now += LM.BLOCK_S
    return last


def test_mic_reference_median_and_threshold():
    """3s 的 −24 dBFS 语音 → db() ≈ −24 ± 1；只喂 3 块 → None（不猜）。"""
    cfg = LevelConfig()
    ref = MicReference(cfg)
    assert ref.db() is None, "一块都没喂就该是 None"
    assert ref.count() == 0

    short = MicReference(cfg)
    amp = _amp_for_rms_db(-24.0)
    per = int(16000 * LM.BLOCK_S)
    for i in range(3):
        short.feed(_sine(amp, per), 16000, 1, now=i * LM.BLOCK_S)
    assert short.count() == 3, f"应收到 3 个有声块，实际 {short.count()}"
    assert short.db() is None, f"样本 {short.count()} < mic_min_blocks=" \
                               f"{cfg.mic_min_blocks}，必须返回 None 而不是硬估"
    assert not short.valid(3 * LM.BLOCK_S), "样本不足时 valid() 必须为 False"

    end = _feed_voiced(ref, rms_db=-24.0, seconds=3.0)
    assert ref.count() == 30, f"3s / 100ms 应是 30 块，实际 {ref.count()}"
    got = ref.db()
    assert got is not None, "样本够了就该给出估计"
    assert abs(got - (-24.0)) < 1.0, f"麦克风电平估计 {got:.2f} 应 ≈ −24 ± 1"
    assert ref.valid(end), "刚喂完，valid() 必须为 True"
    print(f"  MicReference：3s@−24dBFS → {got:.2f} dBFS（30 块）；3 块 → None OK")


def test_mic_reference_window_expiry():
    """`now` 超过 `mic_window_s` → valid() 变 False（电平过期不该再采信）。"""
    cfg = LevelConfig(mic_window_s=5.0)
    ref = MicReference(cfg)
    end = _feed_voiced(ref, rms_db=-20.0, seconds=1.0)
    assert ref.valid(end), "刚喂完应有效"
    assert ref.valid(end + 4.9), "窗口内应有效"
    assert not ref.valid(end + 5.1), f"超过 mic_window_s={cfg.mic_window_s:g}s 必须失效"
    assert ref.db() is not None, "过期只影响 valid()，db() 仍给出最后一次估计"
    print("  MicReference：超过 mic_window_s → valid() False（db() 仍可读）OK")


def test_mic_reference_rejects_spike():
    """插一个 +20 dB 的尖峰块（拍麦/咳嗽）→ 中位数变化 < 1 dB。"""
    ref = MicReference(LevelConfig())
    _feed_voiced(ref, rms_db=-24.0, seconds=3.0)
    before = ref.db()
    assert before is not None
    per = int(16000 * LM.BLOCK_S)
    ref.feed(_sine(_amp_for_rms_db(-4.0), per), 16000, 1, now=99.0)   # −24 + 20 = −4
    after = ref.db()
    assert after is not None
    drift = abs(after - before)
    assert drift < 1.0, f"中位数被一个尖峰带跑了 {drift:.2f} dB（应 < 1）"
    print(f"  MicReference 抗尖峰：{before:.2f} → {after:.2f} dBFS（漂移 {drift:.3f} dB）OK")


def test_mic_reference_ignores_silence():
    """喂静音**不得**得出「麦克风很轻」的结论：db() 保持前值（或 None）。"""
    ref = MicReference(LevelConfig())
    _feed_voiced(ref, rms_db=-24.0, seconds=3.0)
    before = ref.db()
    n_before = ref.count()
    _feed_voiced(ref, rms_db=-24.0, seconds=0.0)          # 0 秒 = 不喂
    for i in range(50):
        ref.feed(_silence(1600), 16000, 1, now=100.0 + i * LM.BLOCK_S)
    assert ref.count() == n_before, \
        f"静音不该进队列：块数 {n_before} → {ref.count()}"
    after = ref.db()
    assert after is not None and abs(after - before) < 1e-9, \
        f"静音不得把估计拉成「很轻」：{before} → {after}"

    # 全新实例只喂静音 → 必须是 None（而不是 LEVEL_FLOOR_DB 这种「超轻」结论）
    fresh = MicReference(LevelConfig())
    for i in range(50):
        fresh.feed(_silence(1600), 16000, 1, now=i * LM.BLOCK_S)
    assert fresh.count() == 0, f"静音一块都不该收，实际 {fresh.count()}"
    assert fresh.db() is None, "只喂静音必须返回 None"
    print("  MicReference：静音不进队列、不刷新估计、全新实例返回 None OK")


def test_mic_reference_multichannel_and_short_chunks():
    """多声道先取均值；比 100ms 还短的块攒起来算（不把碎片当一整块）。"""
    mono = MicReference(LevelConfig())
    stereo = MicReference(LevelConfig())
    amp = _amp_for_rms_db(-24.0)
    per = int(48000 * LM.BLOCK_S)
    for i in range(10):
        mono.feed(_sine(amp, per, rate=48000), 48000, 1, now=i * LM.BLOCK_S)
        # 左右同幅度 → 均值后仍是同一电平
        stereo.feed(_sine(amp, per, rate=48000, channels=2), 48000, 2,
                    now=i * LM.BLOCK_S)
    m, s = mono.db(), stereo.db()
    assert m is not None and s is not None
    assert abs(m - s) < 0.5, f"单/双声道估计应一致：{m:.2f} vs {s:.2f}"
    assert abs(m - (-24.0)) < 1.0, f"48k 下也应 ≈ −24：{m:.2f}"

    # 20ms 碎片 × 5 = 正好 1 个 100ms 块
    frag = MicReference(LevelConfig(mic_min_blocks=1))
    for i in range(5):
        frag.feed(_sine(amp, per // 5, rate=48000), 48000, 1, now=i * 0.02)
    assert frag.count() == 1, f"5 个 20ms 碎片应合成 1 块，实际 {frag.count()}"
    got = frag.db()
    assert got is not None and abs(got - (-24.0)) < 1.0, \
        f"碎片拼接后电平应 ≈ −24，实测 {got}"
    print(f"  MicReference：多声道取均值（{m:.2f} vs {s:.2f}）、20ms 碎片攒成 100ms 块 OK")


def test_mic_reference_set_config_keeps_history():
    """`set_config` 换参数**不清历史**（UI 拖一下滑块不该逼用户重说 0.8s）。"""
    ref = MicReference(LevelConfig())
    _feed_voiced(ref, rms_db=-24.0, seconds=3.0)
    n = ref.count()
    ref.set_config(LevelConfig(mic_min_blocks=4, mic_window_s=60.0))
    assert ref.count() == n, f"set_config 不该丢历史：{n} → {ref.count()}"
    assert ref.db() is not None
    print(f"  MicReference.set_config：保留 {n} 块历史 OK")


# ------------------------------------------------- 4./5. 增益计算（纯逻辑）

def _run_sentences(matcher: LevelMatcher, n: int, *, rms_db: float = -18.0,
                   peak_db: float = -2.2, seconds: float = 0.5) -> list[float]:
    """跑 n 句「合成译音」，返回每句句尾算出的增益。"""
    gains = []
    for _ in range(n):
        matcher.apply(_speech_like(rms_db, peak_db, seconds))
        gains.append(matcher.note_sentence_end())
    return gains


def _ramp_endpoints(src: bytes, out: bytes) -> tuple[float, float]:
    """句首 20ms 斜坡的**起点 / 终点**增益（dB）：只比有信号的帧，避开过零点。"""
    s = np.frombuffer(src, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    d = np.frombuffer(out, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    per_frame = np.full(s.size, np.nan)
    loud = np.abs(s) > 3000
    per_frame[loud] = 20.0 * np.log10(np.abs(d[loud]) / np.abs(s[loud]))
    win = 240
    assert int(np.count_nonzero(loud[:win])) > 10, "斜坡起点窗口内可比样本太少"
    assert int(np.count_nonzero(loud[LM.RAMP_FRAMES - win:LM.RAMP_FRAMES])) > 10, \
        "斜坡终点窗口内可比样本太少"
    head = float(np.nanmean(per_frame[:win]))
    tail = float(np.nanmean(per_frame[LM.RAMP_FRAMES - win:LM.RAMP_FRAMES]))
    return head, tail


def test_translated_level_measures_sentence():
    """`TranslatedLevel`：RMS / 峰值分别钉得住；reset 后回到 None。"""
    pcm = _speech_like(-18.0, -2.2, 0.5)
    lvl = TranslatedLevel()
    assert lvl.level_db() is None and lvl.peak_db() is None, "空累积器必须返回 None"
    lvl.feed(pcm)
    got_rms, got_peak = lvl.level_db(), lvl.peak_db()
    assert got_rms is not None and got_peak is not None
    assert abs(got_rms - (-18.0)) < 0.3, f"合成 RMS {got_rms:.2f} 应 ≈ −18"
    assert abs(got_peak - (-2.2)) < 0.1, f"合成峰值 {got_peak:.2f} 应 ≈ −2.2"
    assert got_peak > got_rms, "语音的峰值必须高于 RMS（波峰因数为正）"
    lvl.reset()
    assert lvl.level_db() is None and lvl.peak_db() is None, "reset 后必须回到 None"

    # 整句电平 == 分块喂的电平（累积器是平方和，不该受切块方式影响）
    whole = TranslatedLevel()
    whole.feed(pcm)
    parts = TranslatedLevel()
    step = 3840                                        # 40ms @48k 立体声
    for i in range(0, len(pcm) - step, step):
        parts.feed(pcm[i:i + step])
    a, b = whole.level_db(), parts.level_db()
    assert a is not None and b is not None
    assert abs(a - b) < 0.05, f"分块喂不该改变整句 RMS：{a:.3f} vs {b:.3f}"
    print(f"  TranslatedLevel：RMS {got_rms:.2f} / 峰值 {got_peak:.2f} dBFS，"
          f"分块==整块（{a:.3f} vs {b:.3f}）OK")


def test_off_mode_is_transparent():
    """mode=off：增益恒 0、`apply()` 逐字节原样返回（P0：默认行为不变）。"""
    cfg = LevelConfig(mode=MODE_OFF, fixed_gain_db=-9.0)
    m = LevelMatcher(cfg, mic_db=lambda: -14.0)
    assert m.gain_db() == 0.0, f"off 模式初值增益应为 0，实际 {m.gain_db()}"
    pcm = _speech_like(-18.0, -2.2, 0.2)
    out = m.apply(pcm)
    assert out == pcm and out is pcm, "off 模式必须逐字节原样返回（走短路）"
    for g in _run_sentences(m, 5):
        assert g == 0.0, f"off 模式句尾增益必须恒 0，实际 {g}"
    st = m.status()
    assert st["mode"] == MODE_OFF and st["gain_db"] == 0.0
    assert st["fallback"] is False, "off 模式不该报 fallback"
    print("  mode=off：增益恒 0、apply() 短路原样返回、句尾重算仍为 0 OK")


def test_fixed_mode_uses_fixed_gain():
    """mode=fixed：稳态增益 == fixed_gain_db（且同样过峰值限幅与限速）。"""
    cfg = LevelConfig(mode=MODE_FIXED, fixed_gain_db=-6.0)
    m = LevelMatcher(cfg)
    gains = _run_sentences(m, 4)
    assert abs(gains[0] - (-6.0)) < 1e-9, f"首句就该到 −6，实际 {gains[0]:.3f}"
    assert all(abs(g - (-6.0)) < 1e-9 for g in gains), f"fixed 模式增益漂移：{gains}"
    # 超过天花板预算的正增益会被峰值限幅压下来。注意初值 prev = fixed_gain_db（+12），
    # 所以是**限速一步步走下来**的：12 → 9 → 6 → 3 → 1.2（每句最多 max_step_db=3）。
    hot = LevelMatcher(LevelConfig(mode=MODE_FIXED, fixed_gain_db=12.0))
    assert abs(hot.gain_db() - 12.0) < 1e-9, f"fixed 模式初值应为 fixed_gain_db，实际 {hot.gain_db()}"
    walk = _run_sentences(hot, 6)
    assert all(b <= a + 1e-9 for a, b in zip(walk, walk[1:])), f"应单调下降：{walk}"
    assert all(abs(b - a) <= 3.0 + 1e-9 for a, b in zip([12.0] + walk[:-1], walk)), \
        f"每句变化不得超过 max_step_db=3：{walk}"
    g = walk[-1]
    assert abs(g - 1.2) < 0.2, f"fixed +12 dB 应被峰值限幅压到 ≈ +1.2，实际 {g:+.3f}"
    st = hot.status()
    assert st["tts_db"] is not None and st["headroom_db"] is not None
    assert st["headroom_db"] >= -0.05, f"限幅后不该没有余量：{st['headroom_db']:+.3f}"
    print(f"  mode=fixed：−6 dB 稳定保持；+12 dB 被峰值限幅压到 {g:+.2f} dB "
          f"（余量 {st['headroom_db']:+.2f} dB）OK")


def test_follow_mic_tracks_reference_levels():
    """四档麦克风参考电平：稳态增益 ≈ ref − tts_level，并受限幅/限速约束。"""
    tts_level, peak = -18.0, -2.2
    # ref → (期望稳态增益, 收敛所需句数)。−14 那档的目标 +4 dB 会被峰值限幅压到 +1.2。
    cases = [(-14.0, 1.2, 1), (-20.0, -2.0, 1), (-24.0, -6.0, 2), (-33.0, -15.0, 5)]
    for ref_db, want, sentences in cases:
        m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC), mic_db=lambda r=ref_db: r)
        gains = _run_sentences(m, sentences + 2, rms_db=tts_level, peak_db=peak)
        steady = gains[-1]
        raw = ref_db - tts_level                       # offset_db 默认 0
        expect = min(max(raw, -24.0), 6.0)             # 目标限幅
        expect = min(expect, -1.0 - peak)              # 峰值限幅
        assert abs(expect - want) < 0.05, f"用例自身算错了：{expect} != {want}"
        assert abs(steady - want) < 0.15, \
            f"ref={ref_db:+g} dBFS：稳态增益 {steady:+.3f}，期望 {want:+.3f}"
        # 限速：相邻句的增益变化不超过 max_step_db
        steps = [abs(b - a) for a, b in zip(gains, gains[1:])]
        assert all(s <= 3.0 + 1e-9 for s in steps), f"超过 max_step_db：{steps}"
        st = m.status()
        assert st["fallback"] is False, f"ref={ref_db} 时不该 fallback：{st}"
        assert abs(st["mic_db"] - ref_db) < 1e-9
        assert abs(st["tts_db"] - tts_level) < 0.3, f"status 里的译音电平不对：{st}"
        print(f"    ref={ref_db:+6.1f} dBFS → 目标 {raw:+6.2f} → 稳态 {steady:+6.2f} dB "
              f"（{len(gains)} 句，逐步 {['%+.2f' % g for g in gains]}）")

    # 峰值限幅的**关键断言**：ref=−14 时正增益被压到 ≈ +1.2 dB
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC), mic_db=lambda: -14.0)
    g = _run_sentences(m, 3, rms_db=tts_level, peak_db=peak)[-1]
    assert abs(g - 1.2) < 0.15, \
        f"ref=−14 / 峰值 −2.2 dBFS：增益应被峰值限幅压到 ≈ +1.2 dB，实际 {g:+.3f}"
    print(f"  follow_mic 峰值限幅：ref=−14 → 未限幅应 +4.0，实测 {g:+.2f} dB "
          f"（= ceiling −1.0 − peak −2.2）OK")


def test_follow_mic_offset_and_limits():
    """offset_db 平移目标；max_boost / max_cut / max_step 三个限幅都真的生效。"""
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC, offset_db=-3.0),
                     mic_db=lambda: -20.0)
    g = _run_sentences(m, 3)[-1]
    assert abs(g - (-5.0)) < 0.15, f"offset=−3 应把目标从 −2 拉到 −5，实际 {g:+.3f}"

    # max_cut_db=6：ref 很低时也不许一口气衰减超过 6 dB
    tight = LevelConfig(mode=MODE_FOLLOW_MIC, max_cut_db=6.0, max_step_db=12.0)
    m2 = LevelMatcher(tight, mic_db=lambda: -60.0)
    g2 = _run_sentences(m2, 4)[-1]
    assert abs(g2 - (-6.0)) < 1e-9, f"max_cut_db=6 应把衰减钳在 −6，实际 {g2:+.3f}"

    # max_boost_db=0：目标 +4 也不许提升，且峰值限幅不会把它抬上去
    nb = LevelConfig(mode=MODE_FOLLOW_MIC, max_boost_db=0.0, max_step_db=12.0)
    m3 = LevelMatcher(nb, mic_db=lambda: -14.0)
    g3 = _run_sentences(m3, 4)[-1]
    assert abs(g3) < 1e-9, f"max_boost_db=0 时增益必须 ≤ 0，实际 {g3:+.3f}"

    # max_step_db=0.5：每句最多挪 0.5 dB
    slow = LevelConfig(mode=MODE_FOLLOW_MIC, max_step_db=0.5)
    m4 = LevelMatcher(slow, mic_db=lambda: -33.0)
    gains = _run_sentences(m4, 4)
    steps = [abs(b - a) for a, b in zip([0.0] + gains[:-1], gains)]
    assert all(s <= 0.5 + 1e-9 for s in steps), f"超过 max_step_db=0.5：{steps}"
    assert abs(gains[-1] - (-2.0)) < 1e-9, f"4 句 × 0.5 dB 应到 −2.0，实际 {gains[-1]:+.3f}"
    print(f"  限幅生效：offset−3 → {g:+.2f}；max_cut=6 → {g2:+.2f}；max_boost=0 → "
          f"{g3:+.2f}；max_step=0.5 → {gains[-1]:+.2f}（{['%+.2f' % x for x in gains]}）OK")


def test_apply_preserves_length_and_ramps():
    """`apply()`：输出字节数恒等于输入；增益变化时句首 20ms 是斜坡（不突变）。"""
    # mode=fixed 的初值增益**就是** fixed_gain_db（不会有增益变化 → 不会挂斜坡），
    # 所以这里用 follow_mic + 大 max_step，让一句就把增益从 0 走到 −12 dB。
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC, fixed_gain_db=0.0,
                                 max_step_db=12.0), mic_db=lambda: -30.0)
    block = _speech_like(-18.0, -2.2, 0.1)             # 100ms @48k 立体声 = 4800 帧
    out = m.apply(block)
    assert len(out) == len(block), f"首句（增益仍为 0）长度必须一致：{len(out)}"
    assert out is block, "增益为 0 时应短路返回原对象（不付 numpy 开销）"
    g = m.note_sentence_end()
    assert abs(g - (-12.0)) < 0.2, f"一句就该走到 −12 dB，实际 {g:+.3f}"

    frames = len(block) // 4
    assert frames > LM.RAMP_FRAMES, "用例前提：一块要长过 20ms 斜坡"
    ramp_out = m.apply(block)
    assert len(ramp_out) == len(block), \
        f"增益变化后长度仍须一致：{len(ramp_out)} != {len(block)}"
    src = np.frombuffer(block, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    dst = np.frombuffer(ramp_out, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    per_frame = np.full(src.size, np.nan)
    loud = np.abs(src) > 3000                          # 避开过零点，只比有信号的帧
    per_frame[loud] = 20.0 * np.log10(np.abs(dst[loud]) / np.abs(src[loud]))
    win = 240
    assert int(np.count_nonzero(loud[:win])) > 10, "斜坡起点窗口内可比样本太少"
    assert int(np.count_nonzero(loud[LM.RAMP_FRAMES - win:LM.RAMP_FRAMES])) > 10, \
        "斜坡终点窗口内可比样本太少"
    head = float(np.nanmean(per_frame[:win]))
    tail = float(np.nanmean(per_frame[LM.RAMP_FRAMES - win:LM.RAMP_FRAMES]))
    assert abs(head) < 3.0, f"斜坡起点应接近 0 dB，实测 {head:+.2f}"
    assert tail < -9.0, f"斜坡终点应接近 −12 dB，实测 {tail:+.2f}"
    assert head - tail > 6.0, f"句首 20ms 内必须有明显渐变（{head:+.2f} → {tail:+.2f}）"

    # 斜坡之后的块：恒定增益，且不再有渐变
    steady = m.apply(block)
    assert len(steady) == len(block)
    d2 = np.frombuffer(steady, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    r2 = np.full(src.size, np.nan)
    r2[loud] = 20.0 * np.log10(np.abs(d2[loud]) / np.abs(src[loud]))
    spread = float(np.nanmax(r2) - np.nanmin(r2))
    assert spread < 0.2, f"斜坡结束后增益应恒定，实测抖动 {spread:.3f} dB"
    mean = float(np.nanmean(r2))
    assert abs(mean - (-12.0)) < 0.2, f"稳态增益应 −12，实测 {mean:+.3f}"
    print(f"  apply()：长度恒等、句首 20ms 斜坡（{head:+.2f} → {tail:+.2f} dB）、"
          f"之后恒定 {mean:+.2f} dB（抖动 {spread:.3f}）OK")


def test_ramp_continues_across_short_chunks():
    """★ 首块短于 20ms 时，斜坡必须**接着走**，不许每块从 0 重铺一遍（实测回归）。

    复现口径（末影猫用 5ms/块复现）：增益 0 → −12 dB 后按 5ms（240 帧）块喂。修复前每块
    都从 −0.65 dB 重来、末帧 −11.95 dB（听感 ~200Hz 泵动/锯齿），第 5 块才恒 −12；
    修复后增益沿斜坡**单调**走到 −12 并保持恒定。
    """
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC, fixed_gain_db=0.0, max_step_db=12.0),
                     mic_db=lambda: -30.0)
    m.apply(_speech_like(-18.0, -2.2, 0.1))            # 首句（增益仍 0，短路）
    g = m.note_sentence_end()
    assert abs(g - (-12.0)) < 0.2, f"用例前提：先把增益推到 −12 dB，实际 {g:+.3f}"

    short = _speech_like(-18.0, -2.2, 0.005)           # 5ms @48k 立体声 = 240 帧
    frames = len(short) // 4
    assert 0 < frames < LM.RAMP_FRAMES, \
        f"用例前提：块要比 20ms 斜坡短（{frames} < {LM.RAMP_FRAMES}）"
    src = np.frombuffer(short, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    loud = np.abs(src) > 500

    def _gain_of(pcm_out: bytes) -> float:
        dst = np.frombuffer(pcm_out, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
        assert int(np.count_nonzero(loud)) > 10, "5ms 块里可比样本太少"
        return float(np.mean(20.0 * np.log10(np.abs(dst[loud]) / np.abs(src[loud]))))

    per_chunk = []
    for _ in range(6):
        out = m.apply(short)
        assert len(out) == len(short), "输出字节数必须恒等于输入"
        per_chunk.append(_gain_of(out))
    readable = ["%+.2f" % x for x in per_chunk]

    assert per_chunk[0] > -2.5, f"第一块应从上一次增益（0 dB）起步，实测 {per_chunk[0]:+.2f}"
    for i in (1, 2, 3):
        assert per_chunk[i] < per_chunk[i - 1] - 1.0, (
            f"第 {i} 块的增益没有继续往下走 —— 斜坡被每个块边界重铺了一遍：{readable}")
    assert all(abs(per_chunk[i] - (-12.0)) < 0.6 for i in (4, 5)), \
        f"20ms 斜坡走完后（第 4 块起）必须恒定 −12 dB：{readable}"
    print(f"  短块斜坡：5ms/块 6 块增益 {readable}（单调走低 → 恒定 −12）OK")


def test_follow_mic_reference_unavailable():
    """参考取不到：保住上一次增益（不归零、不炸）；从头到尾没拿到 → 回落 fixed_gain_db。"""
    # A. 从来没拿到过参考 → 回落 fixed_gain_db，并置 fallback
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC, fixed_gain_db=-4.0),
                     mic_db=lambda: None)
    assert abs(m.gain_db() - (-4.0)) < 1e-9, f"初值应为 fixed_gain_db，实际 {m.gain_db()}"
    gains = _run_sentences(m, 4)
    assert all(abs(g - (-4.0)) < 1e-9 for g in gains), f"应稳定回落在 −4 dB：{gains}"
    assert m.status()["fallback"] is True, "从没拿到参考必须置 fallback"
    assert m.status()["mic_db"] is None

    # B. 先拿到过、之后取不到 → **保住上一次增益**
    holder = {"ref": -20.0}
    m2 = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC),
                      mic_db=lambda: holder["ref"])
    _run_sentences(m2, 3)
    settled = m2.gain_db()
    assert abs(settled - (-2.0)) < 0.15, f"先收敛到 −2 dB，实际 {settled:+.3f}"
    holder["ref"] = None
    gains2 = _run_sentences(m2, 4)
    assert all(abs(g - settled) < 1e-9 for g in gains2), \
        f"参考丢失后增益必须原地不动：{settled:+.3f} → {gains2}"
    assert m2.gain_db() != 0.0, "参考丢失**不得**把增益归零（那会让译音突然变响）"
    assert m2.status()["fallback"] is True, "参考丢失必须置 fallback"

    # C. 参考恢复后继续跟随
    holder["ref"] = -30.0
    _run_sentences(m2, 6)
    assert abs(m2.gain_db() - (-12.0)) < 0.2, f"恢复后应跟到 −12 dB，实际 {m2.gain_db():+.3f}"
    assert m2.status()["fallback"] is False, "参考恢复后应清掉 fallback"

    # D. mic_db 抛异常也不能把引擎带崩（当作取不到）
    def _boom():
        raise RuntimeError("device gone")

    m3 = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC, fixed_gain_db=-2.0), mic_db=_boom)
    g3 = _run_sentences(m3, 2)
    assert all(abs(g - (-2.0)) < 1e-9 for g in g3), f"异常应等价于「取不到」：{g3}"
    assert m3.status()["mic_db"] is None and m3.status()["fallback"] is True
    print("  参考取不到：从未拿到 → 回落 fixed_gain_db −4；拿到后丢失 → 原地保住 −2；"
          "恢复 → 跟到 −12；mic_db 抛异常 → 不崩 OK")


def test_first_sentence_uses_prev_gain_no_buffering():
    """首句还没测到电平：用 prev（初值 = fixed_gain_db），**不缓冲**（apply 立刻出声）。"""
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC, fixed_gain_db=-3.0),
                     mic_db=lambda: -24.0)
    assert abs(m.gain_db() - (-3.0)) < 1e-9
    block = _speech_like(-18.0, -2.2, 0.1)
    out = m.apply(block)
    assert len(out) == len(block), "首句 apply 必须立刻等长返回（不许先测后放）"
    src = np.frombuffer(block, dtype="<i2").astype(np.float64)
    dst = np.frombuffer(out, dtype="<i2").astype(np.float64)
    idx = np.arange(0, len(src), 2)
    loud = np.abs(src[idx]) > 4000
    r = 20.0 * np.log10(np.abs(dst[idx][loud]) / np.abs(src[idx][loud]))
    assert abs(float(np.mean(r)) - (-3.0)) < 0.2, \
        f"首句应施加 prev=−3 dB，实测 {float(np.mean(r)):+.3f}"
    assert m.status()["tts_db"] is None, "句尾之前 status 不该有本句电平"
    g = m.note_sentence_end()
    assert abs(g - (-6.0)) < 0.3, \
        f"首句句尾应算到 −6 dB（目标 −24−(−18)=−6，限速一步内），实际 {g:+.3f}"
    print(f"  首句：立刻施加 prev=−3 dB（不缓冲）→ 句尾重算到 {g:+.2f} dB OK")


def test_set_config_hot_update():
    """`set_config` 热更新：**下一句**生效；切到 off 立刻把增益归零。"""
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC), mic_db=lambda: -24.0)
    _run_sentences(m, 4)
    assert abs(m.gain_db() - (-6.0)) < 0.2, f"先收敛到 −6，实际 {m.gain_db():+.3f}"
    m.set_config(LevelConfig(mode=MODE_FOLLOW_MIC, offset_db=-6.0))
    _run_sentences(m, 6)
    assert abs(m.gain_db() - (-12.0)) < 0.2, f"offset 改 −6 后应到 −12，实际 {m.gain_db():+.3f}"
    m.set_config(LevelConfig(mode=MODE_OFF))
    assert m.gain_db() == 0.0, f"切 off 应立即归零，实际 {m.gain_db()}"
    pcm = _speech_like(-18.0, -2.2, 0.1)
    assert m.apply(pcm) == pcm, "切 off 后 apply 必须逐字节原样"
    m.set_config(LevelConfig(mode=MODE_FIXED, fixed_gain_db=-8.0))
    g = _run_sentences(m, 4)[-1]
    assert abs(g - (-8.0)) < 1e-9, f"切 fixed −8 应一步到位，实际 {g:+.3f}"
    print("  set_config：offset 改 −6 → −12 dB；切 off → 立即 0 且原样；切 fixed → −8 dB OK")


def test_config_from_dict_validation():
    """`LevelConfig.from_dict`：合法值照收、脏值留痕并回落默认、缺省 == 默认。"""
    d = LevelConfig.from_dict(None)
    assert d.mode == MODE_OFF and d.fixed_gain_db == 0.0 and d.offset_db == 0.0
    assert d.max_boost_db == 6.0 and d.max_cut_db == 24.0
    assert d.ceiling_dbfs == -1.0 and d.max_step_db == 3.0
    assert d.mic_min_blocks == 8 and d.mic_window_s == 120.0
    assert LevelConfig.from_dict("不是 dict").to_dict() == d.to_dict(), \
        "非 dict 输入应等价于缺省"
    for k in LevelConfig.__slots__:
        assert getattr(LevelConfig.from_dict({}), k) == getattr(d, k), f"缺省应回落：{k}"

    ok = LevelConfig.from_dict({
        "mode": "follow_mic", "fixed_gain_db": -4.5, "offset_db": 1.5,
        "max_boost_db": 3.0, "max_cut_db": 18.0, "ceiling_dbfs": -3.0,
        "max_step_db": 1.5, "mic_min_blocks": 12, "mic_window_s": 60.0,
    })
    assert ok.mode == MODE_FOLLOW_MIC and ok.fixed_gain_db == -4.5
    assert ok.offset_db == 1.5 and ok.max_boost_db == 3.0 and ok.max_cut_db == 18.0
    assert ok.ceiling_dbfs == -3.0 and ok.max_step_db == 1.5
    assert ok.mic_min_blocks == 12 and ok.mic_window_s == 60.0
    assert ok.to_dict()["mode"] == MODE_FOLLOW_MIC

    # 脏值：每一项都该回落默认（下面这些 print 是**预期输出**，不是失败）
    bad = LevelConfig.from_dict({
        "mode": "loud", "fixed_gain_db": 999, "offset_db": "abc",
        "max_boost_db": -1, "max_cut_db": 900, "ceiling_dbfs": 0.0,
        "max_step_db": 0.0, "mic_min_blocks": 0, "mic_window_s": float("nan"),
    })
    assert bad.mode == MODE_OFF, f"非法 mode 应回落 off，实际 {bad.mode!r}"
    assert bad.fixed_gain_db == 0.0 and bad.offset_db == 0.0
    assert bad.max_boost_db == 6.0 and bad.max_cut_db == 24.0
    assert bad.ceiling_dbfs == -1.0 and bad.max_step_db == 3.0
    assert bad.mic_min_blocks == 8 and bad.mic_window_s == 120.0
    assert LevelConfig.from_dict({"ceiling_dbfs": -6.0}).ceiling_dbfs == -6.0
    assert LevelConfig.from_dict({"ceiling_dbfs": -6.1}).ceiling_dbfs == -1.0
    assert LevelConfig.from_dict({"mic_min_blocks": 8.0}).mic_min_blocks == 8
    assert LevelConfig.from_dict({"mic_min_blocks": 8.5}).mic_min_blocks == 8
    # ★ YAML 1.1 的坑：PyYAML 把裸词 `off` 解析成 **False**，而 config.example.yaml 里
    #   就写着 `mode: off`。不还原成词，照模板写的正确配置会被判非法、还刷一条告警。
    assert LevelConfig.from_dict({"mode": False}).mode == MODE_OFF, \
        "yaml 的 `mode: off` 会变成布尔 False，必须还原成 MODE_OFF"
    assert LevelConfig.from_dict({"mode": True}).mode == MODE_OFF, \
        "yaml 的 `mode: on`（True）不是合法模式，应留痕回落默认 off"
    print("  LevelConfig.from_dict：默认值逐字对齐、合法值照收、9 项脏值全部回落、"
          "YAML `off`→False 的坑已还原 OK")


def test_off_to_on_clears_ramp_and_recomputes_at_once():
    """★ off → 再开：斜坡状态必须清干净，且**立刻**重算到新目标（不被 max_step_db 限速）。

    两个实测事故，都是「`off` 期间 `apply()` 走短路、绕过斜坡推进」造成的：

    * 进 `off` 时 `_ramping` 一直挂着 → 切回来第一句从**上一个会话的旧增益**斜到 0 dB
      （`收敛 -11.75 → 切 off → 切回：-11.75 → -11.14 → -8.83 → -5.89 → +0.00`）；
    * 切回来不重算 → 整整一句 0 dB 播完，句尾才对齐（用户听到的是「第一句还是老音量」）。
    """
    pcm = _speech_like(-18.0, -2.2, 0.1)

    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC), mic_db=lambda: -24.0)
    _run_sentences(m, 4)
    assert abs(m.gain_db() - (-6.0)) < 0.2, f"前提：先收敛到 −6，实际 {m.gain_db():+.3f}"
    assert m._last_tts_db is not None, "前提：句尾必须留下译音实测（切回来要用它）"

    m.set_config(LevelConfig(mode=MODE_OFF))
    assert m.gain_db() == 0.0, f"切 off 应立即归零，实际 {m.gain_db()}"
    assert m._ramping is False, "进 off 必须清斜坡状态（短路会跳过斜坡推进）"
    assert m._ramp_done_frames == LM.RAMP_FRAMES, "斜坡进度也必须复位，否则切回来只斜一半"
    assert m.status()["fallback"] is False
    assert m.apply(pcm) is pcm, "off 期间 apply 必须逐字节原样"

    # 切回 follow_mic（offset −6 → 目标 = −24 + (−6) − 实测译音电平 ≈ −12 dB）
    m.set_config(LevelConfig(mode=MODE_FOLLOW_MIC, offset_db=-6.0))
    got = m.gain_db()
    assert abs(got - (-12.0)) < 0.25, \
        f"切回来必须立刻到新目标 ≈−12 dB，实际 {got:+.3f}"
    assert abs(got - 0.0) > LevelConfig().max_step_db + 1.0, \
        f"一步就从 0 跳到 {got:+.2f} dB（远超 max_step_db=3）—— 这一步必须**不限速**"
    assert m._ramping is True, "增益变了就该挂斜坡（防爆音）"
    assert m._ramp_from == 0.0, f"斜坡必须从**当前**增益（off 后是 0）起步，实际 {m._ramp_from:+.3f}"
    assert abs(m._ramp_to - got) < 1e-9, f"斜坡终点应是新目标 {got:+.3f}，实际 {m._ramp_to:+.3f}"
    head, tail = _ramp_endpoints(pcm, m.apply(pcm))
    assert abs(head) < 3.0, f"首块起点应接近 0 dB（不是旧增益 −6），实测 {head:+.2f}"
    assert tail < -9.0, f"首块终点应接近 −12 dB，实测 {tail:+.2f}"

    # off → fixed 同理：一步到位，不被 max_step_db=3 拖着走
    m2 = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC), mic_db=lambda: -24.0)
    _run_sentences(m2, 3)
    m2.set_config(LevelConfig(mode=MODE_OFF))
    m2.set_config(LevelConfig(mode=MODE_FIXED, fixed_gain_db=-9.0))
    assert abs(m2.gain_db() - (-9.0)) < 1e-9, f"off→fixed 应一步到 −9，实际 {m2.gain_db():+.3f}"

    # 一次都没测过就 off→on：没有依据，**不许瞎跳**（首句仍 0 dB、原样输出）
    fresh = LevelMatcher(LevelConfig(mode=MODE_OFF), mic_db=lambda: -24.0)
    fresh.set_config(LevelConfig(mode=MODE_FOLLOW_MIC))
    assert fresh.gain_db() == 0.0, f"没有译音实测就该保持 0 dB，实际 {fresh.gain_db():+.3f}"
    assert fresh._ramping is False, "增益没变就不该挂斜坡"
    assert fresh.apply(pcm) is pcm
    print("  off→on：进 off 清 _ramping/_ramp_done_frames、切回立刻重算到 −12 dB（不限速）、"
          f"斜坡从 0 dB 起步（{head:+.2f} → {tail:+.2f}）；无实测时不瞎跳 OK")


def test_fixed_mode_clears_stale_fallback():
    """切到 `fixed` 必须清掉上一次 `follow_mic` 失参考留下的 `fallback`（过期状态）。"""
    m = LevelMatcher(LevelConfig(mode=MODE_FOLLOW_MIC, fixed_gain_db=-4.0),
                     mic_db=lambda: None)
    _run_sentences(m, 3)
    assert m.status()["fallback"] is True, "前提：失参考时必须置 fallback"

    m.set_config(LevelConfig(mode=MODE_FIXED, fixed_gain_db=-5.0))
    assert m.status()["fallback"] is False, \
        "★ 切到 fixed 就该**立刻**清 fallback（它根本不看麦克风参考）"
    assert m.status()["mic_db"] is None
    g = _run_sentences(m, 2)[-1]
    assert abs(g - (-5.0)) < 1e-9, f"fixed 模式句尾也必须保持 fallback=False，增益 {g:+.3f}"
    assert m.status()["fallback"] is False, "fixed 模式一句都不该报 fallback"

    # 反过来：fixed → follow_mic 且参考仍取不到 → 该报的还得报
    m.set_config(LevelConfig(mode=MODE_FOLLOW_MIC, fixed_gain_db=-5.0))
    _run_sentences(m, 1)
    assert m.status()["fallback"] is True, "follow_mic 失参考时 fallback 必须还能置起来"
    print("  fallback 清理：follow_mic 失参考 → True；切 fixed（立即 + 句尾）→ False；"
          "再切回失参考的 follow_mic → True OK")


def test_gain_and_ceiling_have_one_implementation():
    """★「增益 + 天花板限幅 → 字节」只许有**一份**实现（`_scale_and_limit`）。

    以前 `apply_gain_db`（整块标量增益）与 `LevelMatcher.apply`（每帧增益 + 句首斜坡）
    各写了一份限幅 —— 改一处忘另一处就会「固定增益削波、跟随麦克风不削波」。
    这里既钉源码里只有一处回转字节，也钉两条路**逐字节相同**。
    """
    src = Path(LM.__file__).read_text(encoding="utf-8")
    assert src.count("def _scale_and_limit") == 1, "只该有一份增益+限幅实现"
    assert src.count("def _limit") == 1, "硬限幅只该有一份"
    assert src.count('.astype("<i2").tobytes()') == 1, \
        "「限幅后回转 s16le 字节」只许出现在 _scale_and_limit 里（否则又是两份实现）"

    pcm = _speech_like(-18.0, -2.2, 0.2)
    for db in (-6.0, -24.0, 12.0):
        m = LevelMatcher(LevelConfig(mode=MODE_FIXED, fixed_gain_db=db))
        assert abs(m.gain_db() - db) < 1e-9, "fixed 模式初值增益就是 fixed_gain_db"
        assert m._ramping is False, "初值不该挂斜坡（否则比的就不是恒定增益了）"
        via_matcher = m.apply(pcm)
        via_helper = apply_gain_db(pcm, db, ceiling_dbfs=-1.0)
        assert len(via_matcher) == len(pcm), "输出字节数必须与输入一致"
        assert via_matcher == via_helper, \
            f"db={db:+g}：LevelMatcher.apply 与 apply_gain_db 结果不一致 —— 限幅实现分叉了"

    # 天花板真的生效（不是两条路一起「都没限幅」而恰好相等）
    hot = apply_gain_db(_sine(20000, 4800), 12.0, ceiling_dbfs=-1.0)
    peak = float(np.max(np.abs(np.frombuffer(hot, dtype="<i2").astype(np.float64)))) / 32768.0
    got = 20.0 * math.log10(peak)
    assert abs(got - (-1.0)) < 0.2, f"+12 dB 应被限到天花板 −1 dBFS，实测 {got:+.3f}"
    assert not bool(np.any(np.frombuffer(hot, dtype="<i2") == 32767)), "不许出现 int16 削波样本"
    print(f"  单一实现：源码只有一处回转字节；db ∈ (−6, −24, +12) 两条路逐字节相同；"
          f"+12 dB 被限到 {got:+.2f} dBFS（无削波样本）OK")


def test_block_level_has_one_implementation():
    """★ 「块电平」只许有**一份**实现：`level_match.block_db`（engine.chunk_level_db 委托它）。

    以前 engine.chunk_level_db 与 level_match 里那份各写一份同语义代码，注释给的理由是
    「不能反向 import 成环」—— 但 engine **本来就** import level_match（单向），反向委托
    并不成环。这里既钉源码形态（engine 那份只许是转发），也钉两条路数值一致。
    """
    import vlt.engine as engine_mod

    root = Path(LM.__file__).resolve().parents[2]
    lm_src = Path(LM.__file__).read_text(encoding="utf-8")
    eng_src = (root / "vlt" / "engine.py").read_text(encoding="utf-8")
    assert lm_src.count("def block_db") == 1, "块电平只该有一份实现"
    assert "return block_db(chunk)" in eng_src, \
        "engine.chunk_level_db 必须委托给 level_match.block_db"
    assert 'np.frombuffer(chunk, dtype="<i2")' not in eng_src, \
        "engine 里不该再有一份自己的块电平实现（那份已删）"

    for amp, n in ((10000, 1600), (3000, 4800), (1, 480)):
        pcm = _sine(amp, n)
        a, b = engine_mod.chunk_level_db(pcm), LM.block_db(pcm)
        assert a == b, f"两条路必须逐位一致：engine {a!r} vs level_match {b!r}"
    assert engine_mod.chunk_level_db(b"") == LEVEL_FLOOR_DB
    assert engine_mod.chunk_level_db(_silence(480)) == LEVEL_FLOOR_DB
    print("  块电平单一实现：engine.chunk_level_db → level_match.block_db（源码形态 + 三档幅度逐位一致）OK")


def test_format_db_is_the_only_formatter():
    """★ dB 读数格式只许有**一份**（`level_match.format_db`）；engine / gui_proxy 都 import 它。"""
    assert LM.format_db(None) == "—", "没有测量值必须是 —（不许拿 0.0 冒充「测到 0 dBFS」）"
    assert LM.format_db(0.0) == "+0.0", "0 也要带符号，否则看不出是「测到 0」还是「没测到」"
    assert LM.format_db(-6.0) == "-6.0"
    assert LM.format_db(12.34) == "+12.3", f"应保留一位小数，实际 {LM.format_db(12.34)!r}"

    root = Path(LM.__file__).resolve().parents[2]
    eng = (root / "vlt" / "engine.py").read_text(encoding="utf-8")
    gui = (root / "vlt" / "gui_proxy.py").read_text(encoding="utf-8")
    assert "def _fmt_db" not in eng, "engine 里那份重复实现应已删掉"
    assert "def _fmt_level" not in gui, "gui_proxy 里那份重复实现应已删掉"
    assert "format_db" in eng, "engine 必须改用共享的 format_db"
    assert "from .output.level_match import format_db" in gui, \
        "gui_proxy 必须从 level_match import format_db"
    # 光删旧函数名抓不到**内联**的第二份实现：`f"{gain:+.1f} dB"` 照样能绕过上面两条断言
    # （实测 engine.py:733 / gui_proxy.py:451 各留了一处）。所以把格式字面量本身当判据扫全仓。
    hits = []
    for path in sorted((root / "vlt").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "+.1f" in text:
            hits.append(f"{path.relative_to(root).as_posix()} ×{text.count('+.1f')}")
    assert hits == ["vlt/output/level_match.py ×1"], \
        f"`+.1f` 只许出现在 level_match.format_db 这一处，实际：{hits}"
    print("  format_db：None → —、0.0 → +0.0、一位小数；engine/gui_proxy 各自的重复实现已删，"
          "两处都 import 同一份，且全仓再无内联 `+.1f` OK")


# ------------------------------------- 6. 配置层与 level_match 的口径一致性（#8）

_LEVEL_MIN_YAML = "ui:\n  lang: zh\noutput:\n  audio:\n    enabled: false\n"


def _load_level(extra: str = "") -> tuple[dict, str]:
    """把一段 YAML 落成临时 config.yaml 并 `load_config`，返回 (归一化后的 level 段, 留痕)。

    留痕捕回来既保住测试输出干净，又能断言「不静默」。全程离线：只读临时文件。

    ⚠️ **必须 hermetic：三步逐个封掉凭据来源**。`load_config` 内部会调 `load_api_key()`，
    而后者在「界面保存的 key」存在时会打一行 `[config] API key 来源：…`，被本函数当留痕捕回来
    → 与多处 `assert log == ""` 冲突。那会让用例**只在开发者本机红（存过 key）、CI 与无 key
    机器绿**：实测 Arch 本机 `rc=1`、空 HOME `rc=0`（同一提交）。故在此逐条封：① 界面保存
    （`credentials._storage_dir_override` → 空目录）② `DASHSCOPE_API_KEY` ③ `~/.bailian/config.json`
    （HOME/USERPROFILE 指向空目录，Windows 上 `expanduser("~")` 用后者）。用例结果与机器状态无关。
    """
    import contextlib
    import io
    import os
    import tempfile

    from vlt import credentials
    from vlt.config import load_config

    p = Path(tempfile.mkdtemp(prefix="vlt-level-cfg-")) / "config.yaml"
    p.write_text(_LEVEL_MIN_YAML + extra, encoding="utf-8")

    empty_home = Path(tempfile.mkdtemp(prefix="vlt-level-home-"))
    saved_override = credentials._storage_dir_override
    saved_env = {k: os.environ.pop(k) for k in ("DASHSCOPE_API_KEY",) if k in os.environ}
    saved_homes = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    credentials._storage_dir_override = lambda: empty_home / ".vrchat-livetranslate"
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(empty_home)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cfg = load_config(p, require_key=False)
    finally:
        credentials._storage_dir_override = saved_override
        os.environ.update(saved_env)
        for k, v in saved_homes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    audio = (cfg.output or {}).get("audio") or {}
    return (audio.get("level") or {}), buf.getvalue()


def test_config_layer_matches_level_match():
    """★ `vlt/config.py` 的默认值 / 合法区间必须与 `level_match` 的 `DEFAULT_*` / `RANGES` 一致。

    为什么靠**测试**钉而不是让 config.py 去 import level_match：config.py 在导入图里位于
    output 包之下（与「不 import engine」同一条纪律），反向 import 会把启动路径和音频腿
    耦在一起。于是两处各写一份字面量 —— 那就必须有东西守住它们不分叉。
    """
    base = LevelConfig()
    got, log = _load_level()
    assert set(got) == set(LevelConfig.__slots__), \
        f"config.py 归一化的键与 LevelConfig 不一致：{sorted(set(got) ^ set(LevelConfig.__slots__))}"
    assert got["mode"] == base.mode == LM.DEFAULT_MODE, f"默认模式不一致：{got['mode']!r}"
    assert "⚠️" not in log, f"缺省配置不该留任何痕：{log!r}"
    for key in LevelConfig.__slots__:
        if key == "mode":
            continue
        assert abs(float(got[key]) - float(getattr(base, key))) < 1e-9, \
            f"{key} 默认值分叉：config.py={got[key]!r} vs level_match={getattr(base, key)!r}"
        assert abs(float(got[key]) - float(getattr(LM, f"DEFAULT_{key.upper()}"))) < 1e-9, \
            f"{key} 与 DEFAULT_{key.upper()} 分叉"

    # 区间边界：两端各自照收，且收进来的值相同（ceiling_dbfs 的上界是**开区间**）
    for key, (lo, hi) in LM.RANGES.items():
        for edge in (lo, hi):
            if key == "ceiling_dbfs" and edge == hi:
                continue
            a, _ = _load_level(f"    level:\n      {key}: {edge!r}\n")
            b = LevelConfig.from_dict({key: edge})
            assert abs(float(a[key]) - float(edge)) < 1e-9, \
                f"{key} 边界 {edge} 被 config.py 拒了：{a[key]!r}"
            assert abs(float(b.__getattribute__(key)) - float(edge)) < 1e-9, \
                f"{key} 边界 {edge} 被 level_match 拒了"
        for bad in (lo - 1.0, hi + 1.0):
            a, alog = _load_level(f"    level:\n      {key}: {bad!r}\n")
            b = LevelConfig.from_dict({key: bad})
            assert "⚠️" in alog, f"{key}={bad} 越界必须留痕（禁静默降级）"
            if key == "mic_min_blocks":
                # 刻意的差别：config.py 的整数走 `_int_clamped`（夹到边界仍是可用值），
                # level_match 走「回落默认」。两边都必须**仍在合法区间内**、都留痕。
                assert 1 <= int(a[key]) <= 100 and 1 <= b.mic_min_blocks <= 100, \
                    f"{key}={bad}：两层都该把结果拉回 [1, 100]（{a[key]!r} / {b.mic_min_blocks}）"
                continue
            assert abs(float(a[key]) - float(getattr(base, key))) < 1e-9, \
                f"{key}={bad} 应回落默认 {getattr(base, key)}，config.py 给了 {a[key]!r}"
            assert abs(float(getattr(b, key)) - float(getattr(base, key))) < 1e-9, \
                f"{key}={bad} 应回落默认 {getattr(base, key)}，level_match 给了 {getattr(b, key)!r}"

    # ceiling_dbfs 的上界 0 dBFS = 满刻度，贴边必削波 → 两层都必须**拒收**
    a, alog = _load_level("    level:\n      ceiling_dbfs: 0.0\n")
    assert abs(a["ceiling_dbfs"] - LM.DEFAULT_CEILING_DBFS) < 1e-9, \
        f"ceiling_dbfs=0 必须被拒（开区间），实际 {a['ceiling_dbfs']!r}"
    assert "⚠️" in alog, "拒收必须留痕"
    assert LevelConfig.from_dict({"ceiling_dbfs": 0.0}).ceiling_dbfs == LM.DEFAULT_CEILING_DBFS
    a, _ = _load_level("    level:\n      ceiling_dbfs: -0.01\n")
    assert abs(a["ceiling_dbfs"] - (-0.01)) < 1e-9, "开区间内的值应照收"
    print("  config ↔ level_match：9 个默认值逐字一致、区间边界两端照收、越界都留痕回落、"
          "ceiling_dbfs 的 0 dBFS 开区间两层都拒 OK")


def test_config_float_in_range_and_str_choice():
    """`vlt/config.py` 两个校验助手：合法 / 非数字 / NaN / 越界 / 开区间 / YAML 1.1 的 `off`。"""
    import contextlib
    import io

    from vlt.config import _float_in_range, _str_choice

    def _cap(fn, *a, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = fn(*a, **kw)
        return out, buf.getvalue()

    # 合法值照收（含两个端点），且**不留痕**
    for v in (-36.0, -4.5, 0.0, 12.0):
        got, log = _cap(_float_in_range, v, 0.0, key="k", lo=-36.0, hi=12.0)
        assert got == v, f"合法值 {v} 被改了：{got!r}"
        assert log == "", f"合法值 {v} 不该留痕：{log!r}"

    # 非数字 / None / NaN / ±inf → 回落默认 + 留痕（三种坏法各有各的话）
    got, log = _cap(_float_in_range, "abc", 3.0, key="k", lo=0.5, hi=12.0)
    assert got == 3.0 and "不是数字" in log, f"非数字应回落默认并留痕：{got!r} / {log!r}"
    got, log = _cap(_float_in_range, None, 3.0, key="k", lo=0.5, hi=12.0)
    assert got == 3.0 and "不是数字" in log, f"None 应回落默认并留痕：{got!r} / {log!r}"
    got, log = _cap(_float_in_range, float("nan"), 3.0, key="k", lo=0.5, hi=12.0)
    assert got == 3.0 and "不是有限数" in log, f"NaN 应回落默认并留痕：{got!r} / {log!r}"
    got, log = _cap(_float_in_range, float("inf"), 3.0, key="k", lo=0.5, hi=12.0)
    assert got == 3.0 and "不是有限数" in log, f"inf 应回落默认并留痕：{got!r} / {log!r}"

    # 越界 → 回落默认（**不夹到边界**：dB 是声学设置，夹边界等于替用户编一个他没要的值）
    for bad, word in ((0.4, "max_step_db"), (900.0, "max_cut_db")):
        got, log = _cap(_float_in_range, bad, 3.0, key=word, lo=0.5, hi=12.0)
        assert got == 3.0, f"{word}={bad} 应回落默认 3.0，实际 {got!r}（不许夹到边界）"
        assert "超出合理范围" in log and word in log, f"越界必须留痕且带上键名：{log!r}"

    # 开区间（ceiling_dbfs 专用）：hi 本身拒收，hi 内侧照收，日志里的括号也得是 `)`
    got, log = _cap(_float_in_range, 0.0, -1.0, key="ceiling_dbfs",
                    lo=-6.0, hi=0.0, hi_exclusive=True)
    assert got == -1.0, f"0 dBFS = 满刻度，必须拒收，实际 {got!r}"
    assert "超出合理范围 [-6, 0)" in log, f"开区间的留痕该写 `)`：{log!r}"
    got, log = _cap(_float_in_range, -0.01, -1.0, key="ceiling_dbfs",
                    lo=-6.0, hi=0.0, hi_exclusive=True)
    assert got == -0.01 and log == "", f"开区间内侧应照收：{got!r} / {log!r}"
    got, log = _cap(_float_in_range, -6.0, -1.0, key="ceiling_dbfs",
                    lo=-6.0, hi=0.0, hi_exclusive=True)
    assert got == -6.0 and log == "", f"下界是闭区间，−6 应照收：{got!r} / {log!r}"
    # 同一组参数、闭区间口径 → 0 就该照收（证明 hi_exclusive 真的在起作用）
    got, log = _cap(_float_in_range, 0.0, -1.0, key="k", lo=-6.0, hi=0.0)
    assert got == 0.0 and log == "", f"闭区间下 0 应照收：{got!r} / {log!r}"

    # _str_choice：白名单 + 首尾空格宽容 + YAML 1.1 布尔还原 + 留痕
    choices = ("off", "fixed", "follow_mic")
    for v in ("off", "fixed", "follow_mic", "  fixed  "):
        got, log = _cap(_str_choice, v, "off", key="mode", choices=choices)
        assert got == v.strip() and log == "", f"{v!r} 应照收：{got!r} / {log!r}"
    # 大小写**敏感**，且与 level_match._norm_mode 同一口径（两层各写一份校验，
    # 口径分叉就会出现「config.py 收下了、level_match 又判非法」的自相矛盾留痕）
    for upper in ("FIXED", "Follow_Mic", "OFF"):
        got, log = _cap(_str_choice, upper, "off", key="mode", choices=choices)
        assert got == "off" and "⚠️" in log, f"{upper!r} 应留痕回落：{got!r} / {log!r}"
        assert LevelConfig.from_dict({"mode": upper}).mode == MODE_OFF, \
            f"{upper!r}：两层口径必须一致（level_match 也该判非法）"
    got, log = _cap(_str_choice, False, "off", key="mode", choices=choices)
    assert got == "off" and log == "", \
        f"★ YAML 1.1 把裸词 `off` 解析成 False，必须还原成 'off' 且**不留痕**：{got!r} / {log!r}"
    got, log = _cap(_str_choice, True, "off", key="mode", choices=choices)
    assert got == "off" and "⚠️" in log, f"YAML 的 `mode: on`（True）不是合法模式：{got!r} / {log!r}"
    for bad in ("loud", "", None, 3):
        got, log = _cap(_str_choice, bad, "off", key="mode", choices=choices)
        assert got == "off" and "不是" in log, f"{bad!r} 应留痕回落：{got!r} / {log!r}"

    # 端到端：照 config.example.yaml 写的 `mode: off` 走完整条 load_config，读回来是**词**不是布尔
    lv, log = _load_level("    level:\n      mode: off\n")
    assert lv["mode"] == "off" and isinstance(lv["mode"], str), \
        f"`mode: off` 应读成字符串 'off'，实际 {lv['mode']!r}（{type(lv['mode']).__name__}）"
    assert "⚠️" not in log, f"照模板写的正确配置不该刷告警：{log!r}"
    lv, log = _load_level("    level:\n      mode: follow_mic\n      fixed_gain_db: -4.5\n")
    assert lv["mode"] == "follow_mic" and lv["fixed_gain_db"] == -4.5, lv
    assert log == "", f"合法配置不该留痕：{log!r}"
    lv, log = _load_level("    level: 不是字典\n")
    assert lv["mode"] == "off" and lv["max_step_db"] == 3.0, f"level 非 dict 应回落默认：{lv!r}"
    print("  config 校验助手：合法照收（含端点）/ 非数字 / None / NaN / inf / 越界全部留痕回落默认，"
          "ceiling_dbfs 开区间拒 0 收 −0.01，YAML `off`→False 已还原（端到端读回字符串 'off'）OK")


# --------------------------------- 7. 跨线程 / 单一实现 / 离线约束（#8 #10 + 复审）

def test_mic_reference_is_thread_safe():
    """★ `feed` 跑在代理的麦克风泵线程，`db`/`valid`/`count` 跑在 Tk 线程（500ms 读数 tick）。

    以前 `db()` 直接在 Tk 线程 `sorted(deque)`，泵线程同时 `append/popleft` 就会偶发
    `RuntimeError: deque mutated during iteration` —— 而它被调用方的 `except Exception`
    吞掉，用户看到的只是读数无缘无故闪一下 `—`（最难查的那类故障）。
    """
    import threading

    ref = MicReference(LevelConfig())
    lock = getattr(ref, "_lock", None)
    assert lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release"), \
        "MicReference 必须自带一把锁（共享状态：_blocks / _last_ts / _pending / _cfg）"
    assert "_lock" in MicReference.__slots__, "__slots__ 里漏了 _lock"

    amp = _amp_for_rms_db(-24.0)
    block = _sine(amp, int(16000 * LM.BLOCK_S))
    errors: list[tuple[str, Exception]] = []
    stop = threading.Event()

    def feeder(idx: int) -> None:
        try:
            t0 = float(idx)
            for i in range(300):
                ref.feed(block, 16000, 1, now=t0 + i * LM.BLOCK_S)
        except Exception as exc:                            # noqa: BLE001
            errors.append((f"feed#{idx}", exc))

    def reader(idx: int) -> None:
        try:
            while not stop.is_set():
                ref.db()
                ref.count()
                ref.valid(1e6)
                _ = ref.cfg
        except Exception as exc:                            # noqa: BLE001
            errors.append((f"read#{idx}", exc))

    def twiddler() -> None:
        # 只翻 `mic_min_blocks`，**不动** `mic_window_s`：窗口决定 `_trim` 的容量
        # （max(min_blocks, window/BLOCK_S)），把它改小就会裁掉块，让「900 块全在」无从断言。
        try:
            flip = False
            while not stop.is_set():
                flip = not flip
                ref.set_config(LevelConfig(mic_min_blocks=4 if flip else 8))
        except Exception as exc:                            # noqa: BLE001
            errors.append(("set_config", exc))

    threads = ([threading.Thread(target=reader, args=(i,), daemon=True) for i in range(3)]
               + [threading.Thread(target=twiddler, daemon=True)]
               + [threading.Thread(target=feeder, args=(i,), daemon=True) for i in range(3)])
    for th in threads[:4]:
        th.start()
    for th in threads[4:]:
        th.start()
    for th in threads[4:]:
        th.join(timeout=30.0)
    stop.set()
    for th in threads[:4]:
        th.join(timeout=10.0)
    assert not errors, f"并发下抛异常了：{errors[:3]}"
    assert not any(th.is_alive() for th in threads), "有线程没退（锁没放？）"

    # 结果仍然自洽：3×300 块有声语音都进了队列，中位数还是 −24 dBFS
    n = ref.count()
    assert n == 900, f"900 块有声语音一块都不该丢，实际 {n}"
    got = ref.db()
    assert got is not None and abs(got - (-24.0)) < 1.0, f"并发写入后估计失真：{got!r}"
    before = ref.count()
    for _ in range(200):
        ref.db()
    assert ref.count() == before, "db() 只读，不许改队列长度"
    print(f"  线程安全：3 泵 + 3 读 + 1 改参数并发无异常；{n} 块全在、中位数 {got:.2f} dBFS、"
          "db() 不改队列 OK")


def test_level_matcher_is_thread_safe():
    """★ `LevelMatcher` 的共享态也要互斥：`apply()` 有**两个**调用者。

    说话译音走 `Engine._on_audio`（事件循环线程），打字译音走 `Engine._speak_stream`
    （`asyncio.to_thread` 的工作线程）——而 `_speak_lock` 只串行化两条「打字」路径，管不到
    说话那条腿。两句重叠时 `_tts._sumsq/_n/_peak`（`+=` 会丢更新）与
    `_ramping/_ramp_done_frames` 会被交错读改写：句子电平互相污染、句首斜坡丢帧。

    这条用例**钉的是互斥本身**，不是「恰好没抛异常」：把一个记录并发进入次数的
    `TranslatedLevel` 换进 `_tts`，只要两线程能同时进 `apply()`，`feed` 就会被并发进入
    （修复前实测会 ≥2）——「全绿」区分不出空转的用例和有效的用例，所以判据必须是
    「互斥成立」这种**去掉锁就必然变红**的性质。
    """
    import threading
    import time

    m = LevelMatcher(LevelConfig(mode=MODE_FIXED, fixed_gain_db=-6.0))
    lock = getattr(m, "_lock", None)
    assert lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release"), \
        "LevelMatcher 必须自带一把锁（_cfg/_gain_db/_ramping/_ramp_*/_tts 都是共享态）"
    assert "_lock" in LevelMatcher.__slots__, "__slots__ 里漏了 _lock"

    class _OverlapWatch(TranslatedLevel):
        """只多一个「同一时刻有几个人在 feed 里」的计数器，其余是真实现。"""

        def __init__(self) -> None:
            super().__init__()
            self._inside = 0
            self.max_inside = 0
            self._guard = threading.Lock()

        def feed(self, pcm_s16: bytes) -> None:
            with self._guard:
                self._inside += 1
                self.max_inside = max(self.max_inside, self._inside)
            time.sleep(0.002)               # 放大窗口：2ms 足够让另一个线程挤进来
            try:
                super().feed(pcm_s16)
            finally:
                with self._guard:
                    self._inside -= 1

    watch = _OverlapWatch()
    m._tts = watch                          # 只替换累积器；apply 的其余逻辑走真实现
    block = _speech_like(-18.0, -2.2, 0.1)
    errors: list[Exception] = []
    stop = threading.Event()

    def _audio_thread() -> None:
        try:
            for _ in range(60):
                out = m.apply(block)
                assert len(out) == len(block), "输出字节数必须恒等于输入"
        except Exception as exc:                            # noqa: BLE001
            errors.append(exc)

    def _ui_thread() -> None:
        try:
            while not stop.is_set():
                m.status()
                m.gain_db()
                m.mic_db()
                m.cfg
                m.set_config(LevelConfig(mode=MODE_FIXED, fixed_gain_db=-6.0))
        except Exception as exc:                            # noqa: BLE001
            errors.append(exc)

    audio = [threading.Thread(target=_audio_thread, daemon=True) for _ in range(3)]
    ui = threading.Thread(target=_ui_thread, daemon=True)
    ui.start()
    for th in audio:
        th.start()
    for th in audio:
        th.join(timeout=30.0)
    stop.set()
    ui.join(timeout=10.0)

    assert not errors, f"并发下抛异常了：{errors[:3]}"
    assert not any(th.is_alive() for th in audio + [ui]), "有线程没退（锁没放？）"
    assert watch.max_inside == 1, (
        f"{watch.max_inside} 个线程同时进了 apply()（_tts.feed 被并发进入）—— "
        "斜坡推进与电平累积会丢更新，必须全部进 _lock")
    print(f"  线程安全（LevelMatcher）：3 音频线程 × 60 块 + 1 界面线程并发无异常；"
          f"_tts.feed 最大并发进入 = {watch.max_inside}（互斥成立）OK")


def test_module_has_no_device_or_engine_dependency():
    """本模块必须**离线**：不 import engine（会成环）、不 import 任何音频设备库。

    用 AST 看**真实的 import 语句**而不是子串匹配 —— level_match.py 自己的文档字符串里
    就写着「不 import engine」，子串匹配会自己绊自己。
    """
    import ast

    tree = ast.parse(Path(LM.__file__).read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # 相对 import → 记成 vlt.<第一段>
                mods.add("vlt." + (node.module.split(".")[0] if node.module else ""))
            elif node.module:
                mods.add(node.module.split(".")[0])
    banned = {"sounddevice", "pyaudio", "engine", "platform", "tkinter",
              "vlt.engine", "vlt.platform", "vlt.audio_dsp"}
    hit = sorted(mods & banned)
    assert not hit, f"level_match.py 必须离线可测，却 import 了 {hit}（全部：{sorted(mods)}）"
    assert not any(m.startswith("vlt") for m in mods), \
        f"只许依赖标准库 + numpy（否则 engine → virtualmic → level_match 会成环）：{sorted(mods)}"
    assert not hasattr(LM, "open_mic"), "纯逻辑模块不该有开设备的入口"
    print(f"  离线约束：level_match.py 只 import {sorted(mods)}（无 engine/设备库）OK")


def test_load_level_hermetic_against_saved_key() -> None:
    """★ `_load_level` 必须与「本机是否存过 API key」无关（复审实测的非 hermetic 回归）。

    机器上存过界面 key 时 `load_api_key()` 会打一行 `[config] API key 来源：…`；`_load_level`
    若不封来源，这行会被当留痕捕回来 → 同一条断言在**开发者本机红、CI / 无 key 机器绿**。
    这里造一个含假 key 的凭据目录（= 模拟「存过 key 的机器」），断言留痕仍为空。
    """
    import tempfile

    from vlt import credentials

    fake = Path(tempfile.mkdtemp(prefix="vlt-fake-cred-"))
    (fake / "api_key.txt").write_text("sk-" + "F4k3K3y" * 14, encoding="utf-8")
    saved = credentials._storage_dir_override
    credentials._storage_dir_override = lambda: fake
    try:
        lv, log = _load_level("    level:\n      mode: follow_mic\n")
        assert lv["mode"] == "follow_mic", lv
        assert log == "", f"本机存过 key 不得让合法配置留痕：{log!r}"
    finally:
        credentials._storage_dir_override = saved
    print("  `_load_level` hermetic：本机存过界面 key（假凭据目录）时仍零留痕 OK")


if __name__ == "__main__":
    print("test_level_match:")
    test_block_db_known_sine()
    test_apply_gain_db_zero_is_byte_identical()
    test_apply_gain_db_minus_six()
    test_apply_gain_db_positive_hits_ceiling_not_int16_rail()
    test_mic_reference_median_and_threshold()
    test_mic_reference_window_expiry()
    test_mic_reference_rejects_spike()
    test_mic_reference_ignores_silence()
    test_mic_reference_multichannel_and_short_chunks()
    test_mic_reference_set_config_keeps_history()
    test_translated_level_measures_sentence()
    test_off_mode_is_transparent()
    test_fixed_mode_uses_fixed_gain()
    test_follow_mic_tracks_reference_levels()
    test_follow_mic_offset_and_limits()
    test_apply_preserves_length_and_ramps()
    test_ramp_continues_across_short_chunks()
    test_follow_mic_reference_unavailable()
    test_first_sentence_uses_prev_gain_no_buffering()
    test_set_config_hot_update()
    test_config_from_dict_validation()
    test_off_to_on_clears_ramp_and_recomputes_at_once()
    test_fixed_mode_clears_stale_fallback()
    test_gain_and_ceiling_have_one_implementation()
    test_block_level_has_one_implementation()
    test_format_db_is_the_only_formatter()
    test_config_layer_matches_level_match()
    test_config_float_in_range_and_str_choice()
    test_load_level_hermetic_against_saved_key()
    test_mic_reference_is_thread_safe()
    test_level_matcher_is_thread_safe()
    test_module_has_no_device_or_engine_dependency()
    print("ALL PASSED")
