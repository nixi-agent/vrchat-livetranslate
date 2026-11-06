"""Windows 裸盘访问与 $Pagefile 定位（最小 NTFS 子集）。

只做三件事：
  1. 用 ``\\\\.\\PhysicalDriveN`` 只读打开物理盘（卷设备在某些机器上 raw read 返回
     err=50，只有物理盘这条路过得了）；
  2. 从 boot sector 解析出 MFT 位置，沿 $MFT 的 runlist 找到 ``pagefile.sys``，
     跨 ``$ATTRIBUTE_LIST`` 把它的 $DATA 完整 runlist 拼出来；
  3. 按块把 pagefile 的物理区间读出来，给上层做内存镜像搜索。

**不含 CLI、不 print、不 SystemExit**：错误一律抛 :class:`PagefileError`，
进度通过 ``on_log`` 回调外传。这样它才能被主程序直接 import 使用
（Linux 侧的 ``procfs`` 也是同样的纯库形态）。

线程模型：:class:`Disk` 复用一块 IO 缓冲（``read_view`` 返回的 memoryview
下次读就被覆盖），**不是线程安全的**。多线程扫描时每个 worker 自己
``Disk.open()`` 一个句柄 —— 与 ``scripts/pf_key2.py`` 的做法一致。

偏移语义：NTFS 解析路径的 ``offset`` 都是**卷内字节偏移**（分区起点为 0），
:class:`Disk` 内部加 ``base_offset`` 换成盘上偏移；**开句柄一律走
:func:`open_volume`**，它会把「这块盘是 `PhysicalDriveN`」一起带出来
（只取偏移、写死 disk0 会在系统盘不是 0 号盘的机器上读到另一块盘）。
pagefile 扫描路径则用 :class:`Layout` 把卷内区间翻成**盘上绝对偏移**，
再 ``Disk.open(disk_no, 0)``。
"""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable, Iterator
from ctypes import wintypes as wt
from dataclasses import dataclass

__all__ = [
    "Disk",
    "DiskExtent",
    "Extent",
    "Layout",
    "Pagefile",
    "PagefileError",
    "Volume",
    "base_offset_for",
    "find_pagefile",
    "open_volume",
    "volume_extents",
]

# 非 Windows 上进不来（`ctypes` 没有 `WinDLL`）：本模块的 NTFS 解析是纯逻辑，
# 让它在 Linux CI 上也能 import，卷→盘映射那部分才跑得了离线用例。
try:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
except (OSError, AttributeError):                    # pragma: no cover - 平台分派
    _k32 = None

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
#: 绕过系统缓存读（DMA 直达用户缓冲）。扫 34 GiB 这种一次性数据全靠它：
#: 走缓存会把 standby list 冲干净、还要和游戏自己换页抢内存。
FILE_FLAG_NO_BUFFERING = 0x20000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
#: NO_BUFFERING 的硬要求：偏移、长度、缓冲地址全部按**扇区**对齐。
#: 4 KiB 是本机 NTFS 卷扇区（物理扇区 512 的盘也满足这个更严的值）。
READ_ALIGN = 4096


def _alloc_aligned(size: int):
    """按 `READ_ALIGN` 对齐分配一块可复用缓冲（VirtualAlloc 返回 64 KiB 对齐）。"""
    k32 = _k32
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
    return k32.VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE) or None


def _free_aligned(ptr) -> None:
    if not ptr:
        return
    k32 = _k32
    k32.VirtualFree.restype = wt.BOOL
    k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
    k32.VirtualFree(ctypes.c_void_p(ptr), 0, MEM_RELEASE)

# IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS：拿分区在物理盘上的起点与长度
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
ERROR_MORE_DATA = 234

ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80

# pagefile 的规范名（含 8.3 短名与 NTFS 元数据名）。用于在 MFT 里区分真
# pagefile.sys 和名字里恰好含 "pagefile" 的普通文件（我们的探测脚本就中过招）。
PAGEFILE_EXACT = frozenset({
    "pagefile.sys", "$pagefile", "$pagefile.sys", "$pagef~1.sys", "pagef~1.sys",
})


def _is_pagefile_name(name: str) -> bool:
    """这个名字像不像 pagefile（含 8.3 短名 `pagef~1.sys`）。

    「按缓存记录号直取」和「全表扫描」必须用**同一套**判据：以前两处都写成
    `"pagefile" in name.lower()`，而 8.3 短名是 `pagef~1.sys`；万一某条记录只
    剩短名，直取会判「不可信」退回全表扫，全表扫又用同一个子串判据找不到 ——
    白扫 60 秒之后失败。
    """
    low = (name or "").lower()
    return low in PAGEFILE_EXACT or "pagefile" in low

