"""独立设备回读探针：证明「共享贴图的内容，跨进程消费者什么时候才看得见」。

## 为什么需要它（这条是本 PR 的判据，不是可选的排障脚本）

`vlt/output/win_d3d11.py` 的 `upload()` 走的是
`Map(WRITE_DISCARD) → 逐行 memmove → Unmap → CopyResource(源 → 共享纹理)`。
这些命令是**排进 GPU 队列**的，D3D11 的立即上下文会**攒着**它们 —— 只有
`Flush()`（或 `Present()`）才把命令真正提交给 GPU。手腕屏这条路上**没有 Present**，
所以不 Flush 时那份 `CopyResource` 可能长时间停在 CPU 侧的命令缓冲里：

    SteamVR 合成器（vrcompositor，**另一个进程**）读这张 MISC_SHARED 贴图时，
    读到的还是**上一帧**的内容 —— 用户看到的就是「手腕屏比桌面晚几秒才更新」。

本脚本用一个**独立的 D3D11 设备**（自己的 device/context，与生产者没有命令队列顺序关系）
打开同一份 DXGI 共享句柄并回读，逐帧比较「刚 upload 进去的内容」与「消费者读到的内容」：
一致 = 消费者看得见新帧；读到上一帧 = 延迟缺陷成立。

## 三种取证

  · `--mode same`：同进程、**两个独立设备**。逐帧判定 fresh / stale，并可把两边回读结果
    写成 PNG（`--png`）用眼睛看是「新内容 / 旧内容 / 空面板」。
  · `--mode cross`：**跨进程**（最接近真实：vrcompositor 是另一个进程）。父进程每写一帧
    就打一条带时间戳的 `UPLOAD` 行；子进程（本脚本 `--mode reader`）轮询共享句柄，
    内容一变就打一条 `CHANGE` 行 → 直接量出「上传完成 → 另一个进程看到新内容」的滞后。
    共享句柄用**整数**传给子进程（DXGI 共享句柄是全局的，不需要 DuplicateHandle）。
  · `--png`：把生产者 `_read_back()`（staging 回读）与独立消费者读到的内容各存一份 PNG，
    用 `vision` 直接看图判断。

## 用法（本机 Windows + 真 GPU；不需要 SteamVR）

    .venv/Scripts/python.exe scripts/verify/verify_d3d11_shared_readback.py --mode all
    .venv/Scripts/python.exe scripts/verify/verify_d3d11_shared_readback.py --mode same --no-flush
    .venv/Scripts/python.exe scripts/verify/verify_d3d11_shared_readback.py --mode cross --frames 12

`--no-flush` 把上传路径切回**修复前**的写法（不 Flush），用来做故障注入 / A/B。

⚠️ ctypes-COM 脚本结尾必须 `os._exit()`：否则结论打印完了，解释器拆卸阶段还会
在 Release/GC 里 SIGSEGV（退出码 139，看起来像失败）。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import statistics
import subprocess
import sys
import time
from ctypes import POINTER, byref, c_uint, c_void_p
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vlt.output.win_d3d11 import (  # noqa: E402
    CPU_ACCESS_READ, CTX_COPY_RESOURCE, CTX_MAP, CTX_UNMAP,
    DEV_CREATE_TEXTURE2D, D3D11_MAPPED_SUBRESOURCE, D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC,
    DRIVER_HARDWARE, DRIVER_WARP, DXGI_SAMPLE_DESC, FEATURE_LEVELS, FMT_RGBA8_UNORM,
    MAP_READ, S_OK, USAGE_STAGING, D3D11Texture, _bind, _release,
)

# ID3D11Device::OpenSharedResource = 28 —— 双源解析核对过（mingw-w64 d3d11.h 的
# ID3D11DeviceVtbl 与 wine 的 d3d11.idl，两处都给出同一个序号；见 win_d3d11.py docstring
# 的「常量 / 虚表索引来源」）。手写索引前一律先从头文件解析，别数。
DEV_OPEN_SHARED_RESOURCE = 28
# {6f15aaf2-d208-4e89-9ab4-489535d34f9c} = ID3D11Texture2D（前三段按 GUID 内存布局 little-endian）
IID_ID3D11_TEXTURE2D = bytes((0xF2, 0xAA, 0x15, 0x6F, 0x08, 0xD2, 0x89, 0x4E,
                              0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C))
OUT_DIR = ROOT / "out" / "d3d11_shared_probe"


# ------------------------------------------------------------------ 独立消费者
class IndependentReader:
    """**自己的 D3D11 设备** + `OpenSharedResource(共享句柄)` + staging 回读。

    与生产者没有任何命令队列顺序关系 —— 这正是 vlt 进程与 vrcompositor 之间的关系。
    """

    def __init__(self, shared_handle: int, width: int, height: int, fmt: int = FMT_RGBA8_UNORM) -> None:
        self.width, self.height, self.row_pitch = width, height, width * 4
        d3d11 = ctypes.WinDLL("d3d11")
        fn = d3d11.D3D11CreateDevice
        fn.restype = ctypes.c_long
        fn.argtypes = [c_void_p, ctypes.c_int, c_void_p, c_uint, POINTER(c_uint), c_uint, c_uint,
                       POINTER(c_void_p), POINTER(c_uint), POINTER(c_void_p)]
        levels = (c_uint * len(FEATURE_LEVELS))(*FEATURE_LEVELS)
        last = None
        for driver in (DRIVER_HARDWARE, DRIVER_WARP):
            dev, ctx, got = c_void_p(), c_void_p(), c_uint()
            hr = fn(None, driver, None, 0, levels, len(FEATURE_LEVELS), D3D11_SDK_VERSION,
                    byref(dev), byref(got), byref(ctx))
            if hr == S_OK:
                self.driver, self.device, self.ctx = driver, dev.value, ctx.value
                break
            last = f"hr=0x{hr & 0xFFFFFFFF:08X}"
        else:
            raise RuntimeError(f"消费者 D3D11CreateDevice 失败：{last}")

        self._open_shared = _bind(self.device, DEV_OPEN_SHARED_RESOURCE,
                                  [c_void_p, c_void_p, POINTER(c_void_p)])
        self._create_texture2d = _bind(self.device, DEV_CREATE_TEXTURE2D,
                                       [POINTER(D3D11_TEXTURE2D_DESC), c_void_p, POINTER(c_void_p)])
        self._copy = _bind(self.ctx, CTX_COPY_RESOURCE, [c_void_p, c_void_p], restype=None)
        self._map = _bind(self.ctx, CTX_MAP, [c_void_p, c_uint, c_uint, c_uint,
                                              POINTER(D3D11_MAPPED_SUBRESOURCE)])
        self._unmap = _bind(self.ctx, CTX_UNMAP, [c_void_p, c_uint], restype=None)

        iid = (ctypes.c_ubyte * 16)(*IID_ID3D11_TEXTURE2D)
        res = c_void_p()
        hr = self._open_shared(self.device, c_void_p(shared_handle), iid, byref(res))
        if hr != S_OK or not res.value:
            raise RuntimeError(f"OpenSharedResource(0x{shared_handle:X}) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        self.texture = res.value
        desc = D3D11_TEXTURE2D_DESC(self.width, self.height, 1, 1, fmt, DXGI_SAMPLE_DESC(1, 0),
                                    USAGE_STAGING, 0, CPU_ACCESS_READ, 0)
        out = c_void_p()
        hr = self._create_texture2d(self.device, byref(desc), None, byref(out))
        if hr != S_OK:
            raise RuntimeError(f"消费者建 staging 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        self.staging = out.value
        _ = iid

    def read(self) -> bytes:
        """把共享贴图拷进本地 staging 再回读（**消费者自己的**命令队列）。"""
        self._copy(self.ctx, self.staging, self.texture)
        mapped = D3D11_MAPPED_SUBRESOURCE()
        hr = self._map(self.ctx, self.staging, 0, MAP_READ, 0, byref(mapped))
        if hr != S_OK:
            raise RuntimeError(f"消费者 Map 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        try:
            return b"".join(ctypes.string_at(mapped.pData + y * mapped.RowPitch, self.row_pitch)
                            for y in range(self.height))
        finally:
            self._unmap(self.ctx, self.staging, 0)

    def close(self) -> None:
        for attr in ("staging", "texture"):
            ptr = getattr(self, attr, 0)
            if ptr:
                _release(ptr)
                setattr(self, attr, 0)


# ------------------------------------------------------------------ 帧内容
def make_pattern(idx: int, w: int, h: int) -> bytes:
    """确定性图案，末尾 4 字节带帧号（右下角像素在渲染图里是透明的，不挡画面）。"""
    body = bytes(((idx * 7 + (i // 4) + (i % 4) * 3) % 256 for i in range(w * h * 4 - 4)))
    return body + idx.to_bytes(4, "little")


def _idx_of(data: bytes) -> int:
    return int.from_bytes(data[-4:], "little")


def make_panel(idx: int, cfg) -> bytes:
    """走**真实渲染**：两帧内容在视觉上明显不同，便于用眼判断是新帧还是旧帧。"""
    from vlt.output.overlay import render_conversation

    texts = [("theirs", f"旧帧 OLD #{idx}", f"旧内容 old content #{idx}"),
             ("mine", f"新帧 NEW #{idx}", f"新内容 new content #{idx}")]
    entries = [texts[idx % 2]]
    img = render_conversation(entries, cfg)
    raw = bytearray(img.convert("RGBA").tobytes())
    raw[-4:] = idx.to_bytes(4, "little")          # 帧号塞进透明角落像素
    return bytes(raw)


def _upload(tex: D3D11Texture, data: bytes, flush: bool | None) -> None:
    """flush=None → 用生产默认（修复后即 Flush 打开）。"""
    if flush is None:
        tex.upload(data)
        return
    try:
        tex.upload(data, flush=flush)
    except TypeError:
        if flush:
            raise RuntimeError("生产 upload() 还没有 flush= 参数（修复未落地）") from None
        tex.upload(data)          # 旧写法：不 Flush


def _save_png(data: bytes, w: int, h: int, path: Path) -> None:
    from PIL import Image

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 合成到不透明黑底，便于看图（面板底色本身是半透明的）
    img = Image.frombytes("RGBA", (w, h), data).convert("RGBA")
    bg = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    bg.alpha_composite(img)
    bg.convert("RGB").save(path)


# ------------------------------------------------------------------ 同进程 A/B
def mode_same(args) -> int:
    w, h = args.size
    cfg = _cfg(args.size)
    tex = D3D11Texture(w, h)
    reader = IndependentReader(tex.shared_handle, w, h)
    print(f"[same] 生产者设备={tex.driver} 消费者设备={reader.driver} "
          f"共享句柄=0x{tex.shared_handle:X} 贴图={w}x{h}")

    def frame(i: int) -> bytes:
        return make_panel(i, cfg) if args.png else make_pattern(i, w, h)

    # 预热：先写第 0 帧，确认消费者能读到（否则后面「读到旧帧」无从判定）
    f0 = frame(0)
    _upload(tex, f0, args.flush)
    seen0 = reader.read()
    ok0 = seen0 == f0 or seen0[-4:] == f0[-4:]
    print(f"[same] 预热帧 0：消费者读到 idx={_idx_of(seen0)}，"
          f"{'一致 OK' if ok0 else '不一致（消费者根本读不到内容）'}")

    fresh = stale = 0
    for i in range(1, args.frames + 1):
        data = frame(i)
        t0 = time.perf_counter()
        _upload(tex, data, args.flush)
        dt = (time.perf_counter() - t0) * 1000
        seen = reader.read()
        idx = _idx_of(seen)
        is_fresh = seen[-4:] == data[-4:]
        fresh += is_fresh
        stale += (not is_fresh)
        mark = "fresh ✅" if is_fresh else f"stale ❌（消费者看到的是第 {idx} 帧）"
        print(f"[same] 第 {i:>3} 帧：upload {dt:5.2f}ms → 消费者 idx={idx:<3} {mark}")
    print(f"[same] 汇总（flush={args.flush}）：fresh={fresh} stale={stale} / 共 {args.frames} 帧")

    # ---- 取证帧：把「我们写进去的」与「另一个消费者读到的」各存一张 PNG，用眼判断 ----
    if args.png:
        old, new = 98, 99                      # 98 = 旧帧（OLD），99 = 新帧（NEW）
        tag = "flush" if (args.flush is None or args.flush) else "noflush"
        _upload(tex, frame(old), args.flush)
        seen_before = reader.read()
        _upload(tex, frame(new), args.flush)
        # ⚠️ 顺序要紧：消费者那次读**必须**排在生产者的 `_read_back()` 之前。
        #    `_read_back()` 里的 Map(READ) 会**阻塞到自己的命令（含拷进共享纹理）执行完**
        #    → 等于替消费者做了一次同步，会让 --no-flush 也「看起来正常」（实测踩过）。
        seen_after = reader.read()              # 独立设备看到的内容
        prod = tex._read_back()                 # 生产者自己回读（staging）＝我们写进去的内容
        _save_png(prod, w, h, OUT_DIR / f"producer_readback_{tag}.png")
        _save_png(seen_before, w, h, OUT_DIR / f"consumer_before_{tag}.png")
        _save_png(seen_after, w, h, OUT_DIR / f"consumer_after_{tag}.png")
        verdict = "看到的是刚上传的新帧 ✅" if seen_after[-4:] == frame(new)[-4:] else \
                  f"看到的是**旧帧** idx={_idx_of(seen_after)} ❌（整帧滞后）"
        print(f"[same] 取证帧：上传第 {new} 帧后 —— 生产者回读 idx={_idx_of(prod)}，"
              f"独立消费者 {verdict}")
        print(f"[same] PNG：{OUT_DIR}\\producer_readback_{tag}.png / "
              f"consumer_before_{tag}.png / consumer_after_{tag}.png")

    reader.close()
    tex.close()
    return 0


def _cfg(size):
    from vlt.output.overlay import OverlayConfig

    c = OverlayConfig()
    c.size_px = size
    return c


# ------------------------------------------------------------------ 跨进程
def mode_reader(args) -> int:
    """子进程：打开父进程给的共享句柄，轮询内容变化并打时间戳。"""
    import zlib

    reader = IndependentReader(args.handle, args.size[0], args.size[1])
    last = None
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        data = reader.read()
        sig = zlib.crc32(data)
        if sig != last:
            print(f"CHANGE {time.time():.6f} sig={sig}", flush=True)
            last = sig
        time.sleep(0.002)
    reader.close()
    print("READER_DONE", flush=True)
    return 0


def mode_wrist(args) -> int:
    """**生产类**（`WristOverlay.update_entries`）→ 独立进程消费者：量「内容更新到看得见」的滞后。

    这是本 PR 最贴近用户现象的一层判据：生产者不是手写的 D3D 调用，而是 GUI 每来一条
    译文就调用的那条路（渲染 → upload(含 Flush) → setOverlayTexture）。
    消费者是**另一个进程**里独立 D3D11 设备打开同一份 DXGI 共享句柄（≈ vrcompositor）。
    帧的识别用整帧 CRC32（生产渲染是确定性的，父进程自己渲染同一份内容取 CRC）。
    """
    import zlib

    from vlt.output.openvr_overlay import WristOverlay, _rgba_bytes
    from vlt.output.overlay import render_conversation

    cfg = _cfg(args.size)
    cfg.overlay_key = "vlt.probe.wristlag"      # 独立 key：不碰正在运行的程序那条
    cfg.anchor = args.anchor
    ov = WristOverlay(cfg, config_path=None)
    if not ov.start():
        raise RuntimeError("WristOverlay.start() 失败（SteamVR 没在跑？）")
    if ov._d3d is None:
        raise RuntimeError("没走上持久贴图路（降级了）—— 这次量不到新路")
    handle = ov._d3d.shared_handle
    print(f"[wrist] 生产类已接管：anchor={cfg.anchor} 共享句柄=0x{handle:X} "
          f"贴图={args.size[0]}x{args.size[1]} 降级说明={ov.degraded_note!r}")

    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--mode", "reader",
         "--handle", str(handle), "--size", f"{args.size[0]}x{args.size[1]}",
         "--seconds", str(args.frames * args.gap + 4.0)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    time.sleep(0.6)

    uploads: dict[int, float] = {}
    sigs: dict[int, int] = {}
    for i in range(1, args.frames + 1):
        entries = [("theirs", f"第 {i} 帧原文 source #{i}",
                    f"第 {i} 帧译文 translation #{i}")]
        sigs[i] = zlib.crc32(_rgba_bytes(render_conversation(entries, cfg)))
        ov.update_entries(entries, force=True)
        uploads[i] = time.time()
        print(f"UPLOAD {uploads[i]:.6f} idx={i}", flush=True)
        time.sleep(args.gap)

    by_sig: dict[int, float] = {}
    assert child.stdout is not None
    try:
        while True:
            line = child.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("CHANGE"):
                _, ts, kv = line.split()
                by_sig[int(kv.split("=")[1])] = float(ts)
            if line == "READER_DONE":
                break
    finally:
        child.terminate()
        child.wait(timeout=10)

    lags = [round((by_sig[sigs[i]] - uploads[i]) * 1000, 1) for i in sorted(uploads)
            if sigs[i] in by_sig]
    misses = [i for i in sorted(uploads) if sigs[i] not in by_sig]
    print(f"[wrist] update_entries → 另一个进程看到新内容：滞后（ms）{lags}")
    if lags:
        print(f"[wrist] 中位 {statistics.median(lags):.1f}ms / 最大 {max(lags):.1f}ms "
              f"/ 帧数 {len(lags)}/{len(uploads)}")
    if misses:
        print(f"[wrist] ⚠️ 这些帧消费者全程没看到（整帧滞后）：{misses}")
    print(f"[wrist] 生产类现场：{ov._state_brief()}")
    ov.close()
    return 0


def mode_cross(args) -> int:
    import zlib

    w, h = args.size
    cfg = _cfg(args.size)
    tex = D3D11Texture(w, h)
    print(f"[cross] 生产者设备={tex.driver} 共享句柄=0x{tex.shared_handle:X} 贴图={w}x{h}")
    # 先写一帧，让子进程有个已知起点
    _upload(tex, make_panel(0, cfg), args.flush)

    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--mode", "reader",
         "--handle", str(tex.shared_handle), "--size", f"{w}x{h}",
         "--seconds", str(args.frames * args.gap + 4.0)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    time.sleep(0.6)                      # 等子进程把设备与共享资源建好

    uploads: dict[int, float] = {}
    sigs: dict[int, int] = {}
    for i in range(1, args.frames + 1):
        data = make_panel(i, cfg)
        sigs[i] = zlib.crc32(data)
        _upload(tex, data, args.flush)
        uploads[i] = time.time()
        print(f"UPLOAD {uploads[i]:.6f} idx={i}", flush=True)
        time.sleep(args.gap)

    by_sig: dict[int, float] = {}
    assert child.stdout is not None
    try:
        while True:
            line = child.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("CHANGE"):
                _, ts, kv = line.split()
                by_sig[int(kv.split("=")[1])] = float(ts)
            if line == "READER_DONE":
                break
    finally:
        child.terminate()
        child.wait(timeout=10)

    lags = [round((by_sig[sigs[i]] - uploads[i]) * 1000, 1) for i in sorted(uploads)
            if sigs[i] in by_sig]
    misses = [i for i in sorted(uploads) if sigs[i] not in by_sig]
    print(f"[cross] 消费者看到新帧的滞后（ms）：{lags}")
    if lags:
        print(f"[cross] 中位 {statistics.median(lags):.1f}ms / 最大 {max(lags):.1f}ms")
    if misses:
        print(f"[cross] ⚠️ 全程没看到这些帧（整帧滞后/漏掉）：{misses}")
    tex.close()
    return 0


# ------------------------------------------------------------------ main
def _parse_size(s: str) -> tuple[int, int]:
    a, b = s.lower().split("x")
    return int(a), int(b)


def main() -> int:
    ap = argparse.ArgumentParser(description="共享贴图跨设备/跨进程回读探针")
    ap.add_argument("--mode", choices=("all", "same", "cross", "wrist", "reader"), default="all")
    ap.add_argument("--frames", type=int, default=20)
    ap.add_argument("--size", type=_parse_size, default=(1024, 440))
    ap.add_argument("--gap", type=float, default=0.3, help="跨进程/生产类模式每帧间隔（秒）")
    ap.add_argument("--flush", dest="flush", action="store_true", default=None)
    ap.add_argument("--no-flush", dest="flush", action="store_false")
    ap.add_argument("--png", action="store_true", help="用真实渲染面板 + 存 PNG（便于看图）")
    ap.add_argument("--handle", type=int, default=0, help="内部：子进程模式用")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--anchor", default="hmd", help="wrist 模式用（null 驱动下只有 hmd 有确定位姿）")
    args = ap.parse_args()

    if args.mode == "reader":
        return mode_reader(args)
    rc = 0
    if args.mode in ("all", "same"):
        rc |= mode_same(args)
    if args.mode in ("all", "cross"):
        rc |= mode_cross(args)
    if args.mode == "wrist":
        rc |= mode_wrist(args)
    return rc


if __name__ == "__main__":
    import traceback

    code = 0
    try:
        code = main()
    except BaseException:                    # noqa: BLE001 —— 必须自己打，否则 os._exit 会吞掉堆栈
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)        # ⚠️ 跳过解释器拆卸（ctypes-COM 场景会 SIGSEGV，见模块 docstring）
