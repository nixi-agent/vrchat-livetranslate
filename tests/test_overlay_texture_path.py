"""离线验证手腕屏的「持久 D3D11 贴图 + SetOverlayTexture」上传路径（**不开真实设备/SteamVR**）。

## 为什么要有这个测试

`vlt/output/openvr_overlay.py` 新增了一条上传路：自建 D3D11 设备 + 一张只建一次的持久纹理，
每帧更新它的内容再 `SetOverlayTexture`，用来替掉每帧新建贴图、约 200 次就撞墙的 `setOverlayRaw`
（详见 `vlt/output/win_d3d11.py` 的模块 docstring）。

⚠️ 内容更新走的是 `DYNAMIC + CPU_ACCESS_WRITE` 源纹理 → `Map(WRITE_DISCARD)` → `CopyResource`，
**不是** `UpdateSubresource`（对共享纹理直写会把它弄坏、之后永久 `InvalidTexture`）。
真机出屏与 700 次连刷已验（`scripts/verify_overlay_texture.py`）；本测试只负责**接线正确性**，
全部用假对象注入，绝不碰真实 D3D/SteamVR：

  · 有 D3D 层（假对象注入）→ `_upload` 走 `setOverlayTexture`，且**不**调用 `setOverlayRaw`；
  · D3D 建不起来 → 降级走 `setOverlayRaw`，且有一行降级留痕；
  · `setOverlayTexture` 报「不支持 / InvalidTexture」→ **永久回退**且**只留痕一次**；
  · 重建 / 硬重启后 → `setOverlayTexture` 被重新调用（新 handle 上没有任何贴图）；
  · `size_px` 变化后 → D3D 纹理被**重建**以匹配新尺寸；
  · **D3D11 设备丢失**（TDR / 驱动重置 / 适配器移除，`healthy()` 为假）→ 重建持久纹理，
    而不是一直挂在坏设备上（只重建 SteamVR 一侧是救不回来的）。

注入手法：把 `vlt.output.openvr_overlay.D3D11Texture` 换成假类（`_init_texture_path` /
`_rebuild_d3d` 在调用时才查这个名字），再装一个**带 `setOverlayTexture` 的假 openvr**。
（现有的 `tests/test_overlay_steamvr.py` 用的假 openvr **没有** `setOverlayTexture`，因此那套
用例天然走旧的 raw 路，行为一字不变 —— 见 `_init_texture_path` 的能力判据。）
"""

from wrist_texture_cases import (
    test_texture_path_uses_setoverlaytexture,
    test_d3d_unavailable_falls_back_to_raw,
    test_unsupported_texture_permanent_fallback_logs_once,
    test_recreate_and_reinit_rebind_texture,
    test_size_change_rebuilds_texture,
    test_d3d_device_loss_rebuilds_texture,
)

from wrist_texture_retry_cases import (
    test_recreate_overlay_rebuilds_when_device_dead,
    test_tex_path_retries_once_after_transient_failure,
    test_device_loss_rebuild_failure_then_retry_recovers,
    test_base_reuse_copy_list_only_names_the_class_sets,
    test_upload_flushes_after_copy,
)

if __name__ == "__main__":
    print("test_overlay_texture_path:")
    test_upload_flushes_after_copy()
    test_texture_path_uses_setoverlaytexture()
    test_d3d_unavailable_falls_back_to_raw()
    test_unsupported_texture_permanent_fallback_logs_once()
    test_recreate_and_reinit_rebind_texture()
    test_size_change_rebuilds_texture()
    test_d3d_device_loss_rebuilds_texture()
    test_recreate_overlay_rebuilds_when_device_dead()
    test_device_loss_rebuild_failure_then_retry_recovers()
    test_tex_path_retries_once_after_transient_failure()
    test_base_reuse_copy_list_only_names_the_class_sets()
    print("ALL PASSED")
