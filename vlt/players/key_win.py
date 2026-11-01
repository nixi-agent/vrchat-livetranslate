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
import threading
import time
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
SCAN_TIMEOUT = 45.0
MIN_FLOW_PACKETS = 2                           # 一个 flow 至少 2 包才算数
MAX_KEEP = 200_000                             # 候选上限，纯防爆内存

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
    chunks: int = 0
    threads: int = 0
    pagefile_mft: int = 0
    unread_bytes: int = 0          # 应当扫、但读不到（盘打不开/读失败）的字节数
    failed_disks: list[int] = field(default_factory=list)


class SessionCache:
    """按进程身份缓存会话密钥。

    换房间不换 key（实测），所以命中缓存时可以**零 UAC**：拿新流量的样本快验一下，
    通过了就直接用；验不过只说明 key 变了（游戏重启过），丢弃重扫。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ident: Identity | None = None
        self._key: bytes | None = None

    def lookup(self, ident: Identity, samples, *, auth: gcm_win.GcmAuth | None = None
               ) -> bytes | None:
        """缓存命中且能通过样本认证才返回 key。"""
        with self._lock:
            if self._key is None or self._ident != ident:
                return None
            key = self._key
        own = auth is None
        a = auth or gcm_win.GcmAuth()
        try:
            return key if verify_key(key, samples, auth=a) else None
        finally:
            if own:
                a.close()

    def store(self, ident: Identity, key: bytes) -> None:
        with self._lock:
            self._ident, self._key = ident, bytes(key)

    def forget(self) -> None:
        with self._lock:
            self._ident = self._key = None

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


def _plausible(key: bytes) -> bool:
    return len(key) == KEY_LEN and len(set(key)) >= MIN_DISTINCT


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
        self.last_report = 0.0
        # 「该扫但没读到」的字节与盘号：不许把「盘打不开」静默算成「扫过了没命中」，
        # 否则上层会把 0% 覆盖率报成「扫了 X GiB 都不通过 GCM」。
        self.unread = 0
        self.failed_disks: set[int] = set()

    def note_unread(self, nbytes: int, disk_no: int) -> None:
        with self.lock:
            self.unread += int(nbytes)
            self.failed_disks.add(int(disk_no))

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

    def take_chunk(self, nbytes: int) -> None:
        with self.lock:
            self.chunks_done += 1
            self.scanned += nbytes

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


def _scan_buffer(data: bytes, base_off: int, state: _State, auth: gcm_win.GcmAuth,
                 groups: list[tuple[tuple[bytes, bytes], list[bytes]]],
                 deadline: float, aad_kind: str) -> ScanHit | None:
    """扫一块数据：找锚点 → 熵预筛 → 逐候选认证。"""
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
        cand = data[pos + KEY_OFF: pos + KEY_OFF + KEY_LEN]
        if not _plausible(cand):
            continue
        if not state.offer(cand):
            continue
        for flow, pkts in groups:
            n = min(len(pkts), 8)
            if n < MIN_FLOW_PACKETS:
                continue
            if auth.opens_all(cand, pkts[:n], aad_kind) == n:
                hit = ScanHit(key=cand, flow=flow, ok=n, total=len(pkts),
                              offset=base_off + pos + KEY_OFF)
                if state.finish(hit):
                    return hit
                return None
    if hits:
        state.note_hits(hits)
    return None


def _worker(state: _State, tasks: list[tuple[int, int, int]], groups, *,
            deadline: float, aad_kind: str) -> None:
    """扫自己那份任务。任务是 ``(盘号, 盘上偏移, 字节数)``，按盘惰性开句柄。"""
    disks: dict[int, pagefile.Disk] = {}
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
                        # 这块盘打不开：**记下来**（上层要据此报「没扫全」），
                        # 但不拖垮其它盘
                        state.note_unread(size, disk_no)
                        continue
                    disks[disk_no] = disk
                begin = max(((off - OVERLAP) // OVERLAP_ALIGN) * OVERLAP_ALIGN, 0)
                span = off + size - begin
                # 必须走 read_view：Disk.read 每块新建一个 8 MiB 的 ctypes 数组，
                # 实测 8 线程也只有 64 MB/s（45 s 才扫到 2.8 GiB）；复用缓冲后
                # 单线程就能跑到 1 GiB/s 以上。
                try:
                    view = disk.read_view(begin, span)
                except OSError:
                    state.note_unread(size, disk_no)
                    continue
                data = bytes(view)
                view.release()
                del view
                if not data:
                    continue
                state.take_chunk(size)
                if _scan_buffer(data, begin, state, auth, groups,
                                deadline, aad_kind) is not None:
                    return
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
             on_progress=None) -> ScanResult:
    """在 pagefile 里找能把 `samples` 全部解开的 32 字节 key。

    `samples` 是 `{(peerID, challenge): [密文包…]}`（与 `pf_scan.load_samples`、
    `dgram.Datagram` 的口径一致）。

    `layout` 是卷在物理盘上的分段（:class:`pagefile.Layout`）—— 给了它就用它
    把「卷内偏移」翻成 `(盘号, 盘上偏移)`；不给时退回 `disk_no` / `base_offset`
    （单盘单段的老口径，`base_offset=None` 时自己查，非提权就能查）。
    返回的 `ScanResult.hit` 为 None 表示没找到 —— 先确认内存压缩是不是又开了。
    """
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
    nthreads = threads or min(8, max(2, (ctypes.cdll.kernel32.GetActiveProcessorCount(0) or 8)))
    nthreads = max(1, min(nthreads, 16))

    state = _State(total)
    deadline = time.monotonic() + max(1.0, deadline_s)
    t0 = time.monotonic()

    # 任务按块轮转分配：大块在前、交错，避免某个线程捡到全是零的后段
    buckets: list[list[tuple[int, int, int]]] = [[] for _ in range(nthreads)]
    for i, task in enumerate(tasks):
        buckets[i % nthreads].append(task)

    workers = [
        threading.Thread(
            target=_worker, name=f"vlt-keywin-{i}", daemon=True,
            args=(state, buckets[i], groups),
            kwargs=dict(deadline=deadline, aad_kind=aad_kind))
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

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    if state.hit:
        stopped = "found"
    elif state.unread:
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
        needle="q",
        region_bytes=total,
        fingerprints=[key_fingerprint(k) for k in state.candidates[:64]],
        hit=state.hit,
        chunks=state.chunks_done,
        threads=nthreads,
        pagefile_mft=pf.mft_index,
        unread_bytes=state.unread,
        failed_disks=sorted(state.failed_disks),
    )
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
