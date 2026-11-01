"""Windows 抓包层：npcap 抓 UDP → 剥加速器头 → Photon 数据报。

这是 `capture_linux.py` 的 Windows 对位实现，对外形状刻意保持一致
（`Datagram` / `parse_frame` / `direction_of` / `drain` / `PcapWriter` /
`read_pcap`），让 `collector.py`、`leg.py`、`warmup.py` 这些上层模块不必知道
自己跑在哪个平台上。

与 Linux 版的差别只有两处，都在「怎么拿到帧」这一层：

* Linux 用 `AF_PACKET` + 内核 BPF，帧是 socket 直接吐出来的；
* Windows 没有 `AF_PACKET`，改用 Npcap 的 `wpcap.dll`（`pcap_open_live` +
  `pcap_compile`/`pcap_setfilter` + `pcap_next_ex`）。本机只装了 Npcap、
  没有 Wireshark，所以直接 `ctypes.WinDLL` 调 `wpcap.dll`，不引第三方包。

**加速器（UU / 迅游等）会把 Photon 密文原样透传到本地环回端口**，只在前面加
固定长度的头（实测本机 UU：下行 `2101->本地` 加 10 字节，上行 `本地->2101`
加 12 字节）。头长不写死，由 `Stripper` 用「`q[2] == 0x01` 的包占比 ≥ 85%
且一次会话只有一个 challenge」这条内容特征反推；真伪最终由 AES-GCM 认证兜底
（`gcm_win.GcmAuth.opens_all`），猜错了也只会认证失败，不会解出错包。

抓包本身需要 Npcap 已安装；能否免提权取决于安装 Npcap 时是否勾了
"Restrict Npcap driver's access to Administrators only"。
"""

from __future__ import annotations

import ctypes as C
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_PORT = 27002
"""Photon 标准端口。**仅用于直连判定**，加速器链路下两端都不会是它。"""

SNAPLEN = 65535
PCAP_MAGIC = 0xA1B2C3D4
PCAP_NETMASK_UNKNOWN = 0xFFFFFFFF
DLT_NULL, DLT_EN10MB, DLT_RAW = 0, 1, 101

MIN_PKT, MAX_PKT = 35, 65507
"""合法 Photon 加密包的长度区间（35 = 19 字节头 + 16 字节 GCM tag）。"""

MAX_HEADER = 20
"""加速器头最长试探长度。UU 实测 10/12，留一点余量。"""

MIN_HIT_RATIO = 0.85
"""某个偏移下「像 Photon 加密包」的占比下限；允许夹杂明文/心跳包。"""

MIN_SAMPLES = 6
"""至少攒这么多个包才敢判定头长（单包无法区分偏移）。"""

MAX_SAMPLES = 96
"""攒到这么多还判不出来就认定这条流不是 Photon，丢掉。"""

WPCAP_CANDIDATES = (
    r"C:\Windows\System32\Npcap\wpcap.dll",
    r"C:\Windows\System32\wpcap.dll",
    r"C:\Windows\SysWOW64\Npcap\wpcap.dll",
    r"C:\Windows\SysWOW64\wpcap.dll",
)

UDP_TABLE_OWNER_PID = 1
AF_INET4 = 2


class DgramError(OSError):
    """抓包层不可用（没装 Npcap、没有权限、接口不对）。"""


# --------------------------------------------------------------------------
# 数据报
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Datagram:
    """一个 UDP 数据报。

    `direction` 是 "in"（服务器 → 本机，也就是**别人**的声音）或 "out"
    （本机 → 服务器，玩家自己的声音）。
    """

    ts: float
    src: tuple[str, int]
    dst: tuple[str, int]
    payload: bytes
    direction: str

    @property
    def is_encrypted(self) -> bool:
        return len(self.payload) >= 3 and self.payload[2] == 0x01


