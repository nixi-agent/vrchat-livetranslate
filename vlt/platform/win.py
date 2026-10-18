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
from . import first_openable_font
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

    候选逐份**打开校验**（`first_openable_font`）：路径存在但 Pillow 打不开的候选会被跳过。
    """
    return first_openable_font(_THAI_FONT_CANDIDATES)


# 韩文字体候选（按优先级）。
#
# ⚠️ **不能复用 CJK 字体**：与泰文同因。实测 `C:/Windows/Fonts/msyh.ttc`（微软雅黑，
# 本项目默认 CJK 字体）**不含韩文字形** —— 用 PIL 渲染 `안녕하세요`，位图与渲染
# 「私有区缺字位 U+E000 × 5」的位图**逐字节完全相同**（= 豆腐块，用户 2026-10 报的
# 「韩语显示成方框」就是这个）。所以韩语也要走独立探测 + 按书写系统切 run 渲染。
#
# 候选顺序：
#   1. Malgun Gothic（`malgun.ttf`）—— Windows Vista+ 自带的韩文 UI 字体（实测中文 Win11
#      上也存在，即**不装韩文语言包也有**），观感最好；
#   2. Malgun Gothic Semilight / 굴림 / 바탕 —— 굴림・바탕 属「韩文补充字体」可选组件，
#      装了才在，作后备；
#   3. Noto Sans KR / Noto Sans CJK —— 用户自己装过 Noto 字体族的话；
#   4. Arial Unicode MS（老版 Office 会装，含韩文音节）。
# 一个都没有时返回 None，由调用方降级并留痕（`overlay.resolve_korean_font_path` 先试
# 用户配置的字体，再回落到 CJK 字体 → 韩文出豆腐块 + 打一行告警，禁静默降级）。
_KOREAN_FONT_CANDIDATES = (
    "C:/Windows/Fonts/malgun.ttf",       # Malgun Gothic（韩文 UI 字体，Vista+ 自带）
    "C:/Windows/Fonts/malgunsl.ttf",     # Malgun Gothic Semilight
    "C:/Windows/Fonts/gulim.ttc",        # 굴림（韩文补充字体，可选组件）
    "C:/Windows/Fonts/batang.ttc",       # 바탕
    "C:/Windows/Fonts/NotoSansKR-Regular.otf",
    "C:/Windows/Fonts/NotoSansKR-VF.ttf",
    "C:/Windows/Fonts/NotoSansCJK-Regular.ttc",   # 装了 Noto CJK 的话（该族含韩文）
    "C:/Windows/Fonts/ARIALUNI.TTF",     # Arial Unicode MS（老版 Office 会装）
)


def find_korean_font() -> str | None:
    """找一个含韩文字形的字体文件路径；找不到返回 None（由调用方回落）。

    与 `find_cjk_font()` / `find_thai_font()` 是**三条独立**的探测：CJK 字体不含韩文与泰文，
    韩文字体（Malgun Gothic 等）不含中日文字形 —— 混排时必须按书写系统切 run、各用各的字体画。

    候选逐份**打开校验**（`first_openable_font`）：路径存在但 Pillow 打不开的候选会被跳过，
    免得把坏路径交给上层变成「静默回落 = 一排豆腐块」。
    """
    return first_openable_font(_KOREAN_FONT_CANDIDATES)


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


# ---------------------------------------------------------------- 进程级 loopback（Win10 21H2+）
#
# 为什么要有这条腿：端点 loopback 抓的是**整个输出设备的混音** —— 浏览器、音乐、
# 系统提示音都会被当成「游戏内语音」喂给模型（Linux 腿早就按应用流节点抓，见
# `vlt/platform/linux.py::find_vrchat_output_streams` 里那段说明）。Windows 的等价能力
# 是 WASAPI **process loopback**：`ActivateAudioInterfaceAsync` +
# `VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK`，只抓指定进程树。
#
# 实测结论（2026-10，Win11 26200，纯 ctypes，无新依赖；脚本见 Hermes spike）：
#   · 激活 1.7–2.3ms；包长恒定 480 帧 = 10ms；LOOPBACK|EVENTCALLBACK 稳定出数据
#   · 只抓目标进程：子进程 1kHz → 主频 1000.0Hz / SNR 134dB；换别的 PID → 全零
#   · **不抓自己**：同一刻本进程播 500Hz → 500Hz 频段 -90.6dB（这是「耳返不会被
#     自己采集」的物理保证）
#   · `GetMixFormat()` / `IsFormatSupported()` 恒 `E_NOTIMPL` → 格式必须硬编码
#   · `WAVEFORMATEX` **必须 `_pack_=1`**（mmreg.h 是 18 字节；ctypes 默认对齐 20 →
#     `Initialize` 一律 `E_INVALIDARG`，看起来像"格式不支持"，很能骗人）
#   · `AUDCLNT_BUFFERFLAGS_SILENT` 实测**从未出现**：静音就是 480 帧全零，别靠 flag 判静音
#   · 电平：同一素材比端点后端热约 +7.45dB（自播 +2.8dB）→ 换腿后输入门限要重新标定，
#     这也是它**默认关闭**（`capture.loopback_mode: endpoint`）的原因之一
#
# ⚠️ 契约：`QueueAudioSource` 的下游（`engine.to_16k_mono`）只认 **int16le**，
#    而这里收到的是 float32。转换必须在本模块内完成 —— 直接喂 float32 不会报错，
#    只会让 ASR 收到一片噪声（实测那类 bug 极难从日志看出来）。
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
#: 官方文档写 Win10 build 20348，实测 19044/19045（21H2/22H2）可用（OBS 28+ 同口径）。
PROCESS_LOOPBACK_MIN_BUILD = 19044
_PROCESS_LOOPBACK_MODE_INCLUDE_TREE = 0
_PROCESS_LOOPBACK_ACTIVATION_TYPE = 1

_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_WAVE_FORMAT_IEEE_FLOAT = 0x0003
_KSAUDIO_SPEAKER_STEREO = 0x3
_VT_BLOB = 65
_COINIT_MULTITHREADED = 0x0

_HR_NAMES = {
    0x00000000: "S_OK", 0x80004001: "E_NOTIMPL", 0x80004002: "E_NOINTERFACE",
    0x80070057: "E_INVALIDARG", 0x80070005: "E_ACCESSDENIED",
    0x80010106: "RPC_E_CHANGED_MODE", 0x8000000A: "E_PENDING",
    0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED", 0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED",
    0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT", 0x88890021: "AUDCLNT_E_INVALID_STREAM_FLAG",
}


def _hr_name(hr: int) -> str:
    return _HR_NAMES.get(hr & 0xFFFFFFFF, f"0x{hr & 0xFFFFFFFF:08X}")


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s: str) -> None:
        super().__init__()
        parts = s.strip("{}").split("-")
        self.Data1 = int(parts[0], 16)
        self.Data2 = int(parts[1], 16)
        self.Data3 = int(parts[2], 16)
        for i, b in enumerate(bytes.fromhex(parts[3] + parts[4])):
            self.Data4[i] = b

    def __eq__(self, other: object) -> bool:      # noqa: D105 —— 只比较字节
        return bytes(self) == bytes(other)


_IID_IUNKNOWN = _GUID("00000000-0000-0000-C000-000000000046")
_IID_IAGILEOBJECT = _GUID("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
_IID_IMARSHAL = _GUID("00000003-0000-0000-C000-000000000046")
_IID_IAUDIOCLIENT = _GUID("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
_IID_IAUDIOCAPTURECLIENT = _GUID("C8ADBD64-E71E-48a0-A4DE-185C395CD317")
_IID_IACTIVATEAUDIOINTERFACECOMPLETIONHANDLER = _GUID("41D949AB-9862-444A-80F6-C261334DA5EB")

_HRESULT = ctypes.c_long
_E_NOINTERFACE = -2147467262
# Linux 上**不存在** `ctypes.WINFUNCTYPE`，但多条既有用例会 import 本模块只验纯逻辑
# （`test_loopback_errors` / `test_device_wasapi_filter` / `test_i18n` 的 langid 用例…）。
# 占位成 CFUNCTYPE：真正的 COM 调用只会在 Windows 上发生，Linux 上这些类型不会被调用。
_WINFUNCPTR = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

_QI_FN = _WINFUNCPTR(_HRESULT, ctypes.c_void_p, ctypes.POINTER(_GUID),
                            ctypes.POINTER(ctypes.c_void_p))
_AR_FN = _WINFUNCPTR(ctypes.c_ulong, ctypes.c_void_p)
_AC_FN = _WINFUNCPTR(_HRESULT, ctypes.c_void_p, ctypes.c_void_p)


class _WAVEFORMATEX(ctypes.Structure):
    # ⚠️ `_pack_ = 1` 不是洁癖：mmreg.h 用 pshpack1，18 字节；不压紧就是 20，
    #    `IAudioClient::Initialize` 一律 `E_INVALIDARG`（实测烧掉一轮排查）。
    _pack_ = 1
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_ulong), ("nAvgBytesPerSec", ctypes.c_ulong),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("blobSize", ctypes.c_ulong), ("pad", ctypes.c_ulong),
                ("pBlob", ctypes.c_void_p)]


class _PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", ctypes.c_ulong),
                ("ProcessLoopbackMode", ctypes.c_int)]


class _ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int),
                ("ProcessLoopbackParams", _PROCESS_LOOPBACK_PARAMS)]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


class _OSVERSIONINFOW(ctypes.Structure):
    _fields_ = [("dwOSVersionInfoSize", ctypes.c_ulong), ("dwMajorVersion", ctypes.c_ulong),
                ("dwMinorVersion", ctypes.c_ulong), ("dwBuildNumber", ctypes.c_ulong),
                ("dwPlatformId", ctypes.c_ulong), ("szCSDVersion", ctypes.c_wchar * 128)]


def _vcall(ptr, index: int, restype, argtypes: list, *args):
    """按 vtable 下标调一个原始 COM 接口指针。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = _WINFUNCPTR(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(ptr, *args)


def _com_release(ptr) -> None:
    if ptr:
        try:
            _vcall(ptr, 2, ctypes.c_ulong, [])
        except Exception:                        # noqa: BLE001 — 释放失败不该拖垮收尾
            pass


def _windows_build() -> int:
    """真实 build 号（`RtlGetVersion`；`GetVersionEx` 会被兼容层骗）。"""
    try:
        info = _OSVERSIONINFOW()
        info.dwOSVersionInfoSize = ctypes.sizeof(info)
        if ctypes.WinDLL("ntdll").RtlGetVersion(ctypes.byref(info)) == 0:
            return int(info.dwBuildNumber)
    except Exception:                            # noqa: BLE001
        pass
    return 0


def process_loopback_available() -> tuple[bool, str]:
    """这条腿在这台机器上能不能用。返回 (可用, 原因)；原因会进日志。"""
    build = _windows_build()
    if build and build < PROCESS_LOOPBACK_MIN_BUILD:
        return False, f"系统 build {build} < {PROCESS_LOOPBACK_MIN_BUILD}"
    try:
        api = getattr(ctypes.WinDLL("Mmdevapi.dll"), "ActivateAudioInterfaceAsync", None)
    except Exception as exc:                     # noqa: BLE001
        return False, f"Mmdevapi 不可用：{type(exc).__name__}"
    if api is None:
        return False, "Mmdevapi 里没有 ActivateAudioInterfaceAsync"
    return True, f"build {build or '?'}"


def find_process_pid(exe_name: str = "VRChat.exe") -> int | None:
    """按可执行文件名找进程（Toolhelp 快照）。找不到返回 None。

    ⚠️ 必须打**真正出声的那个进程**：venv 的 `python.exe` 是跳板，`Popen.pid` 与实际
    发声线程所属进程不同 → 瞄准跳板会「激活成功 + 完美静音」（实测踩过）。
    VRChat 场景打 `VRChat.exe` 自身即可；进程树模式会把它的子进程一起带上。
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    th32cs_snapproc = 0x00000002
    invalid = ctypes.c_void_p(-1).value
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(th32cs_snapproc, 0)
    if not snap or snap == invalid:
        return None
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        want = exe_name.lower()
        found: list[int] = []
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            if str(entry.szExeFile).lower() == want:
                found.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
        if not found:
            return None
        if len(found) > 1:
            print(f"[loopback] 匹配到 {len(found)} 个 {exe_name}（PID {found}），取第一个",
                  flush=True)
        return found[0]
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))


def pid_alive(pid: int) -> bool:
    """进程是否还活着（`STILL_ACTIVE`）。用于判断「VRChat 退出 → 回等待」。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    handle = k32.OpenProcess(0x1000, False, int(pid))      # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return True                                    # 查不到就别当它死了
        return code.value == 259                           # STILL_ACTIVE
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


#: COM 回调只给得到 `this` 指针 → 用它反查实例（每个采集流一个 handler）。
_HANDLER_BY_ADDR: dict[int, "_CompletionHandler"] = {}


def _handler_of(this):
    try:
        return _HANDLER_BY_ADDR.get(int(this))
    except (TypeError, ValueError):
        return None


def _h_query_interface(this, priid, ppv):
    inst = _handler_of(this)
    if not ppv:
        return _E_NOINTERFACE
    ppv[0] = None
    riid = priid.contents
    if riid in (_IID_IUNKNOWN, _IID_IAGILEOBJECT,
                _IID_IACTIVATEAUDIOINTERFACECOMPLETIONHANDLER):
        ppv[0] = ctypes.c_void_p(this)
        return 0
    if riid == _IID_IMARSHAL and inst is not None and inst._ftm:
        out = ctypes.c_void_p()
        hr = _vcall(inst._ftm, 0, _HRESULT,
                    [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(_IID_IMARSHAL), ctypes.byref(out))
        ppv[0] = out
        return hr
    return _E_NOINTERFACE


def _h_addref(this):                             # noqa: ANN001, ARG001
    # 引用计数只服务激活这一小段：探活对象本身不会被别人长期持有，返回非零即可。
    return 2


def _h_release(this):                            # noqa: ANN001, ARG001
    return 1


def _h_activate_completed(this, op):             # noqa: ANN001, ARG001
    inst = _handler_of(this)
    if inst is not None:
        inst.fired += 1
        ctypes.WinDLL("kernel32").SetEvent(inst.event)
    return 0


# ctypes 的 Structure 字段类型必须是 **WinFunctionType 实例**（不是裸函数），
# 而且这些实例要活到进程结束 —— 挂在模块级即天然保活。
_H_QI = _QI_FN(_h_query_interface)
_H_AR = _AR_FN(_h_addref)
_H_RL = _AR_FN(_h_release)
_H_AC = _AC_FN(_h_activate_completed)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI_FN), ("AddRef", _AR_FN),
                ("Release", _AR_FN), ("ActivateCompleted", _AC_FN)]


