"""麦克风代理（MicProxy）**接线**验收：GUI ↔ 代理 ↔ 引擎三者是不是真的连上了。

跑法：.venv/Scripts/python.exe tests/test_proxy_wiring.py

## 为什么要有这一层（test_micproxy / test_proxy_config 不是都测过了吗）

那两份测的是**零件**：`test_micproxy.py` 测代理自己的重采样/环形缓冲/档位切换，
`test_proxy_config.py` 测配置解析与设置页控件态。而本次改动是**接线**——
代理要在 GUI 构造时就起来、要把 `translated_sink` 递进 `Engine(audio_sink=...)`、
要在翻译启停时 `set_translation_active()`、要让设置页的勾选框真的落到启停上。
接线断掉时零件全是好的（#12 就是这个形态：函数在、调用点的 kwarg 对不上），
所以必须有一份**只盯接线**的用例。

## 打桩纪律（与 test_micproxy.py 同一条，绝不碰真实音频设备）

  · `_start_proxy()` 会真构造 `MicProxy` 并 `start()` —— 涉及开流的两条用例
    （不可用 / 可用）全程用假 `sounddevice`、假采集后端、假 `pick_output_device`；
  · 引擎一律换成 `_FakeEngine`（只记录构造参数），绝不真连 WebSocket / 真开麦克风；
  · GUI 用 **headless**（不建 Tk 窗口），控件用 `_Widget` 占位。

覆盖任务书的四条：
  (a) 代理不可用（没找到虚拟声卡）→ **不注入**引擎、界面回落、状态栏 + 日志各留一行痕；
  (b) 代理可用（打桩）→ 引擎拿到 `proxy.translated_sink`；
  (c) 翻译启停 → `set_translation_active(True/False)`；
  (d) 设置页关掉勾选 → 走真正的 `_restart_proxy()`：关代理、引擎回到自建。
外加两条顺带钉住的：缓冲改动落到 `reopen_with()`；退出时 `close()` 幂等。
"""
from __future__ import annotations

import contextlib
import io
import os
import struct
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

import yaml                                         # noqa: E402

from vlt.output.micproxy import (                   # noqa: E402
    MODE_PASSTHROUGH,
    MODE_TRANSLATED,
)


# ---------------------------------------------------------------- 小工具


def _isolate_env(tmp: Path) -> dict:
    """把 HOME/USERPROFILE 指到临时目录并摘掉环境变量 key：隔绝本机可能存在的 key 来源。"""
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


_CONFIG_TEXT = (
    "ui:\n"
    "  lang: zh\n"
    "session:\n"
    "  api_key: test-key-not-real\n"
    "output:\n"
    "  capture:\n"
    "    mic_device: ''\n"
    "  audio:\n"
    "    enabled: true\n"
    "    device: ['FakeCard Output']\n"
    "    sample_rate: 48000\n"
    "    buffer_ms: 300\n"
    "    max_buffer_ms: 2000\n"
    "    proxy:\n"
    "      enabled: true\n"
    "      passthrough_buffer_ms: 150\n"
)


def _temp_config() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="vlt-proxy-wiring-"))
    p = tmp / "config.yaml"
    p.write_text(_CONFIG_TEXT, encoding="utf-8", newline="\n")
    return p


class _Widget:
    """占位控件：只记 configure 的关键字（headless 下没有真 Tk 控件）。"""

    def __init__(self) -> None:
        self.kw: dict = {}

    def configure(self, **kw) -> None:  # noqa: ANN003
        self.kw.update(kw)

    def cget(self, key: str):
        return self.kw.get(key)


class _Var:
    """占位 Tk 变量（BooleanVar / StringVar / IntVar 都只需要 get/set）。"""

    def __init__(self, value) -> None:               # noqa: ANN001
        self._v = value

    def get(self):
        return self._v

    def set(self, value) -> None:                    # noqa: ANN001
        self._v = value


class _Root:
    """占位 Tk root：只提供 after/after_cancel。"""

    def after(self, *_a, **_k) -> str:
        return "job"

    def after_cancel(self, *_a, **_k) -> None:
        return None


# ---------------------------------------------------------------- 音频打桩


class _FakeOutStream:
    """假 PortAudio 输出流：记录 start/stop/close，绝不触发回调。"""

    instances: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.started = self.stopped = self.closed = False
        _FakeOutStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakeSource:
    """假麦克风采集源：先吐给定的几块，之后返回 None（模拟「还活着但没数据」）。"""

    def __init__(self, chunks, *, rate=16000, channels=1) -> None:  # noqa: ANN001
        self.rate = rate
        self.channels = channels
        self._chunks = list(chunks)
        self.closed = False

    async def read(self, timeout: float = 1.0):      # noqa: ARG002
        import asyncio

        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(0.01)
        return None

    def close(self) -> None:
        self.closed = True


