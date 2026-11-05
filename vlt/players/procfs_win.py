"""Windows 端口：用 Toolhelp32 / IP Helper 取代 `/proc`。

与 `procfs.py` **同形**（同名函数、同一套 dataclass 实例），所以调用方只要
`Proc = procfs_win if os.name == "nt" else procfs` 就能整体换掉，不必改调用点。

## 与 Linux 的三处硬差异（每一处都实测过）

1. **没有 maps** —— `MapSummary` 只能填"从名字推出来的"那几个布尔，`maps_ok` 恒假。
   判断"这是不是游戏"只能靠可执行文件名：
   `CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)` 对 VRChat 直接返回
   `INVALID_HANDLE_VALUE` + `GetLastError() == 5`（EAC 的保护，与它剥掉
   `PROCESS_VM_READ` 同源），所以拿不到 `GameAssembly.dll` 这条最硬的证据。
2. **`starttime` 换成 FILETIME**（100 ns since 1601）。语义不变：同一进程内稳定，
   用来识别 pid 复用 —— 游戏重启后 pid 可能相同，`starttime` 一定不同。
3. **拿不到 UDP 远端端点** —— NT 不维护 UDP 连接状态表，`GetExtendedUdpTable`
   只有本端地址。`udp_endpoints()` 因此 `remote_*=None`：`leg` 里那句
   "游戏当前连的远端端口"提示在 Windows 上会是空串，但那只是失败提示文案；
   远端端口从抓到的 `Datagram.src/dst` 读更准。

## 纪律

- **只读**：只有快照与查询，不碰游戏进程内存，不发任何写操作。
- `pick_host` **不能照抄 `procfs` 那份**：它的排序键全来自 maps，而这里
  `maps_ok` 恒假，照抄会永远返回 `None`。
"""
from __future__ import annotations

import ctypes as C
import time
from ctypes import wintypes as W
from typing import Iterable

from . import dgram
from .procfs import (
    EAC_MARKERS,
    GAME_MARKERS,
    MapSummary,
    ProcInfo,
    UdpEndpoint,
    human_bytes,
)

__all__ = [
    "EAC_MARKERS",
    "GAME_MARKERS",
    "MapSummary",
    "ProcInfo",
    "UdpEndpoint",
    "collect",
    "discover",
    "find_eac",
    "find_vrchat",
    "human_bytes",
    "iter_processes",
    "pick_host",
    "process_created",
    "udp_endpoints",
    "udp_ports",
]

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260

#: 名字**正好**是它才算游戏本体。`start_protected_game.exe` 是 Steam 的启动器，
#: 它会把真正的游戏拉起来然后自己留着 —— 选错就会去扫一个空壳进程。
_GAME_EXE = ("vrchat.exe",)

_k32 = C.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
_k32.CreateToolhelp32Snapshot.restype = W.HANDLE
_k32.Process32FirstW.argtypes = [W.HANDLE, C.c_void_p]
_k32.Process32FirstW.restype = W.BOOL
_k32.Process32NextW.argtypes = [W.HANDLE, C.c_void_p]
_k32.Process32NextW.restype = W.BOOL
_k32.CloseHandle.argtypes = [W.HANDLE]
_k32.CloseHandle.restype = W.BOOL
_k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
_k32.OpenProcess.restype = W.HANDLE
_k32.GetProcessTimes.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p,
                                 C.c_void_p, C.c_void_p]
_k32.GetProcessTimes.restype = W.BOOL
_k32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR,
                                           C.POINTER(W.DWORD)]
_k32.QueryFullProcessImageNameW.restype = W.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _PROCESSENTRY32W(C.Structure):
    _fields_ = [
        ("dwSize", W.DWORD),
        ("cntUsage", W.DWORD),
        ("th32ProcessID", W.DWORD),
        ("th32DefaultHeapID", C.c_void_p),
        ("th32ModuleID", W.DWORD),
        ("cntThreads", W.DWORD),
        ("th32ParentProcessID", W.DWORD),
        ("pcPriClassBase", C.c_long),
        ("dwFlags", W.DWORD),
        ("szExeFile", W.WCHAR * MAX_PATH),
    ]


class _FILETIME(C.Structure):
    _fields_ = [("dwLowDateTime", W.DWORD), ("dwHighDateTime", W.DWORD)]