#: 采信「记录号缓存」的下限。真的 pagefile.sys 至少是这个量级；比它小就说明那条
#: MFT 记录早被系统重建成别的文件了（pagefile 改过大小/重建时记录号会变）。
_HINT_MIN_BYTES = 64 << 20

Log = Callable[[str], None] | None


class PagefileError(OSError):
    """裸盘访问或 NTFS 解析失败。"""


# ---------------------------------------------------------------- 卷与分区
@dataclass(frozen=True)
class DiskExtent:
    """一个分区在物理盘上的位置。"""

    disk_number: int
    starting_offset: int
    length: int


def volume_extents(drive: str = "C") -> list[DiskExtent]:
    """查分区在哪些物理盘上、从哪个字节开始。

    只要求卷设备能打开（``dwDesiredAccess=0`` 即可），不需要读权限。
    非提权进程通常也能成功；打不开时抛 :class:`PagefileError`。
    """
    letter = drive.rstrip(":\\").upper()[:1]
    path = f"\\\\.\\{letter}:"
    k32 = _k32
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    h = k32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE or not h:
        raise PagefileError(f"打不开卷 {path} err={ctypes.get_last_error()}")
    try:
        return _disk_extents(h, path)
    finally:
        _k32.CloseHandle(h)


def _disk_extents(h, path: str) -> list[DiskExtent]:
    # 返回体是 VOLUME_DISK_EXTENTS：[NumberOfDiskExtents(4)][pad(4)][extent...]，
    # 每个 extent 是 [DiskNumber(4)][pad(4)][StartingOffset(8)][ExtentLength(8)]。
    out = (ctypes.c_ubyte * 1024)()
    ret = wt.DWORD(0)
    ok = _k32.DeviceIoControl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                              None, 0, out, ctypes.sizeof(out),
                              ctypes.byref(ret), None)
    if not ok:
        raise PagefileError(
            f"{path} 的 IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS 失败 "
            f"err={ctypes.get_last_error()}")
    count = struct.unpack_from("<I", bytes(out), 0)[0]
    got: list[DiskExtent] = []
    for i in range(count):
        base = 8 + i * 24
        disk, _pad, start, length = struct.unpack_from("<IIQQ", bytes(out), base)
        got.append(DiskExtent(int(disk), int(start), int(length)))
    return got


def base_offset_for(drive: str = "C") -> int | None:
    """分区在物理盘上的起始字节；查不到返回 None（调用方回退到配置值）。

    ⚠️ 只有「单段 + 你知道是哪块盘」时才够用：偏移本身没有盘号，拿它去开
    `PhysicalDrive0` 在系统盘不是 0 号盘的机器上会读到**另一块盘**。
    要开句柄请用 :func:`open_volume`（或 :class:`Layout`）。
    """
    try:
        exts = volume_extents(drive)
    except PagefileError:
        return None
    return exts[0].starting_offset if exts else None


