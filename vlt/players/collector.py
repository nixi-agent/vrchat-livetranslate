"""把解密后的语音按玩家收集起来（wav / 实时回调两条出口）。

从 `scripts/probe_player_audio.py` 抽出来的原因同上：GUI 的分玩家腿和探针必须
跑**同一套**判断（事件码不可信、分片并发、长度前缀多变、opus 自校验）。
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from hashlib import blake2b
from pathlib import Path

from . import capture_linux, gcm, names, photon


class VoiceCollector:
    """边解密边收集语音（**不依赖 socket**，所以离线重放也能用同一套逻辑）。

    P0-b 的教训都在这里：
      * 事件码不可信（真实流量里一个 Event 1 都没有）；
      * 分片是并发交错的（每条消息各自的 (channel, startSeq) 槽）；
      * byte[] 的长度前缀可能是 LEB128，也可能是 u16/u32；
      * 参数表可能错位 —— 所以「有 245 且能切出合法 opus 帧」才是判据。
    """

    def __init__(self, report: dict, *, fill_gaps: bool = False,
                 log_names: dict[str, str] | None = None,
                 pcm_sink=None, positions=None, initial_names=None) -> None:
        from vlt.players import opus  # 延迟导入

        if not opus.available():
            detail = opus.load_error()
            raise RuntimeError(
                "找不到 libopus（Linux 装 opus 包；Windows 用 VRChat 自带的 opus.dll，"
                "路径会由 win_capture 自动加进搜索目录）"
                + (f"：{detail}" if detail else "")
            )
        self.opus = opus
        self.report = report
        self.decoder = opus.Decoder()
        self.fragmenters: dict[str, photon.Fragmenter] = defaultdict(photon.Fragmenter)
        self.pcm: dict[int, bytearray] = defaultdict(bytearray)
        self.frames: Counter = Counter()
        self.layouts: dict[int, Counter] = defaultdict(Counter)
        self.event_codes: Counter = Counter()
        self.cmd_kinds: Counter = Counter()
        self.audio_hits: Counter = Counter()
        self.unknown_types: Counter = Counter()
        self.non_f3: Counter = Counter()
        self.payload_samples: dict[str, str] = {}
        self.prefix_offsets: Counter = Counter()   # 消息在 payload 里的起点（0/4/2/8…）
        # 名字：actor → (usr_id, displayName)。记录只在 42/253/255 里广播，
        # 所以中途启动抓包时可能只认识一部分人（会在进度里显示 X/Y）。
        # 预热期间已经攒好的名字表可以直接注入（独立副本，两边互不影响）
        self.names = initial_names.copy() if initial_names is not None else names.NameTable(log_names)
        self.name_code_hits: Counter = Counter()   # 42/253/255 各来多少条
        self.name_records = 0                      # 解出多少个 (actor, usr, 名字)
        self.name_with_display = 0
        self._name_logs = 0
        self.name_events = 0
        self._last_index: dict[int, int] = {}      # 每人的 USpeak 序号（去重/可选补缺口）
        # 默认**不补**静音：P0-b 实测 90 秒抓包里，序号缺口主要是 VRChat 自己的
        # VAD 停顿（不说话就不发包），补上会把 124 秒的真实人声撑成 383 秒。
        self.fill_gaps = fill_gaps
        # 每解出一帧 20ms 的 16k mono PCM 就回调一次（GUI 腿用它喂翻译会话）。
        # 传 None 时只统计/写 wav，不回调（探针模式）。
        self._pcm_sink = pcm_sink
        # 位置表（可选）：Event 12 里带根节点坐标，喂给距离门限/排序用。
        self.positions = positions
        self.voice_events = 0
        self.first_body_hex = ""
        self.total_packets = 0
        self.decrypted = 0
        # 当前世界（来自出向事件 226）。换世界要清 actor/名字/分片表，否则串名。
        self.world = ""
        self.world_changes = 0
        # 多网卡/加速器会让同一份密文出现两次；按密文指纹 + 1s 窗口去重。
        self._recent: dict[bytes, float] = {}

    # ------------------------------------------------------------ 判据

    def _looks_like_voice(self, body: bytes) -> bool:
        frames, _layout = photon.split_voice_frames(body, valid=self.opus.is_valid_packet)
        return bool(frames)

    # ------------------------------------------------------------ 处理

    def handle(self, dg: capture_linux.Datagram, key: bytes, layout: str) -> None:
        now = time.monotonic()
        stamp = blake2b(dg.payload, digest_size=8).digest()
        last = self._recent.get(stamp)
        if last is not None and now - last < 1.0:
            return
        self._recent[stamp] = now
        if len(self._recent) > 4096:
            cutoff = now - 1.0
            for k in [k for k, v in self._recent.items() if v < cutoff]:
                self._recent.pop(k, None)

        self.total_packets += 1
        plain = gcm.try_open(key, dg.payload, layout)
        if plain is None:
            return
        self.decrypted += 1
        commands, err = photon.parse_commands(plain)
        if err and not commands:
            return
        for cmd in commands:
            self.cmd_kinds[str(cmd.kind)] += 1
            for payload in self.fragmenters[dg.direction].feed(cmd):
                self._on_message(payload, dg)

    def _on_message(self, payload: bytes, dg: capture_linux.Datagram) -> None:
        msg, skip = photon.parse_message_at(payload)
        if msg is not None:
            self.prefix_offsets[skip] += 1
        if msg is None:
            self.non_f3[payload[:1].hex() or "空"] += 1
            self.payload_samples.setdefault("非F3", payload[:48].hex())
            return
        tag = f"{msg.kind}:{msg.code}"
        self.event_codes[tag] += 1
        self.payload_samples.setdefault(tag, payload[:48].hex())
        if msg.error:
            self.unknown_types[msg.error.split("：")[0]] += 1
        if msg.kind == photon.TYPE_EVENT and msg.code == 1:
            self.voice_events += 1

        # 世界切换：出向的「加入世界」事件 226。actor 号只对「这个房间的这次加入」
        # 有意义 —— 不清表，新玩家的声音就会挂到旧名字上（actor 会被复用）。
        if msg.kind == photon.TYPE_OP_REQUEST and msg.code == photon.EVENT_JOIN_WORLD:
            world = photon.joined_world(payload)
            if world is not None and world != self.world:
                self._switch_world(world)

        # 位置：Event 12（移动/IK）。入向的是别人的（顶层 Event）；
        # **出向的是自己的**，包在 RaiseEvent op 253 里（param 244=事件码, 245=数据）。
        if self.positions is not None:
            if msg.kind == photon.TYPE_EVENT and msg.code == 12:
                actor = photon.scan_actor(msg.payload)
                if actor:
                    self.positions.observe(actor, msg.payload, ts=dg.ts)
            elif msg.kind == photon.TYPE_OP_REQUEST and msg.code == 253:
                ev = msg.param(244)
                data = msg.param(245)
                if (ev is not None and isinstance(ev.value, int) and ev.value == 12
                        and data is not None
                        and isinstance(data.value, (bytes, bytearray))):
                    self.positions.observe_body(0, bytes(data.value), ts=dg.ts,
                                                is_self=True)

        # 玩家记录：42/253 是单条（actor 在尾部），255 是加入时的整张列表。
        if msg.kind == photon.TYPE_EVENT and (msg.code in names.NAME_EVENT_CODES
                                              or msg.code == names.LEAVE_EVENT):
            self.name_events += 1
            self.name_code_hits[str(msg.code)] += 1
            parsed, diag = ([], {})
            if msg.code != names.LEAVE_EVENT:
                parsed, diag = names.parse_records_ex(msg.payload)
            self.name_records += len(parsed)
            self.name_with_display += sum(1 for r in parsed if r.display_name)
            # 前 6 条留痕：能一眼看出「事件来了但解不出记录」还是「根本没事件」。
            # 解不出时把**字段名序列**打出来（只有 key，没有值/人名）——
            # 2026-10 那次「12 条事件全部记录=0」就是靠这个定位到字段顺序变了。
            if self._name_logs < 6 and msg.code != names.LEAVE_EVENT:
                self._name_logs += 1
                extra = ""
                if not parsed:
                    fields = diag.get("fields") or []
                    extra = (f" 字段={fields}" if fields
                             else f" 诊断={diag.get('error') or '无 user 字段'}")
                print(f"[players] 名字事件 code={msg.code} len={len(msg.payload)} "
                      f"记录={len(parsed)} 带名={sum(1 for r in parsed if r.display_name)}"
                      + extra
                      + ("" if parsed else f" head={msg.payload[:48].hex()}"),
                      flush=True)
            changed = self.names.feed(msg.payload, code=msg.code, records=parsed)
            for record in changed[:4]:
                print(f"[p0b] 玩家名：actor {record.actor} → {record.display_name!r}"
                      f"（{record.user_id}）", flush=True)
            if len(changed) > 4:
                print(f"[p0b] 玩家名：本条记录里还有 {len(changed) - 4} 位…", flush=True)

        param = msg.param(photon.PARAM_AUDIO)
        body = (bytes(param.value)
                if param is not None and isinstance(param.value, (bytes, bytearray))
                else b"")
        # 结构化解析出来的"音频体"也要自校验：参数表错位时，245 会对上但长度不对。
        if not body or not self._looks_like_voice(body):
            body = photon.scan_voice_body(payload, valid=self._looks_like_voice)
        if not body:
            return
        if dg.direction != "in":
            return                      # 自己说的话在出向包里：不算"别人"
        self.audio_hits[tag] += 1
        if not self.first_body_hex:
            self.first_body_hex = body[:48].hex()

        voice_frames, frame_layout = photon.split_voice_frames(
            body, valid=self.opus.is_valid_packet)
        if not voice_frames:
            self.layouts[0]["unparsed"] += 1
            return
        actor = None
        sender = msg.param(photon.PARAM_SENDER)
        # actor 号实测可以到 1500+（P0-b: 1376~1522），别按 "≤255" 截断，
        # 否则会白白退到启发式扫描。
        if sender is not None and isinstance(sender.value, int) \
                and 1 <= sender.value < (1 << 16):
            actor = sender.value
        if actor is None:
            actor = photon.scan_actor(payload) or 0
        self.layouts[actor][frame_layout] += 1
        for frame in voice_frames:
            missing, duplicate = photon.sequence_gap(self._last_index.get(actor), frame.index)
            if duplicate:
                continue
            try:
                decoded = self.decoder.decode(frame.data)
            except ValueError:
                continue
            if not decoded:
                continue
            # 仅在显式开启时补静音（见 __init__ 的注释）。
            if missing and self.fill_gaps:
                self.pcm[actor] += b"\x00\x00" * (320 * missing)
                self.frames[actor] += missing
            self.pcm[actor] += decoded
            self.frames[actor] += 1
            self._last_index[actor] = frame.index
            # 实时出口：GUI 的翻译腿靠这一行把音频喂给 PlayerRouter。
            # ⚠️ 抽模块时这里漏过一次 —— 现象是"密钥命中、一个会话都不建、一个字都没有"。
            if self._pcm_sink is not None:
                self._pcm_sink(actor, decoded)

    # ------------------------------------------------------------ 世界

    def _switch_world(self, world: str) -> None:
        """换世界：清掉一切「只对上一个房间有效」的状态。

        第一次看到世界（`self.world` 为空）时只记录，不清表 —— 中途启动抓包时
        名字可能已经从 replay 的包和 output_log 里拿过了，清掉反而是退步。
        """
        had = self.world
        self.world = world
        if not had:
            print(f"[players] 当前世界 {world}", flush=True)
            return
        self.world_changes += 1
        dropped = len(self.names)
        self.names.clear()
        self.fragmenters.clear()
        self._last_index.clear()
        if self.positions is not None:
            self.positions.clear_actors()
        print(f"[players] 换世界 → {world}（清掉 {dropped} 个旧 actor 名与"
              f"未完成分片）", flush=True)

    # ------------------------------------------------------------ 收尾

    def summary_line(self) -> str:
        return (f"音频消息 {sum(self.audio_hits.values())}，已解码帧 "
                f"{sum(self.frames.values())}，消息 {sum(self.event_codes.values())}，"
                f"名字 {len(self.names)} 人，"
                f"分片完成 {sum(f.completed for f in self.fragmenters.values())} / "
                f"丢弃 {sum(f.dropped + f.rejected for f in self.fragmenters.values())}")

    def finish(self, out: Path) -> dict:
        self.decoder.close()
        actors: dict[str, dict] = {}
        for actor, data in self.pcm.items():
            if not data:
                continue
            record = self.names.get(actor)
            label = self.names.label(actor)
            safe = names.safe_filename(label) if record and record.display_name else ""
            stem = f"player_{safe}_actor{actor}" if safe else f"player_actor{actor}"
            path = self.opus.write_wav(out / f"{stem}.wav", bytes(data))
            peak = max((abs(int.from_bytes(data[i:i + 2], "little", signed=True))
                        for i in range(0, min(len(data), 32000), 2)), default=0)
            actors[str(actor)] = {
                "frames": self.frames[actor],
                "seconds": round(len(data) / 2 / self.opus.RATE, 2),
                "peak": peak,
                "layout": "、".join(f"{k}×{v}" for k, v in
                                    self.layouts[actor].most_common(3)),
                "wav": path.name,
                "name": label,
                "usr_id": record.user_id if record else "",
            }
        self.report.update({
            "packets": {"total": self.total_packets, "decrypted": self.decrypted},
            "fragments": {
                "completed": sum(f.completed for f in self.fragmenters.values()),
                "dropped": sum(f.dropped for f in self.fragmenters.values()),
                "rejected": sum(f.rejected for f in self.fragmenters.values()),
                "active": sum(f.active for f in self.fragmenters.values()),
            },
            "events": dict(self.event_codes.most_common(12)),
            "command_kinds": dict(self.cmd_kinds.most_common(8)),
            "audio_param_hits": dict(self.audio_hits.most_common(8)),
            "non_f3_payloads": dict(self.non_f3.most_common(4)),
            "payload_samples": self.payload_samples,
            "message_prefix_offsets": dict(self.prefix_offsets.most_common(6)),
            "voice_events": self.voice_events,
            "unknown_param_types": dict(self.unknown_types.most_common(8)),
            "first_voice_body_hex": self.first_body_hex,
            "actors": actors,
            "names": {str(a): {"name": r.display_name or self.names.label(a),
                               "usr_id": r.user_id}
                      for a, r in self.names.snapshot().items()},
            "unnamed_actors": sorted(a for a in actors if self.names.get(int(a)) is None),
            "name_events": self.name_events,
            "world": self.world,
            "world_changes": self.world_changes,
        })
        return actors
