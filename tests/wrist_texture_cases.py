"""overlay texture path regression cases; shared offline fixtures."""
from __future__ import annotations

from overlay_texture_path_fixtures import (
    CALLS,
    FakeD3D,
    ROOT,
    TEX_UNSUPPORTED,
    _cfg,
    _count,
    _fresh,
    _names,
    _raising_d3d,
    contextlib,
    io,
)

def test_texture_path_uses_setoverlaytexture() -> None:
    """有 D3D 层 → `_upload` 走 setOverlayTexture，且**不**碰 setOverlayRaw。"""
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    assert ov._d3d is not None, "start() 后应已建好持久 D3D 纹理"
    built = FakeD3D.instances[0]
    assert (built.width, built.height) == (1024, 440), f"纹理尺寸应取 size_px：{built.width}x{built.height}"
    assert _count("setOverlayTexture") >= 1, "start() 里应立刻绑一次贴图"

    CALLS.clear()
    ov.update_entries([("theirs", "Hello", "你好"), ("mine", "早上好", "Good morning")], force=True)
    names = _names()
    assert "setOverlayTexture" in names, f"没有走 SetOverlayTexture：{names}"
    assert "setOverlayRaw" not in names, f"D3D 路不该再调 setOverlayRaw：{names}"
    assert built.uploads >= 1, "没有把帧内容 upload 进持久纹理"
    # 交给 SteamVR 的语义必须是「DXGI 共享句柄 + DXGISharedHandle(5)」：本机真 SteamVR 实测，
    # eType=0 配 .texture / .srv 一律 OverlayError_InvalidTexture（用户机器上更阴——
    # 不报错但屏幕上什么都不画，所以这里必须把句柄也断言住，光看「没抛异常」不算证据）。
    _, _handle, etype, ecs, tex_handle = next(c for c in CALLS if c[0] == "setOverlayTexture")
    assert etype == 5, f"eType 必须是 DXGISharedHandle(5)，现在是 {etype}"
    assert tex_handle == built.shared_handle, (
        f"交给 SteamVR 的必须是 DXGI 共享句柄 0x{built.shared_handle:X}，现在是 0x{(tex_handle or 0):X}")
    assert tex_handle != built.texture, "传了 ID3D11Texture 指针 —— 这是实测被拒的旧配方"
    assert ecs == oo.OVERLAY_COLOR_SPACE, f"色空间应取模块常量：{ecs}"
    ov.close()
    print("  D3D 路走 setOverlayTexture（共享句柄 + eType=5）、不碰 setOverlayRaw OK")

def test_d3d_unavailable_falls_back_to_raw() -> None:
    """D3D 建不起来 → 降级 setOverlayRaw，且有一行降级留痕。"""
    oo = _fresh(_raising_d3d)
    ov = oo.WristOverlay(_cfg())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert ov.start() is True
    out = buf.getvalue()
    assert ov._d3d is None, "建不起来时不该留下 D3D 对象"
    assert "降级" in out and "setOverlayRaw" in out, f"缺降级留痕：{out}"

    CALLS.clear()
    ov.update_entries([("theirs", "Hello", "你好")], force=True)
    names = _names()
    assert "setOverlayRaw" in names, f"降级后应走 setOverlayRaw：{names}"
    assert "setOverlayTexture" not in names, f"降级后不该再碰 setOverlayTexture：{names}"
    ov.close()
    print("  D3D 不可用 → 降级 setOverlayRaw + 一行留痕 OK")

def test_unsupported_texture_permanent_fallback_logs_once() -> None:
    """setOverlayTexture 报「不支持/InvalidTexture」→ 永久回退，且**只留痕一次**。"""
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True               # 建的时候支持（还没打开不支持开关）
    first = FakeD3D.instances[0]
    CALLS.clear()                           # 抹掉 start() 那次成功绑定，只看回退这几帧
    TEX_UNSUPPORTED["on"] = True            # 之后 setOverlayTexture 一律报 InvalidTexture

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(4):
            ov.update_entries([("theirs", f"hi{i}", f"你好{i}")], force=True)
    out = buf.getvalue()

    assert ov._tex_disabled is True, "应置永久回退标志"
    assert ov._d3d is None, "永久回退时应释放 D3D 对象"
    assert first.closed is True, "回退时应 close 掉持久纹理"
    fallback_lines = [ln for ln in out.splitlines() if "永久回退" in ln]
    assert len(fallback_lines) == 1, f"永久回退只应留痕一次，实际 {len(fallback_lines)} 条：\n{out}"
    assert _count("setOverlayRaw") >= 1, f"回退后应改用 setOverlayRaw：{_names()}"
    # 回退后不再尝试 setOverlayTexture（第 1 帧失败那次之后再无）
    assert _count("setOverlayTexture") == 0, f"回退后不该再调 setOverlayTexture：{_names()}"

    TEX_UNSUPPORTED["on"] = False
    ov.close()
    print("  setOverlayTexture 不支持 → 永久回退且只留痕一次 OK")

