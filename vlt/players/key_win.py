"""从 pagefile 里找 Photon 的 32 字节会话密钥（Windows，需要管理员）。

## 为什么是 pagefile

VRChat 是 IL2CPP，托管 `byte[]` 在内存里的布局是
`[klass 8][monitor 8][bounds 8][max_length 8][data…]`，`max_length` 是 8 字节小端。
所以「32 字节的数组」就是 `<u64 32>` 紧跟 32 字节 —— 这是唯一的 needle。

读进程内存那条路（`key_linux.py` 的 `process_vm_readv`）在 Windows 上行不通：
EAC 在 `ObRegisterCallbacks` 里剥掉 `PROCESS_VM_READ`，连提权助手都拿不到
（实测：SYSTEM 令牌、镜像名伪装、句柄竞速/窃取全部失败）。

于是改成「把 VRChat 的私有页**驱逐到 pagefile**，再从裸盘读 pagefile」：

1. `Pager.flush()` 把 modified page list 刷下去（`NtSetSystemInformation(80, 3)`）；
2. `trim_process()` 用 `EmptyWorkingSet` 把工作集压到最小，脏页被迫落盘；
3. 裸盘读 pagefile 的全部 extent，按 needle 扫，候选过 GCM 认证。

实测（关掉 Windows 内存压缩之后）：31 GiB pagefile、8 线程、**21–22 秒**、
锚点 100–144 万、候选 1.7–2.1 万，一次命中。

**内存压缩必须关**（`Disable-MMAgent -MemoryCompression` + 重启）。开着的时候
压缩存储回写的是 **XPRESS 压缩块**而不是原始堆页，扫描注定失败（实测 9,109 候选全败）。

## key 的生命周期（实测）

key 是 **VRChat 进程级**的：换房间、换世界、重连 Photon 都不换 key
（实测两个房间、challenge 从 `5cc8f740` → `63a183ad`、自己 peerID 从 `91eb` → `a082`，
旧 key 仍 100% 通过认证），只有**重启游戏**才换。所以 `SessionCache` 按
`Identity(pid, created)` 缓存，换房零 UAC。

## 边界

* 只读 pagefile（裸盘 `GENERIC_READ`），**不碰游戏进程**，不写盘、不改配置；
* 候选 key 只在本进程内存里过一遍 GCM，不进日志；
* 需要管理员仅仅是因为 `\\.\PhysicalDriveN` 的读权限。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time

try:  # numpy 是既有依赖（engine/audio_dsp 都用），仅用于扫描加速
    import numpy as _NP
except Exception:  # pragma: no cover
    _NP = None
from ctypes import wintypes as wt
from dataclasses import dataclass, field

from . import gcm_win, pagefile
from .gcm import key_fingerprint

__all__ = [
    "ANCHOR",
    "CHUNK",
    "Identity",
    "MIN_DISTINCT",
    "OVERLAP",
    "OVERLAP_ALIGN",
    "Pager",
    "ScanHit",
    "ScanResult",
    "SessionCache",
    "process_created",
    "scan_key",
    "trim_process",
]

# ---- 扫描参数 ---------------------------------------------------------
# 与 scripts/pf_key2.py 逐项一致（那份实现已经实测命中过两次）
ANCHOR = b"\x20\x00\x00\x00\x00\x00\x00\x00"   # <u64 32>：IL2CPP byte[] 的 max_length
KEY_LEN = gcm_win.KEY_LEN
KEY_OFF = len(ANCHOR)                          # key 紧跟在锚点后面
MIN_DISTINCT = 20                              # 熵预筛：至少 20 个不同字节值
OVERLAP = KEY_OFF + KEY_LEN - 1                # 39：跨块命中不漏
OVERLAP_ALIGN = 4096                           # 物理盘读必须扇区对齐
CHUNK = 8 << 20                                # 8 MiB/块
#: 每次**真正读进内存**的窗口。峰值内存 ≈ 窗口 × 线程数，所以绝不能用 extent 整段
#: 长度去读：本机 pagefile 36 GiB / 33 个 extent，老代码一次 alloc 36 GiB，
#: 16 GB 机器直接被拖死（实测两次 Kernel-Power 41 硬重启，minidump 都写不出来）。
WINDOW = 32 << 20                              # 32 MiB/窗
#: 扫描总预算。36 GiB pagefile 单线程实测 ~1.2 GiB/s ≈ 30 s，这里留足余量；
#: 原来 45 s 在机械盘/热 pagefile 上必然超时（超时 = 「没找到密钥」的假阴性）。
SCAN_TIMEOUT = 180.0

#: 路标统计采样间隔（每 N 个窗口数一次；1 = 每窗都数）。实测每窗都数会吃掉约 25% 吞吐。
MARKER_SAMPLE_EVERY = 8
#: 并发线程数：read_view 复用缓冲后单线程 ~1 GiB/s（空载）。本机（16 核）实测 8 线程
#: 比 4 线程快约 1.8×；上限再按 os.cpu_count() 夹，可用 VLT_KEY_SCAN_THREADS 覆盖。
#: 别盲目拉满：每线程峰值内存 ≈ 读缓冲 + bytes 拷贝 ≈ 2×WINDOW，且会和游戏抢 CPU/IO。
KEY_SCAN_THREADS = 4
MIN_FLOW_PACKETS = 2                           # 一个 flow 至少 2 包才算数
#: 候选 key 相对锚点的偏移。8 = IL2CPP 数组 header「max_length(u64) 之后紧跟数据」；
#: 12/16/24 兼容别的对齐/包装 —— 本机 34 GiB 扫完、52k 候选全败，先怀疑偏移而不是密钥不存在。
CAND_OFFSETS = (8, 12, 16, 24)
#: 默认只用 KEY_OFF：每个「偏移 × AAD 形态」组合都是一次完整 GCM 认证（≈5 µs），
#: 热路径锚点密度实测约 12.8 万/GiB（bench_real.py），4 偏移 × 3 形态 = 12 次/锚点会把吞吐从 ~1.8 压到
#: ~0.25 GiB/s（单线程，见 bench_scan.py）。本机 063bd98 只用 KEY_OFF + hdr19 就能命中，
#: 故默认收敛；换布局可疑时用 VLT_KEY_SCAN_OFFSETS=all 放开。
def _cand_offsets() -> tuple[int, ...]:
    mode = (os.environ.get("VLT_KEY_SCAN_OFFSETS", "") or "").strip().lower()
    if mode in ("all", "1", "true", "yes", "on"):
        return CAND_OFFSETS
    return (KEY_OFF,)


#: challenge 命中后向两侧各看多少字节（对象字段就在附近）
CHALLENGE_ZONE = 2048
#: 同一个 challenge 模式最多展开几处命中（防病态数据把扫描拖死）
MAX_CHALLENGE_HITS = 256
#: challenge 那条路**不是全盘都跑**：锚点法（needle）在正常机器上也能中，双路全程扫一遍
#: 等于白烧一倍 CPU。这两个值把代价钉死：扫过 GATE 之后才开始跑 challenge，且全盘最多
#: 跑 BUDGET 字节（命中即停）。VLT_KEY_SCAN_CHALLENGE=always 可恢复「全盘双路」旧行为，
#: =only 则反过来：第一遍全盘只跑 challenge，没命中再用锚点法补扫一遍（见 scan_key 末尾）。
CHALLENGE_GATE_BYTES = 4 << 30                 # 4 GiB：needle 先跑，别一上来就双路
CHALLENGE_BUDGET = 8 << 30                     # 8 GiB：challenge 路的总预算（only 模式不受它限制）
MAX_KEEP = 400_000                             # 候选上限，纯防爆内存
# 4 种偏移 × 锚点数量：本机 52k 锚点 → 20.8 万候选，留一倍余量，别让 cap 把后半段吃掉

# ---- Pager ------------------------------------------------------------
SYSTEM_MEMORY_LIST_INFORMATION = 80
MEMORY_FLUSH_COMMAND = 3
PAGE_DELTA = 8192                              # 比基线多这么多页就算"刷干净了"
DRAIN_BUDGET = 5.0
DRAIN_INTERVAL = 0.15

# 非 Windows 上 `ctypes` 没有 `WinDLL`：本模块的**纯逻辑**（卷内偏移 → 扫描任务）
# 必须能在 Linux CI 上 import，否则那部分只有 Windows 才跑得到用例。
try:
    _native = ctypes.WinDLL("ntdll", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
except (OSError, AttributeError):                    # pragma: no cover - 平台分派
    _native = _k32 = _psapi = None

if _native is not None:                              # pragma: no cover - 平台分派
    _NtQuerySystemInformation = _native.NtQuerySystemInformation
    _NtQuerySystemInformation.restype = ctypes.c_long
    _NtQuerySystemInformation.argtypes = [
        wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG),
    ]
    _NtSetSystemInformation = _native.NtSetSystemInformation
    _NtSetSystemInformation.restype = ctypes.c_long
    _NtSetSystemInformation.argtypes = [wt.ULONG, ctypes.c_void_p, wt.ULONG]

    _k32.OpenProcess.restype = wt.HANDLE
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.CloseHandle.restype = wt.BOOL
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.GetProcessTimes.restype = wt.BOOL
    _k32.GetProcessTimes.argtypes = [
        wt.HANDLE, ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
        ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
    ]
    _psapi.EmptyWorkingSet.restype = wt.BOOL
    _psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
else:                                                # pragma: no cover - 平台分派
    _NtQuerySystemInformation = _NtSetSystemInformation = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100


class Pager:
    """逼着 Windows 把脏页写进 pagefile。

    两个动作缺一不可：

    * `flush()` —— 把 modified page list 刷下去（`NtSetSystemInformation(80, 3)`）；
    * `trim()` —— 用 `EmptyWorkingSet` 把目标进程的工作集赶到 standby/modified
      list，脏页才有机会被刷盘。

    只做 flush 不做 trim，VRChat 那 6–7 GB 工作集里的 key 页根本不会动。
    """

    @staticmethod
    def modified_pages() -> int | None:
        """当前 modified page list 的页数（读不到返回 None）。"""
        counts = (ctypes.c_size_t * 22)()
        ret = wt.ULONG(0)
        st = _NtQuerySystemInformation(
            SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(counts),
            ctypes.sizeof(counts), ctypes.byref(ret)) & 0xFFFFFFFF
        if st != 0 or ret.value < 3 * ctypes.sizeof(ctypes.c_size_t):
            return None
        return counts[2]

    @staticmethod
    def flush() -> int:
        """刷一次 modified page list，返回 NTSTATUS（0 = 成功）。"""
        cmd = ctypes.c_ulong(MEMORY_FLUSH_COMMAND)
        return _NtSetSystemInformation(
            SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(cmd),
            ctypes.sizeof(cmd)) & 0xFFFFFFFF

    @classmethod
    def drain(cls, baseline: int | None,
              budget: float = DRAIN_BUDGET) -> tuple[int | None, int]:
        """反复刷直到 modified 页数回到基线附近，返回 (最新页数, 刷的次数)。"""
        t0 = time.monotonic()
        flushes = 0
        warned = False
        while time.monotonic() - t0 < budget:
            st = cls.flush()
            if st != 0:
                if not warned:
                    warned = True          # 只提示一次，继续轮询
            else:
                flushes += 1
            time.sleep(DRAIN_INTERVAL)
            cur = cls.modified_pages()
            if cur is None:
                time.sleep(2.0)
                return None, flushes
            if baseline is not None and cur <= baseline + PAGE_DELTA:
                return cur, flushes
        return cls.modified_pages(), flushes


def trim_process(pid: int) -> int:
    """`EmptyWorkingSet(pid)`，返回 errno（0 = 成功）。

    注意权限：`QUERY_INFORMATION | SET_QUOTA`。只给 `SET_QUOTA` 会 `err=5` 静默失败
    （实测踩过）。
    """
    handle = _k32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
    if not handle:
        return ctypes.get_last_error()
    try:
        ok = _psapi.EmptyWorkingSet(ctypes.c_void_p(handle))
        return 0 if ok else ctypes.get_last_error()
    finally:
        _k32.CloseHandle(handle)


def process_created(pid: int) -> int | None:
    """进程创建时间（FILETIME 合成 64 位，不含时区），用来防 PID 复用。"""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wt.FILETIME() for _ in range(4))
        ok = _k32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        _k32.CloseHandle(handle)


@dataclass(frozen=True)
class Identity:
    """一个游戏进程实例的身份。PID 会被复用，创建时间不会。"""

    pid: int
    created: int

    @classmethod
    def of(cls, pid: int) -> "Identity | None":
        created = process_created(pid)
        if created is None:
            return None
        return cls(pid=pid, created=created)

    def alive(self) -> bool:
        return process_created(self.pid) == self.created


@dataclass(frozen=True)
class ScanHit:
    """命中记录。`key` **不要写日志**。"""

    key: bytes
    flow: tuple[bytes, bytes]      # (peerID, challenge)
    ok: int
    total: int
    offset: int = 0                # 盘上**绝对**偏移（任务在 _phys_tasks 之后）
    kind: str = "hdr19"            # 这条 key 是在哪种 AAD 形态下认证通过的
    key_off: int = KEY_OFF         # 相对锚点的偏移（见 CAND_OFFSETS）

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.key)


@dataclass
class ScanResult:
    """与 `key_linux.ScanResult` 同形状，多几个排查字段。"""

    candidates: list[bytes] = field(default_factory=list)
    scanned_bytes: int = 0
    elapsed_ms: float = 0.0
    needle_hits: int = 0
    regions: int = 0
    stopped: str = "done"          # done | deadline | found | partial
    needle: str = "q"
    region_bytes: int = 0
    fingerprints: list[str] = field(default_factory=list)
    hit: ScanHit | None = None
    aad_kind: str = "hdr19"        # 命中时是哪种 AAD 形态（没命中就是调用方给的默认）
    markers: dict[str, int] = field(default_factory=dict)   # pagefile 里的堆路标计数
    chunks: int = 0
    threads: int = 0
    pagefile_mft: int = 0
    unread_bytes: int = 0          # 应当扫、但读不到（盘打不开/读失败）的字节数
    failed_disks: list[int] = field(default_factory=list)
    # ---- 两路各自的成本（用来定量判断 challenge 到底值不值，VLT_KEY_SCAN_STATS=1 时打印）
    scan_mode: str = "auto"        # 本次实际生效的策略：auto | always | only | off
    challenge_bytes: int = 0       # challenge 路扫过的字节（不占 scanned_bytes）
    needle_seconds: float = 0.0    # 锚点法累计「线程秒」（多线程会大于墙钟时间）
    challenge_seconds: float = 0.0 # challenge 路累计线程秒
    needle_windows: int = 0
    challenge_windows: int = 0
    needle_fallback: bool = False  # only 模式第一遍没命中、补跑的那一遍锚点法


class SessionCache:
    """按进程身份缓存会话密钥。

    换房间不换 key（实测），所以命中缓存时可以**零 UAC**：拿新流量的样本快验一下，
    通过了就直接用；验不过只说明 key 变了（游戏重启过），丢弃重扫。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ident: Identity | None = None
        self._key: bytes | None = None
        self._kind: str = "hdr19"

    def take(self, ident: Identity, samples, *, auth: gcm_win.GcmAuth | None = None
             ) -> tuple[bytes, str] | None:
        """缓存命中且能通过样本认证才返回 `(key, aad_kind)`。

        kind 必须一起回：`scan_key()` 可能在 `hdr7/none` 命中，只回 key 的话
        调用方会按 `hdr19` 复验，把合法密钥判成无效。
        """
        with self._lock:
            if self._key is None or self._ident != ident:
                return None
            key, kind = self._key, self._kind
        own = auth is None
        a = auth or gcm_win.GcmAuth()
        try:
            if verify_key(key, samples, auth=a, aad_kind=kind):
                return key, kind
            return None
        finally:
            if own:
                a.close()

    def lookup(self, ident: Identity, samples, *, auth: gcm_win.GcmAuth | None = None
               ) -> bytes | None:
        """缓存命中且能通过样本认证才返回 key。"""
        hit = self.take(ident, samples, auth=auth)
        return hit[0] if hit is not None else None

    def store(self, ident: Identity, key: bytes, kind: str = "hdr19") -> None:
        with self._lock:
            self._ident, self._key = ident, bytes(key)
            self._kind = kind if kind in RUNTIME_KINDS else "hdr19"

    def forget(self) -> None:
        with self._lock:
            self._ident = self._key = None
            self._kind = "hdr19"

    def peek(self) -> bytes | None:
        with self._lock:
            return self._key


