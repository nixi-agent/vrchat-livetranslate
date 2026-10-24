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

from proxy_rebind_cases import (
    test_rebind_to_injected_swaps_leg_and_closes_self_built,
    test_follow_mic_reads_new_reference_after_rebind,
    test_rebind_back_to_self_built_never_closes_injected,
    test_cleanup_never_closes_injected_sink,
    test_rebind_skip_and_not_running_branches,
    test_rebind_skips_directions_without_audio_output,
    test_rebind_failure_when_self_build_cannot_open,
    test_concurrent_emit_and_rebind_no_loss_no_crash,
)

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
