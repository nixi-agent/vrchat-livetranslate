"""Windows 独占：一张**持久 D3D11 贴图**（纯 ctypes，无第三方依赖），供 SteamVR 手腕屏走
`SetOverlayTexture` 上传，替代每帧调 `SetOverlayRaw` 的旧路。

⚠️ 平台纯度：本模块是 **Windows 独占**（`ctypes.WinDLL` / `WINFUNCTYPE` 只在 Windows 有）。
它只被同样 Windows 独占的 `vlt/output/openvr_overlay.py` 引入；打 Linux 版（AppImage）时
构建脚本会把它排掉，`scripts/check_platform_purity.py --platform linux` 会断言它不在产物里
（模块名 `vlt.output.win_d3d11` 已登记进 FORBIDDEN / EXCLUDES）。非 Windows 上一 import 本模块
不会炸（模块级只定义常量与 ctypes 结构体），但 `D3D11Texture(...)` 一构造就会因 `WinDLL`
缺失抛异常 —— 上层据此自动降级回 `setOverlayRaw`。

## 为什么不是 `setOverlayRaw`（这是本模块存在的全部理由）

`IVROverlay::SetOverlayRaw()` 是**给静态图用的**接口，官方与实测都不该拿它刷视频/高频面板：

  · openvr.h 原文（`SetOverlayRaw` 上方）："Separate interface for providing the data as a stream
    of bytes, **but there is an upper bound on data that can be sent.**"
  · Valve 工程师（openvr#941）："SetOverlayRaw is shockingly inefficient. Don't use it for video.
    It's really only useful for things you're not going to update more than once... make your own
    graphics device, get the data into a texture, and call SetOverlayTexture."
  · 实测（用户机器，2026-10-11）：旧路连刷 260 次，**第 202 次开始 `OverlayError_RequestFailed`**
    （与 openvr#1565 的 ~200 次一致）。用户可感知症状：每 ~2.3 分钟手腕屏僵住 4~6 秒。
  · 本路（持久贴图 + SetOverlayTexture）连刷 **700 次零失败**：本机真 SteamVR 用**生产代码**
    （`scripts/verify_overlay_texture.py --steamvr`）单次 mean **1.52ms**，`IsOverlayVisible` 全程为真；
    纯 D3D11 上传（`--offline`）单次 mean **0.907ms**。

做法：**自建 D3D11 设备 + 一张只建一次的 `MISC_SHARED` 纹理**，每次刷新只更新它的内容，再把
**DXGI 共享句柄**交给 `SetOverlayTexture`。没有「每次新分配贴图」，200 次上限与闪烁一起消失。

## ⚠️ 句柄语义（2026-10-11 本机真 SteamVR 逐项实测，别凭头文件猜）

`Texture_t` 的 handle 有三个候选：纹理自身指针、SRV、DXGI 共享句柄。实测结论：

    eType=TextureType_DirectX(0) + 纹理指针        → OverlayError_InvalidTexture（一律被拒）
    eType=TextureType_DirectX(0) + SRV             → OverlayError_InvalidTexture
    eType=TextureType_DirectX(0) + 共享纹理        → OverlayError_InvalidTexture
    eType=TextureType_DXGISharedHandle(5) + 共享句柄 → **被接受**（唯一可行）

与 openvr.h 注释一致：`TextureType_DXGISharedHandle = 5 … only supported for **Overlay**
render targets`。**注意**：用户机器上 `eType=0` 那几种是**静默不报错但屏幕上什么都不画**，
所以「API 没报错」绝不能当成「用户看得见」。配 `ColorSpace_Auto(0)`；`ColorSpace_Linear`
会被 SteamVR 拒绝，`Gamma(1)` 的外观仍在人眼比对。

## ⚠️ 更新内容：**不要**用 `UpdateSubresource` 直写共享纹理

本机实测：对 `MISC_SHARED` 纹理调一次 `UpdateSubresource` 之后，`SetOverlayTexture` 就**永久**
`OverlayError_InvalidTexture`（驱动把资源弄坏了）。必须走「DYNAMIC 源纹理 → Map(WRITE_DISCARD)
逐行写 → Unmap → CopyResource 拷进共享纹理」（见 `upload()`）。`CTX_UPDATE_SUBRESOURCE` 索引
仍列在下面备查，但**本模块刻意不绑定它**，免得有人顺手用回去。

## 常量 / 虚表索引来源（照抄，别自己数）

来源：mingw-w64 `d3d11.h`（WIDL 生成，与 Windows SDK 的 vtable 顺序一致）。这些索引是**踩过的
坑**：数错一个就调到别的 COM 方法上，症状是随机崩溃或 `E_INVALIDARG`，极难定位。

    ID3D11Device:        CreateTexture2D=5   CreateShaderResourceView=7   GetDeviceRemovedReason=39
    ID3D11DeviceContext: Map=14  Unmap=15  CopyResource=47  UpdateSubresource=48（本模块不绑，见上）
    IDXGIResource:       GetSharedHandle=8（实测；IID 见 IID_IDXGI_RESOURCE）
    枚举：DXGI_FORMAT_R8G8B8A8_UNORM=28 / _UNORM_SRGB=29；D3D11_USAGE_DEFAULT=0 / STAGING=3
         D3D11_BIND_SHADER_RESOURCE=8；D3D11_CPU_ACCESS_READ=0x20000；D3D11_MAP_READ=1
         D3D11_SDK_VERSION=7；DRIVER_HARDWARE=1 / WARP=5
         D3D_FEATURE_LEVEL_11_1=0xB100 / 11_0=0xB000

## ⚠️ 已知边界 / 取舍（真机覆盖不到的，写清楚别让人猜）

  1. **混合显卡笔记本**：`D3D11CreateDevice(HARDWARE, pAdapter=None)` 选到的适配器**不保证**
     等于 SteamVR 合成器渲染用的那块（本机是 i5-12400F 无核显，天然覆盖不到这条路径）。
     跨适配器打不开共享句柄时预期落成 `OverlayError_InvalidTexture` → 上层永久降级回
     `setOverlayRaw`（等于把本模块要修的老问题带回来）。**未验**，暂时接受；要彻底解需要
     按 SteamVR 的适配器 LUID 建同一块设备（`IVRSystem::GetOutputDevice`）。
  2. **`MISC_SHARED`（非 KeyedMutex）没有生产者/消费者同步**：我们写、合成器读之间无互斥，
     理论上可能撕裂。面板内容低频（1~2 Hz）、实测未见 → **已知取舍**；要更强保证得换
     `MISC_SHARED_KEYEDMUTEX` + 双方 AcquireSync。
  3. **硬重启（`openvr.shutdown()` → `init()`）之后共享句柄重新绑定的真实行为没有真机验证**：
     单测只断言「重建后又调了一次 `setOverlayTexture`」。旧路上硬重启是救场手段，新路下是否
     仍有效待真机复核。

## ⚠️ close() 的两条铁律（血泪坑）

  1. **必须幂等**：`close()` 可能被上层在异常清理里调多次。
  2. **多张纹理共用一个设备时，只释放自己的纹理/SRV，绝不释放共享的 device/ctx** ——
     否则后续的 `Release` 打在已释放的指针上 → access violation，实测直接把进程带走。
     用 `_owns_device` 标记归属：只有自建设备的实例才在 close 里释放 device/ctx。

## COM 方法只绑一次

ctypes 的 `WINFUNCTYPE` 绑定有成本；`_bind()` 只在 `__init__` 里各绑一次并存成实例属性，
**不要每帧重绑**（那会把 per-call 成本抬高一个量级）。
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_uint, c_void_p

# ---- D3D11 常量 / 虚表索引（来源见模块 docstring，均取自头文件原文）----
D3D11_SDK_VERSION = 7
DRIVER_HARDWARE, DRIVER_WARP = 1, 5
FEATURE_LEVELS = (0xB100, 0xB000)          # D3D_FEATURE_LEVEL_11_1 / 11_0
FMT_RGBA8_UNORM, FMT_RGBA8_SRGB = 28, 29   # 产品默认 UNORM；SRGB 仅色空间比对时用
USAGE_DEFAULT, USAGE_DYNAMIC, USAGE_STAGING = 0, 2, 3
BIND_SHADER_RESOURCE = 8
CPU_ACCESS_READ, CPU_ACCESS_WRITE = 0x20000, 0x10000
MAP_READ, MAP_WRITE_DISCARD = 1, 4
# ⚠️ 共享纹理是**唯一**能被 SteamVR 接受的形态（实测，见模块 docstring「句柄语义」）：
#    openvr 客户端与合成器是两个进程，非共享纹理过不去。
MISC_SHARED = 0x2
# {035f3ab4-482e-4e50-b41f-8a7f8bd8960b} —— 前三段按 little-endian 落内存（GUID 的内存布局）
IID_IDXGI_RESOURCE = bytes((0xB4, 0x3A, 0x5F, 0x03, 0x2E, 0x48, 0x50, 0x4E,
                            0xB4, 0x1F, 0x8A, 0x7F, 0x8B, 0xD8, 0x96, 0x0B))
IDXGI_RESOURCE_GET_SHARED_HANDLE = 8   # 实测可用（见 docstring）
DEV_CREATE_TEXTURE2D, DEV_CREATE_SRV, DEV_REMOVED_REASON = 5, 7, 39
CTX_MAP, CTX_UNMAP, CTX_COPY_RESOURCE, CTX_UPDATE_SUBRESOURCE = 14, 15, 47, 48
S_OK = 0


class DXGI_SAMPLE_DESC(Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class D3D11_TEXTURE2D_DESC(Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint), ("ArraySize", c_uint),
                ("Format", c_uint), ("SampleDesc", DXGI_SAMPLE_DESC), ("Usage", c_uint),
                ("BindFlags", c_uint), ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]


class D3D11_MAPPED_SUBRESOURCE(Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


def _vtbl(ptr: int):
    return ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents


def _bind(ptr: int, idx: int, argtypes: list, restype=ctypes.c_long):
    """绑定一个 COM 方法（**只绑一次**，见模块 docstring）。"""
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(_vtbl(ptr)[idx])


def _release(ptr: int | None) -> None:
    """IUnknown::Release（虚表索引 2）。永不抛异常：清理路径上再抛只会掩盖真问题。"""
    if not ptr:
        return
    try:
        _bind(ptr, 2, [])(ptr)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[win_d3d11] Release(0x{ptr:X}) 失败（忽略）：{type(exc).__name__}: {exc}")


class D3D11Texture:
    """一张持久 D3D11 贴图（默认 RGBA8 UNORM）+ 它的 SRV。建一次，之后只更新内容。

    公开最小接口：`D3D11Texture(width, height)` / `.upload(bytes)` / `.shared_handle` /
    `.texture` / `.srv` / `.healthy()` / `.close()`。**交给 SteamVR 的必须是 `.shared_handle`
    配 `TextureType_DXGISharedHandle`**（`.texture` / `.srv` 只留给离线回读与排障）。

    `base` 参数用于**多张纹理共用一个设备**（例如色空间比对时建 UNORM/SRGB 两张）：传入已有
    实例即复用它的 device/ctx，本实例 `_owns_device=False`，close 时只释放自己的纹理/SRV。
    """

    def __init__(self, width: int, height: int, fmt: int = FMT_RGBA8_UNORM,
                 base: "D3D11Texture | None" = None) -> None:
        self.width, self.height, self.format = width, height, fmt
        self.row_pitch = width * 4
        self._closed = False
        if base is not None:
            # 复用同一个设备/上下文：只搬方法绑定与句柄，close 时**绝不**释放共享的 device/ctx。
            # ⚠️ 这里列的是**实际绑定过的东西**：本模块刻意不绑 UpdateSubresource
            #    （共享纹理不能直写，见 upload），所以不能把 "_update" 放进来 —— 放了必炸
            #    AttributeError（base= 复用路径一用就挂，实测）。
            for attr in ("driver", "feature_level", "device", "ctx", "_create_texture2d",
                         "_create_srv", "_copy", "_map", "_unmap", "_reason"):
                setattr(self, attr, getattr(base, attr))
            # ⚠️ 先把归属定下来再建纹理：建到一半失败时，_release_owned() 要据此决定是否放 device/ctx
            self._owns_device = False
            self._make_shared_pair(fmt)
            return

        d3d11 = ctypes.WinDLL("d3d11")
        fn = d3d11.D3D11CreateDevice
        fn.restype = ctypes.c_long
        fn.argtypes = [c_void_p, ctypes.c_int, c_void_p, c_uint, POINTER(c_uint), c_uint, c_uint,
                       POINTER(c_void_p), POINTER(c_uint), POINTER(c_void_p)]
        levels = (c_uint * len(FEATURE_LEVELS))(*FEATURE_LEVELS)
        last = None
        for driver in (DRIVER_HARDWARE, DRIVER_WARP):     # 硬件不行退 WARP（软件光栅）
            dev, ctx, got = c_void_p(), c_void_p(), c_uint()
            hr = fn(None, driver, None, 0, levels, len(FEATURE_LEVELS), D3D11_SDK_VERSION,
                    byref(dev), byref(got), byref(ctx))
            if hr == S_OK:
                self.driver = "HARDWARE" if driver == DRIVER_HARDWARE else "WARP"
                self.feature_level = got.value
                self.device, self.ctx = dev.value, ctx.value
                break
            last = f"hr=0x{hr & 0xFFFFFFFF:08X}"
        else:
            raise RuntimeError(f"D3D11CreateDevice 失败（HARDWARE/WARP 都不行）：{last}")

        # COM 方法只绑一次（见模块 docstring）
        self._create_texture2d = _bind(self.device, DEV_CREATE_TEXTURE2D,
                                       [POINTER(D3D11_TEXTURE2D_DESC), c_void_p, POINTER(c_void_p)])
        self._create_srv = _bind(self.device, DEV_CREATE_SRV,
                                 [c_void_p, POINTER(c_void_p), POINTER(c_void_p)])
        self._copy = _bind(self.ctx, CTX_COPY_RESOURCE, [c_void_p, c_void_p], restype=None)
        self._map = _bind(self.ctx, CTX_MAP, [c_void_p, c_uint, c_uint, c_uint,
                                              POINTER(D3D11_MAPPED_SUBRESOURCE)])
        self._unmap = _bind(self.ctx, CTX_UNMAP, [c_void_p, c_uint], restype=None)
        self._reason = _bind(self.device, DEV_REMOVED_REASON, [])

        self._owns_device = True          # 同上：先定归属，失败路径才有据可依
        self._make_shared_pair(fmt)

    def _new_texture(self, fmt: int, usage: int, bind: int, cpu: int, misc: int = 0) -> int:
        desc = D3D11_TEXTURE2D_DESC(self.width, self.height, 1, 1, fmt, DXGI_SAMPLE_DESC(1, 0),
                                    usage, bind, cpu, misc)
        out = c_void_p()
        hr = self._create_texture2d(self.device, byref(desc), None, byref(out))
        if hr != S_OK:
            raise RuntimeError(f"CreateTexture2D(fmt={fmt}) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return out.value

    def _new_srv(self, tex: int) -> int:
        out = c_void_p()
        hr = self._create_srv(self.device, tex, None, byref(out))   # NULL desc = 全资源视图
        if hr != S_OK:
            raise RuntimeError(f"CreateShaderResourceView 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return out.value

    def _make_shared_pair(self, fmt: int) -> None:
        """建「共享持久纹理 + 一张 DYNAMIC 源纹理」，并取好 DXGI 共享句柄（建一次就缓存）。

        ⚠️ **共享纹理不能用 `UpdateSubresource` 更新内容**：本机实测调一次之后
        `SetOverlayTexture` 就永久 `OverlayError_InvalidTexture`（驱动把资源弄坏了）。
        所以内容必须先写进 DYNAMIC 源纹理，再 `CopyResource` 拷进共享纹理（见 upload）。
        """
        # 先清零：任何一步失败都靠 _release_owned() 按「已建出哪些」释放，不留无人引用的 COM 指针
        self.texture = self.srv = self._src = self._shared = 0
        try:
            self.texture = self._new_texture(fmt, USAGE_DEFAULT, BIND_SHADER_RESOURCE, 0, MISC_SHARED)
            self.srv = self._new_srv(self.texture)
            self._src = self._new_texture(fmt, USAGE_DYNAMIC, BIND_SHADER_RESOURCE, CPU_ACCESS_WRITE)
            self._shared = self._query_shared_handle()  # 建一次就缓存，别每帧重新 QueryInterface
            if not self._shared:
                raise RuntimeError("取 DXGI 共享句柄失败（MISC_SHARED 纹理才支持）")
        except Exception:
            self._release_owned()          # 构造失败也要好好 Release（含自建的 device/ctx）
            raise

    def _query_shared_handle(self) -> int:
        """QueryInterface(IID_IDXGIResource) → GetSharedHandle()。前者是 IUnknown::QueryInterface(0)。"""
        qi = _bind(self.texture, 0, [c_void_p, c_void_p])
        iid = (ctypes.c_ubyte * 16)(*IID_IDXGI_RESOURCE)
        res = c_void_p()
        hr = qi(self.texture, iid, byref(res))
        if hr != S_OK or not res.value:
            raise RuntimeError(f"QueryInterface(IDXGIResource) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        try:
            gsh = _bind(res.value, IDXGI_RESOURCE_GET_SHARED_HANDLE, [POINTER(c_void_p)])
            h = c_void_p()
            hr = gsh(res.value, byref(h))
        finally:
            _release(res.value)
        if hr != S_OK:
            raise RuntimeError(f"GetSharedHandle 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return h.value or 0

    @property
    def shared_handle(self) -> int:
        """交给 SteamVR 的 **DXGI 共享句柄**（配 `TextureType_DXGISharedHandle`，别用 `.texture`）。"""
        return self._shared

    def upload(self, data: bytes) -> None:
        """只更新内容（不新建任何 D3D 对象 —— 这正是绕开 200 次上限的关键）。

        路径：Map(源, WRITE_DISCARD) → 逐行 memmove（**按 RowPitch 走行**）→ Unmap
        → CopyResource(源 → 共享纹理)。**不要**改成 UpdateSubresource 直写共享纹理。
        """
        if len(data) != self.row_pitch * self.height:
            raise ValueError(f"字节数 {len(data)} != {self.row_pitch * self.height}"
                             f"（{self.width}x{self.height} RGBA）")
        buf = ctypes.create_string_buffer(data, len(data))
        mapped = D3D11_MAPPED_SUBRESOURCE()
        hr = self._map(self.ctx, self._src, 0, MAP_WRITE_DISCARD, 0, byref(mapped))
        if hr != S_OK:
            raise RuntimeError(f"Map(源纹理) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        try:
            src = ctypes.cast(buf, c_void_p).value
            for y in range(self.height):
                ctypes.memmove(mapped.pData + y * mapped.RowPitch, src + y * self.row_pitch,
                               self.row_pitch)
        finally:
            self._unmap(self.ctx, self._src, 0)
        self._copy(self.ctx, self.texture, self._src)

    def healthy(self) -> bool:
        """设备是否还在（`GetDeviceRemovedReason()==S_OK`）。诊断/自愈用，不在热路径上。"""
        return self._reason(self.device) == S_OK

    def _read_back(self) -> bytes:
        """回读校验（staging 纹理 + CopyResource + Map）——**仅供离线测试/排障**，不在产品热路径。

        保留它的理由：能在**不开 SteamVR** 的前提下逐字节证明「upload 的内容确实进了这张贴图」
        （本机 RTX 4080 离线实测：staging 回读与上传逐字节一致）。产品刷新路径从不调用它，
        所以它带来的 3 个额外 COM 绑定（Copy/Map/Unmap）只在需要验证时才付出代价。
        """
        staging = self._new_texture(FMT_RGBA8_UNORM, USAGE_STAGING, 0, CPU_ACCESS_READ)
        self._copy(self.ctx, staging, self.texture)
        mapped = D3D11_MAPPED_SUBRESOURCE()
        hr = self._map(self.ctx, staging, 0, MAP_READ, 0, byref(mapped))
        if hr != S_OK:
            _release(staging)
            raise RuntimeError(f"Map 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        try:
            chunks = []
            for y in range(self.height):
                addr = mapped.pData + y * mapped.RowPitch
                chunks.append(ctypes.string_at(addr, self.row_pitch))
            return b"".join(chunks)
        finally:
            self._unmap(self.ctx, staging, 0)
            _release(staging)

    def close(self) -> None:
        """释放自己拥有的对象。⚠️ **幂等** + **不释放共享设备**（两条铁律见模块 docstring）。"""
        if self._closed:
            return
        self._closed = True
        self._release_owned()

    def _release_owned(self) -> None:
        """释放**本实例拥有**的 COM 对象（幂等；不会动共享的 device/ctx）。

        与 close() 共用：构造中途失败时也走它 —— 否则 device/ctx（以及 base= 路已建出的
        texture/srv/_src）没人放，正是「失败路径比成功路径更该谨慎」的那类泄漏。
        """
        for attr in ("_src", "srv", "texture"):
            ptr = getattr(self, attr, 0)
            if ptr:
                _release(ptr)
                setattr(self, attr, 0)
        if getattr(self, "_owns_device", False):
            for attr in ("ctx", "device"):
                ptr = getattr(self, attr, 0)
                if ptr:
                    _release(ptr)
                    setattr(self, attr, 0)
