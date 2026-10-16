"""overlay recovery regression cases; shared offline fixtures."""
from __future__ import annotations

from overlay_recovery_fixtures import (
    CALLS,
    FailOnceD3D,
    FakeD3D,
    Path,
    ROOT,
    TEX_UNSUPPORTED,
    _cfg,
    _fresh,
    _names,
    ast,
    contextlib,
    io,
    time,
    types,
)

def test_degraded_note_is_loud_and_clears_on_recovery() -> None:
    """降级必须**响亮可查**：`degraded_note` 要有面向用户的说明 + 日志说清代价；恢复后清空。"""
    # ① 老 SteamVR（接口没有 setOverlayTexture）→ 兼容模式：说明 + 留痕
    oo = _fresh(tex_api=False)
    ov = oo.WristOverlay(_cfg())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert ov.start() is True
    out = buf.getvalue()
    assert ov.degraded_note and "兼容" in ov.degraded_note, f"降级说明缺失：{ov.degraded_note!r}"
    assert "不支持贴图直传" in out, f"缺降级留痕：{out}"
    ov.close()

    # ② setOverlayTexture 报不支持 → 永久回退：日志要把**已知代价**写出来（用户可感知）
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    TEX_UNSUPPORTED["on"] = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.update_entries([("theirs", "Hello", "你好")], force=True)
    out = buf.getvalue()
    assert ov.degraded_note, "永久回退后必须有面向用户的降级说明"
    assert "已知代价" in out and "200" in out, f"没把旧路的已知代价说清楚：{out}"
    assert ov._tex_disabled is True
    ov.close()

    # ③ 瞬时失败 → tick 定点重试成功 → 说明必须被清掉（否则状态栏会一直挂着警告）
    FailOnceD3D.attempts = 0
    oo = _fresh(FailOnceD3D)
    oo.TEX_RETRY_AFTER_S = 0.0
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    assert ov.degraded_note, "首次构造失败后应给出「暂时降级」说明"
    ov.tick()                                  # 定点重试 → 成功
    assert ov._d3d is not None
    assert ov.degraded_note is None, f"恢复后降级说明必须清空：{ov.degraded_note!r}"
    ov.close()
    print("  降级说明响亮可查、恢复后清空 OK")

