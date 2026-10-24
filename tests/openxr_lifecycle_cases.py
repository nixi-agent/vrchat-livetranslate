"""Offline OpenXR regression cases; invoked by test_openxr_overlay."""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.openxr_overlay import (  # noqa: E402
    TRACKER_EXT,
    TRACKER_ROLES,
    OpenXrOverlay,
    XrOverlaySession,
)
from vlt.output.overlay import OverlayConfig  # noqa: E402

class _Rec:
    """一条假记录（够 pyopenxr 那种「构造器只装字段」的用法）。"""

    def __init__(self, *args, **kw):
        self.args = args
        self.__dict__.update(kw)


class _FakeXr(types.ModuleType):
    """够用的 pyopenxr 假模块：记调用，并模拟「attach 只能一次」这条规范。

    `bad_path` 里给的子串视为运行时**不认**的路径（`string_to_path` 抛），
    用来验证「某个锚点降级、别的锚点照常」。
    """

    def __init__(self, bad_path: str = ""):
        super().__init__("xr")
        self.calls: list[str] = []
        self.attached = False
        self.bad_path = bad_path
        self.action_names: set[str] = set()
        self.localized_names: set[str] = set()
        # 每个 interaction profile **最终生效**的那一组绑定（见 suggest_... 的替换语义）
        self.suggestions: dict[str, list] = {}
        self.ActionType = types.SimpleNamespace(POSE_INPUT="pose")
        self.SpaceLocationFlags = types.SimpleNamespace(POSITION_TRACKED_BIT=2)
        for name in ("ActionSetCreateInfo", "ActionCreateInfo", "Posef", "Quaternionf",
                     "Vector3f", "InteractionProfileSuggestedBinding", "ActionSuggestedBinding",
                     "SessionActionSetsAttachInfo", "ActionSpaceCreateInfo", "Time"):
            setattr(self, name, _Rec)

    def create_action_set(self, inst, info):
        self.calls.append("create_action_set")
        return _Rec(kind="set")

    def create_action(self, aset, info):
        # 与真 runtime 同款约束：**动作集内** name 与 localizedActionName 都必须唯一
        # （规范 input.adoc：duplicates … must return XR_ERROR_NAME_DUPLICATED /
        #  XR_ERROR_LOCALIZED_NAME_DUPLICATED）。实测踩过：10 个动作共用同一个
        # localized 名字 → 只有第一个建得出来，其余全失败、面板静默回退到 VIEW。
        if info.action_name in self.action_names:
            raise RuntimeError(f"NameDuplicatedError: {info.action_name}")
        if info.localized_action_name in self.localized_names:
            raise RuntimeError("LocalizedNameDuplicatedError: The localized name provided "
                               "was a duplicate of an already-existing resource.")
        self.action_names.add(info.action_name)
        self.localized_names.add(info.localized_action_name)
        self.calls.append(f"create_action:{info.action_name}")
        return _Rec(kind="action", name=info.action_name)

    def create_action_space(self, sess, info):
        self.calls.append("create_action_space")
        return _Rec(kind="space")

    def suggest_interaction_profile_bindings(self, inst, info):
        self.calls.append(f"suggest:{info.interaction_profile}")
        # ★ 规范（input.adoc）：同一个 interaction profile 再次调用会
        #   **丢弃并替换**上一次的全部建议绑定 —— 逐个动作各调一次的话，
        #   最后只剩最后一个动作的绑定（实测：左手就是这样被右手覆盖掉的）。
        self.suggestions[info.interaction_profile] = list(info.suggested_bindings)

    def bound_paths(self) -> dict[str, set[str]]:
        """替换语义算完之后**最终生效**的绑定：动作名 → {绑定路径}。"""
        out: dict[str, set[str]] = {}
        for binds in self.suggestions.values():
            for b in binds:
                out.setdefault(b.action.name, set()).add(b.binding)
        return out

    def attach_session_action_sets(self, sess, info):
        if self.attached:          # ← 规范要求：第二次必须报错（老实现就死在这）
            raise RuntimeError("ActionsetsAlreadyAttachedError: "
                               "The session already has attached action sets.")
        self.attached = True
        self.calls.append("attach")

    def string_to_path(self, inst, s):
        if self.bad_path and self.bad_path in s:
            raise RuntimeError(f"XR_ERROR_PATH_UNSUPPORTED: {s}")
        return s

    @staticmethod
    def wait_frame(sess):
        return _Rec(predicted_display_time=1234)

    @staticmethod
    def locate_space(space, ref, t):
        return _Rec(location_flags=2)          # 一律当作「已追踪」，好验 space 的选择