class _FakeCaptureBackend:
    def __init__(self, source) -> None:              # noqa: ANN001
        self._src = source

    def open_mic(self, name, *, rate, channels, blocksize):   # noqa: ARG002, ANN001
        return self._src


@contextlib.contextmanager
def _stubbed_audio(*, device=(11, "FakeCard", 48000), gui=None):
    """把「真开音频设备」的三处换成假的（与 tests/test_micproxy.py 同一套打桩）。

    *device* 传 None 即模拟「机器上没装虚拟声卡」——那是最常见的降级现场。
    上下文 yield 出**假麦克风采集源**：用例可据此断言「关掉代理后采集源被释放」
    （`MicProxy.close()` 会 join 直通线程，线程的 finally 里必然调过 `src.close()`，
      所以这条断言是确定的，不靠睡多久）。

    ⚠️ 传 *gui* 时，退出会**先关代理再撤补丁**：代理的麦克风线程要去
    ``platform.capture_backend()`` 取后端，补丁先撤的话（断言失败中途退出就是这种）
    它就会去开**用户真实的麦克风**。
    """
    from vlt import platform
    from vlt.output import micproxy as MP

    _FakeOutStream.instances.clear()
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.RawOutputStream = lambda **kw: _FakeOutStream(**kw)      # type: ignore[attr-defined]
    fake_sd.query_devices = lambda *a, **k: {                        # type: ignore[attr-defined]
        "name": "FakeCard", "default_samplerate": 48000}

    src = _FakeSource([struct.pack("<h", 500) * 1600], rate=16000, channels=1)
    saved_sd = sys.modules.get("sounddevice")
    saved_cb = platform.capture_backend
    saved_pick = MP.pick_output_device
    # ★ 「两侧都桩」：`MicProxy.start()` 的**最后一道保险**（测试进程守卫）也得在这里
    #   显式放行 —— 本上下文里所有真开流的动作都已经打桩了，放行是安全的；不放行的话
    #   代理永远起不来，而这两条用例验的正是「打桩环境下代理能起来并接上引擎」。
    saved_guard = MP._test_process_guard
    MP._test_process_guard = lambda *a, **k: None                    # type: ignore[assignment]
    # ★ 门面也钉成 Windows 那个实现：本文件测接线，平台差异（Linux 走 pw-cat 管道 +
    #   运行时声明节点）不该在这里被触发 —— 不钉的话 Linux 上会真去声明 PipeWire 节点。
    #   门面被钉住后，下面那套「假 sounddevice」正好就是它需要的全部依赖。
    from vlt.output.micproxy import MicProxy as _WinMicProxy
    saved_factory = platform.create_mic_proxy

    sys.modules["sounddevice"] = fake_sd
    platform.capture_backend = lambda: _FakeCaptureBackend(src)      # type: ignore[assignment]
    MP.pick_output_device = lambda patterns=None: device             # type: ignore[assignment]
    platform.create_mic_proxy = (                                    # type: ignore[assignment]
        lambda audio_cfg, mic_name=None, on_status=None:
            _WinMicProxy(audio_cfg=audio_cfg, mic_name=mic_name,
                         on_status=on_status or (lambda *_a: None)))
    try:
        yield src
    finally:
        if gui is not None:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    gui._close_proxy()
            except Exception:                        # noqa: BLE001
                pass
        platform.create_mic_proxy = saved_factory                # type: ignore[assignment]
        MP._test_process_guard = saved_guard                     # type: ignore[assignment]
        if saved_sd is None:
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = saved_sd
        platform.capture_backend = saved_cb                          # type: ignore[assignment]
        MP.pick_output_device = saved_pick                           # type: ignore[assignment]


# ---------------------------------------------------------------- GUI / 引擎替身


@contextlib.contextmanager
def _wired_gui():
    """起一个 **headless** GUI，并把「构造代理」的两道门放开（临时配置 + 假测试号）。

    · `DEFAULT_CONFIG` 指到临时文件：既隔绝用户真实配置，也让 YAML 写回可断言；
    · `_is_test_process=lambda: False`：**只在这一个上下文里**放开——所有真会开流的
      动作都在 `_stubbed_audio()` 里面，绝不触碰用户音频图。

    ⚠️ 本文件测的是**接线**（GUI ↔ 代理 ↔ 引擎），不是平台驱动：平台差异现在只在
    `platform.create_mic_proxy()` 门面里，由 `_stubbed_audio()` 把它钉成 Windows 那个
    实现（假 `sounddevice` 正好满足它）。所以在 Linux CI 上照样跑，也不会去声明
    PipeWire 节点。
    """
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod

    saved_env = _isolate_env(Path(tempfile.mkdtemp(prefix="vlt-proxy-wenv-")))
    cfg_path = _temp_config()
    saved = (cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG,
             gui_mod._is_test_process)
    cfg_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui_mod._is_test_process = lambda: False         # type: ignore[assignment]

    from vlt.gui import TranslationGUI

    gui = None
    try:
        # load_config 那行「尚未配置 API key」走的是 stderr，两个流都得拦
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            gui = TranslationGUI(headless=True)
        gui._root = _Root()                          # type: ignore[assignment]
        gui._voice_ctx.voice_mode_btn = _Widget()    # 主界面「原声/译音」按钮占位
        gui._cfg.session_base["api_key"] = "test-key-not-real"
        yield gui, cfg_path
    finally:
        if gui is not None:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    gui._close_proxy()
            except Exception:                        # noqa: BLE001
                pass
        cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, \
            gui_mod._is_test_process = saved
        _restore_env(saved_env)
        from vlt import i18n
        i18n.set_language("zh")


