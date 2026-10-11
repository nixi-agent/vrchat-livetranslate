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


# ---------------------------------------------------------------- 替身


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


# ---------------------------------------------------------------- 离线引擎


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


# ---------------------------------------------------------------- ① 接线生效 + 不双写 + owner


def test_rebind_to_injected_swaps_leg_and_closes_self_built() -> None:
    """自建 → 注入：旧 VirtualMic 被 close、输出腿换到 sink、后续 PCM 只到新 sink（不双写）。

    ★ 这条同时是**故障注入 #1** 的探测点：把 `rebind_audio_sink` 里「关掉自建 VirtualMic」
    那一步去掉，`old_vm.closed` 就仍是 False → 本用例变红。
    """
    created: list[_FakeVM] = []
    eng = _engine(level_mode="off")                     # off → apply 逐字节原样，便于断言
    _arm_self_built(eng, created)
    old_vm = eng._virtualmic                            # noqa: SLF001
    assert old_vm is created[-1] and eng._owns_virtualmic, "前提：应先建立自建腿"  # noqa: SLF001

    sink = _FakeProxySink()
    ok, out = _capture(eng.rebind_audio_sink, sink)
    assert ok is True, f"切到注入 sink 必须成功：{out!r}"

    # owner 正确：自建被 close、_owns_virtualmic 落 False、_virtualmic 指向注入 sink
    assert old_vm.closed is True, f"★ 故障注入#1 探测点：自建 VirtualMic 必须被 close：{out!r}"
    assert eng._owns_virtualmic is False, "切到注入 sink 后不该再自称 owner"  # noqa: SLF001
    assert eng._virtualmic is sink, "输出腿必须换到注入的 sink"               # noqa: SLF001

    # 接线生效 + 不双写：后续 emit 只到新 sink，旧腿一块都收不到
    block = _pcm()
    eng._emit_translated(block)                         # noqa: SLF001
    assert sink.pushed == [block], f"新 sink 应逐字节收到这一块：{sink.pushed!r}"
    assert old_vm.pushed == [], f"旧腿重接线后不该再收到任何 PCM（双写）：{old_vm.pushed!r}"

    # 逐分支留痕：成功一行，含新 owner
    assert "已重接线" in out and "注入 sink" in out, f"成功重接线必须留痕：{out!r}"
    print("  ✓ 自建→注入：旧腿 close、输出腿换到 sink、PCM 只到新 sink（不双写）、成功留痕")


# ---------------------------------------------------------------- ② follow_mic 跟随（核心）


def test_follow_mic_reads_new_reference_after_rebind() -> None:
    """★ issue #90 的核心诉求：注入带 `mic_reference_db` 的 sink 后，`LevelMatcher.mic_db()`
    立刻读到新值（重接线前读不到）。

    ★ 这条同时是**故障注入 #2** 的探测点：把 `_build_level_matcher` 的 `_mic_db` 退回
    「捕获构造时 sink 局部变量」的冻结闭包，重接线后 `mic_db()` 仍读老对象（None）→ 变红。
    """
    created: list[_FakeVM] = []
    eng = _engine(level_mode="follow_mic")
    _arm_self_built(eng, created)

    # 重接线前：没有代理 → 参考读不到（follow_mic 如实回落）
    assert eng._level.mic_db() is None, "前提：自建腿时 mic_db() 应读不到参考"  # noqa: SLF001
    assert eng._level_has_mic_ref is False, "前提：自建腿时无参考接线"          # noqa: SLF001

    sink = _FakeProxySink(mic_db=-18.5)
    ok, _out = _capture(eng.rebind_audio_sink, sink)
    assert ok is True, "切到注入 sink 必须成功"

    # 重接线后：活引用立刻读到新 sink 的参考电平（这正是冻结闭包做不到的）
    assert eng._level.mic_db() == -18.5, \
        f"★ 故障注入#2 探测点：mic_db() 应读到新 sink 的参考：{eng._level.mic_db()!r}"  # noqa: SLF001
    assert eng._level.status()["mic_db"] == -18.5, "status() 读数的 mic_db 也应是新参考"  # noqa: SLF001
    assert eng._level_has_mic_ref is True, "参考接线开关应跟着翻成 True"        # noqa: SLF001
    assert eng._level_fallback_logged is False, "换接线后应允许再留一次痕（清标志）"  # noqa: SLF001
    print("  ✓ follow_mic：重接线后 mic_db() 立刻读到新参考 -18.5（重接线前 None）")


