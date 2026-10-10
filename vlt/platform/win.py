"""Windows 平台实现（sounddevice / pyaudiowpatch / Win32 API）。

⚠️ 本模块是 **Windows 独占**：打包 Windows 版时它被收进产物；Linux 版通过
`--exclude-module vlt.platform.win` 剔除。反过来 Linux 版打进的是
`vlt/platform/linux.py`，Windows 产物里不含它的任何字样。

因此**不要**在共享代码里直接 `import vlt.platform.win`，一律走
`vlt/platform/__init__.py` 的门面（见那边的说明）。
"""
from __future__ import annotations

import ctypes
import logging
from typing import Any
import threading
import time
from pathlib import Path

from .audio import QueueAudioSource
from .base import PA_LOCK, AudioSource, LoopbackTarget

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 设备枚举

def query_devices() -> list[dict]:
    """sounddevice 的设备表 —— **只保留 Windows 已启用（WASAPI）那一套**。

    失败**不在这里吞**：调用方（`vlt/devices.py`）负责把异常翻成「空列表」，
    这样「枚举不到设备」与「库坏了」在日志里还分得开。

    ## 为什么要按 host API 收敛（2026-10-02 实机对照）

    Windows 上 PortAudio 有 4 个 host API（MME / DirectSound / WASAPI / WDM-KS），
    **同一块声卡在每个 API 下各算一条**，还夹着一堆已禁用 / 未插入的幽灵端点。
    本机实测 `sd.query_devices()` **41 条**，而 Windows「声音」里真正**已启用**的只有 **7 个**：

      · Windows 已启用端点（`Get-PnpDevice -Class AudioEndpoint`，Status=OK）：**7 个**
      · PortAudio 的 **WASAPI** 那一套：**正好这 7 个**（名字逐个对得上，采样率统一 48000）
      · MME(8) + DirectSound(9) + WDM-KS(16) 共 33 条 = 那 7 个的重复 **+**
        `立体声混音` / `Realtek HD Audio` 旧滤镜 / `耳机 ()` 空名幽灵 / 蓝牙免手操
        这类**未启用**的端点

    不收敛的后果（都是用户实测报上来的）：
      1. 界面上同一支麦克风出现两三条（界面按「名字 (采样率Hz)」显示）；
      2. 同名那几条**采样率还不一样** —— MME/DirectSound 报 44100、WASAPI 报 48000；
      3. 会列出**根本没启用**的设备，选了当然打不开；
      4. 按名字解析（`devices.resolve_device_name`）总是命中**第一条**，而 MME 排最前
         —— 于是用户就算挑了 WASAPI 那条，实际打开的仍是 MME（最老、延迟最高的一路）。

    ## 口径

    只返回 WASAPI host API 的设备（= Windows 已启用端点，且天然去重）。
    **只在 WASAPI 一个都枚举不到时才回落完整设备表**并留痕 —— 极端环境下宁可列表丑，
    也不能让用户一个设备都选不到。

    另外给每条 dict 打上 `pa_index`（**真实 PortAudio 索引**）：过滤后列表下标不再等于
    PortAudio 索引，`devices.py` 靠这个键取索引，否则会打开到错位的设备。
    """
    import sounddevice as sd
    with PA_LOCK:                    # PortAudio 串行（并发 init/destroy 会段错误）
        devices = [dict(d) for d in sd.query_devices()]
        try:
            apis = [dict(a) for a in sd.query_hostapis()]
        except Exception as exc:     # noqa: BLE001 — 老版本/打桩环境可能没有这个接口
            print(f"[devices] ⚠️ 拿不到 host API 列表（{type(exc).__name__}: {exc}）"
                  "→ 设备表按原样返回（可能含重复项）", flush=True)
            return devices
    for i, d in enumerate(devices):
        d["pa_index"] = i            # 真实索引：过滤后 devices.py 也拿它当设备号

    wasapi = next((i for i, a in enumerate(apis)
                   if "WASAPI" in str(a.get("name", "")).upper()), None)
    if wasapi is None:
        print("[devices] ⚠️ 没有 WASAPI host API，设备表按原样返回"
              "（可能含 MME/DirectSound 的重复项）", flush=True)
        return devices
    kept = [d for d in devices if d.get("hostapi") == wasapi]
    if not kept:
        print("[devices] ⚠️ WASAPI 下没枚举到任何设备，回落完整设备表"
              "（可能含未启用的端点，列表会变长）", flush=True)
        return devices
    dropped = len(devices) - len(kept)
    if dropped:
        print(f"[devices] 设备表收敛到 WASAPI 已启用设备：{len(kept)} 个"
              f"（另有 {dropped} 条是 MME/DirectSound/WDM-KS 的重复项或未启用端点，已隐藏）",
              flush=True)
    return kept


def query_loopback_devices() -> list[dict]:
    """WASAPI loopback 设备表（= 每个输出设备的一份「录音副本」）。

    这是 Windows 独有的能力，Linux 上对应的是 PipeWire 的 monitor source。
    """
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        p = pyaudio.PyAudio()
        try:
            return [dict(d) for d in p.get_loopback_device_info_generator()]
        finally:
            p.terminate()


