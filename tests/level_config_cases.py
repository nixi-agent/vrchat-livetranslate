"""level match regression cases; shared offline fixtures."""
from __future__ import annotations

from level_match_fixtures import (
    LEVEL_FLOOR_DB,
    LM,
    LevelConfig,
    LevelMatcher,
    MODE_FIXED,
    MODE_OFF,
    Path,
    _load_level,
    _silence,
    _sine,
    _speech_like,
    apply_gain_db,
    math,
    np,
)

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
    gui += (root / "vlt" / "gui_level.py").read_text(encoding="utf-8")
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