class _HandlerObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVtbl))]


class _CompletionHandler:
    """`IActivateAudioInterfaceCompletionHandler` 的 ctypes 实现（agile）。

    为什么要 agile：`ActivateAudioInterfaceAsync` 的回调可能从**别的线程**打进来
    （实测回调线程 ≠ 调用线程）。不聚合 Free-Threaded Marshaler 就会被 COM 判
    `E_ILLEGAL_METHOD_CALL`。这里照 OBS 的口径：`CoCreateFreeThreadedMarshaler`
    + `QueryInterface` 应答 `IUnknown / IAgileObject / 自己 / IMarshal`。
    """

    def __init__(self) -> None:
        self._keep: list = []
        self._ftm = ctypes.c_void_p()
        self.event = ctypes.WinDLL("kernel32").CreateEventW(None, False, False, None)
        self.fired = 0
        tbl = _HandlerVtbl(_H_QI, _H_AR, _H_RL, _H_AC)
        obj = _HandlerObj(ctypes.pointer(tbl))
        self._keep.extend([tbl, obj])
        self._addr = ctypes.addressof(obj)
        _HANDLER_BY_ADDR[self._addr] = self
        hr = ctypes.WinDLL("ole32").CoCreateFreeThreadedMarshaler(
            ctypes.c_void_p(self._addr), ctypes.byref(self._ftm))
        if hr < 0:
            _HANDLER_BY_ADDR.pop(self._addr, None)
            raise RuntimeError(f"CoCreateFreeThreadedMarshaler 失败：{_hr_name(hr)}")

    def close(self) -> None:
        _HANDLER_BY_ADDR.pop(self._addr, None)
        self._keep.clear()
        self._ftm = ctypes.c_void_p()


