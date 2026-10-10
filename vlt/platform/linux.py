"""Linux 平台实现（PipeWire 原生工具 + 子进程管道）。

⚠️ 本模块是 **Linux 独占**：打包 Windows 版时通过 `--exclude-module vlt.platform.linux`
剔除，所以 Windows 产物里不会出现 pipewire / pw-dump / pw-record 等任何字样。
（这就是「内嵌为常量 + 模块排除」方案的结构性保证，见 vlt/platform/base.py。）

## 为什么全程用 `pw-*` 而不是 `pactl` / `parec` / `pacat`

PipeWire 提供了 PulseAudio 兼容层（`pipewire-pulse`），`pactl`/`parec`/`pacat` 也能用。
**但实测在本机上 `pactl` 直接 `Connection refused`**（Pulse 套接字不可达），
而 `pw-dump` 正常返回 —— 兼容层不是必装件，原生工具才是。
设备枚举、采集、播放因此全部走 PipeWire 原生的 `pw-dump` / `pw-record` / `pw-cat`。

## 为什么设备表不直接用 sounddevice

`sounddevice.query_devices()` 在 Linux 上走 JACK 后端（pipewire-jack），它会把
**每个正在播放的应用流**也暴露成「输入设备」——实测列表里出现
`Google Chrome`、`vesktop`、`speech-dispatcher-dummy` 这些。直接拿来当「麦克风下拉框」
会是一堆噪音。

所以：**设备表从 `pw-dump` 构建**（只取 `Audio/Source` 与 `Audio/Sink`），
**麦克风采集也走 `pw-record`**（`--target=<源 node.name>`）——与本模块其它采集同一条
原生路径。

> ⚠️ 2026-10 修正：麦克风曾走 sounddevice 的按名打开，但 PortAudio 会把那个名字解析到
> **JACK** host API 的条目（名字只存在于 JACK 侧），而 `libjack` 由**可选包**
> `pipewire-jack` 提供 —— 没装它的环境里麦克风整条腿挂掉，而且「自动检测」同样中招。
> 改走 `pw-record` 后只依赖 PipeWire 本体（已声明/已自检的 `pw-record`）。
> 顺带：`--channels=1` 让 PipeWire 服务端把源的全部声道降混进一路（双声道/5.1/7.1 都覆盖）。

## 与 Windows 的形状对齐

`query_loopback_devices()` 返回与 pyaudiowpatch 同形状的 dict
（`index` / `name` / `defaultSampleRate` / `maxInputChannels`），
所以 `vlt/devices.py` 的纯逻辑（名称解析、回退链）一行都不用改。

注意 `index` 用的是 PipeWire 的**节点 id**（是个真 int），但它**只用于一次枚举之内**
的定位 —— 配置里永远只存 `name` 字符串，因为节点 id 每次重启都会变。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import child_env
from .audio import QueueAudioSource
from .base import AudioSource, LoopbackTarget

log = logging.getLogger(__name__)

# `pw-dump` 一次约 0.4MB / 100ms，而 GUI 一次设备扫描会连着问好几遍
# （麦克风一遍、输出一遍、loopback 一遍）→ 加个极短的 TTL 缓存，避免白跑。
_DUMP_TTL_S = 2.0
_dump_cache: tuple[float, list[dict]] | None = None


class PipeWireUnavailable(RuntimeError):
    """连不上 PipeWire（没装 / 没跑 / 沙盒里看不到套接字）。"""


# ---------------------------------------------------------------- pw-dump

def _pw_dump(force: bool = False) -> list[dict]:
    """调 `pw-dump` 拿全量对象快照（带 2 秒 TTL 缓存）。

    这是本模块唯一的数据来源：设备表、默认设备、采样率全部从这一份快照里读，
    避免多次调用之间状态漂移（用户正在插拔设备时尤其明显）。
    """
    global _dump_cache
    now = time.monotonic()
    if not force and _dump_cache is not None and now - _dump_cache[0] < _DUMP_TTL_S:
        return _dump_cache[1]

    if shutil.which("pw-dump") is None:
        raise PipeWireUnavailable("找不到 pw-dump（装 pipewire 包）")
    try:
        res = subprocess.run(["pw-dump"], capture_output=True, timeout=10,
                             env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipeWireUnavailable(f"执行 pw-dump 失败：{exc}") from exc
    if res.returncode != 0:
        raise PipeWireUnavailable(
            f"pw-dump 退出码 {res.returncode}：{res.stderr.decode('utf-8', 'replace').strip()}")
    try:
        data = json.loads(res.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise PipeWireUnavailable(f"pw-dump 输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, list):
        raise PipeWireUnavailable("pw-dump 输出不是数组")

    _dump_cache = (now, data)
    return data


def clear_cache() -> None:
    """丢掉 pw-dump 缓存 —— 设备扫描前想拿最新状态时调。"""
    global _dump_cache
    _dump_cache = None


def _nodes(dump: list[dict]) -> list[tuple[int, str, dict]]:
    """(节点 id, media.class, props) 三元组，只取音频节点。"""
    out = []
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        media_class = str(props.get("media.class") or "")
        if not media_class.startswith("Audio/"):
            continue
        out.append((int(obj.get("id") or 0), media_class, props))
    return out


def _default_rate(dump: list[dict]) -> int:
    """PipeWire 的 `default.clock.rate`（取不到就 48000，PipeWire 的常规默认）。"""
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Core":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        try:
            return int(props.get("default.clock.rate") or 48000)
        except (TypeError, ValueError):
            return 48000
    return 48000


def _display_name(props: dict) -> str:
    """人类可读的设备名：description → nick → node.name 依次回落。

    `node.name`（如 `alsa_output.usb-Mi_REDMI____...analog-stereo`）是稳定标识，
    但塞进下拉框太丑；description 才是给人看的。
    """
    for key in ("node.description", "node.nick", "node.name"):
        val = str(props.get(key) or "").strip()
        if val:
            return val
    return ""


def _channels(props: dict) -> int:
    try:
        return max(1, int(props.get("audio.channels") or 2))
    except (TypeError, ValueError):
        return 2


# ---------------------------------------------------------------- 设备枚举

def query_devices() -> list[dict]:
    """设备表，形状与 sounddevice 一致（`max_input_channels` / `max_output_channels`）。

    * `Audio/Source` → `max_input_channels > 0`（麦克风）
    * `Audio/Sink`   → `max_output_channels > 0`（播放设备 / 译音目标）

    顺序按 `node.name` 排序，保证同一台机器上多次枚举结果稳定
    （`devices.py` 的 index 是列表位置，不能每次变）。
    """
    dump = _pw_dump()
    rate = _default_rate(dump)
    rows: list[dict] = []
    for node_id, media_class, props in _nodes(dump):
        name = _display_name(props)
        if not name:
            continue
        node_name = str(props.get("node.name") or "")
        ch = _channels(props)
        if media_class == "Audio/Source":
            rows.append({"name": name, "node_name": node_name,
                         "max_input_channels": ch, "max_output_channels": 0,
                         "default_samplerate": float(rate), "pw_id": node_id})
        elif media_class == "Audio/Sink":
            rows.append({"name": name, "node_name": node_name,
                         "max_input_channels": 0, "max_output_channels": ch,
                         "default_samplerate": float(rate), "pw_id": node_id})
    rows.sort(key=lambda d: d.get("node_name") or d.get("name") or "")
    return rows


def query_loopback_devices() -> list[dict]:
    """可抓取的「系统输出」，形状与 pyaudiowpatch 的 loopback 设备一致。

    Windows 上每个输出设备都有一个对应的 loopback「录音副本」；
    Linux 上对应概念就是**输出节点本身** —— 对 sink 录音拿到的正是它的 monitor
    （`pw-record --target <node.name>`）。

    `name` 用 description（给人看、给用户存进 config），
    `node_name` 是 `--target` 真正要用的稳定标识。
    """
    dump = _pw_dump()
    rate = _default_rate(dump)
    rows = []
    for node_id, media_class, props in _nodes(dump):
        if media_class != "Audio/Sink":
            continue
        name = _display_name(props)
        if not name:
            continue
        rows.append({
            "index": node_id,                                   # 仅本次枚举内有效
            "name": name,
            "node_name": str(props.get("node.name") or ""),
            "defaultSampleRate": int(rate),
            "maxInputChannels": _channels(props),
        })
    rows.sort(key=lambda d: d.get("node_name") or d.get("name") or "")
    return rows


# VRChat（Proton/Wine）的**播放输出**在 PipeWire 里是 `Stream/Output/Audio` 节点，
# 而**不是** `Audio/Sink` —— 所以 `query_loopback_devices()`（只列 sink）永远看不到它，
# 「VRChat 音频」下拉里自然也就选不到。实测节点名取自可执行文件：
# `application.name` / `node.name` = `"VRChat.exe"`（同一进程会开 2 个输出流，
# 都连到默认 sink；定向采集任一个都是 VRChat 的声音）。
#
# 为什么抓流而不抓默认 sink：默认 sink 上是**所有**应用混在一起的声音
# （实测同一条链路上还有 Google Chrome / vesktop / 系统提示音），
# 抓它会把音乐、浏览器声音一起当成「游戏内语音」喂给翻译模型。
_VRCHAT_HINTS = ("vrchat",)

# 最多抓几路 VRChat 输出流。实测正常是 2 路；给个上限纯属防御（万一 Wine 抽风开一堆）。
VRCHAT_MAX_STREAMS = 8


def find_vrchat_output_streams() -> list[dict]:
    """找 VRChat 的**全部音频输出流**节点（Linux 专用，`Stream/Output/Audio`）。

    ⚠️ VRChat（Wine/Proton 经 pipewire-pulse）会开**多个**播放流：实测同一次运行里
    有 `media.name` = `audio stream #1` 与 `audio stream #5` 两个节点，各自可能承载
    一部分声音（也可能有一个处于 `pulse.corked` 暂停态）。所以这里返回**全部**，
    由调用方每一路开一条 `pw-record` 再混音 —— 只抓第一个会丢声音。

    返回与 `query_loopback_devices()` 同形状的 dict，外加：
      * `media_class`：固定 `Stream/Output/Audio`；
      * `serial`：`object.serial` —— **这才是 `pw-record --target=` 要用的标识**。
        全部节点的 `node.name` 都是 `VRChat.exe`，按名字无法区分多个流；
        而 `--target` 只接受「**序列号或名称**」，传数字 node id 会被当成名字匹配落空
        （实测：`--target=147` 没连到 147，而是回落到默认目标）。用 serial 精确。

    刻意用 `_pw_dump(force=True)`：调用方是「等 VRChat 出现」的轮询，
    2 秒 TTL 缓存会把「刚刚启动」的流节点挡在门外。

    找不到 VRChat（没跑 / 还没出声）返回空列表。
    """
    try:
        dump = _pw_dump(force=True)
    except PipeWireUnavailable:
        return []
    rate = _default_rate(dump)
    found: list[tuple[int, dict]] = []
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        # ⚠️ 这里**不能**走 `_nodes()`：它按 `media.class` 前缀 `Audio/` 过滤，
        # 而应用流是 `Stream/Output/Audio`，前缀是 `Stream/`。
        if str(props.get("media.class") or "") != "Stream/Output/Audio":
            continue
        hay = (str(props.get("application.name") or "") + " "
               + str(props.get("node.name") or "")).lower()
        if not any(hint in hay for hint in _VRCHAT_HINTS):
            continue
        node_name = str(props.get("node.name") or "")
        serial = props.get("object.serial")
        if not node_name or serial is None:
            continue
        stream_name = str(props.get("media.name") or "")
        label = str(props.get("application.name") or node_name)
        if stream_name:
            label = f"{label} ({stream_name})"       # 多个流时日志里才好区分
        found.append((int(obj.get("id") or 0), {
            "index": int(obj.get("id") or 0),
            "name": label,
            "node_name": node_name,
            "serial": str(serial),
            "defaultSampleRate": int(rate),
            "maxInputChannels": _channels(props),
            "media_class": "Stream/Output/Audio",
        }))
    found.sort(key=lambda t: t[0])                    # 按 node id 稳定排序（同一次快照内确定）
    if len(found) > VRCHAT_MAX_STREAMS:
        log.warning("[vrchat] 匹配到 %d 路 VRChat 输出流，只取前 %d 路",
                    len(found), VRCHAT_MAX_STREAMS)
        found = found[:VRCHAT_MAX_STREAMS]
    return [d for _id, d in found]


def default_output_index() -> int:
    """WirePlumber 的默认输出（sink）节点 id；取不到返回 -1。

    从 Metadata 对象的 `default.audio.sink` 里读 —— 这是 WirePlumber 存放
    「当前默认设备」的地方（`pactl get-default-sink` 读的也是同一份）。
    """
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return -1
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        for entry in (obj.get("metadata") or []):
            if entry.get("key") != "default.audio.sink":
                continue
            val = entry.get("value")
            if isinstance(val, dict):
                target = str(val.get("name") or "")
                for node_id, media_class, props in _nodes(dump):
                    if media_class == "Audio/Sink" and str(props.get("node.name")) == target:
                        return node_id
    return -1


def default_source_node() -> str:
    """系统默认**输入源**的 `node.name`（PipeWire 稳定标识）；取不到返回 ""。

    读 WirePlumber 元数据：优先 `default.audio.source`（当前生效），缺了看
    `default.configured.audio.source`（用户配置的）。「自动检测」据此解析成具体设备，
    再走与手选完全相同的通路（`pw-record --target=<node.name>`）。
    """
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return ""
    preferred = configured = ""
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        for entry in (obj.get("metadata") or []):
            key = entry.get("key")
            val = entry.get("value")
            if not isinstance(val, dict):
                continue
            name = str(val.get("name") or "")
            if not name:
                continue
            if key == "default.audio.source":
                preferred = name
            elif key == "default.configured.audio.source":
                configured = name
    return preferred or configured


def default_source_name() -> str:
    """系统默认**输入源**在人可读名字（= 我们设备表里的描述）；取不到返回 ""。

    由 `default_source_node()` 的 `node.name` 映射出 description（给人看、进日志）。
    """
    node = default_source_node()
    if not node:
        return ""
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return ""
    for _node_id, media_class, props in _nodes(dump):
        if media_class == "Audio/Source" and str(props.get("node.name")) == node:
            return _display_name(props)
    return ""


def device_info_by_index(index: int) -> dict:
    """按节点 id 取 props（拿名字用）；取不到返回 {}。"""
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return {}
    for node_id, _media_class, props in _nodes(dump):
        if node_id == int(index):
            return dict(props)
    return {}


# ---------------------------------------------------------------- 字体

# fc-match 找不到时的兜底路径（Arch 上 noto-cjk / adobe-source-han-sans 都常见）。
_CJK_FONT_FALLBACKS = (
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Light.ttc",
    "/usr/share/fonts/adobe-source-han-sans/SourceHanSansCN-Regular.otf",
    "/usr/share/fonts/TTF/NotoSansCJK-Regular.ttc",
)


def find_cjk_font() -> str | None:
    """用 fontconfig 找一个中日韩字体；找不到返回 None。

    先问 `fc-match`（尊重用户自己的字体偏好），再退回已知路径。
    Windows 侧对应的是 `C:/Windows/Fonts/msyh.ttc`（见 win.py）。
    """
    if shutil.which("fc-match"):
        try:
            res = subprocess.run(
                ["fc-match", "-f", "%{file}", "Noto Sans CJK SC:lang=zh-cn"],
                capture_output=True, timeout=5,
                env=child_env(),          # 别让宿主 fc-match 加载包内旧 fontconfig
            )
            if res.returncode == 0:
                cand = res.stdout.decode("utf-8", "replace").strip()
                if cand and Path(cand).exists():
                    return cand
        except (OSError, subprocess.TimeoutExpired):
            pass
    for cand in _CJK_FONT_FALLBACKS:
        if Path(cand).exists():
            return cand
    return None


# 泰文字体兜底路径（fc-match 找不到时用）。
#
# ⚠️ **不能复用 CJK 字体**：Noto Sans CJK 不含泰文字形，渲染泰文会出豆腐块
# （与 Windows 侧 msyh.ttc 同因）。所以泰语必须走这条独立探测，渲染侧按书写系统
# 切 run、各用各的字体画。
#
# 常见 Linux 泰文字体包：
#   * `noto-fonts-thai` / `fonts-noto-thai` → NotoSansThai-Regular.ttf
#   * `ttf-thai-tlwg` → Loma.ttf / Garuda.ttf / Norasi.ttf 等
#   * `fonts-thai-tlwg` → 同上（Debian/Ubuntu 包名）
_THAI_FONT_FALLBACKS = (
    "/usr/share/fonts/noto-thai/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/truetype/tlwg/Loma.ttf",
    "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
    "/usr/share/fonts/truetype/tlwg/Norasi.ttf",
    "/usr/share/fonts/TTF/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/truetype/thai-tlwg/Loma.ttf",
)


def find_thai_font() -> str | None:
    """用 fontconfig 找一个**确实含泰文字形**的字体；找不到返回 None。

    ⚠️ 判据必须是 `fc-list :lang=th`（只列**覆盖泰文**的字体），**不能只信 `fc-match`**：
    `fc-match` 永远会返回一个字体（哪怕它不含泰文字形，例如 DejaVu Sans），于是
    「拿到路径」≠「能画泰文」→ 既不告警、渲染出来还是豆腐块，属于**静默降级**。
    所以：fc-list 拿不到 → 再走下面的已知路径兜底 → 都没有就返回 None，
    由调用方（`overlay.resolve_thai_font_path`）打一次性告警后回落 CJK 字体。

    Windows 侧对应的是 `LeelawUI.ttf` / `tahoma.ttf`（见 win.py）。
    """
    if shutil.which("fc-list"):
        try:
            res = subprocess.run(
                ["fc-list", ":lang=th", "-f", "%{file}\n"],
                capture_output=True, timeout=5,
                env=child_env(),          # 别让宿主 fc-list 加载包内旧 fontconfig
            )
            if res.returncode == 0:
                for line in res.stdout.decode("utf-8", "replace").splitlines():
                    cand = line.strip()
                    # 字体集合（.ttc）在部分 fontconfig 版本里会带 `:index=0` / `:face=0` 后缀
                    cand = re.sub(r":(?:index|face)=\d+$", "", cand)
                    if cand and Path(cand).exists():
                        return cand
        except (OSError, subprocess.TimeoutExpired):
            pass
    for cand in _THAI_FONT_FALLBACKS:
        if Path(cand).exists():
            return cand
    return None


# ---------------------------------------------------------------- 界面语言

# 语言代码前缀 → 本项目支持的界面语言。与 win.py 的口径必须一致：
# **不支持的语言一律回落 "en"**（外国用户按英文接待远比按中文合理）。
_LANG_MAP = {"zh": "zh", "en": "en", "ja": "ja", "ko": "ko", "ru": "ru"}


def detect_ui_language() -> str:
    """按 POSIX 惯例读界面语言：`LC_ALL` → `LC_MESSAGES` → `LANG`。

    形如 `zh_CN.UTF-8` / `ja_JP.utf8` / `en_US` / `C` / `POSIX`。
    取不到、是 `C`/`POSIX`、或不在支持列表里 → `"en"`。
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = (os.environ.get(var) or "").strip()
        if not raw:
            continue
        code = raw.split(".", 1)[0].split("@", 1)[0].strip().lower()
        primary = code.replace("_", "-").split("-", 1)[0]
        if primary in _LANG_MAP:
            return _LANG_MAP[primary]
        return "en"                       # C / POSIX / 德语 / 法语… → 英文
    return "en"


