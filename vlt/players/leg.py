"""分玩家翻译腿：抓包 → 取钥 → 解码 → **每玩家一条翻译会话** → 文本回调。

这条腿与主线的「别人说话」腿**互斥**：分玩家模式可用时用它（能给出名字与各自的
原文/译文），不可用时由调用方回落混音腿。

## 线程模型

```
线程 A（worker，阻塞）                    线程 B（GUI / 引擎的事件循环）
  抓包 / 解密 / 分片 / opus 解码
  └─ 每解出 20ms PCM ──run_coroutine_threadsafe──▶ PlayerRouter.feed()
                                                    └─ QwenLiveTranslateSession
```

为什么不在一个循环里做：`drain()` 要 select 0.1~0.2s、扫描密钥要 1~2s，放在事件
循环里会卡住 websocket 心跳。反过来，把这个线程写成纯同步代码也最简单（探针就是
这么跑的）。

## 失败语义

- 闸门（EAC v2）命中 → 立刻放弃并 `on_status("error", …)`，**不重试**；
- 抓包/取钥失败 → 带退避重试（5/10/20s），并把原因报给界面；
- **预热没就绪 → 只等待并 warn，绝不自己起提权助手**（开始翻译时的二次 pkexec 会
  把正在进行的预热授权卡住）；
- 会话建不起来（限流/断网）→ 只影响那个人，路由器自己按 LRU 回收，不拖垮整条腿。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

from ..session.base import SessionConfig
from . import capture_linux, gcm, guard, names, world
from .collector import VoiceCollector
from .pending import PreKeyBuffer
from .positions import PositionTable, format_distance
from .router import PlayerRouter

# 平台分派：Windows 没有 AF_PACKET（抓包换 npcap，见 `dgram`），也没有
# `process_vm_readv`（EAC 在句柄创建时就把 PROCESS_VM_READ 剥掉了，见 `key_win`），
# 所以进程发现换 `procfs_win`、取钥换 `win_capture`（读裸盘上的 pagefile）。
# 两条腿共用 `leg`/`collector`/`photon`，口径不分裂。
if os.name == "nt":                                      # pragma: no cover - 平台分派
    from . import procfs_win as Proc
else:
    from . import procfs as Proc

RETRY_BACKOFF = (5.0, 10.0, 20.0)
#: 验钥要几个样本（同一个 flow 至少 2 个才够判，40 个是给内存扫描留的余量）。
SAMPLE_TARGET = 40
#: 世界还在加载时最多等多久再照常取钥（日志格式变了不能让人干等）。
WORLD_WAIT_S = 25.0
#: 游戏 UDP 端点表的刷新间隔（VRChat 重连会换本地端口）。
ENDPOINT_REFRESH_S = 2.0
#: 右栏「正在说话」字形的持续时间：最后一个真实音频块之后这么久还算在说。
SPEAKING_WINDOW_S = 2.0


class _KeyUnavailable(RuntimeError):
    """取钥失败。`fatal=True` = 再重试也没用（读内存被拒 / 未授权 / EAC 闸门）。"""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class _WarmupCapture:
    """预热路径：socket / 密钥 / 名字表都借自 `PlayerWarmup`。

    ⚠️ 这里**绝不触发提权**：腿只在预热已经拿到密钥后才租借；万一密钥在这期间
    失效，也只报"未就绪"让外层退避，取钥重扫一律由预热线程自己做。
    """

    def __init__(self, lease) -> None:
        self._lease = lease
        # 端口/本端端点由 `_decode` 通过 `set_local_ports` 灌进来；预热器自己有
        # 一份，但方向判定必须和这条腿用的是同一个值，否则两端都认不出来。
        self._port = int(getattr(lease, "port", 27002))
        self._local_ports: set[int] | None = None

    def drain(self, timeout: float = 0.2):
        return self._lease.drain(timeout)

    def parse(self, raw):
        """把一帧解析成数据报；`local_ports` 跟着端点刷新走。

        Windows 上 lease 后面坐的是 `_WinCapture`，它 `drain()` 交出来的已经是
        `Datagram`（剥加速器头必须基于已认出的 UDP 载荷），所以这里恒等 ——
        和 `warmup._process` 的平台分派同一个口径。
        """
        if os.name == "nt":
            return raw
        return capture_linux.parse_frame(raw, port=self._port,
                                        local_ports=self._local_ports)

    def set_local_ports(self, ports) -> None:
        self._local_ports = ports
        # Windows 的方向判定活在底下的 `_WinCapture` 上，端口一变就转给它，
        # 不必等预热线程下一轮 5 秒刷新。
        forward = getattr(self._lease, "set_local_ports", None)
        if forward is not None:
            forward(ports)

    def cached_key(self):
        return self._lease.key

    def initial_names(self):
        return self._lease.names_copy()

    def recent_cipher(self):
        return self._lease.recent_cipher()

    def bind_status(self, provider) -> None:
        self._lease.bind_status(provider)

    def adopt_names(self, table) -> None:
        self._lease.adopt(table)

    def acquire_key(self, samples):
        hit = self._lease.key
        if hit is None:
            # 等预热自己重扫（腿不参与取钥，避免二次 pkexec）
            raise _KeyUnavailable("预热还没有密钥（等待预热重试）")
        return hit


class PlayerLeg:
    """分玩家翻译腿。生命周期：`start()` → 后台线程 → `stop()`。"""

    def __init__(self, cfg: SessionConfig, *, on_text=None, on_status=None,
                 on_names=None, max_sessions: int = 3, gate_db: float = -55.0,
                 gate_hysteresis_db: float = 6.0,
                 pre_seconds: float = 120.0, port: int = 27002,
                 log_dir: str | Path | None = None,
                 distance_mode: str = "off", max_distance_m: float = 0.0,
                 select_mode: str = "all", select_users=(),
                 warmup) -> None:
        self.cfg = cfg
        self.on_text = on_text or (lambda *_a: None)
        self.on_status = on_status or (lambda *_a: None)
        self.on_names = on_names or (lambda *_a: None)
        self.max_sessions = max_sessions
        self.gate_db = gate_db
        self.gate_hysteresis_db = max(0.0, float(gate_hysteresis_db))
        self.pre_seconds = pre_seconds
        self.port = port
        # log_dir 为空时自动找 VRChat 的 output_log 目录：世界加载闸门与名字兜底
        # 都靠它，而 GUI 侧从来不会传这个参数（实测就是「一条日志都没读」）。
        self.log_dir = Path(log_dir) if log_dir else names.default_log_dir()
        # 距离：off | order | gate | both（位置来自 Event 12 的根节点坐标）
        self.distance_mode = str(distance_mode or "off").lower()
        self.max_distance_m = max(0.0, float(max_distance_m or 0.0))
        # 翻译名单（可选）：all = 全翻；only = 只翻名单里的；except = 除了名单都翻。
        # 名单按 user_id 存（actor 号会在同一进程里复用），运行时用 `_user_of` 映射。
        self.select_mode = str(select_mode or "all").lower()
        self.select_users = tuple(str(u) for u in (select_users or ()) if u)
        # 位置表优先借预热器的（未开始翻译时它也在喂，右栏排序靠它）；
        # 租不到时才用自己这份，collector 会把位置写进 `self.positions` 指向的表。
        self._own_positions = PositionTable()
        self.positions = self._own_positions
        # 预热器：**必填**。socket/密钥/名字表都借自它；没有预热就没有这条腿
        # （开始翻译不再单独提权 —— 否则会和正在进行的预热授权互相卡住）。
        self.warmup = warmup
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.router: PlayerRouter | None = None
        self._collector: VoiceCollector | None = None
        self._known_names: dict[str, str] = {}
        self._warned_names = False
        self._warned_world = False
        self._warned_loading = False
        # 等预热就绪时的状态去重（phase/detail 或 30s 一次）
        self._warmup_note: tuple[str, str] = ("", "")
        self._warmup_note_at = 0.0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="vlt-players-leg")
        self._thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_selection(self, select_mode: str = "", select_users=None) -> None:
        """换翻译名单（GUI 线程调）。

        名单存在 config 里、每次启动都会读，所以这里**只影响正在跑的这一轮**：
        运行中改勾选要立刻生效，才需要把新名单塞进事件循环里的路由。
        `PlayerRouter.set_selection` 会顺带 `_kick()` 唤醒 `_tick_once` 去收掉
        不在名单里的会话 —— 它跑在腿的事件循环里，所以只能 `call_soon_threadsafe`。
        """
        loop = self._loop
        router = self.router
        if loop is None or router is None:
            return
        try:
            loop.call_soon_threadsafe(router.set_selection, select_mode, select_users)
        except RuntimeError:                              # 循环已关（正在收尾）
            pass

    # ------------------------------------------------------------ 线程内

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:                          # noqa: BLE001
            self.on_status("error", f"分玩家腿异常退出：{type(exc).__name__}: {exc}")
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            except Exception:                             # noqa: BLE001
                pass
            self._loop = None

    async def _async_main(self) -> None:
        verdict = guard.assess(guard.collect())
        if verdict.level == "blocked":
            self.on_status("error", "分玩家模式不可用：" + "；".join(verdict.reasons))
            return

        log_names: dict[str, str] = {}
        latest = names.latest_output_log(self.log_dir)
        if latest is not None:
            log_names = names.parse_output_log(latest)
            self.on_status("info", f"名字兜底：{latest.name}（{len(log_names)} 个）")
        else:
            self.on_status("info", f"没找到 output_log（{self.log_dir}）："
                                   f"名字只能来自事件广播，世界加载闸门也不生效")

        self.router = PlayerRouter(
            self.cfg, max_sessions=self.max_sessions, gate_db=self.gate_db,
            gate_hysteresis_db=self.gate_hysteresis_db,
            on_text=self.on_text, on_status=self.on_status,
            label_of=lambda actor: self._label(actor),
            distance_of=lambda actor: self.positions.distance(actor),
            distance_mode=self.distance_mode,
            max_distance_m=self.max_distance_m,
            user_of=self._user_of,
            select_mode=self.select_mode,
            select_users=self.select_users,
        )
        await self.router.start()
        try:
            await asyncio.to_thread(self._capture_blocking, log_names)
        finally:
            await self.router.close()

    def _user_of(self, actor: int) -> str | None:
        """actor → user_id，给路由的白名单判据用。

        先问 collector（翻译中它手上的表最新：Event 253/255 一到就有），
        退到预热器的表（没开始翻译、或刚建连还没收到名字事件时）。
        """
        collector = self._collector
        if collector is not None:
            record = collector.names.get(actor)
            if record is not None and record.user_id:
                return record.user_id
        ask = getattr(self.warmup, "user_of", None)
        if ask is None:
            return None
        try:
            return ask(actor)
        except Exception:                                 # noqa: BLE001
            return None

    def _live_rows(self) -> dict[int, dict]:
        """actor → {speaking, translating}，给预热器的状态页覆盖用。

        `speaking` 看**真实**音频（`last_real`）：主动补的静音不算说话。
        没有会话的人不出现 —— 状态页据此把字形显示成静音。

        读的是 `router.live_real` 这份**快照**（由腿线程每 tick 整体换新），
        不是 `router.sessions` 本体：后者由事件循环线程增删，GUI 线程直接遍历
        小概率撞 `RuntimeError: dictionary changed size during iteration`（被
        `player_rows` 的 except 吞掉，表现为「在说」字形偶尔少一拍）。
        """
        router = self.router
        if router is None:
            return {}
        now = time.monotonic()
        snapshot = router.live_real
        return {actor: {"speaking": (now - real) < SPEAKING_WINDOW_S,
                        "translating": True}
                for actor, real in snapshot.items()}

    def _label(self, actor: int) -> str:
        collector = self._collector
        base = f"actor{actor}"
        if collector is not None:
            label = collector.names.label(actor)
            if label != f"actor{actor}":
                base = label
        if self.distance_mode != "off":
            tag = format_distance(self.positions.distance(actor))
            if tag:
                return f"{base} · {tag}"
        return base

    # ------------------------------------------------------------ 抓包/解码（阻塞）

    def _capture_blocking(self, log_names: dict[str, str]) -> None:
        attempt = 0
        while not self._stop.is_set():
            host = self._wait_host(20.0)
            if host is None:
                self.on_status("info", "等待 VRChat 进程…")
                # 别忙等：等待是常态（用户可能还没开游戏）
                if self._stop.wait(2.0):
                    return
                continue
            # 预热没就绪（还在授权/取钥）就只等 —— 绝不能在这里再起一次助手：
            # 那会弹第二个 pkexec，把正在进行的授权卡住。
            if not self._warmup_ready():
                self._note_warmup_wait()
                if self._stop.wait(2.0):
                    return
                continue
            try:
                self._run_once(host, log_names)
                return                      # 正常结束时整条腿收工
            except Exception as exc:        # noqa: BLE001
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                attempt += 1
                self.on_status("warn", f"分玩家腿出错（{type(exc).__name__}: {exc}）"
                                       f"，{wait:.0f}s 后重试")
                if self._stop.wait(wait):
                    return

    def _warmup_ready(self) -> bool:
        """预热是否已经可以借（socket + 密钥都到位）。

        只等到「有 socket」不够：bootstrap 的取钥有 1~20 秒延迟，这期间租借会让腿
        亲自去扫内存（旧实现就是这样二次提权的）。所以必须等密钥也拿到。
        """
        try:
            return self.warmup.available() and self.warmup.key is not None
        except Exception:                                # noqa: BLE001 - 预热坏了就继续等
            return False

    def _note_warmup_wait(self) -> None:
        """等预热就绪时按 phase/detail 变化或 30s 报一次（warn）。"""
        try:
            status = self.warmup.status()
        except Exception:                                # noqa: BLE001
            status = {}
        phase = str(status.get("phase") or "")
        detail = str(status.get("detail") or "")
        now = time.monotonic()
        if (phase, detail) == self._warmup_note and now - self._warmup_note_at < 30.0:
            return
        self._warmup_note = (phase, detail)
        self._warmup_note_at = now
        hint = detail or "预热尚未就绪"
        self.on_status(
            "warn",
            f"分玩家等待预热：{hint}（不会单独弹授权；若一直停在这里，"
            f"去「设置 → 分玩家」点「重新授权」）")

    def _wait_host(self, seconds: float) -> Proc.ProcInfo | None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            host = Proc.pick_host(Proc.find_vrchat())
            if host is not None:
                return host
            if time.monotonic() >= deadline:
                return None
            time.sleep(2.0)
        return None

    def _run_once(self, host: Proc.ProcInfo, log_names: dict[str, str]) -> None:
        """一次完整的「取钥 → 解码」流程（失败抛异常，由外层退避重试）。

        socket/密钥/名字表**只从预热器借用**：开始翻译不再单独提权（否则会和正在
        进行的预热授权抢 pkexec，把授权卡住）。

        租借是**首选**路径，Windows 也走它：预热器一直握着抓包句柄和名字表，租
        下来既不用重开抓包，`_decode` 收尾的 `adopt_names` 还会把表交还预热器 ——
        这正是「停止翻译之后名字表不丢」的关键；只有租不到（预热还没起来）时才
        退回本平台的一次性抓包。
        """
        lease = None
        if self.warmup is not None:
            try:
                lease = self.warmup.lease()
            except Exception as exc:                     # noqa: BLE001 - 预热坏了不能拖垮翻译
                self.on_status("warn", f"预热不可用（{type(exc).__name__}: {exc}），改用常规取钥")
                lease = None
        # 位置表跟着租约走：借到就用预热器那张（它一直在喂），借不到用自己那份。
        # router 的 distance_of 是运行时读 self.positions，重新赋值即生效。
        self.positions = (getattr(lease, "positions", None) if lease is not None
                          else None) or self._own_positions
        if lease is not None:
            self.on_status("info", "复用预热：跳过授权与内存扫描")
            try:
                self._decode(host, log_names, _WarmupCapture(lease))
            finally:
                lease.release()
            return
        if os.name == "nt":                              # pragma: no cover - 平台分派
            # Windows 没有 pkexec、也没有助手进程：抓包本身不需要提权（npcap），
            # 只有「读裸盘上的 pagefile」那一步要一次性 UAC；而 key 是进程级的，
            # 缓存命中后换世界/换房间都不再弹。key 由预热器先取好（`_capture_blocking`
            # 会等到 warmup.key 就绪），`_WinCapture` 复用它的 session cache。
            from . import win_capture
            with win_capture._WinCapture(self, host) as cap:
                self._decode(host, log_names, cap)
            return
        # Linux：socket/密钥/名字表**只从预热器借用**，开始翻译不再单独提权
        # （否则会和正在进行的预热授权抢 pkexec，把授权卡住）。
        raise RuntimeError("预热器尚未就绪（等待授权/抓包）")

    def _decode(self, host: Proc.ProcInfo, log_names: dict[str, str], cap) -> None:
        report: dict = {}
        # 端点表：游戏自己绑了哪些本地 UDP 端口。它只用来**确认方向归属**
        # （别把同机别的进程、或者自己的上行包算成别人的声音），读不到就退回端口比较。
        local_ports = Proc.udp_ports(host.pid) or None
        if local_ports:
            self.on_status("info", f"游戏 UDP 端点：{sorted(local_ports)}")
        cap.set_local_ports(local_ports)
        gate = world.WorldGate(self.log_dir) if self.log_dir is not None else None

        self.on_status("info", "等待游戏流量（先进世界）…")
        samples: list[tuple[gcm.Flow, bytes]] = []
        # 取钥期间抓到的包全部留着：密钥晚到十几秒也不丢名单和开头语音。
        pre_key = PreKeyBuffer(world_ready=False)
        deadline = time.monotonic() + self.pre_seconds
        world_wait_until = min(deadline, time.monotonic() + WORLD_WAIT_S)

        def _drain(timeout: float = 0.2) -> int:
            """把 socket 里的帧读干净：加密包进 samples（前 40 个）与 pre_key 缓冲。"""
            count = 0
            for _ts, raw in cap.drain(timeout):
                dg = cap.parse(raw)
                if dg is None or not dg.is_encrypted:
                    continue
                count += 1
                if len(samples) < SAMPLE_TARGET:
                    samples.append((gcm.Flow.of(dg.src, dg.dst), dg.payload))
                pre_key.add(dg)
            return count

        while time.monotonic() < deadline and not self._stop.is_set():
            _drain()
            state = gate.poll() if gate is not None else world.STATE_UNKNOWN
            if state == world.STATE_LOADING and pre_key.world_ready is False:
                # 世界还在加载：现在扫内存是白扫（密钥还没进流量）。
                # 只等到 world_wait_until，之后带着告警继续 —— 不能让读不到日志的人干等。
                if time.monotonic() < world_wait_until:
                    continue
                if not self._warned_loading:
                    self._warned_loading = True
                    self.on_status("warn", "日志里还没出现「世界加载完成」，先继续取钥"
                                           "（不影响后续解码，只是可能白扫一次）")
                pre_key.world_ready = True
            elif state == world.STATE_UNKNOWN and gate is not None:
                if not self._warned_world:
                    self._warned_world = True
                    self.on_status("info", f"世界状态未知（{gate.detail}），直接取钥")
                pre_key.world_ready = True
            else:
                pre_key.world_ready = True
            if pre_key.world_ready and len(samples) >= SAMPLE_TARGET:
                break
        if len(samples) < 2 and cap.cached_key() is None:
            # 把游戏当前连的远端端口带出来：如果它不是 27002，用户一眼就知道
            # 是「端口变了/走了代理」，而不是干看一句"没抓到流量"。
            remotes = sorted({e.remote_port for e in Proc.udp_endpoints(host.pid)
                              if e.remote_port})
            hint = f"（游戏当前 UDP 远端端口：{remotes}）" if remotes else ""
            raise RuntimeError("没抓到加密流量（游戏在跑吗？进世界了吗？）" + hint)

        # 扫内存在**后台线程**里跑：主线程继续 drain，把取钥那十几秒的包留住。
        scan_box: dict = {}

        def _scan() -> None:
            try:
                scan_box["hit"] = cap.acquire_key(samples)
            except _KeyUnavailable as exc:
                scan_box["error"] = str(exc)
                scan_box["fatal"] = exc.fatal
            except Exception as exc:                     # noqa: BLE001 - 原样回主线程
                scan_box["error"] = f"取钥异常：{type(exc).__name__}: {exc}"
                # Windows 的 win_capture 抛的是 KeyUnavailable(fatal=...) 而不是
                # _KeyUnavailable：没管理员权限 / 内存压缩开着这两类重试一万次也一样，
                # 必须靠这条被判成致命，否则外层只会无限退避重启。
                scan_box["fatal"] = bool(getattr(exc, "fatal", False))

        scanner = threading.Thread(target=_scan, daemon=True, name="vlt-players-scan")
        scanner.start()
        while scanner.is_alive() and not self._stop.is_set():
            _drain()
        scanner.join(timeout=1.0)
        hit = scan_box.get("hit")
        if hit is None:
            if self._stop.is_set():
                return
            message = str(scan_box.get("error") or "取钥失败")
            if scan_box.get("fatal"):
                self.on_status("error", message)
                return
            raise RuntimeError(message)

        self.on_status("info", f"密钥命中（指纹 {hit.fingerprint}），开始实时解码")
        self.on_status("info", f"每人门限：{self.gate_db:g} dBFS（RMS，hold 500ms / "
                               f"preroll 250ms / 滞回 {self.gate_hysteresis_db:g}dB "
                               f"/ 关闸补 600ms 静音）")

        collector = VoiceCollector(report, log_names=log_names,
                                   pcm_sink=self._forward_pcm,
                                   positions=self.positions,
                                   initial_names=cap.initial_names())
        self._collector = collector
        # 翻译期间预热线程暂停读包 → 它的计数会冻结；状态页改看这条腿的实时计数。
        # `rows` 只有这条腿知道（谁真的在说话、谁已经有 ASR 会话）。
        cap.bind_status(lambda: {"players": len(collector.names),
                                 "name_events": collector.name_events,
                                 "decrypted": collector.decrypted,
                                 "rows": self._live_rows()})
        # ① 回放：预热期间的名字早已在表里；语音只补最近几秒（避免一次灌十几分钟）。
        #    取钥期间自己攒的包（pre_key）按时间序补一遍。
        buffered = pre_key.summary()
        replayed = list(cap.recent_cipher() or []) + pre_key.release()
        for dg in replayed:
            self._ingest(collector, dg, hit)
        report["replay"] = {"packets": len(replayed), **buffered}
        if replayed:
            self.on_status(
                "info",
                f"补回 {len(replayed)} 个包：名字 {len(collector.names)} 人，"
                f"名字事件 {collector.name_events}，语音消息 "
                f"{sum(collector.audio_hits.values())}")

        try:
            loop = self._loop
            next_stat = time.monotonic() + 10.0
            next_ports = time.monotonic() + ENDPOINT_REFRESH_S
            while not self._stop.is_set():
                for _ts, raw in cap.drain(0.2):
                    dg = cap.parse(raw)
                    if dg is None:
                        continue
                    self._ingest(collector, dg, hit)
                if loop is None:
                    break
                # 端点表跟着重连走：VRChat 换本地端口后，方向判定不能还认旧的。
                now = time.monotonic()
                if now >= next_ports:
                    next_ports = now + ENDPOINT_REFRESH_S
                    fresh = Proc.udp_ports(host.pid) or None
                    if fresh and fresh != local_ports:
                        local_ports = fresh
                        cap.set_local_ports(local_ports)
                        self.on_status("info", f"游戏 UDP 端点更新：{sorted(fresh)}")
                # 每 10 秒报一次"到底有没有在解出语音/建会话" —— 这一条能直接区分
                # 「没人说话」「门限挡住了」「会话没建起来」三种情况。
                if time.monotonic() >= next_stat:
                    next_stat = time.monotonic() + 10.0
                    stats = self.router.stats() if self.router is not None else {}
                    known = len(collector.names)
                    evict_note = (
                        f" / 按距离换人 {stats.get('distance_evictions', 0)}"
                        if "order" in str(stats.get("distance_mode", "")) else "")
                    self.on_status(
                        "info",
                        f"解码：音频消息 {sum(collector.audio_hits.values())}，"
                        f"帧 {sum(collector.frames.values())}，"
                        f"丢弃 {stats.get('gated_blocks', 0) + stats.get('distance_dropped', 0)}"
                        f"，会话 {stats.get('sessions', 0)}/{stats.get('max_sessions', 0)}"
                        f"（建连 {stats.get('sessions_created', 0)}"
                        f" / 失败 {stats.get('create_failures', 0)}"
                        f" / 预算排队 {stats.get('budget_waits', 0)}"
                        f" / 排队 {stats.get('queued', 0)}"
                        f" / 溢出丢弃 {stats.get('overflow_dropped', 0)}"
                        f"{evict_note}），"
                        f"已发块 {stats.get('sent_chunks', 0)}，"
                        f"文本 {stats.get('text_events', 0)}，名字 {known}，"
                        f"换世界 {collector.world_changes}，"
                        f"名字事件 {collector.name_events}"
                        f"({dict(collector.name_code_hits)})，"
                        f"分片 {sum(f.completed for f in collector.fragmenters.values())}/"
                        f"丢 {sum(f.dropped + f.rejected for f in collector.fragmenters.values())}")
                    # 一个名字都没有 = 启动太晚（记录只在加入/属性变更时广播）。
                    if known == 0 and not self._warned_names:
                        self._warned_names = True
                        self.on_status(
                            "warn",
                            "还没拿到任何玩家名：玩家记录只在「加入 / 属性变更」时广播。"
                            "保持本工具运行，**离开并重新进入世界**（或换世界再回来）即可拿到全员名字")
                    elif known > 0:
                        self._warned_names = True

        finally:
            # 停止翻译/异常退出：把名字表交还预热器，状态页才不会突然变成 0 人
            cap.adopt_names(collector.names)

    def _ingest(self, collector: VoiceCollector, dg, hit) -> None:
        """喂一个数据报给采集器；名字表变了才推给界面。

        用 `NameTable.version` 比整数，而不是每个包复制一份 dict —— 后者在人多时
        是每秒几十次 O(n) 拷贝。
        """
        before = collector.names.version
        collector.handle(dg, hit.key, hit.layout)
        if collector.names.version != before:
            self._emit_names(collector.names.snapshot())

    def _forward_pcm(self, actor: int, pcm: bytes) -> None:
        """在采集线程里被调用 → 投递到事件循环。"""
        loop = self._loop
        if loop is None or self.router is None or self._stop.is_set():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.router.feed(actor, pcm), loop)
        except RuntimeError:
            pass

    def _emit_names(self, snapshot: dict) -> None:
        fresh = []
        for actor, record in snapshot.items():
            key = f"{actor}:{record.user_id}"
            if key not in self._known_names:
                self._known_names[key] = record.display_name
                fresh.append((actor, record.user_id, record.display_name))
        if fresh:
            self.on_names(fresh)