# ---------------------------------------------------------------- ③ 换回自建 + 不变量


def test_rebind_back_to_self_built_never_closes_injected() -> None:
    """注入 → 自建：重新自建 VirtualMic、`_owns_virtualmic` 回 True、**绝不** close 注入的 sink。"""
    created: list[_FakeVM] = []
    eng = _engine(level_mode="follow_mic")
    _arm_self_built(eng, created)
    sink = _FakeProxySink(mic_db=-18.5)
    _capture(eng.rebind_audio_sink, sink)
    n_after_inject = len(created)

    ok, out = _capture(eng.rebind_audio_sink, None)
    assert ok is True, f"换回自建必须成功：{out!r}"

    # 不变量：注入的 sink 归代理管 —— 引擎重接线时绝不能 close 它
    assert sink.close_calls == 0, f"★ 不变量：注入 sink 不该被引擎 close：{sink.close_calls}"
    # 重新自建了一条**新**腿，owner 回 True
    assert len(created) == n_after_inject + 1, f"换回自建应新造一条腿：{created!r}"
    new_vm = created[-1]
    assert eng._virtualmic is new_vm and eng._owns_virtualmic is True, \
        "换回自建后 _virtualmic 应指向新腿且自称 owner"          # noqa: SLF001
    assert new_vm.opened is True and new_vm.closed is False, "新腿应已 open、未 close"
    # 参考接线跟着断开（活引用读 None）
    assert eng._level.mic_db() is None, "换回自建后参考应读不到"   # noqa: SLF001
    assert eng._level_has_mic_ref is False                        # noqa: SLF001
    assert "已重接线" in out and "自建" in out, f"成功留痕应写明换回自建：{out!r}"
    print("  ✓ 注入→自建：重新自建 + owner 回 True + 注入 sink 从未被 close（不变量）")


# ---------------------------------------------------------------- ④ _cleanup 不 close 注入 sink


def test_cleanup_never_closes_injected_sink() -> None:
    """★ 引擎停止（`_cleanup`）时：注入的 sink **绝不能** close；自建的腿**必须** close。"""
    # (a) 注入 sink：cleanup 后 close_calls 仍为 0，且引用被摘掉
    created: list[_FakeVM] = []
    eng = _engine(level_mode="off")
    _arm_self_built(eng, created)
    sink = _FakeProxySink()
    _capture(eng.rebind_audio_sink, sink)
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(eng._cleanup())                     # noqa: SLF001
    assert sink.close_calls == 0, f"★ 不变量：_cleanup 不得 close 注入 sink：{sink.close_calls}"
    assert eng._virtualmic is None and eng._owns_virtualmic is False  # noqa: SLF001

    # (b) 对照：纯自建腿 cleanup 必须 close（否则漏关设备）
    created2: list[_FakeVM] = []
    eng2 = _engine(level_mode="off")
    _arm_self_built(eng2, created2)
    self_vm = eng2._virtualmic                          # noqa: SLF001
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(eng2._cleanup())                    # noqa: SLF001
    assert self_vm.closed is True, "对照：自建腿在 _cleanup 时必须被 close"
    print("  ✓ _cleanup：注入 sink 从不 close（close_calls=0）；自建腿照关（对照）")


# ---------------------------------------------------------------- ⑤ 跳过 / 未运行 两条留痕


def test_rebind_skip_and_not_running_branches() -> None:
    """无变化 → 一行「跳过」；引擎未运行 → 只记下接线、**绝不**凭空 open 设备。"""
    # (a) 无变化：已指向同一 sink → 跳过，不 close/不重造
    created: list[_FakeVM] = []
    eng = _engine(level_mode="off")
    _arm_self_built(eng, created)
    sink = _FakeProxySink()
    _capture(eng.rebind_audio_sink, sink)
    n = len(created)
    ok, out = _capture(eng.rebind_audio_sink, sink)     # 再切同一个 sink
    assert ok is True and ("未变化" in out or "跳过" in out), f"无变化应留「跳过」痕：{out!r}"
    assert len(created) == n and sink.close_calls == 0, "跳过分支不该 close/重造任何腿"

    # (b) 未运行、也没有在用的腿：只记下接线，下次 start 生效（不 open 设备）
    created2: list[_FakeVM] = []
    eng2 = _engine(level_mode="follow_mic")
    # 注意：**不**调 _arm_self_built —— _virtualmic 保持 None，running 为 False
    sink2 = _FakeProxySink(mic_db=-30.0)
    ok2, out2 = _capture(eng2.rebind_audio_sink, sink2)
    assert ok2 is True and ("未运行" in out2 or "记下" in out2), f"未运行应留「记下」痕：{out2!r}"
    assert eng2._audio_sink is sink2, "未运行也应把接线记下来"        # noqa: SLF001
    assert eng2._virtualmic is None, "★ 未运行绝不凭空 open 设备"      # noqa: SLF001
    assert created2 == [], "未运行不该造任何 VirtualMic"
    assert eng2._level_has_mic_ref is True, "记下接线后参考开关应翻 True"  # noqa: SLF001
    print("  ✓ 无变化→「跳过」留痕（不 close/不重造）；未运行→只记下接线、不 open 设备")