def default_output_index() -> int:
    """系统默认**输出**设备在 pyaudiowpatch 里的 index；取不到返回 -1。

    用来在 loopback 列表里优先挑「默认输出设备」对应的那一份 ——
    用户把 VRChat 的声音切到别的声卡时，这一步决定我们抓的是不是同一路。
    """
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        p = pyaudio.PyAudio()
        try:
            return int(p.get_host_api_info_by_type(pyaudio.paWASAPI)
                       .get("defaultOutputDevice", -1))
        finally:
            p.terminate()


def default_input_index() -> int:
    """系统默认**输入**端点在 PortAudio 里的索引；取不到返回 -1。

    与 `default_output_index()` 对偶。优先取 **WASAPI host API 的 `default_input_device`**
    （与 `query_devices()` 收敛到的那张表同一口径）；拿不到再退 PortAudio 全局默认
    （`sd.default.device[0]`）。

    用途：让「自动检测」也解析成**具体索引**，再走与手选完全相同的打开通路
    （原生采样率 + 端点声道数 + 同名回落）—— 否则自动会走 `device=None`，还会绕过
    「WASAPI 只吃端点原生采样率」那条修复。
    """
    try:
        import sounddevice as sd
        with PA_LOCK:
            apis = [dict(a) for a in sd.query_hostapis()]
            wasapi = next((a for a in apis
                           if "WASAPI" in str(a.get("name", "")).upper()), None)
            if wasapi is not None:
                idx = int(wasapi.get("default_input_device", -1) or -1)
                if idx >= 0:
                    return idx
            dev = sd.default.device
            return int(dev[0]) if dev and len(dev) > 0 and dev[0] is not None else -1
    except Exception as exc:                    # noqa: BLE001 — 拿不到就回落 PortAudio 默认
        print(f"[devices] ⚠️ 取默认输入设备失败（{type(exc).__name__}: {exc}）"
              "→ 改用 PortAudio 默认输入", flush=True)
        return -1


def _sounddevice_input_name(index: int) -> str:
    """按 PortAudio 索引取设备名（用 sounddevice，与解析索引同一套口径）。"""
    try:
        import sounddevice as sd
        with PA_LOCK:
            return str(sd.query_devices(index).get("name") or "")
    except Exception:                           # noqa: BLE001 — 拿不到名字不是致命错
        return ""


def device_info_by_index(index: int) -> dict:
    """按 index 取设备信息（拿名字用）；取不到返回 {}。"""
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        p = pyaudio.PyAudio()
        try:
            return dict(p.get_device_info_by_index(index))
        except Exception:  # noqa: BLE001 — 拿不到名字不是致命错，调用方有兜底
            return {}
        finally:
            p.terminate()


# ---------------------------------------------------------------- 字体

# 雅黑优先（原作者实测调好的观感），其余按「有中日韩字形」的顺序兜底。
# 打不开字体时 Pillow 会抛 OSError: cannot open resource —— 那会让整条手腕屏腿挂掉，
# 所以宁可回落到任意一个可用字体，也不要把路径写死。
_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",         # 微软雅黑（简体，默认）
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "C:/Windows/Fonts/NotoSansJP-VF.ttf",  # 装过 Noto 的话，日文更好看
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)


def find_cjk_font() -> str | None:
    for cand in _CJK_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


# 泰文字体候选（按优先级）。
#
# ⚠️ **不能复用 CJK 字体**：实测 `C:/Windows/Fonts/msyh.ttc`（微软雅黑，默认 CJK 字体）
# 不含泰文字形 —— 用 PIL 渲染 `สวัสดี`，位图与「私有区缺字位 U+E000」的位图**完全相同**
# （= 豆腐块）。所以泰语必须走这条独立探测，渲染侧按书写系统切 run、各用各的字体画。
#
# 候选顺序：
#   1. Leelawadee UI（`LeelawUI.ttf`）—— Windows 8+ 自带的泰文 UI 字体，观感最好；
#   2. Tahoma（`tahoma.ttf`）—— Windows XP 起就带泰文字形，兜底最稳；
#   3. Noto Sans Thai —— 用户自己装过 Noto 字体族的话；
#   4. 其它可能含泰文的 Windows 字体（Arial Unicode MS 等）。
# 一个都没有时返回 None，由调用方降级并留痕：`overlay.resolve_thai_font_path` 先试
# 用户配置的字体，再回落到 CJK 字体（泰文会出豆腐块）+ 打一行告警（禁静默降级）。
_THAI_FONT_CANDIDATES = (
    "C:/Windows/Fonts/LeelawUI.ttf",     # Leelawadee UI（泰文 UI 字体，Win 8+）
    "C:/Windows/Fonts/tahoma.ttf",       # Tahoma（含泰文，Win XP 起）
    "C:/Windows/Fonts/NotoSansThai-Regular.ttf",
    "C:/Windows/Fonts/NotoSansThaiVF.ttf",
    "C:/Windows/Fonts/ARIALUNI.TTF",     # Arial Unicode MS（老版 Office 会装）
)