def verify_key(key: bytes, samples, *, auth: gcm_win.GcmAuth | None = None,
               aad_kind: str = "hdr19") -> bool:
    """任一 flow 的**全部**抽样包都通过即算这把 key 有效。

    判据与 `gcm.validate_key` 一致：要求 `>= MIN_FLOW_PACKETS` 个包，
    单个 flow 全通就够（密码学级别的零误报）。
    """
    own = auth is None
    a = auth or gcm_win.GcmAuth()
    try:
        for pkts in samples.values():
            if len(pkts) < MIN_FLOW_PACKETS:
                continue
            if a.opens_all(key, pkts[:8], aad_kind) == len(pkts[:8]):
                return True
        return False
    finally:
        if own:
            a.close()


#: 只试**运行期解包能对应上**的形态（gcm.py 的 LAYOUTS = hdr+nonce / hdr / none）。
#: challenge/peeronce 在这里就算命中了也用不了 —— 解包器没有对应布局，会白扫。
RUNTIME_KINDS = ("hdr19", "hdr7", "none")


def _kind_order(primary: str) -> tuple[str, ...]:
    """AAD 形态顺序：默认只试主形态（老版口径）。VLT_KEY_SCAN_KINDS=all 才依次全试。

    错的 key 在第一个包就失败，但**每种形态仍要各做一次 GCM 认证**（key schedule ≈5 µs），
    热路径上锚点密度一高就是主要成本，所以不做无条件全展开。
    """
    mode = (os.environ.get("VLT_KEY_SCAN_KINDS", "") or "").strip().lower()
    if mode in ("all", "1", "true", "yes", "on"):
        rest = tuple(k for k in RUNTIME_KINDS if k != primary)
        return (primary,) + rest
    return (primary,)


