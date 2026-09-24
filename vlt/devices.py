"""音频设备枚举与名称解析。

三类设备，三种枚举来源：
  - 麦克风（输入）：sounddevice.query_devices() 中 max_input_channels > 0
  - VRChat 音频（loopback）：pyaudiowpatch 的 get_loopback_device_info_generator()
  - 译音输出（输出）：sounddevice.query_devices() 中 max_output_channels > 0

名称解析顺序：全名精确匹配 → 不区分大小写 → 去重后的子串匹配 → 解析不到返回 None。
配置里只存纯设备名字符串，绝不存设备索引（索引会随插拔变化）。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

# ⚠️ PortAudio 的初始化/销毁是**进程级且线程绑定**的资源（WASAPI 走 COM 单元）。
# 历史教训（都是实测出来的）：
#   1) 用后台线程做枚举 → sounddevice 在扫描线程 Pa_Initialize、却在主线程
#      Pa_Terminate（atexit 钩子）→ 退出时报
#      `Tcl_AsyncDelete: async handler deleted by the wrong thread`，更早的写法直接段错误
#   2) 多个线程并发 PyAudio()/terminate() → 进程退出时段错误
# 所以：**设备枚举一律在主线程同步做**（实测冷启动 348ms、预热后 23ms，完全可接受），
# 这个锁只是防止将来有人又把它挪回线程里。
_PA_LOCK = threading.Lock()


@dataclass
class DeviceInfo:
    index: int
    name: str
    sample_rate: int
    channels: int
    kind: str  # "input" | "output" | "loopback"


def enumerate_mic_devices(devices: list[dict] | None = None) -> list[DeviceInfo]:
    """枚举麦克风（输入）设备。devices 参数用于测试注入。"""
    try:
        import sounddevice as sd
        with _PA_LOCK:                      # PortAudio 串行（并发 init/destroy 会段错误）
            devs = devices if devices is not None else list(sd.query_devices())
        result = []
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) > 0:
                result.append(DeviceInfo(
                    index=i,
                    name=str(d.get("name", "")),
                    sample_rate=int(d.get("default_samplerate", 0)),
                    channels=int(d.get("max_input_channels", 0)),
                    kind="input",
                ))
        return result
    except Exception:
        return []


def enumerate_loopback_devices(devices: list[dict] | None = None) -> list[DeviceInfo]:
    """枚举 VRChat 音频（WASAPI loopback）设备。devices 参数用于测试注入。"""
    if devices is not None:
        result = []
        for d in devices:
            result.append(DeviceInfo(
                index=int(d.get("index", 0)),
                name=str(d.get("name", "")),
                sample_rate=int(d.get("defaultSampleRate", 0)),
                channels=int(d.get("maxInputChannels", 0)),
                kind="loopback",
            ))
        return result
    try:
        import pyaudiowpatch as pyaudio
        with _PA_LOCK:                      # PortAudio 串行（并发 init/destroy 会段错误）
            p = pyaudio.PyAudio()
            try:
                result = []
                for d in p.get_loopback_device_info_generator():
                    result.append(DeviceInfo(
                        index=int(d.get("index", 0)),
                        name=str(d.get("name", "")),
                        sample_rate=int(d.get("defaultSampleRate", 0)),
                        channels=int(d.get("maxInputChannels", 0)),
                        kind="loopback",
                    ))
                return result
            finally:
                p.terminate()
    except Exception:
        return []


def enumerate_audio_out_devices(devices: list[dict] | None = None) -> list[DeviceInfo]:
    """枚举译音输出（输出）设备。devices 参数用于测试注入。"""
    try:
        import sounddevice as sd
        with _PA_LOCK:                      # PortAudio 串行（并发 init/destroy 会段错误）
            devs = devices if devices is not None else list(sd.query_devices())
        result = []
        for i, d in enumerate(devs):
            if d.get("max_output_channels", 0) > 0:
                result.append(DeviceInfo(
                    index=i,
                    name=str(d.get("name", "")),
                    sample_rate=int(d.get("default_samplerate", 0)),
                    channels=int(d.get("max_output_channels", 0)),
                    kind="output",
                ))
        return result
    except Exception:
        return []


def resolve_device_name(
    name: str,
    kind: str,
    devices: list[dict] | None = None,
) -> int | None:
    """按设备名解析出设备索引。

    kind: "input" | "output" | "loopback"
    返回 None 表示"用默认/回退链"。

    解析顺序：
      1. 全名精确匹配
      2. 不区分大小写匹配
      3. 去重后的子串匹配（正则特殊字符用字面量匹配，不会崩也不会误匹配）
      4. 解析不到 → 返回 None
    """
    if not name:
        return None

    if kind == "loopback":
        infos = enumerate_loopback_devices(devices)
    elif kind == "input":
        infos = enumerate_mic_devices(devices)
    elif kind == "output":
        infos = enumerate_audio_out_devices(devices)
    else:
        return None

    # 1. 全名精确匹配
    for info in infos:
        if info.name == name:
            return info.index

    # 2. 不区分大小写
    name_lower = name.lower()
    for info in infos:
        if info.name.lower() == name_lower:
            return info.index

    # 3. 子串匹配（用字面量，不用正则——设备名里的括号/加号/方括号不会崩）
    seen_names: set[str] = set()
    for info in infos:
        if info.name in seen_names:
            continue
        seen_names.add(info.name)
        if name_lower in info.name.lower():
            return info.index

    return None


def format_device_display(info: DeviceInfo) -> str:
    """格式化设备显示字符串，例如 'Steam Streaming Speakers (48000Hz)'。"""
    if info.sample_rate > 0:
        return f"{info.name} ({info.sample_rate}Hz)"
    return info.name
