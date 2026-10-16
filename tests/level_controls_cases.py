"""level gui regression cases; shared offline fixtures."""
from __future__ import annotations

from level_gui_fixtures import (
    ROOT,
    _CFG_NO_LEVEL,
    _CFG_WITH_LEVEL,
    _FakeEngine,
    _FakeLevelMatcher,
    _FakeProxy,
    _gui,
    _n_comments,
    _quiet,
    _yaml_scalar,
    level_match,
    t,
    yaml,
)

def test_level_read_db_survives_dirty_spinbox() -> None:
    """★ `_level_read_db` 必须接住 `tk.TclError`：脏值 → 回落 0.0 并**写回控件**。"""
    import tkinter as tk

    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        var = gui._level_fixed_var                                   # noqa: SLF001

        # 前提先自证：手打 `abc` 时 DoubleVar.get() 抛的确实是 TclError（不是 ValueError）。
        # 少了这条，用例就算漏接 TclError 也可能因为别的原因「碰巧」过。
        var._tk.globalsetvar(var._name, "abc")                       # noqa: SLF001
        try:
            var.get()
            raise AssertionError("前提不成立：脏值本该让 DoubleVar.get() 抛 tk.TclError")
        except tk.TclError:
            pass

        got = gui._level_read_db("_level_fixed_var", -24.0, 6.0,     # noqa: SLF001
                                key="fixed_gain_db")
        assert got == 0.0, f"脏值应回落 0.0，实际 {got!r}"
        assert var.get() == 0.0, f"脏值必须写回控件（看到的==生效的），实际 {var.get()!r}"

        # 空串 / 越界 / 非 0.5 倍数：一律夹回界面档位、对齐 0.5 dB、并写回控件
        for raw, want in (("", 0.0), ("999", 6.0), ("-999", -24.0),
                          ("3.37", 3.5), ("not a number", 0.0), ("6", 6.0)):
            var._tk.globalsetvar(var._name, raw)                     # noqa: SLF001
            got = gui._level_read_db("_level_fixed_var", -24.0, 6.0,  # noqa: SLF001
                                    key="fixed_gain_db")
            assert got == want, f"{raw!r} 应得 {want}，实际 {got!r}"
            assert var.get() == want, f"{raw!r} 应写回 {want}，实际 {var.get()!r}"

        # 界面档位比 config 的校验区间窄（−24~6 vs −36~12）：夹的是**界面**那一档
        off = gui._level_offset_var                                  # noqa: SLF001
        off._tk.globalsetvar(off._name, "99")                        # noqa: SLF001
        assert gui._level_read_db("_level_offset_var", -12.0, 12.0,   # noqa: SLF001
                                 key="offset_db") == 12.0

        # 控件还没建（headless / 设置页未建）→ 回落配置里的值，同样夹取；配置也脏 → 0.0
        gui._level_fixed_var = None                                  # noqa: SLF001
        gui._level_cfg_mem()["fixed_gain_db"] = 999.0                # noqa: SLF001
        assert gui._level_read_db("_level_fixed_var", -24.0, 6.0,     # noqa: SLF001
                                 key="fixed_gain_db") == 6.0
        gui._level_cfg_mem()["fixed_gain_db"] = "abc"                # noqa: SLF001
        assert gui._level_read_db("_level_fixed_var", -24.0, 6.0,     # noqa: SLF001
                                 key="fixed_gain_db") == 0.0
        assert gui._level_read_db("_no_such_var", -24.0, 6.0, key="nope") == 0.0
    print("  ✓ _level_read_db：TclError 脏值/空串 → 0.0 且写回控件；999→6、-999→-24、"
          "3.37→3.5；控件未建时回落配置值（同样夹取，脏配置 → 0.0）")