def _plausible(key: bytes) -> bool:
    """熵预筛：32 字节随机 key 至少 20 种不同字节值。

    先用两次 `bytes.count`（C 实现，≈50 ns）做廉价否决：真 pagefile 的锚点密度实测
    12.8 万/GiB（见 bench_real.py），每个锚点都构造一次 `set()` 会把 needle 路压到
    ~0.7 GiB/s；随机 key 里出现 9 个以上 0x00/0xFF 的概率低于 1e-6，挡掉不亏。
    """
    if len(key) != KEY_LEN:
        return False
    if key.count(0) > 8 or key.count(255) > 8:
        return False
    return len(set(key)) >= MIN_DISTINCT


#: pagefile 里「游戏堆」的路标（世界/用户 ID 前缀，一定出现在 VRChat 托管堆里）
_MARKERS = ((b"wrld_", "wrld"), (b"usr_", "usr"), (b"avtr_", "avtr"))


class _State:
    """多线程共享的扫描状态。"""

    def __init__(self, size: int) -> None:
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.hit: ScanHit | None = None
        self.chunks_done = 0
        self.scanned = 0
        self.total_size = size
        self.needle_hits = 0
        self.candidates: list[bytes] = []
        self.seen: set[bytes] = set()
        self.markers: dict[str, int] = {}          # pagefile 里的堆路标计数（见 note_markers）
        self.last_report = 0.0
        # 「该扫但没读到」按**读窗**记账：键是 (盘号, 窗起点)，值是 (窗字节, 读到的字节)。
        # 不许把「盘打不开」静默算成「扫过了没命中」，否则上层会把 0% 覆盖率报成
        # 「扫了 X GiB 都不通过 GCM」。按区域记还顺带解决两件事：补扫那一遍会把同样
        # 的窗再读一次（旧实现直接相加，覆盖率翻倍）；补扫读成功能修掉第一遍的缺口。
        self.windows: dict[tuple[int, int], tuple[int, int]] = {}
        self.failed_disks: set[int] = set()
        #: challenge 第二条路已经跑过多少字节（用来钉住总预算，见 CHALLENGE_BUDGET）
        self.challenge_bytes = 0
        #: 两路各自的累计成本（线程秒 / 窗口数）：多线程下秒数之和会大于墙钟时间，
        #: 这里要的就是「这条路烧掉了多少 CPU」，用来判断 challenge 到底值不值。
        self.needle_seconds = 0.0
        self.challenge_seconds = 0.0
        self.needle_windows = 0
        self.challenge_windows = 0

    def note_window(self, disk_no: int, pos: int, step: int, ok: int) -> None:
        """记一个读窗能读到多少字节（`ok < step` 就是缺口）。

        同一个窗被两遍（锚点法 / challenge 补扫）各读一次也只留一条，取两次里更好的
        那个 `ok` —— 所以 `unread_bytes()` 不可能把同一片盘算两次，补扫成功还能把
        第一遍的缺口消掉。
        """
        if step <= 0:
            return
        step = int(step)
        ok = max(0, min(int(ok), step))
        key = (int(disk_no), int(pos))
        with self.lock:
            prev = self.windows.get(key)
            if prev is None or ok > prev[1]:
                self.windows[key] = (step, ok)
            if ok < step:
                self.failed_disks.add(int(disk_no))

    def unread_bytes(self) -> int:
        """应当扫、却没读到的字节数（按读窗并集，重复那遍不会翻倍）。"""
        with self.lock:
            items = list(self.windows.values())
        return sum(step - ok for step, ok in items if ok < step)

    def merge_windows(self, other) -> None:
        """把另一遍（challenge 补扫）的读窗账并进来，同窗取更大的 `ok`。"""
        with self.lock:
            for key, (step, ok) in other.items():
                step, ok = int(step), int(ok)
                prev = self.windows.get(key)
                if prev is None or ok > prev[1]:
                    self.windows[key] = (step, ok)
                if ok < step:
                    self.failed_disks.add(int(key[0]))

    def note_hits(self, n: int) -> None:
        with self.lock:
            self.needle_hits += n

    def offer(self, cand: bytes) -> bool:
        """登记一个候选；返回 True 表示"这个候选值得做认证"。"""
        with self.lock:
            if cand in self.seen:
                return False
            if len(self.candidates) >= MAX_KEEP:
                return False
            self.seen.add(cand)
            self.candidates.append(cand)
            return True

    def note_markers(self, data: bytes) -> None:
        """数一数页文件里出现了多少游戏堆的「路标」字符串。

        目的只有一个：判断 **VRChat 的堆到底有没有落进 pagefile**。
        世界 ID（`wrld_`）、用户 ID（`usr_`）是它堆里必然存在的字符串；
        扫完 34 GiB 一个都没有 → 再怎么调锚点也找不到密钥（前提就不成立）。
        """
        with self.lock:
            for marker, key in _MARKERS:
                self.markers[key] = self.markers.get(key, 0) + data.count(marker)

    def take_chunk(self, nbytes: int) -> None:
        with self.lock:
            self.chunks_done += 1
            self.scanned += nbytes

    def take_challenge(self, nbytes: int) -> None:
        """登记 challenge 路扫过的字节（不占 scanned：scanned 只算物理读过的量）。"""
        with self.lock:
            self.challenge_bytes += int(nbytes)

    def note_needle(self, seconds: float) -> None:
        """记一次锚点法耗时（线程秒）。"""
        with self.lock:
            self.needle_seconds += float(seconds)
            self.needle_windows += 1

    def note_challenge(self, seconds: float) -> None:
        """记一次 challenge 路耗时（线程秒）。"""
        with self.lock:
            self.challenge_seconds += float(seconds)
            self.challenge_windows += 1

    def finish(self, hit: ScanHit) -> bool:
        """抢占命中。返回 True 表示是我抢到的。"""
        with self.lock:
            if self.hit is not None:
                return False
            self.hit = hit
        self.stop.set()
        return True


