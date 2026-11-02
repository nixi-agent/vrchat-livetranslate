"""Windows 侧的抓包/取钥提供者，对位 `leg._HelperCapture`。

Linux 走的是「小助手进程（pkexec 提权）持有 AF_PACKET socket 并读游戏内存」，
Windows 上这两件事都不成立：

* EAC 用 `ObRegisterCallbacks` 把 `PROCESS_VM_READ` 从任何指向游戏的句柄上剥掉，
  所以读不了内存（`key_win.py` 的 docstring 记了全部试过的路）；
* 没有 `AF_PACKET`，抓包改走 Npcap（`dgram.py`），而且**不需要提权**。

于是取钥拆成两段：**非提权**抓包拿到密文样本 → **一次性提权**读裸盘在 pagefile
里找那把 32 字节的 key（`pagefile.py` + `key_win.py`）。key 是进程级的（换房间
不换，只有重启游戏才换），所以 `SessionCache` 命中后可以零 UAC 反复进世界。

加速器（UU 等）把 Photon 密文原样透传到本地环回、只在前面加固定头，
`dgram.Stripper` 按「加密包占比 + challenge 唯一」反推头长，真伪由 GCM 认证兜底。

本模块只依赖 ctypes + 标准库（`dgram`/`pagefile`/`key_win`/`procfs_win`/`gcm_win`）
和游戏自带的 `opus.dll`，不需要 `cryptography`、不需要 numpy —— 换句话说，
它可以在一台没跑过 `setup.bat` 的裸 Python 上直接跑起来做回归。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from . import dgram, gcm, gcm_win, key_win, keyd, names, opus, pagefile, procfs, procfs_win

# AAD 口径：`key_win` 的命名 → `gcm` 的 layout 名。
# `hdr19` = AAD 取包前 19 字节（peerID+flag+challenge+nonce），实测唯一成立的那个。
AAD_KIND = "hdr19"
_AAD_TO_LAYOUT = {
    "hdr19": gcm.LAYOUTS[0],
    "hdr7": gcm.LAYOUTS[1],
    "none": gcm.LAYOUTS[2],
}

# 回放窗口：取钥那十几秒里抓到的包要补一遍，否则名字表和开场语音会丢。
REPLAY_S = 5.0
MAX_REPLAY = 4096
# 与 `leg.SAMPLE_TARGET` 对齐：够了就开始扫，不用等收满。
SAMPLE_TARGET = 40
# pagefile 的 MFT 记录号在单机上是不变的；给出来能省掉整表扫描（实测 67 s → 0 s）。
# 优先级：环境变量 `VLT_PAGEFILE_MFT` > 上次扫到的缓存文件 > 自己扫。
# 缓存带卷序列号校验：换了系统盘/重装系统会自动失效，退回整表扫一次再刷新。
HINT_ENV = "VLT_PAGEFILE_MFT"
HINT_FILE = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
             / "vlt" / "pagefile-hint.json")
# 方向判定端口的刷新间隔。加速器的出站端口是临时端口，重连后会换；
# 与 `leg.ENDPOINT_REFRESH_S` 同值，但这里自己定义 —— 从 `leg` 取会循环导入。
DIR_REFRESH_S = 2.0


class KeyUnavailable(RuntimeError):
    """取不到密钥。`fatal=True` 表示再重试也没用（没提权 / 没定位到 pagefile）。"""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def _hint_from_env() -> int:
    raw = os.environ.get(HINT_ENV, "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _hint_mft() -> int:
    """pagefile.sys 的 MFT 记录号：环境变量 > 上次缓存 > 0（自己扫）。

    给了它 `find_pagefile` 就直接读那一条记录，省掉 324 万条整表扫描
    （实测 67 s → 0 s）。缓存只存卷序列号 + 记录号，不含任何凭据。
    """
    hint = _hint_from_env()
    if hint:
        return hint
    try:
        data = json.loads(HINT_FILE.read_text("utf-8"))
        mft = int(data.get("mft_index") or 0)
        return mft if mft > 0 else 0
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _save_hint(mft_index: int, volume_serial: int) -> None:
    """记住这次扫到的 pagefile MFT 记录号（写不进就算了，不影响取钥）。"""
    if mft_index <= 0:
        return
    try:
        HINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HINT_FILE.write_text(
            json.dumps({"mft_index": int(mft_index),
                        "volume_serial": int(volume_serial or 0)},
                       ensure_ascii=False) + "\n",
            "utf-8")
    except OSError:
        pass


def _cached_serial() -> int:
    """缓存里记的卷序列号（0 = 没有缓存）。用来判断缓存是否还适用于当前系统盘。"""
    try:
        data = json.loads(HINT_FILE.read_text("utf-8"))
        return int(data.get("volume_serial") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def samples_to_groups(samples) -> tuple[dict, dict]:
    """把 `leg` 的 `[(gcm.Flow, payload)]` 转成 `key_win` 要的口径。

    `key_win` 按 `(peerID, challenge)` 分组 —— 即密文头的前 5 个字节
    （`payload[0:2]` 是 peerID、`payload[1:5]` 是 challenge，与 `scripts/pf_key2.py`
    的 `load_samples` 同口径）。同时返回 `(peerID, challenge) → Flow` 的反查表，
    因为最后构造 `gcm.KeyHit` 还要知道这把 key 属于哪条流。
    """
    groups: dict[tuple[bytes, bytes], list[bytes]] = {}
    flows: dict[tuple[bytes, bytes], gcm.Flow] = {}
    for flow, payload in samples:
        if len(payload) < gcm.MIN_PACKET or payload[2] != 0x01:
            continue
        tag = (bytes(payload[0:2]), bytes(payload[1:5]))
        groups.setdefault(tag, []).append(bytes(payload))
        flows.setdefault(tag, flow)
    # 只留够判定的流：一个包解不开就没法区分「key 不对」和「包不全」。
    groups = {t: p for t, p in groups.items() if len(p) >= 2}
    return groups, flows


def make_hit(key: bytes, groups: dict, flows: dict, tag=None) -> gcm.KeyHit:
    """把裸 key 包成 `gcm.KeyHit`（`leg` 只认这个类型：要 `.key/.layout/.fingerprint`）。"""
    tag = tag or next(iter(groups))
    pkts = groups[tag]
    return gcm.KeyHit(
        key=bytes(key),
        flow=flows.get(tag) or next(iter(flows.values())),
        ok=len(pkts),
        total=len(pkts),
        layout=_AAD_TO_LAYOUT[AAD_KIND],
    )


# key 是**进程级**的（换世界/换房间都不变，只有游戏重启才换），所以缓存必须跨
# `_WinCapture` 实例共享：预热器（warmup）先取一次钥，`leg` 随后新建的 cap 才能在
# 同一份缓存上命中 → 换世界不再弹 UAC、也不用重扫 20 秒的 pagefile。
_SESSION = key_win.SessionCache()


class _WinCapture:
    """`leg._decode` 眼里的 `cap`：喂数据报、给 key、收养名字表、报状态。"""

    def __init__(self, leg, host: procfs.ProcInfo) -> None:
        self._leg = leg
        self._host = host
        self._port = int(getattr(leg, "port", dgram.DEFAULT_PORT))
        self._pid = int(getattr(host, "pid", 0) or 0)
        self._ident = key_win.Identity.of(self._pid) if self._pid > 0 else None
        self._local_ports: set[int] | None = None
        self._dir_ports: set[int] = set()      # 方向判定用（游戏端口 ∪ 全系统本地端口）
        self._dir_until: float = 0.0

        self._lock = threading.Lock()
        self._sniffer: dgram.Sniffer | None = None
        self._stripper = dgram.Stripper()
        self._cache = _SESSION
        self._key: gcm.KeyHit | None = None
        self._table: names.NameTable | None = None
        self._status_provider = None
        self._replay: deque = deque(maxlen=MAX_REPLAY)
        self._frames = 0
        self._opened = False

        # 游戏自带的 opus.dll 与它自己编码语音用的是同一版本，优先用它。
        path = procfs_win.image_path(self._pid) if self._pid > 0 else None
        if path:
            self._game_exe = path
            opus.add_search_dir(Path(path).parent)
        else:
            self._game_exe = ""

    # ---- 生命周期 -------------------------------------------------------
    def __enter__(self) -> "_WinCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            sniffer, self._sniffer = self._sniffer, None
        if sniffer is not None:
            try:
                sniffer.close()
            except Exception:                             # noqa: BLE001 - 关就关了
                pass

    def _open(self) -> dgram.Sniffer:
        """惰性开接口：构造 `_WinCapture` 时不碰驱动，出错点留在第一次 drain。"""
        with self._lock:
            if self._sniffer is not None:
                return self._sniffer
            if self._opened:
                raise dgram.DgramError("抓包接口已关闭")
            sniffer = dgram.open_sniffer(port=self._port)
            self._sniffer = sniffer
            self._opened = True
            return sniffer

    # ---- 状态回调 -------------------------------------------------------
    def _status(self, level: str, message: str) -> None:
        try:
            self._leg.on_status(level, message)
        except Exception:                                 # noqa: BLE001 - 状态不影响主流程
            pass

    def bind_status(self, provider) -> None:
        """翻译开始后，状态页改看腿的实时计数。"""
        self._status_provider = provider

    def status_counts(self) -> dict:
        provider = self._status_provider
        if provider is None:
            return {}
        try:
            return dict(provider() or {})
        except Exception:                                 # noqa: BLE001
            return {}

    # ---- 数据面 ---------------------------------------------------------
    def set_local_ports(self, ports) -> None:
        """游戏当前的本地 UDP 端口集合。

        UU 链路上两端都不是 27002（客户端连的是加速器的一个高位端口），
        所以方向归属要靠「本端端口在不在这个集合里」判，不能用固定端口。
        但只认游戏自己的端口还不够：走加速器时真正带 Photon 密文的是加速器
        进程的出站段（`<加速器出站端口> -> <加速器服务器端口>`），两个端口都
        不属于游戏，整条隧道会被判成方向不明而丢掉 —— 所以判据里还要并入
        「任何本端端口」（见 `_direction_ports`），剩下的交给 `Stripper` 按流剥头。
        """
        self._local_ports = set(ports) if ports else None
        self._dir_ports = set()                       # 立刻重算
        self._dir_until = 0.0

    def _direction_ports(self) -> set[int]:
        """方向判定用的端口集合：游戏自己的端口 ∪ 全系统本地 UDP 端口。

        后者每 `DIR_REFRESH_S` 秒重算一次 —— 加速器的出站端口是临时端口，
        重连后会换。
        """
        now = time.monotonic()
        if now < self._dir_until and self._dir_ports:
            return self._dir_ports
        ports = set(self._local_ports or ())
        try:
            ports |= dgram.local_udp_ports()
        except Exception:                             # noqa: BLE001 - 查不到就退回游戏端口
            pass
        self._dir_ports = ports
        self._dir_until = now + DIR_REFRESH_S
        return ports

    def drain(self, timeout: float = 0.2) -> list[tuple[float, dgram.Datagram]]:
        """读一批帧并就地剥掉加速器头，返回已解析的数据报。

        与 `capture_linux.drain` 的差别只在第二项：那边给的是链路层原始帧
        （`leg` 再调 `parse_frame`），这里给的是 `Datagram` 本身 —— 因为剥头
        必须基于「已经认出 UDP 载荷」才能做，重复解析没有意义。
        `parse` 因此是恒等映射。
        """
        sniffer = self._open()
        out: list[tuple[float, dgram.Datagram]] = []
        now = time.monotonic()
        for ts, frame in sniffer.drain(timeout=timeout):
            # 端口两侧都认：直连靠 27002，走加速器靠「本端端口集合」。
            # 集合为空（读不到表）时退回 `port` 判据。
            dg = dgram.parse_frame(
                frame, port=self._port, local_ports=self._direction_ports() or None
            )
            if dg is None:
                continue
            stripped = self._stripper.feed(dg)
            if stripped is None:
                continue
            # `parse_frame` 只认载荷、把 ts 留成 0.0；这里必须补上 pcap 给的墙钟，
            # 否则位置表的 ts 全是 0，`PositionTable.get()` 一律判过期 → 右栏距离全 None。
            stripped = replace(stripped, ts=ts)
            self._frames += 1
            if stripped.is_encrypted:
                self._replay.append((now, stripped))
            out.append((ts, stripped))
        # 回放窗口按墙钟裁掉：取钥等了十几秒，太老的包不再补。
        while self._replay and now - self._replay[0][0] > REPLAY_S:
            self._replay.popleft()
        return out

    def parse(self, raw):
        """`_WinCapture.drain` 已经解析过，这里恒等。"""
        return raw

    def recent_cipher(self) -> list[dgram.Datagram]:
        with self._lock:
            return [dg for _ts, dg in list(self._replay)]

    def cached_key(self) -> gcm.KeyHit | None:
        return self._key

    # ---- 名字表 ---------------------------------------------------------
    def initial_names(self) -> names.NameTable | None:
        table = self._table
        return table.copy() if table is not None else None

    def adopt_names(self, table: names.NameTable) -> None:
        """停翻译时把腿的名字表接回来：下一轮直接就有名单，不用再等广播。"""
        if table is not None:
            self._table = table

    # ---- 取钥 -----------------------------------------------------------
    def acquire_key(self, samples) -> gcm.KeyHit:
        if self._key is not None:
            return self._key
        groups, flows = samples_to_groups(samples)
        if not groups:
            raise KeyUnavailable("没有可用的 Photon 加密包样本")

        ident = self._ident or key_win.Identity.of(self._pid)
        if ident is None:
            raise KeyUnavailable("读不到游戏进程身份（游戏退出了？）", fatal=True)

        hit = self._from_cache(ident, groups, flows)
        if hit is None:
            hit = self._scan(ident, groups, flows)
        self._key = hit
        return hit

    def _from_cache(self, ident: key_win.Identity, groups: dict, flows: dict):
        """会话缓存命中就零 UAC —— key 是进程级的，换房间不换。"""
        if self._cache.peek() is None:
            return None
        auth = gcm_win.GcmAuth()
        try:
            key = self._cache.lookup(ident, groups, auth=auth)
        finally:
            auth.close()
        if key is None:
            return None
        self._status("info", "命中会话缓存（key 是进程级的），跳过提权与整盘扫描")
        return make_hit(key, groups, flows)

    def _scan(self, ident: key_win.Identity, groups: dict, flows: dict) -> gcm.KeyHit:
        """分派：已是管理员就在本进程内扫，否则拉起一次性提权子进程（`keyd`）。

        两条路做的事完全一样（定位 pagefile → 驱逐脏页 → 扫锚点 → GCM 认证），
        区别只是权限来源：`\\.\PhysicalDriveN` 只给管理员 `GENERIC_READ`。
        提权只用于这**一次**读盘；密钥随即进 `SessionCache`，换房间不再弹 UAC。
        """
        if keyd.is_admin():
            return self._scan_local(ident, groups, flows)
        return self._scan_via_keyd(ident, groups, flows)

    def _scan_via_keyd(self, ident: key_win.Identity, groups: dict,
                       flows: dict) -> gcm.KeyHit:
        """非管理员路径：把样本交给提权子进程，它只回一个 32 字节候选。"""
        self._status("info", "取钥需要读 pagefile 的原始扇区 —— 现在弹一次 UAC"
                            "（只这一次，换房间不会再弹）")
        try:
            reply = keyd.run_scan(
                pid=ident.pid,
                created=ident.created,
                samples=groups,
                hint_mft=_hint_mft(),
                hint_serial=_cached_serial(),
                on_log=lambda m: self._status("info", m),
            )
        except keyd.KeyDenied as exc:
            raise KeyUnavailable(str(exc)) from exc
        if not reply.get("ok"):
            raise KeyUnavailable(
                str(reply.get("error") or "提权子进程没有交出密钥"),
                fatal=bool(reply.get("fatal")),
            )

        raw = str(reply.get("key") or "")
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise KeyUnavailable(          # 不把 raw 打出来：那是密钥本身
                "提权子进程交回的密钥格式不对（长度或编码异常）", fatal=True
            ) from exc
        if len(key) != gcm_win.KEY_LEN:
            raise KeyUnavailable("提权子进程交回的密钥长度不对", fatal=True)

        # 子进程扫的是它启动那一刻的 pagefile；本地再验一次，免得样本与
        # 扫到的内容对不上却把错 key 缓存下来（比如中间又换了世界）。
        auth = gcm_win.GcmAuth()
        try:
            if not key_win.verify_key(key, groups, auth=auth, aad_kind=AAD_KIND):
                raise KeyUnavailable(
                    f"子进程交回的密钥（指纹 {key_win.key_fingerprint(key)}）"
                    "在本地样本上不通过 GCM 认证：抓包与读盘之间多半又换了世界，重试一次"
                )
            tag = self._tag_for(key, groups, auth)
        finally:
            auth.close()

        self._cache.store(ident, key)
        # 子进程顺带报告了它定位页文件用的记录号；存下来下次就不用再扫 324 万条 MFT。
        _save_hint(int(reply.get("pagefile_mft") or 0),
                   int(reply.get("volume_serial") or 0))
        self._status("info", f"密钥命中（指纹 {key_win.key_fingerprint(key)}，"
                             f"位置 0x{int(reply.get('offset') or 0):X}，"
                             f"由提权子进程扫得）")
        return make_hit(key, groups, flows, tag)

    @staticmethod
    def _tag_for(key: bytes, groups: dict, auth) -> tuple[bytes, bytes] | None:
        """找出这把 key 真正认证通过的那个流，作为 `KeyHit` 的标签。"""
        for (peer, chal), pkts in groups.items():
            n = min(len(pkts), 8)
            if n < key_win.MIN_FLOW_PACKETS:
                continue
            if auth.opens_all(key, pkts[:n], aad_kind=AAD_KIND) == n:
                return (bytes(peer), bytes(chal))
        return None

    def _scan_local(self, ident: key_win.Identity, groups: dict,
                    flows: dict) -> gcm.KeyHit:
        """已是管理员：本进程直接读盘扫，省掉一次子进程往返。"""
        try:
            disk, layout = pagefile.open_volume("C")
        except pagefile.PagefileError as exc:
            raise KeyUnavailable(
                f"打不开系统盘所在的物理盘：{exc}\n"
                "取钥要读 pagefile.sys 的原始扇区，这一步必须管理员权限（会弹一次 UAC）",
                fatal=True,
            ) from exc
        self._status("info", f"系统盘在 PhysicalDrive{layout.disk_no}"
                            f"（盘上起点 {layout.base_offset / 2**30:.1f} GiB）")

        with disk:
            try:
                volume = pagefile.read_boot(disk)
            except pagefile.PagefileError as exc:
                raise KeyUnavailable(
                    f"PhysicalDrive{layout.disk_no} 的 {layout.base_offset} 处"
                    f"读不到 NTFS 引导扇区：{exc}", fatal=True) from exc
            hint = _hint_mft()
            want = _cached_serial()
            if hint and want and int(getattr(volume, "serial", 0)) != want:
                self._status("info", "缓存的 pagefile 记录号属于另一块盘，改走全表扫描")
                hint = 0
            pf = pagefile.find_pagefile(disk, volume, hint_mft=hint,
                                        on_log=lambda m: self._status("info", m))
            if pf is None:
                raise KeyUnavailable("没在 MFT 里定位到 pagefile.sys", fatal=True)
            self._status("info", f"{pf.summary()}")
            if not pf.real_bytes:
                raise KeyUnavailable("pagefile 是空的：取钥前要先进一次世界", fatal=True)

            stats = key_win.prepare(self._pid, on_log=lambda m: self._status("info", m))
            if stats.get("trim_err"):
                raise KeyUnavailable(
                    f"驱逐脏页失败（errno={stats['trim_err']}）：多半是没提权",
                    fatal=True,
                )

            last = [0.0]

            def _progress(done: int, elapsed: float) -> None:
                # 每 2 秒报一次，别把状态页刷满。
                if elapsed - last[0] < 2.0:
                    return
                last[0] = elapsed
                self._status("info", f"扫 pagefile：{done / 2**30:.1f} GiB / {elapsed:.1f}s")

            result = key_win.scan_key(pf, groups, layout=layout,
                                      aad_kind=AAD_KIND, on_progress=_progress)

        if result.hit is None:
            unread = ""
            if result.unread_bytes:
                unread = (f"；另有 {result.unread_bytes / 2**20:.1f} MiB 根本没读到"
                          f"（PhysicalDrive{result.failed_disks} 打不开/读失败）")
            raise KeyUnavailable(
                f"扫了 {result.scanned_bytes / 2**30:.1f} GiB（{result.chunks} 块）、"
                f"候选 {len(result.candidates)} 个，都不通过 GCM 认证。"
                "最可能的原因：内存压缩又开着了（Disable-MMAgent -MemoryCompression 后重启）"
                f"{unread}",
            )
        key = result.hit.key
        self._cache.store(ident, key)
        _save_hint(pf.mft_index, int(getattr(volume, "serial", 0) or 0))
        self._status("info", f"密钥命中（指纹 {key_win.key_fingerprint(key)}，"
                             f"位置 0x{result.hit.offset:X}）")
        return make_hit(key, groups, flows, result.hit.flow)

    # ---- 诊断 -----------------------------------------------------------
    def summary(self) -> dict:
        return {
            "pid": self._pid,
            "game": self._game_exe,
            "frames": self._frames,
            "stripper": self._stripper.stats(),
            "key": self._key.fingerprint if self._key is not None else "",
        }