# ---------------------------------------------------------------- ⑤b 不需要输出腿的方向（review #2）


def test_rebind_skips_directions_without_audio_output() -> None:
    """★ review #2：本方向**不需要**译音输出腿 → 重接线只记接线，绝不建腿 / open 设备。

    以前 `rebind_audio_sink` 第 3 步无条件调 `_setup_virtualmic`，绕过了 `_build_and_run`
    建腿那个门（`audio_enabled and d.output_audio`）。后果：关代理时 `rebind(None)` 会给
    `theirs` 腿（或没勾「译音输出」的 `mine` 腿）真去 open 引擎自建虚拟声卡并**占住端点**，
    用户再开代理就可能因设备被自己占着而失败 → 代理降级。

    ★ **故障注入**：把 `rebind_audio_sink` 里 `if not self._audio_out_wanted():` 那个分支
    删掉 → (a) 的 `created_a` 会多出一条已 open 的 `_FakeVM`、`_virtualmic` 不再是 None
    → 本用例变红。
    """
    # (a) 方向不含「我说」（theirs）：引擎在跑，手里还攥着代理的 sink → 关代理
    created_a: list[_FakeVM] = []
    sink_a = _FakeProxySink()
    eng_a = _engine(level_mode="off", audio_sink=sink_a,
                    direction="theirs", output_audio=False)
    eng_a._make_audio_out = _recording_stub(created_a)      # noqa: SLF001
    _fake_running(eng_a)
    assert eng_a.running, "前提：引擎在跑"
    assert eng_a._virtualmic is None, "前提：theirs 腿本来就没有输出腿"   # noqa: SLF001

    ok_a, out_a = _capture(eng_a.rebind_audio_sink, None)
    assert ok_a is True, f"跳过也算「接线可用」→ True：{out_a!r}"
    assert eng_a._virtualmic is None, \
        f"★ 不该凭空多出一条输出腿：{eng_a._virtualmic!r}"                # noqa: SLF001
    assert created_a == [], f"★ 一次都不许 open 设备：{created_a!r}"
    assert eng_a._audio_sink is None, "接线本身仍要记下来（下次 start 照它建）"  # noqa: SLF001
    assert sink_a.close_calls == 0, "不变量：注入的 sink 归代理管，绝不 close"
    assert "不需要译音输出" in out_a and "跳过" in out_a, \
        f"必须逐分支留痕说清「本方向不需要译音输出 → 跳过」：{out_a!r}"

    # (b) 没勾「译音输出」的 mine 腿（output.audio.enabled=False）→ 开代理时同样只记接线
    created_b: list[_FakeVM] = []
    eng_b = _engine(level_mode="off", audio_enabled=False)
    eng_b._make_audio_out = _recording_stub(created_b)      # noqa: SLF001
    _fake_running(eng_b)
    sink_b = _FakeProxySink(mic_db=-21.0)
    ok_b, out_b = _capture(eng_b.rebind_audio_sink, sink_b)
    assert ok_b is True, f"跳过 → True：{out_b!r}"
    assert eng_b._virtualmic is None and created_b == [], \
        f"★ 不建腿、不开设备：{eng_b._virtualmic!r} {created_b!r}"        # noqa: SLF001
    assert eng_b._audio_sink is sink_b, "接线仍要记下来"                   # noqa: SLF001
    assert "不需要译音输出" in out_b, f"必须逐分支留痕：{out_b!r}"

    # (c) 对照：同一个门**开着**（mine + output_audio + enabled）→ 照常真的换腿
    created_c: list[_FakeVM] = []
    eng_c = _engine(level_mode="off")
    _arm_self_built(eng_c, created_c)
    sink_c = _FakeProxySink()
    ok_c, out_c = _capture(eng_c.rebind_audio_sink, sink_c)
    assert ok_c is True and eng_c._virtualmic is sink_c, \
        f"对照：门开着就该真的换腿：{out_c!r}"                             # noqa: SLF001
    assert "已重接线" in out_c, f"对照：成功留痕：{out_c!r}"
    print("  ✓ 不需要输出腿的方向（theirs / 没勾译音输出）→ 只记接线、_virtualmic 仍 None、"
          "0 次 open（对照：门开着照常换腿）")