def direction_of(src: tuple[str, int], dst: tuple[str, int], *,
                 port: int = DEFAULT_PORT,
                 local_ports: set[int] | None = None) -> str | None:
    """判断方向。加速器链路上两端都不是 27002，必须靠 `local_ports`。"""
    if local_ports:
        src_local = src[1] in local_ports
        dst_local = dst[1] in local_ports
        if src_local and not dst_local:
            return "out"
        if dst_local and not src_local:
            return "in"
        if src_local and dst_local:
            return None
    if src[1] == port and dst[1] != port:
        return "in"
    if dst[1] == port and src[1] != port:
        return "out"
    return None


# --------------------------------------------------------------------------
# 帧解析
# --------------------------------------------------------------------------


def _parse_ipv4(frame: bytes, off: int, port: int,
                local_ports: set[int] | None) -> Datagram | None:
    if len(frame) < off + 20:
        return None
    vihl = frame[off]
    if vihl >> 4 != 4:
        return None
    ihl = (vihl & 0x0F) * 4
    if ihl < 20 or len(frame) < off + ihl + 8:
        return None
    if frame[off + 9] != socket.IPPROTO_UDP:
        return None
    total = struct.unpack_from("!H", frame, off + 2)[0]
    udp = off + ihl
    sport, dport, ulen = struct.unpack_from("!HHH", frame, udp)
    if ulen < 8:
        return None
    end = min(udp + ulen, off + total if total >= ihl + 8 else len(frame))
    payload = frame[udp + 8:end]
    src = (socket.inet_ntoa(frame[off + 12:off + 16]), sport)
    dst = (socket.inet_ntoa(frame[off + 16:off + 20]), dport)
    direction = direction_of(src, dst, port=port, local_ports=local_ports)
    if direction is None:
        return None
    return Datagram(ts=0.0, src=src, dst=dst, payload=payload, direction=direction)


def parse_frame(frame: bytes, *, port: int = DEFAULT_PORT,
                local_ports: set[int] | None = None) -> Datagram | None:
    """解析一帧（以太网 / VLAN / 裸 IPv4 都吃）。`ts` 由调用方补。"""
    if len(frame) >= 14:
        ethertype = struct.unpack_from("!H", frame, 12)[0]
        if ethertype == 0x0800:
            dg = _parse_ipv4(frame, 14, port, local_ports)
            if dg is not None:
                return dg
        elif ethertype == 0x8100 and len(frame) >= 18:
            inner = struct.unpack_from("!H", frame, 16)[0]
            if inner == 0x0800:
                dg = _parse_ipv4(frame, 18, port, local_ports)
                if dg is not None:
                    return dg
    if frame and frame[0] >> 4 == 4:
        return _parse_ipv4(frame, 0, port, local_ports)
    return None


def strip_link(linktype: int, frame: bytes) -> bytes:
    """把链路层头去掉，只留 IP 报文（给离线 pcap 用）。"""
    if linktype == DLT_EN10MB:
        return frame[14:] if len(frame) >= 14 else b""
    if linktype == DLT_NULL:
        return frame[4:] if len(frame) >= 4 else b""
    return frame


# --------------------------------------------------------------------------
# 加速器头剥离
# --------------------------------------------------------------------------


def pick_offset(payloads, *, max_offset: int = MAX_HEADER,
                min_hit_ratio: float = MIN_HIT_RATIO) -> tuple[int, dict] | None:
    """在一组同流 UDP 载荷上反推加速器头长。

    判据：剥掉 off 字节后，绝大多数包满足 `MIN_PKT <= len <= MAX_PKT` 且
    `q[2] == 0x01`（Photon 加密标志），并且所有包的 challenge 唯一
    （一次会话只有一个）。返回 `(off, {(peer, challenge): [包…]})`。
    """
    best: tuple[tuple[int, int], int, dict] | None = None
    for off in range(max_offset + 1):
        groups: dict[tuple[bytes, bytes], list[bytes]] = {}
        total = 0
        for p in payloads:
            q = p[off:]
            if not (MIN_PKT <= len(q) <= MAX_PKT):
                continue
            total += 1
            if q[2] != 0x01:
                continue
            groups.setdefault((q[0:2], q[3:7]), []).append(q)
        if not groups:
            continue
        hit = sum(len(v) for v in groups.values())
        if total == 0 or hit / total < min_hit_ratio:
            continue
        if len({c for (_p, c) in groups}) != 1:
            continue
        score = (hit, -off)
        if best is None or score > best[0]:
            best = (score, off, groups)
    if best is None:
        return None
    _score, off, groups = best
    return off, groups