def _sess_in_fake_xr(fake: _FakeXr, extensions: list[str] | None = None):
    """起一个不碰真 XR 的会话对象（动作相关的方法只用 instance/session 句柄）。

    `extensions` 就是「本会话实际启用的扩展」——tracker 整族要不要建，看的就是它里面
    有没有 `TRACKER_EXT`。
    """
    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    sess = XrOverlaySession(None, (64, 32))    # type: ignore[arg-type]
    sess.instance, sess.session, sess.ref_space = object(), object(), object()
    sess.view_space = object()
    sess.extensions = list(extensions or [])
    return sess, saved


def _restore_xr(saved) -> None:
    if saved is None:
        sys.modules.pop("xr", None)
    else:
        sys.modules["xr"] = saved


def test_anchor_actions_are_built_once_and_switchable():
    """★ 回归：**动作集只能 attach 一次**，切锚点不许再建动作/再 attach。

    真机故障（用户日志 `ActionsetsAlreadyAttachedError`）：老实现每换一次锚点就
    `create_action` + `attach_session_action_sets` 一遍，而 OpenXR 规范明写——

      * `xrAttachSessionActionSets`：called more than once → **must** return
        `XR_ERROR_ACTIONSETS_ALREADY_ATTACHED`；
      * `xrCreateAction` / `xrSuggestInteractionProfileBindings`：动作集 attach 之后就
        **immutable**，再调同样返回该错。

    于是「切左手」在真机上静默失败（异常被热重载的 except 吃掉），面板纹丝不动 ——
    表现为「没法把面板绑到左手上」。修法是**开局一次建全**（左右手 + 8 个 tracker role），
    attach 一次，之后切锚点只换用哪个 space。
    """
    fake = _FakeXr()
    sess, saved = _sess_in_fake_xr(fake, extensions=[TRACKER_EXT])
    try:
        sess.ensure_anchor("right_hand", 0)
        right = sess.anchor_space("right_hand", 0)
        assert right is not sess.view_space, "已追踪的右手锚点应当用动作空间"
        assert sess.anchor_tracked("right_hand", 0) is True
        assert fake.calls.count("attach") == 1, "首次应当 attach"

        # 切锚点：绝不能抛（真机上这里就是 ActionsetsAlreadyAttachedError 的位置）
        sess.ensure_anchor("left_hand", 0)
        sess.ensure_anchor("tracker", 3)
        sess.ensure_anchor("hmd", 0)
        sess.ensure_anchor("right_hand", 0)

        assert fake.calls.count("attach") == 1, f"attach 只能调用一次（规范）：{fake.calls}"
        n_actions = sum(1 for c in fake.calls if c.startswith("create_action:"))
        assert n_actions == 2 + len(TRACKER_ROLES), \
            f"动作应当开局一次建全（左右手 + {len(TRACKER_ROLES)} 个 tracker role）：{n_actions}"
        assert len(fake.action_names) == n_actions, "动作名要唯一"
        assert len(fake.localized_names) == n_actions, \
            "localizedActionName 也必须在动作集内唯一（实测：共用名字会让除第一个外的动作全建不出来）"

        # ★ 绑定必须**一次给全**：逐个动作各调一次的话，同一个 profile 上后一次会替换掉
        #   前一次 —— 左右手只能活一个（实测：修好右手之后左手被覆盖，面板跟着头走）。
        bound = fake.bound_paths()
        assert bound.get("pose_left_hand_0") == {"/user/hand/left/input/grip/pose"}, \
            f"左手的绑定被覆盖掉了（同一 profile 上只能调一次、要一次给全）：{bound}"
        assert bound.get("pose_right_hand_0") == {"/user/hand/right/input/grip/pose"}, \
            f"右手的绑定不对：{bound}"
        assert len(fake.suggestions[XrOverlaySession.TRACKER_PROFILE]) == len(TRACKER_ROLES), \
            "8 个 tracker role 的绑定应当在同一次调用里给全"
        first_attach = fake.calls.index("attach")
        last_action = max(i for i, c in enumerate(fake.calls) if c.startswith("create_action:"))
        assert last_action < first_attach, \
            f"attach 之后动作集就 immutable 了，建动作必须在 attach 之前：{fake.calls}"

        left = sess.anchor_space("left_hand", 0)
        assert left is not right and left is not sess.view_space, "左手要用自己的 space"
        assert sess.anchor_space("right_hand", 0) is right, "切回来要拿回原来那个 space"
        assert sess.anchor_space("hmd", 0) is sess.view_space, "hmd 用 VIEW 空间"
        assert sess.anchor_tracked("hmd", 0) is None, "hmd 不该有锚点动作"
    finally:
        _restore_xr(saved)
    print(f"  锚点动作只建一次（{2 + len(TRACKER_ROLES)} 个）+ attach 一次；"
          f"切锚点/切回都取各自的 space OK")