# ---------------------------------------------------------------- ⑥ 失败分支（自建打不开）


def test_rebind_failure_when_self_build_cannot_open() -> None:
    """换回自建但虚拟声卡打不开 → 返回 False + WARN 留痕 + 注入 sink 仍不被 close。"""
    created: list[_FakeVM] = []
    eng = _engine(level_mode="off")
    state = _arm_self_built(eng, created)               # 先正常建立自建腿
    sink = _FakeProxySink()
    _capture(eng.rebind_audio_sink, sink)               # 切到注入

    state["opens"] = False                              # 之后自建一律打不开
    ok, out = _capture(eng.rebind_audio_sink, None)
    assert ok is False, f"自建打不开时 rebind 必须返回 False：{out!r}"
    assert eng._virtualmic is None, "打不开就不该留下半死的腿"        # noqa: SLF001
    assert "⚠️" in out and "不可用" in out, f"失败必须 WARN 留痕：{out!r}"
    assert sink.close_calls == 0, "★ 失败路径也绝不能 close 注入 sink"
    print("  ✓ 失败分支：自建打不开→False + WARN「译音输出不可用」+ 注入 sink 未被 close")


# ---------------------------------------------------------------- ⑦ 并发：切腿 vs 灌 PCM


def test_concurrent_emit_and_rebind_no_loss_no_crash() -> None:
    """一边 `_emit_translated` 灌、一边 `rebind_audio_sink` 切：不崩、不丢块、不双写。

    判据靠 `_sink_lock`：emit 读 `_virtualmic` + push 全程持锁，rebind 的「置 None → close 旧
    → 建新」整段也持锁 ⇒ emit 永远看不到那个瞬态 None、也永远不会 push 到刚被 close 的旧腿。
    于是**每一块都恰好落到某一条腿上**：所有 sink 收到的块数之和 == emit 的次数。
    """
    created: list[_FakeVM] = []
    eng = _engine(level_mode="off")
    _arm_self_built(eng, created)
    sink = _FakeProxySink(mic_db=-20.0)

    n_emit, n_swap = 4000, 400
    block = _pcm()
    errors: list[tuple[str, Exception]] = []

    def _emit_loop() -> None:
        try:
            for _ in range(n_emit):
                eng._emit_translated(block)             # noqa: SLF001
        except Exception as exc:                        # noqa: BLE001
            errors.append(("emit", exc))

    def _rebind_loop() -> None:
        try:
            for i in range(n_swap):
                eng.rebind_audio_sink(sink if i % 2 == 0 else None)  # noqa: SLF001
        except Exception as exc:                        # noqa: BLE001
            errors.append(("rebind", exc))

    with contextlib.redirect_stdout(io.StringIO()):     # 400 次重接线留痕太多，吞掉
        ta = threading.Thread(target=_emit_loop)
        tb = threading.Thread(target=_rebind_loop)
        ta.start(); tb.start()
        ta.join(); tb.join()

    assert not errors, f"并发下出现异常：{errors!r}"
    total = len(sink.pushed) + sum(len(vm.pushed) for vm in created)
    assert total == n_emit, f"每块应恰好落一条腿（不丢/不双写）：实到 {total}，应到 {n_emit}"
    assert sink.close_calls == 0, "★ 并发路径也绝不能 close 注入 sink"
    # 收尾后没有半死的腿挂着
    print(f"  ✓ 并发：{n_emit} 次 emit × {n_swap} 次切腿 → 无异常、总块数 {total}=={n_emit}"
          "（不丢不双写）、注入 sink 未被 close")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_rebind_to_injected_swaps_leg_and_closes_self_built,
        test_follow_mic_reads_new_reference_after_rebind,
        test_rebind_back_to_self_built_never_closes_injected,
        test_cleanup_never_closes_injected_sink,
        test_rebind_skip_and_not_running_branches,
        test_rebind_skips_directions_without_audio_output,
        test_rebind_failure_when_self_build_cannot_open,
        test_concurrent_emit_and_rebind_no_loss_no_crash,
    ]
    print("test_proxy_rebind:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:                        # noqa: BLE001
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