def _build_tasks(spans, chunk: int) -> list[tuple[int, int]]:
    """把 pagefile 的 spans 切成 (卷内偏移, 字节数) 任务。

    每块往前多读 `OVERLAP` 字节并对齐到 4096（物理盘 ReadFile 要求扇区对齐），
    这样跨块边界的锚点不会漏。
    """
    tasks: list[tuple[int, int]] = []
    for off, size in spans:
        pos = 0
        while pos < size:
            n = min(chunk, size - pos)
            tasks.append((off + pos, n))
            pos += n
    return tasks


def _phys_tasks(tasks: list[tuple[int, int]],
                layout: "pagefile.Layout") -> list[tuple[int, int, int]]:
    """卷内任务 → ``[(盘号, 盘上偏移, 字节数), ...]``（跨段的在段边界切开）。

    这一步是「系统盘不是 `PhysicalDrive0`」那个 bug 的落脚点：以前任务只带
    卷内偏移，worker 一律开 disk0，于是读的是另一块盘。现在每个任务自带盘号。
    """
    out: list[tuple[int, int, int]] = []
    for off, size in tasks:
        for disk_no, pos, n in layout.resolve(off, size):
            if n > 0:
                out.append((disk_no, pos, n))
    want = sum(int(size) for _off, size in tasks)
    got = sum(n for _d, _p, n in out)
    if got != want:
        # 映射丢了字节 = 这些偏移会被静默跳过。宁可炸，也不要报「扫完了没命中」。
        raise ValueError(
            f"卷→物理盘映射丢了 {want - got} 字节（任务 {want} → 映射 {got}）："
            f"Layout 的分段没覆盖 pagefile 的区间")
    return out


