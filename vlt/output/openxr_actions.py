"""OpenXR action profiles, anchors and tracked spaces."""
from __future__ import annotations
import time
import logging
from typing import Any
from .openxr_model import (
    TRACKER_EXT,
    TRACKER_ROLES,
    anchor_paths,
)

log = logging.getLogger("vlt.output.openxr_overlay")

class _XrActions:
    HAND_PROFILES = (
        "/interaction_profiles/khr/simple_controller",
        "/interaction_profiles/oculus/touch_controller",
        "/interaction_profiles/valve/index_controller",
        "/interaction_profiles/microsoft/motion_controller",
        "/interaction_profiles/htc/vive_controller",
    )
    # tracker 走 **HTC Vive Tracker 专用 profile**（`/user/vive_tracker_htcx/role/...`
    # 是它的子动作路径）。拿控制器 profile 去绑 tracker 路径会被运行时判成无效绑定。
    TRACKER_PROFILE = "/interaction_profiles/htc/vive_tracker_htcx"

    @staticmethod
    def _anchor_keys() -> list[tuple[str, int]]:
        """开局就把这些锚点的动作全建出来（attach 之后就再也不能建了）。"""
        return ([("left_hand", 0), ("right_hand", 0)]
                + [("tracker", i) for i in range(len(TRACKER_ROLES))])

    def _ensure_actions(self) -> None:
        """建好全部锚点动作 + 建议绑定 + attach —— **整个会话只做一次**。

        规范（OpenXR `input.adoc`）写得没有余地：
          * `xrAttachSessionActionSets`「**must** return XR_ERROR_ACTIONSETS_ALREADY_ATTACHED
            if called more than once for a given session」；
          * `xrCreateAction` / `xrSuggestInteractionProfileBindings`：「If `actionSet` has been
            included in a call to `xrAttachSessionActionSets`, the implementation **must**
            return XR_ERROR_ACTIONSETS_ALREADY_ATTACHED」——「When an action set is attached
            to a session, that action set becomes **immutable**」。

        老代码是「按需建动作」：每换一次锚点就 `create_action` + `attach` 一次，于是从
        第二次起必然抛 `ActionsetsAlreadyAttachedError`，被热重载的 `except` 吃掉 ——
        **面板压根没换过挂点**（用户实测日志：切左手/切 tracker 各报一次这条错）。
        所以这里改成「一次建全、之后只换 space」。

        ⚠️ 建议绑定**必须按 interaction profile 聚合，每个 profile 只调一次**。规范同一节：
        「If the application successfully calls xrSuggestInteractionProfileBindings more than
        once for an interaction profile, the runtime **must discard the previous suggested
        bindings and replace them** with the new suggested bindings」。逐个动作各调一次的话，
        每个 profile 上只剩**最后一个动作**的绑定 —— 实测踩过：修好右手之后，右手那条把
        左手那条覆盖掉，左手锚点就永远 untracked、静默退回 VIEW（面板跟着头）。
        这个 API 收的就是一张绑定**表**，一次给全才是它的用法。
        """
        if self._actions_ready:
            return
        import xr
        if self.action_set is None:
            self.action_set = xr.create_action_set(self.instance, xr.ActionSetCreateInfo(
                action_set_name="vlt_wrist", localized_action_set_name="VLT Wrist Panel",
                priority=0))
        # tracker 整族的可用性：HTC tracker 的 role 路径属于扩展
        # `XR_HTCX_vive_tracker_interaction`，运行时不支持（没接 tracker 的 WiVRn/Monado 就是）
        # 就**整族跳过** —— 否则每开一次程序对着 8 个 role 各报一次错，真正的故障反而被淹。
        tracker_ok = TRACKER_EXT in self.extensions
        if not tracker_ok:
            log.info("[overlay:xr] 本运行时没启用 %s → 跳过 tracker 动作（左右手锚点不受影响；"
                     "要用 tracker 锚点得先在运行时里接上 tracker）", TRACKER_EXT)
        skip_tracker = not tracker_ok
        for anchor, idx in self._anchor_keys():
            # ★ 逐个锚点降级：某个运行时（或某版 Monado）不认 tracker 的 role 路径时，
            #   只该让**那一个锚点**不可用，绝不能把整条手腕屏拖死 —— 现在开局就会建
            #   全部 8 个 tracker role 的动作，一个不认就整条腿没了（老实现只建当前
            #   锚点，所以踩不到，改成「一次建全」之后这层兜底是必须的）。
            if anchor == "tracker" and skip_tracker:
                continue
            try:
                full, top = anchor_paths(anchor, idx)
                if full is None:                 # hmd → 用 VIEW 参考空间，不需要动作
                    continue
                act = xr.create_action(self.action_set, xr.ActionCreateInfo(
                    action_name=f"pose_{anchor}_{idx}", action_type=xr.ActionType.POSE_INPUT,
                    # ⚠️ `localizedActionName` 在**同一个动作集内必须唯一**（规范：
                    #    duplicates of the corresponding field for any existing action in
                    #    the specified action set → **must** XR_ERROR_LOCALIZED_NAME_DUPLICATED）。
                    #    实测踩过：10 个动作共用 "Wrist Anchor" → 只有第一个建得出来，
                    #    右边/左手那个建不出来 → 查不到 space → 面板静默回退到 VIEW（跟着头）。
                    localized_action_name=f"Wrist Anchor {anchor} {idx}",
                    subaction_paths=[xr.string_to_path(self.instance, top)]))
                self._actions[(anchor, idx)] = act
            except Exception as exc:  # noqa: BLE001
                if anchor == "tracker":
                    skip_tracker = True          # 一个 role 建不出来 → 整族跳过（同一扩展管）
                    log.warning("[overlay:xr] ⚠️ tracker 的 role 路径用不了 → tracker 锚点整族跳过"
                                "（左右手不受影响）：%s: %s", type(exc).__name__, exc)
                else:
                    log.warning("[overlay:xr] ⚠️ 锚点 %s#%d 的动作建不出来，该锚点退回 VIEW 空间"
                                "（其它锚点不受影响）：%s: %s", anchor, idx, type(exc).__name__, exc)

        # 按 interaction profile 聚合绑定：**每个 profile 一次性给全**（见上面的规范引用）。
        # 左右手可以放同一次调用：控制器 profile 的 allowlist 对 /user/hand/left 与
        # /user/hand/right 都有效且都定义了 /input/grip/pose。
        by_profile: dict[str, list[tuple[Any, str]]] = {}
        for (anchor, idx), act in self._actions.items():
            full, _ = anchor_paths(anchor, idx)
            profiles = ((self.TRACKER_PROFILE,) if anchor == "tracker" else self.HAND_PROFILES)
            for profile in profiles:
                by_profile.setdefault(profile, []).append((act, full))
        for profile, pairs in by_profile.items():
            try:
                xr.suggest_interaction_profile_bindings(
                    self.instance, xr.InteractionProfileSuggestedBinding(
                        interaction_profile=xr.string_to_path(self.instance, profile),
                        suggested_bindings=[xr.ActionSuggestedBinding(
                            action=act, binding=xr.string_to_path(self.instance, full))
                            for act, full in pairs]))
            except Exception as exc:  # noqa: BLE001 — 运行时不认的 profile 整组跳过
                log.info("[overlay:xr] 交互 profile %s 的绑定建议被运行时拒绝（该 profile 上的"
                         "锚点会退回 VIEW 空间）：%s", profile, exc)

        # ★ 唯一的 attach 机会
        xr.attach_session_action_sets(self.session, xr.SessionActionSetsAttachInfo(
            action_sets=[self.action_set]))
        # 空间可以在 attach 之后再建（它不改动作集）。全部建好，切锚点就只是查表。
        for (anchor, idx), act in self._actions.items():
            try:
                _, top = anchor_paths(anchor, idx)
                self._spaces[(anchor, idx)] = xr.create_action_space(
                    self.session, xr.ActionSpaceCreateInfo(
                        action=act, subaction_path=xr.string_to_path(self.instance, top),
                        pose_in_action_space=xr.Posef(
                            orientation=xr.Quaternionf(0.0, 0.0, 0.0, 1.0),
                            position=xr.Vector3f(0.0, 0.0, 0.0))))
            except Exception as exc:  # noqa: BLE001
                log.warning("[overlay:xr] ⚠️ 锚点 %s#%d 的空间建不出来，退回 VIEW 空间：%s: %s",
                            anchor, idx, type(exc).__name__, exc)
        self._actions_ready = True

    def ensure_anchor(self, anchor: str, tracker_index: int) -> None:
        """切到某个锚点：确保动作都已建好（只建一次），然后记下当前用哪一个 space。

        ⚠️ 这里**不做任何会改动动作集的 XR 调用** —— 换了锚点只是换 `anchor_space()`
        返回哪个 space（以及 `sync_actions` 照旧每帧跑）。重建动作集/重新 attach 在
        OpenXR 里是被规范明确禁止的（见 `_ensure_actions`）。
        """
        self._ensure_actions()
        self._anchor_key = (anchor, int(tracker_index))

    def anchor_space(self, anchor: str, tracker_index: int = 0) -> Any:
        """层要挂到哪个 space。

        ⚠️ action space 在动作同步生效前是**未追踪**的，层会落到 LOCAL 原点 ——
        实测表现就是「面板固定不动、不跟手也不跟头」。所以这里做一次回退：
        锚点没被追踪时改用 `view_space`（至少跟着头），sync 生效后下一帧自然切回。
        """
        if anchor == "hmd":
            return self.view_space
        space = self._spaces.get((anchor, int(tracker_index)))
        if space is not None and self._space_tracked(space):
            return space
        return self.view_space

    def _space_tracked(self, space: Any) -> bool:
        """这个 space 当前是否带位置追踪（locate 一次看标志位）。"""
        import xr
        fr = self._frame_state
        t = int(getattr(fr, "predicted_display_time", 0) or 0) or time.monotonic_ns()
        try:
            loc = xr.locate_space(space, self.ref_space, xr.Time(t))
            return bool(int(loc.location_flags)
                        & int(xr.SpaceLocationFlags.POSITION_TRACKED_BIT))
        except Exception:  # noqa: BLE001
            return False

    def anchor_tracked(self, anchor: str, tracker_index: int = 0) -> bool | None:
        """锚点是否被追踪（诊断用；拿不到返回 None）。"""
        import xr
        if anchor == "hmd":
            return None
        space = self._spaces.get((anchor, int(tracker_index)))
        if space is None:
            return None
        try:
            self._frame_state = self._frame_state or xr.wait_frame(self.session)
            loc = xr.locate_space(space, self.ref_space,
                                  xr.Time(self._frame_state.predicted_display_time
                                          or time.monotonic_ns()))
            return bool(int(loc.location_flags)
                        & int(xr.SpaceLocationFlags.POSITION_TRACKED_BIT))
        except Exception:  # noqa: BLE001
            return None