# ---------------------------------------------------------------- 虚拟声卡（运行时声明）

# 默认**输出**的候选类，抄自 WirePlumber 源码
# `/usr/share/wireplumber/scripts/default-nodes/rescan.lua:63-65`：
#     pushSelectDefaultNodeEvent (..., "audio.sink", "in", { "Audio/Sink", "Audio/Duplex" })
# 这个列表是**精确匹配**（约束动词 "c" = 「属于其中之一」），不是前缀匹配 ——
# 所以只要我们的类不在这两项里，就永远不会被选成默认输出。
DEFAULT_OUTPUT_CLASSES = ("Audio/Sink", "Audio/Duplex")

# ⚠️⚠️ capture 侧必须用 `Audio/Sink/Internal`，**绝不能用 `Audio/Sink`**。
#
# 这是整个虚拟声卡设计里最要命的一条，理由是实测出来的：
#
#   一开始 capture 侧写的是 `Audio/Sink`。结果**一创建虚拟声卡，用户的系统声音就没了** ——
#   WirePlumber 把我们的节点选成了默认输出，用户正在放的音乐/通话/游戏声音全被改道进了
#   虚拟设备。实测时间线：创建后约 1.8 秒，`default.audio.sink` 变成 `vlt_mic_sink`。
#
#   试过并且**确认无效**的「防护」（都读过 WP 源码或实测排除，别再走回头路）：
#     ✗ priority.session = 0   —— 实测挡不住（真实声卡是 696/1009/1109，我们 0，仍然被选中；
#                                 根因至今没查清，所以**不能依赖它**）
#     ✗ node.virtual = true    —— 默认选举根本不看这个属性（它只被 media-role 链接用）
#     ✗ node.hidden            —— 不存在这个属性
#     ✗ node.disabled          —— 只被 alsa / v4l2 / libcamera 这些硬件 monitor 读
#     ✗ node.link-group        —— 只有 `module-filter-chain` 注册过的 smart filter 才被豁免
#                                 （is_filter_smart 要求 link-group 已登记），pw-loopback 拿不到
#
#   `Audio/Sink/Internal` 是 WirePlumber 自己在用的合法类（bluez 设备集的内部成员节点，
#   见 monitors/bluez/create-set-node.lua），**不在默认输出的候选列表里** ——
#   于是「抢默认输出」这件事从机制上不可能发生，而不是靠事后还原去补救。
#
#   用户明确的口径：「虚拟麦就是正常的麦克风，可以参与默认选举」，
#   所以我们**不动 source 侧**（它保持 Audio/Source）——被选成默认麦克风是可接受的，
#   而且真实麦克风的 priority.session（2009/2100）本来就压过我们的 0。
VIRTUAL_SINK_PROPS: dict[str, object] = {
    "media.class": "Audio/Sink/Internal",
    "node.virtual": "true",
    "node.autoconnect": "false",
}
VIRTUAL_SOURCE_PROPS: dict[str, object] = {
    "media.class": "Audio/Source",
    "node.virtual": "true",
    "node.autoconnect": "false",
}


