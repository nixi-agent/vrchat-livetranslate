"""运行期**重接线**验收：翻译进行中开 / 关麦克风代理，引擎的译音输出腿要整条换掉（issue #90）。

跑法：.venv/Scripts/python.exe tests/test_proxy_rebind.py

## 为什么要有这一层（test_proxy_wiring / test_level_match 不是都测过了吗）

那两份测的是**别的接缝**：`test_proxy_wiring.py` 测「GUI 构造时把 `translated_sink` 递进
`Engine(audio_sink=...)`」这条**静态**接线；`test_level_match.py` 测 `LevelMatcher` 自己的
增益 / 斜坡 / 回落逻辑（纯离线，不碰引擎）。而 issue #90 是**运行期动态换腿**：翻译已经在跑，
用户中途勾开 / 关掉代理，在跑的引擎手里仍是构造时那条 `audio_sink` + 冻结的 `_mic_db` 闭包 ——
`follow_mic` 于是静默降级成固定增益。这条缝只能靠「构造一个离线引擎、真的调
`rebind_audio_sink()`、断言输出腿与参考都换了」来堵。

## 打桩纪律（与 test_engine_stop / test_proxy_wiring 同一条，绝不碰真实音频设备）

  · 引擎**离线构造**（`dry_run=True`、不 `start()`），译音输出腿用 `_setup_virtualmic({})`
    手动建立，`_make_audio_out` 换成返回 `_FakeVM` 的桩 —— 全程不开任何真实设备；
  · 代理的 `translated_sink` 用 `_FakeProxySink`（带 `mic_reference_db`）替身；
  · 收尾用 `asyncio.run(engine._cleanup())`，不连网络、不起事件循环线程。

覆盖任务书的六条：接线生效 / 不双写 / owner 正确（含 `_cleanup` 不 close 注入 sink）/
follow_mic 跟随 / 并发 / 故障注入可被本文件抓红。
外加 review #2 补的一条：**本方向不需要译音输出腿**（`theirs`，或没勾「译音输出」的 `mine`）
→ 重接线只记下接线，`_virtualmic` 仍为 None、一次都不 open 设备（与 `_build_and_run` 同一个门）。
"""

from __future__ import annotations

import asyncio

import contextlib

import io

import sys

import threading

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑

sys.path.insert(0, str(ROOT))

import vlt.engine as E                              # noqa: E402

from vlt.config import AppConfig, Direction         # noqa: E402