def test_engine_relays_degraded_note_once() -> None:
    """引擎必须把降级说明转发到状态栏（**只在变化时**一次），并且在 tick 里调用它。"""
    from vlt.engine import Engine, EngineEvents
    from vlt.output.overlay import DEGRADED_NOTE_RECOVERED, DegradedNoteRelay

    eng = Engine.__new__(Engine)               # 只测这一个方法，不构造整台引擎
    seen: list[tuple[str, str]] = []
    eng._events = EngineEvents(on_status=lambda lvl, msg: seen.append((lvl, msg)))
    eng._note_relay = DegradedNoteRelay()
    eng._overlay = types.SimpleNamespace(degraded_note="手腕屏暂时退回兼容刷新方式（测试）")
    eng._relay_overlay_note()
    eng._relay_overlay_note()                  # 同一说明不重复推
    assert seen == [("warn", "手腕屏暂时退回兼容刷新方式（测试）")], f"应在变化时推一次：{seen}"

    # 恢复：必须再推一条 info 把那条警告**顶掉**（状态栏只显示最新一条，没人再推就等于
    # 「降级说明永远挂在那里」—— 判据 1 的「恢复后说明消失」靠它，见 issue #99）
    eng._overlay.degraded_note = None
    eng._relay_overlay_note()
    assert seen == [("warn", "手腕屏暂时退回兼容刷新方式（测试）"),
                    ("info", DEGRADED_NOTE_RECOVERED)], f"恢复要改写状态栏：{seen}"
    eng._relay_overlay_note()                  # 已经是「正常」了，不再重复推
    assert len(seen) == 2, f"恢复不该反复打扰用户：{seen}"

    # 没有 degraded_note 属性的后端（Linux / 假对象）必须静默跳过
    eng._overlay = object()
    eng._note_relay = DegradedNoteRelay()
    eng._relay_overlay_note()
    assert len(seen) == 2, "可选属性缺失时不该抛异常/不该推空消息"

    # 接线守卫：`_relay_overlay_note()` 必须真的挂在每帧的 `_pump_loop` 里（别只定义不调用）
    src = Path(ROOT / "vlt" / "engine.py").read_text(encoding="utf-8")
    # `_pump_loop` 是 async def（AsyncFunctionDef），别只挑 FunctionDef（踩过：KeyError）
    fns = {n.name: n for n in ast.walk(ast.parse(src))
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    pump = ast.unparse(fns["_pump_loop"])
    assert "_relay_overlay_note" in pump and "tick()" in pump, (
        f"_pump_loop 里没有转发降级说明（等于用户永远看不到）：{pump}")
    print("  降级说明经 on_status 推一次（恢复时改写）+ 接线在 _pump_loop OK")

def test_close_blocks_tex_retry_resurrection() -> None:
    """D3D 定点重试必须和 `_maybe_reinit` / `_maybe_apply_anchor` 同一套纪律：`close()` 过的不复活。

    场景（复审 #99 第 2 条）：D3D 路失败 → `_schedule_tex_retry()` 已排定 → 用户取消勾选
    `close()` → 下一次 `tick()`。旧写法会**真的新建一个 D3D11 设备/纹理**（真机泄漏到进程退出）
    再拿 `handle=None` 调 `setOverlayTexture`。

    两条护栏各验一次（缺一条就有一半路径能复活）：
      * 护栏一：`close()` 把已排定的时间点直接作废 —— 故障注入 B：删掉 `close()` 里的
        `self._tex_retry_at = None` → 第一条断言红；
      * 护栏二：`_maybe_retry_tex_path()` 的 `_closed` 守卫 —— 故障注入 A：把它删掉 →
        下面「时间点还在也必须拦住」那一段红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    # 模拟「D3D 路不可用 → 已排定 2s 后定点重试」
    ov._tex_retried, ov._tex_retry_at, ov.available = False, 0.0, False
    ov.close()

    # 护栏一（物理上不可能）：close() 把已排定的时间点直接作废
    assert ov._tex_retry_at is None, "close() 应把已排定的 D3D 定点重试清掉（护栏要物理上不可能）"

    # 护栏二（`_closed`）：就算时间点还在，已 close() 的实例也不许再建 D3D 设备
    ov._tex_retry_at = 0.0                     # 故障注入 A 靠这一段抓（护栏一被删时时间点正是这个）
    before = len(FakeD3D.instances)
    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov.tick()
    out = buf.getvalue()
    names = _names()
    assert "setOverlayTexture" not in names, f"close() 之后 tick 又提交了贴图：{names}"
    assert len(FakeD3D.instances) == before, (
        f"close() 之后 tick 又建了 D3D 设备（真机上是泄漏）：{len(FakeD3D.instances)}")
    assert "再试一次启用 D3D11 持久贴图路径" not in out, f"close() 之后还在做定点重试：{out}"
    assert "shutdown" not in names, f"close() 之后不该再走接管流程：{names}"
    print("  close() 之后不做 D3D 定点重试（两条护栏各验一次）OK")

def test_start_resets_session_heal_state() -> None:
    """`start()` = 新的一轮会话：上一轮的自愈档位/一次性名额必须归零（复审 #99 第 3 条）。

    旧写法的实测后果（本机复现）：
      * 退避从上一轮的档位接着走 —— 下一次「面板不可用」排定 **40s** 而不是 5s；
      * 「整个会话只给一次」的 D3D 定点重试名额已被上一轮用掉（`_schedule_tex_retry` 成 no-op）；
      * `_upload_fails` 攒着 → 新会话第一次失败就凑够 3 次、立刻升级成重建/硬重启。

    故障注入：把 `start()` 里那段归零删掉 → 本条红。
    """
    oo = _fresh()
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov._reinit_fails = 3                       # 上一轮的退避档位
    ov._tex_retried = True                     # 上一轮已用掉定点重试名额
    ov._anchor_tries = 9
    ov._upload_fails = 2
    ov._fails_in_stage = 2
    ov._recover_stage = "rebuild"
    ov._tex_retry_at = time.monotonic() + 5.0
    ov.close()
    assert ov.start() is True                  # 重新勾选 = 新的一轮
    assert (ov._reinit_fails, ov._tex_retried, ov._anchor_tries,
            ov._upload_fails, ov._fails_in_stage) == (0, False, 0, 0, 0), (
        f"start() 没把会话级自愈状态归零：{ov._reinit_fails}/{ov._tex_retried}/"
        f"{ov._anchor_tries}/{ov._upload_fails}/{ov._fails_in_stage}")
    assert ov._recover_stage == "none", f"阶段没归零：{ov._recover_stage}"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ov._schedule_reinit(oo.OVERLAY_ERR_OTHER)
    out = buf.getvalue()
    delay = ov._reinit_at - time.monotonic()
    assert abs(delay - oo.REINIT_BACKOFF_S) < 0.6, (
        f"新会话的退避应从 {oo.REINIT_BACKOFF_S:.0f}s 起步，实测 {delay:.0f}s：{out}")
    assert "（第 1 次尝试）" in out, f"新会话应从第 1 次算起：{out}"

    ov._tex_retry_at = None
    ov._schedule_tex_retry()
    assert ov._tex_retry_at is not None, "D3D 定点重试名额应随新会话重新可用（旧写法是 no-op）"
    ov.close()
    print("  start() 归零会话级自愈状态 OK（退避 5s 起步 + 定点名额可用）")