def _reslice(dg: Datagram, off: int) -> Datagram | None:
    if off <= 0:
        return dg
    if len(dg.payload) <= off:
        return None
    return Datagram(ts=dg.ts, src=dg.src, dst=dg.dst,
                    payload=dg.payload[off:], direction=dg.direction)


class Stripper:
    """按流自适应剥掉加速器头。

    每条流先攒 `MIN_SAMPLES` 个载荷反推头长，判定后固定使用；攒到
    `MAX_SAMPLES` 仍判不出来就认定这条流不是 Photon（UU 自己的心跳、
    DNS 之类），此后直接丢弃。直连流会判出 `off = 0`，等价于不剥。
    """

    def __init__(self, *, max_offset: int = MAX_HEADER,
                 min_hit_ratio: float = MIN_HIT_RATIO,
                 min_samples: int = MIN_SAMPLES,
                 max_samples: int = MAX_SAMPLES) -> None:
        self._max_offset = max_offset
        self._min_hit_ratio = min_hit_ratio
        self._min_samples = max(2, min_samples)
        self._max_samples = max(self._min_samples, max_samples)
        self._lock = threading.Lock()
        self._pending: dict[tuple, list[bytes]] = {}
        self._offset: dict[tuple, int] = {}
        self.settled = 0
        self.dropped = 0

    @staticmethod
    def _flow(dg: Datagram) -> tuple:
        # UU 的两端都在 127.0.0.1，只能靠端口区分方向
        return (dg.src, dg.dst)

    def offset_of(self, dg: Datagram) -> int | None:
        return self._offset.get(self._flow(dg))

    def feed(self, dg: Datagram) -> Datagram | None:
        """喂一个未剥头的数据报；返回剥好的（或 None 表示还在判/不是 Photon）。"""
        flow = self._flow(dg)
        with self._lock:
            off = self._offset.get(flow)
            if off is not None:
                if off < 0:
                    return None
                return _reslice(dg, off)
            buf = self._pending.setdefault(flow, [])
            buf.append(dg.payload)
            if len(buf) < self._min_samples:
                return None
            if len(buf) > self._max_samples:
                self._offset[flow] = -1
                self._pending.pop(flow, None)
                self.dropped += 1
                return None
            picked = pick_offset(buf, max_offset=self._max_offset,
                                 min_hit_ratio=self._min_hit_ratio)
            if picked is None:
                return None
            off, _groups = picked
            self._offset[flow] = off
            self._pending.pop(flow, None)
            self.settled += 1
            return _reslice(dg, off)

    def stats(self) -> dict:
        with self._lock:
            return {
                "flows": {k: v for k, v in self._offset.items()},
                "settled": self.settled,
                "dropped": self.dropped,
                "pending": len(self._pending),
            }


# --------------------------------------------------------------------------
# 本机 UDP 端口（判方向用）
# --------------------------------------------------------------------------


class _MIB_UDPROW_OWNER_PID(C.Structure):
    _fields_ = [
        ("dwLocalAddr", C.c_uint32),
        ("dwLocalPort", C.c_uint32),
        ("dwProcessId", C.c_uint32),
    ]


class _MIB_UDPTABLE_OWNER_PID(C.Structure):
    _fields_ = [
        ("dwNumEntries", C.c_uint32),
        ("table", _MIB_UDPROW_OWNER_PID * 1),
    ]


