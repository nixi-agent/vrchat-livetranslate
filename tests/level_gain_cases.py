"""level match regression cases; shared offline fixtures."""
from __future__ import annotations

from level_match_fixtures import (
    LM,
    LevelConfig,
    LevelMatcher,
    MODE_FIXED,
    MODE_FOLLOW_MIC,
    MODE_OFF,
    _ramp_endpoints,
    _run_sentences,
    _speech_like,
    np,
)

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