def test_on_level_change_persists_syncs_and_hot_updates() -> None:
    """★ 改一次要同时落到磁盘、内存真源、正在跑的代理与引擎；没变化则一次都不动。"""
    with _gui(_CFG_NO_LEVEL) as (gui, cfg_path):
        proxy = _FakeProxy()
        lm = _FakeLevelMatcher(status={"mode": "fixed", "mic_db": None, "tts_db": None,
                                       "gain_db": 0.0, "headroom_db": None,
                                       "fallback": False})
        gui._proxy = proxy                                          # noqa: SLF001
        gui._engines = [_FakeEngine(level_match=lm)]                # noqa: SLF001

        gui._level_mode_var.set(t("固定增益"))                       # noqa: SLF001
        gui._level_fixed_var.set(-4.5)                              # noqa: SLF001
        gui._level_offset_var.set(1.5)                              # noqa: SLF001
        with _quiet() as buf:
            gui._on_level_change()                                  # noqa: SLF001
        log = buf.getvalue()
        assert "[level] 译音音量已更新：模式 fixed / 固定增益 -4.5 dB / 相对麦克风 1.5 dB" \
            in log, f"该留一行痕：{log!r}"

        # ① 磁盘：整段 level 是**新建**出来的（老配置里没有）。
        #    写出的那一行必须与 `_yaml_scalar` 的输出一致 —— 加不加引号由 PyYAML 判，
        #    `fixed` 是普通词不加引号（只有 off/on/yes/no/true/false 这些 YAML 1.1 布尔
        #    字面量才必须加，见下面 ⑥），但绝不能是手拼 f-string 出来的裸布尔。
        text = cfg_path.read_text(encoding="utf-8")
        assert f"mode: {_yaml_scalar('fixed')}\n" in text, f"模式没走 _yaml_scalar：{text!r}"
        assert "mode: true" not in text and "mode: false" not in text, \
            f"模式被写成了布尔字面量（下次启动读到的就不是字符串）：{text!r}"
        data = yaml.safe_load(text)
        audio = (data.get("output") or {}).get("audio") or {}
        lv = audio.get("level") or {}
        assert lv.get("mode") == "fixed" and isinstance(lv["mode"], str), lv
        assert lv.get("fixed_gain_db") == -4.5 and lv.get("offset_db") == 1.5, lv
        assert audio.get("buffer_ms") == 300, f"兄弟键被动了：{audio!r}"

        # ② 内存真源：设置窗可反复重建，读的是这份快照 → 不同步就会关窗再开看见旧值
        mem = gui._level_cfg_mem()                                  # noqa: SLF001
        assert mem.get("mode") == "fixed", f"内存真源没同步：{mem!r}"
        assert mem.get("fixed_gain_db") == -4.5 and mem.get("offset_db") == 1.5, mem

        # ③ 热更新：代理与在跑的引擎各收到一份同一套参数
        assert len(proxy.calls) == 1, f"代理应收到 1 次热更新：{len(proxy.calls)}"
        got = proxy.calls[0]
        assert isinstance(got, level_match.LevelConfig), type(got)
        assert got.mode == "fixed" and got.fixed_gain_db == -4.5 and got.offset_db == 1.5, got
        assert len(lm.calls) == 1 and lm.calls[0].mode == "fixed", lm.calls

        # ④ 状态栏：用户得知道「已生效」
        assert gui._last_status_level == "info"                     # noqa: SLF001
        shown = str(gui._chat_ctx.status_label.cget("text"))         # noqa: SLF001
        assert t("译音音量已更新（逐句生效）") in shown, f"状态栏文案不对：{shown!r}"

        # ⑤ 幂等：没变化 → 不重写文件、不再热更新一次
        before = cfg_path.read_text(encoding="utf-8")
        with _quiet() as buf:
            gui._on_level_change()                                  # noqa: SLF001
        assert cfg_path.read_text(encoding="utf-8") == before, "没有变化就不该重写配置"
        assert len(proxy.calls) == 1 and len(lm.calls) == 1, "没有变化就不该再热更新"
        assert "译音音量已更新" not in buf.getvalue(), "没有变化就不该再报一次「已更新」"

        # ⑥ ★ 切回 off：YAML 1.1 把裸词 `off` 读成布尔 False —— 写回必须带引号
        gui._level_mode_var.set(t("关闭音量匹配"))                    # noqa: SLF001
        with _quiet():
            gui._on_level_change()                                  # noqa: SLF001
        text = cfg_path.read_text(encoding="utf-8")
        assert "mode: 'off'" in text, f"`off` 是 YAML 布尔字面量，必须加引号：{text!r}"
        back = yaml.safe_load(text)
        mode = ((back.get("output") or {}).get("audio") or {}).get("level", {}).get("mode")
        assert mode == "off" and isinstance(mode, str), \
            f"不加引号下次启动读到的就是 False 而不是 'off'：{mode!r}（{type(mode).__name__}）"
    print("  ✓ _on_level_change：磁盘（level 段新建 + mode 走 _yaml_scalar）/ 内存真源 / 代理+引擎热更新 "
          "三处同步，状态栏 info，无变化时一次都不动，切回 off 写的是 'off' 不是布尔")

