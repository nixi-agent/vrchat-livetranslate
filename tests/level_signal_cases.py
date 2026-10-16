"""level match regression cases; shared offline fixtures."""
from __future__ import annotations

from level_match_fixtures import (
    LEVEL_FLOOR_DB,
    LM,
    LevelConfig,
    LevelMatcher,
    MODE_FIXED,
    MODE_FOLLOW_MIC,
    MODE_OFF,
    MicReference,
    TranslatedLevel,
    _amp_for_rms_db,
    _feed_voiced,
    _run_sentences,
    _silence,
    _sine,
    _speech_like,
    apply_gain_db,
    math,
    np,
)

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