def loopback_argv(sink_name: str, source_name: str, description: str,
                  *, channels: int = 2) -> list[str]:
    """构造 `pw-loopback` 的 argv：一趟产出「可写入端 + 虚拟麦」。

    ⚠️ 方向容易设反（实测核实过）：
      * `-i/--capture-props` → 带「音频输入」的那端 → 我们往里写的那一端
      * `-o/--playback-props` → 带「音频输出」的那端 → **Audio/Source**（VRChat 选作麦克风）
      设反的后果：没有可写入的端，或没有 VRChat 能选的麦克风。

    写入侧用 `pw-cat --playback --target=<sink_name>` 显式指定目标 ——
    WirePlumber 的 `linking/find-defined-target` 按 `node.name` 匹配，且它跑在
    `find-default-target` **之前**，所以我们的音频只会进自己的节点，
    绝不会落到默认输出（= 用户的扬声器）上去。
    """
    def _props(base: dict, name: str) -> str:
        items = dict(base)
        items["node.name"] = name
        items["node.description"] = description
        inner = " ".join(f'{k}="{v}"' if isinstance(v, str) and " " in str(v) else f"{k}={v}"
                         for k, v in items.items())
        return "{ " + inner + " }"

    return [
        "pw-loopback",
        "-c", str(channels),
        "-m", "[ FL FR ]" if channels == 2 else "[ MONO ]",
        "--capture-props", _props(VIRTUAL_SINK_PROPS, sink_name),
        "--playback-props", _props(VIRTUAL_SOURCE_PROPS, source_name),
    ]


def node_props_by_name(name: str) -> dict:
    """按 `node.name` 取一个节点的 props（取不到返回 {}）。"""
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return {}
    for _node_id, _media_class, props in _nodes(dump):
        if str(props.get("node.name") or "") == name:
            return dict(props)
    return {}


def assert_not_default_output_candidate(node_name: str) -> tuple[bool, str]:
    """**只读**断言：这个节点的类不能落在默认输出的候选类里。

    这是「不打扰用户音频」的最后一道保险 —— 万一将来有人把 VIRTUAL_SINK_PROPS
    改回 `Audio/Sink`（或 WirePlumber 改了候选规则），这里会拦住，让我们禁用译音这条腿，
    而不是悄悄把用户的系统声音改道。

    ⚠️ 刻意**不做任何全局状态修改**（不 set-default、不写元数据）：那种「先抢再还原」
    的做法用户已经明确否决 —— 还原期间用户的音频是真的断的。
    """
    props = node_props_by_name(node_name)
    media_class = str(props.get("media.class") or "")
    if not media_class:
        return False, f"找不到节点 {node_name!r} 的 media.class"
    if media_class in DEFAULT_OUTPUT_CLASSES:
        return False, (f"节点 {node_name!r} 的类 {media_class!r} 属于默认输出候选"
                       f"{DEFAULT_OUTPUT_CLASSES} —— 会抢走用户的默认输出，已拒绝启用")
    return True, f"{node_name!r} 的类 {media_class!r} 不在默认输出候选里（安全）"