def _settle(seconds: float = 0.08) -> None:
    """让真 MicProxy 的麦克风直通线程跑完它的开机留痕。

    那行 ``[proxy][info] 麦克风直通已启动…`` 是**异步**打的：不等一下它就会落在
    ``redirect_stdout`` 之外，漏到控制台上（看起来像用例在乱打日志）。
    """
    time.sleep(seconds)


def _capture_statuses(gui) -> list[tuple[str, str]]:  # noqa: ANN001
    """把状态栏写入抓下来（headless 下 `_set_status` 本来只记级别、不碰控件）。"""
    seen: list[tuple[str, str]] = []

    def _fake(level: str, msg: str) -> None:
        seen.append((level, msg))
        gui._last_status_level = level

    gui._set_status = _fake                          # type: ignore[method-assign]
    gui._engine_ctx.set_status_fn = _fake            # 同步 EngineCtx 里已经绑走的那份
    return seen


class _FakeSink:
    """`TranslatedSink` 替身：引擎只认这几个成员。"""

    device_name = "FakeCard Output"
    opened = True

    def __init__(self) -> None:
        self.pushed: list[bytes] = []
        self.sentences = 0
        self.close_calls = 0

    def push(self, pcm) -> None:                     # noqa: ANN001
        self.pushed.append(pcm)

    def end_sentence(self) -> None:
        self.sentences += 1

    def close(self) -> None:
        self.close_calls += 1


class _FakeProxy:
    """`MicProxy` 替身：只记录接线相关的调用，绝不开任何音频流。"""

    def __init__(self, mode: str = MODE_PASSTHROUGH) -> None:
        self.mode = mode
        self.opened = True
        self.active_calls: list[bool] = []
        self.reopen_calls: list[tuple] = []
        self.mic_calls: list = []
        self.close_calls = 0
        self.translated_sink = _FakeSink()

    @property
    def underrun_count(self) -> int:
        return 0

    def set_translation_active(self, active: bool) -> None:
        self.active_calls.append(bool(active))
        if not active and self.mode == MODE_TRANSLATED:
            self.mode = MODE_PASSTHROUGH             # 与真代理同语义：停翻译即回落

    def set_mode(self, mode: str) -> bool:
        if mode == MODE_TRANSLATED and True not in self.active_calls:
            return False
        self.mode = mode
        return True

    def reopen_with(self, passthrough_ms=None, translated_buffer_ms=None) -> None:
        self.reopen_calls.append((passthrough_ms, translated_buffer_ms))

    def reopen_mic(self, mic_name) -> None:              # noqa: ANN001
        self.mic_calls.append(mic_name)

    def close(self) -> None:
        self.close_calls += 1
        self.opened = False


class _FakeEngine:
    """引擎替身：本文件只关心构造参数里的 `audio_sink` 有没有被注进来。"""

    instances: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.running = False
        self.start_calls = 0
        self.stop_requests = 0
        _FakeEngine.instances.append(self)

    def start(self) -> None:
        self.start_calls += 1
        self.running = True

    def request_stop(self) -> None:
        self.stop_requests += 1
        self.running = False

    def wait_stopped(self, timeout: float = 5.0) -> bool:   # noqa: ARG002
        return True


@contextlib.contextmanager
def _fake_engine_class():
    """把 `gui_engine.Engine` 换成替身（`EngineEvents` 保持真的：它只是个数据类）。"""
    from vlt import gui_engine

    saved = gui_engine.Engine
    _FakeEngine.instances.clear()
    gui_engine.Engine = _FakeEngine                  # type: ignore[assignment]
    try:
        yield _FakeEngine.instances
    finally:
        gui_engine.Engine = saved                    # type: ignore[assignment]