@dataclass(frozen=True)
class Layout:
    """卷内偏移 ↔ 物理盘偏移的分段映射（一个卷可能跨多块物理盘）。

    `volume_extents()` 的每一段都带「盘号 + 盘上起点」，而本模块其余部分的
    offset 都是**卷内**偏移。以前只取第一段的起点、把盘号丢掉、调用方一律
    `Disk.open(0, …)`：在「系统盘不是 `PhysicalDrive0`」的机器上（实测：
    NVMe 是 disk3、disk0 是挂着 F: 的数据盘）就会拿 C: 的分区起点去读 disk0，
    读到的是 F: 里的文件数据 —— boot sector 判不出 NTFS，取钥恒失败。
    """

    extents: tuple[DiskExtent, ...]

    def __post_init__(self) -> None:
        if not self.extents:
            raise PagefileError("Layout 没有任何 extent")
        if any(int(e.length) <= 0 for e in self.extents):
            raise PagefileError("Layout 里有长度为 0 的 extent")

    @classmethod
    def of(cls, drive: str = "C") -> "Layout":
        """查 `drive` 在物理盘上的分布（非提权即可，只要卷设备能打开）。"""
        exts = tuple(volume_extents(drive))
        if not exts:
            raise PagefileError(f"卷 {drive}: 没有任何 disk extent")
        return cls(exts)

    @property
    def disk_no(self) -> int:
        """卷起点所在物理盘（boot sector / $MFT 都从这里读）。"""
        return self.extents[0].disk_number

    @property
    def base_offset(self) -> int:
        """卷起点在这块盘上的字节偏移。"""
        return self.extents[0].starting_offset

    @property
    def total_bytes(self) -> int:
        return sum(int(e.length) for e in self.extents)

    def resolve(self, offset: int, size: int) -> list[tuple[int, int, int]]:
        """卷内区间 → ``[(盘号, 盘上偏移, 长度), ...]``（跨段处切开）。

        返回值是**盘上绝对偏移**，`Disk.open(disk_no, 0)` 之后可以直接
        `read_view(偏移, 长度)`。区间落在所有段之外时返回空列表。
        """
        if size <= 0:
            return []
        start = int(offset)
        end = start + int(size)
        if start < 0:
            raise ValueError(f"卷内偏移不能为负：{start}")
        out: list[tuple[int, int, int]] = []
        vol_at = 0
        for e in self.extents:
            ext_end = vol_at + int(e.length)
            lo, hi = max(start, vol_at), min(end, ext_end)
            if lo < hi:
                out.append((int(e.disk_number),
                            int(e.starting_offset) + (lo - vol_at),
                            hi - lo))
            vol_at = ext_end
            if vol_at >= end:
                break
        return out


def open_volume(drive: str = "C") -> tuple["Disk", Layout]:
    """打开「卷起点所在的那块物理盘」，并把它的分段布局一起交回来。

    各调用点（`keyd._scan_job` / `win_capture._scan_local`）都**必须**走这里，
    不要再自己 `Disk.open(0, base_offset_for(...))` —— 那正是「系统盘不在
    disk0 时取钥恒失败」的根因。

    只支持**单段**卷：NTFS 解析（boot sector / $MFT / pagefile runlist）全程按
    「卷内偏移 + 一个 base_offset」算，跨多块物理盘的 spanned volume 会在第二段
    之后读错位置。这种情况明确抛 `PagefileError`，不猜、不静默读垃圾
    （扫描侧 `Layout.resolve()` 虽然会按段切开，但盘上位置仍需要每个段各开一个
    句柄，本实现没有做，所以宁可拒绝）。
    """
    lay = Layout.of(drive)
    if len(lay.extents) > 1:
        raise PagefileError(
            f"卷 {drive}: 跨了 {len(lay.extents)} 块物理盘（spanned volume），"
            "本实现只支持单段 —— 请把 pagefile 放到一段连续的分区上")
    try:
        return Disk.open(lay.disk_no, lay.base_offset), lay
    except PagefileError as exc:
        raise PagefileError(
            f"{exc}；要读的是卷 {drive}: 的起点"
            f"（PhysicalDrive{lay.disk_no} @ {lay.base_offset}）") from exc


