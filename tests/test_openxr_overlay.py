#!/usr/bin/env python
"""OpenXR 手腕屏后端的**纯逻辑**测试（不需要头显、不需要运行时）。

## 为什么这几个纯函数值得单独测

`euler_to_quaternion` 的**旋转顺序**如果和 Windows 侧不一致，
配置里那组实测调好的 `rot: [-47, -16, 0]` 在 Linux 上会**静默转到别的方向** ——
面板会以错误的角度贴在手腕上，而且没有任何报错。这种错只能靠测试钉住。

这里的验证方式刻意**与实现不同路**：
  * 实现：四元数乘法 `qz ⊗ qy ⊗ qx`
  * 测试：3×3 转置矩阵连乘 `Rz · Ry · Rx`，再用它去转向量
两条路算出同一个结果才算对（不是拿实现自己的公式自证）。

`vlt/platform/linux.py` 顶层只 import 标准库，`vlt/output/overlay.py` 也只是**惰性**
import openvr，所以这个测试在两个平台上都能跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


from openxr_model_cases import (
    test_quaternion_matches_matrix_convention,
    test_quaternion_is_normalized,
    test_anchor_paths_mapping,
    test_swapchain_format_prefers_srgb,
    test_color_space_needs_linearize_and_lut,
    test_linearize_rgb_touches_only_rgb,
    test_prepared_linearizes_only_for_linear_swapchain_format,
    test_layer_geometry_matches_windows_curvature,
    test_cylinder_pose_offset_rotates_with_panel,
    test_effective_curvature_degrades_without_extension,
)
from openxr_contract_cases import (
    test_extensions_does_not_gamble_on_cylinder_when_unknown,
    test_submit_records_and_uses_enabled_extensions,
    test_should_rebuild_detects_changes,
    test_layer_alpha_flags_value,
    test_apply_overlay_alpha_scales_only_alpha,
    test_panels_have_transparent_margin_and_translucent_plate,
    test_backend_config_field,
    test_composition_layers_declare_alpha_flags,
    test_backend_xr_calls_are_wellformed,
    test_extensions_per_backend_required,
    test_glx_binding_struct_fields,
    test_create_gl_context_selection,
)
from openxr_lifecycle_cases import (
    test_anchor_actions_are_built_once_and_switchable,
    test_unsupported_tracker_path_degrades_only_that_anchor,
    test_session_set_size_rebuilds_swapchain,
    test_panel_size_reload_resizes_swapchain,
    test_escalation_ladder_climbs_to_session_and_instance,
)

if __name__ == '__main__':
    print("test_openxr_overlay:")
    test_quaternion_matches_matrix_convention()
    test_quaternion_is_normalized()
    test_anchor_paths_mapping()
    test_swapchain_format_prefers_srgb()
    test_color_space_needs_linearize_and_lut()
    test_linearize_rgb_touches_only_rgb()
    test_prepared_linearizes_only_for_linear_swapchain_format()
    test_layer_geometry_matches_windows_curvature()
    test_cylinder_pose_offset_rotates_with_panel()
    test_effective_curvature_degrades_without_extension()
    test_extensions_does_not_gamble_on_cylinder_when_unknown()
    test_submit_records_and_uses_enabled_extensions()
    test_should_rebuild_detects_changes()
    test_layer_alpha_flags_value()
    test_apply_overlay_alpha_scales_only_alpha()
    test_panels_have_transparent_margin_and_translucent_plate()
    test_backend_config_field()
    test_composition_layers_declare_alpha_flags()
    test_backend_xr_calls_are_wellformed()
    test_extensions_per_backend_required()
    test_glx_binding_struct_fields()
    test_create_gl_context_selection()
    test_anchor_actions_are_built_once_and_switchable()
    test_unsupported_tracker_path_degrades_only_that_anchor()
    test_session_set_size_rebuilds_swapchain()
    test_panel_size_reload_resizes_swapchain()
    test_escalation_ladder_climbs_to_session_and_instance()
    print("ALL PASSED")