def udp_table() -> list[tuple[str, int, int]]:
    """全系统 UDP 端点表 `[(local_addr, local_port, pid)]`。

    只有**本端**：NT 不维护 UDP 连接状态表，`MIB_UDPROW_OWNER_PID` 里没有对端地址，
    所以远端端口只能从抓到的包里读（`Datagram.src/dst`），比查表准。

    `dwLocalPort` 是**网络字节序**塞进 DWORD 的低 16 位，`dwLocalAddr` 同理，
    两者都要手工换序。`procfs_win` 与本模块共用这一份遍历。
    """
    iphlpapi = C.WinDLL("iphlpapi", use_last_error=True)
    iphlpapi.GetExtendedUdpTable.argtypes = [
        C.c_void_p, C.POINTER(C.c_uint32), C.c_int, C.c_uint32,
        C.c_uint32, C.c_uint32,
    ]
    iphlpapi.GetExtendedUdpTable.restype = C.c_uint32

    size = C.c_uint32(0)
    iphlpapi.GetExtendedUdpTable(None, C.byref(size), False, AF_INET4,
                                 UDP_TABLE_OWNER_PID, 0)
    if size.value == 0:
        return []
    buf = C.create_string_buffer(size.value)
    rc = iphlpapi.GetExtendedUdpTable(buf, C.byref(size), False, AF_INET4,
                                      UDP_TABLE_OWNER_PID, 0)
    if rc != 0:
        return []
    table = C.cast(buf, C.POINTER(_MIB_UDPTABLE_OWNER_PID)).contents
    row_size = C.sizeof(_MIB_UDPROW_OWNER_PID)
    base = C.addressof(buf) + _MIB_UDPTABLE_OWNER_PID.table.offset
    rows: list[tuple[str, int, int]] = []
    for i in range(int(table.dwNumEntries)):
        row = _MIB_UDPROW_OWNER_PID.from_address(base + i * row_size)
        raw = int(row.dwLocalPort) & 0xFFFF
        port = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)
        addr = socket.inet_ntoa(struct.pack("<I", int(row.dwLocalAddr) & 0xFFFFFFFF))
        rows.append((addr, port, int(row.dwProcessId)))
    return rows


def udp_ports(pid: int) -> set[int]:
    """某个进程当前占用的全部本地 UDP 端口。"""
    if pid <= 0:
        return set()
    return {port for _addr, port, owner in udp_table() if owner == pid}


def local_udp_ports() -> set[int]:
    """全系统所有本地 UDP 端口（不分进程）。

    走加速器时方向判定的兜底：Photon 的出站段（`<加速器出站端口> -> <加速器服务器端口>`）
    两端都不是 27002，而出站端口属于加速器进程、不是游戏进程，所以只用
    「游戏的端口集合」会把整条隧道判成 `None` 丢掉。把「任何本端端口」都算成本端，
    再交给 `Stripper` 按流自适应剥头，直连与加速器两种情况就都通了。
    """
    return {port for _addr, port, _owner in udp_table()}


# --------------------------------------------------------------------------
# npcap
# --------------------------------------------------------------------------


class _PCAP_IF(C.Structure):
    pass


_PCAP_IF._fields_ = [
    ("next", C.POINTER(_PCAP_IF)),
    ("name", C.c_char_p),
    ("description", C.c_char_p),
    ("addresses", C.c_void_p),
    ("flags", C.c_uint),
]


class _TIMEVAL(C.Structure):
    _fields_ = [("tv_sec", C.c_long), ("tv_usec", C.c_long)]


class _PKTHDR(C.Structure):
    _fields_ = [("ts", _TIMEVAL), ("caplen", C.c_uint), ("len", C.c_uint)]


class _BPF_PROGRAM(C.Structure):
    _fields_ = [("bf_len", C.c_uint), ("bf_insns", C.c_void_p)]


_lib = None
_lib_lock = threading.Lock()


