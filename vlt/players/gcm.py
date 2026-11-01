"""Photon UDP 数据报的 AES-256-GCM 解密与**候选密钥验证**。

## 报文布局（来自 VoxTrace 的协议笔记，实测由 P0-b 复核）

```
[peerID:2][0x01][challenge:4][nonce:12][ciphertext][tag:16]
└──────────────── AAD（19 字节）────────────┘
```

- 密钥：32 字节，客户端每场会话随机生成，同一会话的多个连接共用；
- nonce：报文里的 12 字节字段（**不是**自己造的）；
- AAD：**前 19 字节**（7 字节头 + 12 字节 nonce）。少一个字节都会认证失败 ——
  这条是 P0-b 最该先验证的，所以失败时要能区分「密钥不对」和「布局不对」：
  如果**所有**候选都失败，优先怀疑布局，而不是密钥没找到。

## 候选验证为什么要按 flow 分组

同一个游戏会话里可能有多个 Photon 连接（房间服务、名字服务回退等），它们的
密钥**不保证相同**。所以不能用「随便几个包里通过率最高」来挑密钥 —— 那会把
「A flow 的密钥 + B flow 的包」错配成低通过率。正确判据是：

    **存在某个五元组，它抽样的每一个包都能解开**（且至少 2 个包）

这样误报率是密码学级别的零（GCM 认证 128 bit）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

HEADER_LEN = 19          # peerID(2) + 0x01(1) + challenge(4) + nonce(12)
NONCE_OFFSET = 7
NONCE_LEN = 12
TAG_LEN = 16
MIN_PACKET = HEADER_LEN + TAG_LEN
KEY_LEN = 32


@dataclass(frozen=True)
class Flow:
    """一条连接的五个要素里我们关心的部分（方向无关，用来分组）。"""

    a: tuple[str, int]
    b: tuple[str, int]

    @classmethod
    def of(cls, src: tuple[str, int], dst: tuple[str, int]) -> "Flow":
        lo, hi = sorted((src, dst))
        return cls(a=lo, b=hi)

    def __str__(self) -> str:
        return f"{self.a[0]}:{self.a[1]}↔{self.b[0]}:{self.b[1]}"


# AAD 的三种可能布局。第一个是 VoxTrace 笔记里的（前 7 字节头 + nonce = 19 字节）；
# 另外两个是"笔记记错了/版本变了"时的退路 —— 三者都能用同一串候选去试，代价只有
# 每个候选多两次 AEAD 构造，却能省掉用户一次"再跑一遍真机"。
LAYOUTS = ("hdr+nonce", "hdr", "none")
LAYOUT_DESC = {
    "hdr+nonce": "AAD = 前 19 字节（7 字节头 + 12 字节 nonce）",
    "hdr": "AAD = 前 7 字节头，nonce 取第 7..19 字节",
    "none": "AAD 为空，nonce 取第 7..19 字节",
}


@dataclass(frozen=True)
class KeyHit:
    key: bytes
    flow: Flow
    ok: int
    total: int
    layout: str = LAYOUTS[0]

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.key)


def key_fingerprint(key: bytes) -> str:
    """日志里只出现指纹，**永不打印密钥明文**。"""
    import hashlib
    return hashlib.sha256(key).hexdigest()[:8]


def _aesgcm(key: bytes):
    """返回一个只有 `decrypt(nonce, data, aad)` 的 AEAD。

    后端分派：Windows 用系统自带的 CNG（`bcrypt.dll`，零新依赖，见 `gcm_win.Aead`），
    其它平台用 `cryptography`（**可选依赖**，见 requirements-players.txt）。

    为什么都延迟导入：`cryptography` 带编译产物，现有 Linux 用户装的是
    requirements-linux.txt，不该因为一个默认关闭的实验功能而多装它；而
    `gcm_win` 里的 `WinDLL("bcrypt")` 在非 Windows 上会 OSError，也必须延迟。
    """
    if os.name == "nt":                              # pragma: no cover - 平台分派
        from . import gcm_win
        if gcm_win.available():
            return gcm_win.Aead(key)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:                       # pragma: no cover - 环境问题
        from . import INSTALL_HINT
        raise RuntimeError(
            "分玩家语音需要 cryptography：\n  " + INSTALL_HINT
        ) from exc
    return AESGCM(key)


def split_packet(pkt: bytes, layout: str = LAYOUTS[0]) -> tuple[bytes, bytes, bytes] | None:
    """按指定布局拆成 (aad, nonce, ciphertext+tag)；不合法返回 None。"""
    if len(pkt) < MIN_PACKET:
        return None
    if pkt[2] != 0x01:
        # 第 3 字节是「是否加密」标记（实测加密包恒为 1）。明文包（建连那 31 个）
        # 不归我们处理，直接跳过 —— 把它们塞进 GCM 只会制造噪声。
        return None
    nonce = pkt[NONCE_OFFSET:NONCE_OFFSET + NONCE_LEN]
    if layout == "hdr+nonce":
        aad = pkt[:HEADER_LEN]
    elif layout == "hdr":
        aad = pkt[:NONCE_OFFSET]
    elif layout == "none":
        aad = b""
    else:
        raise ValueError(f"未知布局 {layout!r}")
    return aad, nonce, pkt[HEADER_LEN:]


def _open_with(aead, pkt: bytes, layout: str) -> bytes | None:
    parts = split_packet(pkt, layout)
    if parts is None:
        return None
    aad, nonce, body = parts
    if len(body) <= TAG_LEN:
        return None
    try:
        return aead.decrypt(nonce, body, aad)
    except Exception:                                # noqa: BLE001 - 认证失败有很多种异常
        return None


def opener(key: bytes, layout: str = LAYOUTS[0]):
    """返回一个「复用同一把 AEAD」的解密函数（预热/长跑时用）。

    `try_open` 每个包都 `AESGCM(key)` 构造一次，密钥调度比重放解密还贵；
    预热要连续解几分钟的流量，所以把 AEAD 缓存起来。
    """
    aead = _aesgcm(key)

    def _open(pkt: bytes) -> bytes | None:
        return _open_with(aead, pkt, layout)

    return _open


def try_open(key: bytes, pkt: bytes, layout: str = LAYOUTS[0]) -> bytes | None:
    """用一把候选密钥解一个包；解不开返回 None（**不许抛**，扫描要靠它快速筛）。"""
    return _open_with(_aesgcm(key), pkt, layout)


def validate_key(key: bytes, samples: list[tuple[Flow, bytes]],
                 layout: str = LAYOUTS[0]) -> bool:
    """单把密钥的判定：**存在一个 flow，全部抽样都能解开**。"""
    by_flow: dict[Flow, list[bytes]] = {}
    for flow, pkt in samples:
        by_flow.setdefault(flow, []).append(pkt)
    aead = _aesgcm(key)
    for packets in by_flow.values():
        if len(packets) < 2:
            continue
        if all(_open_with(aead, pkt, layout) is not None for pkt in packets):
            return True
    return False


def validate_candidates(candidates: list[bytes], samples: list[tuple[Flow, bytes]],
                        *, limit: int = 0) -> KeyHit | None:
    """在候选里找出真钥。返回命中（含它属于哪条 flow），全部失败返回 None。"""
    by_flow: dict[Flow, list[bytes]] = {}
    for flow, pkt in samples:
        by_flow.setdefault(flow, []).append(pkt)
    usable = [f for f, pkts in by_flow.items() if len(pkts) >= 2]
    if not usable:
        return None

    tried = 0
    for cand in candidates:
        if len(cand) != KEY_LEN:
            continue
        if limit and tried >= limit:
            break
        tried += 1
        # 每把候选只构造一次 AEAD，三种布局共用（构造比 decrypt 贵得多）。
        try:
            aead = _aesgcm(cand)
        except Exception:                            # noqa: BLE001 - 依赖缺失等
            raise
        for layout in LAYOUTS:
            for flow in usable:
                packets = by_flow[flow]
                # 便宜的先判：错钥几乎总在**第一个**包就失败。
                if _open_with(aead, packets[0], layout) is None:
                    continue
                if _open_with(aead, packets[1], layout) is None:
                    continue
                if all(_open_with(aead, pkt, layout) is not None for pkt in packets):
                    return KeyHit(key=cand, flow=flow, ok=len(packets),
                                  total=len(packets), layout=layout)
    return None