def _test_process_guard(action: str = "真的声明虚拟声卡") -> str | None:
    """防呆：**测试进程里拒绝做「会碰用户会话」的真实操作**（默认：声明虚拟声卡）。

    写这个不是洁癖 —— 实测踩过四次：单元测试只桩住了 Windows 侧
    （`E.pick_output_device` / `E.VirtualMic`），于是 Linux 分支绕过打桩、
    真的拉起了 `pw-loopback`，动到了用户的音频图。
    正确修法是测试两侧都桩（见 `tests/test_virtualmic.py` 的 `_stub_audio_out`），
    这里只是**最后一道保险**：万一又漏了，宁可这条腿不启用，也不能动用户的音频。

    正常使用（`python -m vlt.gui` / `-m vlt.app`）永远不会命中这个判断。

    `action` 给第二类用途：桌面字幕的原生窗（测试进程建窗会连到**用户正在用的
    合成器**上 —— 实测 GUI 用例在 Wayland 机器上真的弹出了一块 layer 面）。
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return None
    p = Path(path)
    if p.name.startswith("test_") or "tests" in p.parts:
        return f"检测到测试进程（{p.name}）→ 拒绝{action}"
    return None


class VirtualMicCable:
    """**运行时**声明一对 PipeWire 节点：可写入端 + 虚拟麦。

    生命周期 = 本对象的生命周期：`start()` 拉起 `pw-loopback`，`stop()` 发 SIGTERM，
    节点由 PipeWire 自动回收。全程不写任何配置文件、不重启任何服务、不改任何全局状态。
    """

    def __init__(self, sink_name: str = "vlt_mic_sink",
                 source_name: str = "vlt_mic_source",
                 description: str = "VLT Mic", channels: int = 2) -> None:
        self.sink_name = sink_name
        self.source_name = source_name
        self._description = description
        self._channels = channels
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, ready_timeout: float = 8.0) -> bool:
        """拉起并确认就绪。任何一步不对都返回 False（调用方据此禁用译音这条腿）。"""
        if self.running:
            return True
        blocked = _test_process_guard()
        if blocked:
            log.warning("[vmic] %s（这条腿不启用；测试必须两侧都打桩）", blocked)
            return False
        try:
            self._proc = subprocess.Popen(
                loopback_argv(self.sink_name, self.source_name, self._description,
                              channels=self._channels),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=child_env())
        except Exception as exc:  # noqa: BLE001
            log.warning("[vmic] 拉起 pw-loopback 失败：%s: %s", type(exc).__name__, exc)
            return False

        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if not self.running:
                log.warning("[vmic] pw-loopback 已退出（stderr：%s）", self.stderr_tail()[:400])
                self._proc = None
                return False
            clear_cache()                       # 别被 TTL 缓存骗到（刚建的节点还看不到）
            if node_props_by_name(self.source_name) and node_props_by_name(self.sink_name):
                break
            time.sleep(0.1)
        else:
            log.warning("[vmic] 等待虚拟声卡节点超时（%.0fs）", ready_timeout)
            self.stop()
            return False

        # ★ 关键防线：确认可写入端的类**不参与默认输出选举**。
        # 这一步是只读的 —— 失败就整条腿不启用，绝不「先抢再还原」。
        ok, detail = assert_not_default_output_candidate(self.sink_name)
        if not ok:
            log.error("[vmic] ❌ 拒绝启用译音输出：%s", detail)
            self.stop()
            return False
        log.info("[vmic] 虚拟声卡就绪：%s（可写入）/ %s（虚拟麦）", self.sink_name, self.source_name)
        return True

    def stderr_tail(self) -> str:
        proc = self._proc
        if proc is None or proc.stderr is None or proc.poll() is None:
            return ""
        try:
            return proc.stderr.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""

    def stop(self) -> None:
        """幂等。SIGTERM → 节点由 PipeWire 回收。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("[vmic] pw-loopback 未响应 SIGTERM，改用 SIGKILL")
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:  # noqa: BLE001
            pass


class LinuxAudioOut:
    """Linux 译音输出：虚拟声卡声明 + 写入端，绑成一个对象一起管生命周期。

    对外接口与 Windows 的 `VirtualMic` 一致（`open` / `push` / `end_sentence` / `close`），
    所以引擎侧不需要知道自己在哪个平台。
    """

    def __init__(self, cable: VirtualMicCable, sink) -> None:  # noqa: ANN001
        self._cable = cable
        self._sink = sink

    @property
    def device_name(self) -> str:
        return self._cable.sink_name

    @property
    def _sample_rate(self) -> int:                 # 兼容 VirtualMic 的属性名
        return self._sink._sample_rate

    def open(self) -> bool:
        return self._sink.open()

    def push(self, pcm_48k_stereo: bytes) -> None:
        self._sink.push(pcm_48k_stereo)

    def end_sentence(self) -> None:
        self._sink.end_sentence()

    def close(self) -> None:
        self._sink.close()
        self._cable.stop()


def open_audio_out(audio_cfg: dict, on_status) -> LinuxAudioOut | None:  # noqa: ANN001
    """声明虚拟声卡并接上写入端。任何一步失败都返回 None（其余功能不受影响）。

    生命周期：`close()` 会先关写入端、再销毁虚拟声卡 —— 顺序不能反，
    否则正在播的那句会被管道断开截断。
    """
    from ..output.virtualmic import PwCatVirtualMic

    cable = VirtualMicCable()
    if not cable.start():
        on_status("error", "虚拟声卡声明失败 → 译音输出已禁用（其余功能不受影响）")
        return None

    sink = PwCatVirtualMic(
        cable.sink_name,
        sample_rate=int(audio_cfg.get("sample_rate", 48000)),
        buffer_ms=int(audio_cfg.get("buffer_ms", 300)),
        max_buffer_ms=int(audio_cfg.get("max_buffer_ms", 2000)),
        on_status=on_status,
    )
    # 注意：这里**不打开** sink —— 交给调用方统一调 open()，
    # 与 Windows 侧「造出来 → 调 open()」的形状保持一致。
    return LinuxAudioOut(cable, sink)


def create_mic_proxy(audio_cfg: dict, mic_name: str | None = None,
                     on_status=None):  # noqa: ANN001
    """Linux 麦克风代理：`pw-loopback` 声明虚拟麦 + `pw-cat` 写管道驱动输出。

    与 Windows 版同构（见 `vlt/output/micproxy_linux.py` 的模块头）；本函数只是
    把「平台独占的模块」挡在共享代码之外 —— 与 `open_audio_out` / `create_wrist_overlay`
    同一条纪律。
    """
    from ..output.micproxy_linux import LinuxMicProxy

    return LinuxMicProxy(audio_cfg=audio_cfg, mic_name=mic_name,
                         on_status=on_status or (lambda *_a: None))



# ---------------------------------------------------------------- 平台能力探测

def pw_tools_missing() -> list[str]:
    """缺哪些 PipeWire 原生工具（给 setup.sh / 启动自检报错用）。"""
    return [t for t in ("pw-dump", "pw-record", "pw-cat") if shutil.which(t) is None]


# ---------------------------------------------------------------- 采集

# ⚠️ **`--raw` 不能省**（实测踩到的静默坑）：
# 不给 `--raw` 时 pw-record 会用 libsndfile 写一个**容器格式**——实测 stdout 头 4 字节
# 是 ASCII "dns."（容器魔数），后面才是数据。把它当裸 PCM 解析出来的"音频"是垃圾，
# 而且不会报错（stderr 干净），只是峰值恒定、看起来"有信号"，极难发现。
# 播放侧同理：`pw-cat --playback` 不给 `--raw` 会报
# `sndfile: failed to open audio file "-": Format not recognised` 而**根本没播出去**。
_RAW = "--raw"


