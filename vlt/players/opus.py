"""libopus 的 ctypes 封装：Opus 帧 → **16 kHz 单声道 PCM16**。

## 为什么用系统 libopus 而不是 pip 包

- VRChat 的语音本来就是 Opus，解码不需要任何额外语义；
- 系统里有 `libopus.so.0`（`pacman -S opus`，PipeWire 的依赖，必然存在），
  ctypes 直接调，零新依赖；
- 让 libopus 自己做重采样：`opus_decoder_create(16000, ch)` 就能输出 16 kHz，
  正好是 ASR 要的格式 —— 比"解 48k 再 numpy 重采样"少一次误差来源。

## 一个坑：解码器声道数必须与码流一致

`opus_decoder_create(16000, 1)` 解不了立体声包（返回 `OPUS_INVALID_PACKET`）。
所以先 `opus_packet_get_nb_channels()` 判码流声道数，按声道各缓存一个解码器；
立体声解出来后在本地平均成单声道（16 kHz 立体声 → 单声道只是 (L+R)/2）。

## 自校验

`is_valid_packet()` 用 `opus_packet_get_nb_samples()` 判断一串字节是不是合法
Opus 帧。这是 P0-b 里**唯一客观的**判据：当我们不确定 USpeak 的帧头长度/字节序
时，靠它裁决哪一种切法是真，而不是靠肉眼比十六进制。
"""
from __future__ import annotations

import ctypes
import wave
from ctypes import POINTER, c_char_p, c_int, c_int16, c_int32, c_ubyte, c_void_p
from pathlib import Path

RATE = 16000
MAX_FRAME_SAMPLES = 5760        # 120ms @ 48k；16k 下更小，留足余量

_lib = None
_load_error = ""

# 上游追加的搜索目录（见 `add_search_dir`），优先级高于 `_LIB_NAMES`。
_EXTRA_DIRS: list[Path] = []

# 候选库名。顺序即优先级：
# - Linux：`libopus.so.0` 是 `pacman -S opus` 装的那个，PipeWire 的依赖，必然存在；
# - Windows：系统里通常没有 libopus，但 **VRChat 自带**一份（见 `_vrchat_opus_dirs`），
#   而且它正好就是解码自己语音的同版本，优先用它比找系统库更稳。
_LIB_NAMES = (
    "libopus.so.0", "libopus.so",              # Linux
    "opus.dll", "libopus-0.dll", "libopus.dll",  # Windows
)


def _vrchat_opus_dirs() -> list[Path]:
    """可能藏着 VRChat 自带 opus.dll 的目录。

    Windows 系统里通常没有 libopus，但 VRChat 自带一份，而且正好是解自己语音
    的同版本 —— 优先用它比找系统库更稳。这里只做「尽力而为」的默认猜测：
    Steam 装在默认位置时能直接命中；装在别的盘（本机就是 `E:\\SteamLibrary`）
    时由上游用 `add_search_dir()` 注入真实路径。
    """
    import os
    if os.name != "nt":
        return []
    roots: list[Path] = []
    for key in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(key)
        if base:
            roots.append(Path(base) / "Steam" / "steamapps" / "common" / "VRChat")
    extra = os.environ.get("VLT_VRCHAT_DIR")
    if extra:
        roots.append(Path(extra))
    roots.extend(_EXTRA_DIRS)
    out: list[Path] = []
    for root in roots:
        out.append(root / "VRChat_Data" / "Plugins" / "x86_64")
        out.append(root)
    return [p for p in out if p.is_dir()]


def add_search_dir(path: str | Path) -> None:
    """把 VRChat 安装目录（或直接是含 opus.dll 的目录）加入搜索路径。

    幂等；目录不存在就静默忽略。上游拿到 `procfs_win.image_path(vrchat_pid)`
    之后调它，这样 Steam 装在非默认盘也能找到游戏自带的 opus.dll。
    """
    p = Path(path)
    if p.name.lower() == "opus.dll":
        p = p.parent
    if not p.is_dir() or p in _EXTRA_DIRS:
        return
    _EXTRA_DIRS.append(p)
    global _lib, _load_error
    if _lib is None:
        _load_error = ""   # 之前「找不到」的结论作废，下次重试


def _candidates() -> list[str]:
    """按优先级给出要试的库名/路径。"""
    cands: list[str] = []
    for d in _vrchat_opus_dirs():
        cands.append(str(d / "opus.dll"))
    cands.extend(_LIB_NAMES)
    return cands


def _library():
    """惰性加载：没装 opus 的机器只会在这里报一次清楚的错。"""
    global _lib, _load_error
    if _lib is not None:
        return _lib
    tried: list[str] = []
    for name in _candidates():
        try:
            lib = ctypes.CDLL(name)
        except OSError as exc:
            tried.append(f"{name}: {exc}")
            continue
        lib.opus_decoder_create.argtypes = [c_int32, c_int, POINTER(c_int)]
        lib.opus_decoder_create.restype = c_void_p
        lib.opus_decoder_destroy.argtypes = [c_void_p]
        lib.opus_decode.argtypes = [c_void_p, POINTER(c_ubyte), c_int32,
                                    POINTER(c_int16), c_int, c_int]
        lib.opus_decode.restype = c_int
        lib.opus_packet_get_nb_samples.argtypes = [POINTER(c_ubyte), c_int32, c_int32]
        lib.opus_packet_get_nb_samples.restype = c_int
        lib.opus_packet_get_nb_channels.argtypes = [POINTER(c_ubyte)]
        lib.opus_packet_get_nb_channels.restype = c_int
        lib.opus_strerror.argtypes = [c_int]
        lib.opus_strerror.restype = c_char_p
        _lib = lib
        return lib
    _load_error = "找不到 libopus（" + "；".join(tried) + "）"
    raise RuntimeError(_load_error)