def _load():
    """加载并绑定 wpcap.dll（幂等）。"""
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        path = next((p for p in WPCAP_CANDIDATES if os.path.exists(p)), None)
        if path is None:
            raise DgramError("找不到 wpcap.dll —— 这台机器没装 Npcap")
        lib = C.WinDLL(path)
        lib.pcap_findalldevs.argtypes = [C.POINTER(C.POINTER(_PCAP_IF)), C.c_char_p]
        lib.pcap_findalldevs.restype = C.c_int
        lib.pcap_freealldevs.argtypes = [C.POINTER(_PCAP_IF)]
        lib.pcap_freealldevs.restype = None
        lib.pcap_open_live.argtypes = [C.c_char_p, C.c_int, C.c_int, C.c_int, C.c_char_p]
        lib.pcap_open_live.restype = C.c_void_p
        lib.pcap_datalink.argtypes = [C.c_void_p]
        lib.pcap_datalink.restype = C.c_int
        lib.pcap_compile.argtypes = [C.c_void_p, C.POINTER(_BPF_PROGRAM),
                                     C.c_char_p, C.c_int, C.c_uint]
        lib.pcap_compile.restype = C.c_int
        lib.pcap_setfilter.argtypes = [C.c_void_p, C.POINTER(_BPF_PROGRAM)]
        lib.pcap_setfilter.restype = C.c_int
        lib.pcap_freecode.argtypes = [C.POINTER(_BPF_PROGRAM)]
        lib.pcap_freecode.restype = None
        lib.pcap_next_ex.argtypes = [C.c_void_p, C.POINTER(C.POINTER(_PKTHDR)),
                                     C.POINTER(C.POINTER(C.c_ubyte))]
        lib.pcap_next_ex.restype = C.c_int
        lib.pcap_geterr.argtypes = [C.c_void_p]
        lib.pcap_geterr.restype = C.c_char_p
        lib.pcap_close.argtypes = [C.c_void_p]
        lib.pcap_close.restype = None
        _lib = lib
        return lib


def available() -> bool:
    """Npcap 是否可用（不打开接口，不需要权限）。"""
    try:
        _load()
    except (DgramError, OSError):
        return False
    return True


def devices() -> list[tuple[str, str]]:
    """列出可抓包接口 `(name, description)`。"""
    lib = _load()
    errbuf = C.create_string_buffer(256)
    head = C.POINTER(_PCAP_IF)()
    if lib.pcap_findalldevs(C.byref(head), errbuf) != 0:
        raise DgramError(f"pcap_findalldevs 失败：{errbuf.value.decode('utf-8', 'replace')}")
    out: list[tuple[str, str]] = []
    node = head
    while node:
        d = node.contents
        name = d.name.decode("utf-8", "replace") if d.name else ""
        desc = d.description.decode("utf-8", "replace") if d.description else ""
        out.append((name, desc))
        node = d.next
    lib.pcap_freealldevs(head)
    return out


_SKIP_DESC = (
    "hyper-v", "loopback", "wan miniport", "adapter for loopback",
    "wireless", "wi-fi", "wifi", "wlan", "802.11", "bluetooth",
    "tap-windows", "openvpn", "virtual", "vpn", "inode", "uu ",
)


def pick_device(devs: list[tuple[str, str]], want: str | None = None) -> tuple[str, str]:
    """挑接口。默认跳过无线/虚拟网卡 —— 本机插过一个名字带 Realtek 的
    USB 无线网卡，之前就是被它抢走接口，30 秒抓到 0 个包。"""
    if want:
        needle = want.lower()
        for name, desc in devs:
            if needle in name.lower() or needle in desc.lower():
                return name, desc
        raise DgramError(f"没有匹配 {want!r} 的接口")
    cand = [(n, d) for n, d in devs if not any(s in d.lower() for s in _SKIP_DESC)]
    for key in ("ethernet", "gbe", "gbps", "pcie", "以太网",
                "realtek", "intel", "killer", "broadcom"):
        for name, desc in cand:
            if key in desc.lower():
                return name, desc
    if cand:
        return cand[0]
    if devs:
        return devs[0]
    raise DgramError("没有任何可抓包的接口")


