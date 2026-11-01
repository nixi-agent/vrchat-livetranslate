"""用 CNG（`bcrypt.dll`）原生做 AES-256-GCM 认证 —— 不依赖 `cryptography` 包。

## 为什么不直接用 `gcm.py`

`gcm.py` 走 `cryptography` 的 `AESGCM`，那是可选第三方依赖
（`requirements-players.txt`），而且它的用法是「每包构造一次 `AESGCM(key)`」。
扫描阶段要对 1 万多个候选 × 每流 6 个包认证，密钥调度的开销会压过解密本身。

这里的实现把成本压到底：

* 算法句柄**建一次**（GCM 模式必须在生成密钥之前设到算法句柄上）；
* 一个候选只做**一次** key schedule，然后用同一个 key handle 认证该流的全部
  抽样包（`opens_all`）—— 这是扫描期的主要提速点，实测 12 µs/候选（6 包）；
* 认证失败靠 `BCryptDecrypt` 返回 `STATUS_AUTH_TAG_MISMATCH`（`0xC000A002`）判断，
  不需要自己比 tag。

## 与 `gcm.py` 的关系

* 语义完全一致：`[peerID:2][0x01][challenge:4][nonce:12][ct][tag:16]`，
  AAD = 前 19 字节，nonce = `pkt[7:19]`；
* 只做认证/解密，**不做**候选分组与择优 —— 那是 `key_win.scan_key` 的事；
* 扫描走 `GcmAuth`（按候选批量认证，快）；运行期实时解语音包走 `gcm.py`，
  而 `gcm._aesgcm()` 在 Windows 上会拿到下面的 `Aead`（接口同 `cryptography`），
  于是 `collector`/`leg`/`warmup` 一行都不用改就跑到 CNG 上。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes as wt

__all__ = [
    "AAD_KINDS",
    "Aead",
    "GcmAuth",
    "HEADER_LEN",
    "InvalidTag",
    "KEY_LEN",
    "MIN_PACKET",
    "NONCE_LEN",
    "NONCE_OFFSET",
    "TAG_LEN",
    "available",
    "default_auth",
]

# Photon 加密包的固定布局（与 gcm.py 保持逐字节一致）
HEADER_LEN = 19
NONCE_OFFSET = 7
NONCE_LEN = 12
TAG_LEN = 16
MIN_PACKET = HEADER_LEN + TAG_LEN
KEY_LEN = 32

STATUS_SUCCESS = 0
STATUS_AUTH_TAG_MISMATCH = 0xC000A002

BCRYPT_CHAINING_MODE = "ChainingMode"
BCRYPT_CHAIN_MODE_GCM = "ChainingModeGCM"
BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO_VERSION = 1

AAD_KINDS = ("hdr19", "hdr7", "challenge", "peeronce", "none")

try:
    _bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)
except OSError:                                     # 非 Windows 或系统缺 bcrypt
    _bcrypt = None                              # type: ignore[assignment]


def available() -> bool:
    """这台机器能不能用 CNG 做 AES-GCM。"""
    return _bcrypt is not None


class BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.ULONG),
        ("dwInfoVersion", wt.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wt.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wt.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wt.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wt.ULONG),
        ("cbAAD", wt.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wt.ULONG),
    ]


def _bind() -> None:
    if _bcrypt is None:
        return
    _bcrypt.BCryptOpenAlgorithmProvider.restype = wt.LONG
    _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(wt.HANDLE), wt.LPCWSTR, wt.LPCWSTR, wt.ULONG,
    ]
    _bcrypt.BCryptSetProperty.restype = wt.LONG
    _bcrypt.BCryptSetProperty.argtypes = [
        wt.HANDLE, wt.LPCWSTR, ctypes.POINTER(ctypes.c_ubyte), wt.ULONG, wt.ULONG,
    ]
    _bcrypt.BCryptGenerateSymmetricKey.restype = wt.LONG
    _bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        wt.HANDLE, ctypes.POINTER(wt.HANDLE), ctypes.POINTER(ctypes.c_ubyte),
        wt.ULONG, ctypes.POINTER(ctypes.c_ubyte), wt.ULONG, wt.ULONG,
    ]
    _bcrypt.BCryptDecrypt.restype = wt.LONG
    _bcrypt.BCryptDecrypt.argtypes = [
        wt.HANDLE, ctypes.POINTER(ctypes.c_ubyte), wt.ULONG,
        ctypes.POINTER(BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO),
        ctypes.POINTER(ctypes.c_ubyte), wt.ULONG,
        ctypes.POINTER(ctypes.c_ubyte), wt.ULONG,
        ctypes.POINTER(wt.ULONG), wt.ULONG,
    ]
    _bcrypt.BCryptDestroyKey.restype = wt.LONG
    _bcrypt.BCryptDestroyKey.argtypes = [wt.HANDLE]
    _bcrypt.BCryptCloseAlgorithmProvider.restype = wt.LONG
    _bcrypt.BCryptCloseAlgorithmProvider.argtypes = [wt.HANDLE, wt.ULONG]


_bind()


def _u8(data: bytes) -> tuple[ctypes.Array, ctypes.POINTER(ctypes.c_ubyte)]:
    """把 bytes 摊到一块 ctypes 缓冲上，返回 (持有者, 指针)。

    必须返回持有者，否则临时对象会被回收、指针悬空。
    """
    n = len(data)
    buf = (ctypes.c_ubyte * max(n, 1))()
    if n:
        ctypes.memmove(buf, data, n)
    return buf, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))


def _aad_of(pkt: bytes, kind: str, nonce: bytes) -> bytes:
    if kind == "hdr19":
        return pkt[:HEADER_LEN]
    if kind == "hdr7":                              # peerID + flag + challenge
        return pkt[:7]
    if kind == "challenge":
        return pkt[3:7]
    if kind == "peeronce":                          # peerID + flag + nonce
        return pkt[:3] + nonce
    if kind == "none":
        return b""
    raise ValueError(f"未知 aad_kind {kind!r}")


class GcmAuth:
    """复用算法句柄；一个候选只做一次 key schedule。

    用法：

        with GcmAuth() as auth:
            if auth.opens_all(key, packets):   # 同一 key 认证多包
                ...

    不是线程安全的（内部持有算法句柄，但 key handle 是每次现建的），
    多线程扫描时每线程各建一个即可（建一次约 0.1 ms，可忽略）。
    """

    def __init__(self) -> None:
        if _bcrypt is None:
            raise RuntimeError("CNG 不可用：这台机器拿不到 bcrypt.dll")
        h = wt.HANDLE()
        st = _bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(h), "AES", None, 0) & 0xFFFFFFFF
        if st != STATUS_SUCCESS:
            raise OSError(f"BCryptOpenAlgorithmProvider(AES) -> 0x{st:08X}")
        self._h_algo = h
        mode = BCRYPT_CHAIN_MODE_GCM.encode("utf-16-le") + b"\x00\x00"
        mode_buf, mode_ptr = _u8(mode)
        st = _bcrypt.BCryptSetProperty(
            self._h_algo, BCRYPT_CHAINING_MODE, mode_ptr, len(mode), 0) & 0xFFFFFFFF
        if st != STATUS_SUCCESS:
            self.close()
            raise OSError(f"BCryptSetProperty(GCM) -> 0x{st:08X}")

    def close(self) -> None:
        if self._h_algo:
            _bcrypt.BCryptCloseAlgorithmProvider(self._h_algo, 0)
            self._h_algo = None

    def __enter__(self) -> "GcmAuth":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 内部 ---------------------------------------------------------
    def _new_key(self, key: bytes) -> wt.HANDLE:
        if len(key) != KEY_LEN:
            raise ValueError(f"key 必须 {KEY_LEN} 字节，收到 {len(key)}")
        key_buf, key_ptr = _u8(key)
        hk = wt.HANDLE()
        st = _bcrypt.BCryptGenerateSymmetricKey(
            self._h_algo, ctypes.byref(hk), None, 0, key_ptr, len(key), 0
        ) & 0xFFFFFFFF
        if st != STATUS_SUCCESS:
            raise OSError(f"BCryptGenerateSymmetricKey -> 0x{st:08X}")
        return hk

    @staticmethod
    def _split(pkt: bytes, aad_kind: str,
               nonce_offset: int) -> tuple[bytes, bytes, bytes, bytes] | None:
        """校验布局并拆出 (nonce, aad, ct, tag)；不合规返回 None。

        判据与 `gcm.split_packet` 一致：只有 `pkt[2] == 0x01` 的加密包参与认证。
        """
        if len(pkt) < MIN_PACKET or pkt[2] != 0x01:
            return None
        body = pkt[HEADER_LEN:]
        if len(body) <= TAG_LEN:
            return None
        nonce = pkt[nonce_offset:nonce_offset + NONCE_LEN]
        if len(nonce) != NONCE_LEN:
            return None
        aad = _aad_of(pkt, aad_kind, nonce)
        return nonce, aad, body[:-TAG_LEN], body[-TAG_LEN:]

    def _decrypt(self, hk: wt.HANDLE, nonce: bytes, aad: bytes,
                 ct: bytes, tag: bytes) -> tuple[bool, bytes | None]:
        nonce_buf, nonce_ptr = _u8(nonce)
        aad_buf, aad_ptr = _u8(aad)
        tag_buf, tag_ptr = _u8(tag)
        ct_buf, ct_ptr = _u8(ct)
        out_buf = (ctypes.c_ubyte * max(len(ct), 1))()
        got = wt.ULONG(0)

        info = BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
        info.cbSize = ctypes.sizeof(info)
        info.dwInfoVersion = BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO_VERSION
        info.pbNonce = nonce_ptr
        info.cbNonce = len(nonce)
        info.pbAuthData = aad_ptr if aad else None
        info.cbAuthData = len(aad)
        info.pbTag = tag_ptr
        info.cbTag = len(tag)
        info.pbMacContext = None
        info.cbMacContext = 0
        info.cbAAD = len(aad)
        info.cbData = 0
        info.dwFlags = 0

        st = _bcrypt.BCryptDecrypt(
            hk, ct_ptr, len(ct), ctypes.byref(info), None, 0,
            ctypes.cast(out_buf, ctypes.POINTER(ctypes.c_ubyte)), len(ct),
            ctypes.byref(got), 0) & 0xFFFFFFFF
        if st == STATUS_SUCCESS:
            return True, bytes(bytearray(out_buf[:got.value]))
        if st == STATUS_AUTH_TAG_MISMATCH:
            return False, None
        raise OSError(f"BCryptDecrypt -> 0x{st:08X}")

    # ---- 对外 ---------------------------------------------------------
    def opens(self, key: bytes, pkt: bytes, aad_kind: str = "hdr19",
              nonce_offset: int = NONCE_OFFSET) -> bool:
        """单个候选、单包。等价 `gcm.try_open(key, pkt, "hdr")`。"""
        parts = self._split(pkt, aad_kind, nonce_offset)
        if parts is None:
            return False
        hk = self._new_key(key)
        try:
            ok, _ = self._decrypt(hk, *parts)
            return ok
        finally:
            _bcrypt.BCryptDestroyKey(hk)

    def opens_all(self, key: bytes, packets, aad_kind: str = "hdr19",
                  nonce_offset: int = NONCE_OFFSET) -> int:
        """同一把 key 认证一串包，返回通过的数量（遇到失败立刻停）。

        **这是扫描期的热路径**：整个候选只做一次 key schedule。
        `gcm.Oracle.opens_with` 每包都重建一次 key handle，在 1 万候选的量级上
        会多付出约 6 倍的密钥调度成本。
        """
        if len(key) != KEY_LEN:
            return 0
        hk = self._new_key(key)
        passed = 0
        try:
            for pkt in packets:
                parts = self._split(pkt, aad_kind, nonce_offset)
                if parts is None:
                    break
                ok, _ = self._decrypt(hk, *parts)
                if not ok:
                    break
                passed += 1
        finally:
            _bcrypt.BCryptDestroyKey(hk)
        return passed

    def open_packet(self, key: bytes, pkt: bytes, aad_kind: str = "hdr19",
                    nonce_offset: int = NONCE_OFFSET) -> bytes | None:
        """认证通过时返回明文，失败返回 None。"""
        parts = self._split(pkt, aad_kind, nonce_offset)
        if parts is None:
            return None
        hk = self._new_key(key)
        try:
            ok, plain = self._decrypt(hk, *parts)
            return plain if ok else None
        finally:
            _bcrypt.BCryptDestroyKey(hk)


# ---- 给 gcm.py 用的后端适配 --------------------------------------------
#
# `gcm.py` 里所有解密都经过 `_aesgcm(key)`，拿到的对象只要求一个方法：
# `decrypt(nonce, data, aad)`（`data` = 密文 + 16 字节 tag，认证失败抛异常）。
# 所以下面这个 `Aead` 只要对齐 `cryptography` 的 `AESGCM` 签名，
# `collector`/`leg`/`warmup` 一行都不用改就跑到 CNG 上。


class InvalidTag(Exception):
    """GCM 认证失败。与 `cryptography.exceptions.InvalidTag` 同名同义。"""


_default_auth: "GcmAuth | None" = None
_default_lock = threading.Lock()


def default_auth() -> "GcmAuth":
    """进程级共享的算法句柄。

    `BCryptOpenAlgorithmProvider` + `SetProperty` 一次约 0.1 ms，实时解码时
    每秒几十个包、每包都建一次就是白烧 CPU。共享安全：`_new_key`/`_decrypt`
    每次都现建独立的 key handle，互不干扰。
    """
    global _default_auth
    if _default_auth is None:
        with _default_lock:
            if _default_auth is None:
                _default_auth = GcmAuth()
    return _default_auth


class Aead:
    """`gcm._aesgcm()` 的 Windows 实现（接口同 `cryptography` 的 `AESGCM`）。"""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LEN:
            raise ValueError(f"key 必须 {KEY_LEN} 字节，收到 {len(key)}")
        self._key = bytes(key)

    def decrypt(self, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        if len(data) <= TAG_LEN:
            raise InvalidTag("密文长度不够容纳 tag")
        auth = default_auth()
        hk = auth._new_key(self._key)
        try:
            ok, plain = auth._decrypt(
                hk, bytes(nonce), bytes(aad or b""),
                bytes(data[:-TAG_LEN]), bytes(data[-TAG_LEN:]),
            )
        finally:
            _bcrypt.BCryptDestroyKey(hk)
        if not ok or plain is None:
            raise InvalidTag("GCM 认证失败")
        return plain