class ProcessLoopbackSource(QueueAudioSource):
    """只抓一个进程（树）的 WASAPI 采集流。出参**int16le 立体声 48k**。

    生命周期：`open_process_loopback()` 里完成激活 + Initialize + Start（失败就抛，
    让调用方回落到端点模式）；之后后台线程只负责 `GetBuffer`；`close()` 里 Stop +
    释放接口。
    """

    label = "loopback"

    def __init__(self, loop, *, pid: int, rate: int = 48000,
                 client=None, capture=None, event=None, handler=None) -> None:
        super().__init__(loop, rate=rate, channels=2)
        self.pid = int(pid)
        self._client = client
        self._capture = capture
        self._event = event
        self._handler = handler
        self._bits = 32
        self._block_align = 2 * 32 // 8
        self._com_inited = False

    # ---- 对外小接口（engine 用来判断「进程没了 → 回等待」）
    def process_gone(self) -> bool:
        return not pid_alive(self.pid)

    # ---- 采集线程
    def _pump(self, stop: threading.Event) -> None:
        k32 = ctypes.WinDLL("kernel32")
        while not stop.is_set():
            k32.WaitForSingleObject(ctypes.c_void_p(self._event), 50)
            while not stop.is_set():
                n = ctypes.c_ulong(0)
                hr = _vcall(self._capture, 5, _HRESULT,
                            [ctypes.POINTER(ctypes.c_ulong)], ctypes.byref(n))
                if hr < 0:
                    log.warning("[loopback] 进程采集 GetNextPacketSize 失败：0x%08X %s",
                                hr & 0xFFFFFFFF, _hr_name(hr))
                    return
                if n.value == 0:
                    break
                pdata = ctypes.POINTER(ctypes.c_ubyte)()
                frames = ctypes.c_ulong(0)
                flags = ctypes.c_ulong(0)
                devpos = ctypes.c_ulonglong(0)
                qpc = ctypes.c_ulonglong(0)
                hr = _vcall(self._capture, 3, _HRESULT,
                            [ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                             ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                             ctypes.POINTER(ctypes.c_ulonglong),
                             ctypes.POINTER(ctypes.c_ulonglong)],
                            ctypes.byref(pdata), ctypes.byref(frames),
                            ctypes.byref(flags), ctypes.byref(devpos), ctypes.byref(qpc))
                if hr < 0:
                    log.warning("[loopback] 进程采集 GetBuffer 失败：0x%08X %s",
                                hr & 0xFFFFFFFF, _hr_name(hr))
                    return
                nf = int(frames.value)
                # 静音块实测**不带** SILENT 标志（就是全零数据），但 pdata 可能是空指针
                if (flags.value & _AUDCLNT_BUFFERFLAGS_SILENT) or not pdata or nf <= 0:
                    self._emit(b"\x00\x00" * max(0, nf) * 2)
                else:
                    raw = ctypes.string_at(pdata, nf * self._block_align)
                    self._emit(_float32_to_s16(raw))
                _vcall(self._capture, 4, _HRESULT, [ctypes.c_ulong], nf)

    def _teardown(self) -> None:
        try:
            if self._client is not None:
                _vcall(self._client, 11, _HRESULT, [])          # Stop
        except Exception:                                        # noqa: BLE001
            pass
        for ptr in (self._capture, self._client):
            _com_release(ptr)
        self._capture = self._client = None
        if self._event:
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._event))
            self._event = None
        if self._handler is not None:
            self._handler.close()
            self._handler = None
        if self._com_inited:
            try:
                ctypes.WinDLL("ole32").CoUninitialize()
            except Exception:                                    # noqa: BLE001
                pass
            self._com_inited = False


