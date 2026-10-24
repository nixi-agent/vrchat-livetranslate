"""overlay texture path regression cases; shared offline fixtures."""
from __future__ import annotations

from overlay_texture_path_fixtures import (
    CALLS,
    FailOnceD3D,
    FailSecondD3D,
    FakeD3D,
    _boom_upload,
    _cfg,
    _fresh,
    _names,
)

def test_recreate_overlay_rebuilds_when_device_dead() -> None:
    """设备已死时重建 overlay → 必须**重建纹理**，而不是把一张死纹理绑到新 handle 上。

    实测踩过（复审第 5 条）：`_recreate_overlay()` 原来无条件重绑 → 死纹理绑到新 handle，
    只能等下一轮**破坏性**的硬重启（还会 shutdown+init openvr）才有机会修回来。
    """
    oo = _fresh(FakeD3D)
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    dead = FakeD3D.instances[0]
    dead.healthy = lambda: False              # type: ignore[method-assign]
    n_before = len(FakeD3D.instances)

    assert ov._recreate_overlay() is True
    assert len(FakeD3D.instances) > n_before, "设备已死却没重建纹理（等于把死纹理绑到新 handle）"
    assert dead.closed is True, "旧（已死）纹理应被 close"
    assert ov._d3d is not dead and ov._d3d is not None
    ov.close()
    print("  设备已死时重建 overlay → 一并重建纹理 OK")

def test_tex_path_retries_once_after_transient_failure() -> None:
    """「D3D 路建不起来」后应再给**一次**机会，否则本会话永久退回旧路。

    实测场景（复审第 1 条）：设备丢失后那次重建恰好在 TDR 复位窗口内失败 → `_d3d` 永久停在
    None，而 `_init_texture_path()` 只在 `start()` 调过一次 → 静默跑回 `setOverlayRaw`
    （正是本 PR 要消灭的那条老路）。判据：**建出来了**、且**只再试一次**（别对真死 GPU 反复建设备）。
    """
    FailOnceD3D.attempts = 0
    oo = _fresh(FailOnceD3D)
    oo.TEX_RETRY_AFTER_S = 0.0              # 测试里不等 2s
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    assert ov._d3d is None, "第一次构造失败 → 此刻应无 D3D 纹理（暂时走旧路）"

    ov.tick()                               # ← 定点重试在这里兑现（不依赖上传失败）
    assert ov._d3d is not None, "给了重试机会却没建出 D3D 纹理（会一直停在旧路）"
    assert FailOnceD3D.attempts == 2, f"只该再试一次，实际试了 {FailOnceD3D.attempts} 次"

    CALLS.clear()
    ov.update_entries([("mine", "你好", "Hello")], force=True)
    assert "setOverlayTexture" in _names() and "setOverlayRaw" not in _names(), (
        f"恢复后应稳定走 D3D 路：{_names()}")
    ov.tick()
    ov.tick()
    assert FailOnceD3D.attempts == 2, f"定点重试只该一次，实际 {FailOnceD3D.attempts} 次"
    ov.close()
    print("  start 期构造失败 → tick 定点重试一次并切回 D3D 路 OK")