class Sniffer:
    """Npcap 抓包句柄。

    `drain()` 的签名与 `capture_linux.drain(sock, timeout=…, max_frames=…)`
    对齐（少一个位置参数，因为句柄就是 self），所以 `warmup` 那种注入
    `drain_fn` 的地方可以原样塞进来。
    """

    def __init__(self, *, port: int = DEFAULT_PORT,
                 device: str | None = None,
                 bpf: str = "udp",
                 snaplen: int = SNAPLEN,
                 timeout_ms: int = 100,
                 promisc: bool = False,
                 local_ports: set[int] | None = None) -> None:
        self.port = port
        self.local_ports = set(local_ports or ())
        self._handle = None
        self._closed = False
        self.frames = 0
        lib = _load()
        name, desc = pick_device(devices(), device)
        self.device, self.description = name, desc
        errbuf = C.create_string_buffer(256)
        handle = lib.pcap_open_live(name.encode(), snaplen, 1 if promisc else 0,
                                    timeout_ms, errbuf)
        if not handle:
            raise DgramError(
                f"打开接口失败（{errbuf.value.decode('utf-8', 'replace')}）；"
                "抓包一般需要管理员权限"
            )
        self._handle = handle
        self.linktype = int(lib.pcap_datalink(handle))
        prog = _BPF_PROGRAM()
        if lib.pcap_compile(handle, C.byref(prog), bpf.encode(), 1,
                            PCAP_NETMASK_UNKNOWN) != 0:
            msg = lib.pcap_geterr(handle)
            self.close()
            raise DgramError(f"BPF 编译失败：{msg.decode('utf-8', 'replace') if msg else '?'}")
        try:
            if lib.pcap_setfilter(handle, C.byref(prog)) != 0:
                msg = lib.pcap_geterr(handle)
                raise DgramError(f"BPF 安装失败：{msg.decode('utf-8', 'replace') if msg else '?'}")
        finally:
            lib.pcap_freecode(C.byref(prog))

    # -- 生命周期 ---------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed or self._handle is None

    def close(self) -> None:
        if self._handle is not None:
            _load().pcap_close(self._handle)
            self._handle = None
        self._closed = True

    def __enter__(self) -> "Sniffer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 读取 -------------------------------------------------------------

    def read(self) -> tuple[float, bytes] | None:
        """读一帧，超时返回 None。"""
        if self.closed:
            return None
        lib = _load()
        hdr = C.POINTER(_PKTHDR)()
        data = C.POINTER(C.c_ubyte)()
        rc = lib.pcap_next_ex(self._handle, C.byref(hdr), C.byref(data))
        if rc == 1:
            h = hdr.contents
            blob = bytes(C.string_at(data, int(h.caplen)))
            self.frames += 1
            return (float(h.ts.tv_sec) + float(h.ts.tv_usec) / 1e6, blob)
        if rc == -2:
            self.close()
            return None
        if rc < 0:
            msg = lib.pcap_geterr(self._handle)
            raise DgramError(f"pcap_next_ex 失败：{msg.decode('utf-8', 'replace') if msg else rc}")
        return None

    def drain(self, *, timeout: float = 0.2,
              max_frames: int = 128) -> list[tuple[float, bytes]]:
        """在 `timeout` 秒内尽量多读，最多 `max_frames` 帧。"""
        out: list[tuple[float, bytes]] = []
        deadline = time.monotonic() + max(0.0, timeout)
        while len(out) < max_frames:
            item = self.read()
            if item is None:
                if self.closed or time.monotonic() >= deadline:
                    break
                continue
            out.append(item)
            if time.monotonic() >= deadline:
                break
        return out

    # -- 便利方法 ---------------------------------------------------------

    def drain_datagrams(self, *, timeout: float = 0.2, max_frames: int = 128,
                        stripper: "Stripper | None" = None
                        ) -> list[Datagram]:
        """一步到胃：读帧 → 解析 → 剥加速器头，返回可喂 photon 的数据报。"""
        out: list[Datagram] = []
        for ts, frame in self.drain(timeout=timeout, max_frames=max_frames):
            dg = parse_frame(frame, port=self.port, local_ports=self.local_ports)
            if dg is None:
                continue
            dg = Datagram(ts=ts, src=dg.src, dst=dg.dst,
                          payload=dg.payload, direction=dg.direction)
            if stripper is not None:
                dg = stripper.feed(dg)
                if dg is None:
                    continue
            out.append(dg)
        return out

    def stats(self) -> dict:
        return {"device": self.device, "linktype": self.linktype,
                "frames": self.frames, "closed": self.closed}


