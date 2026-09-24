"""手腕屏改版验收：一块屏 + 对话视图（镜像聊天区）+ 由界面持有。

改动背景（用户实测 + 明确要求）：
1. 双引擎各自创建同名 overlay → 撞 `OverlayError_KeyInUse`（一条腿的屏直接挂掉）；
2. 用户要求「手腕上应该只有一块屏，就像 GUI 上那个对话框那样的东西」——
   所以手腕屏改为**界面持有**，内容 = 最近几句对话（别人在左、我在右），
   两个方向的文字都进这一块屏。
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_overlay_steamvr as fakeov  # noqa: E402  复用假 openvr 与调用记录


# ---------------------------------------------------------------- 渲染（纯函数）
def test_render_conversation_single_panel() -> None:
    from vlt.output.overlay import OverlayConfig, render_conversation

    cfg = OverlayConfig()
    entries = [("theirs", "Hello, how are you?", "你好，你好吗？"),
               ("mine", "我很好，谢谢你。", "I'm fine, thank you.")]
    img = render_conversation(entries, cfg)
    assert img.size == cfg.size_px, f"尺寸应保持 {cfg.size_px}（不改物理宽高比），实际 {img.size}"

    px = img.load()
    w, h = img.size
    # 两条是**上下堆叠**的（最新在下），所以扫描整列而不是取中间一点
    left_col = [px[24 + 2, y] for y in range(h)]
    right_col = [px[w - 24 - 2, y] for y in range(h)]
    print(f"  左缘出现灰条像素 {sum(1 for p in left_col if p[:3] == cfg.color_theirs)} 个，"
          f"右缘出现蓝条像素 {sum(1 for p in right_col if p[:3] == cfg.color_mine)} 个")
    assert any(p[:3] == cfg.color_mine for p in right_col), \
        "我说的那条应在右缘画蓝条，实际整列都没有"
    assert any(p[:3] == cfg.color_theirs for p in left_col), \
        "别人说的那条应在左缘画灰条，实际整列都没有"
    print("  一块屏 + 左右分侧 OK")


def test_render_conversation_keeps_newest() -> None:
    """条目多到塞不下时，必须保留**最新**的（更早的丢弃），且不能画出血。"""
    from vlt.output.overlay import OverlayConfig, render_conversation

    cfg = OverlayConfig()
    entries = [(f"theirs", f"第{i}句原文", f"第{i}句译文，稍微长一点点用来占位置") for i in range(8)]
    img = render_conversation(entries, cfg)
    assert img.size == cfg.size_px
    px = img.load()
    # 最新那条（第7句）必须出现：它的译文是白字，底部区域应有近白像素
    w, h = img.size
    bottom = [px[x, h - 40] for x in range(30, w - 30, 3)]
    bright = [p for p in bottom if p[0] > 200 and p[1] > 200 and p[2] > 200]
    assert bright, "底部没找到最新那条的文字（被更早的条目挤掉了）"
    print(f"  塞不下时保留最新 OK（底部近白像素 {len(bright)} 个）")


# ---------------------------------------------------------------- GUI 持有（假 SteamVR）
def test_gui_owns_single_wrist_panel() -> None:
    fakeov.CALLS.clear()
    fakeov._install_fake_openvr()
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    gui._root.geometry("920x620")
    gui._direction_var.set("dual")
    gui._chatbox_var.set(True)
    gui._overlay_var.set(True)

    got: dict = {}

    def step_start() -> None:
        gui._start()

    def step_check() -> None:
        got["has_overlay"] = gui._overlay_out is not None
        got["available"] = bool(gui._overlay_out and gui._overlay_out.available)
        got["engine_sinks"] = [sorted(e._sinks) for e in gui._engines]
        # 模拟两条文本事件（引擎回调就是推这两个方向的文字）
        gui._q.put(("text", "theirs", "Hello there", "你好", True))
        gui._q.put(("text", "mine", "早上好", "Good morning", True))
        gui._poll()
        got["frames"] = gui._overlay_out.frames_updated if gui._overlay_out else -1
        got["entries"] = len(gui._bubbles)
        gui._stop()
        got["closed"] = "destroyOverlay" in [c[0] for c in fakeov.CALLS]
        got["after_stop"] = gui._overlay_out is None
        gui._root.after(200, gui._on_close)

    gui._root.after(0, step_start)
    gui._root.after(2500, step_check)
    gui._root.mainloop()

    print(f"  引擎输出面={got['engine_sinks']} 手腕屏帧数={got['frames']} "
          f"关闭={got.get('closed')}")
    assert got["has_overlay"] and got["available"], "界面没有成功接管手腕屏"
    # ★ 关键：引擎不再持有手腕屏（否则两条腿各建一块 → KeyInUse）
    for sinks in got["engine_sinks"]:
        assert "overlay" not in sinks, f"引擎仍在持有手腕屏：{sinks}"
        assert "chatbox" in sinks, f"chatbox 不该被顺手去掉：{sinks}"
    assert got["entries"] == 2, f"聊天区应收到 2 条，实际 {got['entries']}"
    assert got["frames"] > 0, "两个方向的文字没有推到手腕屏（贴图未上传）"
    assert got.get("closed"), "停止时没有销毁 overlay"
    assert got.get("after_stop"), "停止后没有清掉引用"
    print("  界面持有唯一手腕屏 + 两个方向都上屏 + 停止时销毁 OK")


if __name__ == "__main__":
    print("test_overlay_conversation:")
    test_render_conversation_single_panel()
    test_render_conversation_keeps_newest()
    test_gui_owns_single_wrist_panel()
    print("ALL PASSED")