# ---------------------------------------------------------------- 裸盘读
class Disk:
    """物理盘只读句柄。

    两种 offset 口径并存，别混用：

    * **卷内偏移**（默认，NTFS 解析路径用）：内部加 `base_offset` 换成盘上位置；
    * **盘上绝对偏移**（pagefile 扫描路径用）：`Disk.open(disk_no, 0)` 之后由
      `Layout.resolve()` 给出，直接 `read_view(abs_off, len)`。
    """

    def __init__(self, h, base_offset: int, disk_no: int = 0, *, h_nb=None) -> None:
        self._h = h
        self.base_offset = int(base_offset)
        self.disk_no = disk_no
        #: NO_BUFFERING 句柄（可选）。大顺序扫走它，小读仍走 `_h`。
        self._h_nb = h_nb
        self._buf = None
        self._buf_size = 0
        self._nb_buf = None
        self._nb_size = 0
        self._closed = False

    @classmethod
    def open(cls, disk_no: int = 0, base_offset: int = 0) -> Disk:
        path = f"\\\\.\\PhysicalDrive{disk_no}"
        k32 = _k32
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                    ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
        h = k32.CreateFileW(path, GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, None)
        if h == INVALID_HANDLE_VALUE or not h:
            raise PagefileError(
                f"打开物理盘 {path} 失败 err={ctypes.get_last_error()}（需要管理员）")
        # 再开一个**绕系统缓存**的句柄给大顺序扫用。为什么要它：一次扫 34 GiB，
        # 走缓存的话这批一次性数据会把 standby list 冲干净、还和游戏自己换页抢内存
        # —— 本机实测同一块盘同一份 pagefile，速率在 88~691 MB/s 之间飘。失败就
        # 算了（退回带缓存的老路），不影响取钥。
        # 关掉：`VLT_READ_NOBUF=0`。
        h_nb = None
        if os.environ.get("VLT_READ_NOBUF", "1").strip() != "0":
            h_nb = k32.CreateFileW(
                path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                OPEN_EXISTING, FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, None)
            if h_nb == INVALID_HANDLE_VALUE or not h_nb:
                h_nb = None
        return cls(h, base_offset, disk_no=disk_no, h_nb=h_nb)

    def close(self) -> None:
        if not self._closed:
            _k32.CloseHandle(self._h)
            if self._h_nb:
                _k32.CloseHandle(self._h_nb)
                self._h_nb = None
            if self._nb_buf:
                _free_aligned(self._nb_buf)
                self._nb_buf = None
                self._nb_size = 0
            self._closed = True

    def __enter__(self) -> Disk:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _position(self, offset: int) -> int:
        return int(offset) + self.base_offset

    def read(self, offset: int, size: int) -> bytes:
        """读一段并复制成 bytes（每次新建缓冲，慢；大块扫用 read_view）。"""
        pos = self._position(offset)
        _k32.SetFilePointerEx.restype = wt.BOOL
        _k32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong,
                                          ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
        cur = ctypes.c_longlong(0)
        if not _k32.SetFilePointerEx(self._h, pos, ctypes.byref(cur), 0):
            raise PagefileError(f"SetFilePointerEx({pos}) err={ctypes.get_last_error()}")
        buf = (ctypes.c_ubyte * size)()
        got = wt.DWORD(0)
        if not _k32.ReadFile(self._h, buf, size, ctypes.byref(got), None):
            raise PagefileError(f"ReadFile({pos},{size}) err={ctypes.get_last_error()}")
        return bytes(buf[:got.value])

    def _read_aligned(self, offset: int, size: int) -> memoryview | None:
        """走 `NO_BUFFERING` 句柄读一块；任何前置条件不满足就返回 None（调用方退老路）。

        NO_BUFFERING 要求偏移、长度、**缓冲地址**全按扇区对齐。页文件 extent 的末尾
        不一定对齐到 4 KiB，所以最后一块常常读不了 —— 那种情况直接交给带缓存的老路径，
        不为了几 KiB 让整轮扫描失败。
        """
        pos = self._position(offset)
        if pos % READ_ALIGN:
            return None
        # ⚠️ 句柄是 64 位 HANDLE：不写 argtypes 会被当 32 位 int 传（截断 → 读错
        # 位置或静默失败）。`read()` 里也是这么设的。
        _k32.SetFilePointerEx.restype = wt.BOOL
        _k32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong,
                                          ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
        _k32.ReadFile.restype = wt.BOOL
        _k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                  ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
        want = int(size)
        size = (want + READ_ALIGN - 1) // READ_ALIGN * READ_ALIGN
        if size > self._nb_size:
            if self._nb_buf:
                _free_aligned(self._nb_buf)
                self._nb_buf = None
                self._nb_size = 0
            self._nb_buf = _alloc_aligned(size)
            if not self._nb_buf:
                self._h_nb = None            # 分不出对齐缓冲：以后都走老路
                return None
            self._nb_size = size
        cur = ctypes.c_longlong(0)
        if not _k32.SetFilePointerEx(self._h_nb, pos, ctypes.byref(cur), 0):
            return None
        got = wt.DWORD(0)
        if not _k32.ReadFile(self._h_nb, ctypes.c_void_p(self._nb_buf), size,
                             ctypes.byref(got), None):
            # 跨 extent 末尾 / 盘尾：老路径按字节读，不受对齐约束
            return None
        n = min(int(got.value), want)
        return memoryview((ctypes.c_ubyte * n).from_address(self._nb_buf))

    def read_view(self, offset: int, size: int) -> memoryview:
        """读进复用缓冲，返回 memoryview（**下次读会被覆盖**，用完立刻 release）。

        单次 read 只有 ~65 MB/s（ctypes 数组分配 + 整块拷贝），复用缓冲后
        实测 1.2 GiB/s。

        有 `NO_BUFFERING` 句柄时优先走它（绕系统缓存，大顺序扫更快也更稳）；
        对齐不满足或系统拒绝就自动退回下面这条带缓存的老路。
        """
        if self._h_nb is not None:
            view = self._read_aligned(offset, size)
            if view is not None:
                return view
        if size > self._buf_size:
            self._buf = (ctypes.c_ubyte * size)()
            self._buf_size = size
        pos = self._position(offset)
        cur = ctypes.c_longlong(0)
        if not _k32.SetFilePointerEx(self._h, pos, ctypes.byref(cur), 0):
            raise PagefileError(f"SetFilePointerEx({pos}) err={ctypes.get_last_error()}")
        got = wt.DWORD(0)
        if not _k32.ReadFile(self._h, self._buf, size, ctypes.byref(got), None):
            raise PagefileError(f"ReadFile({pos},{size}) err={ctypes.get_last_error()}")
        return memoryview(self._buf)[:got.value]