def open_sniffer(*, port: int = DEFAULT_PORT, device: str | None = None,
                 bpf: str = "udp", local_ports: set[int] | None = None,
                 **kw) -> Sniffer:
    """`capture_linux.open_packet_socket` 的对位入口。"""
    return Sniffer(port=port, device=device, bpf=bpf,
                   local_ports=local_ports, **kw)


# --------------------------------------------------------------------------
# pcap 读写（格式与 capture_linux 完全一致，两边可互换）
# --------------------------------------------------------------------------


class PcapWriter:
    """经典 pcap 写出器（纳秒精度不写，用标准 µs）。"""

    def __init__(self, path) -> None:
        self.path = str(path)
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._fh = open(self.path, "wb")
        self._fh.write(struct.pack("<IHHiIII", PCAP_MAGIC, 2, 4, 0, 0, SNAPLEN, DLT_EN10MB))
        self.frames = 0

    def write(self, ts: float, frame: bytes) -> None:
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        if usec < 0:
            usec = 0
        elif usec > 999_999:
            usec = 999_999
        self._fh.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)))
        self._fh.write(frame)
        self.frames += 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "PcapWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def pcap_frames(path) -> tuple[int, list[tuple[float, bytes]]]:
    """读经典 pcap，返回 `(linktype, [(ts, frame)])`。

    比 `read_pcap` 多带一个 linktype —— Windows 侧离线分析经常要自己
    `strip_link`，而 `capture_linux.read_pcap` 只返回帧列表。
    """
    with open(path, "rb") as fh:
        gh = fh.read(24)
        if len(gh) < 24:
            return DLT_EN10MB, []
        magic = struct.unpack_from("<I", gh, 0)[0]
        if magic in (PCAP_MAGIC, 0xA1B23C4D):
            endian = "<"
        elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
            endian = ">"
        else:
            raise DgramError(f"{path} 不是经典 pcap（magic=0x{magic:08X}）")
        linktype = struct.unpack_from(endian + "I", gh, 20)[0]
        out: list[tuple[float, bytes]] = []
        while True:
            ph = fh.read(16)
            if len(ph) < 16:
                break
            sec, usec, caplen, _orig = struct.unpack(endian + "IIII", ph)
            blob = fh.read(caplen)
            if len(blob) < caplen:
                break
            out.append((sec + usec / 1e6, blob))
    return linktype, out


def read_pcap(path) -> list[tuple[float, bytes]]:
    """读经典 pcap，返回 `[(ts, frame)]` —— 与 `capture_linux.read_pcap` 同形。"""
    return pcap_frames(path)[1]


def read_datagrams(path, *, port: int = DEFAULT_PORT,
                   local_ports: set[int] | None = None,
                   stripper: "Stripper | None" = None) -> list[Datagram]:
    """离线把 pcap 读成（已剥头的）数据报列表，调试与回放用。"""
    linktype, frames = pcap_frames(path)
    out: list[Datagram] = []
    for ts, frame in frames:
        dg = parse_frame(frame, port=port, local_ports=local_ports)
        if dg is None:
            continue
        dg = Datagram(ts=ts, src=dg.src, dst=dg.dst,
                      payload=dg.payload, direction=dg.direction)
        if stripper is not None:
            dg = stripper.feed(dg)
            if dg is None:
                continue
        out.append(dg)
    return out