def test_level_write_preserves_comments_and_siblings() -> None:
    """就地写回不许吃掉注释、也不许动兄弟键（与 test_config_save 同款回归点）。"""
    with _gui(_CFG_WITH_LEVEL) as (gui, cfg_path):
        base = cfg_path.read_text(encoding="utf-8")
        n_base = _n_comments(base)
        assert "# 译音音量" in base, "前提：模板里应有那条段注释"

        gui._level_mode_var.set(t("跟随麦克风"))                      # noqa: SLF001
        gui._level_offset_var.set(-3.0)                             # noqa: SLF001
        with _quiet():
            gui._on_level_change()                                  # noqa: SLF001

        text = cfg_path.read_text(encoding="utf-8")
        assert "# 译音音量" in text, "就地写入把段注释吃掉了"
        assert _n_comments(text) == n_base, f"注释行数变了：{n_base} → {_n_comments(text)}"
        data = yaml.safe_load(text)                 # 写坏了 YAML 下次启动就起不来
        audio = (data.get("output") or {}).get("audio") or {}
        lv = audio.get("level") or {}
        assert lv.get("mode") == "follow_mic" and lv.get("offset_db") == -3.0, lv
        assert lv.get("fixed_gain_db") == 0.0, f"没改的键不该被动：{lv!r}"
        assert audio.get("buffer_ms") == 300, f"兄弟键 buffer_ms 被动了：{audio!r}"
        assert (audio.get("proxy") or {}).get("passthrough_buffer_ms") == 150, audio
    print("  ✓ 就地写回：段注释保住（注释行数不变）、仍是合法 YAML、没改的键与兄弟键原封不动")

def test_apply_level_config_leaves_trace_and_never_raises() -> None:
    """★ 热更新失败只留痕、绝不打断界面；`hasattr` 那条死分支已删（有代理就必须真调到）。"""
    src = (ROOT / "vlt" / "gui_proxy.py").read_text(encoding="utf-8")
    assert 'hasattr(p, "set_level_config")' not in src, \
        "评审 #5：`hasattr(p, 'set_level_config')` 恒真（LinuxMicProxy 继承基类），死分支该删"

    cfg = level_match.LevelConfig(mode=level_match.MODE_FIXED, fixed_gain_db=-3.0)
    with _gui(_CFG_NO_LEVEL) as (gui, _cfg_path):
        # 代理与引擎都抛 → 各留一行痕，异常不许逃出（调参不该把界面打断）
        gui._proxy = _FakeProxy(boom=True)                          # noqa: SLF001
        gui._engines = [_FakeEngine(level_match=_FakeLevelMatcher(boom=True))]  # noqa: SLF001
        with _quiet() as buf:
            gui._apply_level_config(cfg)                            # noqa: SLF001
        log = buf.getvalue()
        assert "[level] ⚠️ 代理热更新失败：RuntimeError" in log, f"代理失败该留痕：{log!r}"
        assert "[level] ⚠️ 引擎热更新失败：RuntimeError" in log, f"引擎失败该留痕：{log!r}"
        assert "已落盘" in log, f"该说清楚「已落盘」，别让用户以为白改了：{log!r}"

        # 有代理就必须真的调到它（死分支删掉后没有「悄悄跳过」这条路）
        proxy = _FakeProxy()
        gui._proxy = proxy                                          # noqa: SLF001
        gui._engines = []                                           # noqa: SLF001
        with _quiet() as buf:
            gui._apply_level_config(cfg)                            # noqa: SLF001
        assert len(proxy.calls) == 1 and proxy.calls[0] is cfg, proxy.calls
        assert "都没在跑" not in buf.getvalue(), "有代理在跑就不该说「都没在跑」"

        # 都没在跑 → 明确留痕（禁静默降级）
        gui._proxy = None                                           # noqa: SLF001
        with _quiet() as buf:
            gui._apply_level_config(cfg)                            # noqa: SLF001
        assert "代理与引擎都没在跑：参数已落盘，下次启动生效" in buf.getvalue(), buf.getvalue()

        # 引擎没有 level_match 属性（旧引擎/别的东西）→ 不算命中，也不炸
        gui._engines = [_FakeEngine(level_match=None)]              # noqa: SLF001
        with _quiet() as buf:
            gui._apply_level_config(cfg)                            # noqa: SLF001
        assert "都没在跑" in buf.getvalue(), f"没有可热更新的对象就该留痕：{buf.getvalue()!r}"
    print("  ✓ _apply_level_config：代理/引擎抛异常各留一行痕且不外泄；有代理必调到；"
          "都没在跑 / 引擎无 level_match → 明确留痕；hasattr 死分支已删")