def _scan_near_challenges(data: bytes, base_off: int, state: _State,
                          auth: gcm_win.GcmAuth,
                          groups: list[tuple[tuple[bytes, bytes], list[bytes]]],
                          deadline: float, aad_kind: str) -> ScanHit | None:
    """在「challenge 附近」找 key。

    为什么需要这条：锚点法假设密钥存在 `byte[32]` 托管数组里（长度字段 32 紧挨数据）。
    本机 34 GiB 全扫、25 万候选全败 —— 说明这个假设在这个 VRChat 版本上不成立（或那一页
    压根没落盘）。但 Photon 的连接状态对象里**同时**存着 challenge 与 EncryptionKey，
    两者相距通常在一个对象/几十字节内，所以：

        用报文中真实的 challenge（4 字节随机数）当锚 → 在命中点 ±2 KiB 内按 8 字节对齐
        取 32 字节窗口当候选 → GCM 认证

    challenge 是 4 字节随机数，在 34 GiB 里几乎不会误命中，比「等于 32 的 u64」精确得多。
    """
    zone = CHALLENGE_ZONE
    for flow, pkts in groups:
        peer, chal = flow
        pats = [bytes(chal)]
        if len(peer) and len(chal):
            pats.insert(0, bytes(peer) + b"" + bytes(chal))
        for pat in pats:
            if len(pat) < 4:
                continue
            start = 0
            seen = 0
            while seen < MAX_CHALLENGE_HITS:
                pos = data.find(pat, start)
                if pos < 0:
                    break
                start = pos + 1
                seen += 1
                lo = max(pos - zone, 0)
                hi = min(pos + len(pat) + zone, len(data) - KEY_LEN)
                for off in range(lo - (lo % 8), hi, 8):
                    if time.monotonic() >= deadline:
                        return None
                    cand = data[off:off + KEY_LEN]
                    if len(cand) != KEY_LEN or not _plausible(cand):
                        continue
                    if not state.offer(cand):
                        continue
                    for kind in _kind_order(aad_kind):
                        for flow2, pkts2 in groups:
                            n = min(len(pkts2), 8)
                            if n < MIN_FLOW_PACKETS:
                                continue
                            if auth.opens_all(cand, pkts2[:n], kind) == n:
                                hit = ScanHit(key=cand, flow=flow2, ok=n,
                                              total=len(pkts2),
                                              offset=base_off + off,
                                              kind=kind, key_off=-1)
                                if state.finish(hit):
                                    return hit
                                return None
    return None

def _scan_buffer_np(data: bytes, base_off: int, state: _State, auth: gcm_win.GcmAuth,
                    groups: list[tuple[tuple[bytes, bytes], list[bytes]]],
                    deadline: float, aad_kind: str,
                    offsets: tuple[int, ...], kinds: tuple[str, ...]) -> ScanHit | None:
    """numpy 批量版：一次把整块的候选与熵预筛算完，只对幸存候选走 GCM。

    实测 pagefile 的锚点密度约 12.8 万/GiB，逐锚点做 Python 循环（切片 + count +
    `set()`）会吃掉 0.7~0.9 GiB/s 的 CPU；这里用一次数组运算替代，成本降到 ~1 ms/窗。
    语义与 `_scan_buffer_py` 一致：按锚点升序、每个锚点内按 `offsets` 顺序尝试。
    """
    positions: list[int] = []
    find = data.find
    pos = find(ANCHOR)
    while pos >= 0:
        positions.append(pos)
        pos = find(ANCHOR, pos + 1)
    if not positions:
        return None
    state.note_hits(len(positions))

    n = len(data)
    idx = _NP.asarray(positions, dtype=_NP.int64)
    offs = _NP.asarray(offsets, dtype=_NP.int64)
    starts = idx[:, None] + offs[None, :]
    inside = starts + KEY_LEN <= n
    # 越界的候选用 0 位置占位，最后靠 inside 掩掉
    win = _NP.lib.stride_tricks.sliding_window_view(_NP.frombuffer(data, _NP.uint8), KEY_LEN)
    cand = win[_NP.where(inside, starts, 0)]
    keep = inside & ((cand == 0).sum(-1) <= 8) & ((cand == 255).sum(-1) <= 8)
    rows, cols = _NP.nonzero(keep)
    if not rows.size:
        return None

    for r, c in zip(rows.tolist(), cols.tolist()):
        if time.monotonic() >= deadline:
            return None
        start = int(starts[r, c])
        cand_bytes = data[start:start + KEY_LEN]
        if not _plausible(cand_bytes):
            continue
        if not state.offer(cand_bytes):
            continue
        for flow, pkts in groups:
            n_pkt = min(len(pkts), 8)
            if n_pkt < MIN_FLOW_PACKETS:
                continue
            for kind in kinds:
                if auth.opens_all(cand_bytes, pkts[:n_pkt], kind) == n_pkt:
                    hit = ScanHit(key=cand_bytes, flow=flow, ok=n_pkt,
                                  total=len(pkts), offset=base_off + start,
                                  kind=kind, key_off=int(offs[c]))
                    if state.finish(hit):
                        return hit
                    return None
    return None


def _scan_buffer(data: bytes, base_off: int, state: _State, auth: gcm_win.GcmAuth,
                 groups: list[tuple[tuple[bytes, bytes], list[bytes]]],
                 deadline: float, aad_kind: str) -> ScanHit | None:
    """扫一块数据：找锚点 → 熵预筛 → 逐候选认证。"""
    offsets = _cand_offsets()
    kinds = _kind_order(aad_kind)
    if _NP is not None and len(data) >= (1 << 16):
        return _scan_buffer_np(data, base_off, state, auth, groups, deadline,
                               aad_kind, offsets, kinds)
    return _scan_buffer_py(data, base_off, state, auth, groups, deadline,
                           aad_kind, offsets, kinds)