class _FakeVM:
    """引擎**自建**译音输出（VirtualMic）的替身：只记录 open/push/close/end_sentence。

    `opens=False` 用来模拟「虚拟声卡打不开」——那是重接线**失败**分支的现场。
    """

    def __init__(self, *, opens: bool = True, name: str = "FakeVM") -> None:
        self.device_name = name
        self._opens = opens
        self.opened = False
        self.closed = False
        self.pushed: list[bytes] = []
        self.sentences = 0

    def open(self) -> bool:
        self.opened = self._opens
        return self._opens

    def push(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    def end_sentence(self) -> None:
        self.sentences += 1

    def close(self) -> None:
        self.closed = True

class _FakeProxySink:
    """代理的 `TranslatedSink` 替身：带 `mic_reference_db`（follow_mic 的参考来源）。

    `close_calls` 钉的是本 issue 的**核心不变量**：注入的 sink 归代理管生命周期，引擎
    重接线 / 收尾时**永远不能** close 它（否则代理那条常驻输出流被引擎从背后捅死）。
    """

    device_name = "FakeCard Output"

    def __init__(self, mic_db: float | None = -24.0) -> None:
        self._mic_db = mic_db
        self.pushed: list[bytes] = []
        self.sentences = 0
        self.close_calls = 0

    @property
    def mic_reference_db(self) -> float | None:
        return self._mic_db

    def push(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    def end_sentence(self) -> None:
        self.sentences += 1

    def close(self) -> None:
        self.close_calls += 1

def _cfg(level_mode: str, *, output_audio: bool = True,
         audio_enabled: bool = True, direction: str = "mine") -> AppConfig:
    """一份够用的离线配置：`output.audio.level.mode` 可调，方向带 `output_audio`。

    *output_audio* / *audio_enabled* / *direction* 用来摆出「本方向压根不需要译音输出腿」
    的现场（review #2）：`theirs` 腿、或没勾「译音输出」的 `mine` 腿。
    """
    return AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={direction: Direction(source_lang="zh", target_lang="en",
                                         output_audio=output_audio)},
        chatbox={}, merger={}, overlay={},
        output={"audio": {"enabled": audio_enabled, "level": {"mode": level_mode}}},
    )

def _engine(level_mode: str = "off", audio_sink=None, *,            # noqa: ANN001
            direction: str = "mine", output_audio: bool = True,
            audio_enabled: bool = True):
    """离线构造一个 Engine（**不 start**）：不连网络、不开设备、不起事件循环线程。

    构造期的 `[net] ⚠️ 出网端点派生失败…`（dummy base_url="x" 解析不出 host）与本用例无关，
    吞掉以免污染验收输出。
    """
    events = E.EngineEvents(on_status=lambda lvl, msg: None)
    cfg = _cfg(level_mode, output_audio=output_audio, audio_enabled=audio_enabled,
               direction=direction)
    with contextlib.redirect_stdout(io.StringIO()):
        return E.Engine(cfg=cfg, direction=direction, source="mic",
                        sinks=set(), events=events, settle_s=30.0, dry_run=True,
                        audio_sink=audio_sink)

def _arm_self_built(engine, created: list, *, opens: bool = True) -> dict:  # noqa: ANN001
    """把 `_make_audio_out` 打成桩并建立**自建**译音输出腿；返回可翻转 open 结果的 state。

    桩每被调一次就 append 一个新的 `_FakeVM` 到 *created* —— 用例据此断言「重接线换回自建
    时确实重新造了一条腿」以及「注入的 sink 不在 created 里、从没被 close」。
    """
    state = {"opens": opens}

    def _stub(_audio_cfg: dict) -> _FakeVM:
        vm = _FakeVM(opens=state["opens"], name=f"VM{len(created)}")
        created.append(vm)
        return vm

    engine._make_audio_out = _stub                      # noqa: SLF001
    engine._setup_virtualmic({})                        # noqa: SLF001
    return state

def _recording_stub(created: list):
    """只**记录**「引擎想自建输出腿」这件事，本身不建立任何腿。

    与 `_arm_self_built` 的桩不同：那个用完就 `_setup_virtualmic()` 真建一条腿，这个只用来
    数「引擎有没有试图造腿 / open 设备」—— review #2 要断言的正是**一次都没有**。
    """
    def _stub(_audio_cfg: dict) -> _FakeVM:
        vm = _FakeVM(name=f"VM{len(created)}")
        created.append(vm)
        return vm

    return _stub

class _AliveThread:
    """`Engine.running` 只看 `_thread.is_alive()` —— 摆个恒活的替身即可模拟「翻译进行中」。"""

    @staticmethod
    def is_alive() -> bool:
        return True

def _fake_running(engine) -> None:                      # noqa: ANN001
    """把离线引擎摆成「在跑」：不真起线程、不起事件循环。"""
    engine._thread = _AliveThread()                     # noqa: SLF001
    engine._stopping = False                            # noqa: SLF001

def _capture(fn, *a, **k):                              # noqa: ANN001, ANN202
    """跑 *fn* 并抓走它的 stdout（重接线的逐分支留痕要拿来断言，别漏到控制台）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rv = fn(*a, **k)
    return rv, buf.getvalue()

def _pcm(frames: int = 480, amp: int = 4000) -> bytes:
    """一段 48k 立体声 int16 假译音：*frames* 帧 → ``frames * 4`` 字节。"""
    import struct

    return struct.pack("<h", amp) * (frames * 2)

__all__ = ['_FakeProxySink', '_FakeVM', '_arm_self_built', '_capture', '_engine', '_fake_running', '_pcm', '_recording_stub', 'asyncio', 'contextlib', 'io', 'threading']
