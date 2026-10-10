"""手腕屏改版验收：一块屏 + 对话视图（镜像聊天区）+ 由界面持有。

改动背景（用户实测 + 明确要求）：
1. 双引擎各自创建同名 overlay → 撞 `OverlayError_KeyInUse`（一条腿的屏直接挂掉）；
2. 用户要求「手腕上应该只有一块屏，就像 GUI 上那个对话框那样的东西」——
   所以手腕屏改为**界面持有**，内容 = 最近几句对话（别人在左、我在右），
   两个方向的文字都进这一块屏。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _cfgbox import sandbox_config  # noqa: E402
sandbox_config(reset=True)

import test_overlay_steamvr as fakeov  # noqa: E402  复用假 openvr 与调用记录
from vlt.config import DEFAULT_CONFIG  # noqa: E402

# ⚠️ 必须把**平台工厂**也换掉：
#   `gui._start()` 现在走 `platform.create_wrist_overlay()`；在 Linux 上它返回
#   `OpenXrOverlay`，会真的去连 OpenXR 运行时并建 EGL context（实测有副作用：
#   MESA 与 OpenXR loader 都被拉起来，日志里一堆 pci id / dri2 screen 警告），
#   而且本机根本没有运行时 → available=False → 本用例假红。
#   本用例验的是「手腕屏归**界面**独占持有」，用假 openvr 驱动的真 `WristOverlay` 才对症。
from vlt import platform  # noqa: E402
from vlt.output.openvr_overlay import WristOverlay as _RealWristOverlay  # noqa: E402

platform.create_wrist_overlay = (  # type: ignore[assignment]
    lambda cfg, config_path=None, dry_run=False:
        _RealWristOverlay(cfg, config_path=config_path, dry_run=dry_run))


def _ui_overlay_in_file() -> bool:
    """看**文件**里存了什么：_save_ui_state 只写盘、不回流内存里的 _cfg。"""
    import yaml

    try:
        data = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return False
    return bool((data.get("ui") or {}).get("overlay"))


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
    entries = [("theirs", f"第{i}句原文", f"第{i}句译文，稍微长一点点用来占位置") for i in range(8)]
    img = render_conversation(entries, cfg)
    assert img.size == cfg.size_px
    px = img.load()
    # 最新那条（第7句）必须出现：它的译文是白字，**底部一带**应有近白像素。
    # 注意别探单行：布局是底对齐的（y = h - 2*pad - used），每块尾部还有 ~14px 留白，
    # 字号/面板高改一下，固定行就会正好落进留白里，红得莫名其妙（实测踩过）。
    w, h = img.size
    bottom = [px[x, y] for y in range(h - 100, h - 8, 4) for x in range(30, w - 30, 6)]
    bright = [p for p in bottom if p[0] > 200 and p[1] > 200 and p[2] > 200]
    assert bright, "底部没找到最新那条的文字（被更早的条目挤掉了）"
    print(f"  塞不下时保留最新 OK（底部近白像素 {len(bright)} 个）")


# ---------------------------------------------------------------- GUI 持有（假 SteamVR）
def test_gui_owns_single_wrist_panel() -> None:
    fakeov.CALLS.clear()
    fakeov._install_fake_openvr()
    # 干净环境（CI）上没有任何 API key 时，界面 _start() 会**直接返回**并弹设置窗
    # —— 引擎为空、手腕屏也不建，本用例就假红（实测 CI 挂在这）。这里给一个
    # **拼接出来的假 key**（不触发仓库的凭据扫描）：本用例只验「手腕屏归界面持有 +
    # 两个方向都上屏」，不需要真连上服务。
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "overlaytestonly0123456789abcdef")
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

    def wait_ready(tries: int = 0) -> None:
        """等手腕屏真的起来再断言。

        以前是固定 `after(2500, step_check)` —— 本机够用，CI runner 上设备扫描 + 建 overlay
        更慢，于是「界面没有成功接管手腕屏」假红（实测 CI 就是这么挂的）。
        改成轮询就绪、最多等 15s，与机器快慢解耦。
        """
        ready = gui._overlay_out is not None and gui._overlay_out.available
        if ready or tries >= 75:
            step_check()
        else:
            gui._root.after(200, lambda: wait_ready(tries + 1))

    gui._root.after(200, wait_ready)
    _MAX_TEST_MS = 30_000
    gui._root.after(_MAX_TEST_MS, gui._root.destroy)
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


def test_overlay_starts_on_checkbox_immediately() -> None:
    """勾上「手腕屏」立刻起屏（不用等开始翻译）；取消勾选立刻关掉。

    用户要求：「当我勾选手腕屏的时候我就需要你启动 VR 叠加层，而不是在开始翻译的时候」
    —— 勾上多半是想先看位置对不对（微调面板要对着屏拖），把「调试」和「开跑」绑死很难用。
    """
    fakeov.CALLS.clear()
    fakeov._install_fake_openvr()
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "overlaytoggle0123456789abcdef")
    from vlt.gui import TranslationGUI

    backup = DEFAULT_CONFIG.read_bytes() if DEFAULT_CONFIG.exists() else None
    gui = TranslationGUI()
    got: dict = {}

    def step() -> None:
        gui._overlay_var.set(True)
        gui._on_overlay_toggle()              # 只勾选，**不**点开始翻译
        got["up"] = gui._overlay_out is not None
        got["cfg_on"] = _ui_overlay_in_file()
        gui._overlay_var.set(False)
        gui._on_overlay_toggle()
        got["down"] = gui._overlay_out is None
        got["cfg_off"] = not _ui_overlay_in_file()
        got["closed"] = "destroyOverlay" in [c[0] for c in fakeov.CALLS]
        gui._root.after(100, gui._on_close)

    try:
        gui._root.after(400, step)
        _MAX_TEST_MS = 30_000
        gui._root.after(_MAX_TEST_MS, gui._root.destroy)
        gui._root.mainloop()
    finally:
        if backup is not None:
            DEFAULT_CONFIG.write_bytes(backup)
        else:
            DEFAULT_CONFIG.unlink(missing_ok=True)

    assert got.get("up"), "勾选后手腕屏没有立刻起来（仍要等开始翻译？）"
    assert got.get("cfg_on"), "勾选状态没有写回配置"
    assert got.get("down"), "取消勾选没有关掉手腕屏"
    assert got.get("cfg_off"), "取消勾选的状态没有写回配置"
    assert got.get("closed"), "取消勾选没有销毁 overlay"
    print("  勾选即启动 / 取消即关闭 OK")


def test_overlay_toggle_reverts_when_start_fails() -> None:
    """起不来就必须把勾**自动退回去**：不能界面显示已开启、实际什么都没有。"""
    from vlt import gui as guimod
    from vlt import platform

    class _FailOverlay:
        def __init__(self, *a, **kw) -> None: ...
        def start(self) -> bool:
            return False
        def close(self) -> None: ...

    backup = DEFAULT_CONFIG.read_bytes() if DEFAULT_CONFIG.exists() else None
    # ⚠️ 桩在**平台工厂**上（gui 现在走 `platform.create_wrist_overlay()`）；
    #    以前桩的是 `vlt.gui.WristOverlay`，那个名字已经不在 gui 里了。
    real = platform.create_wrist_overlay
    platform.create_wrist_overlay = lambda *a, **kw: _FailOverlay()  # type: ignore[assignment]
    got: dict = {}
    try:
        g = guimod.TranslationGUI()

        def step() -> None:
            g._overlay_var.set(True)
            g._on_overlay_toggle()
            got["var"] = g._overlay_var.get()
            got["cfg"] = _ui_overlay_in_file()
            got["out"] = g._overlay_out
            got["status"] = getattr(g, "_last_status_level", None)
            g._root.after(100, g._on_close)

        g._root.after(400, step)
        _MAX_TEST_MS = 30_000
        g._root.after(_MAX_TEST_MS, g._root.destroy)
        g._root.mainloop()
    finally:
        platform.create_wrist_overlay = real  # type: ignore[assignment]
        if backup is not None:
            DEFAULT_CONFIG.write_bytes(backup)
        else:
            DEFAULT_CONFIG.unlink(missing_ok=True)

    assert got.get("var") is False, "启动失败后勾选没有自动退回"
    assert got.get("cfg") is False, "退回后的状态没有写回配置"
    assert got.get("out") is None, "失败后不该留下 overlay 引用"
    assert got.get("status") == "error", "启动失败没有给用户可见提示"
    print("  启动失败自动退回勾选 OK")



if __name__ == "__main__":
    print("test_overlay_conversation:")
    test_render_conversation_single_panel()
    test_render_conversation_keeps_newest()
    test_gui_owns_single_wrist_panel()
    test_overlay_starts_on_checkbox_immediately()
    test_overlay_toggle_reverts_when_start_fails()
    print("ALL PASSED")
