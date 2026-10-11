"""「译音音量」界面层的离线验收：脏值不炸、三处同步、热更新留痕、读数 tick 的开关。

## 治什么

评审（PR #87）指出界面这条腿上有几处**只有真点过界面才看得见**的坑，纯逻辑测试
（`tests/test_level_match.py`）覆盖不到：

1. `_level_read_db` 漏接 `tk.TclError` —— 用户在 Spinbox 里手打 ``abc``、或清空后失焦，
   `DoubleVar.get()` 抛的是 TclError 而不是 ValueError。异常逃出 Tk 回调 → crashlog 记一条
   「Tk 回调异常」、设置**静默不保存**、控件还留着脏值（与「看到的==生效的」正好相反）。
2. `_apply_level_config` 里那条 `hasattr(p, "set_level_config")` 恒真（`LinuxMicProxy`
   继承基类），else 分支永远走不到，注释还是错的（写着「Linux 本期没接」）。
3. 改一次参数要同时落到**三处**：磁盘 config.yaml、内存真源 `self._cfg`（设置窗可反复重建，
   只落盘不同步就会关窗再开看见旧值）、正在跑的代理/引擎（热更新）。漏一处都是「我改了没生效」。
4. 500ms 读数 tick 必须**只在设置窗可见时跑**，关窗就地停下且不许自己爬起来。

## 离线保证

配置用**临时目录里的临时 config.yaml**（绝不碰仓库/用户真配置）；界面语言钉死 zh；
独立电平探针打桩成假类（`_NoProbe`），**绝不开真 loopback 设备**；麦克风代理因
`_is_test_process()` 守卫本来就不会被构造。跑法：

    .venv/Scripts/python.exe tests/test_level_gui.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

# CI / 新克隆上没有 key，而 load_config 默认要求 key。本用例只验界面，与 key 真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "levelguionly0123456789abcde")

# 界面文案跟随系统语言，钉死成中文保证可复现（产品代码不依赖这个补丁）。
import vlt.i18n as _i18n                            # noqa: E402
_i18n.detect_system_language = lambda: "zh"

from vlt.config_io import _yaml_scalar               # noqa: E402
from vlt.i18n import t                              # noqa: E402
from vlt.output import level_match                  # noqa: E402

_BASE = """\
session:
  model: qwen3.8-livetranslate-flash-realtime
  base_url: wss://example.invalid/realtime
  voice: Tina
  api_key: sk-unused
directions:
  mine:
    source_lang: zh
    target_lang: en
ui:
  lang: zh
output:
  audio:
    enabled: false
    buffer_ms: 300
    proxy:
      enabled: true
      passthrough_buffer_ms: 150
"""

# 老用户的 config.yaml 里整段 `level:` 都不存在 → `_yaml_set_or_create` 必须现建出来
# （用 `_yaml_set_in_text` 遇到缺父键会**静默 no-op**，设置就永远存不下去）。
_CFG_NO_LEVEL = _BASE

# 照 config.example.yaml 写的样子：段注释是回归点，写回后必须一行不少。
_CFG_WITH_LEVEL = _BASE + """\
    # 译音音量：把译音对齐到你的麦克风（默认关闭）
    level:
      mode: 'off'
      fixed_gain_db: 0.0
      offset_db: 0.0
"""


# ---------------------------------------------------------------- 小工具


def _n_comments(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.strip().startswith("#"))


@contextlib.contextmanager
def _quiet():
    """把界面自己的日志捕回来：既保住测试输出干净，又能断言「留痕了 / 没静默」。"""
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        yield buf


def _pump(gui, seconds: float = 0.3) -> None:       # noqa: ANN001
    """推着 Tk 事件循环走一会儿（`after` 的回调要事件循环转才跑得动）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        gui._root.update()                           # noqa: SLF001
        time.sleep(0.02)


class _NoProbe:
    """假「独立电平探针」：接口对齐 `vlt/level_probe.py`，但**绝不开设备**。

    打开设置窗会顺带起门限那条电平探针（`gui_audio.sync_gate_level_probe`），
    真跑起来就会去开 loopback 端点 —— 那是用户正在用的声卡，跑用例不许碰。
    """

    def __init__(self, *a, **kw) -> None:            # noqa: ANN002, ANN003
        self.running = False
        self.has_data = False
        self.level_db = -120.0
        self.chunks = 0
        self.last_error = None
        self.started = 0
        self._thread = None

    def start(self) -> None:
        self.started += 1
        self.running = True

    def stop(self) -> None:
        self.running = False