_SNAP_WARNED = False
_UDP_FALLBACK_LOGGED = False
_UDP_WARNED = False
#: 快照本身拿不到（不是「没找到 VRChat」）。留着做诊断/日志措辞用，但
#: `find_vrchat()` 不再拿它当「要不要兜底」的开关 —— 打包版见过快照成功却
#: 只有几十条用户态进程的形态，那时它是 False，兜底就永远不跑。
_SNAP_BROKEN = False
#: UDP 反查的最小间隔（秒）。它是兜底而非主路径，游戏没开时也会被调用，
#: 限制到每秒一次就够：`_run()` 的轮询本身是 2 s 一轮。
_UDP_MIN_INTERVAL_S = 1.0
_last_udp_ts = 0.0
#: 最近一次**成功**的结果 + 它的时间戳。给 `find_vrchat()` 兜一层宽限：
#: 打包版里「快照被过滤 + 兜底被节流」会让它每隔一轮吐一次空表，上层
#: `PlayerWarmup._run()` 会把这次空当成「游戏退了」→ 清 host、断抓包、
#: 再选一次 host（`_key` 被清、`_next_scan` 归零）→ 立刻重新提权取钥，
#: 真机上就是「反复弹 UAC + 状态反复跳」。宽限期内只要进程还活着就不吐空。
_LAST_GOOD_TTL_S = 10.0
_last_good: list[ProcInfo] = []
_last_good_ts = 0.0


def _warn_snapshot_once(detail: str) -> None:
    """快照拿不到时**只喊一次**。

    这条以前是静默 `return []`：打包成 exe 后它一直返回空表，界面就永远停在
    「等待 VRChat 进程…」，日志里一个字都没有 —— 排查全靠猜。现在至少留下
    `GetLastError`，并且 `find_vrchat()` 会改走 UDP 表反查兜底。
    """
    global _SNAP_WARNED
    if _SNAP_WARNED:
        return
    _SNAP_WARNED = True
    print(f"[procfs][warn] 进程枚举不可用：{detail}（改用 UDP 表反查）", flush=True)


def _warn_udp_once(detail: str) -> None:
    """UDP 反查自己失败时的告警。

    不能复用 `_warn_snapshot_once`：那句话的主语是「进程枚举不可用」，而这里
    枚举是好的、坏的是兜底路径 —— 文案错了会把人带偏。
    """
    global _UDP_WARNED
    if _UDP_WARNED:
        return
    _UDP_WARNED = True
    print(f"[procfs][warn] UDP 表反查也失败了：{detail}（看不到 VRChat 进程）",
          flush=True)