def _float32_to_s16(raw: bytes) -> bytes:
    """float32 交错立体声 → int16le（clamp ±1.0）。下游只认 int16，见模块头。"""
    import numpy as np
    a = np.frombuffer(raw, dtype=np.float32)
    if not a.size:
        return b""
    a = np.clip(a, -1.0, 1.0) * 32767.0
    return a.astype(np.int16).tobytes()


def _build_process_format(rate: int = 48000, channels: int = 2) -> _WAVEFORMATEX:
    """process loopback 的虚拟设备没有 mix format，只能按 OBS 的配方硬编码。"""
    f = _WAVEFORMATEX()
    ctypes.memset(ctypes.byref(f), 0, ctypes.sizeof(f))
    f.wFormatTag = _WAVE_FORMAT_IEEE_FLOAT
    f.nChannels = channels
    f.nSamplesPerSec = rate
    f.wBitsPerSample = 32
    f.nBlockAlign = channels * 4
    f.nAvgBytesPerSec = rate * f.nBlockAlign
    f.cbSize = 0
    return f


def open_process_loopback(pid: int, *, rate: int = 48000) -> AudioSource:
    """打开「只抓某个进程树」的采集流。失败抛 `RuntimeError`（调用方回落端点模式）。

    为什么要**同步**激活：这样才能把「这台机器不支持 / VRChat 不让我抓」变成一次
    明确的失败，而不是一条永远读不到数据的哑流（哑流会让上层一直显示"在采集"）。
    """
    import asyncio

    ok, why = process_loopback_available()
    if not ok:
        raise RuntimeError(f"进程级采集不可用：{why}")
    if not pid_alive(pid):
        raise RuntimeError(f"目标进程 {pid} 不存在")

    ole32 = ctypes.WinDLL("ole32")
    mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
    ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)     # MTA：接口可跨线程用

    handler = _CompletionHandler()
    params = _ACTIVATION_PARAMS()
    params.ActivationType = _PROCESS_LOOPBACK_ACTIVATION_TYPE
    params.ProcessLoopbackParams.TargetProcessId = int(pid)
    params.ProcessLoopbackParams.ProcessLoopbackMode = _PROCESS_LOOPBACK_MODE_INCLUDE_TREE
    pv = _PROPVARIANT()
    pv.vt = _VT_BLOB
    pv.blobSize = ctypes.sizeof(params)
    pv.pBlob = ctypes.cast(ctypes.byref(params), ctypes.c_void_p)

    mmdevapi.ActivateAudioInterfaceAsync.restype = _HRESULT
    mmdevapi.ActivateAudioInterfaceAsync.argtypes = [
        ctypes.c_wchar_p, ctypes.POINTER(_GUID), ctypes.POINTER(_PROPVARIANT),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    op = ctypes.c_void_p()
    hr = mmdevapi.ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(_IID_IAUDIOCLIENT),
        ctypes.byref(pv), ctypes.c_void_p(handler._addr), ctypes.byref(op))
    if hr < 0:
        raise RuntimeError(f"ActivateAudioInterfaceAsync 失败：{_hr_name(hr)}")

    ctypes.WinDLL("kernel32").WaitForSingleObject(ctypes.c_void_p(handler.event), 3000)
    result = _HRESULT(0)
    iface = ctypes.c_void_p()
    hr2 = _vcall(op, 3, _HRESULT, [ctypes.POINTER(_HRESULT),
                                   ctypes.POINTER(ctypes.c_void_p)],
                 ctypes.byref(result), ctypes.byref(iface))
    _com_release(op)
    if hr2 < 0 or result.value < 0 or not iface:
        raise RuntimeError(f"激活进程采集失败：hr={_hr_name(result.value)} op={_hr_name(hr2)}")

    client = iface
    fmt = _build_process_format(rate)
    flags = _AUDCLNT_STREAMFLAGS_LOOPBACK | _AUDCLNT_STREAMFLAGS_EVENTCALLBACK
    hr = _vcall(client, 3, _HRESULT,
                [ctypes.c_int, ctypes.c_ulong, ctypes.c_longlong, ctypes.c_longlong,
                 ctypes.POINTER(_WAVEFORMATEX), ctypes.c_void_p],
                _AUDCLNT_SHAREMODE_SHARED, flags, 0, 0, ctypes.byref(fmt), None)
    if hr < 0:
        _com_release(client)
        raise RuntimeError(f"Initialize 失败：{_hr_name(hr)}")

    cap = ctypes.c_void_p()
    hr = _vcall(client, 14, _HRESULT,
                [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                ctypes.byref(_IID_IAUDIOCAPTURECLIENT), ctypes.byref(cap))
    if hr < 0 or not cap:
        _com_release(client)
        raise RuntimeError(f"GetService(IAudioCaptureClient) 失败：{_hr_name(hr)}")

    k32 = ctypes.WinDLL("kernel32")
    event = k32.CreateEventW(None, False, False, None)
    hr = _vcall(client, 13, _HRESULT, [ctypes.c_void_p], ctypes.c_void_p(event))
    if hr < 0:
        _com_release(cap)
        _com_release(client)
        raise RuntimeError(f"SetEventHandle 失败：{_hr_name(hr)}")
    hr = _vcall(client, 10, _HRESULT, [])                       # Start
    if hr < 0:
        _com_release(cap)
        _com_release(client)
        raise RuntimeError(f"Start 失败：{_hr_name(hr)}")

    src = ProcessLoopbackSource(asyncio.get_running_loop(), pid=pid, rate=rate,
                                client=client, capture=cap, event=event, handler=handler)
    src._com_inited = True
    src.start()
    return src


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
