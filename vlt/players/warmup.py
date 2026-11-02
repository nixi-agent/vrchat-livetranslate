"""预热：**没开始翻译**时也抓包、取钥、解析玩家名。

## 为什么需要

名字（Event 42/253/255 的 `actor → usr_id → displayName`）只在「加入 / 属性变更」
时广播一次。以前抓包要等用户点「开始翻译」—— 那时可能已经错过整张名单，只能靠
`output_log` 兜底（没有 actor 号）。

预热把「抓包」从翻译腿里独立出来：程序发现 VRChat 进程后就开 socket、取一次钥，
之后**持续解密并只解析名字**（不解码 opus、不建任何翻译会话，CPU 接近零）。
点开始翻译时直接复用 socket + 密钥 + 名字表，既不用重新授权，也不用再扫 11 秒内存。

## 权限与生命周期（与现有承诺一致）

* 预热是分玩家的**前提**（开 `mode: decrypt` 就自动预热，没有单独开关）；本模块也是
  **唯一**的提权入口 —— 翻译腿只借用 socket/密钥，绝不自己起助手；
* 一次授权把「开 socket + 采样 + 扫内存」放进同一个**短命**提权助手
  （`helper.bootstrap`）：干完就退，主程序只拿到 fd / 候选，永远拿不到原始内存；
  只有游戏重启/密钥失效后的重扫才会再起一次（`helper.scan_once`）；
* socket 的 fd 留在主进程里，助手退出后照样继续收包；停止翻译也不关，
  所以「停止后继续记名字」不需要再授权；
* 游戏重启（pid/starttime 变了）→ 旧密钥失效 → 自动重新采样 + 重扫。

## 读者只有一个

预热线程与翻译腿都通过 `drain()` 取帧（一把锁 + `_leased` 标志）。腿借用期间
预热线程**不读** socket，避免两个读者抢包。

## 密钥失效判定

连续 `MAX_DECRYPT_FAILURES` 个加密包都解不开 → 认为游戏重启/换号，清掉密钥并重扫
（名字表保留：新会话里 actor 会重新广播，旧的按 actor 复用规则会被覆盖）。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from . import capture_linux, gcm, guard, names, pending, photon, positions, procfs
from ..players_settings import MODE_DECRYPT, PlayersSettings

_WIN = os.name == "nt"

if _WIN:                                             # pragma: no cover - 平台分派
    from . import procfs_win, win_capture

    def _pick_host() -> object | None:
        """Windows：进程发现走 `procfs_win`（`procfs` 读的是 `/proc`）。"""
        return procfs_win.pick_host(procfs_win.find_vrchat())

    def _udp_ports(pid: int) -> set[int]:
        from . import dgram

        return dgram.local_udp_ports() or set()

    def _assess() -> object:
        """Windows 的闸门判断没有 Linux 那套内核证据可查。

        EAC 在 Windows 上用 `ObRegisterCallbacks` 剥 `PROCESS_VM_READ`，但本实现
        **不读游戏内存**（读裸盘上的 pagefile），所以这里恒判 legacy，不阻塞。
        """
        return guard.Verdict(
            level="legacy",
            reasons=("Windows：EAC 拦截的是读游戏内存，本实现只读 pagefile 的原始扇区",),
            advice="只读、不 attach、不注入；换世界不重扫，只有游戏重启才再弹一次 UAC。",
        )

    def _default_drain(cap, *, timeout: float = 0.2):
        return cap.drain(timeout=timeout)

else:
    _pick_host = lambda: procfs.pick_host(procfs.find_vrchat())   # noqa: E731
    _udp_ports = procfs.udp_ports
    _assess = lambda: guard.assess(guard.collect())              # noqa: E731
    _default_drain = capture_linux.drain

PHASE_OFF = "off"
PHASE_WAITING = "waiting_game"          # 没有 VRChat 进程
PHASE_BLOCKED = "blocked"               # EAC 闸门拒绝
PHASE_UNAUTHORIZED = "unauthorized"     # pkexec 被拒 / 助手起不来
PHASE_ACQUIRING = "acquiring"           # 已授权、正在取密钥（还没拿到）
PHASE_RECORDING = "recording"           # 有密钥，正在记名字
PHASE_FAILED = "failed"                 # 取钥失败（重试也失败）

SAMPLE_TARGET = 40
MAX_DECRYPT_FAILURES = 10
VOICE_REPLAY_S = 5.0
POLL_S = 2.0
DRAIN_S = 0.25
LOCAL_PORTS_REFRESH_S = 5.0
WIN_SAMPLE_TIMEOUT = 20.0          # Windows：等第一批加密包当样本的上限


class _WinBootstrap:
    """Windows 版 bootstrap 的返回值（对位 `helper.Bootstrap`）。

    `socket` 槽位放的是 `win_capture._WinCapture` —— 它自己就是「抓包句柄」，
    `drain/close/acquire_key/set_local_ports` 都由它提供，所以预热器的其余代码
    不需要知道 Linux 那边的裸 fd 概念。密钥这一步**不在这里做**：交给
    `ensure_key()` 走统一路径（缓存命中/扫盘都在那儿）。
    """

    def __init__(self, cap, samples: list, packets: list) -> None:
        self.socket = cap
        self.samples = samples
        self.packets = packets
        self.key = None
        self.blocked = False
        self.detail = ""


class WarmupLease:
    """翻译腿向预热器「借用」抓包/密钥/名字表的一张小票。"""

    def __init__(self, warmup: "PlayerWarmup") -> None:
        self._warmup = warmup

    def drain(self, timeout: float = 0.2) -> list[tuple[float, bytes]]:
        return self._warmup.drain(timeout)

    # ⚠️ 这里**故意不提供** `ensure_key()`：取钥/提权只能由预热线程自己做。
    # 翻译腿只读 `key` 缓存，避免任何一次误用又弹出第二个 pkexec。

    @property
    def key(self):
        return self._warmup.key

    @property
    def host(self):
        return self._warmup.host

    def names_copy(self) -> names.NameTable:
        return self._warmup.names_copy()

    def user_of(self, actor: int) -> str | None:
        return self._warmup.user_of(actor)

    @property
    def positions(self) -> positions.PositionTable:
        """位置表（**活对象**）：腿直接用这张表算距离，不必自己再喂一份。"""
        return self._warmup.positions

    def status(self) -> dict:
        return self._warmup.status()

    def recent_cipher(self, seconds: float = VOICE_REPLAY_S) -> list[capture_linux.Datagram]:
        return self._warmup.recent_cipher(seconds)

    def bind_status(self, provider) -> None:
        """翻译腿接管后，状态页用**腿的实时计数**（预热自己的计数会冻结）。"""
        self._warmup.bind_leased_status(provider)

    def adopt(self, table: names.NameTable) -> None:
        """停止翻译时把腿的名字表交还预热器（否则状态页会显示成 0 人）。"""
        self._warmup.adopt_names(table)

    def set_local_ports(self, ports) -> None:
        """把游戏端口集合转给底层抓包对象（Windows 判方向要用它）。"""
        self._warmup.set_local_ports(ports)

    def release(self) -> None:
        self._warmup.release()


class PlayerWarmup:
    """常驻预热器（App 级）。`start()` 起线程，`close()` 收摊。"""

    def __init__(self, settings: PlayersSettings, *, port: int = 27002,
                 on_status: Callable[[str, str], None] | None = None,
                 find_host: Callable[[], procfs.ProcInfo | None] | None = None,
                 bootstrap: Callable[[procfs.ProcInfo], object] | None = None,
                 open_capture: Callable[[procfs.ProcInfo], object] | None = None,
                 scan: Callable[[procfs.ProcInfo, str], dict] | None = None,
                 guard_check: Callable[[], object] | None = None,
                 drain_fn: Callable[..., list[tuple[float, bytes]]] | None = None,
                 poll_s: float = POLL_S) -> None:
        self.settings = settings
        self._port = port
        self._on_status = on_status or (lambda *_a: None)
        self._find_host = find_host or _pick_host
        self._bootstrap = bootstrap or self._default_bootstrap
        # 下面两个只用于「密钥失效后的重扫」（那时 socket 已经在本进程里）
        self._open_capture = open_capture or self._default_open
        self._scan = scan or self._default_scan
        self._guard = guard_check or _assess
        self._drain_fn = drain_fn or _default_drain
        self._poll_s = max(0.02, float(poll_s))

        self._lock = threading.RLock()          # socket 读 + 租借状态
        self._names_lock = threading.Lock()
        self._key_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._socket: object | None = None
        self._host: procfs.ProcInfo | None = None
        self._local_ports: set[int] | None = None
        self._ports_at = 0.0
        self._leased = False
        self._auth_failed = False

        self._key: gcm.KeyHit | None = None
        self._opener = None
        self._samples: list[tuple[gcm.Flow, bytes]] = []
        self._fail_streak = 0
        self._next_scan = 0.0
        # 拿到密钥之前收到的包：装好密钥后**补解一遍**（加入世界那批名单就在里面）
        self._pre_key: list[capture_linux.Datagram] = []
        self._retry_pending = False

        self._names = names.NameTable()
        # 位置表也归预热器：没开始翻译时右栏要按距离排序，就靠它在后台一直喂。
        # 翻译腿通过租约借**同一个对象**（不是副本）—— 那边只读，写还是这里和
        # 腿自己的 collector（表内部有锁）。
        self._pos = positions.PositionTable()
        # 本机账号的 usr_…（从 VRChat 的 output_log 读一次）。名字表里也有自己，
        # 不剔就会出现两行「我」（一行硬编码、一行来自事件表）。
        # 注意：Photon 出向 RaiseEvent **不带**发送者 actor，出向 Event 12 的
        # body 前 4 字节是服务器时间而不是 actor，所以只能走 user id。
        self._log_scan: tuple[dict[str, str], str] | None = None
        self._frag: dict[str, photon.Fragmenter] = {
            "in": photon.Fragmenter(), "out": photon.Fragmenter()}
        self._world = ""
        self._buffer = pending.PreKeyBuffer(
            max_bytes=max(1, int(settings.prewarm_buffer_mb)) << 20, world_ready=True)

        self._phase = PHASE_OFF
        self._detail = ""
        # 翻译腿借用期间由它提供实时计数（预热线程此时暂停读包，自己的计数会冻结）
        self._leased_status = None
        # 统计（状态页/日志用；名字本身不打印）
        self.name_events = 0
        self.packets = 0
        self.decrypted = 0
        self.rescans = 0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self.settings.mode != MODE_DECRYPT:
            self._set_phase(PHASE_OFF, "分玩家已关闭")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        # 名字兜底表**不能在这里灌**：`_scan_vrchat_log()` 要整份读 output_log
        # （可达数 MB + 全文件正则），而 `start()` 是在 GUI 线程里（保存设置 / 点开始
        # 翻译）同步跑的 —— 放这儿会卡界面。改到 `_run()` 的首轮做（见那里）。
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="vlt-players-warmup")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._set_phase(PHASE_OFF, "预热已停止")

    def reconfigure(self, settings: PlayersSettings) -> None:
        """配置变了（设置页保存）：按需启停，不必重启程序。"""
        was_on = self.settings.mode == MODE_DECRYPT
        self.settings = settings
        if settings.mode != MODE_DECRYPT:
            if was_on:
                self.close()
            return
        if not was_on or self._thread is None or not self._thread.is_alive():
            self.start()

    def retry(self) -> None:
        """用户点「重新授权」：清掉失败/拒绝状态，由**预热线程**重开（不跨线程动 socket）。"""
        self._auth_failed = False
        self._retry_pending = True

    # ------------------------------------------------------------ 对外查询

    @property
    def key(self) -> gcm.KeyHit | None:
        return self._key

    @property
    def host(self) -> procfs.ProcInfo | None:
        return self._host

    def available(self) -> bool:
        with self._lock:
            return self._socket is not None

    def names_copy(self) -> names.NameTable:
        with self._names_lock:
            return self._names.copy()

    @property
    def positions(self) -> positions.PositionTable:
        """位置表（**活对象**，不是副本）。未开始翻译时也在喂。"""
        return self._pos

    def user_of(self, actor: int) -> str | None:
        """actor → user_id（活查）。翻译名单按 user_id 存，路由每块音频都要问一次。

        表里只有**当前世界**的玩家（换世界就 `clear()`），所以拿不到就是"还不知道"，
        调用方按自己的保守策略处理（`only` 先不翻、`except` 照翻）。
        """
        with self._names_lock:
            record = self._names.get(actor)
        return record.user_id if record is not None and record.user_id else None

    def _scan_vrchat_log(self) -> tuple[dict[str, str], str]:
        """读一次 VRChat 的 `output_log`：`usr_ → 名字` 兜底表 + 本机账号 id。

        只读一次（结果缓存）：GUI 每秒都会问一次右栏数据，不能每次去读几 MB 的文件。
        """
        if self._log_scan is None:
            table: dict[str, str] = {}
            self_uid = ""
            try:
                latest = names.latest_output_log(names.default_log_dir())
                if latest is not None:
                    table = names.parse_output_log(latest)
                    self_uid = names.parse_self_user_id(latest)
            except Exception:                       # noqa: BLE001 - 读不到日志不算故障
                table, self_uid = {}, ""
            self._log_scan = (table, self_uid)
        return self._log_scan

    def _load_log_names(self) -> None:
        """把 `output_log` 的名字兜底表灌进名字表（只灌空表）。

        谁该看这张表：**事件里没名字**的记录（名字事件偶尔只给 `user_id`）。它**不会**
        多出行 —— `NameTable(log_names)` 只填 `_log_names`，`snapshot()` 只看
        `_by_actor`，所以右栏不会「一进来就列出整间房」，那要有进房整表事件（255）
        或每人各自的进房事件 253 才行。

        在预热线程首轮调用（不是 `start()`）：读日志是几 MB 的同步 IO，放 GUI 线程会卡。
        """
        log_table, _self_uid = self._scan_vrchat_log()
        if not log_table:
            return
        with self._names_lock:
            if not self._names:
                self._names = names.NameTable(log_table)

    def _self_user_id(self) -> str:
        """本机账号的 `usr_…`（认不出就是空串）。

        为什么不用 actor 号：Photon **出向** RaiseEvent 不带发送者 actor，出向
        Event 12 的 body 前 4 字节是服务器时间而不是 actor（见 `positions.py`
        开头的实测布局），所以从协议里认不出「我是哪个 actor」。VRChat 自己的
        日志里有 `Preforming post login checks for 名字 (usr_…)`，那是唯一稳定的
        自我标识；名字事件表里自己的 `user_id` 与它一致，据此剔掉重复行。

        **只读缓存**：真正的读取在预热线程首轮（`_load_log_names`）。这里若也去读，
        GUI 每秒调一次 `player_rows()` 就会反复卡在文件 IO 上；缓存还没填好时返回
        空串，右栏那一秒会多出一行自己，下一秒就正常。
        """
        cached = self._log_scan
        return "" if cached is None else cached[1]

    def player_rows(self) -> list[dict]:
        """房间玩家（名字表 + 位置表拼出来），给 GUI 右栏用。

        键名不叫 `players`：那个键是**人数**（旧状态页在用），改成列表会破坏它。
        借出期间用翻译腿的实时状态覆盖 `speaking`/`translating`（只有它知道
        谁真的在说话、谁已经有 ASR 会话）。
        """
        with self._names_lock:
            snapshot = self._names.snapshot()
        self_uid = self._self_user_id()
        rows: list[dict] = []
        for actor, record in snapshot.items():
            if self_uid and (record.user_id or "").lower() == self_uid:
                continue                            # 自己由右栏硬编码那一行代表
            rows.append({
                "actor": actor,
                "user_id": record.user_id or "",
                "name": record.display_name or "",
                "distance": self._pos.distance(actor),
                "self": False,
                "speaking": False,
                "translating": False,
            })
        live = self._leased_status
        if live is not None:
            try:
                detail = (live() or {}).get("rows") or {}
            except Exception:                       # noqa: BLE001 - 状态查询不该影响抓包
                detail = {}
            for row in rows:
                patch = detail.get(row["actor"])
                if patch:
                    row["speaking"] = bool(patch.get("speaking"))
                    row["translating"] = bool(patch.get("translating"))
        return rows

    def status(self) -> dict:
        with self._names_lock:
            players = len(self._names)
        name_events = self.name_events
        decrypted = self.decrypted
        # 借出期间：用翻译腿的实时计数（否则状态页会停在「借用前的数字」）
        provider = self._leased_status
        if provider is not None:
            try:
                live = provider() or {}
            except Exception:                       # noqa: BLE001 - 状态查询不该影响抓包
                live = {}
            players = int(live.get("players", players))
            name_events = int(live.get("name_events", name_events))
            decrypted = int(live.get("decrypted", decrypted))
        return {
            "phase": self._phase,
            "detail": self._detail,
            "players": players,
            "name_events": name_events,
            "packets": self.packets,
            "decrypted": decrypted,
            "rescans": self.rescans,
            "key": self._key.fingerprint if self._key else "",
            "host_pid": self._host.pid if self._host else 0,
            "player_rows": self.player_rows(),
        }

    # ------------------------------------------------------------ 租借（给翻译腿）

    def lease(self) -> WarmupLease | None:
        with self._lock:
            if self._socket is None or self._leased:
                return None
            self._leased = True
        return WarmupLease(self)

    def release(self) -> None:
        with self._lock:
            self._leased = False
        # 腿已经停了，它的回调读数会永远冻在最后一帧；解绑掉，状态页才会回到
        # 预热器自己的计数（否则「停止翻译」之后数字不动，看着像卡死）。
        self._leased_status = None

    def bind_leased_status(self, provider) -> None:
        self._leased_status = provider

    def adopt_names(self, table: names.NameTable) -> None:
        if table is None:
            return
        with self._names_lock:
            self._names = table.copy()

    def drain(self, timeout: float = 0.2) -> list[tuple[float, bytes]]:
        with self._lock:
            sock = self._socket
            if sock is None:
                return []
            try:
                return self._drain_fn(sock, timeout=timeout)
            except OSError:
                return []

    def set_local_ports(self, ports) -> None:
        """把游戏端口集合转给底层抓包对象。

        只有 Windows 的 `_WinCapture` 有这个口子（UU 下两端都不是 27002，方向
        判定要靠「本端端口在不在这个集合里」）；POSIX 的助手会话没有，直接跳过。
        """
        with self._lock:
            sock = self._socket
        forward = getattr(sock, "set_local_ports", None)
        if forward is None:
            return
        try:
            forward(ports)
        except Exception:                               # noqa: BLE001 - 端口表查不到不影响抓包
            pass

    def recent_cipher(self, seconds: float = VOICE_REPLAY_S) -> list[capture_linux.Datagram]:
        """最近几秒的密文（给翻译腿回放语音；名字早已解析进表）。"""
        since = time.time() - max(0.0, seconds)
        return self._buffer.snapshot_recent(since)

    # ------------------------------------------------------------ 密钥

    def ensure_key(self, samples=None, *, deadline_s: float = 40.0):
        """有缓存就用缓存；否则**起短命助手扫一次**。失败返回 None。"""
        with self._key_lock:
            if self._key is not None:
                return self._key
            host = self._host
            usable = list(samples or [])
            if len(usable) < 2:
                # 翻译腿刚接管时可能只有 1 个样本：用预热自己攒的
                usable = list(self._samples)
            if host is None or len(usable) < 2:
                return None
            self._set_phase(PHASE_ACQUIRING, "正在获取密钥（扫描游戏内存，约 1~20 秒）…")
            for needle in ("q", "i"):
                if self._stop.is_set():
                    return None
                try:
                    reply = self._scan(host, needle)
                except Exception as exc:                 # noqa: BLE001 - 授权/助手问题
                    self._auth_failed = True
                    self._samples.clear()
                    self._set_phase(PHASE_UNAUTHORIZED, f"授权失败：{exc}")
                    return None
                if not reply.get("ok"):
                    if reply.get("blocked"):
                        self._set_phase(PHASE_FAILED, f"读内存被拒绝：{reply.get('error')}")
                        return None
                    print(f"[warmup] ⚠️ 扫描失败（needle={needle}）：{reply.get('error')}",
                          flush=True)
                    continue
                candidates = [bytes.fromhex(h) for h in reply.get("candidates", [])]
                hit = gcm.validate_candidates(candidates, usable)
                if hit is not None:
                    self._install_key(hit)
                    return hit
            self._set_phase(PHASE_FAILED, "没找到能通过 GCM 认证的密钥")
            return None

    def _install_key(self, hit: gcm.KeyHit) -> None:
        self._key = hit
        self._opener = gcm.opener(hit.key, hit.layout)
        self._fail_streak = 0
        # 把「拿到密钥之前」的包补解一遍：加入世界时的 253/255 名单就在这里，
        # 不补的话预热会永远错过整张名单（实测就是这个现象）。
        pending_frames, self._pre_key = self._pre_key, []
        for dg in pending_frames:
            self._decrypt_into(dg, count_failure=False)
        with self._names_lock:
            count = len(self._names)
        print(f"[warmup] 密钥就绪（指纹 {hit.fingerprint}），开始记录玩家名"
              f"（已识别 {count} 位）", flush=True)
        self._set_phase(PHASE_RECORDING, "")

    # ------------------------------------------------------------ 线程主体

    def _run(self) -> None:
        self._load_log_names()
        while not self._stop.is_set():
            host = self._find_host()
            if host is None:
                self._drop_capture()
                if self._host is not None:
                    self._host = None
                    self._set_phase(PHASE_WAITING, "等待 VRChat 进程…")
                elif self._phase == PHASE_OFF:
                    self._set_phase(PHASE_WAITING, "等待 VRChat 进程…")
                self._stop.wait(self._poll_s)
                continue
            if self._retry_pending:
                self._retry_pending = False
                self._select_new_host(host, force=True)
            elif self._host is None or (host.pid, host.starttime) != (
                    self._host.pid, self._host.starttime):
                self._select_new_host(host)
            if self._socket is None:
                # 授权失败：不反复弹密码，等用户点「重新授权」或游戏重启
                self._stop.wait(min(self._poll_s, 1.0))
                continue
            self._refresh_local_ports()
            with self._lock:
                leased = self._leased
            if leased:
                self._stop.wait(0.05)
                continue
            frames = self.drain(DRAIN_S)
            if frames:
                self._process(frames)
            if self._key is None and len(self._samples) >= 2 and time.monotonic() >= self._next_scan:
                self.ensure_key()
                # 失败别立刻重扫：等一轮（10s），避免把助手/日志刷爆
                self._next_scan = time.monotonic() + 10.0

    def _select_new_host(self, host: procfs.ProcInfo | None = None, *,
                         force: bool = False) -> None:
        host = host or self._find_host()
        if host is None:
            return
        had_host = self._host is not None
        self._drop_capture()
        self._host = host
        if had_host:
            # 第一次绑定时不能清：那里面是预热线程首轮从 output_log 灌进来的名字兜底表。
            with self._names_lock:
                self._names.clear()
        self._pos.clear_actors()
        self._key = None
        self._opener = None
        self._samples.clear()
        self._fail_streak = 0
        self._pre_key.clear()
        self._world = ""
        self._frag = {"in": photon.Fragmenter(), "out": photon.Fragmenter()}
        self._buffer = pending.PreKeyBuffer(
            max_bytes=max(1, int(self.settings.prewarm_buffer_mb)) << 20, world_ready=True)
        self._next_scan = 0.0
        if self._auth_failed and not force:
            self._set_phase(PHASE_UNAUTHORIZED, "未授权（可点「重新授权」）")
            return
        verdict = self._guard()
        if getattr(verdict, "level", "") == "blocked":
            reasons = "；".join(getattr(verdict, "reasons", []) or [])
            self._set_phase(PHASE_BLOCKED, f"EAC 闸门拒绝：{reasons}")
            return
        # **一次授权**做完：开抓包 + 采样 + 扫内存（同一个短命助手）。
        # 以前是「开抓包一个助手 + 扫内存一个助手」→ 用户被连着要两次密码。
        self._set_phase(PHASE_ACQUIRING, "正在等待系统授权…")
        try:
            result = self._bootstrap(host)
        except Exception as exc:                          # noqa: BLE001
            self._auth_failed = True
            self._set_phase(PHASE_UNAUTHORIZED, f"授权失败：{exc}")
            return
        with self._lock:
            self._socket = result.socket
        if result.socket is None:
            self._auth_failed = True
            self._set_phase(PHASE_UNAUTHORIZED, result.detail or "没有拿到抓包 fd")
            return
        self._auth_failed = False
        self._samples = list(getattr(result, "samples", []) or [])[:SAMPLE_TARGET]
        # bootstrap 采到的包要留着：里面就有加入世界的名单（装好密钥后补解）
        for dg in getattr(result, "packets", []) or []:
            self._buffer.add(dg)
            if len(self._pre_key) < 4096:
                self._pre_key.append(dg)
        key = getattr(result, "key", None)
        if key is not None:
            self._install_key(key)
        elif getattr(result, "blocked", False):
            self._set_phase(PHASE_FAILED, result.detail or "读内存被拒绝")
        else:
            self._set_phase(PHASE_ACQUIRING,
                            result.detail or "已授权，正在获取密钥（约 1~20 秒）…")

    def _drop_capture(self) -> None:
        with self._lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _refresh_local_ports(self) -> None:
        host = self._host
        now = time.monotonic()
        if host is None or now - self._ports_at < LOCAL_PORTS_REFRESH_S:
            return
        self._ports_at = now
        try:
            self._local_ports = _udp_ports(host.pid) or None
        except OSError:
            self._local_ports = None
        if _WIN and self._local_ports:
            # Windows 的方向判定要喂给 cap（UU 下两端都不是 27002）
            cap = self._socket
            if cap is not None:
                try:
                    cap.set_local_ports(self._local_ports)
                except Exception:                          # noqa: BLE001 - 端口刷新不该拖垮抓包
                    pass

    def _process(self, frames: list[tuple[float, bytes]]) -> None:
        for ts, raw in frames:
            # Windows 的 drain 已经拆好包（返回 Datagram），Linux 返回裸帧要自己解
            dg = raw if _WIN else capture_linux.parse_frame(
                raw, port=self._port, local_ports=self._local_ports)
            if dg is None or not dg.is_encrypted:
                continue
            self.packets += 1
            if len(self._samples) < SAMPLE_TARGET:
                self._samples.append((gcm.Flow.of(dg.src, dg.dst), dg.payload))
            self._buffer.add(dg)
            if self._key is None:
                if len(self._pre_key) < 4096:
                    self._pre_key.append(dg)
                continue
            self._decrypt_into(dg)

    def _decrypt_into(self, dg: capture_linux.Datagram, *, count_failure: bool = True) -> None:
        opener = self._opener
        plain = opener(dg.payload) if opener is not None else None
        if plain is None:
            if not count_failure:
                return
            self._fail_streak += 1
            if self._fail_streak >= MAX_DECRYPT_FAILURES:
                # 连续解不开 = 游戏重启/换号：清钥重扫（名字表保留）
                self.rescans += 1
                print(f"[warmup] ⚠️ 连续 {self._fail_streak} 个包解不开 → "
                      f"密钥失效，重新取钥（第 {self.rescans} 次）", flush=True)
                self._key = None
                self._opener = None
                self._samples.clear()
                self._fail_streak = 0
                self._next_scan = 0.0
                self._set_phase(PHASE_ACQUIRING, "密钥失效，正在重新获取…")
            return
        self._fail_streak = 0
        self.decrypted += 1
        self._on_plaintext(dg, plain)

    def _on_plaintext(self, dg: capture_linux.Datagram, plain: bytes) -> None:
        commands, _err = photon.parse_commands(plain)
        fragmenter = self._frag[dg.direction]
        for cmd in commands:
            for payload in fragmenter.feed(cmd):
                self._on_message(payload, ts=dg.ts)

    def _on_message(self, payload: bytes, *, ts: float | None = None) -> None:
        msg, _skip = photon.parse_message_at(payload)
        if msg is None:
            return
        # 位置：Event 12（移动/IK）。入向的是别人的；**出向的是自己的**，
        # 包在 RaiseEvent op 253 里（param 244=事件码, 245=数据）。
        # 预热阶段就喂，右栏才能「没开始翻译也按距离排序」。
        if msg.kind == photon.TYPE_EVENT and msg.code == 12:
            actor = photon.scan_actor(msg.payload)
            if actor:
                self._pos.observe(actor, msg.payload, ts=ts)
        elif msg.kind == photon.TYPE_OP_REQUEST and msg.code == 253:
            ev = msg.param(244)
            data = msg.param(245)
            if (ev is not None and isinstance(ev.value, int) and ev.value == 12
                    and data is not None
                    and isinstance(data.value, (bytes, bytearray))):
                body = bytes(data.value)
                self._pos.observe_body(0, body, ts=ts, is_self=True)
        if msg.kind == photon.TYPE_EVENT and (
                msg.code in names.NAME_EVENT_CODES or msg.code == names.LEAVE_EVENT):
            parsed, _diag = ([], {})
            if msg.code != names.LEAVE_EVENT:
                parsed, _diag = names.parse_records_ex(msg.payload)
            with self._names_lock:
                self._names.feed(msg.payload, code=msg.code, records=parsed)
            self.name_events += 1
            return
        if msg.kind == photon.TYPE_OP_REQUEST and msg.code == photon.EVENT_JOIN_WORLD:
            world = photon.joined_world(payload)
            if world and world != self._world:
                if self._world:
                    with self._names_lock:
                        self._names.clear()
                    self._pos.clear_actors()
                    self._frag = {"in": photon.Fragmenter(), "out": photon.Fragmenter()}
                self._world = world

    # ------------------------------------------------------------ 状态

    def on_status(self, level: str, message: str) -> None:
        """给抓包腿用的公开状态出口。

        `_WinCapture` 会通过 `leg.on_status(...)` 报告取钥进度；预热这边原来只有
        私有的 `_on_status`，于是那些文案全被 `except` 吞掉、界面上什么都看不到。
        """
        try:
            self._on_status(level, message)
        except Exception:                                 # noqa: BLE001 - 状态回调不该影响抓包
            pass

    def _set_phase(self, phase: str, detail: str) -> None:
        if phase == self._phase and detail == self._detail:
            return
        self._phase = phase
        self._detail = detail
        try:
            self._on_status(phase, detail)
        except Exception:                                 # noqa: BLE001 - 状态回调不该影响抓包
            pass

    # ------------------------------------------------------------ 默认实现
    #
    # Linux：走 `helper` 助手（pkexec 提权 + process_vm_readv 扫内存）。
    # Windows：抓包本身不要提权（npcap），只有「读裸盘上的 pagefile」那一步要一次性
    # UAC；`win_capture._WinCapture` 把「抓包 + 采样 + 取钥」三件事包在同一个对象上，
    # 所以这里三个入口都围绕它转，`self._socket` 槽位直接放这个 cap 对象。

    def _win_capture(self, host: procfs.ProcInfo):
        """Windows 的 cap 对象；`leg` 需要 `port` 与 `on_status` 两个属性当 leg 用。"""
        return win_capture._WinCapture(self, host)

    def _default_bootstrap(self, host: procfs.ProcInfo):
        if not _WIN:
            from . import helper
            return helper.bootstrap(host, port=self._port)
        cap = self._open_capture(host)
        ports = _udp_ports(int(getattr(host, "pid", 0) or 0))
        if ports:
            cap.set_local_ports(ports)
        # 先收一批加密包当样本（样本同时用于「取钥」和「验缓存」）。
        samples: list[tuple[object, bytes]] = []
        packets: list[object] = []
        deadline = time.monotonic() + WIN_SAMPLE_TIMEOUT
        while (self._samples_needed(samples, packets)
               and time.monotonic() < deadline and not self._stop.is_set()):
            for _ts, dg in self._drain_fn(cap, timeout=0.2) or ():
                if not getattr(dg, "is_encrypted", False):
                    continue
                if len(packets) < 4096:
                    packets.append(dg)
                if len(samples) < SAMPLE_TARGET:
                    samples.append((gcm.Flow.of(dg.src, dg.dst), dg.payload))
            if len(samples) < 2:
                time.sleep(0.05)
        return _WinBootstrap(cap, samples, packets)

    def _default_open(self, host: procfs.ProcInfo):
        if not _WIN:
            from . import helper
            return helper.open_capture_once(host, port=self._port)
        return self._win_capture(host)

    def _default_scan(self, host: procfs.ProcInfo, needle: str) -> dict:
        if not _WIN:
            from . import helper
            return helper.scan_once(host, needle=needle, port=self._port)
        # Windows 没有 needle 之分（不读内存，扫的是 pagefile），一次就出结果。
        cap = self._socket
        if cap is None:
            return {"ok": False, "error": "抓包还没起来"}
        try:
            hit = cap.acquire_key(list(self._samples))
        except win_capture.KeyUnavailable as exc:
            if getattr(exc, "fatal", False):
                # 没管理员权限 / 内存压缩开着：重试一万次也一样，判成致命
                self._auth_failed = True
                return {"ok": False, "blocked": True, "error": str(exc)}
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "candidates": [hit.key.hex()]}

    @staticmethod
    def _samples_needed(samples: list, packets: list) -> bool:
        return len(samples) < SAMPLE_TARGET or len(packets) < 2
