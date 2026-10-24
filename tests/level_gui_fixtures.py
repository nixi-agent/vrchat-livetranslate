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

os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "levelguionly0123456789abcde")

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

_CFG_NO_LEVEL = _BASE

_CFG_WITH_LEVEL = _BASE + """\
    # 译音音量：把译音对齐到你的麦克风（默认关闭）
    level:
      mode: 'off'
      fixed_gain_db: 0.0
      offset_db: 0.0
"""

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

__all__ = ['ROOT', '_CFG_NO_LEVEL', '_CFG_WITH_LEVEL', '_FakeEngine', '_FakeLevelMatcher', '_FakeProxy', '_gui', '_n_comments', '_pump', '_quiet', '_yaml_scalar', 'level_match', 't', 'yaml']
