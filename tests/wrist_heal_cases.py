"""overlay recovery regression cases; shared offline fixtures."""
from __future__ import annotations

from overlay_recovery_fixtures import (
    CALLS,
    INIT_FAIL,
    _cfg,
    _count,
    _fresh,
    _names,
    contextlib,
    io,
    time,
)

def test_unavailable_selfheals_and_repushes() -> None:
    """`available=False` 之后：退避窗口内不重试；到点必须自己救回来并重贴内容。

    故障注入：把 `tick()` 里的 `_maybe_reinit()` 删掉 → 本条红（面板永远停在最后一帧）。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.update_entries([("theirs", "Hello", "你好")], force=True)
    assert ov.available is True

    # —— 制造「硬重启失败」现场（真实成因：SteamVR 换代 / 合成器重启后接口指针全废）——
    INIT_FAIL["on"] = True
    ov.available = False
    oo.REINIT_BACKOFF_S = 30.0                      # 退避窗口 30s
    ov._schedule_reinit("硬重启失败")
    attempts = INIT_FAIL["count"]
    ov.tick()
    ov.tick()
    assert INIT_FAIL["count"] == attempts, (
        f"退避窗口内不该反复重试（会变成重建风暴）：{INIT_FAIL['count'] - attempts} 次")

    # —— 到点重试一次：仍然失败 → 保持不可用 + 重排下一次（不风暴、也不静默死掉）——
    oo.REINIT_BACKOFF_S = 0.0
    ov._reinit_at = time.monotonic() - 1.0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    assert INIT_FAIL["count"] - attempts == 1, "到点应正好重试一次"
    assert ov.available is False, "init 仍失败时不该假装可用"
    assert ov._reinit_at is not None, "失败后必须重排下一次（否则又变成永远不试）"
    assert "面板不可用 → 重试接管 SteamVR" in buf.getvalue(), f"缺重试留痕：{buf.getvalue()}"

    # —— SteamVR 回来了 → 同一条路必须把面板救回来，并把内容重贴回去 ——
    INIT_FAIL["on"] = False
    ov._reinit_at = time.monotonic() - 1.0
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    assert ov.available is True, f"init 恢复后 tick 没能救回面板：{ov.available}"
    assert ov._reinit_fails == 0, "救回来后退避计数要清零"
    assert "createOverlay" in _names(), f"没有重新接管 overlay：{_names()}"
    assert "setOverlayTexture" in _names() or "setOverlayRaw" in _names(), (
        f"恢复后没有把面板内容重贴回去：{_names()}")
    ov.close()
    print("  available=False → 退避重试自愈并重贴内容 OK")

def test_reinit_backoff_is_bounded() -> None:
    """退避必须**翻倍且封顶**（5s→10s→20s→40s→60s→60s）—— 真死 GPU 时不能风暴。"""
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    delays = []
    for _ in range(6):
        t0 = time.monotonic()
        ov._schedule_reinit("测试")
        delays.append(round(ov._reinit_at - t0))
    assert delays == [5, 10, 20, 40, 60, 60], f"退避节奏不对（应翻倍封顶 60s）：{delays}"
    ov.close()
    print(f"  不可用重试退避有界：{delays} OK")

def test_heartbeat_logs_even_when_unavailable() -> None:
    """心跳必须在不可用时**继续**打（其余路径全早退，日志一安静就再也查不出死在哪一刻）。"""
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.available = False
    ov.HEARTBEAT_S = 0.0                     # 立刻到点
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()
    assert "心跳" in out, f"不可用时心跳没了（等于事后无据可查）：{out}"
    assert "面板=不可用" in out, f"心跳里要能看出面板状态：{out}"
    assert "上传路=" in out, f"心跳里要能看出走的是哪条上传路：{out}"
    ov.close()
    print("  面板不可用时心跳仍留痕（含面板状态/上传路）OK")

def test_close_blocks_reinit_resurrection() -> None:
    """取消勾选（close）后，**已排定的**「重试接管」不许把面板复活（复审第 1 条）。

    场景：`available=False` → `_schedule_reinit()` 已排定 → 用户取消勾选 `close()`。
    旧守卫只看 `available`（close() 恰好把它置 False ⇒ 满足「可以往下走」）与
    `_reinit_at`（非 None）⇒ 下一次 tick 会 `_hard_reinit()` → `openvr.init()`，
    面板**自己回来**（正是本 PR 要消灭的那条症状，只不过换了一条路径）。
    判据：tick 之后不得出现 `init`/`shutdown`，且已排定的时间点被清掉。

    故障注入：把 `_maybe_reinit` 守卫里的 `self._closed` 删掉 → 本条红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.available = False                       # 模拟「面板不可用」
    ov._schedule_reinit("测试：已排定重试")
    assert ov._reinit_at is not None, "前置条件：应已排定一次重试"
    ov.close()                                 # 用户取消勾选

    # 护栏一（物理上不可能）：close() 把已排定的时间点直接作废。
    #   故障注入 B：删掉 `close()` 里的 `self._reinit_at = None` → 这一条红。
    assert ov._reinit_at is None, "close() 应把已排定的重试清掉（护栏要物理上不可能）"

    # 护栏二（守卫）：即使 `_reinit_at` 又被弄成「已到点」（旧状态残留 / 将来有人挪走清理），
    #   `_closed` 也必须拦住 —— ⚠️ 这一条**只有** `_closed` 能守：`available` 被 close()
    #   置成 False 恰好满足「可以重试」的条件。
    #   故障注入 A：删掉 `_maybe_reinit` 守卫里的 `self._closed` → 这一条红。
    ov._reinit_at = time.monotonic() - 1.0
    CALLS.clear()
    INIT_FAIL["count"] = 0
    ov._last_heartbeat = 0.0
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()

    assert INIT_FAIL["count"] == 0, f"close() 之后 tick 又调了 openvr.init()（面板复活）：\n{out}"
    assert "shutdown" not in _names(), f"close() 之后 tick 又走了接管流程：\n{out}"
    assert ov.available is False, "close() 之后不该变成 available"
    print("  close() 之后「已排定的重试」不再复活面板 OK（两条护栏各验一次）")