def _scan_buffer_py(data: bytes, base_off: int, state: _State, auth: gcm_win.GcmAuth,
                    groups: list[tuple[tuple[bytes, bytes], list[bytes]]],
                    deadline: float, aad_kind: str,
                    offsets: tuple[int, ...], kinds: tuple[str, ...]) -> ScanHit | None:
    """扫一块数据：找锚点 → 熵预筛 → 逐候选认证（纯 Python 兜底路径）。"""
    hits = 0
    start = 0
    while True:
        pos = data.find(ANCHOR, start)
        if pos < 0:
            break
        start = pos + 1
        hits += 1
        if hits & 1023 == 0 and time.monotonic() >= deadline:
            return None
        for key_off in offsets:
            if pos + key_off + KEY_LEN > len(data):
                continue
            cand = data[pos + key_off: pos + key_off + KEY_LEN]
            if not _plausible(cand):
                continue
            if not state.offer(cand):
                continue
            for flow, pkts in groups:
                n = min(len(pkts), 8)
                if n < MIN_FLOW_PACKETS:
                    continue
                for kind in kinds:
                    if auth.opens_all(cand, pkts[:n], kind) == n:
                        hit = ScanHit(key=cand, flow=flow, ok=n, total=len(pkts),
                                      offset=base_off + pos + key_off,
                                      kind=kind, key_off=key_off)
                        if state.finish(hit):
                            return hit
                        return None
    if hits:
        state.note_hits(hits)
    return None


def _lower_thread_priority() -> None:
    """按 `VLT_KEY_SCAN_PRIORITY` 调整扫描线程优先级。

    取钥是后台任务，本该让前台优先，但降级会直接吃掉吞吐：本机实测同一份 pagefile，
    BELOW_NORMAL（-1）+ 进程 BELOW_NORMAL 只有 ~0.27 GiB/s，而老版本（不降级）1.0 GiB/s。
    所以默认 ``normal`` 不降；``below`` 用 -1，``lowest`` 用 -2，需要时自行权衡。
    32 MiB 切窗才是防止 Kernel-Power 41 的关键，不依赖降优先级。
    """
    mode = (os.environ.get("VLT_KEY_SCAN_PRIORITY", "") or "normal").strip().lower()
    level = {"below": -1, "lowest": -2}.get(mode)
    if level is None:
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), level)
    except (AttributeError, OSError):
        pass


def _challenge_mode() -> str:
    """challenge 第二条路的启用策略：auto（默认）/ always / off / only。

    ``auto``：第一遍只跑锚点法；整盘扫完没命中才补一遍 challenge（零成功成本）；
    ``always``：每个窗口双路都跑（PR136 的原始行为，慢约 2×，只在排查时用）；
    ``off``：只跑锚点法；
    ``only``：第一遍全盘只跑 challenge（不受 GATE/BUDGET 限制），没命中再用锚点法
    补扫一遍 —— 成功路径省掉 needle 的候选展开，失败路径才付第二遍读盘。
    """
    mode = (os.environ.get("VLT_KEY_SCAN_CHALLENGE", "") or "auto").strip().lower()
    return mode if mode in ("auto", "always", "off", "only") else "auto"


def _challenge_enabled(state: _State, mode: str) -> bool:
    """这一窗口要不要跑 challenge 路。

    ``auto`` 在第一遍**完全不跑** challenge：这条路要按 8 字节步长在命中点附近展开
    候选，实测每窗 30+ ms（本机全盘 34 线程秒 ≈ 20% 墙钟），而锚点法 22 GiB 就命中了。
    改为「扫完没命中再补一遍」（见 `scan_key` 尾部的第二遍），成功路径零成本，
    needle 彻底失效的机器仍会走到 challenge。
    """
    if mode == "off":
        return False
    if mode in ("always", "only", "chal"):
        # only/chal 是「challenge 主路」，不能被预算掐掉，否则等于没扫。
        return True
    return False


def _stats_on() -> bool:
    """`VLT_KEY_SCAN_STATS=1` 时把两路成本打一行（默认关，避免刷日志）。"""
    return (os.environ.get("VLT_KEY_SCAN_STATS", "").strip().lower()
            in ("1", "true", "yes", "on"))