def _arm_engine_ctx(gui):                            # noqa: ANN001, ANN202
    """把 headless GUI 的 EngineCtx 武装到「能跑 start()/stop()」的最小可用状态。"""
    gui._power_btn = _Widget()                       # 单按钮开关占位（_sync_engine_ctx 会拷进 ctx.power_btn）
    gui._sync_engine_ctx()
    ctx = gui._engine_ctx
    ctx.power_state_fn = gui._set_power_state        # 单按钮刷新回调（bind_gui_callbacks 已绑，这里显式补一遍）
    ctx.direction_var = _Var("mine")
    ctx.chatbox_var = _Var(True)
    ctx.overlay_var = _Var(False)                    # 手腕屏/桌面字幕都不勾 → 两条腿直接短路
    ctx.desktop_var = _Var(False)
    ctx.vmic_var = _Var(True)                        # 「译音输出」勾上：这才有 audio_sink 的事
    ctx.lang_pair = {"source": "zh", "target": "en"}
    ctx.refresh_api_key_fn = None                    # 别让用例去碰真实 key 解析
    ctx.start_room_fn = None
    ctx.refresh_room_status_fn = None
    ctx.sync_gate_probe_fn = None
    ctx.set_text_input_enabled_fn = None
    # 直接驱动 start_engine(ctx, 0) 时没人替它建 specs（平时是 start() 干的活），
    # 不摆好的话函数第一行 `index >= len(specs)` 就返回了，什么都测不到。
    ctx.specs = [("mine", "mine", "mic", "zh", "en")]
    ctx.sinks = {"chatbox"}
    ctx.pending_starts = 1
    return ctx


def _wire_settings_controls(gui, *, enabled: bool) -> None:   # noqa: ANN001
    """给 headless GUI 补上设置页「麦克风代理」那几件控件的占位。"""
    gui._proxy_check = _Widget()
    gui._proxy_enabled_var = _Var(enabled)
    gui._passthrough_var = _Var(150)
    gui._translated_buf_var = _Var(300)
    # ⚠️ 不能是 None：`_on_proxy_buffer_change` 开头就用 `_passthrough_spin is None`
    #    当「设置页没建」的判据，给 None 的话整条写回路径直接短路。
    gui._passthrough_spin = _Widget()
    gui._translated_spin = _Widget()
    gui._proxy_hint = _Widget()


# ---------------------------------------------------------------- (a) 不可用


def test_proxy_unavailable_falls_back_with_trace() -> None:
    """(a) 机器上没有虚拟声卡 → 代理不可用：**不注入引擎** + 界面回落 + 双留痕。"""
    from vlt import gui_engine
    from vlt.i18n import t

    with _wired_gui() as (gui, _cfg_path):
        seen = _capture_statuses(gui)
        btn = gui._voice_ctx.voice_mode_btn

        with _stubbed_audio(device=None, gui=gui):   # 没装虚拟声卡
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = gui._start_proxy()
            out = buf.getvalue()

        assert ok is False, "找不到虚拟声卡时 _start_proxy() 必须返回 False"
        assert gui._proxy is None, f"降级后不该留下半死的代理实例：{gui._proxy!r}"

        # ★ 禁静默降级：日志一行 + 状态栏一行，两处都要有
        proxy_lines = [ln for ln in out.splitlines() if ln.startswith("[proxy]")]
        assert proxy_lines, f"代理不可用必须在 stdout 留痕（[proxy] 开头）：{out!r}"
        assert any("回到旧行为" in ln for ln in proxy_lines), \
            f"留痕必须说清楚回落到了旧行为：{proxy_lines!r}"
        assert seen and seen[-1][0] == "warn", f"状态栏必须给出警告级提示：{seen!r}"
        assert seen[-1][1] == t("麦克风代理不可用（虚拟声卡没打开？）；原声/译音切换已禁用"), \
            f"状态栏文案不对：{seen[-1]!r}"

        # 界面回落：主界面「原声/译音」按钮置灰、文案弹回原声档
        assert str(btn.cget("state")) == "disabled", f"无代理时按钮必须置灰：{btn.kw!r}"
        assert btn.cget("text") == t("🎙 原声"), f"无代理时文案应是原声档：{btn.kw!r}"

        # 引擎侧：没有代理 → audio_sink 必须是 None（引擎自建虚拟声卡输出 = 旧行为）
        ctx = _arm_engine_ctx(gui)
        with _fake_engine_class() as made:
            gui_engine.start_engine(ctx, 0)
        assert len(made) == 1, f"应构造 1 个引擎，实际 {len(made)}"
        assert "audio_sink" in made[0].kw, "构造引擎时必须显式传 audio_sink（None 也要传）"
        assert made[0].kw["audio_sink"] is None, \
            f"代理不可用时不许注入 sink（引擎要自建）：{made[0].kw['audio_sink']!r}"
        assert gui_engine._proxy_of(ctx) is None, "_proxy_of 应返回 None"
    print("  ✓ (a) 代理不可用：不注入引擎 + 按钮置灰 + 状态栏/日志双留痕")


# ---------------------------------------------------------------- (b) 可用