def test_unsupported_tracker_path_degrades_only_that_anchor():
    """运行时不支持 tracker 时：只有 tracker 锚点降级，左右手照常。

    「动作一次建全」把风险也一起放大了：老实现只建当前锚点，所以运行时认不认
    `/user/vive_tracker_htcx/role/...` 都不影响用左右手；改成开局全建之后，
    一个不认的路径若直接抛出去，整条手腕屏都会起不来。所以每个锚点单独 try。

    两种「不支持」都要兜住：
      a) 运行时压根没启用 `XR_HTCX_vive_tracker_interaction` → 整族**不去建**；
      b) 扩展启用了、路径还是解析不了 → 建第一个就失败，**整族跳过**（只留一行痕）。
    """
    for label, extensions, bad_path in (
            ("没启用 tracker 扩展", [], ""),
            ("扩展启用但路径用不了", [TRACKER_EXT], "vive_tracker_htcx")):
        fake = _FakeXr(bad_path=bad_path)
        sess, saved = _sess_in_fake_xr(fake, extensions=extensions)
        try:
            sess.ensure_anchor("left_hand", 0)          # 不该抛
            assert sess.anchor_space("left_hand", 0) is not sess.view_space, \
                f"{label}：左手必须照常可用"
            assert sess.anchor_space("right_hand", 0) is not sess.view_space, \
                f"{label}：右手必须照常可用"
            assert sess.anchor_space("tracker", 0) is sess.view_space, \
                f"{label}：tracker 应当退回 VIEW 空间"
            assert sess.anchor_tracked("tracker", 0) is None, f"{label}：tracker 不该有动作"
            assert not any(c.startswith("create_action:pose_tracker") for c in fake.calls), \
                f"{label}：应当整族跳过 tracker，别对着 8 个 role 各撞一次：{fake.calls}"
            assert fake.calls.count("attach") == 1, f"{label}：一个锚点不可用不影响 attach"
            assert fake.bound_paths().get("pose_left_hand_0") == {"/user/hand/left/input/grip/pose"}, \
                f"{label}：tracker 降级不该影响左右手的绑定"
        finally:
            _restore_xr(saved)
    print("  ★ tracker 不可用（没扩展 / 路径不认）时：只有该锚点降级到 VIEW，左右手绑定不受影响 OK")


# ---------------------------------------------------------------- 面板尺寸热重载（回归）


class _FakeXrSwap(types.ModuleType):
    """只覆盖 swapchain 那几条调用链的假 xr（`set_size` / 重建的回归用）。"""

    def __init__(self) -> None:
        super().__init__("xr")
        self.created: list[tuple[int, int]] = []
        self.destroyed: list[object] = []
        self.SwapchainCreateInfo = _Rec
        self.SwapchainImageOpenGLKHR = object()

    def enumerate_swapchain_formats(self, sess):  # noqa: ANN001
        return [0x8058]                           # GL_RGBA8

    def create_swapchain(self, sess, info):  # noqa: ANN001
        self.created.append((int(info.width), int(info.height)))
        return object()

    def destroy_swapchain(self, sc):  # noqa: ANN001
        self.destroyed.append(sc)

    def enumerate_swapchain_images(self, sc, cls):  # noqa: ANN001
        return [types.SimpleNamespace(image=7)]