def test_sync_level_controls_state_follows_mode() -> None:
    """按模式置灰控件 + 换说明；`off` 时读数整行「—」；下拉三个名字与 `_level_mode_names` 同源。"""
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        def states() -> tuple[str, str]:
            return (str(gui._level_fixed_spin.cget("state")),         # noqa: SLF001
                    str(gui._level_offset_spin.cget("state")))        # noqa: SLF001

        names = gui._level_mode_names()                             # noqa: SLF001
        assert list(gui._level_mode_combo.cget("values")) == [       # noqa: SLF001
            names[k] for k in ("off", "fixed", "follow_mic")], "下拉项必须与 names 同源"

        gui._level_mode_var.set(names["off"])                       # noqa: SLF001
        gui._sync_level_controls_state()                            # noqa: SLF001
        assert states() == ("disabled", "disabled"), f"off 该两个都置灰：{states()}"
        assert gui._level_hint.cget("text") == t(                    # noqa: SLF001
            "已关闭：译音逐字节原样输出（与升级前行为一致）")
        assert gui._level_readout_lbl.cget("text") == "—", "off 不参与计算，读数该是 —"  # noqa: SLF001
        assert gui._level_gain_lbl.cget("text") == "—"              # noqa: SLF001

        gui._level_mode_var.set(names["fixed"])                     # noqa: SLF001
        gui._sync_level_controls_state()                            # noqa: SLF001
        assert states() == ("normal", "disabled"), f"fixed 只该放开固定增益：{states()}"
        assert gui._level_hint.cget("text") == t(                    # noqa: SLF001
            "固定增益：所有译音统一加减这么多 dB（负数=更轻）")

        gui._level_mode_var.set(names["follow_mic"])                # noqa: SLF001
        gui._sync_level_controls_state()                            # noqa: SLF001
        assert states() == ("disabled", "normal"), f"follow_mic 只该放开相对麦克风：{states()}"
        assert gui._level_hint.cget("text") == t(                    # noqa: SLF001
            "跟随麦克风：把译音对齐到你说话时的电平（跨设备免调）")

        # 名字 → 代码的反查（三档都得认得出来；认不出来会静默按 off 跑）
        for code in ("off", "fixed", "follow_mic"):
            gui._level_mode_var.set(names[code])                    # noqa: SLF001
            assert gui._level_mode_code() == code, code             # noqa: SLF001
        gui._level_mode_var.set("瞎写的")                            # noqa: SLF001
        assert gui._level_mode_code() == "off", \
            f"认不出来的文案该退回配置里的值（本例 off）：{gui._level_mode_code()!r}"  # noqa: SLF001
    print("  ✓ 控件态：off 全灰 + 读数「—」、fixed 只放开固定增益、follow_mic 只放开相对麦克风；"
          "三档说明文案各自对上；下拉项与 names 同源、反查三档都认得（认不出退回配置值）")