class _FakeLevelMatcher:
    """假 `LevelMatcher`：只记 `set_config` 收到过什么，`status()` 回一份可控快照。"""

    def __init__(self, status: dict | None = None, *, boom: bool = False) -> None:
        self.calls: list = []
        self._status = status
        self._boom = boom

    def set_config(self, cfg) -> None:               # noqa: ANN001
        if self._boom:
            raise RuntimeError("engine busy")
        self.calls.append(cfg)

    def status(self) -> dict:
        return dict(self._status or {})


class _FakeEngine:
    def __init__(self, level_match=None) -> None:    # noqa: ANN001
        self.running = True
        self.level_match = level_match


class _FakeProxy:
    """假麦克风代理：只实现界面这条腿用到的两样（`set_level_config` / `mic_reference_db`）。"""

    mode = "passthrough"

    def __init__(self, *, boom: bool = False, mic_db: float | None = None) -> None:
        self.calls: list = []
        self._boom = boom
        self._mic_db = mic_db

    def set_level_config(self, cfg) -> None:         # noqa: ANN001
        if self._boom:
            raise RuntimeError("stream gone")
        self.calls.append(cfg)

    @property
    def mic_reference_db(self) -> float | None:
        return self._mic_db


@contextlib.contextmanager
def _gui(body: str):
    """用**临时配置**起真窗口；退出时按真实生命周期收尾（关设置窗 → 停探针 → 销毁）。

    ⚠️ 收尾顺序不能反：电平探针跑在后台线程，根窗销毁后设置页那十几个 `tkinter.Variable`
    会留在循环垃圾里，GC 在探针线程上执行 `Variable.__del__`（内部调 `self._tk.call`）
    → 非主线程调 Tcl → Linux 上段错误（详见 tests/test_osc_port_save.py 的同款说明）。
    """
    import vlt.config as config_mod
    import vlt.gui as gui_mod
    import vlt.gui_audio as gui_audio_mod

    saved = (config_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG,
             gui_audio_mod.LevelProbe, gui_mod.LevelProbe)
    saved_env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    env_tmp = Path(tempfile.mkdtemp(prefix="vlt-levelgui-env-"))
    os.environ["USERPROFILE"] = str(env_tmp)
    os.environ["HOME"] = str(env_tmp)
    cfg_path = Path(tempfile.mkdtemp(prefix="vlt-levelgui-cfg-")) / "config.yaml"
    cfg_path.write_text(body, encoding="utf-8", newline="\n")
    config_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path                 # `_on_level_change` 就是往这里写
    gui_audio_mod.LevelProbe = _NoProbe               # type: ignore[assignment]
    gui_mod.LevelProbe = _NoProbe                     # type: ignore[assignment]
    gui = None
    try:
        gui = gui_mod.TranslationGUI()
        if gui._update_check_job is not None:          # 别真去连 GitHub 查更新  # noqa: SLF001
            gui._root.after_cancel(gui._update_check_job)   # noqa: SLF001
            gui._update_check_job = None               # noqa: SLF001
        gui._root.update()                             # noqa: SLF001
        yield gui, cfg_path
    finally:
        if gui is not None:
            # 逐步兜：某一步失败（例：用例自己把设置窗**销毁**过，`withdraw()` 会 TclError）
            # 不该把后面的收尾一起跳过 —— 少一步销毁根窗就会把窗口/解释器留在进程里。
            for step in (gui._stop_gate_probe, gui._close_settings, gui._root.destroy):
                try:
                    step()
                except Exception:                      # noqa: BLE001
                    pass
        (config_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG,
         gui_audio_mod.LevelProbe, gui_mod.LevelProbe) = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _i18n.set_language("zh")


# ---------------------------------------------------------------- ① 脏值不炸（#1）


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


# ---------------------------------------------------------------- ② 三处同步 + 落盘


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


# ---------------------------------------------------------------- ③ 热更新留痕（#5）


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


# ---------------------------------------------------------------- ④ 控件态随模式


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


# ---------------------------------------------------------------- ⑤ 读数 tick 的开关