# ---------------------------------------------------------------- NTFS 结构
@dataclass(frozen=True)
class Volume:
    """从 boot sector 解析出的 NTFS 卷参数（不需要 FSCTL，物理盘也能用）。"""

    serial: int
    bytes_per_sector: int
    bytes_per_cluster: int
    record_size: int
    mft_start_lcn: int
    mft2_start_lcn: int
    total_clusters: int


def read_boot(disk: Disk) -> Volume:
    bs = disk.read(0, 512)
    if len(bs) < 512 or bs[3:11] != b"NTFS    ":
        raise PagefileError(f"boot sector 不是 NTFS: oem={bs[3:11]!r}")
    bps = struct.unpack_from("<H", bs, 0x0B)[0]
    spc = bs[0x0D]
    if not bps or not spc:
        raise PagefileError(f"boot sector 参数异常 bps={bps} spc={spc}")
    total_sectors = struct.unpack_from("<Q", bs, 0x28)[0]
    mft_lcn = struct.unpack_from("<Q", bs, 0x30)[0]
    mft2_lcn = struct.unpack_from("<Q", bs, 0x38)[0]
    serial = struct.unpack_from("<Q", bs, 0x48)[0]
    cprs = struct.unpack_from("<b", bs, 0x40)[0]
    bpc = bps * spc
    rec_size = (1 << -cprs) if cprs < 0 else cprs * bpc
    return Volume(
        serial=serial,
        bytes_per_sector=bps,
        bytes_per_cluster=bpc,
        record_size=rec_size,
        mft_start_lcn=mft_lcn,
        mft2_start_lcn=mft2_lcn,
        total_clusters=total_sectors // spc,
    )


def _apply_fixup(rec: bytes, bytes_per_sector: int) -> bytes:
    """把 MFT 记录尾部的 USA 序列号还原回真实字节。"""
    buf = bytearray(rec)
    if len(buf) < 8 or buf[0:4] != b"FILE":
        return bytes(buf)
    usa_off, usa_cnt = struct.unpack_from("<HH", buf, 4)
    if usa_cnt == 0 or usa_off + usa_cnt * 2 > len(buf):
        return bytes(buf)
    usa = buf[usa_off:usa_off + usa_cnt * 2]
    for i in range(1, usa_cnt):
        end = i * bytes_per_sector
        if end > len(buf):
            break
        buf[end - 2] = usa[i * 2]
        buf[end - 1] = usa[i * 2 + 1]
    return bytes(buf)


def _parse_runlist(data: bytes) -> list[tuple[int, int]]:
    """返回 [(lcn, cluster_count), ...]。sparse run 直接跳过。"""
    runs: list[tuple[int, int]] = []
    i = 0
    lcn = 0
    n = len(data)
    while i < n:
        head = data[i]
        i += 1
        if head == 0:
            break
        n_len = head & 0x0F
        n_off = (head >> 4) & 0x0F
        if n_len == 0 or i + n_len + n_off > n:
            break
        length = int.from_bytes(data[i:i + n_len], "little")
        i += n_len
        delta = int.from_bytes(data[i:i + n_off], "little", signed=True)
        i += n_off
        if n_off == 0:
            continue
        lcn += delta
        if length:
            runs.append((lcn, length))
    return runs


def _iter_attrs(rec: bytes) -> Iterator[tuple[int, bytes]]:
    if len(rec) < 0x18 or rec[0:4] != b"FILE":
        return
    pos = struct.unpack_from("<H", rec, 0x14)[0]
    while pos + 4 <= len(rec):
        atype, alen = struct.unpack_from("<II", rec, pos)
        if atype == 0xFFFFFFFF or alen == 0 or pos + alen > len(rec):
            break
        yield atype, rec[pos:pos + alen]
        pos += alen