def test_recreate_and_reinit_rebind_texture() -> None:
    """重建 / 硬重启后 setOverlayTexture 被重新调用（新 handle 上没有任何贴图）。"""
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    ov.update_entries([("theirs", "Hello", "你好")], force=True)   # 有点内容，重建后会回贴

    CALLS.clear()
    assert ov._recreate_overlay() is True
    assert _count("setOverlayTexture") >= 1, f"重建后没有重新绑贴图：{_names()}"

    CALLS.clear()
    assert ov._hard_reinit() is True
    assert ("shutdown",) in CALLS, "硬重启应真的 shutdown openvr"
    assert _count("setOverlayTexture") >= 1, f"硬重启后没有重新绑贴图：{_names()}"
    ov.close()
    print("  重建 / 硬重启后重新 setOverlayTexture OK")

def test_size_change_rebuilds_texture() -> None:
    """size_px 变化后 D3D 纹理被重建以匹配新尺寸（否则 upload 字节数对不上）。"""
    oo = _fresh(FakeD3D)
    d = ROOT / "out" / "overlay_cfg_tex"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "config.yaml"
    f.write_text("overlay:\n  size_px: [1024, 440]\n", encoding="utf-8")

    from vlt.output.overlay import OverlayConfig
    ov = oo.WristOverlay(OverlayConfig(), config_path=f)
    assert ov.start() is True
    assert (ov._d3d.width, ov._d3d.height) == (1024, 440)
    old = ov._d3d
    ov.update_entries([("theirs", "Hello", "你好")], force=True)   # 有内容，热重载后会重渲

    f.write_text("overlay:\n  size_px: [800, 600]\n", encoding="utf-8")
    ov.tick()

    assert ov.cfg.size_px == (800, 600), f"配置没热重载：{ov.cfg.size_px}"
    assert ov._d3d is not None and ov._d3d is not old, "尺寸变化后应重建出**新的** D3D 纹理"
    assert (ov._d3d.width, ov._d3d.height) == (800, 600), f"新纹理尺寸不对：{ov._d3d.width}x{ov._d3d.height}"
    assert old.closed is True, "旧纹理应被 close"
    assert any((t.width, t.height) == (800, 600) for t in FakeD3D.instances), "没有按新尺寸建纹理"
    ov.close()
    print("  size_px 变化 → D3D 纹理重建 OK")

def test_d3d_device_loss_rebuilds_texture() -> None:
    """D3D11 设备丢失（TDR / 驱动重置 / 适配器移除）→ 必须重建**持久纹理**。

    实测过不补这道时的形态：坏设备一直挂着 —— 连失败只让 `_recreate_overlay`/`_hard_reinit`
    反复重建 SteamVR 一侧（还会 shutdown+init openvr），面板永远回不来。
    判据不是「没抛异常」，而是**建出了新的纹理实例**、且后续帧重新走新纹理 + `setOverlayTexture`。
    """
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    dead = FakeD3D.instances[0]

    def _boom(_data: bytes) -> None:
        raise RuntimeError("Map(源纹理) 失败 hr=0x887A0005")     # DEVICE_REMOVED

    dead.upload = _boom                       # type: ignore[method-assign]
    dead.healthy = lambda: False              # type: ignore[method-assign]

    CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):     # 顺便钉住「日志报的是真实那一步」
        for _ in range(5):                    # 连续失败到触发自愈阶梯
            ov.update_entries([("theirs", "Hello", "你好")], force=True)
    out = buf.getvalue()
    assert "D3D11 upload 失败" in out, f"失败日志应报出真实的操作（D3D11 upload）：{out[-400:]}"
    assert "setOverlayTexture 失败" not in out, (
        f"D3D 侧失败被报成了 overlay 接口失败（排障方向会被带偏）：{out[-400:]}")

    assert len(FakeD3D.instances) >= 2, (
        f"D3D11 设备丢失后应重建出**新的**纹理实例（现在只有 {len(FakeD3D.instances)} 个）")
    fresh = FakeD3D.instances[-1]
    assert fresh is not dead and fresh.closed is False, "新纹理不该是坏的那个、也不该被关掉"
    assert fresh.uploads >= 1, "重建后应继续用**新**纹理上传（坏设备不该再被喂数据）"
    assert dead.uploads == 0, "坏设备不该再收到 upload"
    assert "setOverlayTexture" in _names(), f"重建后应重新绑定贴图：{_names()}"
    # 关键判据：设备丢失时**不该**走破坏性的 openvr 硬重启 —— 问题在 D3D 一侧，重建纹理就够了。
    # （踩过：只靠下面那条硬重启顺带重建，也会「碰巧」建出新纹理，用例就守不住第一级修复了。）
    assert "D3D11 设备已丢失" in out, f"没有走「设备丢失 → 重建纹理」这条修复：{out[-400:]}"
    assert not [c for c in CALLS if c[0] == "shutdown"], (
        f"D3D 设备丢失却去 shutdown+init 了整个 openvr 连接（破坏性，且救不回来）：{_names()}")
    ov.close()
    print("  D3D11 设备丢失 → 重建持久纹理 OK")