def test_device_loss_rebuild_failure_then_retry_recovers() -> None:
    """复审第 1 条的**真场景**：设备已建好 → 设备丢失 → 那次重建瞬时失败 → 恢复。

    上一版把重试挂在 `_escalate` 且要求 `stage == "none"`：设备丢失分支先跑并把阶段推成
    "rebuild" → 判据恒不成立；raw 路随后成功又没人再调 `_escalate` → 会话静默跑回旧路。
    本用例按这条路径构造，断言 `tick()` 的定点重试救回来、且只试一次。
    """
    FailSecondD3D.attempts = 0
    oo = _fresh(FailSecondD3D)
    oo.TEX_RETRY_AFTER_S = 0.0
    ov = oo.WristOverlay(_cfg())
    assert ov.start() is True
    dead = FakeD3D.instances[0]
    dead.upload = _boom_upload                  # type: ignore[method-assign]
    dead.healthy = lambda: False                # type: ignore[method-assign]

    # ⚠️ 正好 3 帧：第 3 帧触发自愈阶梯（设备丢失分支 → 重建失败）。**别多跑**——第 4 帧的 raw 成功
    #    会把阶段重置回 "none"，那样「重建失败时阶段必须保持 none」这条判据就失去分辨力了（踩过）。
    for _ in range(3):
        ov.update_entries([("theirs", "Hello", "你好")], force=True)
    assert FailSecondD3D.attempts == 2, f"没走到「设备丢失后重建」：{FailSecondD3D.attempts} 次"
    assert ov._d3d is None, "那次重建应失败（模拟 TDR 复位窗口）"
    assert ov._recover_stage == "none", (
        f"重建失败时阶段必须保持 none（否则下一级直接是破坏性的硬重启）：{ov._recover_stage}")

    CALLS.clear()
    ov.tick()                                   # ← 定点重试
    assert ov._d3d is not None, "tick 的定点重试没建出纹理（会话会静默退回旧路）"
    assert FailSecondD3D.attempts == 3, f"应正好再试一次：{FailSecondD3D.attempts} 次"

    ov.update_entries([("mine", "你好", "Hello")], force=True)
    assert "setOverlayTexture" in _names() and "setOverlayRaw" not in _names(), (
        f"恢复后应稳定走 D3D 路：{_names()}")
    assert not [c for c in CALLS if c[0] == "shutdown"], "不该为此去硬重启 openvr"
    ov.tick()
    ov.tick()
    assert FailSecondD3D.attempts == 3, f"定点重试只该一次：{FailSecondD3D.attempts} 次"
    ov.close()
    print("  设备丢失→重建瞬时失败→tick 定点重试救回 OK")