def _parse_file_name(attr: bytes) -> str | None:
    if len(attr) < 0x10:
        return None
    val_off = struct.unpack_from("<H", attr, 0x14)[0]
    val_len = struct.unpack_from("<I", attr, 0x10)[0]
    val = attr[val_off:val_off + val_len]
    if len(val) < 66:
        return None
    name_len = val[64]
    raw = val[66:66 + name_len * 2]
    try:
        return raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def _parse_data_runs(attr: bytes) -> list[tuple[int, int]]:
    if len(attr) < 0x21 or attr[8] == 0:      # resident，pagefile 不会走这条
        return []
    run_off = struct.unpack_from("<H", attr, 0x20)[0]
    return _parse_runlist(attr[run_off:])


def _parse_attr_list(attr: bytes) -> list[tuple[int, int, int]]:
    """解析 $ATTRIBUTE_LIST → [(attr_type, mft_ref, start_vcn)]。

    大文件的 $DATA 挂在扩展记录上，主记录只剩 $STANDARD_INFORMATION /
    $ATTRIBUTE_LIST / $FILE_NAME，必须先走这里才能拿到 runlist。
    entry 布局：type(4) rec_len(2) name_len(1) name_off(1) start_vcn(8)
                base_ref(8) attr_id(2)，base_ref 低 48 位是 MFT 记录号。
    """
    if len(attr) < 0x10 or attr[8]:
        return []
    val_off = struct.unpack_from("<H", attr, 0x14)[0]
    val_len = struct.unpack_from("<I", attr, 0x10)[0]
    val = attr[val_off:val_off + val_len]
    out: list[tuple[int, int, int]] = []
    pos = 0
    while pos + 26 <= len(val):
        atype = struct.unpack_from("<I", val, pos)[0]
        ent_len = struct.unpack_from("<H", val, pos + 4)[0]
        if ent_len < 26 or pos + ent_len > len(val):
            break
        start_vcn = struct.unpack_from("<q", val, pos + 8)[0]
        base_ref = struct.unpack_from("<Q", val, pos + 16)[0]
        out.append((atype, base_ref & 0xFFFFFFFFFFFF, start_vcn))
        pos += ent_len
    return out