def test_readout_tick_only_while_settings_visible() -> None:
    """★ 500ms 读数 tick：窗口可见才跑（且自己续期），关窗就地停、不许自己爬起来。"""
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        status = {"mode": "fixed", "mic_db": -24.0, "tts_db": -18.0,
                  "gain_db": -6.0, "headroom_db": 1.2, "fallback": False}
        gui._engines = [_FakeEngine(level_match=_FakeLevelMatcher(status=status))]  # noqa: SLF001
        gui._proxy = _FakeProxy(mic_db=-22.5)                       # noqa: SLF001
        names = gui._level_mode_names()                             # noqa: SLF001
        gui._level_mode_var.set(names["fixed"])                     # noqa: SLF001

        # ① 设置窗还关着 → 不排 tick（窗口关着不许每 500ms 去读引擎/代理）
        gui._refresh_level_readout()                                # noqa: SLF001
        assert gui._level_readout_job is None, "窗口不可见时不该排 tick"   # noqa: SLF001

        # ② 打开设置窗 → 排上 tick，读数出数（读的是**已在跑**的东西，绝不新开采集）
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._settings_win_viewable(), "前提：设置窗此刻应可见"   # noqa: SLF001
        gui._refresh_level_readout()                                # noqa: SLF001
        assert gui._level_readout_job is not None, "窗口可见时必须自己续期"  # noqa: SLF001
        line = str(gui._level_readout_lbl.cget("text"))              # noqa: SLF001
        for want in ("-24.0", "-18.0", "-6.0"):
            assert want in line, f"读数行缺 {want}：{line!r}"
        assert gui._level_gain_lbl.cget("text") == "-6.0 dB"        # noqa: SLF001

        # ③ 真等一跳（>500ms）且**不手动调**：读数自己刷新 → 证明「自己再排一次」有效
        status["gain_db"] = -9.5
        with _quiet():
            _pump(gui, 0.9)
        assert gui._level_gain_lbl.cget("text") == "-9.5 dB", \
            f"tick 没有自己续期：{gui._level_gain_lbl.cget('text')!r}"  # noqa: SLF001

        # ④ 引擎没给麦克风电平 → 回落读代理的 mic_reference_db（读数不许新开设备）
        status["mic_db"] = None
        gui._refresh_level_readout()                                # noqa: SLF001
        assert "-22.5" in str(gui._level_readout_lbl.cget("text")), \
            f"该回落读代理的参考电平：{gui._level_readout_lbl.cget('text')!r}"  # noqa: SLF001

        # ⑤ 三个来源全没有 → 整行「—」（绝不显示半真半假的数）
        gui._engines = []                                           # noqa: SLF001
        gui._proxy = None                                           # noqa: SLF001
        gui._refresh_level_readout()                                # noqa: SLF001
        assert gui._level_readout_lbl.cget("text") == "—"           # noqa: SLF001
        assert gui._level_gain_lbl.cget("text") == "—"              # noqa: SLF001

        # ⑥ 关窗 → 取消挂起的 tick 且不再续期
        with _quiet():
            gui._close_settings()                                   # noqa: SLF001
        assert gui._level_readout_job is None, "关窗后必须取消 tick"   # noqa: SLF001
        with _quiet():
            _pump(gui, 0.3)
        assert gui._level_readout_job is None, "关窗后不许自己爬起来"   # noqa: SLF001
    print("  ✓ 读数 tick：窗口关着不排；打开→排上并出读数（-24.0/-18.0/-6.0）；"
          "等 0.9s 自己刷到 -9.5 dB；引擎无麦克风电平→回落代理 -22.5；三源全无→整行「—」；"
          "关窗→取消且不再爬起来")


