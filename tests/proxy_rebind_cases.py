"""proxy rebind regression cases; shared offline fixtures."""
from __future__ import annotations

from proxy_rebind_fixtures import (
    _FakeProxySink,
    _FakeVM,
    _arm_self_built,
    _capture,
    _engine,
    _fake_running,
    _pcm,
    _recording_stub,
    asyncio,
    contextlib,
    io,
    threading,
)

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