def find_thai_font() -> str | None:
    """找一个含泰文字形的字体文件路径；找不到返回 None（由调用方回落）。

    与 `find_cjk_font()` 是**两条独立**的探测：CJK 字体（雅黑等）不含泰文字形，
    泰文字体（Leelawadee UI 等）不含中日韩字形 —— 混排时必须按书写系统切 run、
    各用各的字体画，否则会出豆腐块。
    """
    for cand in _THAI_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


# ---------------------------------------------------------------- 界面语言

# Windows 主语言 ID → 界面语言。表里没有的（德语/法语等已知但未支持的语言）按 en 接待；
# 这是本平台自己的口径，`vlt/platform/linux.py` 有对等的一张表（读环境变量）。
_PRIMARY_LANG: dict[int, str] = {0x04: "zh", 0x09: "en", 0x11: "ja", 0x12: "ko", 0x19: "ru"}


def detect_ui_language() -> str:
    """读 Windows 用户默认 UI 语言（`GetUserDefaultUILanguage`）。

    返回 LANGID（如 0x0804=zh-CN、0x0409=en-US），低 10 位是主语言 ID。
    取不到值/异常 → "en"：用户口径「不是支持的语言就显示英文」——
    外国用户按英文接待远比按中文合理；中文环境的 LANGID 恒为 0x04，
    检测正常时绝不会掉进这条兜底。
    """
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        primary = int(langid) & 0x3FF
    except Exception:  # noqa: BLE001 — 任何意外：不认识 → 英文
        return "en"
    return _PRIMARY_LANG.get(primary, "en")


# ---------------------------------------------------------------- 采集