def test_heartbeat_skips_closed_instance() -> None:
    """已 close() 的实例不再打心跳（少刷日志、少探活）—— 复审第 2 条。

    与 `test_heartbeat_logs_even_when_unavailable` 成对：**不可用**要留痕，**已关闭**要安静。
    判据用「探测次数 + 留痕」两条：`findOverlay`/`isOverlayVisible` 一次都不该被调。

    故障注入：把心跳条件里的 `not self._closed` 删掉 → 本条红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov._last_heartbeat = 0.0                   # 下一跳必到点
    ov.close()

    probes = {"find": 0, "vis": 0}
    ovl = ov._overlay

    def _find(key):  # noqa: ANN001, ANN202
        probes["find"] += 1
        return 7

    def _vis(handle):  # noqa: ANN001, ANN202
        probes["vis"] += 1
        return True

    ovl.findOverlay, ovl.isOverlayVisible = _find, _vis
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()

    assert "心跳" not in out, f"close() 之后还在打心跳：\n{out}"
    assert probes["find"] == 0 and probes["vis"] == 0, \
        f"close() 之后还在探活：findOverlay={probes['find']} isOverlayVisible={probes['vis']}"
    print("  close() 之后不再打心跳 / 不再探活 OK")

def test_raw_path_rebuilds_before_the_200_wall() -> None:
    """兼容模式（旧路）必须**主动**重建，而不是等每 ~200 次上传挂掉之后再自愈。

    故障注入：删掉 `_upload_raw` 里的主动重建 → `createOverlay` 只会有最初那一次 → 本条红。
    """
    oo = _fresh(tex_api=False)                 # 老 SteamVR：没有 setOverlayTexture
    oo.RAW_REBUILD_EVERY_FRAMES = 3            # 测试里缩短（生产 150）
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(3):                     # 第 3 帧到点 → 主动重建
            ov.update_entries([("theirs", f"hi{i}", f"你好{i}")], force=True)
    out = buf.getvalue()
    assert _count("createOverlay") == 1, f"应正好主动重建一次：{_names()}"
    assert _count("destroyOverlay") == 1, f"重建前应先销毁旧 overlay：{_names()}"
    assert "主动重建" in out, f"缺主动重建留痕：{out}"
    assert _count("setOverlayRaw") >= 4, (
        f"重建后必须把面板内容重贴回去（3 帧 + 重贴 1 帧）：{_names()}")
    ov.close()
    print("  兼容模式按帧数主动重建（不等 ~200 次挂掉）OK")
