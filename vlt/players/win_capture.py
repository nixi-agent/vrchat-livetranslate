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

from .. import paths
from ..paths import _user_data_dir
from . import (dgram, gcm, gcm_win, key_win, keyd, names, opus, pagefile,
               procfs, procfs_win)

# AAD 口径：`key_win` 的命名 → `gcm` 的 layout 名。
# `hdr19` = AAD 取包前 19 字节（peerID+flag+challenge+nonce），实测唯一成立的那个。
AAD_KIND = "hdr19"
_AAD_TO_LAYOUT = {
    "hdr19": gcm.LAYOUTS[0],
    "hdr7": gcm.LAYOUTS[1],
    "none": gcm.LAYOUTS[2],
}

# ---------------------------------------------------------------- 密钥磁盘缓存
# 为什么要落盘：冷启动一次扫描要读 34 GiB（本机 170 s）。密钥是**进程级**的
# （换房间不换，只有游戏重启才换），所以「同一个 pid+created 的 VRChat」可以复用。
# 用之前一定再用当前样本做一次 GCM 认证 —— 命中缓存绝不等于可信。
KEY_CACHE_FILE = _user_data_dir() / "key_cache.json"   # 与 api_key.txt 同一个用户目录


def _disk_key_read(ident: "key_win.Identity") -> bytes | None:
    try:
        d = json.loads(KEY_CACHE_FILE.read_text(encoding="utf-8"))
        if int(d.get("pid") or 0) != ident.pid:
            return None
        if int(d.get("created") or 0) != ident.created:
            return None
        key = bytes.fromhex(str(d.get("key") or ""))
        return key if len(key) == gcm.KEY_LEN else None
    except (OSError, ValueError, TypeError):
        return None


