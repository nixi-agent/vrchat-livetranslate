#!/usr/bin/env python
"""预热器（`vlt/players/warmup.py`）的离线验收。

## 这里钉住的事

1. 发现 VRChat 进程 → 开 socket → 取钥 → **只解名字**（42/253/255/254 + 226），
   不碰 opus、不建翻译会话；
2. **拿到密钥之前**收到的包（加入世界那批名单）在装好密钥后被补解一遍；
3. 租借给翻译腿期间预热线程**不读 socket**；归还后恢复；
4. 连续解不开的包达到阈值 → 判定密钥失效并重扫（名字表保留）；
5. 游戏进程变了（pid/starttime）→ 重开 socket、清名字表；
6. 分玩家关闭（`mode: off`）时不起线程、不碰任何东西。

跑法：.venv/bin/python tests/test_players_warmup.py
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import gcm, procfs, warmup  # noqa: E402
from vlt.players.helper import Bootstrap  # noqa: E402
from vlt.players_settings import PlayersSettings  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


KEY_A = bytes(range(32))
KEY_B = bytes([9]) * 32
LOCAL = ("192.168.1.10", 53000)
SERVER = ("34.216.184.93", 27002)


# ---------------------------------------------------------------- 假 AEAD / 报文


class _FakeAead:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def _xor(self, data: bytes) -> bytes:
        return bytes(b ^ self.key[i % len(self.key)] for i, b in enumerate(data))

    def encrypt(self, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        ct = self._xor(data)
        return ct + hashlib.sha256(self.key + nonce + aad + ct).digest()[:16]

    def decrypt(self, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        ct, tag = data[:-16], data[-16:]
        if tag != hashlib.sha256(self.key + nonce + aad + ct).digest()[:16]:
            raise ValueError("tag 不匹配")
        return self._xor(ct)


def _packet(key: bytes, plaintext: bytes, nonce: bytes) -> bytes:
    aad = b"\x00\x07\x01\x01\x02\x03\x04" + nonce
    return aad + _FakeAead(key).encrypt(nonce, plaintext, aad)


def _command(message: bytes) -> bytes:
    return (b"\x01\x00\x00\x00\x00"
            + b"\x02\x00\x00\x00" + struct.pack("!I", 12 + len(message)) + b"\x00\x00\x00\x01"
            + message)


def _text(value: str) -> bytes:
    raw = value.encode()
    out = b""
    left = len(raw)
    while True:
        byte = left & 0x7F
        left >>= 7
        out += bytes([byte | (0x80 if left else 0)])
        if not left:
            break
    return b"\x07" + out + raw


def _map(pairs) -> bytes:
    out = bytes([len(pairs)])
    for key, value in pairs:
        raw = key.encode()
        out += b"\x07" + bytes([len(raw)]) + raw + value
    return out


def _name_message(actor: int, display: str, *, code: int = 42) -> bytes:
    usr = "usr_00000000-0000-4000-8000-000000000007"
    user = _map([("displayName", _text(display)), ("id", _text(usr))])
    props = _map([("showSocialRank", b"\x1c"),
                  ("avatarEyeHeight", b"\x0d\x4c\x05"),
                  ("user", b"\x15" + user)])
    return (b"\xf3\x04" + bytes([code]) + b"\x02" + b"\xf5\x15" + props
            + b"\xfe\x0d" + actor.to_bytes(2, "little"))


def _frame(payload: bytes, *, direction: str = "in") -> bytes:
    src, dst = (SERVER, LOCAL) if direction == "in" else (LOCAL, SERVER)
    udp = struct.pack("!HHHH", src[1], dst[1], 8 + len(payload), 0) + payload
    ip = bytearray(20)
    ip[0] = 0x45
    ip[2:4] = struct.pack("!H", 20 + len(udp))
    ip[8] = 64
    ip[9] = 17
    ip[12:16] = bytes(int(x) for x in src[0].split("."))
    ip[16:20] = bytes(int(x) for x in dst[0].split("."))
    return bytes(ip) + udp


def _name_frame(actor: int, display: str, *, key: bytes = KEY_A,
                nonce: bytes = bytes(12), code: int = 42) -> bytes:
    plain = _command(_name_message(actor, display, code=code))
    return _frame(_packet(key, plain, nonce))


# ---------------------------------------------------------------- 假宿主/工厂


class _Verdict:
    level = "legacy"
    reasons: list[str] = []


class _FakeSocket:
    closed = False

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, *, host=None, frames=None, scan_reply=None, poll_s=0.02):
        self.host = host
        self.frames: list[tuple[float, bytes]] = list(frames or [])
        self.scan_reply = scan_reply or {"ok": True, "candidates": [KEY_A.hex()]}
        self.scan_calls: list[str] = []
        self.bootstrap_calls = 0
        self.open_calls = 0
        self.drains = 0
        self.sockets: list[_FakeSocket] = []
        self.warmup = warmup.PlayerWarmup(
            PlayersSettings(mode="decrypt"),
            find_host=lambda: self.host,
            bootstrap=self._bootstrap,
            open_capture=self._open,
            scan=self._scan,
            guard_check=lambda: _Verdict(),
            drain_fn=self._drain,
            poll_s=poll_s,
        )

    def _bootstrap(self, host) -> Bootstrap:   # noqa: ANN001
        """假 bootstrap：只开 socket、不采样本也不取钥（交给主循环的假扫描）。"""
        self.bootstrap_calls += 1
        return Bootstrap(self._open(host), [], [], None, "")

    def _open(self, host) -> _FakeSocket:      # noqa: ANN001
        self.open_calls += 1
        sock = _FakeSocket()
        self.sockets.append(sock)
        return sock

    def _scan(self, host, needle: str) -> dict:  # noqa: ANN001
        self.scan_calls.append(needle)
        return self.scan_reply

    def _drain(self, sock, *, timeout=0.2, max_frames=128):  # noqa: ANN001
        self.drains += 1
        batch, self.frames = self.frames[:max_frames], self.frames[max_frames:]
        return batch

    def feed(self, *frames: bytes) -> None:
        for frame in frames:
            self.frames.append((time.time(), frame))

    def wait_phase(self, *phases: str, timeout: float = 3.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            phase = self.warmup.status()["phase"]
            if phase in phases:
                return phase
            time.sleep(0.02)
        return self.warmup.status()["phase"]


def _host():
    info = procfs.collect(os.getpid(), with_maps=False)
    return info


def test_names_only() -> None:
    print("[warmup] 解名字（只 names，不解码语音）")
    host = _host()
    h = Harness(host=host)
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)       # noqa: SLF001 - 测的就是这里
    try:
        h.warmup.start()
        h.feed(_name_frame(7, "阿诺owo"), _name_frame(8, "第二个人"))
        phase = h.wait_phase(warmup.PHASE_RECORDING)
        check("取钥后进入 recording", phase == warmup.PHASE_RECORDING, phase)
        snapshot = h.warmup.names_copy().snapshot()
        check("名字解出来了",
              {r.display_name for r in snapshot.values()} == {"阿诺owo", "第二个人"},
              str(snapshot))
        check("扫内存用的是两种 needle",
              h.scan_calls[:2] == ["q", "i"] or h.scan_calls == ["q"], str(h.scan_calls))
        check("不解码语音（没有 PCM 相关统计）",
              "pcm" not in h.warmup.status(), str(h.warmup.status()))
        check("状态里有密钥指纹", len(h.warmup.status()["key"]) == 8,
              str(h.warmup.status()))
    finally:
        h.warmup.close()
        gcm._aesgcm = real


def test_pre_key_buffer_replayed() -> None:
    """装好密钥之前收到的名单，要补解一遍（否则预热永远错过整张名单）。"""
    print("[warmup] 取钥前的包补解")
    host = _host()
    h = Harness(host=host)
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)       # noqa: SLF001
    try:
        # 先把名字帧排好，再让扫描慢一点：第一批包一定在「没有密钥」时被 drain
        h.feed(_name_frame(7, "补解测试"), _name_frame(8, "第二个人"))
        h.warmup.start()
        phase = h.wait_phase(warmup.PHASE_RECORDING)
        check("进入 recording", phase == warmup.PHASE_RECORDING, phase)
        time.sleep(0.2)
        snapshot = h.warmup.names_copy().snapshot()
        check("取钥前的包也被解出名字",
              "补解测试" in {r.display_name for r in snapshot.values()}, str(snapshot))
    finally:
        h.warmup.close()
        gcm._aesgcm = real


def test_single_bootstrap() -> None:
    """检测到进程时只走**一次** bootstrap（不是「开抓包 + 扫描」两个助手）。"""
    print("[warmup] 一次授权（bootstrap）")
    h = Harness(host=_host())
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)       # noqa: SLF001
    try:
        h.feed(_name_frame(7, "x"), _name_frame(8, "y"))
        h.warmup.start()
        h.wait_phase(warmup.PHASE_RECORDING)
        check("bootstrap 只调用一次", h.bootstrap_calls == 1, str(h.bootstrap_calls))
        check("没有额外的 open_capture", h.open_calls == 1, str(h.open_calls))
    finally:
        h.warmup.close()
        gcm._aesgcm = real


def test_lease_status_bridge() -> None:
    """借用期间：状态页用翻译腿的实时计数；归还时把名字交还预热器。"""
    print("[warmup] 借用期间的状态桥接")
    h = Harness(host=_host())
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)       # noqa: SLF001
    try:
        h.feed(_name_frame(7, "预热名字"), _name_frame(8, "第二"))
        h.warmup.start()
        h.wait_phase(warmup.PHASE_RECORDING)
        lease = h.warmup.lease()
        check("拿到租借", lease is not None)
        lease.bind_status(lambda: {"players": 9, "name_events": 42, "decrypted": 100})
        status = h.warmup.status()
        check("状态用腿的实时计数",
              status["players"] == 9 and status["name_events"] == 42,
              str(status))
        # 归还时交还名字表（模拟翻译腿攒到 3 人）
        table = h.warmup.names_copy()
        table.feed(_name_message(9, "腿里的名字", code=42), code=42)
        lease.adopt(table)
        lease.release()
        check("归还后名字表保留腿里的记录",
              "腿里的名字" in {r.display_name for r in h.warmup.names_copy().snapshot().values()},
              str(h.warmup.names_copy().snapshot()))
    finally:
        h.warmup.close()
        gcm._aesgcm = real


def test_lease_pauses_reader() -> None:
    print("[warmup] 租借期间预热线程不读 socket")
    host = _host()
    h = Harness(host=host)
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)       # noqa: SLF001
    try:
        h.warmup.start()
        h.feed(_name_frame(7, "x"), _name_frame(8, "y"))
        h.wait_phase(warmup.PHASE_RECORDING)
        lease = h.warmup.lease()
        check("能拿到租借", lease is not None)
        time.sleep(0.1)
        before = h.drains
        time.sleep(0.3)
        check("租借期间预热线程停止读包", h.drains == before, f"{h.drains} vs {before}")
        check("租借期间再租会失败（单一读者）", h.warmup.lease() is None)
        lease.release()
        time.sleep(0.3)
        check("归还后恢复读包", h.drains > before, f"{h.drains} vs {before}")
    finally:
        h.warmup.close()
        gcm._aesgcm = real


def test_key_rotation_rescan() -> None:
    print("[warmup] 密钥失效 → 自动重扫（名字表保留）")
    host = _host()
    h = Harness(host=host)
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)       # noqa: SLF001
    try:
        h.warmup.start()
        h.feed(_name_frame(7, "旧名字"), _name_frame(8, "旧名字2"))
        h.wait_phase(warmup.PHASE_RECORDING)
        # 10 个用另一把密钥加密的包 → 判定失效
        for i in range(warmup.MAX_DECRYPT_FAILURES):
            h.feed(_name_frame(9, "新名字", key=KEY_B, nonce=bytes([i] * 12)))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and h.warmup.status()["rescans"] < 1:
            time.sleep(0.02)
        check("rescans 计数", h.warmup.status()["rescans"] >= 1, str(h.warmup.status()))
        # 新密钥的候选：再喂两包（会进 _pre_key），然后让扫描返回 KEY_B
        h.scan_reply = {"ok": True, "candidates": [KEY_B.hex()]}
        h.feed(_name_frame(10, "新名字A", key=KEY_B, nonce=bytes([20] * 12)),
               _name_frame(11, "新名字B", key=KEY_B, nonce=bytes([21] * 12)))
        phase = h.wait_phase(warmup.PHASE_RECORDING, timeout=4.0)
        check("重扫后回到 recording", phase == warmup.PHASE_RECORDING, phase)
        got = {r.display_name for r in h.warmup.names_copy().snapshot().values()}
        check("名字表保留（旧名字还在）", "旧名字" in got, str(got))
        check("新密钥的包也解出来", {"新名字A", "新名字B"} <= got, str(got))
    finally:
        h.warmup.close()
        gcm._aesgcm = real


def test_host_change() -> None:
    print("[warmup] 游戏重启（pid/starttime 变了）→ 重开 socket + 清名字")
    host = _host()
    h = Harness(host=host)
    real = gcm._aesgcm
    gcm._aesgcm = lambda k: _FakeAead(k)       # noqa: SLF001
    try:
        h.warmup.start()
        h.feed(_name_frame(7, "上一局"), _name_frame(8, "上一局2"))
        h.wait_phase(warmup.PHASE_RECORDING)
        check("先有名字", len(h.warmup.names_copy()) >= 1)
        opens_before = h.open_calls
        # 伪造「新进程」：同 pid 但 starttime 变
        import dataclasses
        h.host = dataclasses.replace(host, starttime=host.starttime + 1)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and h.open_calls == opens_before:
            time.sleep(0.02)
        check("重新开了 socket", h.open_calls == opens_before + 1, str(h.open_calls))
        check("旧名字被清掉", len(h.warmup.names_copy()) == 0,
              str(h.warmup.names_copy().snapshot()))
    finally:
        h.warmup.close()
        gcm._aesgcm = real


def test_mode_off() -> None:
    print("[warmup] 分玩家关闭（mode=off）什么都不做")
    host = _host()
    calls = {"open": 0}

    w = warmup.PlayerWarmup(
        PlayersSettings(mode="off"),
        find_host=lambda: host,
        open_capture=lambda _h: calls.__setitem__("open", calls["open"] + 1),
        scan=lambda _h, _n: {"ok": True, "candidates": []},
        guard_check=lambda: _Verdict(),
        poll_s=0.02,
    )
    w.start()
    time.sleep(0.1)
    check("不起线程/不开 socket", calls["open"] == 0, str(calls))
    check("状态是 off", w.status()["phase"] == warmup.PHASE_OFF, str(w.status()))
    w.close()


def test_acquiring_before_key() -> None:
    """授权完成后、拿到密钥前要停在「获取中」，不能装成 waiting_game。"""
    print("[warmup] 取钥窗口 → acquiring")
    host = _host()
    h = Harness(host=host)          # 假 bootstrap：只给 socket，不给密钥
    try:
        h.warmup.start()
        phase = h.wait_phase(warmup.PHASE_ACQUIRING, timeout=2.0)
        check("取钥期间是 acquiring", phase == warmup.PHASE_ACQUIRING, phase)
        detail = str(h.warmup.status().get("detail") or "")
        check("提示里说明在取密钥", "获取" in detail, detail)
    finally:
        h.warmup.close()


def main() -> int:
    if _host() is None:
        print("SKIP 读不到 /proc/self")
        return 0
    test_names_only()
    test_pre_key_buffer_replayed()
    test_single_bootstrap()
    test_lease_status_bridge()
    test_lease_pauses_reader()
    test_key_rotation_rescan()
    test_host_change()
    test_mode_off()
    test_acquiring_before_key()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（预热器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
