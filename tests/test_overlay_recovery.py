"""手腕屏「面板不可用自愈断链」+「兼容模式（旧路）降级留痕 / 主动重建」的离线守卫。

## 为什么要有这个测试（三条实测出来的真问题）

1. **自愈断链**：`openvr_overlay.WristOverlay` 里上传 / 心跳 / 热重载**全都**有
   `if not self.available` 早退，而 `_hard_reinit()` 失败会把 `available` 置 False。
   于是从那一刻起：没有任何上传会发生 → 没有失败 → `_escalate()` 再也不会被调用 →
   `_hard_reinit` 里那句「下轮失败时再试」**永远等不到**。用户看到的就是「手腕屏内容不
   再更新 / 比桌面慢」而日志一片安静。判据：`available=False` 后 tick 必须能自己救回来，
   且退避有界（不能变成重建风暴）。

2. **兼容模式（`setOverlayRaw` 旧路）的僵住**：旧路每约 200 次上传必挂
   （`OverlayError_RequestFailed`，见 `vlt/output/win_d3d11.py` 的 docstring 与用户真机日志
   「重建=20/硬重启=20」）。原来**等它挂了**才靠自愈阶梯救 → 症状（僵住数秒）已经发生。
   现在按帧数主动重建，把「僵住数秒」换成「一次极短的刷新」。

3. **降级必须响亮**：用户报「手腕屏比桌面晚几秒」时，如果只打一行日志、界面什么都不说，
   他只能靠猜。`degraded_note` 由引擎每帧转发到状态栏（`Engine._relay_overlay_note`）。

全部离线：假 openvr + 假 D3D，不碰真实 steamvr/d3d11。
"""

from wrist_heal_cases import (
    test_unavailable_selfheals_and_repushes,
    test_reinit_backoff_is_bounded,
    test_heartbeat_logs_even_when_unavailable,
    test_close_blocks_reinit_resurrection,
    test_heartbeat_skips_closed_instance,
    test_raw_path_rebuilds_before_the_200_wall,
)

from wrist_recovery_cases import (
    test_degraded_note_is_loud_and_clears_on_recovery,
    test_engine_relays_degraded_note_once,
    test_close_blocks_tex_retry_resurrection,
    test_start_resets_session_heal_state,
)

if __name__ == "__main__":
    print("test_overlay_recovery:")
    test_unavailable_selfheals_and_repushes()
    test_reinit_backoff_is_bounded()
    test_heartbeat_logs_even_when_unavailable()
    test_close_blocks_reinit_resurrection()
    test_heartbeat_skips_closed_instance()
    test_raw_path_rebuilds_before_the_200_wall()
    test_degraded_note_is_loud_and_clears_on_recovery()
    test_engine_relays_degraded_note_once()
    test_close_blocks_tex_retry_resurrection()
    test_start_resets_session_heal_state()
    print("ALL PASSED")