def test_proxy_available_injects_translated_sink() -> None:
    """(b) 代理可用（打桩真 MicProxy）→ 引擎拿到 `proxy.translated_sink`。"""
    from vlt import gui_engine

    with _wired_gui() as (gui, _cfg_path):
        _capture_statuses(gui)
        with _stubbed_audio(gui=gui) as src:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = gui._start_proxy()
                _settle()                            # 收编直通线程那行异步留痕
            out = buf.getvalue()
            assert ok is True, f"打桩环境下代理应能起来；输出={out!r}"
            proxy = gui._proxy
            assert proxy is not None and proxy.opened, "代理应处于已打开状态"
            assert _FakeOutStream.instances, "应该开了一条虚拟声卡输出流"
            assert "[proxy] 麦克风代理已启动" in out, f"启动成功必须留痕：{out!r}"
            assert "直通缓冲 150ms" in out, f"启动摘要应带上配置里的直通缓冲：{out!r}"

            ctx = _arm_engine_ctx(gui)
            with _fake_engine_class() as made:
                gui_engine.start_engine(ctx, 0)
            assert len(made) == 1, f"应构造 1 个引擎，实际 {len(made)}"
            sink = made[0].kw.get("audio_sink")
            assert sink is proxy.translated_sink, \
                f"引擎必须拿到代理的 translated_sink，实际 {sink!r}"
            assert sink.device_name == "FakeCard", f"垫片应报告代理那条流的设备名：{sink.device_name!r}"

            # 退出清理：close() 幂等，且不留麦克风线程/输出流
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                gui._close_proxy()
                gui._close_proxy()                   # 第二次必须是无操作
            assert all(s.closed for s in _FakeOutStream.instances), "输出流应被关掉"
            assert src.closed, "麦克风采集源必须被释放：不许留下直通线程（任务书第 7 条）"
            assert gui._proxy is None, "关闭后引用应摘掉"
            assert buf2.getvalue().count("[proxy] 麦克风代理已关闭") == 1, \
                f"close 幂等：只该留一行痕，实际={buf2.getvalue()!r}"
    print("  ✓ (b) 代理可用：引擎拿到 translated_sink + 启动/关闭各留一行痕（close 幂等）")


# ---------------------------------------------------------------- (c) 启停联动


def test_translation_start_stop_notifies_proxy() -> None:
    """(c) 翻译开始/停止 → `set_translation_active(True/False)`，且启动日志写明走代理。"""
    from vlt import gui_engine

    with _wired_gui() as (gui, _cfg_path):
        _capture_statuses(gui)
        fake = _FakeProxy()
        gui._proxy = fake
        ctx = _arm_engine_ctx(gui)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with _fake_engine_class() as made:
                gui_engine.start(ctx)
                start_out = buf.getvalue()
                assert fake.active_calls == [True], \
                    f"开始翻译必须先通知代理：{fake.active_calls!r}"
                assert len(made) == 1 and made[0].kw["audio_sink"] is fake.translated_sink, \
                    "走 start() 这条完整路径时也要注入 sink"
                gui_engine.stop(ctx)
        stop_out = buf.getvalue()

        assert fake.active_calls == [True, False], \
            f"停止翻译必须回落原声档：{fake.active_calls!r}"
        assert fake.mode == MODE_PASSTHROUGH, f"停翻译后档位应回落：{fake.mode!r}"
        assert "经麦克风代理" in start_out, f"启动日志要写明译音走的是代理：{start_out!r}"
        assert any(ln.startswith("[proxy] 翻译开始") for ln in start_out.splitlines()), \
            f"翻译开始要留痕：{start_out!r}"
        assert any(ln.startswith("[proxy] 翻译停止") for ln in stop_out.splitlines()), \
            f"翻译停止要留痕：{stop_out!r}"

        # 主界面按钮：翻译在跑 + 有代理 → 解禁；停了 → 置灰（gui._stop 薄壳负责刷新）
        btn = gui._voice_ctx.voice_mode_btn
        gui._engines = [_FakeEngine()]
        gui._engines[0].running = True
        gui._refresh_voice_mode_btn()
        assert str(btn.cget("state")) == "normal", f"有代理且翻译在跑应解禁：{btn.kw!r}"
        gui._engines = []
        gui._refresh_voice_mode_btn()
        assert str(btn.cget("state")) == "disabled", f"翻译停了应置灰：{btn.kw!r}"
    print("  ✓ (c) 翻译启停 → set_translation_active(True/False) + 按钮解禁/置灰 + 日志留痕")


# ---------------------------------------------------------------- (d) 设置页开关