def test_session_set_size_rebuilds_swapchain():
    """★ 回归（用户实测）：面板尺寸热重载必须**重建交换链**。

    430 → 440 一步就触发：新图比交换链大 → 上传越界（GL 0x501）+ image_rect 越界
    （SwapchainRectInvalid）→ 每帧提交失败；而自愈重建用的还是旧尺寸 → 232 次
    「已重建 swapchain」也不恢复。这里钉住 set_size 的交换链行为（同尺寸幂等）。
    """
    fake = _FakeXrSwap()
    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    try:
        sess = XrOverlaySession(None, (1024, 430))      # type: ignore[arg-type]
        sess.session = object()
        sess._create_swapchain()                        # noqa: SLF001
        assert fake.created == [(1024, 430)], fake.created
        first = sess.swapchain

        sess.set_size((1024, 440))
        assert sess.size_px == (1024, 440)
        assert fake.created[-1] == (1024, 440), f"新尺寸没建出新交换链：{fake.created}"
        assert fake.destroyed == [first], "旧交换链没销毁"
        assert sess.textures == [7], "新交换链的纹理没接管"

        n = len(fake.created)
        sess.set_size((1024, 440))                      # 同尺寸：幂等
        assert len(fake.created) == n, "同尺寸不该重建"
    finally:
        _restore_xr(saved)
    print("  set_size：交换链跟随重建 / 旧链销毁 / 同尺寸幂等 OK")


def test_panel_size_reload_resizes_swapchain():
    """★ 回归（用户实测的入口）：热重载改了 `overlay.size_px` → 会话交换链跟随换。

    老实现只把新图重渲染一遍、交换链不动 —— 这正是「面板高拖过当前值就挂死」的入口。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="vlt-xr-resize-"))
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text("overlay:\n  size_px: [1024, 430]\n", encoding="utf-8")

    ov = OpenXrOverlay(OverlayConfig(enabled=True, size_px=(1024, 430)),
                       config_path=cfg_path)

    class _Sess:
        def __init__(self) -> None:
            self.size_px = (1024, 430)
            self.sizes: list[tuple[int, int]] = []

        def set_size(self, s):  # noqa: ANN001
            self.sizes.append(tuple(s))
            self.size_px = tuple(s)

    ov._sess = _Sess()                                  # noqa: SLF001
    cfg_path.write_text("overlay:\n  size_px: [1024, 440]\n", encoding="utf-8")
    ov._reload_config_if_changed()                      # noqa: SLF001
    assert ov._sess.sizes == [(1024, 440)], ov._sess.sizes
    assert tuple(ov.cfg.size_px) == (1024, 440)
    print("  热重载改 size_px：会话交换链跟随重建 OK")


def test_escalation_ladder_climbs_to_session_and_instance():
    """★ 回归（用户实测）：自愈必须**逐级升高**（swapchain → session → instance）。

    老实现每做一级就把 `_stage` 放回 "none" → 永远停在第一级（日志实测：
    232 次重建 swapchain、0 次重建会话）。梯子的回落只在真的恢复上传时
    （帧循环 `_fails = 0` 处）——所以这里只验「动作后 stage 不回落、逐级往上」。
    """
    ov = OpenXrOverlay.__new__(OpenXrOverlay)
    calls: list[tuple] = []

    class _S:
        def rebuild_swapchain(self):  # noqa: ANN201
            calls.append(("swapchain",))

    ov._sess = _S()                     # noqa: SLF001
    ov._stage = "none"                  # noqa: SLF001
    ov._fails_in_stage = 3              # noqa: SLF001
    ov._rebuilds = 0                    # noqa: SLF001
    ov._reinits = 0                     # noqa: SLF001
    ov._teardown = lambda *, keep_gl: calls.append(("teardown", keep_gl))        # noqa: SLF001,E731
    ov._bring_up = lambda *, rebuild_gl: calls.append(("bring_up", rebuild_gl))  # noqa: SLF001,E731

    ov._escalate()                      # noqa: SLF001
    assert calls == [("swapchain",)], calls
    assert ov._stage == "swapchain"     # noqa: SLF001
    ov._fails_in_stage = 3              # noqa: SLF001
    ov._escalate()                      # noqa: SLF001
    assert calls[-2:] == [("teardown", True), ("bring_up", False)], calls
    assert ov._stage == "session"       # noqa: SLF001
    ov._fails_in_stage = 3              # noqa: SLF001
    ov._escalate()                      # noqa: SLF001
    assert calls[-2:] == [("teardown", True), ("bring_up", True)], calls
    assert ov._stage == "instance" and ov._reinits == 1   # noqa: SLF001
    print("  自愈阶梯：swapchain → session → instance 逐级升高 OK")
