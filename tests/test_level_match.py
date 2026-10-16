"""译音音量匹配（vlt/output/level_match.py）纯逻辑测试：**全程离线，绝不碰音频设备**。

本模块只做数字信号上的算术（RMS / 峰值 / 增益 / 限幅 / 斜坡），所以每条用例都用
`struct` 合成的 PCM 直接喂进去，不需要虚拟声卡、不需要 sounddevice、不需要 pytest。
和 `tests/test_micproxy.py` 一样是**单文件脚本**：全绿打印 `ALL PASSED` 并退出 0。

覆盖 .qoder-brief.md §3 的五组，外加评审后补的四组回归：
  1. 块电平 `block_db`（已知幅度正弦 / 全零静音；engine.chunk_level_db 委托到这里）
  2. `apply_gain_db`（0 dB 逐字节相等 / −6 dB / 正增益的天花板限幅）
  3. `MicReference`（中位数、样本门槛、有效期、抗尖峰、静音不得出结论）
  4. 增益计算（四档麦克风参考电平、峰值限幅把 +4 压到 +1.2、限速收敛）
  5. 参考取不到（保住上一次增益 / 从头到尾没拿到 → 回落固定增益）
  6. off→on：进 off 清斜坡状态、切回来**立刻**重算到新目标（不被限速）；fixed 清过期 fallback
  7. 单一实现：「增益+限幅→字节」/ 块电平 / dB 读数格式各只有一份（两条路逐字节相同）
  8. 配置层一致性：`vlt/config.py` 的默认值/区间 == `level_match` 的 `DEFAULT_*`/`RANGES`
     （靠测试钉，而不是让 config.py 反向 import output 包），以及 `_float_in_range` / `_str_choice`
  9. 跨线程（泵/音频线程 vs Tk 线程）：`MicReference` 无异常不丢块、`LevelMatcher` 互斥成立
 10. 句首斜坡在**短块**（< 20ms/块）下必须接着走，不许每个块边界重铺一遍
"""

from level_signal_cases import (
    test_block_db_known_sine,
    test_apply_gain_db_zero_is_byte_identical,
    test_apply_gain_db_minus_six,
    test_apply_gain_db_positive_hits_ceiling_not_int16_rail,
    test_mic_reference_median_and_threshold,
    test_mic_reference_window_expiry,
    test_mic_reference_rejects_spike,
    test_mic_reference_ignores_silence,
    test_mic_reference_multichannel_and_short_chunks,
    test_mic_reference_set_config_keeps_history,
    test_translated_level_measures_sentence,
    test_off_mode_is_transparent,
    test_fixed_mode_uses_fixed_gain,
    test_follow_mic_tracks_reference_levels,
)

from level_gain_cases import (
    test_follow_mic_offset_and_limits,
    test_apply_preserves_length_and_ramps,
    test_ramp_continues_across_short_chunks,
    test_follow_mic_reference_unavailable,
    test_first_sentence_uses_prev_gain_no_buffering,
    test_set_config_hot_update,
    test_config_from_dict_validation,
    test_off_to_on_clears_ramp_and_recomputes_at_once,
    test_fixed_mode_clears_stale_fallback,
)

from level_config_cases import (
    test_gain_and_ceiling_have_one_implementation,
    test_block_level_has_one_implementation,
    test_format_db_is_the_only_formatter,
    test_config_layer_matches_level_match,
    test_config_float_in_range_and_str_choice,
)

from level_thread_cases import (
    test_mic_reference_is_thread_safe,
    test_level_matcher_is_thread_safe,
    test_module_has_no_device_or_engine_dependency,
    test_load_level_hermetic_against_saved_key,
)

if __name__ == "__main__":
    print("test_level_match:")
    test_block_db_known_sine()
    test_apply_gain_db_zero_is_byte_identical()
    test_apply_gain_db_minus_six()
    test_apply_gain_db_positive_hits_ceiling_not_int16_rail()
    test_mic_reference_median_and_threshold()
    test_mic_reference_window_expiry()
    test_mic_reference_rejects_spike()
    test_mic_reference_ignores_silence()
    test_mic_reference_multichannel_and_short_chunks()
    test_mic_reference_set_config_keeps_history()
    test_translated_level_measures_sentence()
    test_off_mode_is_transparent()
    test_fixed_mode_uses_fixed_gain()
    test_follow_mic_tracks_reference_levels()
    test_follow_mic_offset_and_limits()
    test_apply_preserves_length_and_ramps()
    test_ramp_continues_across_short_chunks()
    test_follow_mic_reference_unavailable()
    test_first_sentence_uses_prev_gain_no_buffering()
    test_set_config_hot_update()
    test_config_from_dict_validation()
    test_off_to_on_clears_ramp_and_recomputes_at_once()
    test_fixed_mode_clears_stale_fallback()
    test_gain_and_ceiling_have_one_implementation()
    test_block_level_has_one_implementation()
    test_format_db_is_the_only_formatter()
    test_config_layer_matches_level_match()
    test_config_float_in_range_and_str_choice()
    test_load_level_hermetic_against_saved_key()
    test_mic_reference_is_thread_safe()
    test_level_matcher_is_thread_safe()
    test_module_has_no_device_or_engine_dependency()
    print("ALL PASSED")