def available() -> bool:
    try:
        _library()
        return True
    except RuntimeError:
        return False


def load_error() -> str:
    """上一次加载失败的原因（成功过就是空串）。给报错文案用，方便用户判断缺什么。"""
    return _load_error


def _buf(data: bytes):
    return ctypes.cast(c_char_p(data), POINTER(c_ubyte))


def packet_channels(packet: bytes) -> int:
    if len(packet) < 1:
        return 0
    return int(_library().opus_packet_get_nb_channels(_buf(packet)))


def packet_samples(packet: bytes, rate: int = 48000) -> int:
    """合法 Opus 帧 → 该采样率下的样本数；不合法 → -1。"""
    if len(packet) < 1:
        return -1
    return int(_library().opus_packet_get_nb_samples(_buf(packet), len(packet), rate))


def is_valid_packet(packet: bytes) -> bool:
    try:
        return packet_samples(packet) > 0
    except RuntimeError:
        return False


class Decoder:
    """按码流声道数缓存的解码器，输出 16 kHz 单声道 PCM16。"""

    def __init__(self, rate: int = RATE) -> None:
        self.rate = rate
        self._decoders: dict[int, c_void_p] = {}

    def _get(self, channels: int) -> c_void_p:
        key = 1 if channels <= 1 else 2
        if key not in self._decoders:
            err = c_int(0)
            handle = _library().opus_decoder_create(self.rate, key, ctypes.byref(err))
            if not handle or err.value != 0:
                raise RuntimeError(f"opus_decoder_create 失败：{err.value}")
            self._decoders[key] = handle
        return self._decoders[key]

    def decode(self, packet: bytes) -> bytes:
        """解一帧。失败抛 `ValueError`（调用方按坏帧跳过，不要中断整条流）。"""
        if not packet:
            return b""
        channels = packet_channels(packet)
        if channels <= 0:
            raise ValueError("不是合法的 Opus 包")
        handle = self._get(channels)
        out = (c_int16 * (MAX_FRAME_SAMPLES * channels))()
        got = int(_library().opus_decode(handle, _buf(packet), len(packet),
                                         out, MAX_FRAME_SAMPLES, 0))
        if got < 0:
            err = _library().opus_strerror(got)
            message = err.decode("utf-8", "replace") if err else str(got)
            raise ValueError(f"opus_decode 失败：{message}")
        if channels == 1:
            return bytes(memoryview(out).cast("B")[: got * 2])
        # 立体声 → 单声道：libopus 已按 16k 输出，这里只做平均。
        mono = bytearray(got * 2)
        for i in range(got):
            left = out[2 * i]
            right = out[2 * i + 1]
            value = (left + right) // 2
            mono[2 * i:2 * i + 2] = int(value).to_bytes(2, "little", signed=True)
        return bytes(mono)

    def close(self) -> None:
        for handle in self._decoders.values():
            try:
                _library().opus_decoder_destroy(handle)
            except Exception:                       # noqa: BLE001 - 关不掉不影响退出
                pass
        self._decoders.clear()

    def __enter__(self) -> "Decoder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def encode_frame(pcm: bytes, *, rate: int = 48000, channels: int = 1) -> bytes:
    """把 PCM16 编成一帧 Opus（**只用于自检/造测试数据**，产品路径不编码）。

    需要 libopus 的编码器；这里每次新建一个，够慢但够简单。
    """
    lib = _library()
    lib.opus_encoder_create.argtypes = [c_int32, c_int, c_int, POINTER(c_int)]
    lib.opus_encoder_create.restype = c_void_p
    lib.opus_encode.argtypes = [c_void_p, POINTER(c_int16), c_int,
                                POINTER(c_ubyte), c_int32]
    lib.opus_encode.restype = c_int
    lib.opus_encoder_destroy.argtypes = [c_void_p]

    err = c_int(0)
    enc = lib.opus_encoder_create(rate, channels, 2048, ctypes.byref(err))  # VOIP
    if not enc or err.value != 0:
        raise RuntimeError(f"opus_encoder_create 失败：{err.value}")
    try:
        samples = len(pcm) // 2
        src = (c_int16 * samples).from_buffer_copy(pcm)
        out = (c_ubyte * 4000)()
        got = int(lib.opus_encode(enc, src, samples // channels, out, 4000))
        if got < 0:
            raise RuntimeError(f"opus_encode 失败：{got}")
        return bytes(bytearray(out[:got]))
    finally:
        lib.opus_encoder_destroy(enc)


def write_wav(path: str | Path, pcm: bytes, rate: int = RATE) -> Path:
    """写 16 kHz 单声道 PCM16 wav（P0-b 人工确认用）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return target