class PwRecordSource(QueueAudioSource):
    """系统声采集：`pw-record` 子进程读 stdout（PipeWire 原生）。

    两个用途共用本类（`label` 区分子进程/日志归属）：
      * **loopback**（`label="loopback"`）：`--target=<sink>` 抓该 sink 的 **monitor**
        （输出内容的副本）—— Windows 上 WASAPI loopback 的等价物；
      * **麦克风**（`LinuxMicSource`，`label="mic"`）：`--target=<源 node.name>` 抓该输入源。
        ⚠️ 之前麦克风走 sounddevice/PortAudio，设备名只解析得到 **JACK** host API 里的条目，
        于是隐式依赖 `pipewire-jack`（可选包，很多环境没有）；改走 `pw-record` 后只依赖
        PipeWire 本体（`pw-record`，已声明/已自检），且多声道由 PipeWire 服务端降混。

    为什么不用 `parec`：它走的是 PulseAudio 兼容层，实测在本机上直接
    `Connection refused`（兼容层不是必装件）。`pw-record` 连的是 PipeWire 本体。
    """

    label = "loopback"

    def __init__(self, loop, *, target: str = "", rate: int = 16000,
                 channels: int = 1, latency_ms: int = 50) -> None:
        super().__init__(loop, rate=rate, channels=channels)
        self._target = target or ""
        self._latency_ms = latency_ms
        self._block = max(2, int(rate * 0.1)) * 2 * channels   # 100ms 一块
        self._proc: subprocess.Popen | None = None

    @property
    def argv(self) -> list[str]:
        argv = ["pw-record"]
        if self._target:                 # 空 = 用 PipeWire 默认源/输出（loopback 永远有 target）
            argv.append(f"--target={self._target}")
            # ★ 钉死目标（`node.dont-reconnect=true`）：**必须**，否则切系统默认麦时本流会被拽走。
            #
            # WirePlumber 的 `linking-utils.lua:checkFollowDefault`：当一条流被链到的目标
            # **恰好是当时的默认节点**时，它会把该流标记成"跟随默认"（metadata `target.node=-1`，
            # 模仿 PulseAudio）。之后只要用户切换系统默认麦克风，WirePlumber 就把这条流改接到
            # 新默认源——实测（2026-10-08）：`pw-record --target=<某源>` 的 `target.object`
            # 明明写着该源，但只要默认源一变（换成另一支麦 / 降噪源），它立刻跟着变。后果：
            #   ① 路由图（qpwgraph/helvum）里「那条采集线断了」；
            #   ② 代理/引擎**悄悄录错设备**。
            # `checkFollowDefault` 第 182 行 `reconnect = not parseBool(node.dont-reconnect)`：
            # 置 true 后直接跳过"标记跟随默认"，`prepare-link.lua` 也据此"不搬走"。实测加上后
            # 默认源来回切，本流始终钉在原目标。空 target（用 PipeWire 默认）时不加，
            # 保留"跟随默认"语义。
            argv += ["-P", "node.dont-reconnect=true"]
        argv += [
            "--format=s16",
            f"--rate={self.rate}",
            f"--channels={self.channels}",
            f"--latency={self._latency_ms}ms",
            _RAW,
            "-",
        ]
        return argv

    def _pump(self, stop: threading.Event) -> None:
        self._proc = subprocess.Popen(
            self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,                      # 不做用户态缓冲：100ms 一块要实时到位
            env=child_env())
        assert self._proc.stdout is not None
        try:
            while not stop.is_set():
                data = self._proc.stdout.read(self._block)
                if not data:
                    break                    # 子进程结束（设备被拔/daemon 重启）
                self._emit(data)
        finally:
            pass

    def _teardown(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log.warning("[loopback] pw-record 未响应 SIGTERM，改用 SIGKILL")
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

    def stderr_tail(self) -> str:
        """子进程的 stderr（出错排查用；进程已结束才读得到内容）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            if proc.poll() is None:
                return ""
            return proc.stderr.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""


class LinuxMicSource(PwRecordSource):
    """Linux 麦克风采集：`pw-record --target=<源 node.name>`（PipeWire 原生）。

    与 loopback 同一条路径，**只依赖 PipeWire 本体**（`pw-record`，项目已声明/已自检）。

    ⚠️ 为什么不再走 sounddevice（2026-10）：PortAudio 在 Linux 上把我们的设备名解析到
    **JACK** host API（名字只存在于 JACK 条目里），而 `libjack` 由**可选包** `pipewire-jack`
    提供 —— 没装它的 PipeWire 环境里，按名字开麦直接抛错、整条腿挂掉（且「自动检测」同样中招）。
    `pw-record --target=<node.name>` 只连 PipeWire 本体，没有这层隐藏依赖。

    `--channels=1`：由 PipeWire 服务端把源的**全部声道降混进这一路**（双声道 / 5.1 / 7.1 都
    覆盖）—— 正是「把所有声道混在同一个输入端口上」。
    """

    label = "mic"


def open_mic(device_name: str | None, *, rate: int = 16000, channels: int | None = None,
             blocksize: int) -> AudioSource:
    """麦克风采集（Linux 走 `pw-record`；Windows 走 sounddevice，见 `vlt/platform/win.py`）。

    ## 目标解析（描述 → `node.name`）
    `device_name` 是我们设备表里的**描述**，`pw-record --target=` 要的是 `node.name`，所以先经
    设备表映射；为空（自动检测）→ `default_source_node()` 取系统默认输入源的 `node.name`。
    都拿不到就不带 `--target`（PipeWire 默认源）。这样「自动检测」与手选走**同一条通路**。

    ## 采样率 / 声道
    **按调用方给的 `rate` 采集**（默认 16000）。PipeWire 图内重采样是系统级的：

      * 语音识别（ASR）传 16000 → 引擎里的 `to_16k_mono` 是恒等变换；
      * 麦克风代理的原声直通传 48000（= 虚拟声卡输出率）→ **全带宽**，不再被降到 16kHz
        再插值升回（否则人声 8kHz 以上全丢，听感发闷/电话音；见 `vlt/output/micproxy.py`）。

    `--channels=1`：由 PipeWire 服务端把源的**全部声道**降混进这一路，所以 `channels`
    参数在此**忽略**（保留签名只为与 Windows 对齐）。
    """
    import asyncio

    target = ""
    if device_name:
        target = _mic_target_node(device_name)
        if target:
            print(f"[mic] 采集源 {device_name!r} → pw-record --target={target}", flush=True)
        else:
            print(f"[mic] ⚠️ 设备表里查不到 {device_name!r} 的 node.name"
                  " → 用 PipeWire 默认输入", flush=True)
    else:
        target = default_source_node()
        if target:
            print(f"[mic] 自动检测 → 系统默认输入源 node.name={target!r}", flush=True)
        else:
            print("[mic] 自动检测：查不到系统默认输入源 → 用 PipeWire 默认输入", flush=True)

    rate_use = int(rate or 16000)
    src = LinuxMicSource(asyncio.get_running_loop(), target=target,
                         rate=rate_use, channels=1)
    src.start()
    return src


def _mic_target_node(device_name: str | None) -> str:
    """设备描述 / 稳定 `node.name` → PipeWire `node.name`；查不到返回 ""。

    匹配顺序：稳定 `node.name` 精确 → 旧配置的描述精确。
    GUI 保存稳定节点名；优先匹配它，避免另一设备的描述与节点名碰撞时录错设备。
    """
    if not device_name:
        return ""
    try:
        # 局部导入避免与 devices.py↔platform 的循环导入（与 win.open_mic 同一条纪律）。
        from ..devices import enumerate_mic_devices
        infos = enumerate_mic_devices()
        for info in infos:                    # 1) 稳定节点名优先
            node = str(getattr(info, "node_name", "") or "")
            if node and node == device_name:
                return node
        # 2) 旧描述必须唯一，不能按设备列举顺序猜测来源。
        matches = [str(getattr(info, "node_name", "") or "")
                   for info in infos if info.name == device_name]
        return matches[0] if len(matches) == 1 else ""
    except Exception:                         # noqa: BLE001 — 查不到不该让整条腿挂掉
        pass
    return ""


def open_loopback(target: LoopbackTarget, *, blocksize: int) -> AudioSource:
    """打开一路系统声采集。`target.id` 是 PipeWire 的 `node.name`。

    直接在 16kHz 单声道上采集：PipeWire 的图内重采样是系统级的（所有应用共用），
    质量有保证，也省掉 Python 侧一遍重采样。引擎里的 `to_16k_mono` 因此是恒等变换。
    """
    import asyncio

    src = PwRecordSource(asyncio.get_running_loop(), target=target.id,
                         rate=16000, channels=1)
    src.start()
    return src


# ---------------------------------------------------------------- 手腕屏后端

def create_wrist_overlay(cfg: Any, config_path: Any = None, dry_run: bool = False) -> Any:
    """手腕屏后端：Linux 用**自建 OpenXR overlay**（`vlt/output/openxr_overlay.py`）。

    与 Windows 侧结构对等：我们的进程直接作为 overlay session 连合成器，
    不依赖 WayVR 之类的第三方管理器、不注册、不写配置、不重启。
    两者公开接口一致，所以 engine/gui 不需要分平台。
    """
    from ..output.openxr_overlay import OpenXrOverlay
    return OpenXrOverlay(cfg, config_path=config_path, dry_run=dry_run)


# ---------------------------------------------------------------- 桌面叠加窗（issue #11）
#
# `vlt/output/desktop_overlay.py`（共享模块）通过 `vlt.platform` 门面要这组能力：
# `find_window_by_title / window_client_rect / is_window / set_click_through /
# set_tool_window / top_level_hwnd / screen_work_area`。
# Windows 侧在 `win.py` 里用 Win32 扩展样式实现；这里用**纯 ctypes 直调
# libX11 / libXext**（与 `openxr_overlay.py` 直调 libwayland/EGL 同款，不引入新依赖）。
#
# ⚠️ 三条实测结论（Xvfb + openbox 与 niri/XWayland 真机都验过，别踩回去）：
#
#   1. **Tk 的 `winfo_id()` 是内层客户窗**，合成器和命中测试看到的是外层包装窗
#      （带 WM_CLASS 的那个；Tk 自己也把 WM 协议属性写在 wrapper 上）。
#      形状 / 属性 / 位置一律落在 `top_level_hwnd()` 换出的顶层窗上——设错了的话
#      「鼠标穿透」会设了个寂寞：穿透区的点击直接消失，上层下层谁都不收（实测）。
#   2. **点击穿透 = `XShapeCombineRectangles(ShapeInput, 空)`**，实测点击会正确落到
#      下层窗口；恢复 = `XShapeCombineMask(None)`（回到默认输入区）。
#   3. **Tk 回落路径**（Wayland 会话里 Tk 走 XWayland）下这组调用仍可用（能找到窗口、
#      能读几何），但窗口位置 / 置顶 / 透明度由合成器决定：niri 实测忽略位置请求（按
#      平铺管理）、忽略 `_NET_WM_WINDOW_OPACITY`（属性写进去了、像素扫描仍不透明）。
#      ⚠️ 字幕窗现在**优先走两条原生腿**：Wayland 会话走 layer-shell
#      （`vlt/platform/wayland.py`）、X11 会话（含 XWayland）走 32 位 ARGB 覆盖窗
#      （`vlt/platform/x11.py`）；本组 X11 调用用于「找 VRChat 窗口、读几何」，
#      以及被两个原生后端复用（穿透 / 不抢焦点）。只有原生窗都建不起来时才回落 Tk。
#      首次用到本组能力时打印一行说明；边界见 `docs/GUIDE.linux.md` 的「桌面字幕」。
#
# 语义与 Windows 侧对齐（facade 的文档就是契约）：
#   * `find_window_by_title` 返回**客户窗口**（客户区矩形才准；WM 框会带上同样的
#     标题，所以取「最深的可见匹配」）；
#   * `top_level_hwnd` 返回「root 的直接子窗口」——覆盖窗场景就是我方的包装窗，
#     被 WM 管理的窗口则是 WM 框（用于「排除自己」时同样正确）。

_MAX_SEARCH_DEPTH = 6         # find_window_by_title 的树深上限（帧/客户一层就够，留余量）
_SHAPE_BOUNDING = 0           # SHAPE_KIND：0=Bounding 1=Clip 2=Input
_SHAPE_INPUT = 2              # SHAPE_KIND：0=Bounding 1=Clip 2=Input
_SHAPE_SET = 0                # SHAPE_OP：0=Set
_IS_VIEWABLE = 2              # XWindowAttributes.map_state
_XA_CARDINAL = 6              # 预定义 Atom：CARDINAL
_INPUT_HINT = 1 << 0          # XWMHints.flags 的 InputHint

_X11: Any = None
_XEXT: Any = None
_XDPY: Any = None
_X11_DEAD = False             # 加载失败过就不再重试（没装 X / 沙盒里连不上）
_ATOMS: dict[bytes, int] = {}
_WAYLAND_NOTE_DONE = False


class _XRect(ctypes.Structure):
    """XRectangle（libXext 形状接口用）。"""

    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


class _XWindowAttrs(ctypes.Structure):
    """XWindowAttributes（字段顺序/对齐按 Xlib.h；只用 map_state 与宽高）。"""

    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong),
        ("input_class", ctypes.c_int), ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long), ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p),
    ]


class _XWMHints(ctypes.Structure):
    """XWMHints（只用到 input 字段：标记「不抢焦点」）。"""

    _fields_ = [
        ("flags", ctypes.c_long), ("input", ctypes.c_int),
        ("initial_state", ctypes.c_int), ("icon_pixmap", ctypes.c_ulong),
        ("icon_window", ctypes.c_ulong), ("icon_x", ctypes.c_int),
        ("icon_y", ctypes.c_int), ("icon_mask", ctypes.c_ulong),
        ("window_group", ctypes.c_ulong),
    ]


# XErrorHandler 的 ctypes 原型：int (*)(Display *, XErrorEvent *)
_X_ERROR_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _load_x11() -> tuple[Any, Any, Any] | None:
    """惰性加载 libX11/libXext 并常驻一个 X 连接；任何一步失败 → None（整组能力降级）。

    声明全部显式 `argtypes/restype`：ctypes 默认按 C int 传参会把 64 位指针截断，
    这在 Xlib 这种处处 Pointer 的库上是必踩的坑。
    """
    global _X11, _XEXT, _XDPY, _X11_DEAD
    if _X11_DEAD:
        return None
    if _X11 is not None and _XDPY:
        return _X11, _XEXT, _XDPY
    try:
        x11 = ctypes.CDLL("libX11.so.6")
        xext = ctypes.CDLL("libXext.so.6")
    except OSError:
        _X11_DEAD = True
        return None
    try:
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XQueryTree.restype = ctypes.c_int
        x11.XQueryTree.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint)]
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XGetWindowAttributes.restype = ctypes.c_int
        x11.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XWindowAttrs)]
        x11.XTranslateCoordinates.restype = ctypes.c_int
        x11.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong)]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XGetWindowProperty.restype = ctypes.c_int
        x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        x11.XFetchName.restype = ctypes.c_int
        x11.XFetchName.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_char_p)]
        x11.XSetWMHints.restype = ctypes.c_int
        x11.XSetWMHints.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XWMHints)]
        x11.XSync.restype = ctypes.c_int
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        xext.XShapeCombineRectangles.restype = None
        xext.XShapeCombineRectangles.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(_XRect), ctypes.c_int, ctypes.c_int, ctypes.c_int]
        xext.XShapeCombineMask.restype = None
        xext.XShapeCombineMask.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_ulong, ctypes.c_int]
        x11.XCreateBitmapFromData.restype = ctypes.c_ulong
        x11.XCreateBitmapFromData.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_uint, ctypes.c_uint]
        x11.XFreePixmap.restype = ctypes.c_int
        x11.XFreePixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            _X11_DEAD = True
            return None
    except (OSError, AttributeError):
        _X11_DEAD = True
        return None
    _X11, _XEXT, _XDPY = x11, xext, dpy
    return _X11, _XEXT, _XDPY


def _guarded(x11: Any, dpy: Any, call: Any) -> Any:
    """在自有的 X 错误处理器下执行一次请求；出现 X 错误 → 返回 None。

    ⚠️ 这层护栏是**必须的**：Tk 进程里未被接管的 X 错误会被 Tk 错误处理器
    升级成致命错误——对一个刚关闭的窗口调 `XGetWindowAttributes`（BadWindow）
    会直接把整个应用带走。凡是可能落在「已销毁句柄」上的请求都要走这里。
    期间短暂接管全局错误处理器，结束后恢复（Tk 自己的处理器原样放回）；
    `XSync` 是为了把异步错误当场冲出来，不然错误可能在我们恢复处理器之后才到。
    """
    state = {"err": False}

    def _handler(_dpy: Any, _ev: Any) -> int:
        state["err"] = True
        return 0

    cb = _X_ERROR_CB(_handler)
    prev = x11.XSetErrorHandler(ctypes.cast(cb, ctypes.c_void_p))
    try:
        result = call()
        x11.XSync(dpy, 0)
    finally:
        x11.XSetErrorHandler(prev)
    return None if state["err"] else result


def _wayland_note() -> None:
    """Wayland 会话下第一条用户可感的降级说明（只打一次）。"""
    global _WAYLAND_NOTE_DONE
    if _WAYLAND_NOTE_DONE:
        return
    _WAYLAND_NOTE_DONE = True
    if os.environ.get("WAYLAND_DISPLAY"):
        print("[desktop] ℹ️ 检测到 Wayland 会话：字幕窗优先走原生 layer-shell 后端"
              "（本组 X11 调用只用来找 VRChat 窗口、读几何）；没有 layer-shell 的合成器"
              "（GNOME/Weston）只要有 XWayland 会走原生 ARGB 覆盖窗。两条原生腿都建不起来"
              "才回落 Tk（位置/透明度由合成器决定：niri 实测按平铺管理、忽略透明度）；"
              "边界见 docs/GUIDE.linux.md「桌面字幕」")


def _attrs(x11: Any, dpy: Any, wid: int) -> _XWindowAttrs | None:
    """取窗口属性；窗口不存在 / 已销毁 → None（错误由 `_guarded` 吞掉，绝不致命）。"""
    a = _XWindowAttrs()

    def call() -> bool:
        return bool(x11.XGetWindowAttributes(dpy, ctypes.c_ulong(wid), ctypes.byref(a)))

    return a if _guarded(x11, dpy, call) else None


def _query_tree(x11: Any, dpy: Any, wid: int) -> tuple[int, list[int]]:
    """`XQueryTree` 的薄封装：返回 (parent, children)；失败给 (0, [])。"""
    root_r, parent_r = ctypes.c_ulong(), ctypes.c_ulong()
    children_p = ctypes.POINTER(ctypes.c_ulong)()
    n = ctypes.c_uint()
    state = {"parent": 0, "kids": []}

    def call() -> bool:
        if not x11.XQueryTree(dpy, ctypes.c_ulong(wid), ctypes.byref(root_r),
                              ctypes.byref(parent_r), ctypes.byref(children_p),
                              ctypes.byref(n)):
            return False
        state["parent"] = int(parent_r.value)
        state["kids"] = [int(children_p[i]) for i in range(int(n.value))]
        return True

    ok = _guarded(x11, dpy, call)
    if children_p:                       # 无论成败都别泄内存
        x11.XFree(children_p)
    if not ok:
        return 0, []
    return state["parent"], state["kids"]


def _atom(x11: Any, dpy: Any, name: bytes) -> int:
    """`XInternAtom` 带缓存（每帧都问一次太浪费）。"""
    val = _ATOMS.get(name)
    if val is None:
        val = int(x11.XInternAtom(dpy, name, 0))
        _ATOMS[name] = val
    return val


def _window_title(x11: Any, dpy: Any, wid: int) -> str:
    """窗口标题：优先 `_NET_WM_NAME`（UTF8_STRING），退回 `WM_NAME`（XFetchName）。

    窗口可能在中途被销毁（X 错误由 `_guarded` 吞掉）→ 拿不到就给空串。
    """
    name_atom = _atom(x11, dpy, b"_NET_WM_NAME")
    utf8_atom = _atom(x11, dpy, b"UTF8_STRING")
    out = {"title": ""}

    def call() -> None:
        a_type, a_fmt = ctypes.c_ulong(), ctypes.c_int()
        n_items, after = ctypes.c_ulong(), ctypes.c_ulong()
        prop = ctypes.POINTER(ctypes.c_ubyte)()
        status = x11.XGetWindowProperty(
            dpy, ctypes.c_ulong(wid), ctypes.c_ulong(name_atom), 0, 1024, 0,
            ctypes.c_ulong(utf8_atom), ctypes.byref(a_type), ctypes.byref(a_fmt),
            ctypes.byref(n_items), ctypes.byref(after), ctypes.byref(prop))
        if status == 0 and prop:
            try:
                out["title"] = ctypes.string_at(prop, int(n_items.value)).decode(
                    "utf-8", "replace")
                return
            finally:
                x11.XFree(prop)
        name_p = ctypes.c_char_p()
        if x11.XFetchName(dpy, ctypes.c_ulong(wid), ctypes.byref(name_p)) and name_p.value:
            try:
                out["title"] = name_p.value.decode("utf-8", "replace")
            finally:
                x11.XFree(name_p)

    _guarded(x11, dpy, call)
    return out["title"]


# ---------------------------------------------------------------- 门面调用的公开函数


def top_level_hwnd(widget_id: int) -> int:
    """内层客户窗 → 「root 的直接子窗口」。

    覆盖窗（桌面字幕窗）：就是我方 Tk 的包装窗（形状/属性都往它身上设）；
    被 WM 管理的窗口：是 WM 框（拿去做排除清单同样正确）。
    拿不到 / 没有 X：原样返回（与 facade 的安全默认一致）。
    """
    loaded = _load_x11()
    if not loaded:
        return int(widget_id)
    x11, _xext, dpy = loaded
    _wayland_note()
    root = int(x11.XDefaultRootWindow(dpy))
    wid = int(widget_id)
    for _ in range(32):                  # 防御环/深树：最多上溯 32 层
        parent, _kids = _query_tree(x11, dpy, wid)
        if parent in (0, root):
            return wid
        wid = parent
    return wid


def find_window_by_title(substr: str, exclude: Any = ()) -> int | None:
    """按标题子串（不区分大小写）在窗口树里找**客户窗口**；找不到返回 None。

    * WM 框会继承客户的标题，最深层的可见匹配才是真客户窗（客户区才准）；
    * `exclude` = 我方窗口的顶层句柄（facade 传 `top_level_hwnd` 的结果），随时跳过；
    * 树深限 `_MAX_SEARCH_DEPTH`，多余的开销不值得。
    """
    loaded = _load_x11()
    if not loaded:
        return None
    x11, _xext, dpy = loaded
    _wayland_note()
    needle = str(substr).casefold()
    if not needle:
        return None
    skip = {int(v) for v in exclude} if exclude else set()
    root = int(x11.XDefaultRootWindow(dpy))
    best: tuple[int, int, int] | None = None      # (可见排名, 深度, wid)
    stack: list[tuple[int, int]] = [(c, 1) for c in _query_tree(x11, dpy, root)[1]]
    while stack:
        wid, depth = stack.pop()
        if wid in skip:
            continue
        a = _attrs(x11, dpy, wid)
        if a is None or a.width <= 1 or a.height <= 1:
            continue
        title = _window_title(x11, dpy, wid)
        if title and needle in title.casefold():
            rank = 1 if a.map_state == _IS_VIEWABLE else 0
            cand = (rank, depth, wid)
            if best is None or cand[:2] > best[:2]:
                best = cand
        if depth < _MAX_SEARCH_DEPTH:
            for c in _query_tree(x11, dpy, wid)[1]:
                stack.append((c, depth + 1))
    return best[2] if best is not None else None


def window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """目标窗口客户区的屏幕矩形 (left, top, right, bottom)。

    * 窗口不可见（最小化/隐藏）→ None，调用方据此暂停跟随并留一行日志；
    * 坐标经 `XTranslateCoordinates` 折算到 root，多屏（含负坐标）都对。
    """
    loaded = _load_x11()
    if not loaded:
        return None
    x11, _xext, dpy = loaded
    a = _attrs(x11, dpy, int(hwnd))
    if a is None or a.width <= 0 or a.height <= 0:
        return None
    if a.map_state != _IS_VIEWABLE:
        return None
    root = int(x11.XDefaultRootWindow(dpy))
    dx, dy = ctypes.c_int(), ctypes.c_int()
    child = ctypes.c_ulong()

    def call() -> bool:
        return bool(x11.XTranslateCoordinates(dpy, ctypes.c_ulong(int(hwnd)),
                                              ctypes.c_ulong(root), 0, 0,
                                              ctypes.byref(dx), ctypes.byref(dy),
                                              ctypes.byref(child)))

    if not _guarded(x11, dpy, call):
        return None
    return (int(dx.value), int(dy.value),
            int(dx.value) + int(a.width), int(dy.value) + int(a.height))


def is_window(hwnd: int) -> bool:
    """句柄还是一个真窗口吗。"""
    loaded = _load_x11()
    if not loaded:
        return False
    x11, _xext, dpy = loaded
    return _attrs(x11, dpy, int(hwnd)) is not None


def set_click_through(hwnd: int, on: bool) -> bool:
    """开/关鼠标穿透（X Shape 输入区）。

    开 = 输入区置空（点击落到下层窗口）；关 = 移除客户端输入区（回默认）。
    ⚠️ 必须作用在**顶层包装窗**上——facade 传进来的句柄来自 `top_level_hwnd`。
    """
    loaded = _load_x11()
    if not loaded:
        return False
    x11, xext, dpy = loaded
    wid = int(hwnd)
    if _attrs(x11, dpy, wid) is None:      # 句柄已失效：直接失败，别喂 Xlib 出错误日志
        return False

    def call() -> bool:
        if on:
            rects = (_XRect * 1)()         # n_rects=0 + ShapeSet：空区域 = 完全穿透
            xext.XShapeCombineRectangles(dpy, ctypes.c_ulong(wid), _SHAPE_INPUT,
                                         0, 0, rects, 0, _SHAPE_SET, 0)
        else:
            xext.XShapeCombineMask(dpy, ctypes.c_ulong(wid), _SHAPE_INPUT,
                                   0, 0, 0, _SHAPE_SET)   # src=None → 恢复默认输入区
        return True

    return _guarded(x11, dpy, call) is not None


def set_window_shape(hwnd: int, mask: bytes, width: int, height: int) -> bool:
    """给窗口设/换 1 位**形状蒙版**（X Shape Bounding）；蒙版外的像素不画、点击也不收。

    `mask` 口径：每行 `ceil(width/8)` 字节、**LSB-first**（最左像素 = 最低位）、行序
    自上而下 —— 即 `np.packbits(..., bitorder="little")` / XBM 的打包方式
    （`vlt/output/desktop_overlay.py:alpha_mask_bits()` 产的就是它）。Tk 回落路径用它
    抠掉面板外的键色底（Windows 的 Tk 有色键，Linux 只能靠蒙版）。

    ⚠️ 尺寸变了要**重设**（形状不会跟着窗口缩放）；尺寸对不上的蒙版比不设更糟（窗口
    会被裁成花形），所以字节数不够直接拒绝。
    """
    loaded = _load_x11()
    if not loaded:
        return False
    x11, xext, dpy = loaded
    wid = int(hwnd)
    w, h = int(width), int(height)
    if w <= 0 or h <= 0 or not mask or len(mask) < ((w + 7) // 8) * h:
        return False
    if _attrs(x11, dpy, wid) is None:      # 句柄已失效：别喂 Xlib 出错误日志
        return False
    buf = ctypes.create_string_buffer(bytes(mask), len(mask))   # 保活到请求吐给服务器

    def call() -> bool:
        pix = x11.XCreateBitmapFromData(dpy, ctypes.c_ulong(wid),
                                        ctypes.cast(buf, ctypes.c_void_p),
                                        ctypes.c_uint(w), ctypes.c_uint(h))
        if not pix:
            return False
        try:
            xext.XShapeCombineMask(dpy, ctypes.c_ulong(wid), _SHAPE_BOUNDING,
                                   0, 0, ctypes.c_ulong(pix), _SHAPE_SET)
        finally:
            x11.XFreePixmap(dpy, ctypes.c_ulong(pix))
        return True

    return _guarded(x11, dpy, call) is True


def set_tool_window(hwnd: int) -> bool:
    """标「不抢焦点」：WM_HINTS 的 input=False。

    X11 没有「不进 alt-tab」的统一开关，覆盖窗本来也不受 WM 管理；重点是
    **别把焦点从游戏里抢走**（WM 看到 input=False 就不给焦点）。
    """
    loaded = _load_x11()
    if not loaded:
        return False
    x11, _xext, dpy = loaded
    hints = _XWMHints()
    hints.flags = _INPUT_HINT
    hints.input = 0

    def call() -> bool:
        return bool(x11.XSetWMHints(dpy, ctypes.c_ulong(int(hwnd)), ctypes.byref(hints)))

    return bool(_guarded(x11, dpy, call))


def screen_work_area() -> tuple[int, int, int, int]:
    """屏幕工作区 (left, top, right, bottom)。

    优先 EWMH `_NET_WORKAREA`（常规 X11 桌面都有）；没有就退**整屏几何**
    （niri 的 XWayland 实测没有这个属性；整屏是并集画布，夹取不会把字幕夹出屏）。
    """
    loaded = _load_x11()
    if not loaded:
        return (0, 0, 1920, 1080)
    x11, _xext, dpy = loaded
    root = int(x11.XDefaultRootWindow(dpy))
    prop = ctypes.POINTER(ctypes.c_ubyte)()
    result: dict[str, tuple[int, int, int, int]] = {}

    def call() -> None:
        atom = _atom(x11, dpy, b"_NET_WORKAREA")
        a_type, a_fmt = ctypes.c_ulong(), ctypes.c_int()
        n_items, after = ctypes.c_ulong(), ctypes.c_ulong()
        status = x11.XGetWindowProperty(
            dpy, ctypes.c_ulong(root), ctypes.c_ulong(atom), 0, 4, 0,
            ctypes.c_ulong(_XA_CARDINAL), ctypes.byref(a_type), ctypes.byref(a_fmt),
            ctypes.byref(n_items), ctypes.byref(after), ctypes.byref(prop))
        if status == 0 and prop and a_fmt.value == 32 and int(n_items.value) >= 4:
            vals = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))
            left, top, wid, hei = (int(vals[i]) for i in range(4))
            if wid > 0 and hei > 0:
                result["area"] = (left, top, left + wid, top + hei)

    try:
        _guarded(x11, dpy, call)
    finally:
        if prop:
            x11.XFree(prop)
    if "area" in result:
        return result["area"]
    a = _attrs(x11, dpy, root)
    if a is not None and a.width > 0 and a.height > 0:
        return (0, 0, int(a.width), int(a.height))
    return (0, 0, 1920, 1080)


# ---------------------------------------------------------------- 桌面叠加窗：原生窗


def create_desktop_window(size: tuple[int, int], alpha: float = 1.0,
                          click_through: bool = True,
                          on_drag_end: Any = None, backend: str = "auto") -> Any:
    """给桌面字幕开一个**原生窗**（Wayland：layer-shell；X11：32 位 ARGB 覆盖窗）。

    返回 `None` 表示「本会话用不了原生窗」，调用方（`desktop_overlay`）回落 Tk：
      * `backend="tk"`：显式要求 Tk；
      * `auto`/`native` 的候选顺序：有 `WAYLAND_DISPLAY` → 原生 Wayland
        （没有 layer-shell 的合成器由后端自己失败）→ 有 `DISPLAY` → 原生 X11
        （纯 Xorg 会话、以及带 XWayland 的 GNOME/Weston 都吃这条腿）；
      * 建窗失败 / 没有 32 位 visual / 缺库 → 继续/回落。
    每一步的**原因**都在这里/后端模块里打出来（门面与共享模块不吭声）。
    """
    if backend == "tk":
        return None
    blocked = _test_process_guard("建原生桌面窗（会连到用户正在用的合成器）")
    if blocked:
        print(f"[desktop] ⚠️ {blocked} → 回落 Tk", flush=True)
        return None

    order: list[str] = []
    if backend in ("auto", "native"):
        if os.environ.get("WAYLAND_DISPLAY"):
            order.append("wayland")
        if os.environ.get("DISPLAY"):
            order.append("x11")
    else:
        order.append(backend)

    for kind in order:
        if kind == "wayland":
            from .wayland import LayerShellWindow as cls      # noqa: PLC0415
            label = "Wayland"
        elif kind == "x11":
            from .x11 import ArgbWindow as cls                # noqa: PLC0415
            label = "X11"
        else:  # pragma: no cover —— BACKENDS 已挡掉未知值，兜底不崩
            print(f"[desktop] ⚠️ backend={kind!r} 不认识 → 跳过", flush=True)
            continue
        try:
            win = cls(size=size, alpha=alpha, click_through=click_through,
                      on_drag_end=on_drag_end)
        except Exception as exc:  # noqa: BLE001 —— 缺库/构造异常都不该带崩进程
            print(f"[desktop] ⚠️ 建原生 {label} 窗异常（继续/回落 Tk）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            continue
        if not win.available:
            win.close()
            continue
        return win
    return None