def iter_processes() -> list[tuple[int, str, int]]:
    """全系统进程快照 `[(pid, exe 名, 父 pid)]`。

    `TH32CS_SNAPPROCESS` 拿的是**名字**，不碰任何进程内部 —— 这也正是
    `TH32CS_SNAPMODULE` 被 EAC 拒绝时唯一还能用的那份信息。
    """
    global _SNAP_BROKEN
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) == 0xFFFFFFFFFFFFFFFF:
        _SNAP_BROKEN = True
        _warn_snapshot_once(
            f"CreateToolhelp32Snapshot 失败 err={C.get_last_error()}")
        return []
    out: list[tuple[int, str, int]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = C.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, C.byref(entry))
        if not ok:
            _SNAP_BROKEN = True
            _warn_snapshot_once(
                f"Process32FirstW 失败 err={C.get_last_error()}")
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile,
                        int(entry.th32ParentProcessID)))
            ok = _k32.Process32NextW(snap, C.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    if ok or out:
        # 这次快照是好的：清掉标记，下次找不到 VRChat 就只是「游戏没开」，
        # 不该再去拉 UDP 表。
        _SNAP_BROKEN = False
    return out


def process_created(pid: int) -> int | None:
    """进程创建时间（FILETIME，100 ns since 1601）；拿不到返回 None。

    这是 Windows 的 `starttime` 对位物：只要在**同一进程**里问，值就恒定，
    所以能用 `(pid, created)` 这对组合识别 pid 复用。
    """
    if pid <= 0:
        return None
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        creation, exit_t, kernel, user = (_FILETIME(), _FILETIME(),
                                          _FILETIME(), _FILETIME())
        if not _k32.GetProcessTimes(h, C.byref(creation), C.byref(exit_t),
                                    C.byref(kernel), C.byref(user)):
            return None
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    finally:
        _k32.CloseHandle(h)


def image_path(pid: int) -> str | None:
    """进程可执行文件的完整路径；拿不到返回 None。

    只要求 `PROCESS_QUERY_LIMITED_INFORMATION`，EAC 不剥这个权限（对比
    `PROCESS_VM_READ` 会被剥、`TH32CS_SNAPMODULE` 直接 `err=5`），所以这条
    路径在 VRChat 上可用。用途：定位游戏自带的 `opus.dll`（`VRChat_Data/
    Plugins/x86_64`）—— 它和 VRChat 编解码自己语音用的是同一份库。
    """
    if pid <= 0:
        return None
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = C.create_unicode_buffer(MAX_PATH * 4)
        size = W.DWORD(len(buf))
        if not _k32.QueryFullProcessImageNameW(h, 0, buf, C.byref(size)):
            return None
        return buf.value or None
    finally:
        _k32.CloseHandle(h)


def collect(pid: int, *, exe: str = "", with_maps: bool = False) -> ProcInfo:
    """拼一个 `ProcInfo`。

    `with_maps` 只是与 `procfs.collect` 保持签名一致 —— Windows 上无论传什么都
    读不到 maps（见模块 docstring 第 1 条），`summary` 只反映从名字推得的事实。
    """
    name = exe or _exe_of(pid)
    low = name.lower()
    summary = MapSummary(
        has_vrchat_exe=low in _GAME_EXE,
        has_gameassembly=False,  # 拿不到模块表（EAC 拒绝）
        has_eac=any(m in low for m in EAC_MARKERS),
    )
    created = process_created(pid)
    return ProcInfo(
        pid=pid,
        comm=name,
        cmdline=(),          # 读命令行要读 PEB，等同读内存，不做
        starttime=created if created is not None else 0,
        uid=None,
        summary=summary,
        dumpable=None,       # NT 没有 dumpable；等价信息是能否 PROCESS_VM_READ
        tracer_pid=None,
        maps_ok=False,
        error="" if created is not None else "读不到进程创建时间",
    )


def _exe_of(pid: int) -> str:
    for qpid, exe, _ppid in iter_processes():
        if qpid == pid:
            return exe
    return ""


def discover(markers: tuple[str, ...], *, limit: int = 400) -> list[ProcInfo]:
    """扫全系统进程，返回可执行文件名命中标记的那些。

    Linux 版还会拿 `cmdline` 做粗筛；Windows 只能比 `szExeFile`，因为命令行要读
    PEB（等价于读内存，EAC 会拦，我们也不做）。
    """
    wanted = tuple(m.lower() for m in markers)
    found: list[ProcInfo] = []
    for pid, exe, _ppid in iter_processes():
        if len(found) >= limit:
            break
        low = exe.lower()
        if not any(m in low for m in wanted):
            continue
        found.append(collect(pid, exe=exe))
    return found


def find_vrchat() -> list[ProcInfo]:
    """所有看起来像 VRChat 的进程（游戏本体 + 受保护启动器）。

    两条路：正常走 Toolhelp 快照；**快照里看不到 VRChat 时改走 UDP 表反查** ——
    打包成 exe 后见过两种形态：`CreateToolhelp32Snapshot` 直接失败，以及**快照
    成功但列表被过滤成几十条用户态进程**（`_SNAP_BROKEN` 还是 False）。后一种
    以前会在「快照是好的、只是没有 VRChat」这里直接 `return []`，于是每轮都
    「等待 VRChat 进程…」，`[procfs]` 一条日志都没有，看着像卡死。

    所以不再按「快照是否失败」分流，而是按**结果**分流：拿不到 VRChat 就查一遍
    UDP 表（`GetExtendedUdpTable` + `QueryFullProcessImageNameW`，后者只要
    `PROCESS_QUERY_LIMITED_INFORMATION`，EAC 不剥）。开销是几十个 pid 的
    `OpenProcess`，几毫秒，这里再用 `_UDP_MIN_INTERVAL_S` 限一次频率，
    游戏真没开时也不至于每轮都扫。

    但「这一轮没查到」和「游戏退了」是两件事：节流窗口正好落在一轮轮询上，
    或者兜底那次 `image_path` 撞上进程刚建的瞬间，都会返回空表。上层
    `PlayerWarmup._run()` 只看返回值，于是把空表当成「游戏退了」→ 断抓包、
    清 host，再接上时又走一遍 `_select_new_host()`（清 `_key`、`_next_scan`
    归零）→ 立刻重新提权取钥，真机上就是「反复弹 UAC + 状态反复跳」。
    所以这里对**最近一次成功结果**给 `_LAST_GOOD_TTL_S` 的宽限：只要那些进程
    还活着（`(pid, 创建时间)` 一致），就别吐空。
    """
    global _last_udp_ts, _last_good, _last_good_ts
    procs = discover(GAME_MARKERS)
    if not procs:
        now = time.monotonic()
        if now - _last_udp_ts >= _UDP_MIN_INTERVAL_S:
            _last_udp_ts = now
            procs = _discover_by_udp(GAME_MARKERS)
    if procs:
        _last_good = procs
        _last_good_ts = time.monotonic()
        return procs
    if (_last_good and time.monotonic() - _last_good_ts <= _LAST_GOOD_TTL_S
            and _alive_same(_last_good)):
        return list(_last_good)
    return []


def _alive_same(procs: list[ProcInfo]) -> bool:
    """这些进程是否还活着、且还是同一批（用 `(pid, 创建时间)` 认）。

    pid 会被复用，所以只比 pid 不够：游戏退出后新进程占了同一个 pid，
    宽限期就会一直把死进程当成 VRChat。
    """
    for proc in procs:
        created = process_created(proc.pid)
        if created is None:
            return False
        if proc.starttime and created != proc.starttime:
            return False
    return True


def _discover_by_udp(markers: tuple[str, ...]) -> list[ProcInfo]:
    """用 UDP 表里出现的 pid 反查 exe 名（不依赖 Toolhelp）。

    游戏无论直连还是走加速器，都必然至少有一个 UDP 套接字，所以「有 UDP 端点的
    pid」是它的超集；再逐个 `image_path` 比名字即可。开销：本机约几十个 pid，
    每次轮询几毫秒。
    """
    global _UDP_FALLBACK_LOGGED
    wanted = tuple(m.lower() for m in markers)
    pids: dict[int, str] = {}
    try:
        rows = dgram.udp_table()
    except Exception as exc:                     # noqa: BLE001 - 兜底路径不许抛
        _warn_udp_once(str(exc) or "GetExtendedUdpTable 调用失败")
        return []
    for _addr, _port, owner in rows:
        if owner > 0:
            pids.setdefault(owner, "")
    found: list[ProcInfo] = []
    for pid in pids:
        path = image_path(pid)
        if not path:
            continue
        name = path.rsplit("\\", 1)[-1]
        low = name.lower()
        if not any(m in low for m in wanted):
            continue
        found.append(collect(pid, exe=name))
    if found and not _UDP_FALLBACK_LOGGED:
        # 只喊一次：以后看日志就知道这一次是走兜底找到的，还是 Toolhelp 正常。
        _UDP_FALLBACK_LOGGED = True
        detail = "、".join(f"{p.pid} {p.comm}" for p in found)
        print(f"[procfs][info] 进程快照里看不到 VRChat，已按 UDP 表反查到：{detail}",
              flush=True)
    return found


def find_eac() -> list[ProcInfo]:
    """所有看起来像 EAC 的进程。"""
    return discover(EAC_MARKERS)


def pick_host(procs: Iterable[ProcInfo]) -> ProcInfo | None:
    """挑出「托管堆就在这里面」的那个进程。

    排序键：可执行文件名**正好**是 `vrchat.exe` > `starttime` 更新。
    Windows 上 `maps_ok` 恒假、也没有 `GameAssembly.dll` 可比，所以不能沿用
    `procfs.pick_host` 那套按 maps 大小的排序（它会永远返回 None）。
    """
    cands = list(procs)
    if not cands:
        return None

    def key(p: ProcInfo) -> tuple[bool, int]:
        return (p.comm.lower() in _GAME_EXE, p.starttime)

    return max(cands, key=key)


def udp_endpoints(pid: int) -> list[UdpEndpoint]:
    """进程当前绑定的本地 UDP 端点。

    只有本端：NT 不维护 UDP 连接状态表，所以 `remote_addr`/`remote_port` 恒 None、
    `connected` 恒 False、`inode` 恒 0（Windows 没有 inode 概念）。
    远端端口请从抓到的 `Datagram.src/dst` 读。
    """
    if pid <= 0:
        return []
    return [UdpEndpoint(local_addr=addr, local_port=port,
                        remote_addr=None, remote_port=None, inode=0)
            for addr, port, owner in dgram.udp_table() if owner == pid]


def udp_ports(pid: int) -> set[int]:
    """游戏进程绑定的本地 UDP 端口集合（方向判定用）。"""
    return {e.local_port for e in udp_endpoints(pid)}