class PyaudioLoopbackSource(QueueAudioSource):
    """WASAPI loopback 采集（pyaudiowpatch）。

    整段读线程逻辑是从 `vlt/engine.py` **原样搬过来的**，两处踩过的坑都保留：

    1. 必须用 `get_read_available()` **非阻塞轮询**，不能用阻塞的 `stream.read()`：
       WASAPI loopback 在端点没有音频在播时 `read()` 会一直不返回（实测 3 秒 0 帧、
       读线程永久卡住）。
    2. 收尾时「关流 / PortAudio terminate」与「卡住的读」相撞 → **访问违规**
       （用户实测点「停止翻译」闪退，退出码 139，faulthandler 抓到 pyaudiowpatch read）。
       所以关流前必须先把读线程 join 掉 —— 这件事现在由 `QueueAudioSource.close()`
       的顺序保证（置 stop → join → 才 `_teardown`）。
    """

    label = "loopback"

    def __init__(self, loop, *, device_index: int, name: str, rate: int,
                 channels: int) -> None:
        # ⚠️ **按设备原生声道数打开，不要压成 2。**
        # WASAPI 的 loopback 端点只能用**端点混音格式**的声道数打开；压成 2 会被
        # PortAudio 直接拒掉 → `OSError: [Errno -9998] Invalid number of channels`，
        # 端点上一条音频都收不到。海外用户实测（2026-10-02，7.1 / 8 声道设备）：
        # 采集腿与设置里的电平条**都**报这个错 —— 电平条复用同一处代码。
        # 降混到单声道由下游负责（`to_16k_mono(pcm, rate, source.channels)` 按声道数取均值），
        # 所以这里按原生声道数开是安全的。
        # 个别设备只吃立体声 → 逐个候选试，**每个分支各留一行日志**（禁静默降级）。
        # 日志用 print：`[loopback]` 这一族在引擎里就是 print 进日志文件的（用户在界面上
        # 「打开日志文件夹」看的就是它），走 logging 会因 root 级别不够而整条消失。
        wanted = max(1, int(channels or 2))
        candidates = [wanted] if wanted == 2 else [wanted, 2]
        import pyaudiowpatch as pyaudio

        self.device_name = name
        self._chunk_max = int(rate * 0.1)
        self._pa = pyaudio.PyAudio()
        self._stream = None
        last_exc: Exception | None = None
        for ch in candidates:
            try:
                self._stream = self._pa.open(
                    format=pyaudio.paInt16, channels=ch, rate=rate,
                    frames_per_buffer=int(rate * 0.1), input=True,
                    input_device_index=device_index)
            except Exception as exc:  # noqa: BLE001 — 换声道数再试
                last_exc = exc
                print(f"[loopback] 采集端点「{name}」按 {ch} 声道打开失败：{exc}", flush=True)
                continue
            if ch == wanted:
                print(f"[loopback] 采集端点「{name}」{rate}Hz ×{ch}ch 打开成功", flush=True)
            else:
                print(f"[loopback] 采集端点「{name}」原生 {wanted}ch 打不开，"
                      f"已回落 {ch}ch 打开", flush=True)
            break
        else:
            self._pa.terminate()
            raise last_exc if last_exc is not None else RuntimeError("loopback 打不开")
        super().__init__(loop, rate=rate, channels=ch)

    def _pump(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                avail = self._stream.get_read_available()
            except Exception as exc:  # noqa: BLE001
                if not stop.is_set():
                    print(f"[loopback] 采集线程退出（poll）：{type(exc).__name__} "
                          f"errno={getattr(exc, 'errno', None)}", flush=True)
                break
            if avail <= 0:
                time.sleep(0.01)
                continue
            try:
                data = self._stream.read(min(avail, self._chunk_max),
                                         exception_on_overflow=False)
            except Exception as exc:  # noqa: BLE001
                if not stop.is_set():
                    print(f"[loopback] 采集线程退出（read）：{type(exc).__name__} "
                          f"errno={getattr(exc, 'errno', None)}", flush=True)
                break
            self._emit(data)

    def _teardown(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()


def query_devices_all() -> list[dict]:
    """**完整**设备表（含 MME / DirectSound / WDM-KS 的重复项与未启用端点）。

    只给「按名字打开失败后的回落」用 —— 界面与按名字解析一律走 `query_devices()`
    （WASAPI 已启用那一套）。每条同样带 `pa_index`（真实 PortAudio 索引）。

    ⚠️ 失败一律**当「没有回落候选」处理**（返回空表 + 留痕），绝不抛：
    它只是一条锦上添花的兜底路径；而且打桩环境里的 `sounddevice` 常常不是真的
    （实测 `tests/test_mic_device.py` 的假 `query_devices()` 返回的是单个 dict，
    直接迭代会炸）—— 那种环境本来也不需要回落候选。
    """
    try:
        import sounddevice as sd
        with PA_LOCK:
            devices = [dict(d) for d in sd.query_devices()]
    except Exception as exc:                # noqa: BLE001 — 见 docstring
        print(f"[devices] ⚠️ 取完整设备表失败（{type(exc).__name__}: {exc}）"
              "→ 本次没有同名回落候选", flush=True)
        return []
    for i, d in enumerate(devices):
        d["pa_index"] = i
    return devices


def same_name_fallbacks(name: str, kind: str, *, exclude: int | None = None) -> list[int]:
    """同一设备名在**其它 host API** 下的索引（按 host API 顺序）—— 打开失败后的回落候选。

    为什么需要（2026-10-02 测试者真机，两起）：

      · **WASAPI 共享模式只接受端点自己的采样率**：麦克风按 16kHz 打开会被拒
        （`PortAudioError: … Invalid sample rate [PaErrorCode -9997]`）；而 MME/DirectSound
        会自己重采样，所以老路径（那时解析命中的是 MME）一直能用。
      · 个别虚拟声卡端点（Voicemeeter）在 WASAPI 下 KS 属性查询失败：
        `-9999 Unanticipated host error … WdmSyncIoctl … DeviceIoControl GLE = 0x490`
        （同一台机器上它的 MME 条目能正常打开）。

    按同名回落到别的 host API（通常是 MME）能就地打开，比直接判死强；每次尝试都留痕。
    """
    want = str(name or "").strip().lower()
    if not want:
        return []
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out: list[int] = []
    for d in query_devices_all():
        if int(d.get(key, 0) or 0) <= 0:
            continue
        if str(d.get("name", "")).strip().lower() != want:
            continue
        idx = int(d.get("pa_index", 0))
        if idx == exclude or idx in out:
            continue
        out.append(idx)
    return out


def device_native_rate(index: int) -> int:
    """该 PortAudio 索引对应设备的**原生**采样率；取不到返回 0。"""
    try:
        import sounddevice as sd
        info = sd.query_devices(index)      # 传索引 → 直接得到该设备的 dict
        return int(info.get("default_samplerate", 0) or 0)
    except Exception:                       # noqa: BLE001 — 拿不到就不改采样率
        return 0


class SoundDeviceMicSource(QueueAudioSource):
    """麦克风采集（sounddevice / PortAudio）—— **Windows 独占**。

    Linux 麦克风走原生 `pw-record`（见 `vlt/platform/linux.py: LinuxMicSource`），不经过
    PortAudio。本类特意放在 `win.py`（Linux 产物里被 `--exclude-module vlt.platform.win`
    剔除）：它在模块图里 `import sounddevice`，会把 PortAudio 及其依赖
    （`libportaudio` / `libasound` / `libjack`）拉进包 —— Linux 已不需要，不该为它付这个代价。

    Windows 用 **PortAudio 索引**打开（`device=<int>`）—— 由 `win.open_mic` 经
    `resolve_device_name(name, "input")` 解析得到，与 v0.3.x 的口径一致（同名端点并存时，
    选中的物理端点才不会漂）。构造函数仍容忍 `str | int | None`（测试/兼容用）。

    ## 多声道输入

    按调用方给的 `channels` 打开（`win.open_mic` 负责解析出**端点原生声道数**）。开成功后把
    `self.channels` 回填成**实际**声道数，由下游 `engine.to_16k_mono` /
    `micproxy.resample_to_48k_stereo` 取均值降为单声道 —— 双声道 / 5.1 / 7.1 输入因此不会
    只剩第一路。设备只吃 2/1 声道时按 `channel_fallbacks` 逐级回落。
    """

    label = "mic"

    def __init__(self, loop, device: str | int | None, *, rate: int = 16000,
                 channels: int = 1, blocksize: int = 1600,
                 fallbacks: "list[int] | tuple[int, ...]" = (),
                 channel_fallbacks: "list[int] | tuple[int, ...]" = ()) -> None:
        super().__init__(loop, rate=rate, channels=max(1, int(channels)))
        # ⚠️ 不能用 `device or None`：PortAudio 的索引 0 是合法设备，
        #    被 `or` 判成假值就悄悄回落默认设备了。
        self._device = None if device in (None, "") else device
        self._blocksize = blocksize
        #: 首选设备打不开时按序再试的候选（同名设备的其它 host API 条目）
        self._fallbacks = tuple(int(f) for f in fallbacks if f != self._device)
        #: 首选声道数打不开时按序再试的候选（多声道设备被拒时降到 2/1；见 mic_channel_fallbacks）
        want = self.channels
        self._channel_candidates = tuple(
            [want] + [int(c) for c in channel_fallbacks if int(c) >= 1 and int(c) != want])

    def _pump(self, stop: threading.Event) -> None:
        import sounddevice as sd

        def callback(indata, frames, time_info, status):   # noqa: ANN001
            if status:
                log.debug("[mic] callback status: %s", status)
            self._emit(bytes(indata))

        # 候选顺序：设备（首选 → 同名回落）× 声道数（原生 → 逐级回落到 2/1）。
        # 每一次失败都留痕 —— 「麦克风没声音」最难查的就是「到底开的是哪个设备、几声道、为什么没开成」。
        # ★ 多声道设备**按原生声道数打开**（1/2/6/8 都接），下游 `to_16k_mono` 按
        #   `source.channels` 取均值降为单声道。
        candidates = [(self._device, False)] + [(f, True) for f in self._fallbacks]
        last_exc: Exception | None = None
        for dev, is_dev_fallback in candidates:
            for ch in self._channel_candidates:
                # ★ 先回填再开流：PortAudio 可能在 open 返回后立刻回调，先设好才不会
                #   让引擎拿到「上一轮/构造时」的声道数去降混。
                self.channels = ch
                try:
                    # `with` 退出即关流；而 close() 是「先置 stop → 再 join」，
                    # 所以关流时采集线程已经不再产生新数据 —— 顺序与 loopback 侧一致。
                    with sd.RawInputStream(samplerate=self.rate, channels=ch,
                                           dtype="int16", blocksize=self._blocksize,
                                           device=dev, callback=callback):
                        if is_dev_fallback:
                            print(f"[mic] 首选设备 #{self._device} 打不开，"
                                  f"已回落到同名设备 #{dev}（{self.rate}Hz ×{ch}ch）", flush=True)
                        elif ch != self._channel_candidates[0]:
                            print(f"[mic] 设备 #{dev} 原生 {self._channel_candidates[0]}ch 被拒，"
                                  f"已回落到 {ch}ch（下游仍降为单声道）", flush=True)
                        elif ch > 1:
                            print(f"[mic] 设备 #{dev} 按 {ch} 声道打开（下游降混为单声道）",
                                  flush=True)
                        stop.wait()
                    return
                except Exception as exc:  # noqa: BLE001 — 换候选再试
                    last_exc = exc
                    print(f"[mic] 设备 #{dev} 按 {ch} 声道打不开（{self.rate}Hz）：{exc}",
                          flush=True)
                    continue
        log.warning("[mic] 所有候选设备/声道都打不开（%d 设备 × %d 声道）：%s",
                    len(candidates), len(self._channel_candidates), last_exc)
        if last_exc is not None:
            raise last_exc


def mic_channel_fallbacks(primary: int) -> tuple[int, ...]:
    """多声道设备被拒时的**声道回落候选**：只保留比 `primary` 小的 2 / 1。

    与 loopback 侧口径一致（`PyaudioLoopbackSource` 原生失败回落到 2）—— 目的是
    「多声道一定能全采到；万一设备只吃 2/1 声道，也要能开起来，而不是直接判死」。
    """
    out: list[int] = []
    for c in (2, 1):
        if c < int(primary) and c not in out:
            out.append(c)
    return tuple(out)


def open_mic(device_name: str | None, *, rate: int = 16000, channels: int | None = None,
             blocksize: int) -> AudioSource:
    """麦克风采集（与 Linux 共用同一个实现）。

    ⚠️ **Windows 按 PortAudio 索引打开**（`device=<int>`），这是 v0.3.x 的口径，
    不要退化成「直接把名字丢给 sounddevice」：`sd.RawInputStream(device="名字")`
    用的是 PortAudio 自己的取名规则，与我们的 `resolve_device_name`（全名精确 →
    忽略大小写 → 去重子串）口径不同 —— 同名端点（两块同型号声卡 / 多个虚拟声卡）
    并存时，可能打开到与旧版不同的物理端点。

    索引由 `resolve_device_name(..., "input")` 从 **sounddevice 自己的设备表**解析，
    正是 PortAudio 要的那个；解析不到则回落默认输入设备（IndexError 交给我们）。

    ## 采样率：按**设备原生采样率**打开（2026-10-02 测试者真机事故）

    原来一律按 16kHz 打开，靠 PortAudio 自己重采样。设备表收敛到 WASAPI 之后这条路断了：
    **WASAPI 共享模式只接受端点自己的采样率**，要 16kHz 直接
    `PortAudioError: Error opening RawInputStream: Invalid sample rate [PaErrorCode -9997]`
    —— 表现是「麦克风采集线程一启动就退出」，等于自己说话那条腿全哑。

    现在拿设备的原生采样率开（拿不到才用入参），下游 `engine._pump_capture` 里那句
    `to_16k_mono(chunk, source.rate, source.channels)` 本来就负责转 16kHz —— 与 loopback 腿同一口径。
    `blocksize` 跟着采样率走，保持约 100ms 一块。
    另外带上**同名回落候选**（见 `same_name_fallbacks`），首选打不开时按序再试并留痕。

    ## 声道数（双声道 / 5.1 / 7.1 都要全采）

    `channels=None`（默认）= 按**端点原生输入声道数**打开（见 `_endpoint_input_channels`）：
    WASAPI 端点报的是真实声道数（与 `PyaudioLoopbackSource` 同一口径），全声道采集后由
    `to_16k_mono` 取均值降为单声道。只开 1 声道会丢掉其余声道。设备只吃 2/1 声道时按
    `channel_fallbacks` 逐级回落。`channels=int` = 显式指定。
    """
    import asyncio

    index: int | None = None
    rate_use = int(rate or 16000)
    fallbacks: list[int] = []
    if device_name:
        from ..devices import resolve_device_name   # 局部导入，避免与 devices 循环导入
        index = resolve_device_name(device_name, "input")
        if index is None:
            log.warning("[mic] 未找到设备 %r，回退系统默认输入设备", device_name)
        else:
            native = device_native_rate(index)
            if native and native != rate_use:
                rate_use = native
            fallbacks = same_name_fallbacks(device_name, "input", exclude=index)
    else:
        # ★「自动检测」也解析成**具体索引**，再走与手选完全相同的通路（原生采样率 +
        #   端点声道数 + 同名回落）。不能停在 `device=None`：那样会绕过「WASAPI 只吃
        #   端点原生采样率」这条修复，自动时有概率直接 -9997。
        idx = default_input_index()
        if idx >= 0:
            index = idx
            name = _sounddevice_input_name(idx)
            print(f"[mic] 自动检测 → 系统默认输入设备：{name or '（未知）'!r}（#{idx}）", flush=True)
            native = device_native_rate(idx)
            if native and native != rate_use:
                rate_use = native
            if name:
                fallbacks = same_name_fallbacks(name, "input", exclude=idx)
        else:
            print("[mic] 自动检测：拿不到系统默认输入设备 → 改用 PortAudio 默认输入", flush=True)

    if rate_use != rate:
        blocksize = int(rate_use * 0.1)        # 保持 ~100ms 一块（块大小跟着采样率走）

    ch = max(1, int(channels)) if channels else _endpoint_input_channels(index)
    src = SoundDeviceMicSource(asyncio.get_running_loop(), index,
                               rate=rate_use, channels=ch, blocksize=blocksize,
                               fallbacks=fallbacks, channel_fallbacks=mic_channel_fallbacks(ch))
    src.start()
    return src


def _endpoint_input_channels(index: int | None) -> int:
    """端点（`index=None` = 默认输入）的**原生输入声道数**；取不到返回 2。

    ⚠️ 与 loopback 那条腿同一口径：WASAPI 只吃端点自己的声道数，压成别的会被
    PortAudio 拒（`-9998 Invalid number of channels`，见 `PyaudioLoopbackSource`）。
    Windows 端点报的就是真实声道数，没有 Linux ALSA 插件「虚报 128」那种坑。
    """
    try:
        import sounddevice as sd
        info = sd.query_devices(index) if index is not None else sd.query_devices(kind="input")
        n = int(info.get("max_input_channels", 0) or 0)
        return n if n >= 1 else 2
    except Exception:                           # noqa: BLE001 — 查不到不该让整条腿挂掉
        return 2


def open_loopback(target: LoopbackTarget, *, blocksize: int) -> AudioSource:
    """打开一路 WASAPI loopback 采集。`target.id` 是设备 index 的字符串形式。"""
    import asyncio

    src = PyaudioLoopbackSource(asyncio.get_running_loop(),
                                device_index=int(target.id), name=target.name,
                                rate=target.sample_rate, channels=target.channels)
    src.start()
    return src


# ---------------------------------------------------------------- 手腕屏后端

def create_wrist_overlay(cfg: Any, config_path: Any = None, dry_run: bool = False) -> Any:
    """手腕屏后端：Windows 用 pyopenvr 的 `IVROverlay`（`vlt/output/overlay.py`）。

    这个工厂放在平台模块里，是为了让共享代码（engine/gui）**不出现任何后端名字** ——
    Windows 产物里就不该有 openxr 的字样，反之亦然（见 vlt/platform/base.py 的说明）。
    """
    from ..output.openvr_overlay import WristOverlay
    return WristOverlay(cfg, config_path=config_path, dry_run=dry_run)


# ---------------------------------------------------------------- 麦克风代理

def create_mic_proxy(audio_cfg: dict, mic_name: str | None = None,
                     on_status=None) -> Any:  # noqa: ANN001
    """麦克风代理（原声 / 译音一键切）：Windows 走 PortAudio 输出流的原版实现。

    与 Linux 版（`vlt/output/micproxy_linux.py`，`pw-cat` 管道 + 运行时声明虚拟麦）
    接口与语义对齐；工厂留在平台模块里，共享代码里不出现任何后端名字。
    """
    from ..output.micproxy import MicProxy
    return MicProxy(audio_cfg=audio_cfg, mic_name=mic_name,
                    on_status=on_status or (lambda *_a: None))


# ---------------------------------------------------------------- 桌面叠加窗（issue #11）
#
# 桌面（非 VR）模式下的字幕窗：Tk 负责画，这里只提供 Tk 拿不到的那几件 Win32 事实 ——
# 顶层 HWND、目标窗口客户区、鼠标穿透/不抢焦点的扩展样式、工作区（主屏那一份，
# 以及**目标窗口所在显示器**那一份 —— 多屏时只有后者才不会把副屏字幕夹回主屏）。
#
# ⚠️ 共享模块（`vlt/output/desktop_overlay.py`）**只能通过 `vlt/platform/__init__.py`
#    的门面调这些函数**：另一侧平台没有对等实现，门面会返回安全默认值，
#    所以 Linux 侧一个文件都不用改（见 vlt/platform/__init__.py 的说明）。
#
# ⚠️ 所有函数都**不抛异常**：调用点在 50ms 一跳的 tick 里，抛出去会把整条翻译腿打断。
#    取不到就返回 None / False / 原值，由调用方决定怎么降级（并留一行日志）。

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020       # 鼠标穿透
_WS_EX_TOOLWINDOW = 0x00000080        # 不进 alt-tab
_WS_EX_LAYERED = 0x00080000           # 分层窗口（色键 / 整窗透明度都要它）
_WS_EX_NOACTIVATE = 0x08000000        # 显示时不抢焦点
_SPI_GETWORKAREA = 0x0030
_SM_CXSCREEN, _SM_CYSCREEN = 0, 1
# 窗口不与任何显示器相交（移出屏幕外/正在销毁）时，`MonitorFromWindow` 仍返回**最近**
# 的那块屏 —— 比返回 NULL 再回落主屏更贴近用户的直觉（字幕就在那块屏附近）。
_MONITOR_DEFAULTTONEAREST = 2


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


def _get_ex_style(hwnd: int) -> int | None:
    """读窗口扩展样式（GWL_EXSTYLE）；失败返回 None。"""
    try:
        user32 = ctypes.windll.user32
        fn = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        fn.restype = ctypes.c_ssize_t
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return int(fn(ctypes.c_void_p(int(hwnd)), _GWL_EXSTYLE)) & 0xFFFFFFFF
    except Exception:  # noqa: BLE001 — 32/64 位差异、句柄失效都走这里
        return None


def _set_ex_style(hwnd: int, style: int) -> bool:
    """写窗口扩展样式。**读回来确认**才算成功 —— SetWindowLongPtr 返回 0 既可能是
    「失败」也可能是「原值就是 0」，靠返回值判断会误报。"""
    try:
        user32 = ctypes.windll.user32
        fn = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        fn.restype = ctypes.c_ssize_t
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        fn(ctypes.c_void_p(int(hwnd)), _GWL_EXSTYLE, ctypes.c_ssize_t(int(style)))
    except Exception:  # noqa: BLE001
        return False
    return _get_ex_style(hwnd) == (int(style) & 0xFFFFFFFF)


def top_level_hwnd(widget_id: int) -> int:
    """Tk 的 `winfo_id()` 给的是**子窗口** HWND，顶层要再 `GetParent()` 一次。

    拿不到父窗口（本来就是顶层 / 句柄无效）就返回原值 —— 调用方要的是「能设扩展样式的
    那个窗口」，两种情况都满足。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        parent = user32.GetParent(ctypes.c_void_p(int(widget_id)))
        return int(parent) if parent else int(widget_id)
    except Exception:  # noqa: BLE001
        return int(widget_id)


def find_window_by_title(substr: str, exclude: Any = ()) -> int | None:
    """按标题子串（不区分大小写）找**可见的顶层窗口**；找不到返回 None。

    ⚠️ 标题**完全相等**的优先：本程序主窗口标题是「VRChat 实时同传」，也含 "VRChat" ——
    只取 Z 序第一个命中的话，游戏窗口没起来时会贴到**我们自己的界面**上。
    仍然保留子串匹配（用户可能把 VRChat 窗口改名 / 多开），只是让精确命中优先。

    `exclude`：要排除的窗口句柄（可迭代）。调用方把**自己的**窗口（本程序主窗
    与字幕窗）传进来 —— 精确匹配只能挡住「VRChat 实时同传」这种，字幕窗和未来
    别的自建窗不该靠标题去赌。
    """
    needle = (substr or "").strip().lower()
    if not needle:
        return None
    try:
        skip = {int(v) for v in (exclude or ()) if v}
    except TypeError:
        skip = {int(exclude)} if exclude else set()          # 传了单个 int 也认
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]

    exact: list[int] = []
    partial: list[int] = []

    def _cb(hwnd, _lparam) -> bool:
        try:
            if not hwnd or int(hwnd) in skip or not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            low = title.lower()
            if low == needle:
                exact.append(int(hwnd))
                return False                    # 精确命中，不用再找了
            if needle in low:
                partial.append(int(hwnd))
        except Exception:  # noqa: BLE001 — 单个窗口读不到标题不影响其余
            pass
        return True

    try:
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_cb)
        user32.EnumWindows(proc, 0)
    except Exception:  # noqa: BLE001
        return None
    return (exact or partial or [None])[0]


def window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口**客户区**在屏幕上的矩形 (left, top, right, bottom)；失败返回 None。

    用客户区而不是 GetWindowRect：字幕要贴在画面里，标题栏/边框那块不算。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetClientRect.restype = ctypes.c_bool
        user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.ClientToScreen.restype = ctypes.c_bool
        user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(_POINT)]
        rc = _RECT()
        if not user32.GetClientRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(rc)):
            return None
        w, h = int(rc.right) - int(rc.left), int(rc.bottom) - int(rc.top)
        if w <= 0 or h <= 0:                    # 最小化 / 还没布局完
            return None
        pt = _POINT(int(rc.left), int(rc.top))
        if not user32.ClientToScreen(ctypes.c_void_p(int(hwnd)), ctypes.byref(pt)):
            return None
        return (int(pt.x), int(pt.y), int(pt.x) + w, int(pt.y) + h)
    except Exception:  # noqa: BLE001
        return None


def is_window(hwnd: int) -> bool:
    """句柄还是一个真窗口吗（游戏退了 / 换了场景就会变 False）。"""
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.IsWindow.restype = ctypes.c_bool
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        return bool(user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:  # noqa: BLE001
        return False


def set_click_through(hwnd: int, on: bool) -> bool:
    """开/关鼠标穿透（`WS_EX_LAYERED | WS_EX_TRANSPARENT`）。

    ⚠️ 必须**读改写**：直接写死一个常量会把 Tk 的 `-transparentcolor` / `-alpha`
    依赖的 `WS_EX_LAYERED` 冲掉（字幕会变成一块实心矩形）。关穿透时也只摘
    `WS_EX_TRANSPARENT`，保留 LAYERED。
    """
    style = _get_ex_style(hwnd)
    if style is None:
        return False
    style |= _WS_EX_LAYERED
    style = (style | _WS_EX_TRANSPARENT) if on else (style & ~_WS_EX_TRANSPARENT)
    return _set_ex_style(hwnd, style)


def set_tool_window(hwnd: int) -> bool:
    """不抢焦点 + 不进 alt-tab（`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`）。

    抢焦点的后果很具体：字幕窗一刷新就把输入焦点从游戏里夺走，用户打字/按键全丢。
    """
    style = _get_ex_style(hwnd)
    if style is None:
        return False
    return _set_ex_style(hwnd, style | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)


def screen_work_area() -> tuple[int, int, int, int]:
    """主屏**工作区** (left, top, right, bottom)：不含任务栏，字幕不该被它挡住。

    `SPI_GETWORKAREA` 优先；拿不到（策略限制等）退 `GetSystemMetrics` 的整屏尺寸；
    再不行给一个 1920x1080 的保守值 —— 调用方会拿它做夹取，返回 (0,0,0,0) 会把
    字幕夹到左上角一个点，比给个粗略值更糟。
    """
    try:
        user32 = ctypes.windll.user32
        user32.SystemParametersInfoW.restype = ctypes.c_bool
        user32.SystemParametersInfoW.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        rc = _RECT()
        if user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rc), 0):
            if int(rc.right) > int(rc.left) and int(rc.bottom) > int(rc.top):
                return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))
    except Exception:  # noqa: BLE001
        pass
    try:
        user32 = ctypes.windll.user32
        w = int(user32.GetSystemMetrics(_SM_CXSCREEN))
        h = int(user32.GetSystemMetrics(_SM_CYSCREEN))
        if w > 0 and h > 0:
            return (0, 0, w, h)
    except Exception:  # noqa: BLE001
        pass
    return (0, 0, 1920, 1080)


def monitor_work_area(hwnd: int) -> tuple[int, int, int, int]:
    """`hwnd` **所在那块显示器**的工作区；拿不到就回落到 `screen_work_area()`。

    多显示器时这条是必需的：`SPI_GETWORKAREA` 只有**主屏**那一份工作区，副屏上的
    字幕会被它夹回主屏 —— 左侧副屏的坐标本来就是负的，一夹就直接飞到主屏左上角。
    所以贴窗时按「目标窗口待着的那块屏」取工作区，才是用户眼里的那块屏。

    ⚠️ 返回值**可能是负坐标**（副屏在主屏左侧/上方）：不要做任何 `max(0, ...)` 钳制。
    """
    if not hwnd:
        return screen_work_area()
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetMonitorInfoW.restype = ctypes.c_bool
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
        mon = user32.MonitorFromWindow(ctypes.c_void_p(int(hwnd)),
                                       _MONITOR_DEFAULTTONEAREST)
        if not mon:
            return screen_work_area()
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)      # 忘了填这个 GetMonitorInfoW 直接失败
        if not user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
            return screen_work_area()
        left, top = int(mi.rcWork.left), int(mi.rcWork.top)
        right, bottom = int(mi.rcWork.right), int(mi.rcWork.bottom)
        if right <= left or bottom <= top:
            return screen_work_area()
        return (left, top, right, bottom)
    except Exception:  # noqa: BLE001 — 句柄失效 / 远程桌面 / 老系统都走这里
        return screen_work_area()