def test_base_reuse_copy_list_only_names_the_class_sets() -> None:
    """`base=` 复用路径的拷贝表里，每个名字都必须是**类里真的会设置**的属性。

    实测踩过：表里留了 `_update`，而本模块刻意不绑 `UpdateSubresource`（共享纹理不能直写）
    → 任何 `D3D11Texture(..., base=...)` 必抛 `AttributeError`，而生产路径不用 base=，
    会被上层 `except` 吞掉静默降级回 raw —— 一用就炸却没人发现（色空间比对就是唯一用户）。
    这条守卫是静态的（AST 读源码，不开设备）：新增/改名时不至于再漏。
    """
    import ast
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import vlt.output.win_d3d11 as _wd          # 只读源码做静态检查，不构造 D3D 设备

    src = pathlib.Path(_wd.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "D3D11Texture")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")

    copied: list[str] = []
    for node in ast.walk(init):
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            copied += [e.value for e in node.iter.elts
                       if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    assert copied, "没解析出 __init__ 里的拷贝表（for attr in (...)）"

    assigned: set[str] = set()
    for node in ast.walk(cls):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:                      # 元组赋值（self.a, self.b = ...）也要算
            for t in ast.walk(target):
                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    assigned.add(t.attr)
    missing = sorted(set(copied) - assigned)
    assert not missing, (
        f"拷贝表里的这些名字类里根本不会设置（base= 复用路径必炸）：{missing}；"
        f"类里实际会设置的：{sorted(assigned)}")
    # 顺带守住 base= 分支的结构：先定归属（失败路径才知道该不该放 device/ctx）→ 建纹理 → 标记非自建。
    base_branch = [n for n in ast.walk(init) if isinstance(n, ast.If)]
    src_text = ast.unparse(init)
    assert "_owns_device = False" in src_text and "_make_shared_pair(fmt)" in src_text, (
        "base= 复用分支少了「定归属 / 建共享纹理」其中之一")
    assert src_text.index("_owns_device = False") < src_text.index("_make_shared_pair(fmt)"), (
        "base= 分支必须先定 _owns_device 再建纹理（否则构造失败时释放归属无从判断）")
    assert "_release_owned" in ast.unparse(cls), "构造失败路径应复用 _release_owned() 释放半成品"
    assert base_branch, "基类分支解析异常"
    print(f"  base= 拷贝表与实例属性一致（{len(set(copied))} 项）+ 分支结构 OK")

def test_upload_flushes_after_copy() -> None:
    """`upload()` 必须在 `CopyResource` **之后** Flush，且 `flush=False` 真的能关掉它。

    为什么这条离线用例非有不可：共享贴图的命令是排进 GPU 队列的，不 Flush 时跨进程的
    vrcompositor 会一直读到**上一帧**（用户报的「手腕屏比桌面慢几秒」）。
    真机判据在 `scripts/verify/verify_d3d11_shared_readback.py`（独立设备/独立进程回读：
    不 Flush 12/12 帧读到旧帧；Flush 后 12/12 全新）。这条用例守的是**代码形状**：
    顺序错（先 Flush 后 Copy）、漏绑 `_flush`、或 `flush=False` 开关失效，都必须立刻变红。

    手法：`object.__new__` 绕开 `__init__`（不建真实 D3D 设备），只把 upload 依赖的
    几个绑定换成记录用的假函数。
    """
    import ctypes as _ct

    from vlt.output.win_d3d11 import D3D11_MAPPED_SUBRESOURCE, D3D11Texture, S_OK

    calls: list[str] = []
    mapped_buf = _ct.create_string_buffer(64)   # 保持引用：cast 出来的指针不能悬空
    tex = D3D11Texture.__new__(D3D11Texture)          # 绕开 __init__：不碰真实设备
    tex.width, tex.height, tex.row_pitch = 4, 2, 16

    def fake_map(_ctx, _res, _idx, _flag, _n, out):  # noqa: ANN001, ANN202
        calls.append("map")
        out._obj.pData = _ct.cast(mapped_buf, _ct.c_void_p).value
        out._obj.RowPitch = 16
        return S_OK

    def fake_unmap(_ctx, _res, _idx):  # noqa: ANN001
        calls.append("unmap")

    def fake_copy(_ctx, _dst, _src):  # noqa: ANN001
        calls.append("copy")

    def fake_flush(_ctx):  # noqa: ANN001
        calls.append("flush")

    tex._map, tex._unmap, tex._copy, tex._flush = fake_map, fake_unmap, fake_copy, fake_flush
    tex.ctx, tex._src, tex.texture = 0x1, 0x2, 0x3

    tex.upload(bytes(tex.row_pitch * tex.height))
    assert calls == ["map", "unmap", "copy", "flush"], (
        f"upload() 的调用顺序必须是 map→unmap→copy→flush（Flush 必须在 CopyResource 之后）：{calls}")

    calls.clear()
    tex.upload(bytes(tex.row_pitch * tex.height), flush=False)
    assert calls == ["map", "unmap", "copy"], (
        f"flush=False 必须真的不 Flush（验证脚本靠它做修复前/后的 A/B）：{calls}")

    # 常量来源：mingw-w64 `d3d11.h` 的 ID3D11DeviceContextVtbl 与 wine 的 `d3d11.idl`
    # 两处解析都给出 111（见 win_d3d11.py docstring）。写错一个索引 = 调到别的 COM 方法。
    import vlt.output.win_d3d11 as _wd
    assert _wd.CTX_FLUSH == 111, f"ID3D11DeviceContext::Flush 的虚表索引应为 111：{_wd.CTX_FLUSH}"
    assert hasattr(tex, "_flush"), "upload 依赖的 _flush 绑定必须存在"
    _ = D3D11_MAPPED_SUBRESOURCE
    print("  upload() 在 CopyResource 之后 Flush（且 flush=False 可关）OK")
