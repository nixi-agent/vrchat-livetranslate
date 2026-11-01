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
- 会话建不起来（限流/断网）→ 只影响那个人，路由器自己按 LRU 回收，不拖垮整条腿。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

from ..session.base import SessionConfig
from . import capture_linux, gcm, guard, names, procfs, world
from .collector import VoiceCollector
from .helper import HelperSession, KeyBlocked, scan_key_on
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


class _KeyUnavailable(RuntimeError):
    """取钥失败。`fatal=True` = 再重试也没用（读内存被拒 / 未授权 / EAC 闸门）。"""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class _HelperCapture:
    """没有预热时的老路径：一次性助手（socket + 扫内存都在同一个会话里）。"""

    def __init__(self, leg: "PlayerLeg", sess: HelperSession) -> None:
        self._leg = leg
        self._sess = sess
        self._port = leg.port
        self._local_ports: set[int] | None = None

    def drain(self, timeout: float = 0.2):
        return capture_linux.drain(self._sess.packet_socket, timeout=timeout)

    def parse(self, raw):
        """把一帧解析成数据报；`local_ports` 跟着端点刷新走。"""
        return capture_linux.parse_frame(raw, port=self._port,
                                        local_ports=self._local_ports)

    def set_local_ports(self, ports) -> None:
        self._local_ports = ports

    def cached_key(self):
        return None

    def initial_names(self):
        return None

    def recent_cipher(self):
        return None

    def bind_status(self, _provider) -> None:
        return None

    def adopt_names(self, _table) -> None:
        return None

    def acquire_key(self, samples):
        # needle 扫描只有一份实现（`helper.scan_key_on`）：预热、leg、探针共用
        try:
            hit = scan_key_on(self._sess, samples, on_status=self._leg._scan_status)
        except KeyBlocked as exc:
            raise _KeyUnavailable(str(exc), fatal=True) from exc
        except Exception as exc:                         # noqa: BLE001 - 原样回给调用方
            raise _KeyUnavailable(f"扫描失败：{exc}") from exc
        if hit is None:
            raise _KeyUnavailable("没找到能通过 GCM 认证的密钥")
        return hit


class _WarmupCapture:
    """预热路径：socket / 密钥 / 名字表都借自 `PlayerWarmup`。"""

    FATAL_PHASES = ("unauthorized", "blocked")

    def __init__(self, lease) -> None:
        self._lease = lease
        # 端口/本端端点由 `_decode` 通过 `set_local_ports` 灌进来；预热器自己有
        # 一份，但方向判定必须和这条腿用的是同一个值，否则两端都认不出来。
        self._port = int(getattr(lease, "port", 27002))
        self._local_ports: set[int] | None = None

    def drain(self, timeout: float = 0.2):
        return self._lease.drain(timeout)

    def parse(self, raw):
        """把一帧解析成数据报；`local_ports` 跟着端点刷新走。"""
        return capture_linux.parse_frame(raw, port=self._port,
                                        local_ports=self._local_ports)

    def set_local_ports(self, ports) -> None:
        self._local_ports = ports

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
        hit = self._lease.ensure_key(samples)
        if hit is not None:
            return hit
        status = self._lease.status()
        phase = str(status.get("phase") or "")
        detail = str(status.get("detail") or "取钥失败")
        fatal = phase in self.FATAL_PHASES or detail.startswith("读内存被拒绝")
        raise _KeyUnavailable(f"预热取钥失败：{detail}", fatal=fatal)


class PlayerLeg:
    """分玩家翻译腿。生命周期：`start()` → 后台线程 → `stop()`。"""

    def __init__(self, cfg: SessionConfig, *, on_text=None, on_status=None,
                 on_names=None, max_sessions: int = 3, gate_db: float = -55.0,
                 gate_hysteresis_db: float = 6.0,
                 pre_seconds: float = 120.0, port: int = 27002,
                 log_dir: str | Path | None = None,
                 distance_mode: str = "off", max_distance_m: float = 0.0,
                 warmup=None) -> None:
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
        self.positions = PositionTable()
        # 预热器（可选）：有就借用它的 socket/密钥/名字表，跳过授权与 11 秒内存扫描。
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
        )
        await self.router.start()
        try:
            await asyncio.to_thread(self._capture_blocking, log_names)
        finally:
            await self.router.close()

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

        有预热器就**借用**它的 socket/密钥/名字表（跳过 pkexec 与 11 秒内存扫描）；
        没有就走原来的「一次性助手」路径。两条路共用 `_decode`，口径不会分裂。
        """
        if os.name == "nt":                              # pragma: no cover - 平台分派
            # Windows 没有 pkexec、也没有助手进程：抓包本身不需要提权（npcap），
            # 只有「读裸盘上的 pagefile」那一步要一次性 UAC；而 key 是进程级的，
            # 缓存命中后换世界/换房间都不再弹。
            from . import win_capture
            with win_capture._WinCapture(self, host) as cap:
                self._decode(host, log_names, cap)
            return
        lease = None
        if self.warmup is not None:
            try:
                lease = self.warmup.lease()
            except Exception as exc:                     # noqa: BLE001 - 预热坏了不能拖垮翻译
                self.on_status("warn", f"预热不可用（{type(exc).__name__}: {exc}），改用常规取钥")
                lease = None
        if lease is not None:
            self.on_status("info", "复用预热：跳过授权与内存扫描")
            try:
                self._decode(host, log_names, _WarmupCapture(lease))
            finally:
                lease.release()
            return
        with HelperSession(host, port=self.port) as sess:
            sess.start_capture(filter_mode="strict")
            if sess.packet_socket is None:
                raise RuntimeError("没有拿到抓包 fd")
            self._decode(host, log_names, _HelperCapture(self, sess))

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
        # 翻译期间预热线程暂停读包 → 它的计数会冻结；状态页改看这条腿的实时计数
        cap.bind_status(lambda: {"players": len(collector.names),
                                 "name_events": collector.name_events,
                                 "decrypted": collector.decrypted})
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
                        f" / 溢出丢弃 {stats.get('overflow_dropped', 0)}），"
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

    def _scan_status(self, needle: str, reply: dict, candidates: int) -> None:
        self.on_status("info", f"扫内存（needle={needle}）："
                               f"{reply['scanned_bytes'] / 2**30:.1f} GiB，"
                               f"候选 {candidates}")

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