def test_settings_toggle_off_restarts_proxy_and_unwires_engine() -> None:
    """(d) 设置页取消勾选 → 真走 `_restart_proxy()`：关代理、引擎回到自建。"""
    from vlt import gui_engine
    from vlt.i18n import t

    with _wired_gui() as (gui, cfg_path):
        seen = _capture_statuses(gui)
        _wire_settings_controls(gui, enabled=True)
        fake = _FakeProxy()
        gui._proxy = fake
        running = _FakeEngine()
        running.running = True
        gui._engines = [running]                     # 翻译进行中关代理 → 必须额外警告

        gui._proxy_enabled_var.set(False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._on_proxy_toggle()
        out = buf.getvalue()

        # 配置落盘 + 内存快照都跟上（下次重开代理读的是内存那份）
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        proxy_cfg = ((data.get("output") or {}).get("audio") or {}).get("proxy") or {}
        assert proxy_cfg.get("enabled") is False, f"勾选状态没落盘：{proxy_cfg!r}"
        assert gui._proxy_wanted() is False, "内存快照没跟上落盘"

        # 代理真的被关掉了（不是只把引用丢掉）
        assert fake.close_calls == 1, f"应调用一次 close()：{fake.close_calls}"
        assert gui._proxy is None, "关掉后引用应摘掉"

        # 可见降级双留痕
        assert any(ln.startswith("[proxy]") for ln in out.splitlines()), \
            f"关代理必须留痕：{out!r}"
        assert "本轮译音输出已失效" in out, \
            f"翻译进行中关代理，必须说清楚这一轮译音没人接管了：{out!r}"
        assert seen[-1] == ("info", t("麦克风代理已关闭（回到旧行为）")), f"状态栏文案不对：{seen[-1]!r}"
        assert gui._proxy_hint.cget("text") == t(
            "已关闭：回到旧行为（译音输出随翻译启停，主界面切换开关置灰）"), \
            f"设置页提示没跟着刷新：{gui._proxy_hint.kw!r}"

        # 引擎回到自建（audio_sink=None）
        ctx = _arm_engine_ctx(gui)
        with _fake_engine_class() as made:
            gui_engine.start_engine(ctx, 0)
        assert made[0].kw["audio_sink"] is None, \
            f"代理关掉后引擎必须自建译音输出：{made[0].kw['audio_sink']!r}"

        # 再勾回来 → 真的重新启动（打桩环境里用真 MicProxy）
        gui._proxy_enabled_var.set(True)
        with _stubbed_audio(gui=gui):
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                gui._on_proxy_toggle()
                _settle()                # 收编直通线程那行异步留痕
            assert gui._proxy is not None and gui._proxy.opened, \
                f"重新勾选后代理应起来；输出={buf2.getvalue()!r}"
            assert seen[-1] == ("info", t("麦克风代理已启用")), f"状态栏文案不对：{seen[-1]!r}"
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            proxy_cfg = ((data.get("output") or {}).get("audio") or {}).get("proxy") or {}
            assert proxy_cfg.get("enabled") is True, f"重新勾选没落盘：{proxy_cfg!r}"
            with contextlib.redirect_stdout(io.StringIO()):
                gui._close_proxy()       # 撤补丁前收尾：别让麦克风线程去碰真实采集后端
    print("  ✓ (d) 设置页勾选框 → 真 _restart_proxy()：关代理/引擎自建/重新勾选能起来（均留痕）")


# ---------------------------------------------------------------- 缓冲即时生效


def test_buffer_change_reaches_running_proxy() -> None:
    """缓冲改动 → 代理在跑就 `reopen_with()` 即时生效，不在跑就只落盘并如实说明。"""
    from vlt.i18n import t

    with _wired_gui() as (gui, cfg_path):
        seen = _capture_statuses(gui)
        _wire_settings_controls(gui, enabled=True)
        fake = _FakeProxy()
        gui._proxy = fake

        gui._passthrough_var.set(240)
        gui._translated_buf_var.set(600)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._on_proxy_buffer_change()
        assert fake.reopen_calls == [(240, 600)], f"应即时下发新缓冲：{fake.reopen_calls!r}"
        assert seen[-1] == ("info", t("缓冲已更新（即时生效）")), f"状态栏文案不对：{seen[-1]!r}"
        assert "[proxy] 缓冲已更新" in buf.getvalue(), f"即时生效必须留痕：{buf.getvalue()!r}"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        audio = (data.get("output") or {}).get("audio") or {}
        assert (audio.get("proxy") or {}).get("passthrough_buffer_ms") == 240, audio
        assert audio.get("buffer_ms") == 600, audio

        # 代理不在跑：只落盘，且状态栏要说清楚「下次生效」而不是假装已生效
        gui._proxy = None
        gui._passthrough_var.set(200)
        gui._translated_buf_var.set(400)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            gui._on_proxy_buffer_change()
        assert fake.reopen_calls == [(240, 600)], f"代理不在跑时不许下发：{fake.reopen_calls!r}"
        assert seen[-1] == ("warn", t("虚拟声卡未打开——检查「译音输出」设备；缓冲改动已存，下次生效")), \
            f"状态栏文案不对：{seen[-1]!r}"
        assert "[proxy]" in buf2.getvalue(), f"必须留痕：{buf2.getvalue()!r}"
    print("  ✓ 缓冲改动：在跑→reopen_with 即时生效；不在跑→只落盘 + 如实提示下次生效")


def test_mic_device_change_reopens_passthrough() -> None:
    """改麦克风下拉 → 直通腿即时换输入（`reopen_mic`）；名字没变 / 代理没在跑 → 不调用。

    接线点：`_on_device_change` 先保存，再比较前后 `capture.mic_device`，变了才动代理。
    """
    from vlt import gui_audio

    with _wired_gui() as (gui, _cfg_path):
        seen = _capture_statuses(gui)
        fake = _FakeProxy()
        gui._proxy = fake

        orig = gui_audio.on_device_change

        def _setter(val):                                # noqa: ANN001, ANN202
            def _f(_ctx, cfg, _names) -> None:           # noqa: ANN001
                cfg.output.setdefault("capture", {})["mic_device"] = val
            return _f

        try:
            gui_audio.on_device_change = _setter("Mic Y")        # type: ignore[assignment]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                gui._on_device_change()
            assert fake.mic_calls == ["Mic Y"], f"改麦没触发 reopen_mic：{fake.mic_calls!r}"
            assert seen and seen[-1][0] == "info", f"状态栏级别不对：{seen[-1] if seen else None!r}"
            assert "直通麦克风已切换" in buf.getvalue(), f"必须留痕：{buf.getvalue()!r}"

            # 名字没变 → 不该再触发
            with contextlib.redirect_stdout(io.StringIO()):
                gui._on_device_change()
            assert fake.mic_calls == ["Mic Y"], f"名字没变不该触发：{fake.mic_calls!r}"

            # 代理没在跑 → 只落盘、不调用（下次 start() 生效）
            gui._proxy = None
            gui_audio.on_device_change = _setter("Mic Z")        # type: ignore[assignment]
            with contextlib.redirect_stdout(io.StringIO()):
                gui._on_device_change()
            assert fake.mic_calls == ["Mic Y"], f"代理没跑不该调用：{fake.mic_calls!r}"
        finally:
            gui_audio.on_device_change = orig            # type: ignore[assignment]
    print("  ✓ 改麦：在跑→reopen_mic 即时生效；名字没变 / 代理没跑→不调用")


# ---------------------------------------------------------------- 译音音量匹配接线


def _fake_translated_pcm(frames: int = 480, amp: int = 4000) -> bytes:
    """一段 48k 立体声 int16 假译音：*frames* 帧 → ``frames * 4`` 字节。"""
    return struct.pack("<h", amp) * (frames * 2)


def _emit_holder(sink, level):                       # noqa: ANN001, ANN202
    """只装 `Engine._emit_translated` 真正用到的两样东西（`_virtualmic` / `_level`）。

    不构造真 Engine：那要 API key、要连 WebSocket，而这两条用例验的只是
    「译音 PCM 的**唯一出口**有没有把匹配器串进去」这一段接线。方法本身取的是
    `vlt.engine.Engine` 上的**真实现**，不是替身 —— 替身会把接线断掉也测不出来。
    """
    from vlt.engine import Engine

    class _Holder:
        _emit_translated = Engine._emit_translated

        def __init__(self, vm, lv) -> None:           # noqa: ANN001
            self._virtualmic = vm
            self._level = lv

    return _Holder(sink, level)


def test_emit_translated_without_level_match_is_byte_identical() -> None:
    """没注入 LevelMatcher（旧行为）/ `mode=off` → push 的内容**逐字节不变**。

    这是 P0 验收：默认档必须与升级前**完全一致**，一个样点都不许动。
    """
    import inspect

    from vlt.engine import Engine
    from vlt.output.level_match import LevelConfig, LevelMatcher

    params = list(inspect.signature(Engine.__init__).parameters)
    assert "level_match" in params, f"Engine.__init__ 必须有 level_match 形参：{params}"
    assert params[-1] == "level_match", \
        f"level_match 必须放在形参末尾（既有调用点才不用改）：{params}"

    pcm = _fake_translated_pcm()
    sink = _FakeSink()
    _emit_holder(sink, None)._emit_translated(pcm)
    assert sink.pushed[-1] == pcm, "没注入匹配器时必须逐字节原样 push（= 升级前的行为）"

    off = LevelMatcher(LevelConfig(mode="off"))
    _emit_holder(sink, off)._emit_translated(pcm)
    assert sink.pushed[-1] == pcm, f"mode=off 必须逐字节原样 push（默认档）：{off.gain_db()}"
    assert off.gain_db() == 0.0, f"off 档增益恒 0：{off.gain_db()}"

    # 没勾「译音输出」（_virtualmic 为 None）→ 一块都不许 push，也不许抛
    sink2 = _FakeSink()
    _emit_holder(None, None)._emit_translated(pcm)
    assert sink2.pushed == [], "没有译音输出时不该有任何 push"
    print("  ✓ 译音出口：没注入匹配器 / mode=off → 逐字节不变；没有译音输出 → 不 push")


def test_emit_translated_with_level_match_processes_bytes() -> None:
    """注入了 LevelMatcher（`mode=fixed`, −6 dB）→ 长度不变、内容因增益而不同、幅度约减半。"""
    from vlt.output.level_match import LevelConfig, LevelMatcher

    pcm = _fake_translated_pcm(amp=4000)
    sink = _FakeSink()
    m = LevelMatcher(LevelConfig(mode="fixed", fixed_gain_db=-6.0, ceiling_dbfs=-1.0))
    _emit_holder(sink, m)._emit_translated(pcm)

    out = sink.pushed[-1]
    assert len(out) == len(pcm), \
        f"输出字节数必须与输入**完全一致**（下游抖动缓冲靠它算时长）：{len(out)} vs {len(pcm)}"
    assert out != pcm, "施加了 −6 dB 就不该还是原来那串字节"
    amp_in = max(abs(x) for x in struct.unpack(f"<{len(pcm) // 2}h", pcm))
    amp_out = max(abs(x) for x in struct.unpack(f"<{len(out) // 2}h", out))
    ratio = amp_out / amp_in
    assert 0.48 <= ratio <= 0.53, f"−6 dB 应把幅度压到约一半：{amp_in} → {amp_out}（{ratio:.3f}）"

    # status() 是界面读数的唯一来源：三个字段都得在（缺一个读数行就会显示半真半假的数）
    st = m.status()
    for key in ("mode", "mic_db", "tts_db", "gain_db", "headroom_db", "fallback"):
        assert key in st, f"status() 少了 {key}：{st!r}"
    assert st["mode"] == "fixed" and st["gain_db"] == -6.0, st
    print(f"  ✓ 注入匹配器：字节被处理（{amp_in} → {amp_out}，{ratio:.3f}）且长度恒等；status() 字段齐")


def test_mic_pump_feeds_level_reference() -> None:
    """代理的直通线程真的把每块麦克风数据喂给了电平参考（打桩 `MicReference.feed` 计数）。

    钉的是「**复用同一块数据**」：电平参考绝不新开第二个采集设备 —— 参考电平来自
    `_mic_pump` 已经在读的那块 chunk，采样率/声道数也必须是采集源的真实值
    （喂错率会让 100ms 分块算错，中位数就变成一个没有物理意义的数）。
    """
    from vlt.output import level_match

    calls: list[tuple[int, int, int]] = []
    orig = level_match.MicReference.feed

    def _counting(self, pcm, rate, channels, *, now):   # noqa: ANN001
        calls.append((len(pcm), rate, channels))
        return orig(self, pcm, rate, channels, now=now)

    level_match.MicReference.feed = _counting            # type: ignore[assignment]
    try:
        with _wired_gui() as (gui, _cfg_path):
            with _stubbed_audio(gui=gui) as src:
                with contextlib.redirect_stdout(io.StringIO()):
                    assert gui._start_proxy() is True, "打桩环境下代理应能起来"
                    _settle(0.3)                         # 让直通线程读完假源的那一块
                assert calls, ("直通线程必须把麦克风块喂给电平参考"
                               "（MicReference.feed 一次都没被调到 = 接线断了）")
                size, rate, ch = calls[0]
                assert (rate, ch) == (src.rate, src.channels), \
                    f"喂进去的采样率/声道数必须是采集源的真实值：{(rate, ch)} vs {(src.rate, src.channels)}"
                assert size > 0 and size % 2 == 0, f"喂进去的必须是 int16 PCM：{size} 字节"
                ref = gui._proxy.mic_reference_db
                assert ref is None or isinstance(ref, float), \
                    f"mic_reference_db 只能是 float 或 None（样本不足时不许猜）：{ref!r}"
                with contextlib.redirect_stdout(io.StringIO()):
                    gui._close_proxy()                   # 撤补丁前收尾：别去碰真实采集后端
    finally:
        level_match.MicReference.feed = orig             # type: ignore[assignment]
    print(f"  ✓ 代理直通线程喂了电平参考：{len(calls)} 次（{rate}Hz {ch}ch，复用同一块数据，未新开设备）")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_proxy_unavailable_falls_back_with_trace,
        test_proxy_available_injects_translated_sink,
        test_translation_start_stop_notifies_proxy,
        test_settings_toggle_off_restarts_proxy_and_unwires_engine,
        test_buffer_change_reaches_running_proxy,
        test_mic_device_change_reopens_passthrough,
        test_emit_translated_without_level_match_is_byte_identical,
        test_emit_translated_with_level_match_processes_bytes,
        test_mic_pump_feeds_level_reference,
    ]
    print("test_proxy_wiring:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
