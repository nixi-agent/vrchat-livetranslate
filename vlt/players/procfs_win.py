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


def iter_processes() -> list[tuple[int, str, int]]:
    """全系统进程快照 `[(pid, exe 名, 父 pid)]`。

    `TH32CS_SNAPPROCESS` 拿的是**名字**，不碰任何进程内部 —— 这也正是
    `TH32CS_SNAPMODULE` 被 EAC 拒绝时唯一还能用的那份信息。
    """
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) == 0xFFFFFFFFFFFFFFFF:
        return []
    out: list[tuple[int, str, int]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = C.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, C.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile,
                        int(entry.th32ParentProcessID)))
            ok = _k32.Process32NextW(snap, C.byref(entry))
    finally:
        _k32.CloseHandle(snap)
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
    """所有看起来像 VRChat 的进程（游戏本体 + 受保护启动器）。"""
    return discover(GAME_MARKERS)


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
