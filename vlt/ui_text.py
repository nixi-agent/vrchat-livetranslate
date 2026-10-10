"""界面用的纯文本/字符串逻辑：语言标签映射、词库以外的小工具与常量。

为什么放在这里（而不是留在 gui.py）：
  · 这些是**纯函数 / 常量**（语言码↔名对照、YAML 标量引号、可写性探测、
    本地试听、音色不支持判定、房间段模板…），不依赖任何 Tk 实例；
  · gui.py 只做接线，抽出来后便于离线单测（tests/test_langs.py 等直接打这些）。

约束：**不 import tkinter**（保持纯 Python）。需要 Tk 的放到 vlt/ui_tk.py。
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import endpoints, platform
from .config_io import _write_config_text, _yaml_set_in_text
from .i18n import t
from .paths import BUNDLE_DIR

# 音色试听：固定样例句 + 打字侧合成模型。两条腿各走各的模型（音色不通用）：
#   打字侧 qwen3-tts-flash（一次性 HTTP）；说话侧走非实时 Qwen-Omni（见 tts.synthesize_omni，
#   因为 Tina 等实时音色只有 Omni 认）。VOICE_PREVIEW_MODEL 只用于打字侧。
VOICE_PREVIEW_TEXT = "你好，这是我的音色试听。"
VOICE_PREVIEW_MODEL = "qwen3-tts-flash"


def _provider_choices() -> tuple[tuple[str, str], ...]:
    """服务线路下拉的候选：`(显示名, 线路 id)`。

    为什么是**函数**而不是常量表：显示名要走 `t()`，而模块 import 发生在
    `i18n.set_language()` 之前，常量表会把中文冻在里面（英文界面就漏翻了）。

    ⚠️ 配置里只写 id（`qianwen` / `qwencloud`）：把显示名当 key 写进 config.yaml 的话，
    用户一换界面语言配置就"失效"了 —— 同一件事存两份迟早漂移（见 endpoints.py 的铁律）。
    """
    return ((t("千问云"), endpoints.PROVIDER_QIANWEN),
            (t("千问云·海外版"), endpoints.PROVIDER_QWENCLOUD),
            (t("ChatGPT 订阅语音（消耗 Codex 额度）"), endpoints.PROVIDER_CHATGPT))


def _persist_provider(cfg_path: "Path", provider: str, base_url: str) -> None:
    """把线路两项（provider / base_url）就地写回 config.yaml；**先复检、后写盘**。

    为什么要复检：`_yaml_set_in_text` 找不到路径时**原样返回**（不报错、不抛），
    于是 `_write_config_text` 写出的是同一份文本 —— 表现为「点了保存、界面还说成功、
    配置其实一点没变」，正是本仓库最忌讳的静默降级。老版本 config.yaml 若没有
    `session:` 段就会踩这条（room 段有补建逻辑，session 段没有）。

    复检放在**写盘之前**：解析的是即将写入的文本，失败时磁盘上的文件一个字没动，
    所以调用方那句「配置未改动」是真话。
    """
    text = cfg_path.read_text(encoding="utf-8")
    text = _yaml_set_in_text(text, ["session", "provider"], provider)
    # base_url **裸写**：与模板口径一致（加引号会让手改配置的人以为它是个字符串常量）
    text = _yaml_set_in_text(text, ["session", "base_url"], base_url)
    got = (yaml.safe_load(text) or {}).get("session") or {}
    if got.get("provider") != provider or got.get("base_url") != base_url:
        raise RuntimeError(
            "config.yaml 里没有 session: 段（或键名不符），线路写不进去 —— "
            "请手工补一段 session: ，或删掉该文件让它按模板重新生成")
    _write_config_text(cfg_path, text)


# 老用户的 config.yaml（旧模板生成）没有 room 段，而 config_io 的就地改文本
# 「找不到路径就原样返回」→ 表现为静默不保存。勾选房间时若发现缺段，就用这段补建
# （逐字段对齐 config.example.yaml，含已部署的 server_url，补出来即可用）。
_ROOM_SECTION_TEMPLATE = (
    "# ---- 房间：多人各自跑 VLT 时互相看字幕（默认关，不影响现有单机用法）----\n"
    "room:\n"
    "  enabled: false\n"
    '  server_url: "wss://vlt-room.kcm-nixi.cn/ws"\n'
    '  room_code: ""            # 8 位，两端必须一致（不含 I/L/O/U）\n'
    '  nickname: ""             # 空 = 用系统用户名\n'
    '  token: ""                # 服务端开了门禁才需要\n'
    "  broadcast_source: true   # 把「我」说的话发到房间\n"
    "  show_remote: true        # 把别人说的话显示在手腕屏\n"
    "  max_peers: 8\n"
    "  reconnect_backoff: [2, 5, 10, 30]\n"
    "  heartbeat_s: 20\n"
)


def _yaml_quote(s) -> str:  # noqa: ANN001, ANN202
    """把字符串安全地写成 YAML 双引号标量。

    昵称可能含空格 / 冒号 / `#`，裸写会破坏 YAML（`_write_config_text` 会校验并拒写，
    表现为「保存没生效」）；房间码是 Crockford Base32 但也一并引号化，口径统一。
    """
    txt = "" if s is None else str(s)
    return '"' + txt.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sponsor_qr_specs() -> list[tuple[str, Path]]:
    """赞助弹窗的两张收款码：(标签, 图片路径)。只读资源一律走 bundle_dir()。"""
    assets = BUNDLE_DIR / "assets"
    return [(t("微信"), assets / "sponsor-wechat.png"),
            (t("支付宝"), assets / "sponsor-alipay.png")]


SOURCE_LANGS = {
    "自动检测": None,
    "中文": "zh",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
    "俄语": "ru",
    "泰语": "th",
    "意大利语": "it",
}

TARGET_LANGS = {
    "中文": "zh",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
    "俄语": "ru",
    "泰语": "th",
    "意大利语": "it",
}


def updater_env() -> dict[str, str]:
    """启动更新器（以及它间接拉起的新实例）时用的环境变量。

    ⚠️ 必须剥掉 PyInstaller 的内部变量（`_MEI*` / `_PYI_*`）—— 这是真机流程实测抓到的坑：
    单文件 exe 的引导器靠 `_PYI_PARENT_PROCESS_LEVEL` / `_PYI_ARCHIVE_FILE` / `_MEIPASS*`
    判断「我是不是已经被父进程解包好的子进程」。本程序拉起更新器时若原样继承自己的环境，
    更新器里 `start` 出来的**新版本**就会带着 `_PYI_PARENT_PROCESS_LEVEL=1` 启动 →
    引导器以为无需自解包 → 直接起不来（不写日志、无窗口、只留一个空转进程）。
    用户看到的症状是「点完更新、程序自己关了、再没打开」。

    ⚠️ Linux/AppImage 是**同一类坑的另一半**：AppImage 运行时看到 `APPDIR` 已经存在就
    **不会重新挂载**（它以为自己是「已被解包的子进程」），新进程会去用父进程那个马上要随
    父进程消失的挂载点 —— 症状一样是「更新完没再打开」。所以 `APPIMAGE` / `APPDIR` /
    `OWD` / `ARGV0` 也要剥掉，让新 AppImage 干干净净地自己挂载。
    （`PYTHONPATH` 不清：AppRun 是**追加**而不是覆盖继承值，新挂载的路径排在前面，
    清掉反而会抹掉用户自己设的东西。）

    ⚠️ 还有第三条（`platform.child_env()`）：PyInstaller 会把包内目录前置进
    `LD_LIBRARY_PATH`。更新器是宿主程序、新实例自己会重新挂载 —— 都不能带着父进程
    「旧挂载点」的库搜索路径去启动（那目录马上随父进程消失，路径还排在前面）。
    """
    strip = ("_MEI", "_PYI_", "APPIMAGE", "APPDIR", "OWD", "ARGV0")
    return {k: v for k, v in platform.child_env().items() if not k.startswith(strip)}


def _source_name(code: str | None) -> str:
    return next((k for k, v in SOURCE_LANGS.items() if v == code), "自动检测")


def _target_name(code: str) -> str:
    return next((k for k, v in TARGET_LANGS.items() if v == code), "英语")


def _lang_label(name: str) -> str:
    """界面显示用的语言名：表里的中文名 → 当前界面语言的写法。

    语言表本身（SOURCE_LANGS/TARGET_LANGS）永远用中文名做 key —— 它是配置/引擎的
    契约（config.yaml 存的是语言码，表只是码↔名的对照），界面才做翻译。
    词表里没有该条目时 t() 原样返回中文，不会炸。
    """
    return t(name)


def _lang_key(shown: str, table: dict[str, str | None]) -> str | None:
    """反查：下拉框里显示的那一项（可能是译名）→ 语言表里的中文 key。"""
    if shown in table:
        return shown
    for key in table:
        if t(key) == shown:
            return key
    return None


@dataclass
class _Bubble:
    """一条聊天气泡。items = 这条气泡占用的 Canvas 图元（就地重画时整体删掉）。"""
    who: str
    source: str
    text: str
    ts: str
    final: bool = False
    y: int = 0
    h: int = 0
    items: list = field(default_factory=list)
    label: str = ""   # 远端成员昵称：作为小字显示在气泡上方（本机气泡为空）


class _DownloadCancelled(Exception):
    """用户关窗取消下载：progress 回调在下载线程里抛出它，download_and_verify
    会在自己的 except 里清掉残留 .new 再原样上抛 —— 取消路径不需要额外清理。"""


def _dir_writable(d: Path) -> bool:
    """目录可写性探测：真的建一个临时文件再删掉（光猜权限位在 Windows 上不可靠）。"""
    try:
        fd, name = tempfile.mkstemp(dir=d, prefix=".upd_write_probe_")
    except OSError:
        return False
    try:
        os.close(fd)
        Path(name).unlink()
    except OSError:
        pass
    return True


def _play_pcm_local(pcm_24k_mono: bytes) -> None:
    """在**本地默认输出设备**播放 24kHz 单声道 s16le PCM（阻塞到播完）。

    试听走本地扬声器，绝不进虚拟声卡 —— 否则对面会在 VRChat 里听到你的试听音。
    离线测试会把它打桩替换（CI 机器没有音频设备，也不该真出声）。

    ⚠️ 平台差异：Linux 走 **`pw-cat --playback`**（PipeWire 原生，与采集同一条），
    **不再经过 PortAudio** —— 这样 Linux 产物可以整个不打包 sounddevice/PortAudio，
    少一个「缺可选包就静默失效」的隐藏依赖。Windows 仍走 sounddevice。
    失败会抛出（调用方按「试听失败」处理），不静默。
    """
    if not pcm_24k_mono:
        return
    if platform.IS_LINUX:
        import subprocess

        res = subprocess.run(
            ["pw-cat", "--playback", "--format=s16", "--rate=24000",
             "--channels=1", "--raw", "-"],       # `--raw` 不能省：不给会按容器格式打开而没播出去
            input=pcm_24k_mono, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=platform.child_env(), timeout=60)
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"pw-cat 播放失败：退出码 {res.returncode}"
                               + (f"：{err}" if err else ""))
        return
    import numpy as np
    import sounddevice as sd

    arr = np.frombuffer(pcm_24k_mono, dtype=np.int16)
    sd.play(arr, samplerate=24000, blocking=True)


# 服务端拒收音色的典型报错标记：说话译音用的是实时模型（Qwen-Omni）音色，
# 像默认的 Tina 根本不在 qwen3-tts-flash 的支持表里，合成会返回 InvalidParameter。
_UNSUPPORTED_VOICE_MARKERS = ("invalidparameter", "not supported", "is not support",
                              "engine error")


def _is_unsupported_voice_err(msg: str) -> bool:
    """判断一条 TtsError 是不是「音色不被该模型支持」—— 用来把说话侧的失败
    说成人话（而不是甩一串服务端原始报文）。"""
    low = (msg or "").lower()
    return any(m in low for m in _UNSUPPORTED_VOICE_MARKERS)