def test_readout_tick_cancelled_on_window_destroy() -> None:
    """★ 设置窗 / 根窗被**销毁**（而不是走 `close_settings`）时，挂起的读数 tick 必须一起取消。

    末影猫的 L5：`open_settings` 无条件排 500ms tick，收尾若不走 `close_settings`，那个
    `after` 就带着一个已经死掉的命令名到点 —— Tk 收尾时打一行
    `invalid command name "..._refresh_level_readout"`。

    判据用**状态**（`_level_readout_job` 还挂不挂着）而不是去抓那行 stderr：本机（Windows）
    三种收尾方式都没复现出那行（见 PR 说明），所以能被稳定验证的是「挂起的 tick 有没有被
    取消」—— 取消了，那条噪音在原理上就不可能再出现。
    """
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        assert "<Destroy>" in gui._settings_win.bind(), \
            "build_settings_dialog 必须把 <Destroy> 绑到取消读数 tick 上"  # noqa: SLF001
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._level_readout_job is not None, "前提：打开设置窗后应挂起读数 tick"  # noqa: SLF001

        gui._settings_win.destroy()                                 # 不给 close_settings 机会
        assert gui._level_readout_job is None, \
            "设置窗被销毁后必须取消挂起的 tick（否则到点就是 invalid command name）"
        with _quiet():
            _pump(gui, 0.3)
        assert gui._level_readout_job is None, "销毁后不许自己爬起来"   # noqa: SLF001
        with _quiet():
            gui._refresh_level_readout()                            # noqa: SLF001
        assert gui._level_readout_job is None, "窗口已销毁时也不许再排 tick"   # noqa: SLF001

    # 反向：正常收尾（close_settings 后再销毁根窗）仍只留一行「取消」语义，且幂等
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._level_readout_job is not None                    # noqa: SLF001
        gui._cancel_level_tick()                                     # noqa: SLF001
        gui._cancel_level_tick()                                     # 幂等：第二次不许抛
        assert gui._level_readout_job is None                        # noqa: SLF001
        with _quiet():
            gui._close_settings()                                   # noqa: SLF001
    print("  ✓ 读数 tick：设置窗被销毁（不走 close_settings）→ 挂起的 tick 被取消且不再续期；"
          "取消本身幂等")


def test_child_widget_destroy_does_not_cancel_readout_tick() -> None:
    """★ 设置窗的**子控件**被销毁，不许取消读数 tick（issue #89）。

    Tk 的默认 bindtags 是 `(widget, class, toplevel, "all")`，所以绑在设置窗 Toplevel 上的
    `<Destroy>` **也会被子控件销毁触发**（实测 `child.destroy()` → 回调收到
    `e.widget == '.!toplevel.!frame'`）。不加 `e.widget is win` 过滤时：一旦以后有人动态
    重建设置页，窗口**仍可见**的读数 tick 就会被悄悄取消 —— 读数从此不再刷新、且没有任何提示。

    判据用**状态**（`_level_readout_job` 还挂不挂着），不是去抓某行 stderr：过滤正确时
    子控件销毁前后 job 都在；把过滤去掉（不再按 `e.widget` 判），这条立刻变红。
    """
    import tkinter as tk
    with _gui(_CFG_WITH_LEVEL) as (gui, _cfg_path):
        with _quiet():
            gui._open_settings()                                    # noqa: SLF001
            gui._root.update()                                      # noqa: SLF001
        assert gui._level_readout_job is not None, "前提：打开设置窗后应挂起读数 tick"  # noqa: SLF001

        # 造一个子控件再销毁它（模拟动态重建设置页 / 某控件内部销毁子件）
        child = tk.Frame(gui._settings_win)                         # noqa: SLF001
        child.pack()
        gui._root.update()                                          # noqa: SLF001
        child.destroy()
        gui._root.update()                                          # noqa: SLF001
        assert gui._level_readout_job is not None, \
            "销毁设置窗的**子控件**不该取消读数 tick（<Destroy> 须按 e.widget is win 过滤）"  # noqa: SLF001
        with _quiet():
            _pump(gui, 0.3)
        assert gui._level_readout_job is not None, "子控件销毁后读数 tick 仍应活着"   # noqa: SLF001

        with _quiet():
            gui._close_settings()
    print("  ✓ 读数 tick：销毁设置窗的**子控件**（非窗口本身）→ 挂起的 tick 不被取消"
          "（<Destroy> 已按 e.widget is win 过滤）")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_level_read_db_survives_dirty_spinbox,
        test_on_level_change_persists_syncs_and_hot_updates,
        test_level_write_preserves_comments_and_siblings,
        test_apply_level_config_leaves_trace_and_never_raises,
        test_sync_level_controls_state_follows_mode,
        test_readout_tick_only_while_settings_visible,
        test_readout_tick_cancelled_on_window_destroy,
        test_child_widget_destroy_does_not_cancel_readout_tick,
    ]
    print("test_level_gui:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