def _disk_key_write(ident: "key_win.Identity", key: bytes) -> None:
    try:
        KEY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_CACHE_FILE.write_text(json.dumps({
            "pid": ident.pid, "created": ident.created,
            "key": key.hex(), "ts": int(time.time()),
            # 明文说明用：只有 fingerprint 可读，key 本身在这儿（用户自己机器、他自己的会话）
            "note": "vrchat livetranslate session key cache; delete this file to force a rescan",
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _disk_key_forget() -> None:
    try:
        KEY_CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass

# 回放窗口：取钥那十几秒里抓到的包要补一遍，否则名字表和开场语音会丢。
REPLAY_S = 5.0
MAX_REPLAY = 4096
# 与 `leg.SAMPLE_TARGET` 对齐：够了就开始扫，不用等收满。
SAMPLE_TARGET = 40
# pagefile 的 MFT 记录号只在**这个文件被重建之前**有效；给出来能省掉整表扫描
# （实测 67 s → 0 s）。优先级：环境变量 `VLT_PAGEFILE_MFT` > 缓存文件 > 自己扫。
# 缓存带卷序列号校验，读的时候还要过 `pagefile._take_hint` 的名字/大小校验：
# pagefile 改过大小就会被系统重建成新记录，旧记录号可能落到别的文件上。
HINT_ENV = "VLT_PAGEFILE_MFT"
#: 缓存写哪儿：先写可写目录（绿色版就是 exe 旁边，与 config.yaml 同处），
#: 写不进去再退回老位置 `%LOCALAPPDATA%\vlt`。实测打包版写 `%LOCALAPPDATA%`
#: 会被杀软拦掉（静默失败），于是每次都重扫 324 万条 MFT —— 多给一个落点。
HINT_NAME = "pagefile-hint.json"


def _hint_files() -> list[Path]:
    """缓存的候选落点（按优先级）。每次现算，免得 `app_dir()` 过早求值。"""
    out: list[Path] = []
    try:
        out.append(paths.app_dir() / HINT_NAME)
    except Exception:                          # noqa: BLE001 - 只是缓存，别炸
        pass
    legacy = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "vlt" / HINT_NAME
    if legacy not in out:
        out.append(legacy)
    return out
# 方向判定端口的刷新间隔。加速器的出站端口是临时端口，重连后会换；
# 与 `leg.ENDPOINT_REFRESH_S` 同值，但这里自己定义 —— 从 `leg` 取会循环导入。
DIR_REFRESH_S = 2.0
# 取钥失败后的冷却：失败原因多半是「pagefile 记录号过时 / 内存压缩开着」，
# 立刻重试只会再弹一次 UAC —— 用户看到的就是「一直弹窗但一直拿不到密钥」
# （实测 27 秒里连弹了 6 次）。同一进程冷却期内直接报错，不再拉起子进程。
ELEVATE_COOLDOWN_S = 25.0
# 让路等待的上限：一路扫描的生命周期是 240 s（keyd.DEFAULT_LIFETIME），让路的一方
# 必须比它长，否则「等到一半自己放弃、又去抢盘」。300 s 覆盖 240 + 定位 + 余量。
WAIT_SHARED_MAX_S = 300.0
# 轮询间隔：落盘是整文件覆盖写，2 s 足够，也不至于把磁盘唤醒得太勤。
WAIT_SHARED_POLL_S = 2.0


class KeyUnavailable(RuntimeError):
    """取不到密钥。`fatal=True` 表示再重试也没用（没提权 / 没定位到 pagefile）。"""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


#: 取钥失败过的进程 → 失败时刻（单调钟）。用来压住连弹 UAC。
_elevate_fail: dict[tuple[int, int], float] = {}
_elevate_lock = threading.Lock()


def _gate_elevation(ident: "key_win.Identity") -> None:
    """同一游戏进程刚失败过就先别弹 UAC，直接报还有多少秒。"""
    tag = (int(ident.pid), int(ident.created))
    with _elevate_lock:
        last = _elevate_fail.get(tag)
    if last is None:
        return
    wait = ELEVATE_COOLDOWN_S - (time.monotonic() - last)
    if wait > 0:
        raise KeyUnavailable(
            f"上一次取钥失败，{wait:.0f} 秒后再试（避免连续弹 UAC）")


def _note_elevation_failure(ident: "key_win.Identity") -> None:
    with _elevate_lock:
        _elevate_fail[(int(ident.pid), int(ident.created))] = time.monotonic()


def _clear_elevation_failure(ident: "key_win.Identity") -> None:
    with _elevate_lock:
        _elevate_fail.pop((int(ident.pid), int(ident.created)), None)


def _hint_from_env() -> int:
    raw = os.environ.get(HINT_ENV, "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _read_hint() -> "tuple[int, int]":
    """一次读出缓存里的 (记录号, 卷序列号)；没有缓存就是 `(0, 0)`。

    两个值必须来自**同一份**文件：分开遍历 `_hint_files()` 时，若首选那份只写了
    `mft_index`、兜底那份才有 `volume_serial`，就会拿 A 的记录号配 B 的序列号 ——
    而 `want = 0` 会让「这条缓存属于另一块盘」的校验被静默跳过，于是又把别的盘上
    的 MFT 记录号当成 pagefile（review #135 指出）。顺手也省掉一次文件读。
    """
    for path in _hint_files():
        try:
            data = json.loads(path.read_text("utf-8"))
            mft = int(data.get("mft_index") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if mft <= 0:
            continue
        try:
            serial = int(data.get("volume_serial") or 0)
        except (TypeError, ValueError):
            serial = 0
        return mft, serial
    return 0, 0


def _hint_mft() -> int:
    """pagefile.sys 的 MFT 记录号：环境变量 > 上次缓存 > 0（自己扫）。

    给了它 `find_pagefile` 就直接读那一条记录，省掉 324 万条整表扫描
    （实测 67 s → 0 s）。缓存只存卷序列号 + 记录号，不含任何凭据。
    """
    hint = _hint_from_env()
    if hint:
        return hint
    return _read_hint()[0]


def _save_hint(mft_index: int, volume_serial: int) -> "Path | None":
    """记住这次扫到的 pagefile MFT 记录号，返回真正写成功的那个文件。

    写不进就算了（不影响取钥），但要多试一个落点：实测打包版写
    `%LOCALAPPDATA%` 会被杀软静默拦掉，于是每次启动都重扫整表。
    """
    if mft_index <= 0:
        return None
    blob = json.dumps({"mft_index": int(mft_index),
                       "volume_serial": int(volume_serial or 0)},
                      ensure_ascii=False) + "\n"
    for path in _hint_files():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(blob, "utf-8")
        except OSError:
            continue
        return path
    return None


def _cached_serial() -> int:
    """缓存里记的卷序列号（0 = 没有缓存）。用来判断缓存是否还适用于当前系统盘。

    必须与 `_hint_mft()` 取自同一份文件，所以都走 `_read_hint()`。
    """
    return _read_hint()[1]


def samples_to_groups(samples) -> tuple[dict, dict]:
    """把 `leg` 的 `[(gcm.Flow, payload)]` 转成 `key_win` 要的口径。

    Photon 密文头是 `[peerID:2][0x01][challenge:4][nonce:12]`（见 `gcm.py` 顶部
    注释），所以 peerID 是 `payload[0:2]`、challenge 是 **`payload[3:7]`** ——
    不是 `[1:5]`：`payload[2]` 是固定的 `0x01` 标志位，把它算进 challenge 会让
    分组键漂移，也让 `_scan_near_challenges()` 拼的锚 `peer + 0x01 + chal`
    变成一串几乎每个包都对不上的字节 —— 白吃满 `MAX_CHALLENGE_HITS × 偏移`
    的预算却永远定位不到真正的密钥对象（实测：34 GiB 全扫、24 万候选全败）。

    同时返回 `(peerID, challenge) → Flow` 的反查表，因为最后构造 `gcm.KeyHit`
    还要知道这把 key 属于哪条流。
    """
    groups: dict[tuple[bytes, bytes], list[bytes]] = {}
    flows: dict[tuple[bytes, bytes], gcm.Flow] = {}
    for flow, payload in samples:
        if len(payload) < gcm.MIN_PACKET or payload[2] != 0x01:
            continue
        tag = (bytes(payload[0:2]), bytes(payload[3:7]))
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
#: 扫描单飞：同一台机器同一时刻只允许一次提权扫描
_SCAN_LOCK = threading.Lock()

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
        # 单飞锁是**模块级**的（见 _SCAN_LOCK）：多个采集实例共用一把
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

    def forget_key(self) -> None:
        """丢掉手里的密钥（含会话缓存）。

        为什么必须有：`acquire_key()` 会把 key 缓存在**采集对象**上，`warmup` 判定
        「连续 10 个包解不开」时只清了自己的 `_key` —— 下一次 `acquire_key()` 立刻把
        同一把死钥匙还回来，于是「失败→10 秒后重试→又失败」无限循环，
        界面上就是**永远「预热不可用」**（本机实测：找到钥匙 28 秒后进入这个死循环）。
        """
        self._key = None
        _disk_key_forget()
        try:
            self._cache.forget()
        except AttributeError:
            pass

    def _from_cache(self, ident: key_win.Identity, groups: dict, flows: dict):
        """会话缓存/磁盘缓存命中就零 UAC —— key 是进程级的，换房间不换。

        ⚠️ 别在 `self._cache.peek() is None` 时提前 return：那是**会话缓存**
        （挂在采集对象上），而「关一次翻译再开」就会新建采集实例，它的会话缓存
        是空的 —— 提前返回等于把磁盘缓存彻底废掉，于是每次都重扫 34 GiB
        （本机实测：开关一次翻译 = 3 分钟）。会话缓存只当快路径，磁盘才是兜底。
        """
        key = None
        if self._cache.peek() is not None:
            auth = gcm_win.GcmAuth()
            try:
                key = self._cache.lookup(ident, groups, auth=auth)
            finally:
                auth.close()
        if key is None:
            key = _disk_key_read(ident)
            if key is not None:
                auth2 = gcm_win.GcmAuth()
                try:
                    ok = key_win.verify_key(key, groups, auth=auth2)
                finally:
                    auth2.close()
                if not ok:
                    key = None
                    _disk_key_forget()          # 缓存里的钥匙打不开当前流量 → 立刻作废
        if key is None:
            return None
        self._status("info", "命中密钥缓存（key 是进程级的），跳过提权与整盘扫描")
        return make_hit(key, groups, flows)

    def _scan(self, ident: key_win.Identity, groups: dict, flows: dict) -> gcm.KeyHit:
        """分派：已是管理员就在本进程内扫，否则拉起一次性提权子进程（`keyd`）。

        两条路做的事完全一样（定位 pagefile → 驱逐脏页 → 扫锚点 → GCM 认证），
        区别只是权限来源：`\\.\PhysicalDriveN` 只给管理员 `GENERIC_READ`。
        提权只用于这**一次**读盘；密钥随即进 `SessionCache`，换房间不再弹 UAC。
        """
        # ⚠️ **单飞**：预热线程和翻译腿会同时来要钥匙，两边各自拉起提权子进程 =
        # 两个子进程同时扫 34 GiB（本机实测：日志里两路进度交错、IO 翻倍，
        # 而且先连上的那个把监听队列占满，另一个「没有交出结果就断开」）。
        # 这里加锁后，后到的那一方等前一方扫完，直接拿 `self._key`。
        with _SCAN_LOCK:
            if self._key is not None:
                return self._key
            cached = self._from_cache(ident, groups, flows)
            if cached is not None:
                self._key = cached
                return cached
            if keyd.is_admin():
                hit = self._scan_local(ident, groups, flows)
            else:
                hit = self._scan_via_keyd(ident, groups, flows)
            self._key = hit
            _disk_key_write(ident, hit.key)
            return hit

    def _wait_shared_key(self, ident: key_win.Identity, groups: dict, flows: dict,
                         *, max_s: float = WAIT_SHARED_MAX_S):
        """另一路正在扫：轮询它扫完落盘的密钥，**不重复读盘、不弹 UAC**。

        为什么等磁盘而不是等内存：撞锁的「另一路」多半在**另一个进程**里
        （翻译腿 / 右栏面板各有自己的采集实例），实例变量共享不了；而扫完的那一路
        一定会 `_disk_key_write()`，于是磁盘就是它们之间的信箱。

        命中后**仍要过 GCM 复验**：缓存里的钥匙打不开当前流量时必须继续等，
        不能当成命中（也先别删 —— 可能是自己这批样本有问题，别毁掉别人的成果）。

        返回 None 表示「让路到头了」，调用方应自己扫一次。
        """
        self._status("info", "另一个取钥扫描正在进行（本机只允许一路读盘）"
                            "—— 等它的结果，不再弹 UAC")
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            if self._key is not None:
                return self._key
            key = _disk_key_read(ident)
            if key is not None:
                auth = gcm_win.GcmAuth()
                try:
                    ok = key_win.verify_key(key, groups, auth=auth)
                finally:
                    auth.close()
                if ok:
                    self._status("info", "另一路扫完了：密钥命中（复用它落盘的结果）")
                    self._cache.store(ident, key)
                    return make_hit(key, groups, flows)
            if not keyd.scan_busy():
                # 那一路已收工却没留下能用的钥匙（失败/超时）→ 让路到此为止。
                self._status("info", "另一路扫描结束了但没拿到密钥 —— 自己再扫一次")
                return None
            time.sleep(WAIT_SHARED_POLL_S)
        self._status("info", f"等了 {max_s:.0f} 秒另一路还没结果 —— 自己扫一次")
        return None

    def _fresh_samples(self) -> list[tuple]:
        """最新几秒的密文，转成与 `acquire_key()` 同口径的样本 `[(Flow, payload)]`。"""
        try:
            dgs = self.recent_cipher()
        except Exception:                                 # noqa: BLE001 - 诊断不该抛
            return []
        return [(gcm.Flow.of(dg.src, dg.dst), dg.payload)
                for dg in dgs if getattr(dg, "is_encrypted", False)]

    def _scan_via_keyd(self, ident: key_win.Identity, groups: dict,
                       flows: dict) -> gcm.KeyHit:
        """非管理员路径：把样本交给提权子进程，它只回一个 32 字节候选。"""
        # 弹 UAC **之前**先探互斥体：另一路正在扫时等它的结果，别自己再扫一遍、
        # 也别再弹一次窗。扫描要 3~4 分钟，而 warmup 每 10 秒重试一次 ——
        # 把撞锁当失败就是「失败 → 10 s 后重试 → 又撞锁」的死循环，
        # 本机实测 22 秒里连弹 5 次 UAC（用户看到的就是一直弹窗、一直没钥匙）。
        if keyd.scan_busy():
            hit = self._wait_shared_key(ident, groups, flows)
            if hit is not None:
                return hit
            # 让路没等到结果（那一路失败/超时）：自己上，别把自己的机会也让掉。
        # 让路之后再过一次冷却：上一次真失败了（25 秒内）就别再弹窗。
        _gate_elevation(ident)
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
            _note_elevation_failure(ident)
            raise KeyUnavailable(str(exc)) from exc
        if not reply.get("ok"):
            _note_elevation_failure(ident)
            # 失败也要把子进程定位到的记录号写回：hint 过期时正是靠这次全表扫描
            # 才纠正过来的，不写回的话下次还得再扫 324 万条 MFT、再弹一次 UAC。
            # 但 `pagefile_mft == 0` 表示子进程在 `find_pagefile()` 之前就失败了
            # （打不开盘 / 读不到引导扇区），压根没有记录号可存 —— 那时报「没能
            # 缓存下来」等于把另一种故障说成缓存问题，故只在拿到记录号时才写。
            mft = int(reply.get("pagefile_mft") or 0)
            if mft > 0:
                saved = _save_hint(mft, int(reply.get("volume_serial") or 0))
                if saved is None:
                    self._status(
                        "info", "⚠️ pagefile 记录号没能缓存下来（下次要多扫一遍 MFT）")
                else:
                    self._status("info", f"pagefile 记录号已缓存：{saved}")
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
            # ★ 再用**最新**流量验一遍。上面那批样本是扫描**开始**时抓的，扫描要
            # 3~4 分钟，这期间换过世界的话它们照样能解开，但实时流一个都解不开 ——
            # 本机实测（gui_20261106_234031.log）：23:43:47 密钥命中、开始解码，
            # 到 23:52 全是「音频消息 0、帧 0」，然后判「连续 10 个包解不开」再扫
            # 一整轮。用新世界的包当场复验，就能立刻改口说「钥匙过期了」而不是
            # 装着钥匙干等。
            fresh, _fresh_flows = samples_to_groups(self._fresh_samples())
            if fresh and not key_win.verify_key(key, fresh, auth=auth, aad_kind=AAD_KIND):
                raise KeyUnavailable(
                    f"子进程交回的密钥（指纹 {key_win.key_fingerprint(key)}）解不开**最新的**"
                    "流量：扫描这几分钟里换过世界，钥匙已经过期。重新取一次"
                    "（这次会用新世界的样本扫）"
                )
            tag = self._tag_for(key, groups, auth)
        finally:
            auth.close()

        self._cache.store(ident, key)
        _clear_elevation_failure(ident)
        # 子进程顺带报告了它定位页文件用的记录号；存下来下次就不用再扫 324 万条 MFT。
        saved = _save_hint(int(reply.get("pagefile_mft") or 0),
                           int(reply.get("volume_serial") or 0))
        if saved is None:
            self._status("info", "⚠️ pagefile 记录号没能缓存下来（下次要多扫一遍 MFT）")
        else:
            self._status("info", f"pagefile 记录号已缓存：{saved}")
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
        saved = _save_hint(pf.mft_index, int(getattr(volume, "serial", 0) or 0))
        if saved is None:
            self._status("info", "⚠️ pagefile 记录号没能缓存下来（下次要多扫一遍 MFT）")
        else:
            self._status("info", f"pagefile 记录号已缓存：{saved}")
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