# ---------------------------------------------------------------- MFT 遍历
class _Mft:
    """$MFT 自身的 runlist 缓存 + 单条记录读取。一个 Disk 配一个。"""

    def __init__(self, disk: Disk, volume: Volume, *, on_log: Log = None) -> None:
        self._disk = disk
        self._vol = volume
        self._log = on_log
        self._runs: list[tuple[int, int]] | None = None
        self._valid = 0

    def runs(self) -> tuple[list[tuple[int, int]], int]:
        if self._runs is not None:
            return self._runs, self._valid
        vol = self._vol
        base = vol.mft_start_lcn * vol.bytes_per_cluster
        raw = self._disk.read(base, vol.record_size)
        if len(raw) < vol.record_size or raw[0:4] != b"FILE":
            raise PagefileError(f"MFT 记录 0 读不到（lcn={vol.mft_start_lcn}）")
        rec = _apply_fixup(raw, vol.bytes_per_sector)
        runs: list[tuple[int, int]] = []
        real = 0
        for atype, attr in _iter_attrs(rec):
            if atype == ATTR_DATA and attr[9] == 0:
                runs = _parse_data_runs(attr)
                if len(attr) >= 0x38:
                    real = struct.unpack_from("<q", attr, 0x30)[0]
        # 不要对 real 取 min(MftValidDataLength)：$MFT 的 real 通常更大，
        # 取 min 会把尾部后建立的记录截掉。
        self._runs = runs
        self._valid = real or vol.total_clusters * vol.bytes_per_cluster
        if self._log:
            self._log(f"MFT self-runs={len(runs)} valid={self._valid:,}B "
                      f"recs={(self._valid + vol.record_size - 1) // vol.record_size:,}")
        return self._runs, self._valid

    def record(self, idx: int) -> bytes | None:
        runs, _valid = self.runs()
        vol = self._vol
        want = idx * vol.record_size
        seen = 0
        for lcn, cnt in runs:
            span = cnt * vol.bytes_per_cluster
            if seen <= want < seen + span:
                phys = lcn * vol.bytes_per_cluster + (want - seen)
                raw = self._disk.read(phys, vol.record_size)
                if len(raw) < vol.record_size or raw[0:4] != b"FILE":
                    return None
                return _apply_fixup(raw, vol.bytes_per_sector)
            seen += span
        return None

    def stream_runs(self, idx: int) -> tuple[list[tuple[int, int]], int]:
        """取某条记录上无名 $DATA 的完整 runlist，自动跨 $ATTRIBUTE_LIST。"""
        rec = self.record(idx)
        if rec is None:
            return [], 0
        runs, real, ok = _scan_data(rec)
        if ok:
            return runs, real
        for atype, attr in _iter_attrs(rec):
            if atype != ATTR_ATTRIBUTE_LIST:
                continue
            parts: list[tuple[int, list[tuple[int, int]], int]] = []
            for at2, mft_ref, start_vcn in _parse_attr_list(attr):
                if at2 != ATTR_DATA or mft_ref == idx:
                    continue
                ext = self.record(mft_ref)
                if ext is None:
                    continue
                r, rl, ok2 = _scan_data(ext)
                if ok2 and r:
                    parts.append((start_vcn, r, rl))
            parts.sort(key=lambda t: t[0])
            merged: list[tuple[int, int]] = []
            real_size = 0
            for _vcn, r, rl in parts:
                merged.extend(r)
                real_size = max(real_size, rl)
            return merged, real_size
        return [], 0

    def iter_records(self, chunk_recs: int = 8192) -> Iterator[tuple[int, bytes]]:
        """沿 $MFT runlist 遍历全部有效记录，yield (idx, raw_record)。

        用 memoryview + 索引切片，不能 `data = data[rec_size:]`：那会对每块
        8 MB 缓冲做 8192 次整体拷贝，实测 5 分钟毫无进展。
        """
        runs, valid = self.runs()
        vol = self._vol
        rec_size = vol.record_size
        total = (valid + rec_size - 1) // rec_size
        idx = 0
        step = chunk_recs * rec_size
        for lcn, cnt in runs:
            if cnt <= 0 or idx >= total:
                break
            if lcn <= 0:                      # sparse run
                continue
            off = lcn * vol.bytes_per_cluster
            want = cnt * vol.bytes_per_cluster
            pos = 0
            while pos < want and idx < total:
                take = min(step, want - pos)
                chunk = self._disk.read(off + pos, take)
                pos += take
                if not chunk:
                    break
                mv = memoryview(chunk)
                nrec = len(chunk) // rec_size
                for k in range(nrec):
                    if idx >= total:
                        break
                    s = k * rec_size
                    yield idx, bytes(mv[s:s + rec_size])
                    idx += 1
                mv.release()


def _scan_data(rec: bytes) -> tuple[list[tuple[int, int]], int, bool]:
    for atype, attr in _iter_attrs(rec):
        if atype != ATTR_DATA or attr[9] != 0:
            continue
        runs = _parse_data_runs(attr)
        if runs:
            real = struct.unpack_from("<q", attr, 0x30)[0] if len(attr) >= 0x38 else 0
            return runs, real, True
    return [], 0, False


# ---------------------------------------------------------------- pagefile
@dataclass(frozen=True)
class Extent:
    lcn: int
    clusters: int


@dataclass(frozen=True)
class Pagefile:
    """pagefile.sys 在物理盘上的分布。"""

    mft_index: int
    name: str
    extents: tuple[Extent, ...]
    real_bytes: int
    volume: Volume

    @property
    def allocated_bytes(self) -> int:
        return sum(e.clusters for e in self.extents) * self.volume.bytes_per_cluster

    def spans(self) -> list[tuple[int, int]]:
        """[(卷内字节偏移, 长度), ...]，已按磁盘顺序合并相邻段。"""
        bpc = self.volume.bytes_per_cluster
        out: list[tuple[int, int]] = []
        for e in self.extents:
            off = e.lcn * bpc
            size = e.clusters * bpc
            if out and out[-1][0] + out[-1][1] == off:
                prev = out[-1]
                out[-1] = (prev[0], prev[1] + size)
            else:
                out.append((off, size))
        return out

    def summary(self) -> str:
        return (f"{self.name} mft#{self.mft_index} "
                f"extents={len(self.extents)} "
                f"alloc={self.allocated_bytes / 2**30:.2f} GiB "
                f"real={self.real_bytes / 2**30:.2f} GiB")