def _windows_of(off: int, size: int):
    """按 `_worker` 同一套算法枚举一个任务的读窗，产出 ``(窗起点, 本任务字节数)``。

    「盘打不开」时不能整段记一笔就完事：补扫那一遍会用同样的窗边界去读，两边键
    必须对得上才能去重。这里不读盘，只做与 `_worker` 完全一样的回退对齐与切分；
    窗头退到上一个任务的重叠区（`off - OVERLAP` 对齐）时，那一段不属于本任务，
    计数字节要裁掉 —— 否则 unread 会比 region_bytes 还大。
    """
    begin = max(((int(off) - OVERLAP) // OVERLAP_ALIGN) * OVERLAP_ALIGN, 0)
    end = int(off) + int(size)
    pos = begin
    while pos < end:
        step = min(WINDOW, end - pos)
        lo = max(pos, int(off))
        hi = min(pos + step, end)
        yield pos, hi - lo
        pos += step


def _worker(state: _State, tasks: list[tuple[int, int, int]], groups, *,
            deadline: float, aad_kind: str, mode: str | None = None) -> None:
    """扫自己那份任务。任务是 ``(盘号, 盘上偏移, 字节数)``，按盘惰性开句柄。

    ``mode`` 为空时回落到环境变量策略（见 `_challenge_mode`）；``scan_key`` 会显式
    传入，好让 only/chal 模式的第一遍真的只跑 challenge。
    """
    _lower_thread_priority()
    mode = mode or _challenge_mode()
    needle_on = mode not in ("only", "chal")
    disks: dict[int, pagefile.Disk] = {}
    marker_windows = 0
    try:
        with gcm_win.GcmAuth() as auth:
            for disk_no, off, size in tasks:
                if state.stop.is_set() or time.monotonic() >= deadline:
                    return
                disk = disks.get(disk_no)
                if disk is None:
                    try:
                        disk = pagefile.Disk.open(disk_no, 0)
                    except OSError:
                        # 盘打不开：按窗算术登记缺口（不读盘，成本可忽略）。键与真正
                        # 读过的窗一致，所以补扫那一遍再来一次也只算一次。
                        for w_pos, w_step in _windows_of(off, size):
                            state.note_window(disk_no, w_pos, w_step, 0)
                        continue
                    disks[disk_no] = disk
                # 从 off 往前退到 OVERLAP 对齐的位置，好让跨任务边界那 39 字节也能读到
                begin = max(((int(off) - OVERLAP) // OVERLAP_ALIGN) * OVERLAP_ALIGN, 0)
                pos, end = begin, int(off) + int(size)
                while pos < end:
                    step = min(WINDOW, end - pos)
                    # 窗头那段（off 往前退的重叠区）不属于本任务：只按 [off, end)
                    # 内的字节记账，否则 unread 会比 region_bytes 还大。键仍用 pos，
                    # 与 `_windows_of()` 的「盘打不开」账完全对得上。
                    lo = max(pos, int(off))
                    hi = min(pos + step, end)
                    n = min(step + OVERLAP, end - pos)     # 多读 39 字节防跨窗漏 key
                    # 物理盘 ReadFile 的长度必须按扇区对齐（不对齐是 err=87）：跨窗那
                    # 39 字节不满一个扇区，先把读取长度向上取整，再切回 n 字节。
                    align = OVERLAP_ALIGN
                    read_n = n if n % align == 0 else n + (align - n % align)
                    try:
                        view = disk.read_view(pos, read_n)
                    except OSError:
                        state.note_window(disk_no, pos, hi - lo, 0)
                        break
                    try:
                        take = min(len(view), n)
                        data = bytes(view[:take])
                    finally:
                        # 必须放 finally：`bytes()` 抛 MemoryError 时漏掉 release()
                        # 会让 BufferError 顶掉原始异常，线程静默死掉不留任何痕迹。
                        view.release()
                    state.note_window(disk_no, pos, hi - lo, min(pos + take, hi) - lo)
                    if data:
                        # 路标只用来判断「游戏堆有没有落进 pagefile」，不需要每窗都数：
                        # bytes.count 三次实测吃掉约 25% 吞吐（0.736→0.488 GiB/s）。
                        marker_windows += 1
                        if marker_windows % MARKER_SAMPLE_EVERY == 0:
                            state.note_markers(data)
                        state.take_chunk(min(len(data), step))
                        if needle_on:
                            t_needle = time.perf_counter()
                            hit = _scan_buffer(data, pos, state, auth, groups,
                                               deadline, aad_kind)
                            state.note_needle(time.perf_counter() - t_needle)
                            if hit is not None:
                                return
                        if _challenge_enabled(state, mode):
                            state.take_challenge(min(len(data), step))
                            t_chal = time.perf_counter()
                            hit = _scan_near_challenges(data, pos, state, auth, groups,
                                                        deadline, aad_kind)
                            state.note_challenge(time.perf_counter() - t_chal)
                            if hit is not None:
                                return
                    pos += step
    finally:
        for disk in disks.values():
            try:
                disk.close()
            except OSError:
                pass

def _layout_from_one_extent(spans, disk_no: int, base_offset: int) -> pagefile.Layout:
    """老口径（单盘单段）合成一个 Layout，长度要**覆盖到 pagefile 的最远偏移**。

    pagefile 的 spans 是卷内偏移（可能从卷中间的几 GiB 才开始），所以不能拿
    「pagefile 总大小」当 extent 长度 —— 那样高位偏移全落到段外，任务数变 0，
    扫描静默什么都不做。
    """
    end = max((int(off) + int(size) for off, size in spans), default=0)
    return pagefile.Layout(
        (pagefile.DiskExtent(int(disk_no), int(base_offset), end),))


def scan_key(pf: pagefile.Pagefile, samples, *,
             disk_no: int = 0, base_offset: int | None = None,
             layout: pagefile.Layout | None = None,
             threads: int = 0, chunk: int = CHUNK,
             deadline_s: float = SCAN_TIMEOUT, aad_kind: str = "hdr19",
             mode: str = "", _needle_fallback: bool = False,
             on_progress=None) -> ScanResult:
    """在 pagefile 里找能把 `samples` 全部解开的 32 字节 key。

    `samples` 是 `{(peerID, challenge): [密文包…]}`（与 `pf_scan.load_samples`、
    `dgram.Datagram` 的口径一致）。

    `layout` 是卷在物理盘上的分段（:class:`pagefile.Layout`）—— 给了它就用它
    把「卷内偏移」翻成 `(盘号, 盘上偏移)`；不给时退回 `disk_no` / `base_offset`
    （单盘单段的老口径，`base_offset=None` 时自己查，非提权就能查）。
    返回的 `ScanResult.hit` 为 None 表示没找到 —— 先确认内存压缩是不是又开了。

    `mode` 为 auto/always/off/only，空串走环境变量（`_challenge_mode`）。only 模式
    先全盘只跑 challenge，没命中再**自动补一遍**锚点法（`needle_fallback=True`）。
    """
    mode = (mode or _challenge_mode()).strip().lower()
    if mode not in ("auto", "always", "off", "only"):
        mode = "auto"
    groups: list[tuple[tuple[bytes, bytes], list[bytes]]] = []
    for flow, pkts in sorted(samples.items(), key=lambda kv: -len(kv[1])):
        good = [p for p in pkts if len(p) >= gcm_win.MIN_PACKET and p[2] == 0x01]
        if len(good) >= MIN_FLOW_PACKETS:
            groups.append((flow, good))
    if not groups:
        raise ValueError("没有可用的 Photon 加密包样本（需要至少一个 flow 的 2 包）")

    spans = pf.spans()
    total = sum(n for _off, n in spans)
    if layout is None:
        if base_offset is None:
            got = pagefile.base_offset_for("C")
            if got is None:
                raise OSError("拿不到 C: 的分区起始偏移（pagefile.base_offset_for 返回 None）")
            base_offset = got
        layout = _layout_from_one_extent(spans, disk_no, int(base_offset))

    tasks = _phys_tasks(_build_tasks(spans, chunk), layout)
    if not threads:
        try:
            threads = int(os.environ.get("VLT_KEY_SCAN_THREADS", "") or KEY_SCAN_THREADS)
        except ValueError:
            threads = KEY_SCAN_THREADS
    nthreads = max(1, min(int(threads or KEY_SCAN_THREADS), max(1, os.cpu_count() or 4)))

    state = _State(total)
    try:
        deadline_s = float(os.environ.get("VLT_KEY_SCAN_TIMEOUT", "") or deadline_s)
    except ValueError:
        pass
    deadline = time.monotonic() + max(1.0, deadline_s)
    t0 = time.monotonic()

    # 任务轮转分配：交错到各线程，避免某个线程捡到全是零的后段
    buckets: list[list[tuple[int, int, int]]] = [[] for _ in range(nthreads)]
    for i, task in enumerate(tasks):
        buckets[i % nthreads].append(task)

    workers = [
        threading.Thread(
            target=_worker, name=f"vlt-keywin-{i}", daemon=True,
            args=(state, buckets[i], groups),
            kwargs=dict(deadline=deadline, aad_kind=aad_kind, mode=mode))
        for i in range(nthreads)
    ]
    for w in workers:
        w.start()

    try:
        while any(w.is_alive() for w in workers):
            for w in workers:
                w.join(timeout=0.2)
            if on_progress is not None:
                now = time.monotonic()
                if now - state.last_report >= 0.5:
                    state.last_report = now
                    try:
                        on_progress(state.scanned, now - t0)
                    except Exception:                   # noqa: BLE001 - 回调不影响扫描
                        pass
            if state.stop.is_set():
                break
    finally:
        state.stop.set()
        for w in workers:
            w.join(timeout=1.0)

    # ── 第二遍：第一遍锚点法整盘没命中，再单独跑一遍 challenge ──────────────
    # 本机实测：challenge 路每窗 30+ ms，全盘 34 线程秒、约占 20% 墙钟，而锚点法
    # 22 GiB 就命中了 —— 双路并跑等于给每次冷启动白付这笔钱。改成「失败才补扫」
    # 后，成功路径一分钱不花；needle 结构对不上（其他版本/其他布局）的机器仍然
    # 会走到 challenge，只是多付一次读盘。
    if state.hit is None and mode == "auto":
        state2 = _State(total)
        # 补扫复用同一个绝对 deadline：旧实现又拿一份完整 deadline，两遍墙钟可能
        # 接近 2×，直接超过客户端 keyd 的 DEFAULT_LIFETIME(240s)，于是客户端先报
        # 「提权子进程没有交出结果就断开了」，而子进程还在持锁读盘、下一轮只能干等。
        deadline2 = deadline
        workers2 = [
            threading.Thread(
                target=_worker, name=f"vlt-keychal-{i}", daemon=True,
                args=(state2, buckets[i], groups),
                kwargs=dict(deadline=deadline2, aad_kind=aad_kind, mode="chal"))
            for i in range(nthreads)
        ]
        for w in workers2:
            w.start()
        try:
            while any(w.is_alive() for w in workers2):
                for w in workers2:
                    w.join(timeout=0.2)
                if on_progress is not None:
                    now = time.monotonic()
                    if now - state.last_report >= 0.5:
                        state.last_report = now
                        try:
                            on_progress(state.scanned + state2.scanned, now - t0)
                        except Exception:               # noqa: BLE001 - 回调不影响扫描
                            pass
                if state2.stop.is_set():
                    break
        finally:
            state2.stop.set()
            for w in workers2:
                w.join(timeout=1.0)
        state.challenge_bytes += state2.challenge_bytes
        state.challenge_seconds += state2.challenge_seconds
        state.challenge_windows += state2.challenge_windows
        # 读窗账按区域合并（不是相加）：同一片盘被两遍各读一次也只算一次缺口，
        # 补扫读成功能把第一遍的缺口修掉。
        state.merge_windows(state2.windows)
        state.failed_disks |= state2.failed_disks
        if state2.hit is not None:
            state.hit = state2.hit

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    if state.hit:
        stopped = "found"
    elif state.unread_bytes():
        stopped = "partial"          # 有字节根本没读到，不许报成「扫完了没命中」
    elif time.monotonic() >= deadline:
        stopped = "deadline"
    else:
        stopped = "done"
    result = ScanResult(
        candidates=list(state.candidates),
        scanned_bytes=state.scanned,
        elapsed_ms=elapsed_ms,
        needle_hits=state.needle_hits,
        regions=len(spans),
        stopped=stopped,
        aad_kind=(state.hit.kind if state.hit else aad_kind),
        markers=dict(state.markers),
        needle="q",
        region_bytes=total,
        fingerprints=[key_fingerprint(k) for k in state.candidates[:64]],
        hit=state.hit,
        chunks=state.chunks_done,
        threads=nthreads,
        pagefile_mft=pf.mft_index,
        unread_bytes=state.unread_bytes(),
        failed_disks=sorted(state.failed_disks),
        scan_mode=mode,
        challenge_bytes=state.challenge_bytes,
        needle_seconds=state.needle_seconds,
        challenge_seconds=state.challenge_seconds,
        needle_windows=state.needle_windows,
        challenge_windows=state.challenge_windows,
        needle_fallback=_needle_fallback,
    )
    if _stats_on():
        print(
            f"[key_win] scan mode={mode} fallback={_needle_fallback} "
            f"hit={'yes' if result.hit else 'no'} stopped={result.stopped} | "
            f"read={result.scanned_bytes / 2**30:.1f} GiB "
            f"wall={result.elapsed_ms / 1000.0:.1f}s | "
            f"needle={result.needle_seconds:.1f}s/{result.needle_windows}win "
            f"({result.needle_hits} anchors, {len(result.candidates)} cand) | "
            f"challenge={result.challenge_seconds:.1f}s/{result.challenge_windows}win "
            f"({result.challenge_bytes / 2**30:.1f} GiB)",
            flush=True,
        )

    # only 模式：第一遍全盘只有 challenge 路。没命中就补一遍锚点法 —— 失败路径才付
    # 第二遍读盘（本机 34 GiB ≈ 34 s），成功路径省掉 needle 的候选展开。
    if (mode == "only" and result.hit is None and result.stopped != "deadline"
            and time.monotonic() < deadline):
        if _stats_on():
            print("[key_win] only 模式未命中，改用锚点法补扫一遍", flush=True)
        return scan_key(pf, samples, disk_no=disk_no, base_offset=base_offset,
                        layout=layout, threads=nthreads, chunk=chunk,
                        deadline_s=max(1.0, deadline - time.monotonic()),
                        aad_kind=aad_kind, mode="off", _needle_fallback=True,
                        on_progress=on_progress)
    return result


def prepare(pid: int, *, passes: int = 2, on_log=None) -> dict:
    """把目标进程的脏页逼进 pagefile，返回统计（`trim` 的 err 用来判权限）。

    步骤与 `scripts/pf_key2.py` 一致：记基线 → 每遍 (flush 轮询 + EmptyWorkingSet)。
    返回的 `error` 非 0 基本就是没提权。
    """
    log = on_log or (lambda _m: None)
    baseline = Pager.modified_pages()
    log(f"modified 页基线 = {baseline if baseline is not None else '读不到'}")
    stats = {"baseline": baseline, "trim_err": 0, "flushes": 0, "passes": passes}
    for i in range(max(1, passes)):
        cur, flushes = Pager.drain(baseline)
        stats["flushes"] += flushes
        err = trim_process(pid)
        stats["trim_err"] = err
        log(f"第 {i + 1} 遍 trim: err={err} modified={cur}（基线 {baseline}）")
        if err:
            break
        time.sleep(0.2)
    return stats
