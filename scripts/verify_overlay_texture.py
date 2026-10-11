"""验证 Windows 手腕屏贴图路径（持久 D3D11 贴图 + SetOverlayTexture）。

两段可单独跑：

    python scripts/verify_overlay_texture.py --offline     # 不需要 SteamVR
        ① 共享句柄非 0（SteamVR 只接受这种形态）② 上传后回读逐字节一致
        ③ 连刷 N 次耗时稳定、设备不丢 ④ close 幂等

    python scripts/verify_overlay_texture.py --steamvr     # 需要 SteamVR 在跑
        用**生产代码**（vlt.output.openvr_overlay._make_texture_t）把共享句柄交给真 SteamVR：
        N 次零失败 + 每 M 次抽检 IVROverlay::IsOverlayVisible 全程为真

退出码 0 = 全过；1 = 有失败（可人工复验/接 CI）。

⚠️ 为什么一定要跑 `--steamvr`：`eType=TextureType_DirectX(0)` + 纹理指针在**部分机器上
   调用不报错但屏幕上什么都不画** —— 「API 没抛异常」不能证明用户看得见。只有
   `IsOverlayVisible` + 人眼才能区分「提交成功」与「真的在画」。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WIDTH, HEIGHT, FRAMES = 1024, 440, 700


def _pattern(n: int, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """确定性测试图案（逐像素递增），用于回读比对。"""
    row = bytes(((n + i) & 0xFF) for i in range(width * 4))
    return row * height


def check_offline(frames: int) -> bool:
    from vlt.output.win_d3d11 import D3D11Texture

    tex = D3D11Texture(WIDTH, HEIGHT)
    print(f"  设备={tex.driver} feature_level=0x{tex.feature_level:04X} {WIDTH}x{HEIGHT} RGBA8")
    print(f"  共享句柄=0x{tex.shared_handle:X}（0 就会让 SteamVR 拒收）")
    ok = bool(tex.shared_handle)

    data = _pattern(1)
    tex.upload(data)
    # 故意用私有回读：验证脚本就是要逐字节证明「内容真进了这张贴图」
    same = tex._read_back() == data                     # noqa: SLF001
    print(f"  回读校验：{'[OK] 逐字节一致' if same else '[FAIL] 不一致'}")
    ok = ok and same

    times = []
    for n in range(frames):
        t0 = time.perf_counter()
        tex.upload(_pattern(n))
        times.append((time.perf_counter() - t0) * 1000.0)
    p95 = sorted(times)[int(len(times) * 0.95)]
    print(f"  连刷 {frames} 次：mean={statistics.mean(times):.3f}ms p95={p95:.3f}ms "
          f"max={max(times):.3f}ms（末 10 次 max={max(times[-10:]):.3f}ms）")
    healthy = tex.healthy()
    print(f"  设备健康：healthy()={healthy}")
    ok = ok and healthy and tex._read_back() == _pattern(frames - 1)   # noqa: SLF001

    tex.close()
    tex.close()                                          # 幂等
    print("  close 幂等：[OK]")
    return ok


def check_base_reuse() -> bool:
    """真设备验 `base=` 复用设备这条路（AST 守卫只能防「拷贝表与属性不一致」，防不了运行时）。

    ⚠️ 存在的理由：`base=` 只在色空间比对时用过，生产路径不碰 —— 所以它坏了不会被任何人发现
    （实测踩过：拷贝表里留了 `_update`，一用就 `AttributeError`，还被上层 except 吞成静默降级）。
    """
    from vlt.output.win_d3d11 import D3D11Texture

    a = D3D11Texture(WIDTH, HEIGHT)
    b = D3D11Texture(WIDTH, HEIGHT, base=a)          # 复用 a 的 device/ctx
    same_dev = (b.device == a.device and b.ctx == a.ctx)
    print(f"  复用同一 device/ctx：{'[OK]' if same_dev else '[FAIL]'}")
    hs = (a.shared_handle, b.shared_handle)
    ok_h = bool(hs[0]) and bool(hs[1]) and hs[0] != hs[1]
    print(f"  两张纹理共享句柄各自有效且不同：0x{hs[0]:X} / 0x{hs[1]:X} → {'[OK]' if ok_h else '[FAIL]'}")

    da, db = _pattern(1), _pattern(2)
    a.upload(da)
    b.upload(db)
    ok_read = (a._read_back() == da) and (b._read_back() == db)      # noqa: SLF001
    print(f"  各自上传后回读一致：{'[OK]' if ok_read else '[FAIL]'}")

    b.close()                                        # 关掉子纹理不能动共享设备
    ok_alive = a.healthy() and a._read_back() == da                  # noqa: SLF001
    print(f"  关掉 B 后 A 仍可用：{'[OK]' if ok_alive else '[FAIL]'}")
    a.close()
    return same_dev and ok_h and ok_read and ok_alive


def check_steamvr(frames: int, every: int) -> bool:
    import openvr

    from vlt.output.openvr_overlay import _make_texture_t
    from vlt.output.win_d3d11 import D3D11Texture

    # 这个句柄要持有到 shutdown 前，不能省（背景应用模式）
    openvr.init(openvr.VRApplication_Background)
    ov = openvr.IVROverlay()
    handle = ov.createOverlay("vlt.verify.texture", "vlt verify")
    ov.setOverlayWidthInMeters(handle, 0.35)
    ov.showOverlay(handle)
    tex = D3D11Texture(WIDTH, HEIGHT)
    print(f"  共享句柄=0x{tex.shared_handle:X}，用生产代码的 _make_texture_t 提交")

    fails, first_err, times, invisible = 0, None, [], 0
    try:
        tex.upload(_pattern(0))
        ov.setOverlayTexture(handle, _make_texture_t(tex.shared_handle))
        print("  首次提交：[OK] SteamVR 接受")
    except Exception as exc:                             # noqa: BLE001
        print(f"  首次提交：[FAIL] {type(exc).__name__}: {exc}")
        ov.destroyOverlay(handle)
        tex.close()
        openvr.shutdown()
        return False

    for n in range(1, frames + 1):
        t0 = time.perf_counter()
        try:
            tex.upload(_pattern(n))
            ov.setOverlayTexture(handle, _make_texture_t(tex.shared_handle))
        except Exception as exc:                         # noqa: BLE001
            fails += 1
            if first_err is None:
                first_err = f"第 {n} 次：{type(exc).__name__}: {exc}"
        times.append((time.perf_counter() - t0) * 1000.0)
        if every and n % every == 0 and not ov.isOverlayVisible(handle):
            invisible += 1

    p95 = sorted(times)[int(len(times) * 0.95)]
    print(f"  连刷 {frames} 次：失败 {fails} 次 {first_err or ''}")
    print(f"  单次 mean={statistics.mean(times):.3f}ms p95={p95:.3f}ms")
    print(f"  每 {every} 次抽检 IsOverlayVisible：不可见 {invisible} 次 "
          f"{'[OK] 全程可见' if invisible == 0 else '[FAIL] 有丢失时刻'}")

    ov.destroyOverlay(handle)
    tex.close()
    openvr.shutdown()
    return fails == 0 and invisible == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="验证手腕屏持久 D3D11 贴图 + SetOverlayTexture 路径")
    ap.add_argument("--offline", action="store_true", help="只跑不依赖 SteamVR 的那段")
    ap.add_argument("--steamvr", action="store_true", help="只跑真 SteamVR 那段（需 SteamVR 在跑）")
    ap.add_argument("--base-reuse", action="store_true", help="只跑 base= 复用设备那段（需真 D3D 设备）")
    ap.add_argument("--frames", type=int, default=FRAMES, help=f"连刷次数（默认 {FRAMES}）")
    ap.add_argument("--every", type=int, default=20, help="每多少次抽检一次可见性（默认 20）")
    args = ap.parse_args()
    both = not (args.offline or args.steamvr or args.base_reuse)

    ok = True
    if args.offline or both:
        print("=== 段 A：D3D11 持久贴图（不需要 SteamVR）===")
        ok = check_offline(args.frames) and ok
    if args.base_reuse and not both:        # both/--offline 已含段 A-2，别重复跑一遍
        print("=== 段 C：base= 复用设备（两张纹理共用一个 device/ctx）===")
        ok = check_base_reuse() and ok
    if args.offline or both:
        print("=== 段 A-2：base= 复用设备 ===")
        ok = check_base_reuse() and ok
    if args.steamvr or both:
        print("=== 段 B：真 SteamVR overlay + SetOverlayTexture ===")
        ok = check_steamvr(args.frames, args.every) and ok
    print("\n结论：" + ("[OK] 全过" if ok else "[FAIL] 有失败，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