def _take_hint(mft: "_Mft", volume: Volume, hint_mft: int, *,
               on_log: Log = None) -> Pagefile | None:
    """按缓存的记录号直取 pagefile；**不可信就返回 None**，让调用方走全表扫描。

    只判「解析出 extent」会踩坑：pagefile 被系统重建（改大小、换盘、重装）后，
    同一个记录号会落到另一个完全不相干的小文件上，直取拿到的是一份 0 GiB 的
    extent 区间 —— 扫描自然一条候选都出不来，还白弹一次 UAC（实测踩过：
    pagefile 从 31 GiB 变 30 GiB 后，缓存的 mft#42614 指向了别的文件）。
    """
    rec = mft.record(hint_mft)
    if rec is None:
        if on_log:
            on_log(f"mft#{hint_mft} 读不到记录，回退全表扫描")
        return None
    names = _record_names(rec)
    runs, real = mft.stream_runs(hint_mft)
    pf = _make_pagefile(hint_mft, names[0] if names else "pagefile.sys",
                        runs, real, volume)
    named = any(_is_pagefile_name(n) for n in names)
    sized = bool(runs) and pf.allocated_bytes >= _HINT_MIN_BYTES
    if not (named and sized):
        if on_log:
            why = []
            if not runs:
                why.append("解析不出 extent")
            elif pf.allocated_bytes < _HINT_MIN_BYTES:
                why.append(f"只占 {pf.allocated_bytes / 2**20:.1f} MiB")
            if not named:
                why.append(f"名字是 {names or '（读不到）'}")
            on_log(f"缓存的 mft#{hint_mft} 已不可信（{'；'.join(why)}），"
                   "回退全表扫描")
        return None
    if on_log:
        on_log(f"{pf.summary()}（按 mft#{hint_mft} 直取）")
    return pf


def _record_names(rec: bytes) -> list[str]:
    """记录里的所有 FILE_NAME 值（长名与 8.3 短名各一条）。"""
    return [nm for at, a in _iter_attrs(rec) if at == ATTR_FILE_NAME
            for nm in (_parse_file_name(a),) if nm]


def find_pagefile(
    disk: Disk,
    volume: Volume,
    *,
    hint_mft: int = 0,
    on_log: Log = None,
) -> Pagefile | None:
    """定位 pagefile.sys。

    ``hint_mft`` 给了就先按记录号直接解析（省掉整表扫描，实测 72 s → 0 s）；
    但**直取结果要过校验**（见 ``_take_hint``）—— 不可信就退回全 MFT 扫描。
    扫描时**不遇到第一个就返回** —— MFT 里名字带 "pagefile" 的还有普通文件，
    按 (精确名, 物理占用) 取最大。
    """
    mft = _Mft(disk, volume, on_log=on_log)
    if hint_mft:
        pf = _take_hint(mft, volume, hint_mft, on_log=on_log)
        if pf is not None:
            return pf

    best: tuple | None = None
    scanned = 0
    for idx, rec in mft.iter_records():
        scanned += 1
        if len(rec) < volume.record_size or rec[0:4] != b"FILE":
            continue
        rec = _apply_fixup(rec, volume.bytes_per_sector)
        if not struct.unpack_from("<H", rec, 0x16)[0] & 0x01:   # 未使用的记录
            continue
        names = _record_names(rec)
        if not any(_is_pagefile_name(n) for n in names):
            continue
        runs, real = mft.stream_runs(idx)
        if not runs:
            runs, real, _ok = _scan_data(rec)
        if not runs:
            continue
        strict = any(n.lower() in PAGEFILE_EXACT for n in names)
        phys = sum(c for _l, c in runs) * volume.bytes_per_cluster
        cand = (strict, phys, idx, names, runs, real)
        if on_log:
            on_log(f"候选 {names} mft#{idx} strict={strict} "
                   f"phys={phys / 2**20:,.1f} MB runs={len(runs)}")
        if best is None or cand[0:2] > best[0:2]:
            best = cand
    if best is None:
        if on_log:
            on_log(f"扫描 {scanned:,} 条记录未找到 pagefile")
        return None
    _strict, _phys, idx, names, runs, real = best
    pf = _make_pagefile(idx, names[0], runs, real, volume)
    if on_log:
        on_log(f"{pf.summary()}（扫描 {scanned:,} 条记录）")
    return pf


def _make_pagefile(idx: int, name: str, runs: list[tuple[int, int]],
                   real: int, volume: Volume) -> Pagefile:
    return Pagefile(
        mft_index=idx,
        name=name,
        extents=tuple(Extent(lcn, cnt) for lcn, cnt in runs),
        real_bytes=real,
        volume=volume,
    )
