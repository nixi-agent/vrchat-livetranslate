"""Tkinter 图形界面：聊天气泡视图 + 开关 + 语言镜像 + 双向同时。

用法：
  python -m vlt.gui                    # 启动界面
  python -m vlt.gui --self-test        # 自动化验收（单方向，不起窗口）
  python -m vlt.gui --self-test-dual   # 双向同时验收（两个 PCM 驱动两个引擎）
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import yaml

from . import __version__, crashlog, endpoints, i18n, tts, update_check
from .config import Direction, _as_str_map, DEFAULT_CONFIG, load_api_key, load_config
from .config_io import (
    _fmt_scalar,
    _write_config_text,
    _yaml_scalar,
    _yaml_set_in_text,
    _yaml_set_mapping,
    _yaml_set_or_create,
)
from .i18n import t
from .output.overlay import OverlayConfig, resolve_offset
from .devices import (
    DeviceInfo,
    enumerate_audio_out_devices,
    enumerate_loopback_devices,
    enumerate_mic_devices,
    format_device_display,
)
from .engine import (
    INPUT_GATE_MAX_DB,
    INPUT_GATE_MIN_DB,
    LEVEL_FLOOR_DB,
    Engine,
    EngineEvents,
    input_gate_settings,
)
from .level_probe import LevelProbe
from .room.client import RoomClient
from .room.model import ConnectionState, RoomConfig, RoomMessage
from .room.protocol import new_room_code
from .room.publisher import SourcePublisher, should_publish
from .voices import REALTIME_VOICES, TTS_VOICES, voice_choices

from .paths import APP_DIR, BUNDLE_DIR
from . import platform
from .platform import IS_WINDOWS

ROOT = APP_DIR

# 音色试听：固定样例句 + 打字侧合成模型。两条腿各走各的模型（音色不通用）：
#   打字侧 qwen3-tts-flash（一次性 HTTP）；说话侧走非实时 Qwen-Omni（见 tts.synthesize_omni，
#   因为 Tina 等实时音色只有 Omni 认）。VOICE_PREVIEW_MODEL 只用于打字侧。
VOICE_PREVIEW_TEXT = "你好，这是我的音色试听。"
VOICE_PREVIEW_MODEL = "qwen3-tts-flash"

# 界面字体族：**不能写死** "Microsoft YaHei UI"。
# 那个族在 Linux 上不存在，Tk 会静默回落到没有中日韩字形的 `fixed` ——
#   1) 中文靠逐字 fontconfig 回落渲染，实测**每次 measure() 要 0.3 秒**，
#      界面构建要把 4 种语言的控件全量一遍测量，慢到看起来像卡死
#      （tests/test_i18n.py 与 test_update_dialog.py 就是这样超时的）；
#   2) 观感也不对（字形族不一致）。
# 所以运行时按「这台机器真的有什么」挑一个（见 _apply_ui_font）。
_FONT_CANDIDATES = (
    "Microsoft YaHei UI", "Microsoft YaHei",                  # Windows
    "Noto Sans CJK SC", "Source Han Sans CN", "Noto Sans SC",  # Linux（Noto / 思源）
    "WenQuanYi Micro Hei", "Noto Sans", "DejaVu Sans",         # 再兜一层
)
_ui_family: str | None = None

# 下面这组是**占位**默认值，`_apply_ui_font()` 会在建 Tk root 之后按平台重绑。
FONT = ("Microsoft YaHei UI", 11)          # 译文（主）
FONT_SMALL = ("Microsoft YaHei UI", 9)     # 原文（辅，小一号）
FONT_META = ("Microsoft YaHei UI", 8)
FONT_UI = ("Microsoft YaHei UI", 9)        # 控件文字
FONT_STATUS = ("Microsoft YaHei UI", 8)    # 状态栏
FONT_BOLD_SM = ("Microsoft YaHei UI", 8, "bold")    # 分区小标题
FONT_BOLD_MD = ("Microsoft YaHei UI", 12, "bold")   # 弹窗小标题
FONT_BOLD_LG = ("Microsoft YaHei UI", 13, "bold")   # 弹窗大标题（赞助）


def resolve_ui_family(root) -> str:
    """挑一个这台机器上**真实存在**的界面字体族。

    先按候选表找；都没有就退回 Tk 自己的默认字体族（`TkDefaultFont` 的 actual family），
    保证至少是一个有字形的真字体，而不是 `fixed`。
    """
    global _ui_family
    if _ui_family:
        return _ui_family
    try:
        available = {str(f).strip().lower() for f in tkfont.families(root)}
    except Exception:  # noqa: BLE001 — 拿不到列表就退回默认
        available = set()
    for cand in _FONT_CANDIDATES:
        if cand.lower() in available:
            _ui_family = cand
            return cand
    try:
        _ui_family = str(tkfont.nametofont("TkDefaultFont").actual("family"))
    except Exception:  # noqa: BLE001
        _ui_family = "sans-serif"
    return _ui_family


def _apply_ui_font(root) -> None:
    """按当前平台重绑界面字体常量（在建 Tk root 之后、建任何控件之前调用）。"""
    global FONT, FONT_SMALL, FONT_META, FONT_UI, FONT_STATUS
    global FONT_BOLD_SM, FONT_BOLD_MD, FONT_BOLD_LG
    fam = resolve_ui_family(root)
    FONT = (fam, 11)
    FONT_SMALL = (fam, 9)
    FONT_META = (fam, 8)
    FONT_UI = (fam, 9)
    FONT_STATUS = (fam, 8)
    FONT_BOLD_SM = (fam, 8, "bold")
    FONT_BOLD_MD = (fam, 12, "bold")
    FONT_BOLD_LG = (fam, 13, "bold")


MAX_BUBBLES = 500

# ---- 统一配色：深灰 + 蓝（明度阶梯：聊天区最暗 → 面板次之 → 控件最亮） ----
BG            = "#14161c"   # 聊天区背景（最暗）
PANEL         = "#1b1e26"   # 顶栏 / 状态栏 / 窗口底色
SURFACE       = "#262a33"   # 按钮 / 下拉框 / 指示器底色（最亮一档）
SURFACE_HOVER = "#303541"   # 悬停
BORDER        = "#2e333d"   # 边框 / 分割线
ACCENT        = "#2f6fd0"   # 主色蓝（与"我说的"气泡同色）
ACCENT_HOVER  = "#3a7de0"
ACCENT_ACTIVE = "#2559a8"   # 按下
# 「反向动作」按钮（房间的「断开连接」）：红棕一档，明显区别于蓝色的「连接房间」。
# 刻意压暗、不用 COLOR_ERROR 那种亮红 —— 断开不是危险操作，只是"往回走"，
# 亮红会让人以为点了会出大事；跟着面板的明度体系走才不会在深色界面里跳出来。
DANGER        = "#a8443f"
DANGER_HOVER  = "#bf4f49"
DANGER_ACTIVE = "#8a3733"
TEXT          = "#e8eaee"   # 主文字
TEXT_DIM      = "#9aa1ad"   # 次要文字
TEXT_MUTED    = "#6f7480"   # 时间戳 / 占位
COLOR_MINE    = ACCENT      # 气泡：我说的
COLOR_THEIRS  = "#33363f"   # 气泡：别人说的
COLOR_TEXT    = "#ffffff"
COLOR_META    = TEXT_MUTED
COLOR_OK      = "#4a90d9"   # 状态栏 info
COLOR_WARN    = "#d9904a"
COLOR_ERROR   = "#e05a5a"
# 原文小字的颜色：比译文暗一档但仍清晰可读（按气泡底色分别取，保证对比度）
COLOR_SRC_MINE = "#c3d4ee"
COLOR_SRC_THEIRS = TEXT_DIM

# ---- 赞助弹窗 ----
SPONSOR_URL = "https://ko-fi.com/kcmnixi"
SPONSOR_QR_SIZE = 240          # 收款码等比缩放的目标边长（严禁拉伸：拉变形就扫不出来）

# ---- 赞助者名单（「设置 → 关于」页里展示）----
# 只是**名字**：专有名词，**不进词表、不翻译** —— 界面语言换成英/日/韩/俄时也照原样显示
# （`tests/test_i18n.py` 的语言守卫按**控件**显式排除这一行，见那里的说明）。
# 加人 = 往元组末尾追加一项（顺序即展示顺序）；留空元组 = 整区不显示（宁可没有，也不留空标题）。
SPONSORS: tuple[str, ...] = ("小夜",)

# ---- 千问云开通页（未配置 API key 时，状态按钮点击跳转）----
# 链接逐字符照抄，不做任何 URL 解码/重组。
# ⚠️ 这个常量**必须原样保留**：tests/test_api_key_gui.py 直接断言它的值，
# 并断言未配置态点按钮时 webbrowser.open() 收到的就是它。千问云线路的实际跳转
# 走下面的 `_signup_url()`（按当前线路取），在 provider=qianwen 时两者逐字符相同。
QIANWEN_SIGNUP_URL = "https://www.qianwenai.com/"


def _provider_choices() -> tuple[tuple[str, str], ...]:
    """服务线路下拉的候选：`(显示名, 线路 id)`。

    为什么是**函数**而不是常量表：显示名要走 `t()`，而模块 import 发生在
    `i18n.set_language()` 之前，常量表会把中文冻在里面（英文界面就漏翻了）。

    ⚠️ 配置里只写 id（`qianwen` / `qwencloud`）：把显示名当 key 写进 config.yaml 的话，
    用户一换界面语言配置就"失效"了 —— 同一件事存两份迟早漂移（见 endpoints.py 的铁律）。
    """
    return ((t("千问云"), endpoints.PROVIDER_QIANWEN),
            (t("千问云·海外版"), endpoints.PROVIDER_QWENCLOUD))


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
# ---- 设置弹窗（分页）----
# 宽度**固定**：每页的长说明都按 SETTINGS_WRAP 换行，于是各语言的窗宽一致，
# 不会因为俄语文案长就忽然变宽（也不再靠「窗口自然撑大 → 超出屏幕」）。
SETTINGS_WIDTH = 760
SETTINGS_WRAP = 660            # 长说明的换行宽 = 窗宽 - 左右留白(40) - 滚动条(~12) - 余量
SETTINGS_MIN_H = 360           # 再小的屏也至少给这么多高（内容靠页面滚动兜底）
SETTINGS_MAX_H = 900           # 上限：1080p 屏（可用高约 1040）也必须整窗看得见
# 点「停止翻译」后等引擎收尾的上限（**在后台线程里等**，绝不冻界面）。
# 实测正常路径：会话关闭 ≤1.8s + chatbox 排空 ≤2s → 单个引擎基本 2s 内收尾完。
STOP_WAIT_S = 5.0              # 单个引擎；收尾线程**逐个**等，两个引擎最坏 10s（但在后台）
CLOSE_WAIT_STOP_S = 6.0        # 关窗时**界面最多**等这么久，等不到就直接关
#                              （上面的收尾线程是 daemon，进程退出会释放麦克风/虚拟声卡）

SETTINGS_CHROME_H = 66         # tab 条 + 页面上下留白：算窗高时在内容高度上加这一份
# 每页内容 frame 的左右内边距（内容区位置固定，不随标签条动）
TAB_INSET_X = 20


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


_char_width_cache: dict[tuple, int] = {}


def _font_spec_key(font_spec) -> object:
    """把字体规格压成可哈希的缓存 key（跨平台容错）。

    `font_spec` 可能是 tuple、字符串（字体名）、`tkfont.Font`，或 —— 在 Linux 上 ——
    `widget.cget("font")` 返回的 `_tkinter.Tcl_Obj`（不可迭代，直接 `tuple()`
    会抛 `TypeError: '_tkinter.Tcl_Obj' object is not iterable`）。
    """
    if isinstance(font_spec, str):
        return font_spec
    try:
        return tuple(font_spec)
    except TypeError:
        return str(font_spec)


def _char_width_for(text: str, font_spec, minimum: int = 0) -> int:
    """把「这段文字需要多宽」换算成 Tk 的**字符宽度单位**（给 width= 用）。

    ⚠️ 坑（实测）：Tk 的 `width=N` 单位是**字体平均字符宽**（本机 ≈7px），而一个汉字/假名
    约等于 2 倍宽 ⇒ `width=len(text)` 对中日韩文字**必然裁字**：
    「訳文の文字サイズ」自然宽 100px，按 8 个字符只申请到 67px，屏幕上只剩「訳文の文字」。
    中文同样中招（「译文字号」需 52px、按 6 字符只给 46px），只是裁得少不容易看出来。
    这里用字体的真实 measure 换算，并留 1 个字符余量。

    结果**带缓存**：切界面语言会把整套控件重建一遍，同一个词条会被反复测量；
    而一次 `measure()` 在字体需要 fontconfig 回落的机器上要几百毫秒
    （见上方 _FONT_CANDIDATES 的说明），不缓存会明显卡顿。
    """
    key = (text, _font_spec_key(font_spec), minimum)
    hit = _char_width_cache.get(key)
    if hit is not None:
        return hit
    try:
        f = tkfont.Font(font=font_spec)
        avg = max(1, f.measure("0"))
        need = -(-f.measure(text) // avg) + 1     # 向上取整 + 1 字符余量
    except Exception:  # noqa: BLE001 — 量不出来就退回字符数（至少不比改动前差）
        need = len(text) + 1
    out = max(minimum, need)
    _char_width_cache[key] = out
    return out


def _combo_width(names, minimum: int = 9, font_spec=None) -> int:
    """下拉框宽度（字符单位）：按当前语言里最长的名字算，避免被截断。

    同样受「平均字符宽 ≠ 汉字宽」影响，所以走 `_char_width_for` 换算，不直接数字符。
    """
    f = font_spec or FONT_UI
    return max(minimum, max((_char_width_for(str(n), f) for n in names), default=0))


def combo_values(combo) -> list[str]:
    """读回 ttk.Combobox 的候选值（跨平台安全）。

    ⚠️ 坑（实测）：`combo.cget("values")` 的**返回类型依平台而变** ——
    Windows 上 Tk 返回 tuple（可直接 `list()`），Linux 上返回
    `_tkinter.Tcl_Obj`（不可迭代，`list()` 直接抛
    `TypeError: '_tkinter.Tcl_Obj' object is not iterable`）。
    这在设备下拉里是**真会走到**的路径（`_on_device_change` 要按下标取回原始设备名），
    不是只影响测试。

    用 Tk 自己的 `splitlist` 归一化：tuple / 列表 / 空格分隔的字符串 / Tcl_Obj 都能吃。
    """
    try:
        return [str(v) for v in combo.tk.splitlist(combo.cget("values"))]
    except Exception:  # noqa: BLE001 — 读不到就当空，别让「保存设备选择」这一步炸掉
        return []


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """圆角矩形：polygon + smooth=True 才有圆角。"""
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
           x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


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
    """
    if not pcm_24k_mono:
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


# ---------------------------------------------------------------- 专有词库的文本格式
# 界面上一行一条：`原文=译名`。为什么用这个格式而不是 JSON / YAML：
#   · 用户是主播，不是程序员 —— 敲 `原文=译名` 不需要懂缩进和引号；
#   · 一行一条，删一条就删一行，改坏了也不影响别人（JSON 少个逗号整段报废）；
#   · 与 config.yaml 里的映射表一一对应，肉眼能对上。
# 解析纪律：以 `#` 开头的行是注释、空行忽略；**只按第一个等号切**，
# 这样译名里带 `=`（或中文全角 `＝`）也不会切错。

def _iter_glossary_lines(text: str):
    """逐行分类：产出 `(行号, 原文, 译名, 忽略原因, 原样内容)`。

    - 空行 / `#` 注释 = 正常跳过（原因 `""`）
    - 原文/译名为 `None` 且原因非空 = 用户**写了内容但格式看不懂** —— 以前这类行被静默丢掉，
      用户写了 `原文：译名` 只会觉得「保存没反应」，所以要能报出来。
    """
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            yield lineno, None, None, "", line
            continue
        if "=" in line:
            src, _, tgt = line.partition("=")
        elif "＝" in line:
            # 容忍全角等号（中文输入法下极易打出来），否则用户会以为「保存没反应」
            src, _, tgt = line.partition("＝")
        else:
            yield lineno, None, None, "缺少等号", line
            continue
        src, tgt = src.strip(), tgt.strip()
        if not (src and tgt):
            yield lineno, None, None, "等号有一侧是空的", line
            continue
        yield lineno, src, tgt, "", line


def _parse_glossary_lines(text: str) -> dict[str, str]:
    """把界面文本框的内容解析成 {原文: 译名}（纯函数，离线可测）。

    重复的原文以**后出现的为准**（用户在下面写一条更具体的覆盖上面那条，
    与「后写覆盖先写」的直觉一致）。
    """
    out: dict[str, str] = {}
    for _lineno, src, tgt, _reason, _raw in _iter_glossary_lines(text):
        if src:
            out[src] = tgt
    return out


def _glossary_line_issues(text: str) -> list[tuple[int, str]]:
    """格式看不懂的行 `[(行号, 原样内容)]`（纯函数，离线可测）。

    界面拿它给用户一句提示：以前这些行是**静默**丢掉的，用户很容易以为「保存没反应」。
    """
    return [(n, raw) for n, src, _tgt, reason, raw in _iter_glossary_lines(text) if reason]


def _glossary_to_lines(mapping: dict[str, str] | None) -> list[str]:
    """反向：{原文: 译名} → 界面文本框的行（保持配置里的顺序）。"""
    return [f"{k}={v}" for k, v in (mapping or {}).items()]


class TranslationGUI:
    """主界面。headless=True 时不创建 Tk 窗口（给 --self-test / --self-test-dual 用）。"""

    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._q: queue.Queue = queue.Queue()
        self._engines: list[Engine] = []
        self._engine_dirs: list[str] = []      # 与 _engines 一一对应
        # 手腕屏由**界面**持有（不是某个引擎）：手腕上只该有一块屏，内容镜像聊天区，
        # 而聊天区本来就在界面这一层（两个方向的文字都汇到这里）。
        self._overlay_out: Any | None = None
        # 桌面字幕（PC 桌面模式：贴在 VRChat 窗口上的叠加窗）同样由界面持有，
        # 理由与手腕屏一致——内容是聊天区的镜像，而聊天区在界面这一层。
        self._desktop_out: Any | None = None
        self._desktop_dragging = False
        self._desktop_save_job: str | None = None
        # 用户是否**真的动过**透明度滑块：`_save_desktop_cfg()` 也被「锁定位置」和拖动
        # 落盘调到，只有这个标志为真才写 alpha —— 否则用户只是拖了个位置，
        # 滑块上那个（可能是继承来的 / 默认的）值就被写进配置，透明度悄悄变了。
        self._desktop_alpha_touched = False
        self._specs: list[tuple] = []
        self._sinks: set[str] = set()
        self._pending_starts = 0
        self._start_job: str | None = None
        # 停止收尾的完成信号：_stop() 起后台线程等引擎退出，关窗路径靠它做**有界**等待
        self._stop_done_evt = threading.Event()
        self._stop_done_evt.set()                # 初值 = 「当前没有收尾在进行」
        self._preview_busy = False               # 一次只试听一个音色（避免两条音频叠着放）
        self._speech_preview_btn = None
        self._tts_preview_btn = None
        self._bubbles: list[_Bubble] = []
        self._current: dict[str, _Bubble] = {}  # 每个方向一条正在流式刷新的气泡
        self._auto_scroll = True
        self._stats: dict = {}
        self._canvas_w = 600
        self._relayout_job: str | None = None
        self._last_status_level = ""
        # 赞助弹窗（懒建：点开才创建；关掉即销毁，再点重建）
        self._sponsor_win: tk.Toplevel | None = None
        self._sponsor_imgs: list = []          # PhotoImage 必须留引用，否则被 GC 后变空白
        self._sponsor_qr_labels: list = []     # 两张收款码对应的 Label（测试要验证）

        # 更新检查（启动自动查一次 + 设置里手动查；全程守护线程，绝不阻塞翻译主流程）
        self._update_check_done = False        # 启动自动检查：一次会话只查一次
        self._update_check_running = False     # 防连点：进行中再点直接忽略
        self._update_snoozed = False           # 「下次再说」：本会话不再自动弹（不落盘）
        self._update_win: tk.Toplevel | None = None
        self._update_check_job: str | None = None
        # 可注入点：测试换成假检查器/假下载器，全程零网络
        self._update_checker = update_check.check_for_updates
        self._update_downloader = update_check.download_and_verify
        # 下载进度窗（懒建懒销毁；控件引用在窗销毁时一并清空）
        self._dl_win: tk.Toplevel | None = None
        self._dl_bar: ttk.Progressbar | None = None
        self._dl_text: ttk.Label | None = None
        self._dl_note: ttk.Label | None = None
        self._dl_btn_frame: ttk.Frame | None = None
        self._dl_info: update_check.ReleaseInfo | None = None
        self._dl_new_exe: Path | None = None
        self._dl_cancel: threading.Event | None = None
        self._dl_downloading = False
        self._dl_throttle_s = 0.1              # 进度回调节流：至多每 100ms 塞一条队列
        self._dl_last_push = 0.0
        self._dl_reload_btn: ttk.Button | None = None
        self._dl_postpone_btn: ttk.Button | None = None
        self._dl_link: tk.Label | None = None
        # 「稍后更新」/ 启动残留命中：正常退出时替换（绝不自动拉起新版）
        self._update_pending_exit = False
        self._update_pending_info: update_check.ReleaseInfo | None = None
        # 【重载】已安排带拉起的替换：_on_close 别再重复安排退出时替换
        self._reload_started = False
        # 正在退出：_on_close 里置位，之后一律不许再把电平探针拉起来（见 _gate_probe_wanted）
        self._closing = False
        # 「已更新到最新版本」一次性提示（版本变化那一次才弹；版本号只进日志）
        self._updated_hint_win: tk.Toplevel | None = None
        self._updated_hint_job: str | None = None

        # 设置弹窗（分页）：headless 模式下不建 UI，这几个保持空/默认值
        self._settings_win: tk.Toplevel | None = None
        self._settings_nb: ttk.Notebook | None = None
        # 每页一份 (滚动画布, 内容 frame, 滚动条)，顺序 = tab 顺序（滚轮按当前页取用）
        self._settings_pages: list[tuple[tk.Canvas, ttk.Frame, ttk.Scrollbar]] = []
        self._settings_size: tuple[int, int] = (SETTINGS_WIDTH, SETTINGS_MIN_H)

        # 设备选择
        self._mic_names: list[str] = []
        self._loopback_names: list[str] = []
        self._audio_out_names: list[str] = []
        self._device_scan_pending = False

        # 输入门限（只作用于 VRChat 输出 = 「别人说话」那条腿）
        self._gate_level_canvas: tk.Canvas | None = None
        self._gate_level_lbl: ttk.Label | None = None
        self._gate_level_hold = LEVEL_FLOOR_DB     # 峰值保持：读数跳动时靠它平滑
        self._gate_level_tick = 0                  # _poll 节流（50ms → 100ms 刷一次）
        # 独立电平探针：**没在翻译**时的电平来源（有引擎时用引擎的，绝不两路并存）。
        # 只在「设置窗可见 + 勾了启用 + 没有引擎」时活着，见 _sync_gate_level_probe。
        self._gate_probe: LevelProbe | None = None
        self._gate_save_job: str | None = None
        self._gate_hold_ms = 500.0                 # 只从配置读（界面不暴露，避免旋钮过多）
        self._gate_preroll_ms = 250

        self._cfg = load_config(require_key=False)   # 没填 key 也要能起界面（否则没法填 key）
        # 界面语言解析顺序：用户选过（ui.lang）→ 系统语言 → zh。
        # 必须在 _build_ui 之前定下来：之后所有 t() 都按它取词。
        _saved_lang = (self._cfg.ui or {}).get("lang")
        i18n.set_language(_saved_lang if _saved_lang else i18n.detect_system_language())
        mine = self._cfg.directions.get("mine")
        # 语言对：我的语言 A ↔ 对方语言 B。别人说方向自动镜像（B → A）。
        self._lang_pair = {
            "source": mine.source_lang if mine else "zh",
            "target": (mine.target_lang if mine else "en") or "en",
        }

        # 房间文本中继（旁路功能）：由**界面**持有全进程唯一的 RoomClient（与手腕屏同理——
        # 「全进程唯一」的资源只能有一个持有者，而持有者应是能看到所有腿的聚合层）。
        # 默认关：这些对象存在但一行收发都不跑，对现有单机链路零影响。
        self._room: RoomClient | None = None
        self._room_cfg = RoomConfig.from_dict(self._cfg.room)
        self._publisher = SourcePublisher()
        self._room_status_next = 0.0      # 房间行状态文案的刷新节流（monotonic 秒）

        if not headless:
            self._build_ui()

    # ================================================================ UI 构建

    def _build_ui(self) -> None:
        self._root = tk.Tk()
        self._root.title(t("VRChat 实时同传"))
        self._root.geometry("940x600")
        # 下限按第一行实测需求定（含「☕ 赞助」后整行 req=920px）：小于这个宽度
        # Tk 会从最后打包的控件开始裁（实测 860 时目标语言下拉被裁到 40px）。
        self._root.minsize(928, 460)
        self._root.configure(bg=PANEL)
        _apply_ui_font(self._root)   # 必须先于 _apply_theme：字体族要按平台重绑
        self._set_window_icon()      # 标题栏/任务栏图标（失败只留痕，不影响启动）

        self._apply_theme()          # 必须先于任何控件创建
        # 信息架构：主界面只留**高频**操作（开/停、方向、语言、输出、看译文），
        # 低频设置（API key、音频设备）收进「⚙ 设置」弹窗 —— 见 _build_settings_dialog。
        self._build_controls()       # 第一行：开/停 + 方向 + 语言对（会话控制）
        self._build_output_row()     # 第二行：输出勾选 + 手腕屏微调 + key 状态入口
        self._build_room_row()       # 第三行：房间文本中继的连接/断开动作 + 状态（房间码在「设置 → 房间」）
        self._divider()
        self._build_chat()
        self._divider()
        self._build_input_row()      # 打字输入（不想开麦时用键盘替代说话，回车发送）
        self._build_status()
        self._build_settings_dialog()  # 先建好再隐藏：控件属性必须随即可用（测试直接访问）
        self._apply_dark_titlebar()

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_direction_langs()
        self._fit_window_width()     # 文案长度随语言变，窗口宽度得实测一次（俄语比中文宽 300px）
        self._check_api_key()
        self._poll()
        self._start_device_scan()
        # 启动 3 秒后在守护线程里查一次更新（结果经 self._q 回主线程，绝不阻塞启动/翻译）
        self._update_check_job = self._root.after(3000, self._schedule_update_check)
        # 启动兜底：上次下载好了但没来得及换（强杀/关机）→ 复核通过直接给更新入口（不重复下载）
        self._check_pending_update_at_startup()
        # 刚完成过替换/升级 → 一次性「已更新」小提示（同一版本只弹一次）
        self._schedule_version_changed_hint()

    # ================================================================ 主题

    def _fit_window_width(self) -> None:
        """按**当前界面语言**的实际需求定窗口宽度与最小宽度。

        各语言文案长度差得远：第一行（开/停 + 方向 + 语言对 + 赞助/设置）
        中文 959px、俄语 1251px（「Автоопределение」「Направление:」这些词很长）。
        写死 940 时 Tk 会从**最后打包的控件**开始裁 —— 实测中文下目标语言下拉
        已被裁掉 19px，俄语下裁得更多。这里实测一次需求宽度，取「基准 940」
        与「需求 + 余量」的较大者，再夹进屏幕可用宽度（小屏也开不出装不下的窗口）。
        窗口仍然可缩放；这只影响初始尺寸与下限。失败只留痕，不影响启动。
        """
        try:
            self._root.update_idletasks()
            need = self._root.winfo_reqwidth() + 8
            want = max(940, need)
            screen = int(self._root.winfo_screenwidth() or 0)
            if screen:
                # 只留窗口边框的余量：小屏（CI/1024px）也该尽量把内容装下，
                # 别为了「留白」把右侧控件裁掉 —— 裁掉的正是按钮和下拉框。
                want = min(want, max(760, screen - 16))
            height = max(600, self._root.winfo_reqheight())
            self._root.geometry(f"{want}x{height}")
            self._root.minsize(min(want, need), 460)
            if want > 940:
                print(f"[ui] 界面语言 {i18n.current_language()}：文案较宽，窗口按需求开到 "
                      f"{want}px（基准 940 / 需求 {need}）", flush=True)
            if need > want:
                print(f"[ui] ⚠️ 屏幕只有 {screen}px，内容需要 {need}px 装不下，"
                      f"窗口开到 {want}px（右侧可能被裁，可自行拉宽或换更短的语言）",
                      flush=True)
        except Exception as exc:  # noqa: BLE001 — 尺寸算错不该拦住启动
            print(f"[ui] ⚠️ 窗口宽度自适应失败，保持默认：{type(exc).__name__}: {exc}",
                  flush=True)

    def _apply_theme(self) -> None:
        """统一深色主题：深灰 + 蓝。

        ⚠️ Windows 上 ttk 默认主题（vista/xpnative）由系统绘制，
        style.configure(background=...) 会被**静默忽略**——必须切到 clam。
        """
        root = self._root
        style = ttk.Style(root)
        style.theme_use("clam")

        style.configure(".", font=FONT_UI, background=PANEL, foreground=TEXT,
                        bordercolor=BORDER, focuscolor=PANEL)
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=TEXT)
        style.configure("Dim.TLabel", foreground=TEXT_DIM)
        style.configure("Muted.TLabel", foreground=TEXT_DIM, font=FONT_STATUS)
        style.configure("Status.TLabel", font=FONT_STATUS)
        # 分区小标题（设置弹窗里的「API KEY / 音频设备」）：小一号、暗色、加粗
        style.configure("Section.TLabel", foreground=TEXT_DIM,
                        font=FONT_BOLD_SM)
        # API key 状态槽位里的两个控件：**已配置 → 纯展示标签**（「⚙ 设置」是改 key 的入口，
        # 标签不可点）；**未配置 → 可点按钮**，点击用默认浏览器打开千问云开通页（QIANWEN_SIGNUP_URL）。
        style.configure("Chip.TLabel", font=FONT_STATUS, foreground=TEXT_DIM)
        style.configure("ChipWarn.TLabel", font=FONT_STATUS, foreground=COLOR_WARN)
        # 设置弹窗里「保存失败」这类就地提示：警示色，但只是文字（不抢按钮的视觉重量）
        style.configure("Warn.TLabel", font=FONT_STATUS, foreground=COLOR_WARN)
        # 「保存被拒 · 什么都没写」的就地红字：警示橙只表达"注意一下"，而这种情况下
        # 配置**一点没变**（用户以为切了线路、其实还在老线路上）—— 必须比橙更重一档。
        style.configure("Error.TLabel", font=FONT_STATUS, foreground=COLOR_ERROR)
        # 未配置按钮：暗橙底 + 警示橙字，悬停/按下亮一档 —— 警示色系但不刺眼。
        style.configure("ChipWarn.TButton", font=FONT_STATUS, foreground=COLOR_WARN,
                        background="#33291c", borderwidth=0, focusthickness=0,
                        focuscolor="#33291c", padding=(8, 2))
        style.map("ChipWarn.TButton",
                  background=[("pressed", "#453723"), ("active", "#453723")],
                  foreground=[("active", "#e8a85c")])
        # 分割线/分组竖线：用 1px 明度差表达层次，不用 3D 边框
        style.configure("TSeparator", background=BORDER)

        # 按钮：扁平、无边框（clam 的按钮边框会带亮色 bevel，直接不要边框），
        # 悬停/按下有反馈；focuscolor 设成与背景同色，去掉点状焦点框
        style.configure("TButton", background=SURFACE, foreground=TEXT,
                        borderwidth=0, focusthickness=0, focuscolor=PANEL,
                        padding=(12, 7))
        style.map("TButton",
                  background=[("pressed", SURFACE_HOVER), ("active", SURFACE_HOVER),
                              ("disabled", "#20242d")],
                  foreground=[("disabled", TEXT_MUTED)])
        # 主按钮（开始翻译）：蓝色强调
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        borderwidth=0, focusthickness=0, focuscolor=ACCENT,
                        padding=(14, 6))
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_HOVER),
                              ("disabled", "#22374f")],
                  foreground=[("disabled", "#6b87ab")])
        # 反向动作按钮（房间的「断开连接」）：与「连接房间」同形状、**不同颜色** ——
        # 同一个位置在不同连接态下写着相反的动作，只靠文字区分容易点错，
        # 颜色是比文字快得多的提示（用户明确要求「断开用个别的颜色」）。
        style.configure("Danger.TButton", background=DANGER, foreground="#ffffff",
                        borderwidth=0, focusthickness=0, focuscolor=DANGER,
                        padding=(14, 6))
        style.map("Danger.TButton",
                  background=[("pressed", DANGER_ACTIVE), ("active", DANGER_HOVER),
                              ("disabled", "#3a2726")],
                  foreground=[("disabled", "#9c7a78")])

        # API key 输入行：不加这条会沿用 clam 的浅色默认底 —— 深色界面里出现一块白，很扎眼
        # （截图复核时发现的）。字段底/文字/插入符/边框全部对齐 SURFACE/TEXT/BORDER 体系。
        style.configure("Key.TEntry", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, padding=(8, 4))
        style.map("Key.TEntry",
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  fieldbackground=[("disabled", BG), ("readonly", SURFACE)],
                  foreground=[("disabled", TEXT_DIM)])

        # 下拉框：字段、箭头、边框都变深；readonly 下保持深色
        style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=TEXT_DIM, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, insertcolor=TEXT,
                        padding=(8, 4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", SURFACE)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", SURFACE)],   # 去掉选中文字的高亮白块
                  selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  arrowcolor=[("active", TEXT)])
        # 下拉弹出的列表是独立 Listbox，必须单独配色（否则弹出来是白的）
        root.option_add("*TCombobox*Listbox.background", SURFACE)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        root.option_add("*TCombobox*Listbox.font", FONT_UI)

        # 滚动条：细、暗、无箭头，跟聊天区融合
        style.layout("Vertical.TScrollbar",
                     [("Vertical.Scrollbar.trough",
                       {"children": [("Vertical.Scrollbar.thumb",
                                      {"expand": "1", "sticky": "nswe"})],
                        "sticky": "ns"})])
        style.configure("Vertical.TScrollbar", background=SURFACE, troughcolor=BG,
                        bordercolor=BG, darkcolor=BG, lightcolor=BG,
                        arrowcolor=TEXT_DIM, gripcount=0)
        style.map("Vertical.TScrollbar",
                  background=[("pressed", ACCENT_ACTIVE), ("active", SURFACE_HOVER)])

        # 设置弹窗的分页标签（Notebook）：clam 的默认 tab 是浅灰渐变，
        # 深色界面里就是一块亮斑（和「ttk.Entry 默认白底」同一类坑），必须逐状态配色。
        # tabmargins 左侧必须是 **0**：标签条的基准是 Notebook **外框**左边 ——
        # 也就是内容区那条左边框竖线（贯穿整窗、也是用户会拿来对的那条线）。
        # ⚠️ 别把它对齐到「页内分隔线的左端」：那条线本身被页面 20px 内边距缩进过，
        #    拿它当基准会让整排标签比内容区左边框右缩 22px（截图实测过，正是用户说的「没对齐」）。
        style.configure("TNotebook", background=PANEL, bordercolor=BORDER,
                        darkcolor=PANEL, lightcolor=PANEL, tabmargins=(0, 6, 10, 0))
        tab_pad = (16, 7)
        style.configure("TNotebook.Tab", font=FONT_UI, padding=tab_pad,
                        background=PANEL, foreground=TEXT_DIM, bordercolor=BORDER,
                        lightcolor=PANEL, darkcolor=PANEL, focuscolor=PANEL)
        # padding 必须逐状态映射成同一个值：只写 configure 的默认值时，selected/active
        # 会回落成 clam 自己的 tab 布局尺寸，选中标签的外框就比未选中的矮一截。
        # lightcolor/darkcolor/bordercolor 同理——任一状态回落成空值都会画出亮边。
        # 于是选中态**只靠背景色 + 前景色**区分，三态尺寸完全一致。
        # 注意两点实测坑：① ttk 取「第一个匹配的状态规格」，所以默认态必须排在最后；
        # ② 默认态**不能**写成 ("", …)——Tk 8.6 里空规格匹配任意状态，会把 selected 盖掉。
        style.map("TNotebook.Tab",
                  padding=[("selected", tab_pad), ("active", tab_pad),
                           ("!selected !active", tab_pad)],
                  background=[("selected", SURFACE), ("active", SURFACE_HOVER),
                              ("!selected !active", PANEL)],
                  foreground=[("selected", TEXT), ("active", TEXT),
                              ("!selected !active", TEXT_DIM)],
                  lightcolor=[("selected", SURFACE), ("active", SURFACE_HOVER),
                              ("!selected !active", PANEL)],
                  darkcolor=[("selected", SURFACE), ("active", SURFACE_HOVER),
                             ("!selected !active", PANEL)],
                  bordercolor=[("selected", BORDER), ("active", BORDER),
                               ("!selected !active", BORDER)])

    def _set_window_icon(self) -> None:
        """窗口 / 任务栏图标。资源走 bundle_dir()（源码 = 仓库根，打包后 = _MEIPASS）。

        **`.ico` 只在 Windows 上优先**：`iconbitmap` 是 Windows/经典 Tk 的接口，
        Linux 的 Tk 8.6 只接受 `.xbm`，喂 `.ico` 会抛
        `TclError: wrong # args: should be "wm iconbitmap window ?bitmap?"` ——
        每次都刷一行警告。Linux 上直接用 `iconphoto(png)`（跨平台、走现代窗口管理器）。
        整段失败只打一行日志，绝不影响启动。
        """
        assets = BUNDLE_DIR / "assets"
        try:
            if IS_WINDOWS:
                ico = assets / "app.ico"
                if ico.exists():
                    self._root.iconbitmap(default=str(ico))
                    return
            png = assets / "app.png"
            if png.exists():
                self._icon_img = tk.PhotoImage(file=str(png))   # 留引用防 GC
                self._root.iconphoto(True, self._icon_img)
                return
            print(f"[gui] ⚠️ 没找到窗口图标（{assets}），用系统默认图标", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] ⚠️ 设置窗口图标失败（不影响功能）：{type(exc).__name__}: {exc}",
                  flush=True)

    def _apply_dark_titlebar(self, win=None) -> None:
        """Windows 标题栏变深色；老系统不支持就静默跳过（不能因此崩掉）。

        Linux 上直接返回：深色标题栏由桌面环境/主题决定，没有 `dwmapi` 这套东西，
        调 `ctypes.windll` 连属性都不存在。
        """
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            w = win if win is not None else self._root
            w.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(w.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):          # 20 = Win10 20H1+，19 = 更早版本
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    def _divider(self) -> None:
        """1px 深色分割线：用明度差 + 分隔线表达层次，不用 3D 边框。"""
        tk.Frame(self._root, bg=BORDER, height=1, bd=0,
                 highlightthickness=0).pack(fill=tk.X)

    @staticmethod
    def _vsep(parent) -> None:
        """组间竖向分隔线：比留白更明确地表达「这里换了一组」。"""
        ttk.Separator(parent, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y,
                                                       padx=12, pady=5)

    def _attach_edit_menu(self, widget) -> None:
        """给文本输入控件挂右键菜单（剪切/复制/粘贴/全选）。

        Tk 的 Entry/Text 在 Windows 上天生没有右键菜单 —— Ctrl+C/V 能用只是因为 Tk 绑了
        虚拟事件 `<<Copy>>` / `<<Paste>>`，鼠标用户根本没有入口（用户实测报「右键没有复制
        粘贴」）。这里统一走 Tk 的虚拟事件 `<<Cut>>/<<Copy>>/<<Paste>>/<<SelectAll>>`，
        Entry 与 Text 通用，**不自己读写剪贴板**（虚拟事件已经处理了选区/插入点/只读等边界）。
        弹菜单失败只留痕，绝不能让一个右键把界面搞崩。
        """
        try:
            menu = tk.Menu(widget, tearoff=0, bg=SURFACE, foreground=TEXT,
                           activebackground=ACCENT, activeforeground="#ffffff", bd=0)
            for label, action in ((t("剪切"), "<<Cut>>"), (t("复制"), "<<Copy>>"),
                                  (t("粘贴"), "<<Paste>>"), (t("全选"), "<<SelectAll>>")):
                menu.add_command(label=label,
                                 command=lambda a=action: widget.event_generate(a))
            # 存到控件属性上，别让 Tk 把它当垃圾回收掉（局部变量被 GC 后菜单会变空白/点了没反应）
            widget._edit_menu = menu

            def _popup(event) -> None:
                try:
                    widget.focus_set()          # 否则粘贴会落到别的控件上去
                    menu.tk_popup(event.x_root, event.y_root)
                except Exception as exc:  # noqa: BLE001 — 一个右键不该把界面搞崩
                    print(f"[ui] ⚠️ 右键菜单失败：{type(exc).__name__}: {exc}", flush=True)
                finally:
                    menu.grab_release()

            # add="+"：别覆盖控件自己已有的绑定（如「打字:」框的 <Return>/<Escape>）
            widget.bind("<Button-3>", _popup, add="+")
        except Exception as exc:  # noqa: BLE001 — 挂菜单失败也不该拦住建窗
            print(f"[ui] ⚠️ 挂右键菜单失败（不影响输入）：{type(exc).__name__}: {exc}",
                  flush=True)

    def _build_controls(self) -> None:
        """第一行 = 会话控制：开/停 | 方向 | 语言对。VR 里最高频的操作全在这行。"""
        ctrl = ttk.Frame(self._root, padding=(14, 12, 14, 8))
        ctrl.pack(fill=tk.X)

        # 「⚙ 设置」先打包（side=RIGHT）：窗口变窄时 Tk 先挤压后打包的控件，
        # 先占住右侧入口，压缩只会发生在左侧分组之间的留白上。
        # 不写死宽度：英文文案比中文长，定宽会被裁（i18n 实测）。
        self._settings_btn = ttk.Button(ctrl, text=t("⚙ 设置"),
                                        command=self._open_settings)
        self._settings_btn.pack(side=tk.RIGHT)
        # 「☕ 赞助」紧跟其后打包（也在右侧，排在「⚙ 设置」左边）：右侧控件必须
        # 全部先于左侧控件打包，否则空间不足时会被 Tk 从最后打包的开始裁掉。
        self._sponsor_btn = ttk.Button(ctrl, text=t("☕ 赞助"),
                                       command=self._open_sponsor)
        self._sponsor_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self._start_btn = ttk.Button(ctrl, text=t("开始翻译"), style="Accent.TButton",
                                     command=self._start)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._stop_btn = ttk.Button(ctrl, text=t("停止翻译"), command=self._stop,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT)

        self._vsep(ctrl)
        ttk.Label(ctrl, text=t("方向:")).pack(side=tk.LEFT)
        dir_frame = ttk.Frame(ctrl)
        dir_frame.pack(side=tk.LEFT)
        self._direction_var = tk.StringVar(value=str((self._cfg.ui or {}).get("direction", "mine")))
        if self._direction_var.get() not in ("mine", "theirs", "dual"):
            self._direction_var.set("mine")
        # 单选/复选框用经典 tk 控件：ttk 的指示器在 clam 下也吃不准颜色，
        # 经典控件的 bg/fg/selectcolor 一定可控，扁平且与面板融为一体。
        radio_kw = dict(variable=self._direction_var, command=self._on_direction_change,
                        **self._indicator_kw())
        tk.Radiobutton(dir_frame, text=t("我说"), value="mine",
                       **radio_kw).pack(side=tk.LEFT, padx=(8, 0))
        tk.Radiobutton(dir_frame, text=t("别人说"), value="theirs",
                       **radio_kw).pack(side=tk.LEFT, padx=(10, 0))
        tk.Radiobutton(dir_frame, text=t("双向同时"), value="dual",
                       **radio_kw).pack(side=tk.LEFT, padx=(10, 0))

        self._vsep(ctrl)
        # 语言对写成「A → B」：翻译方向一目了然，比「源:/目标:」两个标签省地方
        self._source_combo = ttk.Combobox(ctrl, values=[_lang_label(k) for k in SOURCE_LANGS],
                                           state="readonly",
                                           width=_combo_width([_lang_label(k) for k in SOURCE_LANGS]))
        self._source_combo.pack(side=tk.LEFT)
        self._source_combo.bind("<<ComboboxSelected>>", self._on_lang_change)
        ttk.Label(ctrl, text="→", style="Dim.TLabel").pack(side=tk.LEFT, padx=6)
        self._target_combo = ttk.Combobox(ctrl, values=[_lang_label(k) for k in TARGET_LANGS],
                                           state="readonly",
                                           width=_combo_width([_lang_label(k) for k in TARGET_LANGS]))
        self._target_combo.pack(side=tk.LEFT)
        self._target_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

    def _build_output_row(self) -> None:
        """第二行 = 输出面：译文发到哪。勾选框**单独一行**：和方向/语言挤在同一行时

        整行需要 1062px，而窗口默认只有 920px —— 超出部分会被 Tk 直接裁掉，
        表现就是「某个选项莫名消失」（用户实测看不到「手腕屏」勾选框）。
        """
        out_frame = ttk.Frame(self._root, padding=(14, 0, 14, 10))
        out_frame.pack(fill=tk.X)

        # 右侧：API key 状态槽位（**先 pack 占住右侧**——本行空间不足时 Tk 从最后 pack 的开始裁）。
        # 槽位里两个控件互斥显示：已配置 → 纯展示标签；未配置 → 可点按钮（跳千问云开通页）。
        # 显隐只换**常驻容器**里的孩子：若直接对控件 pack_forget/重 pack，它会被排到本行
        # packing list 末尾，运行时切换后反而成了空间不足时第一个被裁的。
        self._key_slot = ttk.Frame(out_frame)
        self._key_slot.pack(side=tk.RIGHT, padx=(0, 4))
        self._key_chip = ttk.Label(self._key_slot, text="", style="Chip.TLabel")
        self._key_btn = ttk.Button(self._key_slot, text="", style="ChipWarn.TButton",
                                   command=self._open_qianwen_signup)
        # 初始先放标签；随后 _build_settings_dialog() 里的 _refresh_key_status() 会按真实状态切换。
        self._key_chip.pack()

        ttk.Label(out_frame, text=t("输出:"), style="Dim.TLabel").pack(side=tk.LEFT)
        self._chatbox_var = tk.BooleanVar(value=bool((self._cfg.ui or {}).get("chatbox", True)))
        self._overlay_var = tk.BooleanVar(value=bool((self._cfg.ui or {}).get("overlay", False)))
        # 译音输出：把「我说的话」的译音回灌进虚拟声卡，VRChat 里的对方就能听见外语 TTS。
        # 这是**总开关**；config 里 directions.<X>.output_audio 是方向级开关，两者是「与」关系。
        _audio_cfg = (self._cfg.output.get("audio") or {}) if isinstance(self._cfg.output, dict) else {}
        self._vmic_var = tk.BooleanVar(value=bool(_audio_cfg.get("enabled", False)))
        tk.Checkbutton(out_frame, text="chatbox", variable=self._chatbox_var,
                       command=self._save_ui_state,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(6, 0))
        tk.Checkbutton(out_frame, text=t("手腕屏"), variable=self._overlay_var,
                       command=self._on_overlay_toggle,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(10, 0))
        # 微调按钮紧跟「手腕屏」勾选：它是手腕屏的从属工具，放远了看不出归属
        self._tune_btn = ttk.Button(out_frame, text=t("微调 ▸"),
                                    width=_char_width_for(t("微调 ▸"), FONT_UI, 7),
                                    command=self._toggle_tune_panel)
        self._tune_btn.pack(side=tk.LEFT, padx=(4, 0))
        tk.Checkbutton(out_frame, text=t("译音输出"), variable=self._vmic_var,
                       command=self._save_audio_flag,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(10, 0))
        # 桌面字幕（PC 桌面模式，不需要头显）：勾上立刻出一块贴在 VRChat 窗口上的字幕窗
        self._desktop_var = tk.BooleanVar(
            value=bool((self._cfg.ui or {}).get("desktop_overlay", False)))
        tk.Checkbutton(out_frame, text=t("桌面字幕"), variable=self._desktop_var,
                       command=self._on_desktop_toggle,
                       **self._indicator_kw()).pack(side=tk.LEFT, padx=(10, 0))

        # 微调面板：外层常驻（保证位置固定），只切换内层 body 的显隐——
        # 若整块 pack_forget 再 pack，会被排到窗口最底部去。
        self._tune_frame = ttk.Frame(self._root, padding=(14, 0))
        self._tune_frame.pack(fill=tk.X)
        self._tune_body = ttk.Frame(self._tune_frame)
        self._build_tune_body()

    @staticmethod
    def _indicator_kw() -> dict:
        """经典 tk 单选/复选框的统一配色：选中时指示器变蓝，其余深灰。"""
        return dict(bg=PANEL, fg=TEXT, activebackground=PANEL,
                    activeforeground="#ffffff", selectcolor=SURFACE,
                    highlightthickness=0, bd=0, font=FONT_UI)

    # ================================================================ 房间文本中继

    def _build_room_row(self) -> None:
        """独立一行，只留**高频**操作：`[连接房间] 状态：未连接 · 0 人`。

        为什么是按钮不是勾选框：勾选框表达的是「这功能开不开」，而用户在这一行要做的
        是**一个动作** —— 连上去 / 断开；而且这个动作随连接态反过来变（连上之后同一个
        位置就该写着「断开连接」）。文案/可用态由 `_refresh_room_btn` 按连接态刷。
        房间码、昵称是「填一次就不动」的低频输入，搬进「设置 → 房间」页
        （见 `_build_settings_room`）—— 这一行不再有 Checkbutton / Entry。

        ⚠️ 仍然必须**单独一行**：Tk 空间不足时从最后打包的控件开始裁，塞进已经拥挤的
        输出行会让它在小屏上凭空消失（用户实测过的坑）。控件一律左对齐、不给标签写死
        宽度；窗口宽度由 `_fit_window_width` 统一实测。
        """
        row = ttk.Frame(self._root, padding=(14, 0, 14, 10))
        row.pack(fill=tk.X)
        self._room_row = row

        # 连接意图（不再绑任何控件，但与 config 的 room.enabled 同义）
        self._room_var = tk.BooleanVar(value=bool(self._room_cfg.enabled))
        self._room_btn = ttk.Button(row, text=t("连接房间"), style="Accent.TButton",
                                    command=self._on_room_button)
        self._room_btn.pack(side=tk.LEFT)
        self._room_status = ttk.Label(row, text=self._room_status_text(), style="Muted.TLabel")
        self._room_status.pack(side=tk.LEFT, padx=(14, 0))
        self._refresh_room_btn()          # 首屏就把按钮文案/可用态定对

    def _room_status_text(self) -> str:
        """房间行右侧的状态文案：连接态 + 在线人数，全部走 t()（界面禁技术词）。"""
        room = self._room
        if room is None:
            return t("状态：{state} · {n} 人", state=t("未连接"), n=0)
        try:
            st = room.state()
        except Exception:                       # noqa: BLE001  取快照失败就退回「未连接」
            return t("状态：{state} · {n} 人", state=t("未连接"), n=0)
        state_zh = {
            ConnectionState.IDLE: "未连接",
            ConnectionState.CONNECTING: "连接中",
            ConnectionState.ONLINE: "已连接",
            ConnectionState.RECONNECTING: "重连中",
            ConnectionState.STOPPED: "已停止",
            ConnectionState.ERROR: "错误",
        }.get(st.conn, "未连接")
        return t("状态：{state} · {n} 人", state=t(state_zh), n=st.peer_count)

    def _refresh_room_status_label(self) -> None:
        """刷新房间行状态文案（**只在主线程调用**；无头/控件未建时静默跳过）。"""
        if not hasattr(self, "_room_status"):
            return
        try:
            self._room_status.configure(text=self._room_status_text())
        except Exception:                       # noqa: BLE001  文案刷新失败不值得惊动用户
            pass
        self._refresh_room_btn()   # 按钮文案与状态同一口径，跟着一起刷（_poll 已每 0.5s 调这里）

    def _refresh_room_btn(self) -> None:
        """按连接态刷新按钮：未连接→「连接房间」；连接中→「连接中」且禁用；在线/重连→「断开连接」。

        判定口径与 `_room_status_text` 完全一致（`self._room is None` → 未连接；
        `room.state()` 抛异常也按未连接处理）。client 还在但已是 IDLE / STOPPED / ERROR
        时同样给「断开连接」—— 点一下就 `_stop_room()` 回到未连接，比让用户猜怎么复位强。

        颜色也跟着态走：「连接房间」用蓝色强调，「断开连接」用 `Danger.TButton` 的红棕 ——
        同一个位置在不同态下写着相反的动作，颜色是比文字快得多的提示。
        """
        btn = getattr(self, "_room_btn", None)
        if btn is None:
            return                              # 无头模式 / 控件还没建：什么都不做
        st = None
        if self._room is not None:
            try:
                st = self._room.state()
            except Exception:                   # noqa: BLE001  取快照失败就按未连接处理
                st = None
        if st is None:
            text, state, style = t("连接房间"), tk.NORMAL, "Accent.TButton"
        elif st.conn is ConnectionState.CONNECTING:
            text, state, style = t("连接中"), tk.DISABLED, "Accent.TButton"
        else:
            text, state, style = t("断开连接"), tk.NORMAL, "Danger.TButton"
        try:
            btn.configure(text=text, state=state, style=style)
        except Exception:                       # noqa: BLE001  按钮刷新失败不值得惊动用户
            pass

    def _on_room_button(self) -> None:
        """点按钮：已连上/正在连 → 断开；否则先校验房间码再连。"""
        if self._room is not None:
            self._room_var.set(False)
            self._on_room_toggle()
            return
        self._sync_room_cfg_from_fields()
        if not self._room_cfg.room_code:
            # 空房间码：不起连接，明确提示并跳到设置页（禁静默）
            self._set_status("warn", t("先在「设置 → 房间」里填房间码"))
            print("[room] 没填房间码，已取消连接并打开「设置 → 房间」页", flush=True)
            self._open_settings(page="room")
            return
        self._room_var.set(True)
        self._on_room_toggle()

    def _on_room_toggle(self) -> None:
        """按连接意图（`_room_var`）起停 RoomClient，并即时写回 config.yaml。

        语义与「勾选框时代」一致，只是不再被控件直接绑定 —— 唯一入口是
        `_on_room_button`（它负责先把 `_room_var` 设成用户真正想要的值）。
        """
        self._sync_room_cfg_from_fields()
        self._save_room_cfg()
        if self._room_var.get():
            self._start_room()
        else:
            self._stop_room()
        self._refresh_room_status_label()

    def _on_room_field_change(self) -> None:
        """改房间码/昵称：即时写回 config.yaml；房间正开着就重连以套用新值。"""
        was_on = self._room is not None
        self._sync_room_cfg_from_fields()
        self._save_room_cfg()
        if was_on and self._room_var.get():
            # 连接参数在建链时就定死了，换房间码/昵称必须重连才生效
            self._stop_room()
            self._start_room()
        self._refresh_room_status_label()

    def _on_room_generate(self) -> None:
        """生成一个合法房间码填进输入框，并走与手输完全相同的落盘/重连路径。

        生成的是**新建房间**的码：念给对方、对方手输同一个码才能进同一个房间。
        """
        code = new_room_code()
        self._room_code_var.set(code)
        self._on_room_field_change()      # 即时写回 config.yaml；已连接则用新码重连
        print(f"[gui] 已生成随机房间码：{code}", flush=True)

    def _sync_room_cfg_from_fields(self) -> None:
        """把界面上的勾选/房间码/昵称同步进 `self._room_cfg`（只改内存，不落盘）。"""
        code = self._room_cfg.room_code
        nick = self._room_cfg.nickname
        enabled = self._room_cfg.enabled
        if hasattr(self, "_room_code_var"):
            code = (self._room_code_var.get() or "").strip()
        if hasattr(self, "_room_nick_var"):
            nick = (self._room_nick_var.get() or "").strip()
        if hasattr(self, "_room_var"):
            enabled = bool(self._room_var.get())
        self._room_cfg = self._room_cfg.with_overrides(
            enabled=enabled, room_code=code, nickname=nick)

    def _save_room_cfg(self) -> None:
        """把房间三项（enabled/room_code/nickname）就地写回 config.yaml 的 room 段。

        ⚠️ room 段不存在时**补建**：老用户的 config.yaml（旧模板生成）没有这个段，
        而就地改文本「找不到路径就原样返回」→ 不补建就表现为静默不保存。
        """
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            if not re.search(r"^room:", text, re.M):
                text = text.rstrip("\n") + "\n\n" + _ROOM_SECTION_TEMPLATE
            text = _yaml_set_in_text(text, ["room", "enabled"],
                                     _fmt_scalar(bool(self._room_cfg.enabled)))
            text = _yaml_set_in_text(text, ["room", "room_code"],
                                     _yaml_quote(self._room_cfg.room_code))
            text = _yaml_set_in_text(text, ["room", "nickname"],
                                     _yaml_quote(self._room_cfg.nickname))
            _write_config_text(p, text)
            # ⚠️ 上面补建的只是**文件**。内存里的 `self._room_cfg` 还是「配置里根本没有 room 段」
            # 时的默认值（`server_url` 为空）→ 紧接着勾选启用时，`RoomClient` 的启动校验会直接拒：
            #     [room] ❌ 房间链路没启动：没填 server_url（config.yaml 的 room.server_url）
            # 而用户打开 config.yaml 一看，明明有 —— 于是表现为「勾了房间没反应，
            # 重启一次才好」。所以写完把新段回读回来，让**本轮**勾选就能连上。
            self._room_cfg = self._room_cfg_from_text(text)
            print(f"[gui] 房间设置已保存：enabled={_fmt_scalar(bool(self._room_cfg.enabled))} "
                  f"room_code={self._room_cfg.room_code!r} nickname={self._room_cfg.nickname!r}",
                  flush=True)
        except Exception as exc:                # noqa: BLE001  存盘失败只留痕，不影响使用
            print(f"[gui] 保存房间设置失败：{exc}", flush=True)

    def _room_cfg_from_text(self, text: str) -> RoomConfig:
        """从配置**文本**里读 `room:` 段成 RoomConfig（补建段之后立刻回读用）。

        解析失败就沿用内存里的现有设置（宁可用旧设置，也别把用户刚填的选项清掉）。
        """
        try:
            raw = (yaml.safe_load(text) or {}).get("room")
        except Exception as exc:                # noqa: BLE001
            print(f"[gui] 房间段回读失败（沿用内存里的设置）：{exc}", flush=True)
            return self._room_cfg
        if not isinstance(raw, dict):
            return self._room_cfg
        return RoomConfig.from_dict(raw)

    def _start_room(self) -> None:
        """建 RoomClient 并启动（**幂等**）。任何异常只留痕 + 状态栏，绝不影响翻译。"""
        if self._room is not None:
            return
        self._sync_room_cfg_from_fields()
        try:
            self._publisher.reset()             # 起新连接前先封掉半句旧文本
            self._room = RoomClient(self._room_cfg,
                                    on_message=self._on_room_message,
                                    on_status=self._on_room_status)
            self._room.start()
        except Exception as exc:                # noqa: BLE001
            self._room = None
            print(f"[room] ⚠️ 启动失败（翻译不受影响）：{type(exc).__name__}: {exc}", flush=True)
            self._set_status("warn", t("房间出错（翻译不受影响）：{msg}",
                                       msg=f"{type(exc).__name__}: {exc}"))

    def _stop_room(self) -> None:
        """停掉 RoomClient（**幂等、≤5s**）。任何异常只留痕。"""
        room = self._room
        self._room = None
        if room is None:
            return
        try:
            room.stop(timeout=5.0)
        except Exception as exc:                # noqa: BLE001
            print(f"[room] ⚠️ 停止时出错（忽略）：{type(exc).__name__}: {exc}", flush=True)
        finally:
            self._publisher.reset()

    def _on_room_message(self, msg: RoomMessage) -> None:
        """（**房间线程**）收到远端成员的一句话 → 只塞队列，绝不碰 Tk。"""
        try:
            if not self._room_cfg.show_remote:
                return
            self._q.put(("room", msg.nick, msg.peer_id, msg.text, msg.is_final))
        except Exception as exc:                # noqa: BLE001
            print(f"[room] ⚠️ 入队远端消息失败（忽略）：{type(exc).__name__}: {exc}", flush=True)

    def _on_room_status(self, text: str) -> None:
        """（**房间线程**）房间链路状态 → 只塞队列，由 _poll 在主线程落到状态栏。"""
        try:
            self._q.put(("room_status", str(text)))
        except Exception as exc:                # noqa: BLE001
            print(f"[room] ⚠️ 入队房间状态失败（忽略）：{type(exc).__name__}: {exc}", flush=True)

    def _on_engine_text(self, who: str, source_id: str, src_text: str,
                        tgt_text: str, is_final: bool) -> None:
        """引擎每来一条文本：照常进界面队列，再顺带尝试上行到房间（房间没开就零开销）。"""
        self._q.put(("text", who, src_text, tgt_text, is_final))
        self._publish_to_room(source_id, src_text, is_final)

    def _publish_to_room(self, source_id: str, src_text: str, is_final: bool) -> None:
        """把「我自己说的话」的源文切成房间上行帧发出去（loopback 那条腿永不发）。

        铁律：房间链路的任何异常都不能拖垮翻译 / chatbox / 手腕屏 —— 全程包 try，
        出错只在日志留一行。只有 `should_publish` 放行（=mic/pcm 且开关都开）才动发布器。
        """
        room = self._room
        if room is None:
            return                              # 房间没开：一行都不跑
        try:
            if not should_publish(source_id, self._room_cfg.enabled,
                                  self._room_cfg.broadcast_source):
                return
            for item in self._publisher.feed(src_text, is_final):
                room.publish(item.utt, item.rev, item.text, item.is_final)
        except Exception as exc:                # noqa: BLE001
            print(f"[room] ⚠️ 上行发布失败（翻译不受影响）：{type(exc).__name__}: {exc}",
                  flush=True)

    # ---------------------------------------------------------------- 手腕屏微调
    def _toggle_tune_panel(self) -> None:
        """展开/收起微调面板（只切内层 body，外层常驻以固定位置）。

        展开时同步把窗口加高：否则聊天区被面板挤扁（实测 434px → 329px），
        而调参时正需要一边看译文一边拖。
        """
        win_h = self._root.winfo_height()
        win_w = self._root.winfo_width()
        if self._tune_body.winfo_ismapped():
            self._tune_body.pack_forget()
            # ⚠️ 只 pack_forget 不够：Tk 不会重算空 frame 的高度（实测 reqheight 仍停在 106），
            # 那 106px 会一直占着把聊天区压扁。必须显式关掉传播并把高度压到 0。
            self._tune_frame.pack_propagate(False)
            self._tune_frame.configure(height=1)
            self._tune_btn.configure(text=t("微调 ▸"))
            new_h = max(self._root.minsize()[1], win_h - getattr(self, "_tune_added_h", 0))
        else:
            self._tune_frame.pack_propagate(True)
            self._tune_body.pack(fill=tk.X)
            self._tune_btn.configure(text=t("微调 ▾"))
            self._root.update_idletasks()
            self._tune_added_h = self._tune_body.winfo_reqheight() + 8
            new_h = win_h + self._tune_added_h
        self._root.geometry(f"{win_w}x{new_h}")

    def _build_tune_body(self) -> None:
        """手腕屏微调：滑块改动 → 写回 config.yaml → overlay 配置热重载（无需重启）。

        面板做成**可收起**的（默认收起）：它是调试期工具，平时不该占屏幕，
        但参数全都要能拖——位置/旋转/大小/弯曲/透明度 + 锚点。
        """
        ov = self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}
        off = ov.get("offset") or {}
        _sz = list(ov.get("size_px") or [1024, 440])
        self._tune_panel_w = int(_sz[0])
        # 位姿按**当前锚点**取（每个锚点各存一套；口径与两端后端共用 resolve_offset）
        self._anchor_label_to_key = {t("右手"): "right_hand", t("左手"): "left_hand",
                                     t("外部 tracker"): "tracker", t("头显前固定"): "hmd"}
        _key_to_label = {v: k for k, v in self._anchor_label_to_key.items()}
        pos, rot = resolve_offset(ov, str(ov.get("anchor", "right_hand")))
        pos, rot = list(pos), list(rot)
        self._tune_values: dict[str, float] = {
            "pos_x": float(pos[0]), "pos_y": float(pos[1]), "pos_z": float(pos[2]),
            "rot_x": float(rot[0]), "rot_y": float(rot[1]), "rot_z": float(rot[2]),
            # ⚠️ 下面这些 **`ov.get(键, 兜底)` 的兜底值必须等于 `OverlayConfig` 的默认值**
            #    （由 tests/test_tune_rot_range.py 的 test_tune_fallbacks_match_config_defaults
            #    钉住）。兜底只在 config.yaml **缺键**时才生效，真机上很难发现 —— 之前这里
            #    是 width_m=0.24 / font_size=42 / source_font_size=30，而默认值是
            #    0.23 / 36 / 29：用户手写配置少写两行，滑块就显示 42/30，
            #    **碰一下还会把 42/30 写回配置**，字号悄悄变了。
            "width_m": float(off.get("width_m", 0.23)),
            "curvature": float(off.get("curvature", 0.0)),
            "alpha": float(off.get("alpha", 0.9)),
            # 字号 / 面板高度决定「一块屏能显示多少字」——用户明确要能自己调
            "font_size": float(ov.get("font_size", 36)),
            "source_font_size": float(ov.get("source_font_size", 29)),
            "panel_h": float(_sz[1]),
            # 半透明程度（0-255）：底板与原文分开调 —— 底板要透、文字要实心，
            # 是两件事（实测用户就是这么要求的：界面半透明、文字不透明）
            "bg_alpha": float(ov.get("bg_alpha", 205)),
            "source_alpha": float(ov.get("source_alpha", 205)),
        }
        self._ov_save_job: str | None = None

        row = ttk.Frame(self._tune_body)
        row.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(row, text=t("锚点:"), font=FONT_UI).pack(side=tk.LEFT)
        self._anchor_combo = ttk.Combobox(row, values=list(self._anchor_label_to_key),
                                          state="readonly", font=FONT_UI,
                                          width=_combo_width(self._anchor_label_to_key, 12))
        self._anchor_combo.set(_key_to_label.get(str(ov.get("anchor", "right_hand")), t("右手")))
        self._anchor_combo.pack(side=tk.LEFT, padx=(4, 14))
        self._anchor_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_anchor_change())
        ttk.Label(row, text=t("tracker 序号:"), font=FONT_UI).pack(side=tk.LEFT)
        self._tracker_var = tk.StringVar(value=str(ov.get("tracker_index", 0)))
        # 上限 7 = Linux role 表有 8 项（0=右腕 … 7=左脚）；Windows 侧按「第 N 个已配对的
        # GenericTracker」，序号大一点也无妨。原来卡在 0–3，Linux 上胸/腰/脚那几项
        # 从界面根本够不到。
        ttk.Spinbox(row, from_=0, to=7, width=3, font=FONT_UI, textvariable=self._tracker_var,
                    command=self._save_overlay_cfg).pack(side=tk.LEFT, padx=(4, 0))
        # 「外部 tracker」= 挂到第 N 个通用 tracker（绑前臂/手腕只是为了让挂点离手腕近），
        # 跟「前臂」没有绑定关系 —— 老文案写「前臂 tracker」会让人以为得是专用设备。
        # 提示文案保持短：这一行左边还有锚点下拉和序号框，拉太长会把整行撑出窗口。
        ttk.Label(row, text=t("（仅锚点=外部 tracker 时有效）"),
                  font=FONT_STATUS, foreground=TEXT_MUTED).pack(side=tk.LEFT, padx=(10, 0))

        grid = ttk.Frame(self._tune_body)
        self._tune_grid = grid      # 供测试按控件树定位 specs 那批滑块（本面板还挂着桌面字幕的滑块）
        grid.pack(fill=tk.X, pady=(2, 2))
        specs = [
            ("pos_x", t("位置X"), -0.30, 0.30, 0.005, "m"),
            ("pos_y", t("位置Y"), -0.30, 0.30, 0.005, "m"),
            ("pos_z", t("位置Z"), -0.30, 0.30, 0.005, "m"),
            # 旋转三轴定义域是整圈 ±180°：卡到 ±90° 拖不到反戴/侧戴等姿态，还会把手写的 180 夹回 90
            ("rot_x", t("俯仰X"), -180.0, 180.0, 1.0, "°"),
            ("rot_y", t("偏航Y"), -180.0, 180.0, 1.0, "°"),
            ("rot_z", t("翻滚Z"), -180.0, 180.0, 1.0, "°"),
            ("width_m", t("大小"), 0.05, 0.80, 0.01, "m"),
            ("curvature", t("弯曲"), 0.0, 0.50, 0.01, ""),
            ("alpha", t("透明度"), 0.10, 1.00, 0.05, ""),
            # 显示多少字由这三个决定：字号调小 → 同样高度塞更多字；面板调高 → 多一轮对话
            ("font_size", t("译文字号"), 20, 64, 1, ""),
            ("source_font_size", t("原文字号"), 14, 48, 1, ""),
            ("panel_h", t("面板高"), 240, 560, 10, "px"),
            # 0-255 的 8 位 alpha：底板（界面）/ 原文分开 —— 想让文字更实就拉满 255
            ("bg_alpha", t("底板不透明度"), 0, 255, 5, ""),
            ("source_alpha", t("原文不透明度"), 0, 255, 5, ""),
        ]
        # 标签宽度按**当前语言**最长的那条算：中文是 3-4 字（width=6 够），
        # 但英语 "Curvature"、俄语 "Размер оригинала" 会被 6 字宽截断 ——
        # 俄语下「Позиция X/Y/Z」全挤成「Позиц.」，三个位置参数根本分不出来（实测）。
        # 标签长到放不下三列时自动降成两列，宁可面板高一点，也不裁字。
        label_w = max(6, max(_char_width_for(spec[1], FONT_UI) for spec in specs))
        cols = 3 if label_w <= 9 else 2
        # 切锚点要把该锚点那一份位姿**回填到滑块**上（见 _load_anchor_offset），
        # 所以 var / 值标签都得留个引用。
        self._tune_vars: dict[str, tk.DoubleVar] = {}
        self._tune_lbls: dict[str, ttk.Label] = {}
        self._tune_units: dict[str, str] = {}
        for i, (key, label, lo, hi, res, unit) in enumerate(specs):
            row_i, col_i = divmod(i, cols)
            cell = ttk.Frame(grid)
            cell.grid(row=row_i, column=col_i, sticky="w", padx=(0, 18), pady=1)
            ttk.Label(cell, text=label, font=FONT_UI, width=label_w).pack(side=tk.LEFT)
            var = tk.DoubleVar(value=self._tune_values[key])
            val_lbl = ttk.Label(cell, text=f"{self._tune_values[key]:g}{unit}",
                                font=FONT_STATUS, foreground=TEXT_DIM, width=7)
            tk.Scale(cell, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                     variable=var, showvalue=False, length=104, width=10,
                     bg=PANEL, fg=TEXT, troughcolor=SURFACE, activebackground=ACCENT,
                     highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
                     command=self._make_tune_handler(key, var, val_lbl, unit)).pack(side=tk.LEFT, padx=(4, 6))
            val_lbl.pack(side=tk.LEFT)
            self._tune_vars[key] = var
            self._tune_lbls[key] = val_lbl
            self._tune_units[key] = unit

        # 桌面字幕（PC 桌面模式）：只需要「透明度 + 拖动解锁」两件，与上面那堆 VR 参数
        # 无关；放在同一块「微调」里，用户不用记两处入口。
        #
        # ⚠️ 滑块初值必须与**窗口真实透明度同源**：窗口那侧是
        #    `DesktopOverlayConfig.from_dict(desktop_overlay 段, overlay 段)` 解析出来的
        #    （desktop_overlay.alpha → overlay.alpha → 默认值）。只读本段的 alpha 会让
        #    「overlay.alpha=0.5 且没写 desktop_overlay.alpha」的用户看到滑块停在 0.90、
        #    而窗口其实是 0.50 —— 更糟的是碰一下滑块就把 0.90 写回配置（透明度突然变了）。
        from .output.desktop_overlay import DesktopOverlayConfig
        _dcfg = DesktopOverlayConfig.from_dict(self._desktop_cfg(),
                                               visual=self._cfg.overlay or {})
        drow = ttk.Frame(self._tune_body)
        drow.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(drow, text=t("桌面字幕"), font=FONT_UI).pack(side=tk.LEFT)
        self._desktop_alpha_var = tk.DoubleVar(value=float(_dcfg.alpha))
        self._desktop_alpha_lbl = ttk.Label(drow, text=f"{self._desktop_alpha_var.get():.2f}",
                                            font=FONT_STATUS, foreground=TEXT_DIM, width=5)
        tk.Scale(drow, from_=0.20, to=1.00, resolution=0.05, orient=tk.HORIZONTAL,
                 variable=self._desktop_alpha_var, showvalue=False, length=104, width=10,
                 bg=PANEL, fg=TEXT, troughcolor=SURFACE, activebackground=ACCENT,
                 highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
                 command=self._on_desktop_alpha).pack(side=tk.LEFT, padx=(4, 6))
        self._desktop_alpha_lbl.pack(side=tk.LEFT)
        # 按钮文案会在「解锁拖动 / 锁定位置」之间切，宽度按两者里更长的算，免得不换语言也裁字
        self._desktop_drag_btn = ttk.Button(
            drow, text=t("解锁拖动"),
            width=max(_char_width_for(t("解锁拖动"), FONT_UI, 6),
                      _char_width_for(t("锁定位置"), FONT_UI, 6)),
            command=self._toggle_desktop_drag)
        self._desktop_drag_btn.pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(drow, text=t("（字幕窗默认可穿透，先解锁再拖）"), font=FONT_STATUS,
                  foreground=TEXT_MUTED).pack(side=tk.LEFT, padx=(8, 0))

    def _current_anchor(self) -> str:
        """下拉当前选中的锚点键（right_hand / left_hand / tracker / hmd）。"""
        return self._anchor_label_to_key.get(self._anchor_combo.get(), "right_hand")

    def _load_anchor_offset(self, anchor: str) -> None:
        """把**该锚点那一份**位姿回填到滑块上（切锚点时必须做，否则滑块显示的
        是上一个锚点的值，随手拖一下就把那份值写到新锚点头上了）。

        取不到就按 `resolve_offset` 的兜底链（`offset` → 内置默认）走 —— 与后端
        真正用的那份值完全同源，界面显示的和面板的位置不会对不上。
        """
        ov = self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}
        pos, rot = resolve_offset(ov, anchor)
        pairs = list(zip(("pos_x", "pos_y", "pos_z"), pos)) + \
            list(zip(("rot_x", "rot_y", "rot_z"), rot))
        for key, val in pairs:
            v = float(val)
            self._tune_values[key] = v
            self._tune_vars[key].set(v)
            self._tune_lbls[key].configure(text=f"{v:g}{self._tune_units[key]}")
    def _make_tune_handler(self, key: str, var, lbl, unit: str):  # noqa: ANN001
        def _on_move(_v: str) -> None:
            self._tune_values[key] = round(float(var.get()), 4)
            lbl.configure(text=f"{self._tune_values[key]:g}{unit}")
            self._schedule_overlay_save()
        return _on_move

    def _on_anchor_change(self) -> None:
        """换锚点：先把**新锚点自己那一份**位姿回填到滑块，再落盘。

        ⚠️ 顺序不能反：先落盘的话，写进去的是**上一个锚点**的位姿（滑块还没换过来），
        等于换一次锚点就毁一份配置。
        """
        self._load_anchor_offset(self._current_anchor())
        self._save_overlay_cfg()

    def _schedule_overlay_save(self) -> None:
        """拖动时不要每像素写盘：延后 200ms，停手才落盘。"""
        if self._ov_save_job is not None:
            try:
                self._root.after_cancel(self._ov_save_job)
            except Exception:
                pass
        self._ov_save_job = self._root.after(200, self._save_overlay_cfg)

    def _save_overlay_cfg(self) -> None:
        """把微调面板的值写回 config.yaml；overlay 侧有热重载，改完立刻生效。

        用就地改文本的方式（`_yaml_set_in_text` / `_yaml_set_or_create`），**不整文件重写**，
        否则拖动一次滑块就会把配置里的注释和键顺序全抹掉。

        ⚠️ 位姿写到 `overlay.offsets.<当前锚点>`（每个锚点各存一套），**不是**
        `overlay.offset` —— 后者退化为「没单独存过的锚点」的兜底。写错地方就等于
        切一次锚点覆盖一份位姿（用户实测的「没法设置成左手」）。
        """
        self._ov_save_job = None
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            v = self._tune_values
            anchor = self._current_anchor()
            try:
                tracker = int(self._tracker_var.get())
            except (TypeError, ValueError):
                tracker = 0
            pos_s = f"[{_fmt_scalar(v['pos_x'])}, {_fmt_scalar(v['pos_y'])}, {_fmt_scalar(v['pos_z'])}]"
            rot_s = f"[{_fmt_scalar(v['rot_x'])}, {_fmt_scalar(v['rot_y'])}, {_fmt_scalar(v['rot_z'])}]"
            updates: list[tuple[list[str], str]] = [
                (["overlay", "anchor"], anchor),
                (["overlay", "tracker_index"], str(tracker)),
                (["overlay", "offset", "width_m"], _fmt_scalar(v["width_m"])),
                (["overlay", "offset", "curvature"], _fmt_scalar(v["curvature"])),
                (["overlay", "offset", "alpha"], _fmt_scalar(v["alpha"])),
                # 字号 / 面板像素高度（改这些会触发 overlay 重新渲染一帧）
                (["overlay", "font_size"], _fmt_scalar(v["font_size"])),
                (["overlay", "source_font_size"], _fmt_scalar(v["source_font_size"])),
                (["overlay", "size_px"], f"[{self._tune_panel_w}, {_fmt_scalar(v['panel_h'])}]"),
                # 底板 / 原文的 8 位 alpha（同样会触发重渲一帧）
                (["overlay", "bg_alpha"], str(int(v["bg_alpha"]))),
                (["overlay", "source_alpha"], str(int(v["source_alpha"]))),
            ]
            for key_path, val in updates:
                text = _yaml_set_in_text(text, key_path, val)
            # 位姿按锚点分开存；老配置里整段没有 `offsets:` → 用 or_create 补建整条链
            # （`_yaml_set_in_text` 找不到父键时会静默不改，设置就永远存不下去）
            for key_path, val in ((["overlay", "offsets", anchor, "pos"], pos_s),
                                  (["overlay", "offsets", anchor, "rot"], rot_s)):
                text = _yaml_set_or_create(text, key_path, val)
            _write_config_text(p, text)
            # ⚠️ 内存里的 cfg 必须同步：不同步的话，切到别的锚点再切回来时
            #    `_load_anchor_offset` 读到的还是**启动时**那份配置 —— 刚调好的值会被
            #    旧值覆盖，界面上表现为「调了半天，切一下就白调」。
            ov = self._cfg.overlay
            if isinstance(ov, dict):
                ov["anchor"] = anchor
                ov["offsets"] = {**(ov.get("offsets") or {}),
                                 anchor: {"pos": [v["pos_x"], v["pos_y"], v["pos_z"]],
                                          "rot": [v["rot_x"], v["rot_y"], v["rot_z"]]}}
            print(f"[gui] 手腕屏参数已写入 config.yaml：anchor={anchor} "
                  f"offsets.{anchor} pos={pos_s} rot={rot_s} "
                  f"width={_fmt_scalar(v['width_m'])}m "
                  f"curvature={_fmt_scalar(v['curvature'])} alpha={_fmt_scalar(v['alpha'])} "
                  f"字号={_fmt_scalar(v['font_size'])}/{_fmt_scalar(v['source_font_size'])} "
                  f"面板=[{self._tune_panel_w}, {_fmt_scalar(v['panel_h'])}] "
                  f"底板/原文 alpha={int(v['bg_alpha'])}/{int(v['source_alpha'])}"
                  f"（overlay 会热重载，无需重启）",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存手腕屏参数失败：{exc}", flush=True)

    # ---------------------------------------------------------------- 设置弹窗（低频设置）
    def _build_settings_dialog(self) -> None:
        """低频设置收进弹窗：**分页**（常规 / 音频 / 词库 / 房间 / 关于）+ 固定尺寸 + 每页可滚。

        为什么不在主界面：这些是「装好一次、几乎不动」的设置，常驻只会让主界面变成
        4 行控件堆叠（改造前的样子）。

        为什么必须分页（改造前的实测病）：8 个分区一竖列堆下来弹窗高 **1485px**，
        从 y=352 起 ⇒ 底边落到 1837，2560×1600 的屏都装不下 —— 「日志」「软件更新」
        两区整个在屏幕外；而弹窗 `resizable(False, False)` 且没有滚动条，
        所以不是「难找」，是**真的够不着**（1080p 屏只会更糟）。分页后每页最高约 400px，
        整窗按内容实测 + 两道上限（SETTINGS_MAX_H / 屏高-90）定高，真装不下时页面能滚。

        弹窗**先建好再 withdraw**，且各页的控件**一次性全建齐**（不做「切到那页才建」的
        懒加载）：控件属性（_key_entry / _mic_combo / _glossary_text / _room_code_var …）
        必须在弹窗不可见时也随即可用 —— 设备扫描、更新检查回填和自动化测试都直接访问它们。
        """
        win = tk.Toplevel(self._root)
        win.title(t("设置"))
        win.configure(bg=PANEL)
        win.transient(self._root)
        win.resizable(False, False)
        win.withdraw()
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        win.bind("<Escape>", lambda _e: self._close_settings())
        # 滚轮绑在弹窗上（toplevel 是页里每个控件的 bindtag）：指针停在页内哪儿都能滚当前页
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, self._on_settings_wheel)
        self._settings_win = win
        self._apply_dark_titlebar(win)

        nb = ttk.Notebook(win, style="TNotebook")
        nb.pack(fill=tk.BOTH, expand=True)
        self._settings_nb = nb

        # 标题 → tab 本体：`_settings_page` 只返回**内容 frame**，而 `nb.select()` 要的是
        # tab，所以在这里登记一份（_open_settings(page=…) 靠它直接跳到某一页）
        self._settings_tabs: dict[str, ttk.Frame] = {}

        # 分页口径 = 「我要改什么」→ 去哪页：
        #   常规 = 填 key / 换界面语言；音频 = 声音的进出（设备 · 门限 · 音色）；
        #   词库 = 专有名词怎么译；房间 = 多人互看字幕的房间码/昵称；
        #   关于 = 版本与日志（出问题时给维护者的东西）
        self._build_settings_general(self._settings_page(nb, t("常规")))
        self._build_settings_audio(self._settings_page(nb, t("音频")))
        self._build_settings_glossary(self._settings_page(nb, t("词库")))
        self._build_settings_room(self._settings_page(nb, t("房间")))
        self._build_settings_about(self._settings_page(nb, t("关于")))
        self._size_settings_window()

    def _settings_page(self, nb: ttk.Notebook, title: str) -> ttk.Frame:
        """给 Notebook 加一页，返回该页**放控件的内容 frame**（外面套一层可滚动画布）。

        每页都套滚动是兜底、不是常态：窗口高度是固定的（保证整窗在屏幕内），
        而文案长度随界面语言变（俄语普遍更长）—— 装不下时必须能滚，
        绝不能让 Tk 把内容裁掉。滚动条只在**真装不下**时出现（_sync_page_scrollbar）。
        """
        tab = ttk.Frame(nb)
        nb.add(tab, text=title)
        self._settings_tabs[title] = tab    # 只返回 inner，tab 本体在这里登记（供 select）
        canvas = tk.Canvas(tab, bg=PANEL, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(tab, orient=tk.VERTICAL, style="Vertical.TScrollbar",
                           command=canvas.yview)
        inner = ttk.Frame(canvas, padding=(TAB_INSET_X, 16, TAB_INSET_X, 16))
        canvas.configure(yscrollcommand=sb.set)
        slot = canvas.create_window((0, 0), window=inner, anchor="nw")
        # 只打包可伸长的 canvas；滚动条要出现时用 before=canvas 插到它左边（右侧）——
        # 打包顺序恒为「滚动条先、canvas 后」，空间不够时被压缩的才是 canvas，
        # 反过来滚动条会被挤成 1px（按钮行上踩过同一个坑）。
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._settings_pages.append((canvas, inner, sb))

        def _on_inner(_e=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all") or (0, 0, 1, 1))
            self._sync_page_scrollbar(canvas, inner, sb)

        def _on_canvas(e) -> None:
            # 内容宽 = 画布宽：fill=X 的控件（输入框/词库框）才撑得开，也不会横向溢出
            canvas.itemconfigure(slot, width=e.width)
            self._sync_page_scrollbar(canvas, inner, sb)

        inner.bind("<Configure>", _on_inner)
        canvas.bind("<Configure>", _on_canvas)
        return inner

    def _sync_page_scrollbar(self, canvas: tk.Canvas, inner: ttk.Frame,
                             sb: ttk.Scrollbar) -> None:
        """内容比可视高度高才显示滚动条；装得下就收起来（多数语言根本用不上）。"""
        have = canvas.winfo_height()
        if have <= 1:
            return          # 弹窗还 withdraw 着、没布局过：等 <Configure> 或打开时再判
        if inner.winfo_reqheight() > have + 1:
            if sb.winfo_manager() != "pack":
                sb.pack(side=tk.RIGHT, fill=tk.Y, before=canvas)
        elif sb.winfo_manager() == "pack":
            sb.pack_forget()

    def _sync_settings_pages(self) -> None:
        """弹窗显示出来后补一次滚动条判定：建窗时它 withdraw 着，那时量不到可视高度。"""
        try:
            self._settings_win.update_idletasks()
            for canvas, inner, sb in self._settings_pages:
                self._sync_page_scrollbar(canvas, inner, sb)
        except Exception as exc:  # noqa: BLE001 — 滚动条判错不该影响弹窗能用
            print(f"[ui] ⚠️ 设置弹窗滚动条同步失败（不影响使用）："
                  f"{type(exc).__name__}: {exc}", flush=True)

    def _on_settings_wheel(self, event) -> None:
        """滚轮滚**当前那一页**；指针停在自带滚动的控件上（词库 tk.Text）时不抢。

        Tk 的 bindtag 顺序是「控件 → 类 → 顶层 → all」，而 tk.Text 的类绑定滚完自己
        并不 return break，所以这里必须显式跳过它，否则一次滚轮两边一起滚。
        """
        try:
            w = getattr(event, "widget", None)
            if w is not None and w.winfo_class() in ("Text", "Listbox"):
                return
            nb = self._settings_nb
            canvas = self._settings_pages[nb.index(nb.select())][0]
        except Exception:  # noqa: BLE001 — 弹窗没建好/已销毁，滚轮直接忽略
            return
        delta, num = getattr(event, "delta", 0), getattr(event, "num", 0)
        if delta:
            step = -1 if delta > 0 else 1
        elif num == 4:                            # X11 没有 MouseWheel，只有 Button-4/5
            step = -1
        elif num == 5:
            step = 1
        else:
            return
        canvas.yview_scroll(step * 3, "units")

    def _size_settings_window(self) -> None:
        """按当前语言实测各页需求高度，定弹窗的固定尺寸（宽固定、高按内容 + 两道上限）。

        上限两道：SETTINGS_MAX_H（1080p 屏也要整窗可见）与「屏高 - 90」（更小的屏优先保命，
        标题栏/任务栏也要占地方）。真装不下的部分由页面滚动兜底，不再靠「窗口被裁」糊过去。
        """
        win = self._settings_win
        try:
            win.update_idletasks()
            need = max((inner.winfo_reqheight() for _c, inner, _s in self._settings_pages),
                       default=0)
            screen_h = int(win.winfo_screenheight() or 0)
            cap = min(SETTINGS_MAX_H, screen_h - 90) if screen_h else SETTINGS_MAX_H
            h = max(SETTINGS_MIN_H, min(need + SETTINGS_CHROME_H, cap))
            self._settings_size = (SETTINGS_WIDTH, h)
            win.geometry(f"{SETTINGS_WIDTH}x{h}")
            print(f"[ui] 设置弹窗 {SETTINGS_WIDTH}x{h}"
                  f"（最高一页需 {need}px，屏高 {screen_h}px）", flush=True)
        except Exception as exc:  # noqa: BLE001 — 尺寸算错不该拦住启动
            self._settings_size = (SETTINGS_WIDTH, SETTINGS_MAX_H)
            win.geometry(f"{SETTINGS_WIDTH}x{SETTINGS_MAX_H}")
            print(f"[ui] ⚠️ 设置弹窗尺寸自适应失败，按上限值开窗："
                  f"{type(exc).__name__}: {exc}", flush=True)

    # ---------------------------------------------------------------- 设置弹窗 · 常规页
    def _build_settings_general(self, body: ttk.Frame) -> None:
        """「常规」页：API key（新用户第一件事，放最前）→ 服务线路 → 界面语言。

        线路排在 key 紧后面：它决定「连哪个域名 + 用哪一把 key」，和上面那块本来就是
        同一件事的两半 —— 挨着放，用户才不会"填了海外的 key 却还连着国内"。
        """
        # ---- API Key ----
        # 安全约束（与 vlt/credentials.py 一致）：
        # - 输入框用 ● 掩码；保存成功后**立刻清空输入框**，明文不留在界面上；
        # - 状态只出现打码形式（sk-xx-****yyyy）；明文不进日志、不进 config.yaml。
        ttk.Label(body, text="API KEY", style="Section.TLabel").pack(anchor=tk.W)
        key_row = ttk.Frame(body)
        key_row.pack(fill=tk.X, pady=(8, 4))
        # 先占右侧，空间不足时才不会把按钮挤没
        # 不写死宽度：英文文案比中文长，定宽会被裁（i18n 实测）
        self._key_clear_btn = ttk.Button(key_row, text=t("清除"),
                                         command=self._on_clear_key)
        self._key_clear_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self._key_save_btn = ttk.Button(key_row, text=t("保存"),
                                        command=self._on_save_key)
        self._key_save_btn.pack(side=tk.RIGHT)
        self._key_var = tk.StringVar()
        self._key_entry = ttk.Entry(key_row, textvariable=self._key_var, show="●",
                                    width=34, style="Key.TEntry")
        self._key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        self._key_entry.bind("<Return>", lambda _e: self._on_save_key())
        self._attach_edit_menu(self._key_entry)      # 右键剪切/复制/粘贴（key 只能整串粘贴，最需要它）
        self._key_status = ttk.Label(body, text="", style="Dim.TLabel",
                                     justify=tk.LEFT, wraplength=SETTINGS_WRAP)
        self._key_status.pack(anchor=tk.W)
        self._refresh_key_status()

        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)

        # ---- 服务线路（千问云 / 千问云·海外版，互斥）----
        self._build_provider_section(body)

        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)

        # ---- 界面语言 ----
        # 本批只做到「重启后生效」：控件文案全在建窗时按当前语言取词，
        # 运行中换语言不重建树（正在进行的翻译/设备列表状态绝不受影响）。
        ttk.Label(body, text=t("界面语言"), style="Section.TLabel").pack(anchor=tk.W)
        lang_row = ttk.Frame(body)
        lang_row.pack(fill=tk.X, pady=(8, 4))
        self._ui_lang_names = dict(i18n.available_languages())   # code → 母语名称
        self._ui_lang_var = tk.StringVar(
            value=self._ui_lang_names.get(i18n.current_language(), "简体中文"))
        self._ui_lang_combo = ttk.Combobox(
            lang_row, values=list(self._ui_lang_names.values()),
            state="readonly", width=14, textvariable=self._ui_lang_var)
        self._ui_lang_combo.pack(side=tk.LEFT)
        self._ui_lang_combo.bind("<<ComboboxSelected>>", self._on_ui_lang_change)
        self._ui_lang_note = ttk.Label(body, text=t("界面语言在重启程序后生效"),
                                       style="Muted.TLabel", justify=tk.LEFT,
                                       wraplength=SETTINGS_WRAP)
        self._ui_lang_note.pack(anchor=tk.W)

        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)

        # ---- VRChat OSC 端口 ----
        # 默认 9000 是 **VRChat 那边**的 OSC 接收端口（我们往它发 /chatbox/input）。
        # 用户在 VRChat 里改过端口、或中间挂了 OSC 转发工具（VRCT 之类）时，不跟着改就
        # 一条气泡都发不出去 —— 而症状只是「chatbox 没反应」，界面上看不出是端口不对。
        # 「VRChat OSC」是专有名词，不进词表（见仓库约定：专有名词不翻译）。
        ttk.Label(body, text="VRChat OSC", style="Section.TLabel").pack(anchor=tk.W)
        osc_row = ttk.Frame(body)
        osc_row.pack(fill=tk.X, pady=(8, 4))
        # 按钮先占右侧：空间不足时被压的才是输入区（与 key 行同一口径）
        self._osc_save_btn = ttk.Button(osc_row, text=t("保存端口"),
                                        command=self._on_save_osc_port)
        self._osc_save_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Label(osc_row, text=t("端口:"), style="Dim.TLabel").pack(side=tk.LEFT)
        self._osc_port_var = tk.StringVar(value=str(self._chatbox_port()))
        self._osc_port_entry = ttk.Entry(osc_row, textvariable=self._osc_port_var,
                                         width=8, style="Key.TEntry")
        self._osc_port_entry.pack(side=tk.LEFT, padx=(8, 0))
        self._osc_port_entry.bind("<Return>", lambda _e: self._on_save_osc_port())
        self._attach_edit_menu(self._osc_port_entry)
        ttk.Label(body,
                  text=t("VRChat 默认收 9000 端口；只有你在 VRChat 里改过 OSC 端口"
                         "（或中间挂了转发工具）时才需要动这里"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W)
        # 就地红字（与线路页同一口径）：保存被拒时必须在这一屏看得见
        self._osc_err = ttk.Label(body, text="", style="Error.TLabel",
                                  justify=tk.LEFT, wraplength=SETTINGS_WRAP)
        self._osc_err.pack(anchor=tk.W, pady=(6, 0))

    def _chatbox_port(self) -> int:
        """当前 chatbox.port（脏值一律回落到 9000，绝不把非数字显示到输入框里）。"""
        try:
            return int((self._cfg.chatbox or {}).get("port", 9000))
        except Exception:  # noqa: BLE001
            return 9000

    def _on_save_osc_port(self) -> None:
        """把 VRChat 的 OSC 接收端口就地写回 config.yaml 的 `chatbox.port`。

        写法照 `_save_room_cfg` / `_on_save_provider`：就地改文本，保住注释与键顺序；
        `chatbox:` 段在老配置里可能缺失 → 用 `_yaml_set_or_create` 补建。
        校验失败**不写盘**，就地红字 + 状态栏各留一行（禁静默丢弃）。
        """
        raw = (self._osc_port_var.get() or "").strip()
        port = int(raw) if raw.isdigit() else None
        if port is None or not 1 <= port <= 65535:
            err = t("端口必须是 1–65535 之间的整数")
            self._osc_err.configure(text="❌ " + err)
            self._set_status("error", t("❌ 没保存：{msg}", msg=err))
            print(f"[gui] ❌ OSC 端口没保存：{raw!r}（{err}）", flush=True)
            return
        p = DEFAULT_CONFIG
        if not p.exists():
            msg = f"找不到 {p.name}"
            self._osc_err.configure(text=t("❌ 没保存：{msg}", msg=msg))
            self._set_status("error", t("❌ 没保存：{msg}", msg=msg))
            print(f"[gui] ❌ OSC 端口没保存：{msg}", flush=True)
            return
        try:
            text = p.read_text(encoding="utf-8")
            text = _yaml_set_or_create(text, ["chatbox", "port"], str(port))
            _write_config_text(p, text)
        except Exception as exc:                # noqa: BLE001  写盘失败只留痕，配置保持原样
            self._osc_err.configure(text=t("❌ 没保存：{msg}", msg=exc))
            self._set_status("error", t("❌ 没保存：{msg}", msg=exc))
            print(f"[gui] ❌ 保存 OSC 端口失败（配置未改动）：{exc}", flush=True)
            return
        self._osc_err.configure(text="")
        # ⚠️ 必须同步内存：引擎是在「开始翻译」时按 `self._cfg.chatbox` 建 Chatbox 的 ——
        # 不同步的话本轮点开始翻译还用旧端口，症状＝「改了没反应」。
        if isinstance(self._cfg.chatbox, dict):
            self._cfg.chatbox["port"] = port
        if any(e.running for e in self._engines):
            # 已经在跑的那条引擎，端口在建链时就定死了（与线路同理）
            self._set_status("warn", t("OSC 端口已保存：{port}（正在翻译，重开翻译后生效）",
                                       port=port))
            print(f"[gui] ⚠️ OSC 端口已保存：{port}，但翻译正在进行中 —— "
                  f"需先停止再重新开始才生效", flush=True)
        else:
            self._set_status("ok", t("OSC 端口已保存：{port}", port=port))
            print(f"[gui] OSC 端口已保存：{port}", flush=True)

    # ---------------------------------------------------------------- 设置弹窗 · 服务线路
    def _build_provider_section(self, body: ttk.Frame) -> None:
        """「服务线路」区：千问云 / 千问云·海外版（**互斥**，同一时刻只有一条在工作）。

        控件每次都重建（弹窗每次打开都走一遍 `_build_settings_general`），所以状态一律
        从 `self._cfg.session_base` 回填 —— 不在别处另存一份，免得留脏值。

        线路的差别只有两条：**连哪个域名**、**用哪把 key**（两版 key 不互通）；
        海外版不需要业务空间 ID / 地域，所以这里只有一个下拉，没有别的输入框。
        """
        ttk.Label(body, text=t("服务线路"), style="Section.TLabel").pack(anchor=tk.W)

        # 显示名 ↔ 线路 id：两张反向字典都每次重建（显示名随界面语言变）。
        # ⚠️ 写进 config.yaml 的**只有 id**（见 _provider_choices 的说明）。
        self._provider_name_to_id = dict(_provider_choices())
        self._provider_id_to_name = {pid: name
                                     for name, pid in self._provider_name_to_id.items()}
        cur = self._provider()
        self._provider_var = tk.StringVar(value=self._provider_id_to_name[cur])
        self._provider_combo = ttk.Combobox(body, values=list(self._provider_name_to_id),
                                            state="readonly",
                                            textvariable=self._provider_var)
        self._provider_combo.pack(fill=tk.X, pady=(8, 4))
        self._provider_combo.bind("<<ComboboxSelected>>", self._on_provider_change)

        # 一句话讲清两条线路怎么选：海外用户最常问的就是「我该用哪条、key 能不能共用」。
        ttk.Label(body,
                  text=t("国内用「千问云」；海外用「千问云·海外版」（qwencloud.com）。"
                         "两版的 API key 不互通，各存各的，来回切线路不用重填"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

        save_row = ttk.Frame(body)
        save_row.pack(fill=tk.X, pady=(10, 0))
        self._provider_save_btn = ttk.Button(save_row, text=t("保存线路设置"),
                                             command=self._on_save_provider)
        self._provider_save_btn.pack(side=tk.RIGHT)
        # 就地提示（红字）：保存被拒时必须**在这一屏里看得见**"什么都没写"——
        # 只在状态栏闪一行会被漏掉，而漏掉的后果是用户以为已经切到海外线路了。
        # 常态是空文本，但控件常驻（占一行高），出提示时布局不跳。
        self._provider_err = ttk.Label(body, text="", style="Error.TLabel",
                                       justify=tk.LEFT, wraplength=SETTINGS_WRAP)
        self._provider_err.pack(anchor=tk.W, pady=(6, 0))

    def _on_provider_change(self, _event=None) -> None:
        """下拉选完线路：**只清掉就地红字，不写盘** —— 落盘统一走「保存线路设置」。

        为什么不即时保存：用户可能只是点开看看，选一半就写盘会凭空改掉 config.yaml
        （而真正生效还得重开翻译）。
        """
        self._provider_err.configure(text="")

    def _selected_provider(self) -> str:
        """下拉当前选中的线路 id（认不出来就回落千问云，绝不 KeyError）。"""
        return self._provider_name_to_id.get(self._provider_var.get(),
                                             endpoints.PROVIDER_QIANWEN)

    def _on_save_provider(self) -> None:
        """把线路两项（provider / base_url）就地写回 config.yaml。

        线路决定「连哪个域名 + 用哪把 key」，所以保存成功后要顺手刷新 key 状态
        （key 按线路分槽，切完显示的是另一把）。

        写法照 `_save_room_cfg`：就地改文本，保住注释与键顺序。`session:` 段是模板的
        第一段，一定存在，**不需要**像 room 段那样补建。
        """
        provider = self._selected_provider()
        base_url = endpoints.default_base_url(provider)
        p = DEFAULT_CONFIG
        if not p.exists():
            print(f"[gui] ❌ 线路没保存：找不到 {p.name}（配置尚未生成）", flush=True)
            self._provider_err.configure(text=t("❌ 没保存：{msg}", msg=p.name))
            self._set_status("error", t("❌ 没保存：{msg}", msg=p.name))
            return
        try:
            _persist_provider(p, provider, base_url)
        except Exception as exc:                # noqa: BLE001  写盘失败只留痕，配置保持原样
            print(f"[gui] ❌ 保存服务线路失败（配置未改动）：{exc}", flush=True)
            self._provider_err.configure(text=t("❌ 没保存：{msg}", msg=exc))
            self._set_status("error", t("❌ 没保存：{msg}", msg=exc))
            return
        self._provider_err.configure(text="")
        # 写完同步内存：不同步的话「开始翻译」用的还是启动时那份（base_url 尤其致命）
        self._cfg.session_base["provider"] = provider
        self._cfg.session_base["base_url"] = base_url
        # key 按线路分槽：线路一变，该显示/该用的就是**另一个槽**里那把 key
        self._refresh_key_status()
        self._refresh_api_key_in_cfg()
        if any(e.running for e in self._engines):
            # 会话的 base_url 在建链时就定死了，运行中改配置不会重连 —— 必须明说
            self._set_status("warn", t("当前线路需要先停止翻译，改完再重新开始"))
            print("[gui] ⚠️ 服务线路已保存，但翻译正在进行中 —— 需先停止再重新开始才生效",
                  flush=True)
        else:
            self._set_status("ok", t("已切换到 {line}（重启翻译后生效）",
                                     line=self._provider_label()))
        print(f"[gui] 服务线路已保存：provider={provider} base_url={base_url}", flush=True)

    # ---------------------------------------------------------------- 线路取值助手
    def _provider(self) -> str:
        """当前线路 id（脏值由 endpoints 归一化 + 留痕，这里绝不自己判第二遍）。"""
        return endpoints.normalize_provider(self._cfg.session_base.get("provider"))

    def _provider_label(self) -> str:
        """当前线路的**界面显示名**（按界面语言取词）。给用户看的文案一律走这里，
        日志才用 `endpoints.provider_name()`（那份恒为中文）。"""
        for name, pid in _provider_choices():
            if pid == self._provider():
                return name
        return t("千问云")          # 走不到：_provider() 已归一化成合法 id

    def _current_key_slot(self) -> str:
        """当前线路的密钥槽名。千问云与千问云·海外版的 key 不通用 → 分槽各存一份，切线路不用重填。

        ⚠️ 名字里必须带 `current`：`self._key_slot` 已经被主界面第二行的**控件容器**
        （一个 ttk.Frame）占了，方法同名会被那个实例属性盖掉 → 调用即
        `TypeError: 'Frame' object is not callable`（i18n 那批真窗口用例当场就红）。
        """
        return endpoints.key_slot(self._provider())

    def _signup_url(self) -> str:
        """当前线路的开通页地址。qianwen 时与 `QIANWEN_SIGNUP_URL` 逐字符相同
        （那个常量被测试直接断言，故保留不删）。"""
        return endpoints.signup_url(self._provider())

    # ---------------------------------------------------------------- 设置弹窗 · 音频页
    def _build_settings_audio(self, body: ttk.Frame) -> None:
        """「音频」页：设备选择 → 输入门限 → 译音音色（都是「声音怎么进出」这一件事）。"""
        # ---- 音频设备 ----
        dev_head = ttk.Frame(body)
        dev_head.pack(fill=tk.X)
        # 按钮先占右侧（压不动），标题后打包 —— 空间不足时被裁的才是标题
        self._refresh_btn = ttk.Button(dev_head, text=t("刷新"),
                                       command=self._on_refresh_devices)
        self._refresh_btn.pack(side=tk.RIGHT)
        ttk.Label(dev_head, text=t("音频设备"), style="Section.TLabel").pack(side=tk.LEFT)

        grid = ttk.Frame(body)
        grid.pack(fill=tk.X, pady=(8, 2))
        grid.columnconfigure(1, weight=1)
        auto = t("自动检测")
        # ⚠️ 控件名不能改：设备扫描结果直接往这些下拉里写值
        #
        # Linux 上「VRChat 音频」与「译音输出」**不暴露给用户**（末影猫口径）：
        #   · VRChat 音频：采集目标固定为「等 VRChat 的输出流」，不抓系统默认输出
        #     （默认 sink 上混着浏览器/音乐，抓它等于把噪音当游戏内语音）；
        #   · 译音输出：固定写到本程序运行时自建的虚拟麦，手选设备毫无意义
        #     （引擎的 Linux 分支本就忽略 output.audio.device_name）。
        # 于是 Linux 只留「麦克风」一个下拉；两个属性置 None，读写在
        # `self._linux_fixed_audio` 为真时一律跳过。配置键保留不动（向后兼容）。
        self._linux_fixed_audio = platform.IS_LINUX
        self._mic_combo = ttk.Combobox(grid, values=[auto], state="readonly")
        self._loopback_combo: ttk.Combobox | None = None
        self._audio_out_combo: ttk.Combobox | None = None
        rows = [(t("麦克风:"), self._mic_combo)]
        if not self._linux_fixed_audio:
            self._loopback_combo = ttk.Combobox(grid, values=[auto], state="readonly")
            self._audio_out_combo = ttk.Combobox(grid, values=[auto], state="readonly")
            rows += [(t("VRChat 音频:"), self._loopback_combo),
                     (t("译音输出:"), self._audio_out_combo)]
        for i, (label, combo) in enumerate(rows):
            ttk.Label(grid, text=label, style="Dim.TLabel").grid(
                row=i, column=0, sticky="w", pady=3)
            combo.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=3)
            combo.set(auto)
            combo.bind("<<ComboboxSelected>>", self._on_device_change)

        # 从配置恢复选择
        capture_cfg = (self._cfg.output or {}).get("capture") or {}
        audio_cfg = (self._cfg.output or {}).get("audio") or {}
        self._mic_combo.set(capture_cfg.get("mic_device") or auto)
        if not self._linux_fixed_audio:
            self._loopback_combo.set(capture_cfg.get("loopback_device") or auto)
            self._audio_out_combo.set(audio_cfg.get("device_name") or auto)

        if self._linux_fixed_audio:
            ttk.Label(body, text=t("Linux：VRChat 音频与译音输出已自动处理"),
                      style="Muted.TLabel", justify=tk.LEFT,
                      wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))
        else:
            ttk.Label(body, text=t("设备选择自动保存到 config.yaml"),
                      style="Muted.TLabel", justify=tk.LEFT,
                      wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

        # ---- 输入门限（只作用于 VRChat 输出 = 「别人说话」那条腿）----
        # 与设备选择同属「输入侧」：设备选好之后，紧接着就是「收到的东西要多响才送」。
        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
        ttk.Label(body, text=t("输入门限"), style="Section.TLabel").pack(anchor=tk.W)
        # 取值统一走 engine.input_gate_settings：配置里的非法值在那里留痕并回落默认值，
        # 界面与引擎看到的就是同一套值（省得两边各解析一遍、口径还不一致）。
        _gate_en, _gate_db, self._gate_hold_ms, self._gate_preroll_ms = \
            input_gate_settings(capture_cfg)
        self._gate_enabled_var = tk.BooleanVar(value=_gate_en)
        self._gate_check = tk.Checkbutton(
            body, variable=self._gate_enabled_var,
            text=t("启用 —— 低于门限的声音不翻译（滤掉远处说话小声的玩家）"),
            command=self._on_gate_change, wraplength=SETTINGS_WRAP,
            justify=tk.LEFT, anchor="w", **self._indicator_kw())
        self._gate_check.pack(anchor=tk.W, pady=(8, 4))

        ggrid = ttk.Frame(body)
        ggrid.pack(fill=tk.X)
        ggrid.columnconfigure(1, weight=1)
        # 实时电平条：横轴 -70 ~ 0 dBFS；蓝 = 当前已超过门限（这段会被翻译），
        # 白竖线 = 门限位置。数据来自 loopback 采集：正在翻译时用引擎那条腿，
        # 没翻译时用独立探针（设置窗可见 + 勾了「启用」才采）；两路都没有才显示「—」。
        self._gate_level_canvas = tk.Canvas(ggrid, width=320, height=15,
                                            bg=SURFACE, highlightthickness=1,
                                            highlightbackground=BORDER, bd=0)
        ttk.Label(ggrid, text=t("当前电平:"), style="Dim.TLabel").grid(
            row=0, column=0, sticky="w", pady=3)
        self._gate_level_canvas.grid(row=0, column=1, sticky="w", padx=(8, 6), pady=3)
        self._gate_level_lbl = ttk.Label(ggrid, text="—", style="Dim.TLabel", width=9)
        self._gate_level_lbl.grid(row=0, column=2, sticky="w")

        self._gate_var = tk.DoubleVar(value=_gate_db)
        ttk.Label(ggrid, text=t("门限:"), style="Dim.TLabel").grid(
            row=1, column=0, sticky="w", pady=3)
        self._gate_scale = tk.Scale(
            ggrid, from_=INPUT_GATE_MIN_DB, to=INPUT_GATE_MAX_DB, resolution=1,
            orient=tk.HORIZONTAL, variable=self._gate_var, showvalue=False, length=320,
            bg=PANEL, fg=TEXT, troughcolor=SURFACE, activebackground=ACCENT,
            highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
            command=self._on_gate_change)
        self._gate_scale.grid(row=1, column=1, sticky="w", padx=(8, 6), pady=3)
        self._gate_val_lbl = ttk.Label(ggrid, text=f"{_gate_db:g} dB",
                                       style="Dim.TLabel", width=9)
        self._gate_val_lbl.grid(row=1, column=2, sticky="w")
        ttk.Label(body, text=t("只有响度超过门限的声音才会被翻译；改完立刻生效（勾选「启用」后这里显示实时电平）"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

        # ---- 音色 ----
        # 两条出声音色来自**不同模型**，音色 id 不通用（跨模型混用会被服务端拒），
        # 所以分两个下拉：「说话译音」写 session.voice（实时模型直出的译音），
        # 「打字译音」写 text_input.tts.voice（文本翻译后单独调 qwen3-tts-flash）。
        # 下拉**可编辑**：表里没有的（新音色 / 声音复刻的 voice id）也能手填。
        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
        ttk.Label(body, text=t("音色"), style="Section.TLabel").pack(anchor=tk.W)
        vgrid = ttk.Frame(body)
        vgrid.pack(fill=tk.X, pady=(8, 2))
        vgrid.columnconfigure(1, weight=1)
        self._speech_voice_var = tk.StringVar()
        self._tts_voice_var = tk.StringVar()
        self._speech_voice_combo = ttk.Combobox(
            vgrid, textvariable=self._speech_voice_var, state="normal", width=18)
        self._tts_voice_combo = ttk.Combobox(
            vgrid, textvariable=self._tts_voice_var, state="normal", width=18)
        # 每行末尾一个「试听」按钮：合成一句固定样例、在**本地扬声器**播放。
        # 说话侧是尽力而为 —— 实时模型音色（如默认 Tina）多为 Qwen-Omni 独占，
        # qwen3-tts-flash 合成不了，会明确提示「暂不支持试听」，绝不静默失败。
        _preview_w = _char_width_for(t("试听中…"), FONT_UI, 6)
        for i, (label, combo, values, kind, cb) in enumerate((
                (t("说话译音:"), self._speech_voice_combo,
                 voice_choices(self._effective_speech_voice(), REALTIME_VOICES),
                 "speech", self._on_preview_speech_voice),
                (t("打字译音:"), self._tts_voice_combo,
                 voice_choices(str((self._cfg.text_input.get("tts") or {}).get("voice") or ""),
                               TTS_VOICES),
                 "tts", self._on_preview_tts_voice))):
            ttk.Label(vgrid, text=label, style="Dim.TLabel").grid(
                row=i, column=0, sticky="w", pady=3)
            combo.configure(values=values)
            combo.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=3)
            btn = ttk.Button(vgrid, text=t("试听"), width=_preview_w, command=cb)
            btn.grid(row=i, column=2, sticky="w", padx=(8, 0), pady=3)
            if kind == "speech":
                self._speech_preview_btn = btn
            else:
                self._tts_preview_btn = btn
        # 从配置回显：读不到就显服务端默认（说话 Tina / 打字 Cherry），
        # 与 config.load 的回落口径一致 —— 宁可显示真正在生效的值，也不显示空白。
        self._speech_voice_var.set(self._effective_speech_voice())
        self._tts_voice_var.set(
            str((self._cfg.text_input.get("tts") or {}).get("voice") or "Cherry"))
        self._speech_voice_combo.bind("<<ComboboxSelected>>", self._on_speech_voice_change)
        self._speech_voice_combo.bind("<Return>", self._on_speech_voice_change)
        self._tts_voice_combo.bind("<<ComboboxSelected>>", self._on_tts_voice_change)
        self._tts_voice_combo.bind("<Return>", self._on_tts_voice_change)
        self._voice_note = ttk.Label(body, text=t("说话译音跟随「译音输出」开关（改完下次开始翻译生效）；"
                                                  "打字译音立刻生效"),
                                     style="Muted.TLabel", justify=tk.LEFT,
                                     wraplength=SETTINGS_WRAP)
        self._voice_note.pack(anchor=tk.W, pady=(6, 0))

    # ---------------------------------------------------------------- 设置弹窗 · 词库页
    def _build_settings_glossary(self, body: ttk.Frame) -> None:
        """「词库」页：专有名词怎么译（社团名 / 人名 / 术语）。"""
        # ---- 专有词库 ----
        # 用户场景：VRChat 里念社团名 / 人名 / 术语，模型要么听错、要么按字面意译
        # （「VRChat」被翻成「虚拟聊天」这种）。词库就是把这些词**钉死**：
        #   · 实时那条腿 → session.translation.corpus.phrases（顺带提升识别率）
        #   · 打字那条腿 → translation_options.terms（qwen-mt 的术语干预）
        #
        # 词库分两层，`config.merge_hotwords` 是**唯一**合并口径：
        #   全局（`glossary`）—— 两个方向共用；
        #   方向级（`directions.<X>.hotwords`）—— 只作用于一条腿，同名词条**覆盖**全局。
        # 两层都得能改，所以下面给一个「作用方向」下拉切换的是"**编辑哪张表**"：
        # 两个方向的目标语言通常不同（同一个社团名，别人说时要中文译名、我说时要保持原样），
        # 只让用户编辑全局那份的话，这功能对最常见的场景等于白给（实测过：全局写
        # `Nekoya=猫屋` 会让「我说 → 英文」的译文变成 `the Cat House club`）。
        #
        # 为什么这里用 tk.Text 而不是 ttk.Entry：一个词库是**多行**的，单行输入框
        # 逼用户去手改 YAML（这功能就等于没做）。样式手动对齐 SURFACE/TEXT 体系，
        # 因为 tk.Text 不走 ttk style。
        ttk.Label(body, text=t("专有词库"), style="Section.TLabel").pack(anchor=tk.W)
        # 作用方向：显示名（i18n）→ 内部 scope key。顺序固定：全局 / 我说 / 别人说
        self._glossary_scope_names = {
            "global": t("全局"),
            "mine": t("我说"),
            "theirs": t("别人说"),
        }
        scope_row = ttk.Frame(body)
        scope_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(scope_row, text=t("作用方向:"), style="Dim.TLabel").pack(side=tk.LEFT)
        self._glossary_scope_var = tk.StringVar(value=self._glossary_scope_names["global"])
        self._glossary_scope_combo = ttk.Combobox(
            scope_row, values=list(self._glossary_scope_names.values()),
            state="readonly", width=10, textvariable=self._glossary_scope_var)
        self._glossary_scope_combo.pack(side=tk.LEFT, padx=(8, 0))
        self._glossary_scope_combo.bind("<<ComboboxSelected>>", self._on_glossary_scope_change)
        gloss_box = ttk.Frame(body)
        gloss_box.pack(fill=tk.X, pady=(8, 2))
        self._glossary_text = tk.Text(
            gloss_box, height=9, width=44, wrap=tk.NONE, undo=True,
            bg=SURFACE, fg=TEXT, insertbackground=TEXT, selectbackground=ACCENT,
            selectforeground="#ffffff", relief=tk.FLAT, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, font=FONT_UI)
        self._attach_edit_menu(self._glossary_text)   # 右键剪切/复制/粘贴（词库是多行文本）
        # 滚动条**先**占住右侧（它压不动），词库框后打包并 fill=X expand 吸收压缩；
        # 顺序反过来窗口变窄时滚动条会被挤成 1px。样式要显式引用，否则是 clam 的浅灰。
        gloss_sb = ttk.Scrollbar(gloss_box, orient=tk.VERTICAL, style="Vertical.TScrollbar",
                                 command=self._glossary_text.yview)
        gloss_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._glossary_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._glossary_text.configure(yscrollcommand=gloss_sb.set)
        self._glossary_hint = ttk.Label(
            body, text="", style="Muted.TLabel", justify=tk.LEFT,
            wraplength=SETTINGS_WRAP)
        self._glossary_hint.pack(anchor=tk.W, pady=(4, 4))
        gloss_row = ttk.Frame(body)
        gloss_row.pack(fill=tk.X)
        self._glossary_save_btn = ttk.Button(gloss_row, text=t("保存词库"),
                                            command=self._on_save_glossary)
        self._glossary_save_btn.pack(side=tk.RIGHT)
        self._glossary_status = ttk.Label(gloss_row, text="", style="Muted.TLabel")
        self._glossary_status.pack(side=tk.LEFT)
        # 控件建齐后再按当前 scope 填一次（读盘口径与 `_refresh_glossary_box` 完全一致，
        # 这样「打开设置 → 已经是磁盘上的最新内容」这条保证在首屏也成立）
        self._refresh_glossary_box()

    # ---------------------------------------------------------------- 设置弹窗 · 房间页
    def _build_settings_room(self, body: ttk.Frame) -> None:
        """「房间」页：房间码 / 昵称（多人互看字幕要填的两项）。

        这两项从主界面第三行搬来这里：它们是「填一次就不动」的低频输入，常驻主界面
        只会把那一行挤爆（而主界面那一行只该留「连接房间 / 断开连接」这个动作）。
        变量名 `_room_code_var` / `_room_nick_var` 保持不变 —— `_sync_room_cfg_from_fields`
        与回归测试都按这两个名字读写。
        """
        ttk.Label(body, text=t("房间"), style="Section.TLabel").pack(anchor=tk.W)

        # 两个输入框用 grid 同一列 → 标签长度随语言不同（"Код комнаты:" / "Ник:"）时
        # 输入框左缘也照样对齐；列宽由最长的标签自己撑，不写死宽度（俄语会超）。
        form = ttk.Frame(body)
        form.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(form, text=t("房间码:"), style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        self._room_code_var = tk.StringVar(value=self._room_cfg.room_code)
        code_entry = ttk.Entry(form, textvariable=self._room_code_var, width=12,
                               style="Key.TEntry", font=FONT_UI)
        self._room_code_entry = code_entry      # 留引用：右键菜单/自动化测试都要拿到它
        code_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))
        code_entry.bind("<Return>", lambda _e: self._on_room_field_change())
        code_entry.bind("<FocusOut>", lambda _e: self._on_room_field_change())
        self._attach_edit_menu(code_entry)      # 右键粘贴：房间码是从对方那里复制来的，最常用的就是粘贴
        self._room_gen_btn = ttk.Button(form, text=t("随机生成"), command=self._on_room_generate)
        self._room_gen_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(form, text=t("昵称:"), style="Dim.TLabel").grid(row=1, column=0,
                                                                  sticky="w", pady=(6, 0))
        self._room_nick_var = tk.StringVar(value=self._room_cfg.nickname)
        nick_entry = ttk.Entry(form, textvariable=self._room_nick_var, width=12,
                               style="Key.TEntry", font=FONT_UI)
        self._room_nick_entry = nick_entry
        nick_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        nick_entry.bind("<Return>", lambda _e: self._on_room_field_change())
        nick_entry.bind("<FocusOut>", lambda _e: self._on_room_field_change())
        self._attach_edit_menu(nick_entry)

        ttk.Label(body, text=t("和填了同一个房间码的人互相看到对方说的话；只有你自己说的话会被发出去。"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(body, text=t("改动会即时保存；已连接时按新设置重连。"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(4, 0))

    # ---------------------------------------------------------------- 设置弹窗 · 关于页
    def _build_settings_about(self, body: ttk.Frame) -> None:
        """「关于」页：软件更新 + 日志（出问题时要交给维护者的东西）+ 开发者署名。

        这两区改造前排在长列**最末尾**，正好是掉到屏幕外、用户根本看不到也点不着的部分。
        """
        # ---- 软件更新 ----
        upd_head = ttk.Frame(body)
        upd_head.pack(fill=tk.X)
        # 按钮先占右侧（压不动），标题后打包 —— 空间不足时被裁的才是标题
        self._update_check_btn = ttk.Button(
            upd_head, text=t("检查更新"),
            command=lambda: self._schedule_update_check(manual=True))
        self._update_check_btn.pack(side=tk.RIGHT)
        ttk.Label(upd_head, text=t("软件更新"), style="Section.TLabel").pack(side=tk.LEFT)
        self._update_info = ttk.Label(
            body, text=t("当前版本 v{ver} · 启动时会自动检查一次", ver=__version__),
            style="Muted.TLabel", justify=tk.LEFT, wraplength=SETTINGS_WRAP)
        self._update_info.pack(anchor=tk.W, pady=(6, 0))

        # ---- 日志 ----
        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
        log_head = ttk.Frame(body)
        log_head.pack(fill=tk.X)
        self._log_export_btn = ttk.Button(log_head, text=t("导出日志压缩包…"),
                                          command=self._on_export_logs)
        self._log_export_btn.pack(side=tk.RIGHT)
        # side=RIGHT 后打包的排在已有按钮左边：[打开日志文件夹] [导出日志压缩包…]
        self._log_open_btn = ttk.Button(log_head, text=t("打开日志文件夹"),
                                        command=self._on_open_log_folder)
        self._log_open_btn.pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Label(log_head, text=t("日志"), style="Section.TLabel").pack(side=tk.LEFT)
        self._log_info = ttk.Label(body, text="", style="Muted.TLabel", justify=tk.LEFT,
                                   wraplength=SETTINGS_WRAP)
        self._log_info.pack(anchor=tk.W, pady=(6, 0))
        self._refresh_log_info()

        # ---- 开发者 ----
        ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
        ttk.Label(body, text=t("开发者"), style="Section.TLabel").pack(anchor=tk.W)
        ttk.Label(body, text=t("由可爱的赛博巫师和他的朋友们 开发"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(6, 0))

        # ---- 赞助者 ----
        # 名字**不翻译**（专有名词，各语言界面都原样显示）；名单见模块顶部的 SPONSORS。
        # 空名单时整区不出现：留一个没有内容的「赞助者」标题比不显示更难看。
        # 每个名字一个名单项控件 → 名字所在的 Label 列表挂在 self._sponsor_names_widgets，
        # tests/test_i18n.py 的语言守卫按这些控件路径排除名字（绝不按文本内容排除）。
        self._sponsor_names_widgets: list[tk.Label] = []
        if SPONSORS:
            ttk.Separator(body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=14)
            # 标题带个心形，与上面的「开发者」区做轻重区分；心形只出现在这里，
            # 名单里不重复（评审 2026-10-03：同一符号出现两遍显得杂）。
            ttk.Label(body, text=f"❤ {t('赞助者')}", style="Section.TLabel").pack(anchor=tk.W)
            flow = ttk.Frame(body)
            flow.pack(fill=tk.X, pady=(8, 0))
            self._build_sponsor_list(flow)

    def _build_sponsor_list(self, flow: ttk.Frame) -> None:
        """把赞助者名字排成自动换行的**亮字名单**：加粗 + 亮色，**不加底块也不加描边**。

        为什么不是"实心方块"：本界面的按钮就是实心矩形（`SURFACE` / `SURFACE_HOVER` 底 +
        无描边），给名字加同样的实心底块会和真按钮撞脸（四轮视觉评审都提到"像按钮"）。
        为什么不是 1px 描边的名牌：那种"幽灵按钮"同样是按钮的视觉语言（评审第 5 轮原话）。
        为什么不是纯灰字：那样名字会被当成页脚脚注（用户最初的反馈）。
        ⇒ 只靠**加粗 + 亮色**（`COLOR_SRC_MINE`）把名字托出来 —— 这是本页其他正文都没有的待遇，
        既显眼又不带任何"可点击"暗示，和「关于」页的纯文字排版也统一。

        为什么手排而不用 pack/grid：pack 不换行、grid 要预先知道列数，而名单个数不定（1～20+）。
        这里用字体实测宽度做流式布局：一行排不下就开新行，行宽上限 = 设置页内容宽
        （`SETTINGS_WRAP`），窗口尺寸逻辑不用动（页变高有设置页滚动兜底）。
        心形符号只在「❤ 赞助者」标题出现一次，名单里不重复。
        带名字文本的 Label 挂进 `self._sponsor_names_widgets`（属性名是 i18n 守卫的契约，不改）。
        """
        font = tkfont.Font(font=FONT_BOLD_MD)
        gap_x, gap_y = 16, 6
        row = ttk.Frame(flow)
        row.pack(anchor=tk.W)
        used = 0
        for name in SPONSORS:
            need = font.measure(name)
            if used and used + gap_x + need > SETTINGS_WRAP:      # 本行排不下 → 开新行
                row = ttk.Frame(flow)
                row.pack(anchor=tk.W, pady=(gap_y, 0))
                used = 0
            item = tk.Label(row, text=name, font=FONT_BOLD_MD,
                            fg=COLOR_SRC_MINE, bg=PANEL)
            item.pack(side=tk.LEFT, padx=(0 if used == 0 else gap_x, 0))
            used += (0 if used == 0 else gap_x) + need
            self._sponsor_names_widgets.append(item)

    def _log_dir(self) -> Path:
        from .crashlog import _LOG_PATH      # noqa: SLF001  （跟着实际日志走）

        if _LOG_PATH is not None:
            return Path(_LOG_PATH).parent
        return ROOT / "logs"

    def _refresh_log_info(self) -> None:
        """把日志目录/体积/上限显示出来 —— 用户才知道会不会把磁盘吃爆。"""
        from .crashlog import MAX_LOG_TOTAL_BYTES

        d = self._log_dir()
        try:
            files = [p for p in d.glob("*.log*") if p.is_file()]
            total = sum(p.stat().st_size for p in files)
        except OSError:
            files, total = [], 0
        cap_mb = MAX_LOG_TOTAL_BYTES // 1024 // 1024
        self._log_info.config(
            text=t("共 {n} 个文件，{size} MB（超过 {cap} MB 自动删最旧的）\n{path}\n出问题时导出压缩包发给维护者即可（自动脱敏，不含密钥）",
                   n=len(files), size=f"{total / 1024 / 1024:.1f}", cap=cap_mb, path=d))

    def _on_export_logs(self) -> None:
        """把日志打成 zip 到用户指定位置 —— 给朋友用来自证问题的入口。"""
        import datetime as _dt
        from tkinter import filedialog, messagebox

        from .crashlog import export_logs

        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            dest = filedialog.asksaveasfilename(
                parent=self._settings_win, title=t("导出日志压缩包"),
                initialfile=f"vrchat-livetranslate-logs-{stamp}.zip",
                defaultextension=".zip",
                filetypes=[(t("ZIP 压缩包"), "*.zip"), (t("所有文件"), "*.*")])
        except Exception as exc:  # noqa: BLE001
            self._set_status("error", t("打不开保存对话框：{msg}", msg=exc))
            return
        if not dest:
            print("[gui] 导出日志：用户取消", flush=True)
            return
        try:
            path, n, size, redacted = export_logs(Path(dest), log_dir=self._log_dir())
        except Exception as exc:  # noqa: BLE001
            self._set_status("error", t("导出日志失败：{msg}", msg=exc))
            print(f"[gui] ❌ 导出日志失败：{type(exc).__name__}: {exc}", flush=True)
            return
        # 同一条内容两份：日志保持中文（诊断用），界面走 i18n
        msg_log = f"日志已导出：{path}（{n} 个文件，{size / 1024:.0f} KB）"
        msg_ui = t("日志已导出：{path}（{n} 个文件，{size} KB）",
                   path=path, n=n, size=f"{size / 1024:.0f}")
        if redacted:
            msg_log += f"｜已脱敏 {len(redacted)} 个文件"
            msg_ui += t("｜已脱敏 {n} 个文件", n=len(redacted))
        self._set_status("ok", msg_ui)
        print(f"[gui] ✅ {msg_log}", flush=True)
        self._refresh_log_info()
        try:
            messagebox.showinfo(t("导出完成"),
                                f"{msg_ui}\n\n{t('把这个压缩包发给维护者即可。')}",
                                parent=self._settings_win)
        except Exception:
            pass

    def _on_open_log_folder(self) -> None:
        """打开日志文件夹 —— 与「导出日志压缩包」看到的是同一个目录（_log_dir 单一真相，
        源码运行 = 仓库 logs/，打包后 = %APPDATA%\\vrchat-livetranslate\\logs，绿色版 = exe 旁）。

        走跨平台封装 `platform.open_path`（Windows = `os.startfile`，Linux = `xdg-open`）——
        以前这里直接调 `os.startfile`，Linux 上没有该属性会抛 AttributeError，必开必败。
        打不开**不许静默**：状态栏 + 日志都留痕。
        """
        d = self._log_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)   # 还没写过日志时也能打开（空目录）
            platform.open_path(str(d))
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] ⚠️ 打不开日志文件夹（{d}）：{type(exc).__name__}: {exc}", flush=True)
            self._set_status("error", t("打不开日志文件夹：{msg}", msg=exc))
            return
        print(f"[gui] 已打开日志文件夹：{d}", flush=True)

    # `_open_settings(page=…)` 的参数 → tab 标题。**新增可跳转的页只在这里加一行**：
    # tab 是用标题登记的（`_settings_tabs[t(标题)]`），把映射散在各个调用点就会漂移。
    _SETTINGS_PAGE_TAB = {"room": "房间", "general": "常规"}

    def _select_settings_page(self, page: str) -> None:
        """把设置弹窗切到某一页；切页失败只留痕，**绝不拦住弹窗打开**。"""
        title = self._SETTINGS_PAGE_TAB.get(page)
        if not title:
            return                              # 没登记的 page：保持当前页，不自作主张
        try:
            tab = self._settings_tabs.get(t(title))
            if tab is not None and self._settings_nb is not None:
                self._settings_nb.select(tab)
        except Exception as exc:  # noqa: BLE001 — 切页失败不该拦住弹窗打开
            print(f"[ui] ⚠️ 设置弹窗切到「{title}」页失败（不影响使用）："
                  f"{type(exc).__name__}: {exc}", flush=True)

    def _open_settings(self, page: str | None = None) -> None:
        """打开设置弹窗（已建好，只是显示出来），定位到主窗口附近且**整窗都在屏幕内**。

        `page`：打开后停在哪一页（见 `_SETTINGS_PAGE_TAB`）。点「连接房间」却没填房间码、
        或点「连接房间」却缺房间码时，光弹一句提示等于让用户自己去找那个输入框 ——
        直接把他送到该填的那一页。
        """
        win = self._settings_win
        self._refresh_key_status()          # 每次打开都刷新来源/打码显示
        self._refresh_glossary_box()        # 手改过 config.yaml 的话，别让旧内容把它覆盖回去
        win.update_idletasks()
        ww, wh = self._settings_size
        rx, ry = self._root.winfo_x(), self._root.winfo_y()
        rw = self._root.winfo_width()
        x, y = rx + max((rw - ww) // 2, 20), ry + 48
        # 夹进屏幕：改造前不做这一步，1485px 高的弹窗从 y=352 起、底边落到 1837
        # （屏高 1600），最下面两个分区整个在屏幕外 —— 不是难找，是够不着。
        screen_w = int(win.winfo_screenwidth() or 0)
        screen_h = int(win.winfo_screenheight() or 0)
        if screen_w:
            x = max(8, min(x, screen_w - ww - 8))
        if screen_h:
            y = max(8, min(y, screen_h - wh - 48))    # 底部留 48px 给任务栏
        win.geometry(f"{ww}x{wh}+{x}+{y}")
        win.deiconify()
        win.lift()
        win.focus_set()
        if page:
            self._select_settings_page(page)
        # 建窗时它处于 withdraw 状态，那时调 DWM 拿不到有效 hwnd、会静默失败
        # （实测弹窗标题栏仍是浅色、跟主窗口不一致）。显示出来之后再设一次。
        self._apply_dark_titlebar(win)
        # 也是同理：withdraw 时量不到可视高度，滚动条的显隐得等显示出来再判一次
        self._sync_settings_pages()
        # 实时电平：窗口一打开（且勾了「启用」）就自己采一路，不必先点「开始翻译」。
        # 此刻窗口可能还没被 WM 映射完（winfo_viewable 尚为假）—— 没关系，
        # _poll 每 100ms 会再同步一次，最迟一跳就起来。
        self._sync_gate_level_probe()

    def _close_settings(self) -> None:
        self._settings_win.withdraw()
        # 窗口一关就**立刻**停采集、join 线程、关掉设备：窗口关着还占着声卡是不可接受的
        self._sync_gate_level_probe()

    # ---------------------------------------------------------------- 界面语言
    def _on_ui_lang_change(self, _event=None) -> None:
        """选完立即写 ui.lang；本批不重建树 —— 明确提示重启后生效（不许静默）。"""
        name = self._ui_lang_var.get()
        code = next((c for c, n in self._ui_lang_names.items() if n == name), "zh")
        self._save_ui_language(code)
        lang_name = self._ui_lang_names.get(code, name)
        self._ui_lang_note.configure(
            text=t("已保存：重启程序后界面将切换为 {lang}", lang=lang_name))
        self._set_status("info",
                         t("界面语言已保存：{lang}（重启程序后生效）", lang=lang_name))
        print(f"[gui] 界面语言已选择：{code}（{lang_name}），已写入 ui.lang，重启后生效",
              flush=True)

    def _save_ui_language(self, code: str) -> None:
        """把界面语言写进 config.yaml 的 ui.lang（就地改文本，保住注释与键顺序）。"""
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            if not re.search(r"^ui:", text, re.M):
                text = text.rstrip("\n") + "\n\n# 界面上次的选择（启动时自动恢复，不用手改）\nui:\n"
            text = _yaml_set_in_text(text, ["ui", "lang"], code)
            _write_config_text(p, text)
            if isinstance(self._cfg.ui, dict):
                self._cfg.ui["lang"] = code
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存界面语言失败：{exc}", flush=True)

    # ---------------------------------------------------------------- 赞助弹窗
    def _open_kofi(self) -> None:
        """打开 Ko-fi 赞助页面（独立成小方法：测试打桩它，绝不真开浏览器）。"""
        try:
            webbrowser.open(SPONSOR_URL)
            print(f"[gui] 已打开赞助页面 {SPONSOR_URL}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] ⚠️ 打不开浏览器：{type(exc).__name__}: {exc}", flush=True)
            self._set_status("warn", t("打不开浏览器，请手动访问 {url}", url=SPONSOR_URL))

    def _open_sponsor(self) -> None:
        """打开赞助弹窗；已经开着时只聚焦/置顶已有窗口，绝不 new 第二个。"""
        if self._sponsor_win is not None:
            try:
                if self._sponsor_win.winfo_exists():
                    self._sponsor_win.deiconify()
                    self._sponsor_win.lift()
                    self._sponsor_win.focus_set()
                    return
            except Exception:  # noqa: BLE001
                pass
            self._sponsor_win = None
        try:
            self._build_sponsor_dialog()
        except Exception as exc:  # noqa: BLE001
            # 辅助功能挂掉绝不能拖垮主窗口：留痕 + 状态栏提示，不往上抛
            self._sponsor_win = None
            print(f"[gui] ⚠️ 赞助弹窗创建失败（不影响主功能）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            self._set_status("warn", t("赞助弹窗打不开：{msg}", msg=exc))

    def _build_sponsor_dialog(self) -> None:
        """赞助弹窗：Ko-fi 按钮 + 可复制地址 + 两张收款码（微信/支付宝）。

        深色主题完全复用主窗口的颜色常量与 ttk style，不自创颜色。
        收款码必须**等比**缩放（LANCZOS，目标边长 240px）——拉变形就扫不出来。
        """
        win = tk.Toplevel(self._root)
        win.title(t("赞助"))
        win.configure(bg=PANEL)
        win.transient(self._root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._close_sponsor)
        win.bind("<Escape>", lambda _e: self._close_sponsor())
        self._sponsor_win = win
        self._sponsor_imgs = []
        self._sponsor_qr_labels = []

        body = ttk.Frame(win, padding=(20, 16, 20, 14))
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text=t("☕ 请我喝一杯"),
                  font=FONT_BOLD_LG).pack(anchor=tk.CENTER)

        ttk.Button(body, text=t("打开 Ko-fi 赞助页面"), style="Accent.TButton",
                   command=self._open_kofi).pack(anchor=tk.CENTER, pady=(12, 14))

        # 弹窗里**不放** Ko-fi 地址：蓝按钮点一下就直接开浏览器了，再摆一行地址纯属多余
        # （用户口径：2026-09-25）。地址并没有丢：_open_kofi 打不开浏览器时会把它写进状态栏。

        # 两张收款码并排，各带文字标签，码与码之间留间距
        qr_row = ttk.Frame(body)
        qr_row.pack(anchor=tk.CENTER)
        for label, path in _sponsor_qr_specs():
            cell = ttk.Frame(qr_row)
            cell.pack(side=tk.LEFT, padx=14)
            self._load_qr(cell, path).pack(anchor=tk.CENTER)
            ttk.Label(cell, text=label, style="Dim.TLabel").pack(anchor=tk.CENTER,
                                                                 pady=(6, 0))

        ttk.Label(body, text=t("扫码支持 · 你给的钱会变成 API token，然后被我烧掉"),
                  style="Dim.TLabel").pack(anchor=tk.CENTER, pady=(14, 4))
        ttk.Button(body, text=t("关闭"), width=_char_width_for(t("关闭"), FONT_UI, 8),
                   command=self._close_sponsor).pack(anchor=tk.CENTER, pady=(8, 0))

        # 定位到主窗口附近 + 深色标题栏（与设置弹窗同一套做法）
        win.update_idletasks()
        rx, ry = self._root.winfo_x(), self._root.winfo_y()
        rw = self._root.winfo_width()
        ww = win.winfo_reqwidth()
        win.geometry(f"+{rx + max((rw - ww) // 2, 20)}+{ry + 48}")
        self._apply_dark_titlebar(win)

    def _load_qr(self, parent, path: Path):  # noqa: ANN001
        """等比缩放加载一张收款码；图片缺失/加载失败时降级成一行文字 + WARN 日志，
        绝不抛异常 —— 辅助功能挂掉不能拖垮主窗口。"""
        try:
            from PIL import Image, ImageTk

            if not path.exists():
                raise FileNotFoundError(path)
            im = Image.open(path)
            im.thumbnail((SPONSOR_QR_SIZE, SPONSOR_QR_SIZE), Image.LANCZOS)  # 等比，绝不拉伸
            photo = ImageTk.PhotoImage(im)
            self._sponsor_imgs.append(photo)      # 留引用防 GC
            lbl = tk.Label(parent, image=photo, bg=PANEL, bd=0,
                           highlightthickness=1, highlightbackground=BORDER)
            self._sponsor_qr_labels.append(lbl)
            return lbl
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] ⚠️ 收款码加载失败（已降级为文字提示）：{path} "
                  f"→ {type(exc).__name__}: {exc}", flush=True)
            return ttk.Label(parent, text=t("二维码图片缺失"), style="Dim.TLabel")

    def _close_sponsor(self) -> None:
        win, self._sponsor_win = self._sponsor_win, None
        self._sponsor_imgs = []
        self._sponsor_qr_labels = []
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 更新检查（启动自动 + 设置里手动）
    def _schedule_update_check(self, manual: bool = False) -> None:
        """后台线程检查更新。manual=False=启动自动检查（一次会话一次）；
        manual=True=设置弹窗里点的（不受一次限制，但防连点：进行中再点直接返回）。
        headless 自检模式直接跳过（不起网络线程）。"""
        if self._headless or self._update_check_running:
            return
        if not manual:
            if self._update_check_done:
                return
            self._update_check_done = True
        self._update_check_running = True
        if manual and hasattr(self, "_update_check_btn"):
            self._update_check_btn.state(["disabled"])
            self._update_check_btn.configure(text=t("检查中…"))
            self._update_info.configure(text=t("正在检查更新…"))
        threading.Thread(target=self._run_update_check, args=(manual,), daemon=True).start()

    def _run_update_check(self, manual: bool) -> None:
        """工作线程体：调 check_for_updates，结果塞 self._q 回主线程。
        顶层 except 吞掉一切并留 [update] 日志 —— 检查更新绝不允许影响翻译主流程。"""
        err = None
        try:
            status, info = self._update_checker(__version__, DEFAULT_CONFIG)
        except Exception as exc:  # noqa: BLE001 — 失败只留痕，绝不甩给启动/翻译流程
            status, info = "error", None
            err = f"{type(exc).__name__}: {exc}"
            print(f"[update] 检查失败：{err}", flush=True)
        self._q.put(("update_check", status, info, manual, err))

    def _on_update_check_result(self, status: str, info, manual: bool, err: str | None) -> None:
        """主线程（_poll 分支）：有更新 → 弹三按钮窗；其余只留痕/更新设置区标签，不弹窗。"""
        self._update_check_running = False
        if hasattr(self, "_update_check_btn"):
            self._update_check_btn.state(["!disabled"])
            self._update_check_btn.configure(text=t("检查更新"))
        if status == "update" and info is not None:
            # GUI 入口复核忽略列表（双保险：check_for_updates 判过一次，但测试/手动路径
            # 可能绕过它直接给结果 —— 点过「不再提示这个版本」的，说什么也不再弹）
            if info.version in update_check.load_ignored_versions(DEFAULT_CONFIG):
                print(f"[update] v{info.version} 在忽略列表，跳过（不弹窗）", flush=True)
                if manual:
                    self._update_info.configure(
                        text=t("v{ver} 已设为「不再提示这个版本」", ver=info.version))
                return
            if self._update_snoozed and not manual:
                print("[update] 本会话已选「下次再说」，自动提示不再弹（设置里可手动检查）",
                      flush=True)
                return
            self._show_update_dialog(info)
            print("[update] 已弹出「发现新版本」提示窗", flush=True)
            if manual:
                self._update_info.configure(
                    text=t("发现新版本 v{new}（当前 v{cur}）",
                           new=info.version, cur=__version__))
        elif status == "latest" and info is not None:
            if manual:
                self._update_info.configure(text=t("已是最新 v{ver} ✅", ver=info.version))
        elif status == "ignored" and info is not None:
            if manual:
                self._update_info.configure(
                    text=t("v{ver} 已设为「不再提示这个版本」", ver=info.version))
        else:  # error：自动检查对用户完全无感（只留日志）；手动检查把原因显示在设置区
            if manual:
                reason = err or t("原因见日志")
                self._update_info.configure(
                    text=t("检查失败：{reason}。可以点「检查更新」重试。", reason=reason))

    # ---------------------------------------------------------------- 三按钮弹窗（发现新版本）
    def _show_update_dialog(self, info) -> None:
        """懒建 Toplevel（非模态：transient + lift，不 grab_set —— 翻译不能被卡住）。
        文案逐字照抄计划「文案 checklist ①」。已存在弹窗时先销毁再建（防重复）。"""
        self._close_update_dialog()
        win = tk.Toplevel(self._root)
        win.title(t("发现新版本"))
        win.configure(bg=PANEL)
        win.transient(self._root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._on_update_later)   # 右上角 X = 下次再说
        win.bind("<Escape>", lambda _e: self._on_update_later())
        self._update_win = win

        body = ttk.Frame(win, padding=(20, 16, 20, 14))
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=t("发现新版本"),
                  font=FONT_BOLD_MD).pack(anchor=tk.W)
        ttk.Label(body,
                  text=t("VRChat Live Translate 有新版本了。"
                         "现在更新只要一两分钟，不影响你正在进行的翻译。"),
                  wraplength=380, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))
        link = tk.Label(body, text=t("看看这次更新了什么"), fg=ACCENT_HOVER, bg=PANEL,
                        cursor="hand2", font=FONT_UI)
        link.pack(anchor=tk.W, pady=(8, 0))
        link.bind("<Button-1>", lambda _e: self._open_release_page(info.html_url))

        btns = ttk.Frame(body)
        btns.pack(fill=tk.X, pady=(16, 0))
        now = ttk.Button(btns, text=t("立即更新"), style="Accent.TButton",
                         command=lambda: self._on_update_now(info))
        now.pack(side=tk.LEFT)
        ttk.Button(btns, text=t("下次再说"),
                   command=self._on_update_later).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btns, text=t("不再提示这个版本"),
                   command=lambda: self._on_update_ignore(info)).pack(side=tk.LEFT,
                                                                       padx=(8, 0))
        now.focus_set()                       # 默认按钮：回车/焦点都落在「立即更新」

        win.update_idletasks()
        rx, ry = self._root.winfo_x(), self._root.winfo_y()
        rw = self._root.winfo_width()
        win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 60}")
        self._apply_dark_titlebar(win)
        win.lift()

    def _close_update_dialog(self) -> None:
        win, self._update_win = self._update_win, None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _open_release_page(self, url: str) -> None:
        """用默认浏览器打开 Release 页（「看看这次更新了什么」）；失败只留痕 + 状态栏提示。"""
        try:
            webbrowser.open(url)
            print(f"[update] 已打开 Release 页面 {url}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[update] ⚠️ 打不开浏览器：{type(exc).__name__}: {exc}", flush=True)
            self._set_status("warn", t("打不开浏览器，请手动访问 {url}", url=url))

    def _on_update_ignore(self, info) -> None:
        """「不再提示这个版本」→ 写 config.yaml 的 ui.update_ignored，关窗，该版本永不再提。"""
        try:
            update_check.add_ignored_version(DEFAULT_CONFIG, info.version)
            print(f"[update] v{info.version} 已写入忽略列表（config.yaml ui.update_ignored），"
                  f"这个版本不再提示", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[update] ⚠️ 写入忽略列表失败：{type(exc).__name__}: {exc}", flush=True)
            self._set_status("warn", t("「不再提示」没存下来：{msg}", msg=exc))
        self._close_update_dialog()

    def _on_update_later(self) -> None:
        """「下次再说」→ 本会话不再自动弹（不落盘），下次启动照常检查。"""
        self._update_snoozed = True
        print("[update] 用户选择「下次再说」：本会话不再自动弹（不落盘，下次启动照查）",
              flush=True)
        self._close_update_dialog()

    def _on_update_now(self, info) -> None:
        """「立即更新」：源码运行给指引（不自更新）；打包 exe / AppImage 走两段式 —— 先开下载进度窗。"""
        if not update_check.can_self_update():
            open_page = messagebox.askokcancel(
                t("如何更新"),
                t("你现在运行的是源码版，不能自动更新。\n\n"
                  "· 会用 git：在仓库目录跑 git pull 就是最新版；\n"
                  "· 或者点「确定」打开新版本下载页，下载安装包。\n\n"
                  "点「取消」先不更新。"),
                parent=self._update_win)
            if open_page:
                self._open_release_page(info.html_url)
            print("[update] 源码运行：已给出更新指引（git pull / 下载页），不做自更新",
                  flush=True)
            return
        target = update_check.update_target_path()
        if target is None or not _dir_writable(target.parent):
            open_page = messagebox.askokcancel(
                t("无法自动更新"),
                t("程序所在的位置不允许写入（比如放在 Program Files）。\n\n"
                  "点「确定」打开下载页，自己下载新版本；点「取消」先不更新。"),
                parent=self._update_win)
            if open_page:
                self._open_release_page(info.html_url)
            print(f"[update] 安装目录不可写（{target.parent if target else '?'}），"
                  f"转为手动下载指引", flush=True)
            return
        # 这个版本之前已经下载好（点过「稍后更新」/上次没换完就被关掉）→
        # 复验通过直接给更新入口，30MB+ 不白下（硬要求：用残留前必须重新校验）
        try:
            hit = update_check.check_pending_download(target, __version__)
        except Exception as exc:  # noqa: BLE001
            hit = None
            print(f"[update] ⚠️ 待更新文件复核失败：{type(exc).__name__}: {exc}"
                  f"（按重新下载处理）", flush=True)
        if hit is not None and hit[0] == info.version:
            self._close_update_dialog()
            self._show_download_window(info)
            self._enter_download_done_state(update_check.pending_new_asset(target))
            return
        self._close_update_dialog()
        self._show_download_window(info)
        self._start_download(info)

    # ---------------------------------------------------------------- 下载进度窗（两段式之一：只下载）
    def _show_download_window(self, info) -> None:
        """懒建懒销毁 Toplevel（transient + lift，非模态 —— 下载期间翻译照跑）。
        文案逐字照抄「文案 checklist ②」；完成后切「完成态」（checklist ③）。
        总量优先级：Content-Length（回调带）> ReleaseInfo.asset_size > indeterminate 只显示已下载量。"""
        self._close_download_window()
        win = tk.Toplevel(self._root)
        win.title(t("正在下载新版本"))
        win.configure(bg=PANEL)
        win.transient(self._root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._on_download_window_close)  # 关窗 = 取消下载
        self._dl_win = win
        self._dl_info = info
        self._dl_new_exe = None

        body = ttk.Frame(win, padding=(20, 16, 20, 14))
        body.pack(fill=tk.BOTH, expand=True)
        self._dl_bar = ttk.Progressbar(body, mode="determinate", length=380,
                                       maximum=100.0, value=0.0)
        self._dl_bar.pack(fill=tk.X)
        if info.asset_size:
            text = t("已下载 {done} / 约 {total} MB，一般 1–3 分钟就好。下载期间可以正常翻译。",
                     done="0.0", total=f"{info.asset_size / 1048576:.1f}")
        else:
            self._dl_bar.configure(mode="indeterminate")
            self._dl_bar.start(14)
            text = t("已下载 {done} MB，请稍等。", done="0.0")
        self._dl_text = ttk.Label(body, text=text, wraplength=380, justify=tk.LEFT)
        self._dl_text.pack(anchor=tk.W, pady=(10, 0))
        self._dl_note = ttk.Label(body, text=t("点右上角关闭会取消下载，下次可以再下。"),
                                  style="Muted.TLabel")
        self._dl_note.pack(anchor=tk.W, pady=(10, 0))
        # 「打开下载页自己下」：下载失败/完成态都能点到的第三条出路（网络实在不稳时自己下）
        self._dl_link = tk.Label(body, text=t("打开下载页自己下"), fg=ACCENT_HOVER, bg=PANEL,
                                 cursor="hand2", font=FONT_UI)
        self._dl_link.pack(anchor=tk.W, pady=(6, 0))
        self._dl_link.bind("<Button-1>", lambda _e: self._open_download_page())
        self._dl_btn_frame = ttk.Frame(body)   # 完成态才放按钮（checklist ③）
        self._dl_btn_frame.pack(fill=tk.X, pady=(14, 0))

        win.update_idletasks()
        rx, ry = self._root.winfo_x(), self._root.winfo_y()
        rw = self._root.winfo_width()
        win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 80}")
        self._apply_dark_titlebar(win)
        win.lift()

    def _close_download_window(self) -> None:
        win, self._dl_win = self._dl_win, None
        self._dl_bar = self._dl_text = self._dl_note = self._dl_btn_frame = None
        self._dl_reload_btn = self._dl_postpone_btn = self._dl_link = None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _open_download_page(self) -> None:
        """下载窗里的「打开下载页自己下」（与三按钮弹窗的链接同一写法）。"""
        url = self._dl_info.html_url if self._dl_info else ""
        if url:
            self._open_release_page(url)

    def _on_download_window_close(self) -> None:
        """下载中关窗 = 取消下载并清理残留（下载线程在下一个 chunk 边界自己中断）。"""
        if self._dl_downloading and self._dl_cancel is not None:
            self._dl_cancel.set()
            print("[update] 用户关闭下载窗：取消下载", flush=True)
        elif self._dl_new_exe is not None:
            print("[update] 下载窗已关闭：下载好的新版本文件保留，下次可继续用", flush=True)
        self._close_download_window()

    def _start_download(self, info) -> None:
        """守护线程跑 download_and_verify。线程纪律（硬约束）：progress 回调绝不直接
        碰 Tk 控件，只节流后（每 ~100ms 至多一条，允许丢旧留新）塞 self._q，
        由 _poll() 在主线程更新控件 —— 与设备扫描同一模式。"""
        self._dl_cancel = threading.Event()
        self._dl_last_push = 0.0
        self._dl_downloading = True
        # 安装文件（exe / AppImage）同目录：同卷 rename 才近原子。拿不到目标（不该发生：
        # 能走到这儿说明 can_self_update() 为真）就退回 APP_DIR，绝不让它崩在 None 上。
        target = update_check.update_target_path() or (APP_DIR / update_check.asset_name_for())
        dest_dir = target.parent

        def _progress(done: int, total: int | None) -> None:
            if self._dl_cancel is not None and self._dl_cancel.is_set():
                raise _DownloadCancelled()
            now = time.monotonic()
            is_final = bool(total) and done >= total
            if not is_final and now - self._dl_last_push < self._dl_throttle_s:
                return
            self._dl_last_push = now
            self._q.put(("update_progress", done, total))

        def _work() -> None:
            try:
                new_exe = self._update_downloader(info, dest_dir, progress=_progress)
            except _DownloadCancelled:
                print("[update] 下载已取消（残留已清理）", flush=True)
                return
            except Exception as exc:  # noqa: BLE001 — 失败走统一错误分支，绝不静默
                self._q.put(("update_download_error", str(exc)))
                return
            self._q.put(("update_download_done", new_exe))

        threading.Thread(target=_work, daemon=True).start()

    def _on_download_progress(self, done: int, total: int | None) -> None:
        """主线程：更新进度条与文本；total=None 且 asset_size 也没有 → indeterminate。"""
        if self._dl_win is None or self._dl_bar is None or self._dl_text is None:
            return
        try:
            if not self._dl_win.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        total = total or (self._dl_info.asset_size if self._dl_info else None)
        if total:
            if str(self._dl_bar.cget("mode")) != "determinate":
                self._dl_bar.stop()
                self._dl_bar.configure(mode="determinate")
            self._dl_bar.configure(maximum=float(total), value=float(done))
            self._dl_text.configure(
                text=t("已下载 {done} / 约 {total} MB，一般 1–3 分钟就好。下载期间可以正常翻译。",
                       done=f"{done / 1048576:.1f}", total=f"{total / 1048576:.1f}"))
        else:
            if str(self._dl_bar.cget("mode")) != "indeterminate":
                self._dl_bar.configure(mode="indeterminate")
                self._dl_bar.start(14)
            self._dl_text.configure(
                text=t("已下载 {done} MB，请稍等。", done=f"{done / 1048576:.1f}"))

    def _on_download_done(self, new_exe) -> None:
        """主线程：下载+校验完成 → 切「完成态」（文案 checklist ③）。"""
        self._dl_downloading = False
        if self._dl_win is None:
            # 用户在最后一刻关了窗：按取消处理，不留来路不明的下载物（json 一并清）
            try:
                Path(new_exe).unlink(missing_ok=True)
                (Path(new_exe).parent / update_check.PENDING_JSON).unlink(missing_ok=True)
            except OSError:
                pass
            print("[update] 下载完成时窗口已关闭：按取消处理，下载物已删除", flush=True)
            return
        self._enter_download_done_state(Path(new_exe))

    def _enter_download_done_state(self, new_exe: Path) -> None:
        """下载窗切「完成态」（文案 checklist ③）：进度条 100%，按钮区出
        [立即重启并更新] [稍后更新]。下载完成与残留恢复（不重复下载）共用。"""
        self._dl_new_exe = Path(new_exe)
        self._dl_bar.stop()
        self._dl_bar.configure(mode="determinate", maximum=1.0, value=1.0)
        self._dl_win.title(t("下载完成"))
        self._dl_text.configure(
            text=t("新版本已经准备好了。\n"
                   "点「立即重启并更新」：关闭当前窗口、自动换上新版本并重新打开。\n"
                   "点「稍后更新」：继续用现在的版本；等你关闭程序时会自动换好，下次打开就是新版。"))
        self._dl_note.pack_forget()            # 已完成：「关窗会取消」的小字不再适用
        for child in self._dl_btn_frame.winfo_children():   # 防重复进完成态时叠按钮
            child.destroy()
        self._dl_reload_btn = ttk.Button(self._dl_btn_frame, text=t("立即重启并更新"),
                                         style="Accent.TButton",
                                         command=self._on_reload_clicked)
        self._dl_reload_btn.pack(side=tk.LEFT)
        self._dl_postpone_btn = ttk.Button(self._dl_btn_frame, text=t("稍后更新"),
                                           command=self._on_postpone_clicked)
        self._dl_postpone_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._dl_reload_btn.focus_set()
        print(f"[update] 新版本已下载好（{new_exe}），等待用户选择何时更新", flush=True)

    def _on_download_error(self, msg: str) -> None:
        """主线程：清理残留 → 留痕 → 错误提示带可点下一步（重试=默认 / 取消=暂时跳过）。"""
        self._dl_downloading = False
        print(f"[update] 下载失败：{msg}", flush=True)
        if self._dl_win is None:
            return                               # 用户已关窗取消，错误不必再烦他
        # 残留双保险（download_and_verify 失败时已清过一遍）
        target = update_check.update_target_path()
        if target is None:
            target = APP_DIR / update_check.asset_name_for()
        try:
            update_check.pending_new_asset(target).unlink(missing_ok=True)
        except OSError:
            pass
        retry = messagebox.askretrycancel(
            t("下载没有成功"),
            t("下载没有成功（网络可能不太稳定）。\n\n"
              "点「重试」再下载一次；点「取消」暂时跳过。\n"
              "（之后也可以到「设置 → 软件更新」再检查）"),
            parent=self._dl_win)
        if retry and self._dl_win is not None and self._dl_info is not None:
            self._dl_bar.configure(mode="determinate", maximum=100.0, value=0.0)
            self._dl_text.configure(text=t("已下载 {done} MB，请稍等。", done="0.0"))
            print("[update] 用户选择重试下载", flush=True)
            self._start_download(self._dl_info)
        else:
            print("[update] 用户选择暂时跳过本次下载", flush=True)
            self._close_download_window()

    # ---------------------------------------------------------------- 阶段二：重载 / 稍后 / 退出时替换
    #
    # 三条出口分工（防互相打架，改这里前先想清楚是哪一条）：
    #   【立即重启并更新】= 立刻替换 + 自动拉起新版（当前会话结束）
    #   【稍后更新】      = 退出时替换 + 不拉起（下次用户自己开，开起来就是新版 + 一次性提示）
    #   【不再提示这个版本】= 只对这个版本号不再提示，与上面两条无关

    def _on_reload_clicked(self) -> None:
        """「立即重启并更新」= 立刻替换 + 自动拉起新版（当前会话结束）。

        Windows：生成 bat（relaunch=True）→ 分离启动 → 走正常退出流程。
        AppImage：**直接换**（`os.replace`，运行中的旧文件是旧 inode，不受影响）→ 拉起新文件。
        任一步失败：留痕 + 恢复按钮 + 错误提示带可点下一步，绝不静默。"""
        if self._reload_started:
            return                              # 防连点：已经安排上了
        new_exe, info = self._dl_new_exe, self._dl_info
        if new_exe is None or info is None:
            print("[update] ⚠️ 「立即重启并更新」在非完成态被触发，已忽略", flush=True)
            return
        self._reload_started = True
        for btn in (self._dl_reload_btn, self._dl_postpone_btn):
            if btn is not None:
                try:
                    btn.state(["disabled"])
                except Exception:  # noqa: BLE001
                    pass
        if self._dl_reload_btn is not None:
            self._dl_reload_btn.configure(text=t("正在重启…"))
        try:
            if update_check.update_mode() == "appimage":
                target = update_check.update_target_path()
                if target is None:
                    raise update_check.UpdateCheckError(
                        "找不到正在运行的 AppImage 文件（$APPIMAGE 没了？）")
                update_check.install_appimage(new_exe, target)
                self._relaunch_appimage(target)
            else:
                exe = Path(sys.executable).resolve()
                bat = update_check.build_updater_bat(pid=os.getpid(), current_exe=exe,
                                                     new_exe=new_exe, relaunch=True)
                self._launch_updater_bat(bat)
        except Exception as exc:  # noqa: BLE001 — 失败必须被用户看到，不许静默
            print(f"[update] ⚠️ 启动更新器失败：{type(exc).__name__}: {exc}"
                  f"（下载好的新版本保留着，可以再点）", flush=True)
            self._reload_started = False
            for btn in (self._dl_reload_btn, self._dl_postpone_btn):
                if btn is not None:
                    try:
                        btn.state(["!disabled"])
                    except Exception:  # noqa: BLE001
                        pass
            if self._dl_reload_btn is not None:
                self._dl_reload_btn.configure(text=t("立即重启并更新"))
            retry = messagebox.askretrycancel(
                t("更新没有成功"),
                t("更新没有成功，现在的版本不受影响，可以继续用。\n\n"
                  "点「重试」再试一次；点「取消」先继续用现在的版本"
                  "（窗口里也可以「打开下载页自己下」）。"),
                parent=self._dl_win)
            if retry:
                print("[update] 用户选择重试「立即重启并更新」", flush=True)
                self._on_reload_clicked()
            else:
                print("[update] 用户选择先继续用现在的版本（新版本已下载好，保留着）",
                      flush=True)
            return
        print(f"[update] 已安排替换并拉起新版，程序即将退出（重载路径，v{__version__} → "
              f"v{info.version}）", flush=True)
        self._on_close()

    def _on_postpone_clicked(self) -> None:
        """「稍后更新」= 继续用旧版、关下载窗；已下载且校验通过的新版本保留着（不删），
        正常退出时自动换好（绝不自动拉起）、下次打开就是新版。"""
        info = self._dl_info
        if info is None:
            return
        self._mark_update_pending(info)
        print(f"[update] 已下载 v{info.version}，将在退出时完成更新（不自动打开）", flush=True)
        self._close_download_window()

    def _mark_update_pending(self, info) -> None:
        """记下「正常退出时要换成新版本」（稍后更新 / 启动残留命中共用）。"""
        self._update_pending_exit = True
        self._update_pending_info = info

    def _launch_updater_bat(self, bat_text: str) -> Path:
        """把更新器脚本写到程序同目录并分离启动（不等它跑完；它自己会等本程序退出再动手）。
        失败（权限/磁盘/没有 cmd）抛异常给调用方 —— 由调用方负责让用户看见。"""
        exe = Path(sys.executable).resolve()
        bat_path = exe.parent / "_update.bat"
        # bat 正文已是 CRLF：newline="" 关掉写入时的换行翻译，否则 \r\n 会变 \r\r\n
        bat_path.write_text(bat_text, encoding="ascii", newline="")
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
        subprocess.Popen(["cmd", "/c", "start", "", "/min", str(bat_path)],
                         creationflags=flags, env=updater_env())
        return bat_path

    def _relaunch_appimage(self, appimage: Path) -> None:
        """分离启动刚换上的 AppImage（当前会话随后正常退出，不等待它结束）。

        ⚠️ 必须用 `updater_env()`（它会剥掉 APPIMAGE / APPDIR / OWD / ARGV0）：AppImage
        运行时看到 `APPDIR` 已设就**不会重新挂载**，新进程会去用父进程那个马上要消失的
        挂载点 —— 与 Windows 侧漏清 `_MEI*` 是同一类「更新完没再打开」。
        cwd 落在 AppImage 自己所在目录：绝不能是旧挂载点里的路径（那会随进程一起消失）。
        """
        subprocess.Popen([str(appimage)], env=updater_env(), start_new_session=True,
                         cwd=str(Path(appimage).parent))

    def _maybe_replace_on_exit(self) -> None:
        """正常退出时替换（【稍后】路径的另一半）：复验 → 替换 → 退出（绝不拉起新进程，下次
        用户自己打开就是新版）。Windows 走 bat（relaunch=False）；AppImage 直接 rename 顶替。"""
        if self._reload_started:
            return                    # 重载路径已安排了带拉起的替换，别重复安排
        if not self._update_pending_exit:
            return
        if not update_check.can_self_update():
            return                    # 源码运行不做自更新（正常也走不到这）
        target = update_check.update_target_path()
        if target is None:
            return
        try:
            # 硬要求：退出前再复验一次（防下载后文件被改坏/杀软动过），不通过就不换
            hit = update_check.check_pending_download(target, __version__)
        except Exception as exc:  # noqa: BLE001
            print(f"[update] 跳过退出时替换（复核异常：{type(exc).__name__}: {exc}）",
                  flush=True)
            return
        if hit is None:
            print("[update] 跳过退出时替换（待更新文件复核未通过，残留已按规则清理）",
                  flush=True)
            return
        version = hit[0]
        try:
            if update_check.update_mode() == "appimage":
                update_check.install_appimage(update_check.pending_new_asset(target), target)
            else:
                bat = update_check.build_updater_bat(
                    pid=os.getpid(), current_exe=target,
                    new_exe=update_check.pending_new_asset(target), relaunch=False)
                self._launch_updater_bat(bat)
        except Exception as exc:  # noqa: BLE001 — 失败要被用户看到，不能静默退出
            print(f"[update] ⚠️ 退出时替换安排失败：{type(exc).__name__}: {exc}"
                  f"（新版本已下载好并保留，下次启动会再给更新入口）", flush=True)
            self._offer_manual_download(
                t("更新没有成功，现在的版本不受影响，下次打开还是它。\n\n"
                  "点「确定」打开下载页自己下；点「取消」直接退出。"))
            return
        done = ("已就地替换" if update_check.update_mode() == "appimage" else "已安排替换")
        print(f"[update] 退出时{done}（不自动拉起），下次打开就是 v{version}", flush=True)

    def _offer_manual_download(self, body: str) -> None:
        """自动替换没成功时的统一退路：问一句 →「确定」就打开下载页自己下。

        `body` 是给用户看的话（各调用点自己写，措辞必须讲清「现在还能继续用」）。
        """
        info = self._update_pending_info
        open_page = messagebox.askokcancel(t("更新没有成功"), body, parent=self._root)
        if open_page and info is not None and info.html_url:
            self._open_release_page(info.html_url)

    # ---------------------------------------------------------------- 启动兜底与一次性提示

    def _check_pending_update_at_startup(self) -> None:
        """启动兜底：上次下载好了新版本但没来得及换（被强杀/直接关机）→
        重新复验残留，完好就直接出「下载完成」窗口给更新入口（不重复下载），
        并记下退出时替换；损坏/半截 → check_pending_download 内部已清理 + 留痕。"""
        if self._headless or not update_check.can_self_update():
            return
        target = update_check.update_target_path()
        if target is None:
            return
        try:
            hit = update_check.check_pending_download(target, __version__)
        except Exception as exc:  # noqa: BLE001
            print(f"[update] ⚠️ 待更新文件复核失败：{type(exc).__name__}: {exc}", flush=True)
            return
        if hit is None:
            return
        version = hit[0]
        info = update_check.ReleaseInfo(
            tag=f"v{version}", version=version,
            html_url=f"{update_check.RELEASES_HTML}/tag/v{version}",
            asset_url="", asset_name=update_check.asset_name_for(),
            sums_url="", asset_size=None)
        self._mark_update_pending(info)
        self._show_download_window(info)
        self._enter_download_done_state(update_check.pending_new_asset(target))

    def _schedule_version_changed_hint(self) -> None:
        """启动版本提示：上次运行版本 ≠ 本次（刚完成过替换/升级）→ ~1.5 秒后弹一次性
        「已更新」小提示；首次运行只悄悄记下版本；版本没变 → 什么都不做。"""
        self._updated_hint_job = None
        if self._headless or not update_check.can_self_update():
            return
        last = update_check.load_last_seen_version(APP_DIR)
        if last is None:
            # 首次运行（或状态文件损坏）：只记录，不打扰
            update_check.save_last_seen_version(APP_DIR, __version__)
            return
        if last == __version__:
            return
        print(f"[update] 版本已从 v{last} 变为 v{__version__}：准备弹一次性「已更新」提示",
              flush=True)
        self._updated_hint_job = self._root.after(1500, self._show_version_changed_hint)

    def _show_version_changed_hint(self) -> None:
        """一次性、非模态、可秒关的「已更新到最新版本」小提示（逐字文案 checklist ④）。
        同一版本只弹一次：关闭时写回当前版本。版本号只进日志，不丢给普通用户。"""
        self._updated_hint_job = None
        if self._updated_hint_win is not None:
            return
        try:
            if not self._root.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        win = tk.Toplevel(self._root)
        win.title(t("已更新到最新版本"))
        win.configure(bg=PANEL)
        win.transient(self._root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._close_updated_hint)
        win.bind("<Escape>", lambda _e: self._close_updated_hint())
        self._updated_hint_win = win

        body = ttk.Frame(win, padding=(20, 16, 20, 14))
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=t("已更新到最新版本"),
                  font=FONT_BOLD_MD).pack(anchor=tk.W)
        ttk.Label(body, text=t("VRChat Live Translate 已更新到最新版本，一切照常使用。"),
                  wraplength=360, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))
        link = tk.Label(body, text=t("看看这次更新了什么"), fg=ACCENT_HOVER, bg=PANEL,
                        cursor="hand2", font=FONT_UI)
        link.pack(anchor=tk.W, pady=(8, 0))
        link.bind("<Button-1>", lambda _e: self._open_release_page(
            f"{update_check.RELEASES_HTML}/tag/v{__version__}"))
        ok = ttk.Button(body, text=t("知道了"), style="Accent.TButton",
                        command=self._close_updated_hint)
        ok.pack(anchor=tk.E, pady=(16, 0))
        ok.focus_set()

        win.update_idletasks()
        rx, ry = self._root.winfo_x(), self._root.winfo_y()
        rw = self._root.winfo_width()
        win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 60}")
        self._apply_dark_titlebar(win)
        win.lift()
        print("[update] 已弹出一次性「已更新到最新版本」提示", flush=True)

    def _close_updated_hint(self) -> None:
        """关闭「已更新」提示（右上角 X / 知道了 / Esc 共用）：写回当前版本，同一版本不再弹。"""
        win, self._updated_hint_win = self._updated_hint_win, None
        if win is None:
            return                              # 没弹过就什么都不做（别误写状态文件）
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass
        update_check.save_last_seen_version(APP_DIR, __version__)

    def _refresh_key_status(self) -> None:
        """只显示来源 + 打码值，绝不显示明文。

        写两处：设置弹窗里的完整状态（_key_status）+ 主界面第二行右侧的状态槽位
        ——已配置时显示纯展示标签（_key_chip），未配置时换成可点按钮（_key_btn，
        点击打开**当前线路**的开通页）；保存/清除 key 后本方法会被再次调用，界面立刻切换。

        ⚠️ key 按线路分槽（`_current_key_slot()`）：国内版与海外版各存一份、互不通用。所以这里
        读的是**当前线路那一槽**，切完线路再调一次，显示的就是另一把 key 的状态。
        """
        slot = self._current_key_slot()
        try:
            from .credentials import key_source

            src, masked = key_source(slot=slot)
        except Exception as exc:  # noqa: BLE001
            self._key_status.config(text=t("⚠️ 读取 key 状态失败：{msg}", msg=exc))
            return
        if masked:
            # 带上线路名：两条线路各有一把 key，不写清是哪条的话"我明明填了 key"
            # 就会变成"界面说没填" —— 那其实是另一条线路的槽空着。
            self._key_status.config(text=t("当前（{line}）：{src} {masked}",
                                           line=self._provider_label(), src=src, masked=masked))
        else:
            self._key_status.config(text=t("⚠️ 未配置 API key —— 在上面粘贴后点「保存」"))
        if hasattr(self, "_key_chip"):
            if masked:
                # 已配置：恢复纯展示标签（按钮收起，不残留）
                self._key_chip.configure(text=t("API key 已配置"), style="Chip.TLabel")
                if self._key_btn.winfo_manager():
                    self._key_btn.pack_forget()
                if not self._key_chip.winfo_manager():
                    self._key_chip.pack()
            else:
                # 未配置：换成可点按钮（跳转当前线路的开通页）
                # 文案档位：最小宽度 928 下能完整显示（实测见改动报告）。
                # 海外版那条**刻意更短**（"开通海外版" 而不是 "开通千问云·海外版"）：
                # 主界面这一行右侧还挤着四个输出勾选，写长了英文/俄语文案就会被裁。
                self._key_btn.configure(
                    text=(t("⚠ 未配置 API key · 点此开通千问云 ▸")
                          if self._provider() == endpoints.PROVIDER_QIANWEN
                          else t("⚠ 未配置 API key · 点此开通海外版 ▸")))
                if self._key_chip.winfo_manager():
                    self._key_chip.pack_forget()
                if not self._key_btn.winfo_manager():
                    self._key_btn.pack()

    def _open_qianwen_signup(self) -> None:
        """「未配置」状态按钮：用默认浏览器打开**当前线路**的开通页。

        函数名保留 `_open_qianwen_signup`（主界面按钮的 command 与既有测试都按它绑定），
        但地址已改为按线路取：千问云 → QIANWEN_SIGNUP_URL，千问云·海外版 → Qwen Cloud 首页。

        绝不抛异常、绝不影响主功能：失败时把链接写进状态栏让用户手动复制；
        成功/失败都打一行日志（**写清是哪条线路**，否则"打开了个不相干的页面"无从查起）。
        链接原文使用，不做任何解码/重组。
        """
        url = self._signup_url()
        line = endpoints.provider_name(self._provider())      # 日志恒用中文名
        try:
            ok = bool(webbrowser.open(url))
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[gui] ⚠ 打开浏览器失败（{exc}），请手动访问{line}开通页：{url}",
                  flush=True)
        else:
            if ok:
                print(f"[gui] 已在默认浏览器打开{line}开通页：{url}", flush=True)
            else:
                print(f"[gui] ⚠ webbrowser.open 返回 False，请手动访问{line}开通页：{url}",
                      flush=True)
        if not ok:
            self._set_status("warn", t("打不开浏览器，请手动复制访问：{url}", url=url))

    def _refresh_api_key_in_cfg(self) -> None:
        """按既有优先级链重新解析 API key 并写回 self._cfg.session_base["api_key"]。

        启动时 load_config() 解析出的 key 只是那一刻的快照；界面上保存/清除之后
        必须重解，否则「开始翻译」读的还是启动时那份（干净机器：保存了却报"还没配置"；
        有旧来源的机器：贴了新 key 却继续用旧的）。解析只走 config.load_api_key()，
        不自写第二套优先级；一个来源都没有时它会抛 SystemExit —— 这里置空串，
        绝不让异常冒到界面/主循环。留痕：来源 + 打码值，绝不打明文。
        """
        from .config import load_api_key
        from .credentials import key_source, mask_key

        slot = self._current_key_slot()
        try:
            key = load_api_key(slot=slot)
        except SystemExit:
            key = ""
        self._cfg.session_base["api_key"] = key
        if key:
            source, _ = key_source(slot=slot)
            print(f"[gui] API key 已刷新：线路={endpoints.provider_name(self._provider())} "
                  f"来源={source}（{mask_key(key)}）", flush=True)
        else:
            print(f"[gui] API key 已刷新：线路="
                  f"{endpoints.provider_name(self._provider())} 没有任何来源（尚未配置）",
                  flush=True)

    def _on_save_key(self) -> None:
        from .credentials import load_saved_key, mask_key, save_api_key

        slot = self._current_key_slot()       # 存进**当前线路那一槽**：两条线路的 key 不通用
        raw = self._key_var.get()
        try:
            path = save_api_key(raw, slot=slot)
        except ValueError as exc:
            self._key_var.set("")                      # 明文不留在界面上
            self._key_status.config(text=t("❌ 没保存：{msg}", msg=exc))
            self._set_status("error", t("API key 保存失败：{msg}", msg=exc))
            print(f"[gui] ❌ API key 保存失败：{exc}", flush=True)
            return
        self._key_var.set("")
        shown = mask_key(load_saved_key(slot) or "")
        self._refresh_key_status()
        self._refresh_api_key_in_cfg()      # 关键：写回内存快照，否则开始翻译还读启动时的旧值
        self._set_status("ok", t("API key 已保存（{shown}）", shown=shown))
        print(f"[gui] ✅ API key 已保存（线路={endpoints.provider_name(self._provider())}，"
              f"{shown}）→ {path}", flush=True)
        self._check_api_key()

    def _on_clear_key(self) -> None:
        from .credentials import clear_saved_key

        # 只清**当前线路那一槽**：另一条线路的 key 原样留着（切回去还能用）
        removed = clear_saved_key(slot=self._current_key_slot())
        self._refresh_key_status()
        self._refresh_api_key_in_cfg()      # 清除后可能回退到别的来源、也可能变空 —— 内存同步
        msg = t("已清除保存的 API key") if removed else t("本来就没有保存过 API key")
        self._set_status("info", msg)
        # 日志保持中文（诊断用），界面已走 i18n
        print(f"[gui] {'已清除保存的 API key' if removed else '本来就没有保存过 API key'}"
              f"（线路={endpoints.provider_name(self._provider())}）", flush=True)
        self._check_api_key()

    def _build_chat(self) -> None:
        chat_frame = ttk.Frame(self._root)
        # 与头部两行同一个 14px 左边距：左右边界对齐才有「一栏到底」的秩序感
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        # Treeview 不支持多行/换行，聊天气泡用 Canvas 手绘圆角矩形
        self._canvas = tk.Canvas(chat_frame, bg=BG, highlightthickness=1,
                                 highlightbackground=BORDER, highlightcolor=BORDER)
        self._vsb = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_canvas_scroll)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)

    def _build_input_row(self) -> None:
        """打字输入行：不想开麦时用键盘替代麦克风，回车即发。

        它替代的是**麦克风**，所以只在方向含「我说」时可用（没会话时置灰，
        省得用户按了回车却没反应、以为坏了）。
        """
        if not (self._cfg.text_input or {}).get("enabled", True):
            return                       # 配置里关掉了：整行不建（下面各处都有 hasattr 兜底）
        row = ttk.Frame(self._root, padding=(14, 8, 14, 6))
        row.pack(fill=tk.X)
        ttk.Label(row, text=t("打字:"), style="Dim.TLabel").pack(side=tk.LEFT)
        self._text_var = tk.StringVar()
        self._text_entry = ttk.Entry(row, textvariable=self._text_var, font=FONT_UI,
                                     style="Key.TEntry")   # 复用「键输入框」样式：底色=SURFACE，与按钮一致
        self._text_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        self._text_entry.bind("<Return>", self._on_text_enter)
        self._text_entry.bind("<Escape>", lambda _e: self._text_var.set(""))
        self._attach_edit_menu(self._text_entry)
        self._send_btn = ttk.Button(row, text=t("发送"),
                                    width=_char_width_for(t("发送"), FONT_UI, 8),
                                    command=self._send_typed)
        self._send_btn.pack(side=tk.LEFT)
        ttk.Label(row, text=t("回车发送 · Esc 清空"), style="Muted.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        self._set_text_input_enabled(False)

    def _set_text_input_enabled(self, on: bool) -> None:
        """没有「我说」方向的会话时置灰：打字替代的是麦克风，没腿就没输出面。"""
        if not hasattr(self, "_text_entry"):
            return
        for w in (self._text_entry, self._send_btn):
            w.state(["!disabled"] if on else ["disabled"])

    def _on_text_enter(self, _event=None) -> str:
        self._send_typed()
        return "break"                  # 吃掉回车：否则 Tk 会再响一声提示音

    def _send_typed(self) -> None:
        """把输入框里的文字交给「我说」那条腿翻译并送出（下游与说话完全一致）。"""
        if not hasattr(self, "_text_entry"):
            return
        text = self._text_var.get().strip()
        if not text:
            return
        targets = [e for e, dirn in zip(self._engines, self._engine_dirs) if dirn == "mine"]
        if not targets:
            self._set_status("warn", t("打字替代的是麦克风 —— 先点「开始翻译」，"
                                       "且方向要含「我说」"))
            return
        sent = sum(1 for e in targets if e.send_text(text))
        if sent:
            self._text_var.set("")      # 清空：肉眼确认已发出
            self._set_status("info", t("打字已送出（{n} 字），翻译中…", n=len(text)))
        else:
            self._set_status("warn", t("引擎还没就绪，稍后重试"))

    def _build_status(self) -> None:
        bar = ttk.Frame(self._root, padding=(14, 7))
        bar.pack(fill=tk.X)
        # 左侧：彩色圆点（连接状态）+ 最新一条状态消息；右侧放统计汇总——两者分开，
        # 否则"等待收尾"这类瞬时消息会把"已翻译 N 条 / 首增量 Xms"覆盖掉。
        self._status_dot = tk.Label(bar, text="●", bg=PANEL, fg=TEXT_MUTED,
                                    font=FONT_STATUS, bd=0)
        self._status_dot.pack(side=tk.LEFT, padx=(0, 6))
        self._status_label = ttk.Label(bar, text=t("就绪"), style="Status.TLabel")
        self._status_label.pack(side=tk.LEFT)
        self._stats_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self._stats_label.pack(side=tk.RIGHT)

    # ================================================================ 事件处理

    def _check_api_key(self) -> None:
        self._refresh_key_status()
        try:
            from .config import load_api_key
            load_api_key(slot=self._current_key_slot())     # 只认**当前线路那一槽**，别拿另一条的 key 充数
        except SystemExit as e:
            self._set_status("error", str(e))

    def _on_canvas_scroll(self, first: str, last: str) -> None:
        self._vsb.set(first, last)
        # 滚到底部才恢复自动跟随；用户手动往上翻时不抢
        self._auto_scroll = float(last) >= 0.99

    def _on_mousewheel(self, event) -> None:
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_canvas_configure(self, event=None) -> None:
        w = event.width if event is not None else self._canvas.winfo_width()
        if w <= 1 or abs(w - self._canvas_w) <= 2:
            return
        self._canvas_w = w
        # 拖拽窗口会连发 Configure：合并到 120ms 后一次性重排
        if self._relayout_job is not None:
            try:
                self._root.after_cancel(self._relayout_job)
            except Exception:
                pass
        self._relayout_job = self._root.after(120, self._redraw_all)

    def _on_direction_change(self) -> None:
        self._update_direction_langs()
        self._save_ui_state()      # 方向要记下来，下次启动恢复（用户明确要求）

    def _update_direction_langs(self) -> None:
        d = self._direction_var.get()
        a, b = self._lang_pair["source"], self._lang_pair["target"]
        if d == "theirs":
            # 别人说：源=对方语言（=我的目标），目标=我的语言（=我的源）
            src_code, tgt_code = b, a or "zh"
            self._source_combo.configure(values=[_lang_label(k) for k in TARGET_LANGS])
            self._target_combo.configure(values=[_lang_label(k) for k in TARGET_LANGS])
        else:
            src_code, tgt_code = a, b
            self._source_combo.configure(values=[_lang_label(k) for k in SOURCE_LANGS])
            self._target_combo.configure(values=[_lang_label(k) for k in TARGET_LANGS])
        self._source_combo.set(_lang_label(_source_name(src_code)))
        self._target_combo.set(_lang_label(_target_name(tgt_code)))

    def _on_lang_change(self, _event=None) -> None:
        d = self._direction_var.get()
        src_shown = _lang_key(self._source_combo.get(), SOURCE_LANGS if d != "theirs" else TARGET_LANGS)
        tgt_shown = _lang_key(self._target_combo.get(), TARGET_LANGS)
        if d == "theirs":
            self._lang_pair["target"] = TARGET_LANGS.get(src_shown,
                                                         self._lang_pair["target"])
            self._lang_pair["source"] = TARGET_LANGS.get(tgt_shown)
        else:
            self._lang_pair["source"] = SOURCE_LANGS.get(src_shown)
            self._lang_pair["target"] = TARGET_LANGS.get(tgt_shown,
                                                         self._lang_pair["target"])
        if self._lang_pair["source"] is None:
            # 自动检测没有对应目标：别人说方向的目标回落到中文
            self._set_status("info",
                             t("已切换为{target} → 中文",
                               target=_lang_label(_target_name(self._lang_pair["target"]))))
        self._save_lang_config()
        self._push_lang_to_engines()
        self._publisher.reset()      # 切语言：封掉半句旧文本，别把上一语言的残句发进房间
        self._update_direction_langs()

    def _save_lang_config(self) -> None:
        """把互为镜像的两组方向写回 config.yaml（mine: A→B / theirs: B→A）。

        就地改文本，不整文件重写（原因见 `_yaml_set_in_text`）。
        """
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        pairs = {
            "mine": {"source_lang": a, "target_lang": b},
            "theirs": {"source_lang": b, "target_lang": a or "zh"},
        }
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            for name, langs in pairs.items():
                for key, val in langs.items():
                    text = _yaml_set_in_text(text, ["directions", name, key], _fmt_scalar(val))
            _write_config_text(p, text)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存配置失败：{exc}", flush=True)

    def _save_ui_state(self) -> None:
        """把界面上的选择（方向 / 输出勾选）写回 config.yaml 的 `ui:` 段，下次启动恢复。

        用户明确要求「方向这个东西是需要保存的」——之前每次启动都会重置成「我说的话」。
        段不存在时补建：老 config.yaml（从旧模板生成）里没有 ui 段。
        """
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            if not re.search(r"^ui:", text, re.M):
                text = text.rstrip("\n") + "\n\n# 界面上次的选择（启动时自动恢复，不用手改）\nui:\n"
            updates = [
                (["ui", "direction"], self._direction_var.get()),
                (["ui", "chatbox"], _fmt_scalar(bool(self._chatbox_var.get()))),
                (["ui", "overlay"], _fmt_scalar(bool(self._overlay_var.get()))),
                (["ui", "desktop_overlay"], _fmt_scalar(bool(self._desktop_var.get()))),
            ]
            for key_path, val in updates:
                text = _yaml_set_in_text(text, key_path, val)
            _write_config_text(p, text)
            print(f"[gui] 界面选择已保存：direction={updates[0][1]} chatbox={updates[1][1]} "
                  f"overlay={updates[2][1]} desktop={updates[3][1]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存界面选择失败：{exc}", flush=True)

    def _save_audio_flag(self) -> None:
        """把译音输出总开关写回 config.yaml（output.audio.enabled），勾了就一直记住。

        就地改一行（`_yaml_set_in_text`），不整文件重写 —— 保住注释与键顺序。
        """
        want = bool(self._vmic_var.get())
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            text = _yaml_set_in_text(text, ["output", "audio", "enabled"], _fmt_scalar(want))
            _write_config_text(p, text)
            print(f"[gui] 译音输出总开关 → {'开' if want else '关'}（已写入 config.yaml）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存译音开关失败：{exc}", flush=True)

    # ---------------------------------------------------------------- 音色
    def _effective_speech_voice(self) -> str:
        """「说话译音」现在**真正生效**的音色 —— 与 `Direction.to_session_config` 同口径：
        `directions.mine.voice` > `session.voice` > Tina。

        为什么看 mine：译音回灌只对「我说的话」那条腿有意义（theirs 的译文是我自己的
        母语，不需要出声），所以下拉里必须显 mine 那条腿实际会用到的那个值 ——
        显一个被方向级覆盖挡掉的值，用户只会觉得「改了没用」。
        """
        d = (self._cfg.directions or {}).get("mine")
        over = (getattr(d, "voice", "") or "") if d is not None else ""
        base = (self._cfg.session_base or {}).get("voice") or ""
        return str(over or base or "Tina")

    def _on_speech_voice_change(self, _event=None) -> None:
        """「说话译音」音色：写 session.voice 并同步内存（引擎重建会话时读到新值）。

        说话译音是会话创建时定下的，改完不会立刻变声 —— 明确告知「下次开始翻译生效」，
        不静默（用户最容易在这里误以为「改了没用」）。连接预算有限，不主动强制重连。
        """
        voice = self._speech_voice_var.get().strip()
        if not voice:
            return
        self._set_voice_config(voice)
        if isinstance(self._cfg.session_base, dict):
            self._cfg.session_base["voice"] = voice
        # 方向级覆盖比 session.voice 优先：不同步它就会做成一个「改了没反应」的下拉，
        # 所以一并改掉，并在状态栏说清还动了哪里。
        synced = ""
        d = (self._cfg.directions or {}).get("mine")
        if d is not None and (getattr(d, "voice", "") or ""):
            self._write_leaf(["directions", "mine", "voice"], voice, "保存说话译音音色（方向级）",
                             create=True)
            d.voice = voice
            synced = t("（已同步方向级音色 directions.mine.voice）")
            print(f"[gui] ⚠️ directions.mine.voice 优先于 session.voice，已同步改为 {voice!r}",
                  flush=True)
        running = any(e.running for e in self._engines)
        hint = t("（正在翻译：下次开始翻译生效）") if running else t("（下次开始翻译生效）")
        self._set_status("info", t("说话译音音色已保存：{v}", v=voice) + synced + hint)
        print(f"[gui] 说话译音音色 → {voice!r}（已写入 session.voice）", flush=True)

    def _on_tts_voice_change(self, _event=None) -> None:
        """「打字译音」音色：写 text_input.tts.voice 并同步内存 —— 下一次打字立刻生效。

        打字那条腿每次出声都实时读 `self._cfg.text_input['tts']['voice']`（见
        `engine._async_send_text`），所以不像说话那样要等重连，不用提示「下次生效」。
        """
        voice = self._tts_voice_var.get().strip()
        if not voice:
            return
        self._set_tts_voice_config(voice)
        if isinstance(self._cfg.text_input, dict):
            self._cfg.text_input.setdefault("tts", {})["voice"] = voice
        self._set_status("info", t("打字译音音色已保存：{v}（下一条打字即生效）", v=voice))
        print(f"[gui] 打字译音音色 → {voice!r}（已写入 text_input.tts.voice）", flush=True)

    def _set_voice_config(self, voice: str) -> None:
        """把说话译音音色写回 config.yaml 的 `session.voice`（就地改，保住注释与顺序）。"""
        self._write_leaf(["session", "voice"], voice, "保存说话译音音色")

    def _set_tts_voice_config(self, voice: str) -> None:
        """把打字译音音色写回 `text_input.tts.voice`。

        用 `_yaml_set_or_create` 而不是 `_yaml_set_in_text`：老配置可能整段没有
        `text_input`（从更早模板生成），旧函数会静默 no-op —— 这个设置就永远存不下去。
        """
        self._write_leaf(["text_input", "tts", "voice"], voice, "保存打字译音音色",
                         create=True)

    def _write_leaf(self, path: list[str], value: str, err_label: str,
                    *, create: bool = False) -> None:
        """把一个叶子值就地写进 config.yaml；失败只留痕，绝不把配置写坏。"""
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            setter = _yaml_set_or_create if create else _yaml_set_in_text
            text = setter(text, path, value)
            _write_config_text(p, text)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] {err_label}失败：{exc}", flush=True)

    # ---- 专有词库 ----

    def _glossary_scope(self) -> str:
        """当前下拉选中的 scope key（`global` / `mine` / `theirs`）。

        认不出来就退回全局：宁可编辑到那张最不容易出事的表，也不要抛异常把设置窗干掉。
        """
        names = getattr(self, "_glossary_scope_names", None) or {}
        var = getattr(self, "_glossary_scope_var", None)
        shown = var.get() if var is not None else ""
        for key, label in names.items():
            if label == shown:
                return key
        return "global"

    def _glossary_scope_label(self, scope: str) -> str:
        """给用户看的名字（「全局」/「我说」/「别人说」）——别把内部 key 甩到界面上。"""
        return (getattr(self, "_glossary_scope_names", None) or {}).get(scope, scope)

    @staticmethod
    def _glossary_scope_path(scope: str) -> list[str]:
        """该 scope 在 config.yaml 里的键路径（`_yaml_set_mapping` 认这个）。"""
        return ["glossary"] if scope == "global" else ["directions", scope, "hotwords"]

    def _read_glossary_from_disk(self, scope: str) -> dict[str, str] | None:
        """从**磁盘**读某个 scope 的词表；读不到 / 解析失败返回 None（调用方回落内存）。

        为什么按 scope 分开读：界面要能切表编辑，而"用户手改过 config.yaml"这件事
        对两张表都成立 —— 读的必须都是文件，不然切过去看到的是启动那一刻的快照。
        """
        try:
            raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                return None
            if scope == "global":
                return _as_str_map(raw.get("glossary"), "glossary（专有词库）")
            section = (raw.get("directions") or {}).get(scope) or {}
            return _as_str_map(section.get("hotwords"),
                               f"directions.{scope}.hotwords（方向级热词）")
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 读磁盘词库（{scope}）失败，退回内存快照："
                  f"{type(exc).__name__}: {exc}", flush=True)
            return None

    def _read_glossary_from_memory(self, scope: str) -> dict[str, str]:
        """内存快照（磁盘读不到时的兜底）。"""
        if scope == "global":
            return dict((self._cfg.session_base or {}).get("glossary") or {})
        d = (self._cfg.directions or {}).get(scope)
        return dict(getattr(d, "hotwords", None) or {})

    def _glossary_hint_text(self, scope: str) -> str:
        """提示语随 scope 变 —— 「全局」和「方向级」的行为完全不同，不能共用一句。"""
        if scope == "global":
            return t("每行一条，格式：原文=译名（社团名 / 人名 / 专有术语）；"
                     "作用于两个方向 —— 两个方向都要同一个译名时才放这里")
        return t("每行一条，格式：原文=译名（社团名 / 人名 / 专有术语）；"
                 "只对「{dir}」这条腿生效，同名词条会覆盖全局",
                 dir=self._glossary_scope_label(scope))

    def _on_glossary_scope_change(self, _event=None) -> None:
        """切换作用方向 = 换一张表编辑（重读磁盘，别拿上一张表的内容覆盖过去）。"""
        self._refresh_glossary_box()

    def _on_save_glossary(self) -> None:
        """把文本框里的词库写回 config.yaml 的**当前作用方向**那张表。

        三件事必须都做到，少一件都会变成「界面说保存了、实际没生效」：
          1) 落盘（对应键整段替换，保住段外注释）；
          2) 同步**内存里的同一个 cfg 对象** —— 引擎每次都从它现读，不打桩就白存；
          3) 通知**受影响的**跑着的引擎重建会话（词库是会话级配置，改不了热更新）。
        引擎因 RPM 预算没法立刻重建时会自己出 warn 状态，这里不需要替它圆场。
        """
        scope = self._glossary_scope()
        try:
            mapping = _parse_glossary_lines(self._glossary_text.get("1.0", tk.END))
        except Exception as exc:  # noqa: BLE001
            self._set_glossary_status(t("保存失败：{err}", err=f"{type(exc).__name__}: {exc}"),
                                      warn=True)
            return

        saved = self._save_glossary_config(self._glossary_scope_path(scope), mapping)
        # 同步内存：引擎持有的是**同一个** AppConfig 对象（见 _start_engine 的 cfg=self._cfg）
        if scope == "global":
            if isinstance(self._cfg.session_base, dict):
                self._cfg.session_base["glossary"] = dict(mapping)
        else:
            d = (self._cfg.directions or {}).get(scope)
            if d is None:
                # 理论上不可达（config.load 总会从模板建出两个方向）—— 但绝不静默：
                # 内存没同步就意味着引擎不会用上新表，用户会以为「保存了却没生效」。
                print(f"[gui] ⚠️ 配置里没有方向 {scope!r}：本次只落盘，未同步内存", flush=True)
            else:
                d.hotwords = dict(mapping)
        self._push_glossary_to_engines(scope, mapping)

        bad = _glossary_line_issues(self._glossary_text.get("1.0", tk.END))
        if saved:
            if bad:
                # 格式看不懂的行**必须说出来**：以前是静默丢掉，用户写 `原文：译名`
                # 只会觉得「保存没反应」，然后反复重试。
                for _lineno, _raw in bad:
                    print(f"[gui] ⚠️ 词库第 {_lineno} 行格式看不懂（要写成 原文=译名），"
                          f"已忽略：{_raw!r}", flush=True)
                self._set_glossary_status(
                    t("已保存 {n} 条词条到「{scope}」；{bad} 行看不懂已忽略（要写成 原文=译名）",
                      n=len(mapping), bad=len(bad), scope=self._glossary_scope_label(scope)),
                    warn=True)
            else:
                self._set_glossary_status(
                    t("已保存 {n} 条词条到「{scope}」（正在翻译时会重建会话生效）",
                      n=len(mapping), scope=self._glossary_scope_label(scope)))
            print(f"[gui] 专有词库已保存（{scope}）：{len(mapping)} 条"
                  + (f"（另有 {len(bad)} 行格式看不懂已忽略）" if bad else ""), flush=True)
        else:
            # 写盘失败只在日志留痕、且**不回显成功**：不能骗用户说存好了
            self._set_glossary_status(t("保存失败：{err}", err="写入 config.yaml 失败，见日志"),
                                      warn=True)

    def _save_glossary_config(self, path: list[str], mapping: dict[str, str]) -> bool:
        """整段替换 config.yaml 里 `path` 指向的那张词表；成功返回 True。

        路径由调用方给：全局是 `["glossary"]`，方向级是 `["directions", <名>, "hotwords"]`
        —— `_yaml_set_mapping` 对父级缺失会自动补建，所以老配置里没有 `hotwords:` 也能存下去。
        """
        p = DEFAULT_CONFIG
        if not p.exists():
            return False
        try:
            text = p.read_text(encoding="utf-8")
            _write_config_text(p, _yaml_set_mapping(text, path, mapping))
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存专有词库失败（{path}）：{exc}", flush=True)
            return False

    def _push_glossary_to_engines(self, scope: str, mapping: dict[str, str]) -> None:
        """通知**受影响的**引擎。

        全局改动两条腿都吃；方向级只吃那一条 —— 只发给对应引擎，别让另一条腿
        平白撞一次 RPM 预算（那会让用户莫名其妙看到「连接预算不足」）。
        """
        label = self._glossary_scope_label(scope)
        for eng, direction in zip(self._engines, self._engine_dirs):
            if not eng.running:
                continue
            if scope == "global":
                eng.set_glossary(mapping)
            elif direction == scope:
                eng.set_direction_hotwords(scope, mapping, label=label)

    def _set_glossary_status(self, text: str, *, warn: bool = False) -> None:
        lbl = getattr(self, "_glossary_status", None)
        if lbl is None:
            return
        try:
            lbl.configure(text=text, style="Warn.TLabel" if warn else "Muted.TLabel")
        except Exception:  # noqa: BLE001
            pass

    def _refresh_glossary_box(self) -> None:
        """把文本框重填成**磁盘上当前 scope 的那张**词表（读不到就退回内存快照）。

        为什么每次打开设置 / 每次切作用方向都要重填：用户完全可能**手改 config.yaml**
        （模板里就写着怎么改），而内存里的词库只是启动那一刻的快照。不重填的话，
        他改完文件、再点设置里的「保存词库」，就会用界面上的旧内容把手工改动**覆盖掉**
        —— 这是最容易被骂「把我配置搞丢了」的一类 bug。

        所以这里读的是**文件**，不是内存（与 `_refresh_api_key_in_cfg` 同一口径：
        配置文件的真相在磁盘上）。文件读不到 / 解析失败时退回内存快照 ——
        配置坏了不该连设置窗都打不开。改动同时按 scope 刷新提示语、清掉上次的状态提示。
        """
        box = getattr(self, "_glossary_text", None)
        if box is None:
            return
        scope = self._glossary_scope()
        mapping = self._read_glossary_from_disk(scope)
        if mapping is None:
            mapping = self._read_glossary_from_memory(scope)
        try:
            box.delete("1.0", tk.END)
            for line in _glossary_to_lines(mapping):
                box.insert(tk.END, line + "\n")
            hint = getattr(self, "_glossary_hint", None)
            if hint is not None:
                hint.configure(text=self._glossary_hint_text(scope))
            self._set_glossary_status("")
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 刷新词库文本框失败：{exc}", flush=True)

    # ---- 音色试听 ----
    def _on_preview_speech_voice(self) -> None:
        self._preview_voice("speech")

    def _on_preview_tts_voice(self) -> None:
        self._preview_voice("tts")

    def _preview_voice(self, kind: str) -> None:
        """合成一句固定样例并在本地扬声器播放。

        合成是网络调用（几百 ms～数秒），放守护线程里跑，绝不卡界面；线程只把结果
        塞 self._q，由 _poll() 在主线程改控件（与设备扫描/更新检查同一纪律）。
        """
        if self._preview_busy:
            return                              # 一次只放一个，免得两条音频叠着响
        btn = self._speech_preview_btn if kind == "speech" else self._tts_preview_btn
        var = self._speech_voice_var if kind == "speech" else self._tts_voice_var
        voice = (var.get() or "").strip()
        if not voice:
            self._set_status("warn", t("请先选择或填写音色"))
            return
        api_key = self._resolve_api_key_safe()
        if not api_key:
            self._set_status("warn", t("还没配置 API key，无法试听（见右上角「设置」）"))
            return
        # 端点必须按**当前线路**从 base_url 派生：不传的话 tts 会回落到自己的模块常量
        # （千问云域名）—— 海外用户点「试听」就会连到根本用不了的地方，
        # 而报错看起来还像"音色不支持"，排查方向全错。
        base_url = str(self._cfg.session_base.get("base_url") or "")
        try:
            omni_endpoint = endpoints.chat_url(base_url)
            tts_endpoint = endpoints.multimodal_url(base_url)
        except ValueError as exc:
            self._set_status("error", t("试听失败：{msg}", msg=exc))
            print(f"[gui] ❌ 试听取消：无法从当前线路派生端点（{exc}）", flush=True)
            return
        self._preview_busy = True
        if btn is not None:
            btn.configure(state=tk.DISABLED, text=t("试听中…"))
        self._set_status("info", t("正在试听「{v}」…", v=voice))
        # 日志带上线路摘要（endpoints.describe），试听连错地方时第一眼就能看出
        line = endpoints.describe(self._provider(), base_url)
        print(f"[gui] 试听音色 → {voice!r}（{kind}，模型 {VOICE_PREVIEW_MODEL}）| {line}",
              flush=True)
        threading.Thread(target=self._preview_worker,
                         args=(kind, voice, api_key),
                         kwargs={"omni_endpoint": omni_endpoint,
                                 "tts_endpoint": tts_endpoint},
                         daemon=True, name="vlt-voice-preview").start()

    def _preview_worker(self, kind: str, voice: str, api_key: str, *,
                        omni_endpoint: str | None = None,
                        tts_endpoint: str | None = None) -> None:
        """守护线程体：合成 + 播放，结果（含失败原因）回主线程。绝不静默。

        两条腿走**各自的模型**（音色不通用）：打字侧 qwen3-tts-flash；说话侧非实时
        Qwen-Omni（Tina 等实时音色只有它认）。两者输出同为 24k 单声道 PCM，播放路径一致。

        两个 endpoint 参数由 `_preview_voice` 按当前线路派生后传入；为 None 时 tts 内部
        回落到模块常量（千问云默认）—— 保留这个回落只是为了让直接调本函数的老路径不变。
        """
        err = ""
        try:
            if kind == "speech":
                pcm = tts.synthesize_omni(VOICE_PREVIEW_TEXT, voice=voice, api_key=api_key,
                                          endpoint=omni_endpoint)
            else:
                pcm = tts.synthesize(VOICE_PREVIEW_TEXT, voice=voice,
                                     model=VOICE_PREVIEW_MODEL, api_key=api_key,
                                     endpoint=tts_endpoint)
            _play_pcm_local(pcm)
        except tts.TtsError as exc:
            err = str(exc)
        except Exception as exc:  # noqa: BLE001 — 失败也要把原因带回主线程
            err = f"{type(exc).__name__}: {exc}"
        self._q.put(("voice_preview", kind, voice, err))

    def _on_voice_preview_done(self, kind: str, voice: str, err: str) -> None:
        """主线程：恢复按钮 + 报结果。说话侧的「音色不支持」翻成人话。"""
        self._preview_busy = False
        btn = self._speech_preview_btn if kind == "speech" else self._tts_preview_btn
        try:
            if btn is not None and btn.winfo_exists():
                btn.configure(state=tk.NORMAL, text=t("试听"))
        except Exception:  # noqa: BLE001
            pass
        if not err:
            self._set_status("info", t("试听完成：{v}", v=voice))
            return
        if _is_unsupported_voice_err(err):
            self._set_status("warn", t("此音色暂不支持试听（服务端拒收该音色 id）"))
            print(f"[gui] ⚠️ 音色 {voice!r}（{kind}）试听被服务端拒收：{err}", flush=True)
            return
        self._set_status("error", t("试听失败：{msg}", msg=err))
        print(f"[gui] 试听失败（{voice!r}）：{err}", flush=True)

    def _resolve_api_key_safe(self) -> str:
        """按 config.load_api_key 的口径取 key，但**取不到返回空串而不是抛 SystemExit**
        —— 试听失败不该把整个界面带走。取的是**当前线路那一槽**（两条线路的 key 不通用）。"""
        try:
            return load_api_key(slot=self._current_key_slot())
        except SystemExit:
            return ""
        except Exception:  # noqa: BLE001
            return ""

    def _push_lang_to_engines(self) -> None:
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        for name, (src, tgt) in (("mine", (a, b)), ("theirs", (b, a or "zh"))):
            d = self._cfg.directions.setdefault(name, Direction())
            d.source_lang = src
            d.target_lang = tgt
        for eng, direction in zip(self._engines, self._engine_dirs):
            if not eng.running:
                continue
            src, tgt = (a, b) if direction == "mine" else (b, a or "zh")
            eng.set_languages(src, tgt)

    # ================================================================ 引擎控制

    def _start(self) -> None:
        if any(e.running for e in self._engines):
            return
        self._refresh_api_key_in_cfg()   # 防呆：key 可能在运行期被保存/清除/改环境，先重解再检查
        if not (self._cfg.session_base.get("api_key") or "").strip():
            # 没 key 就别白连一次（会撞 401），直接把用户送到填 key 的地方
            self._set_status("error", t("还没配置 API key —— 点右上角「API key ›」填一个再开始"))
            self._open_settings()
            return
        d = self._direction_var.get()

        sinks: set[str] = set()
        if self._chatbox_var.get():
            sinks.add("chatbox")
        if self._overlay_var.get():
            sinks.add("overlay")
        if self._desktop_var.get():
            # "desktop" 只是界面层的标记（字幕窗由界面持有，引擎不认这个 sink），
            # 放在这里是为了让「只勾桌面字幕」也能通过下面的"至少选一个输出"检查。
            sinks.add("desktop")
        if not sinks:
            self._set_status("warn", t("请至少选择一个输出"))
            return

        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        specs: list[tuple] = []
        if d in ("mine", "dual"):
            specs.append(("mine", "mine", "mic", a, b))
        if d in ("theirs", "dual"):
            specs.append(("theirs", "theirs", "loopback", b, a or "zh"))

        # chatbox 只承载「我说的话」的译文（气泡在别人眼里代表我发言）。
        # 若本次方向不含「我说」，用户勾了 chatbox 也一条都发不出去 —— 必须像
        # 译音输出那样明说，别让人对着"没反应的 chatbox"排查（禁静默降级）。
        chatbox_warn = ""
        if "chatbox" in sinks and not any(s[1] == "mine" for s in specs):
            _zh = ("chatbox 只发「我说的话」的译文（当前方向不含它）→ 本次 chatbox 不会输出；"
                   "对方的译文看手腕屏／聊天区")
            chatbox_warn = t(_zh)
            print(f"[gui] ⚠️ {_zh}", flush=True)   # 日志保持中文（诊断用），界面走 i18n

        # 启动时把「方向 + 每条腿的来源/语言 + 输出面」写进日志。
        # 没有这行的话，事后只能从有没有 [loopback] 打印去反推方向——
        # 用户报「英文没翻译」时就是这样，明明是没起 loopback 腿，却看着像翻译坏了。
        _label = {"mine": "我说的话", "theirs": "别人说", "dual": "双向同时"}.get(d, d)
        print(f"[gui] 启动：方向={_label}({d}) | 输出={','.join(sorted(sinks)) or '无'} | "
              f"语言对={a}→{b}", flush=True)
        for _w, _dir, _src, _sl, _tl in specs:
            print(f"[gui]   腿 {_dir}：来源={'麦克风' if _src == 'mic' else '游戏音频(loopback)'}"
                  f" | {_sl}→{_tl}", flush=True)

        # 译音输出：勾选框（总开关）+ 方向级开关，两者是「与」关系，都开才出声。
        # 译音回灌只对「我说的话」有意义（对方要听的是我说的话的译文）；
        # 「别人说」的译文是我自己的母语，回灌进虚拟麦毫无意义 → 明确告知，不静默忽略。
        want_audio = bool(self._vmic_var.get())
        audio_warn = ""
        if want_audio and not any(s[1] == "mine" for s in specs):
            _zh = "译音输出只对「我说的话」方向有效（当前方向不含它）→ 本次已忽略"
            audio_warn = t(_zh)
            print(f"[gui] ⚠️ {_zh}", flush=True)   # 日志保持中文（诊断用），界面走 i18n
            want_audio = False
        if isinstance(self._cfg.output, dict):
            self._cfg.output.setdefault("audio", {})["enabled"] = want_audio
            _dev = (self._cfg.output.get("audio") or {}).get("device_name") or "自动回退链"
        else:
            _dev = "自动回退链"
        print(f"[gui]   译音输出={'开' if want_audio else '关'}（虚拟声卡：{_dev}）", flush=True)

        for _who, direction, _src, src_lang, tgt_lang in specs:
            dd = self._cfg.directions.setdefault(direction, Direction())
            dd.source_lang = src_lang
            dd.target_lang = tgt_lang
            dd.output_audio = want_audio and direction == "mine"

        self._current = {}
        self._auto_scroll = True
        self._engines = []
        self._engine_dirs = []
        self._specs = specs
        self._sinks = sinks
        self._pending_starts = len(specs)
        # 手腕屏由界面持有，内容镜像聊天区（两个方向都进同一块屏）
        self._start_overlay()
        self._start_desktop()
        # 连接意图开着就确保房间在跑（_stop() 会连房间一起停；_start_room 幂等，已在跑则无操作）
        if getattr(self, "_room_var", None) is not None and self._room_var.get():
            self._start_room()
        self._start_engine(0)
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        self._set_text_input_enabled(d in ("mine", "dual"))
        warns = [w for w in (audio_warn, chatbox_warn) if w]
        if warns:
            self._set_status("warn", "；".join(warns))
        elif d == "mine":
            # 只翻自己的话时明确提示一句：用户放英文视频却没选对方向，
            # 表现就是「翻译坏了」，而实际是根本没采集对方/视频的声音。
            self._set_status("info", t("正在启动…（只翻译你说的话；要翻译对方/视频请选「双向同时」）"))
        else:
            self._set_status("info", t("正在启动（双向）…") if len(specs) == 2 else t("正在启动…"))

    def _start_engine(self, index: int) -> None:
        specs = self._specs
        if index >= len(specs):
            return
        who, direction, source, src_lang, tgt_lang = specs[index]
        # 引擎不碰手腕屏：它由界面持有（一块屏显示两个方向的对话）。
        # 若交给两个引擎各自创建，会撞 `OverlayError_KeyInUse`（用户实测）。
        # chatbox 只发「我说的话」的译文——theirs 腿不需要它（与 engine._chatbox_wanted 同义，双保险）。
        own_sinks = {s for s in self._sinks if s not in ("overlay", "desktop")}
        if direction == "theirs":
            own_sinks.discard("chatbox")
        events = EngineEvents(
            # 上行挂在界面这一层（不改 engine.py）：_on_engine_text 照常把文本塞进界面队列，
            # 再顺带把「我自己说的话」的源文发进房间。_srcid 绑定这条腿的真实来源
            # （mic/loopback）—— should_publish 靠它把 loopback 那条腿排除掉（防二次广播/回环）。
            on_text=lambda src, txt, final, who=who, _srcid=source:
                self._on_engine_text(who, _srcid, src, txt, final),
            on_status=lambda lvl, msg, who=who: self._on_engine_status(lvl, msg, who),
            on_stats=lambda s: self._q.put(("stats", s)),
        )
        eng = Engine(
            cfg=self._cfg,
            direction=direction,
            source=source,
            sinks=own_sinks,
            events=events,
            config_path=DEFAULT_CONFIG,
        )
        self._engines.append(eng)
        self._engine_dirs.append(direction)
        self._pending_starts -= 1
        eng.start()
        # 引擎起来了 → 电平改由引擎那条腿提供，探针必须让位
        # （同一时刻只允许一路 loopback，否则两路抢同一个采集端点）
        self._sync_gate_level_probe()
        if self._pending_starts > 0:
            # 两个引擎错开 300ms 启动，避免同时抢占音频设备
            self._start_job = self._root.after(300, self._start_engine, index + 1)

    def _on_engine_status(self, lvl: str, msg: str, who: str) -> None:
        """引擎状态既要进状态栏，也要进日志（stdout）。

        状态栏文字不落盘 —— 少了这一行，「译音输出为什么没出声」「设备为什么没匹配上」
        这类提示在用户发来的日志里完全看不到，只能靠猜。
        """
        print(f"[{who}][{lvl}] {msg}", flush=True)
        self._q.put(("status", lvl, msg))

    # ---------------------------------------------------------------- 手腕屏（界面持有）
    def _start_overlay(self, *, force: bool = False) -> bool:
        """按勾选状态接管 SteamVR 的手腕屏。失败只禁用这一项，绝不影响翻译。

        手腕屏由**界面**持有而不是某个引擎：手腕上只该有一块屏，内容是最近几句对话
        （镜像聊天区）。交给两个引擎各自创建会撞 `OverlayError_KeyInUse`（用户实测）。

        force=True 用于「勾上就起」那条路（此时还没点开始翻译，`self._sinks` 里没有 overlay）。
        返回是否**真的起来了** —— 调用方要靠它决定失败时是否自动退回勾选。
        """
        if self._overlay_out is not None:
            return True                       # 已经起来了（勾选时起过），别重复建（会撞 KeyInUse）
        if not force and "overlay" not in self._sinks:
            return False
        try:
            # 后端按平台选（Windows=pyopenvr / Linux=自建 OpenXR）
            self._overlay_out = platform.create_wrist_overlay(
                OverlayConfig.from_dict(self._cfg.overlay), config_path=DEFAULT_CONFIG)
            if not self._overlay_out.start():
                self._overlay_out = None      # start() 内部已打印原因
                return False
            self._push_overlay(force=True)
            return True
        except Exception as exc:  # noqa: BLE001
            self._overlay_out = None
            print(f"[gui] ⚠️ 手腕屏初始化异常，已禁用（翻译不受影响）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            return False

    def _on_overlay_toggle(self) -> None:
        """勾选/取消「手腕屏」的即时反应。

        勾上就**立刻**把屏拉起来，而不是等点开始翻译 —— 用户勾上多半是想先看位置对不对
        （微调面板要对着屏拖），把「调试」和「开跑」绑死会很难用。
        起不来就自动退回未勾选：否则界面显示已开启、实际什么都没有。
        """
        if self._overlay_var.get():
            if self._start_overlay(force=True):
                self._set_status("info", t("手腕屏已开启（可用「微调 ▸」调位置）"))
            else:
                self._overlay_var.set(False)          # 启动失败 → 自动跳回去
                self._set_status("error", t(
                    "手腕屏没启动起来，已自动取消勾选（先把 SteamVR 打开，再勾一次即可）"))
        else:
            self._stop_overlay()
            self._sinks.discard("overlay")            # 别让下一次「开始翻译」又把它拉起来
        self._save_ui_state()

    def _stop_overlay(self) -> None:
        if self._overlay_out is None:
            return
        try:
            self._overlay_out.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 关闭手腕屏时出错（忽略）：{exc}", flush=True)
        self._overlay_out = None

    def _push_overlay(self, force: bool = False) -> None:
        """把聊天区最近几条推给手腕屏（同一块屏显示两个方向的对话）。"""
        if self._overlay_out is None:
            return
        try:
            entries = [(b.who, b.source, b.text, b.label) for b in self._bubbles[-8:]]
            self._overlay_out.update_entries(entries, force=force)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 手腕屏刷新失败：{type(exc).__name__}: {exc}", flush=True)

    # ---------------------------------------------------------------- 桌面字幕（界面持有）
    def _desktop_cfg(self) -> dict:
        d = self._cfg.desktop_overlay
        return d if isinstance(d, dict) else {}

    def _start_desktop(self, *, force: bool = False) -> bool:
        """把桌面字幕窗拉起来（PC 桌面模式：贴在 VRChat 窗口上的叠加窗）。

        与手腕屏同一套约定：由**界面**持有（内容是聊天区的镜像，而聊天区在界面这一层），
        失败只禁用这一项，绝不影响翻译。force=True 用于「勾上就起」那条路
        （此时还没点开始翻译，`self._sinks` 里没有 desktop）。
        """
        if self._desktop_out is not None:
            return True
        if not force and "desktop" not in self._sinks:
            return False
        try:
            from .output.desktop_overlay import DesktopOverlay, DesktopOverlayConfig
            cfg = DesktopOverlayConfig.from_dict(self._desktop_cfg(),
                                                 visual=self._cfg.overlay or {})
            out = DesktopOverlay(cfg, config_path=DEFAULT_CONFIG, root=self._root)
            if not out.start():
                return False                       # start() 内部已打印原因
            self._desktop_out = out
            self._push_desktop(force=True)
            return True
        except Exception as exc:  # noqa: BLE001
            self._desktop_out = None
            print(f"[gui] ⚠️ 桌面字幕初始化异常，已禁用（翻译不受影响）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            return False

    def _on_desktop_toggle(self) -> None:
        """勾选/取消「桌面字幕」。勾上就立刻出窗（用户多半是想先看位置对不对）。

        起不来就自动退回未勾选 —— 否则界面显示"已开启"、实际什么都没有。
        """
        if self._desktop_var.get():
            if self._start_desktop(force=True):
                self._set_status("info", t("桌面字幕已开启（拖到想要的位置，透明度见「微调 ▸」）"))
            else:
                self._desktop_var.set(False)
                self._set_status("error", t("桌面字幕没启动起来，已自动取消勾选"))
        else:
            self._stop_desktop()
            self._sinks.discard("desktop")         # 别让下一次「开始翻译」又把它拉起来
        self._save_ui_state()

    def _stop_desktop(self) -> None:
        if self._desktop_out is None:
            return
        try:
            self._desktop_out.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 关闭桌面字幕时出错（忽略）：{exc}", flush=True)
        self._desktop_out = None
        self._desktop_dragging = False
        # 文案跟着复位：字幕窗都关掉了还写着「锁定位置」，与真实状态不符（用户会以为
        # 还处在解锁态）。headless 模式下这个按钮没建过 → getattr 兜住。
        btn = getattr(self, "_desktop_drag_btn", None)
        if btn is not None:
            btn.configure(text=t("解锁拖动"))

    def _push_desktop(self, force: bool = False) -> None:
        """把聊天区最近几条推给桌面字幕（与手腕屏同一份内容）。

        ⚠️ 条目形状必须与 `_push_overlay` 一致（4 元组，带说话人昵称）：桌面字幕与手腕屏
        共用 `overlay.render_conversation`，房间里的成员靠这个 label 才显示得出昵称。
        """
        if self._desktop_out is None:
            return
        try:
            entries = [(b.who, b.source, b.text, b.label) for b in self._bubbles[-8:]]
            self._desktop_out.update_entries(entries, force=force)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 桌面字幕刷新失败：{type(exc).__name__}: {exc}", flush=True)

    def _on_desktop_alpha(self, _v: str = "") -> None:
        """透明度滑块：先改窗口（立刻见效），停手 300ms 再落盘。"""
        # 只有**用户拖动滑块**才会走到这里（Tk 的 -command 回调）→ 记下「动过了」，
        # `_save_desktop_cfg()` 才允许把 alpha 写进配置（见那里的说明）。
        self._desktop_alpha_touched = True
        a = float(self._desktop_alpha_var.get())
        lbl = getattr(self, "_desktop_alpha_lbl", None)
        if lbl is not None:
            lbl.configure(text=f"{a:.2f}")
        if self._desktop_out is not None:
            self._desktop_out.set_alpha(a)
        self._schedule_desktop_save()

    def _schedule_desktop_save(self) -> None:
        if self._desktop_save_job is not None:
            try:
                self._root.after_cancel(self._desktop_save_job)
            except Exception:
                pass
        self._desktop_save_job = self._root.after(300, self._save_desktop_cfg)

    def _save_desktop_cfg(self) -> None:
        """把桌面字幕的参数（透明度 + 拖动折算出的锚点/偏移）写回 config.yaml。

        用 `_yaml_set_or_create` 而不是就地改：用户的 config.yaml 是从**旧模板**生成的，
        里面根本没有 `desktop_overlay:` 段，就地改会因为找不到父键静默失效
        （表现就是"拖了、调了，重启全没了"）。

        ⚠️ alpha 只在**用户真的动过滑块**时才写：本函数也被「锁定位置」与拖动落盘调到，
        无条件写的话，用户只是把字幕拖了个位置，滑块上那个值（可能是从 `overlay.alpha`
        继承来的、甚至只是默认的 0.90）就被写进 `desktop_overlay.alpha` 并热重载生效 ——
        表现为「拖一下位置，透明度突然变了」。
        """
        self._desktop_save_job = None
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            updates: list[tuple[list[str], str]] = []
            if self._desktop_alpha_touched:
                updates.append((["desktop_overlay", "alpha"],
                                _fmt_scalar(float(self._desktop_alpha_var.get()))))
            if self._desktop_out is not None:
                for key, val in (self._desktop_out.snap_to_config() or {}).items():
                    if key in ("offset", "pos"):
                        updates.append((["desktop_overlay", key], f"[{val[0]}, {val[1]}]"))
                    else:
                        updates.append((["desktop_overlay", key], str(val)))
            if not updates:
                return                               # 什么都没改：不重写配置、也不打误导日志
            for key_path, value in updates:
                text = _yaml_set_or_create(text, key_path, value)
            _write_config_text(p, text)
            print("[gui] 桌面字幕参数已写入 config.yaml："
                  + " ".join(f"{'/'.join(k)}={v}" for k, v in updates), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存桌面字幕参数失败：{exc}", flush=True)

    def _toggle_desktop_drag(self) -> None:
        """解锁/锁定拖动。

        字幕窗默认**鼠标穿透**（不挡着点 VRChat），穿透开着时窗口收不到鼠标事件，
        所以要拖必须先解锁；锁定 = 把落点折算成锚点+偏移写回配置并恢复穿透。
        """
        if self._desktop_out is None:
            # ⚠️ 这条分支**没有**取消任何勾选（那是 `_on_desktop_toggle` 的事）：
            #    文案必须如实说「没在运行，先去勾上」，不能谎称已经替用户改了勾选状态。
            self._desktop_dragging = False
            self._set_status("warn", t("桌面字幕还没开启，先勾上「桌面字幕」再解锁拖动"))
            return
        self._desktop_dragging = not self._desktop_dragging
        try:
            self._desktop_out.set_draggable(self._desktop_dragging)
        except Exception as exc:  # noqa: BLE001
            self._desktop_dragging = False
            print(f"[gui] 切换桌面字幕拖动失败：{type(exc).__name__}: {exc}", flush=True)
            return
        btn = getattr(self, "_desktop_drag_btn", None)
        if btn is not None:
            btn.configure(text=t("锁定位置") if self._desktop_dragging else t("解锁拖动"))
        if self._desktop_dragging:
            self._set_status("info", t("桌面字幕已解锁：拖动字幕窗到想要的位置，放好后点「锁定位置」"))
        else:
            self._save_desktop_cfg()
            self._set_status("info", t("桌面字幕位置已记住"))

    def _stop(self) -> None:
        """停止翻译：**绝不在界面线程等引擎收尾**（真机实测冻 20s = 窗口无响应）。

        之前的写法是 `for eng in self._engines: eng.stop()`，两处致命：
        ① `Engine.stop()` 在**调用它的线程**上等收尾，而调用者正是 Tk 主线程；
        ② 顺序调用 → 第二个引擎在前一个收尾期间继续采集/上送（用户日志里
        `[mic] 采集结束` 之后 loopback 腿还打了 8 秒的 `[gate]`）。
        现在：先对**所有**引擎并发下发停止信号（采集立刻停），收尾交给后台线程，
        界面只留一行「正在停止…」，收尾完成由 `_poll` 从队列里收到通知再恢复。
        """
        self._pending_starts = 0
        self._specs = []
        if self._start_job is not None:
            try:
                self._root.after_cancel(self._start_job)
            except Exception:
                pass
            self._start_job = None

        engines = list(self._engines)
        for eng in engines:
            try:
                eng.request_stop()      # 只发信号：并发下发，谁都不等谁
            except Exception as exc:    # noqa: BLE001
                print(f"[gui] 下发停止信号失败（忽略）：{type(exc).__name__}: {exc}", flush=True)
        self._stop_overlay()
        self._stop_desktop()
        self._stop_room()          # 关窗 / 停止都连房间一起停（幂等、≤5s，绝不拖住退出）
        self._refresh_room_status_label()
        self._engines = []          # 立刻移走：收尾由后台线程负责，_poll 不再看它们
        self._engine_dirs = []
        self._set_text_input_enabled(False)
        # 引擎没了 → 设置窗若还开着且勾了「启用」，电平交回独立探针
        self._sync_gate_level_probe()
        if not engines:
            # 没有引擎在手：但可能还有上一次的收尾在飞（只有 _on_close 这条重复调用路径会走到），
            # 那就别把「开始翻译」放开 —— 旧引擎还在关麦克风/虚拟声卡。
            if self._stop_done_evt.is_set():
                self._start_btn.configure(state=tk.NORMAL)
                self._stop_btn.configure(state=tk.DISABLED)
            self._set_status("info", t("已停止"))
            return
        # 收尾期间**禁掉「开始翻译」**：旧引擎还在关麦克风/虚拟声卡，立刻重启会抢设备。
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.DISABLED)
        self._set_status("info", t("正在停止…"))
        self._stop_done_evt.clear()
        threading.Thread(target=self._wait_stop_done, args=(engines,), daemon=True,
                         name="vlt-stop-wait").start()

    def _wait_stop_done(self, engines: list) -> None:
        """（**后台线程**）等引擎真正收尾完，再入队让 `_poll` 恢复界面。绝不碰 Tk。"""
        t0 = time.monotonic()
        stuck: list[int] = []
        for i, eng in enumerate(engines):
            try:
                if not eng.wait_stopped(STOP_WAIT_S):
                    stuck.append(i)
            except Exception as exc:            # noqa: BLE001
                print(f"[gui] ⚠️ 等引擎收尾出错（忽略）：{type(exc).__name__}: {exc}", flush=True)
        elapsed = time.monotonic() - t0
        if stuck:
            print(f"[gui] ⚠️ 停止收尾超时（{STOP_WAIT_S:.0f}s）：第 {stuck} 个引擎还没退出"
                  "（仍在关麦克风/虚拟声卡；界面照常恢复，状态栏会如实提示仍在收尾）",
                  flush=True)
        self._q.put(("stop_done", elapsed, len(engines), bool(stuck)))
        self._stop_done_evt.set()

    def _on_close(self) -> None:
        """关窗口：先停引擎（**有界**等采集线程真正退出），再销毁窗口。

        顺序很重要——如果先销毁窗口再去等引擎，主线程会阻塞在一个已经失效的
        Tk 事件循环上，界面看起来就是"卡死后闪退"。
        这里和按钮那条路不同：退出时等一等是对的（要关干净麦克风/虚拟声卡），
        但同样给上限 —— 等不到也得走，绝不把窗口吊在那儿。
        """
        self._closing = True
        # 先放掉电平探针占着的采集设备；_stop() 里那次同步看到 _closing 也不会再拉起来
        self._stop_gate_probe()
        try:
            self._stop()
        except Exception as exc:
            print(f"[gui] 停止引擎时出错（继续关闭）：{exc}", file=sys.stderr)
        try:
            if not self._stop_done_evt.wait(CLOSE_WAIT_STOP_S):
                print(f"[gui] ⚠️ 退出时等引擎收尾超过 {CLOSE_WAIT_STOP_S:.0f}s，"
                      f"直接关闭（进程退出会释放设备）", flush=True)
        except Exception:
            pass
        try:
            # 「稍后更新」的另一半：正常退出时替换（绝不自动拉起新版）。
            # 放在销毁窗口之前：失败提示需要有地方弹；bat 自己会等本程序退出再动手。
            self._maybe_replace_on_exit()
        except Exception as exc:
            print(f"[update] ⚠️ 退出时替换出现异常（继续关闭）：{exc}", file=sys.stderr)
        try:
            self._root.destroy()
        except Exception:
            pass
        crashlog.close()

    # ================================================================ 设备选择

    def _start_device_scan(self) -> None:
        """扫描设备（**主线程同步执行**）。

        为什么不用后台线程：PortAudio 的初始化/销毁是**线程绑定**的（WASAPI 用 COM
        单元）。sounddevice 在首次调用的那个线程里 Pa_Initialize，而它的 atexit 钩子
        在主线程 Pa_Terminate —— 跨线程销毁会抛
        `Tcl_AsyncDelete: async handler deleted by the wrong thread`（实测退出码 3，
        更早的版本还直接段错误）。实测枚举冷启动 348ms、预热后只要 23ms，
        同步做完全可接受，换来的是确定性。
        """
        if self._headless:
            return                        # 无界面的自检模式不需要设备列表
        self._device_scan_pending = True
        try:
            self._root.update_idletasks()     # 先把"正在扫描…"画出来再阻塞
            mics = enumerate_mic_devices()
            # Linux 界面不提供 loopback / 输出两个下拉，也就不必跑这两次 pw-dump。
            loops = [] if self._linux_fixed_audio else enumerate_loopback_devices()
            outs = [] if self._linux_fixed_audio else enumerate_audio_out_devices()
            self._on_device_scan_result(mics, loops, outs)
        except Exception as exc:
            self._device_scan_pending = False
            self._set_status("warn", t("设备扫描失败：{msg}", msg=exc))

    def _on_refresh_devices(self) -> None:
        if any(e.running for e in self._engines):
            self._set_status("warn", t("建议停止翻译后再刷新设备列表"))
        self._start_device_scan()
        self._set_status("info", t("正在扫描设备…"))

    def _on_device_scan_result(self, mics, loops, outs) -> None:
        auto = t("自动检测")
        self._mic_names = []
        mic_display = [auto]
        for info in mics:
            self._mic_names.append(info.name)
            mic_display.append(format_device_display(info))

        self._loopback_names = []
        loop_display = [auto]
        for info in loops:
            self._loopback_names.append(info.name)
            loop_display.append(format_device_display(info))

        self._audio_out_names = []
        out_display = [auto]
        for info in outs:
            self._audio_out_names.append(info.name)
            out_display.append(format_device_display(info))

        self._mic_combo.configure(values=mic_display)
        if not self._linux_fixed_audio:
            self._loopback_combo.configure(values=loop_display)
            self._audio_out_combo.configure(values=out_display)

        # 恢复配置里的选择（如果设备在列表里）
        capture_cfg = (self._cfg.output or {}).get("capture") or {}
        audio_cfg = (self._cfg.output or {}).get("audio") or {}
        mic_name = capture_cfg.get("mic_device") or ""
        loop_name = capture_cfg.get("loopback_device") or ""
        out_name = audio_cfg.get("device_name") or ""

        if mic_name and mic_name in self._mic_names:
            self._mic_combo.set(mic_display[self._mic_names.index(mic_name) + 1])
        else:
            self._mic_combo.set(auto)

        if not self._linux_fixed_audio:
            if loop_name and loop_name in self._loopback_names:
                self._loopback_combo.set(loop_display[self._loopback_names.index(loop_name) + 1])
            else:
                self._loopback_combo.set(auto)

            if out_name and out_name in self._audio_out_names:
                self._audio_out_combo.set(out_display[self._audio_out_names.index(out_name) + 1])
            else:
                self._audio_out_combo.set(auto)

        if not mics and not loops and not outs:
            self._set_status("warn", t("未扫描到设备（远程会话下枚举为空是正常的）"))
        elif self._linux_fixed_audio:
            self._set_status("info",
                t("已扫描到 {m} 个麦克风（VRChat 音频与译音输出自动处理）", m=len(mics)))
        else:
            self._set_status("info",
                t("已扫描到 {m} 个麦克风 / {l} 个 loopback / {o} 个输出",
                  m=len(mics), l=len(loops), o=len(outs)))
        self._device_scan_pending = False

    def _on_device_change(self, _event=None) -> None:
        auto = t("自动检测")
        mic_text = self._mic_combo.get()

        mic_name = ""
        if mic_text != auto and mic_text:
            display_list = combo_values(self._mic_combo)
            idx = display_list.index(mic_text) if mic_text in display_list else -1
            if idx > 0 and idx - 1 < len(self._mic_names):
                mic_name = self._mic_names[idx - 1]

        # Linux 没有这两个下拉：保持空串（_save_device_config 会跳过对应配置键）
        loop_name = ""
        out_name = ""
        if not self._linux_fixed_audio:
            loop_text = self._loopback_combo.get()
            if loop_text != auto and loop_text:
                display_list = combo_values(self._loopback_combo)
                idx = display_list.index(loop_text) if loop_text in display_list else -1
                if idx > 0 and idx - 1 < len(self._loopback_names):
                    loop_name = self._loopback_names[idx - 1]

            out_text = self._audio_out_combo.get()
            if out_text != auto and out_text:
                display_list = combo_values(self._audio_out_combo)
                idx = display_list.index(out_text) if out_text in display_list else -1
                if idx > 0 and idx - 1 < len(self._audio_out_names):
                    out_name = self._audio_out_names[idx - 1]

        self._save_device_config(mic_name, loop_name, out_name)
        # 回显完整设备名：下拉框宽度有限（长设备名会被截断），
        # 状态栏给一次完整确认，免得用户不知道自己到底选了哪个。
        picked = [t for t in (mic_name, loop_name, out_name) if t]
        if picked:
            self._set_status("info", t("已选设备：{names}", names=" | ".join(picked)))
        else:
            self._set_status("info", t("设备：全部自动检测"))

    def _save_device_config(self, mic_name: str, loop_name: str, out_name: str) -> None:
        """把设备选择写回 config.yaml —— **就地改那几行**，不整份重写。

        ⚠️ 这里以前用 `yaml.safe_load` + `yaml.dump` 整文件重写：用户每改一次设备下拉，
        `config.yaml` 的**注释、空行、键顺序就全没了** —— 实测一份 154 行、53 行注释的
        配置被拍成 116 行、键按字母重排的转储（注释是配置里唯一的说明书，丢了只能重看模板）。
        其它保存路径（语言 / 译音开关 / 手腕屏微调 / 音色 / 输入门限）早就改成就地写了，
        只有这条漏了 —— 而设备下拉恰恰是用户最常动的控件之一。

        Linux：只写麦克风。`loopback_device` / `output.audio.device_name` **原样保留**
        （界面不给选、引擎也忽略它们），别把用户旧配置清成空串。
        """
        write_fixed = not self._linux_fixed_audio
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            # 值用 _yaml_scalar 渲染：设备名是外部枚举来的，含 `#`/`: `/`[` 时
            # 手拼会写出坏 YAML（旧整份 dump 自动处理了这件事，这里要自己保证）。
            updates: list[tuple[list[str], str]] = [
                (["capture", "mic_device"], _yaml_scalar(mic_name)),
            ]
            if write_fixed:
                updates.append((["capture", "loopback_device"], _yaml_scalar(loop_name)))
                updates.append((["output", "audio", "device_name"], _yaml_scalar(out_name)))
            for key_path, val in updates:
                # 用 _yaml_set_or_create：老配置可能整段没有 `capture` / `output.audio`，
                # 那个「只在已存在路径上替换」的函数会静默 no-op（设置就永远存不下去）。
                text = _yaml_set_or_create(text, key_path, val)
            _write_config_text(p, text)     # 写前校验 YAML：宁可这次不生效，也不写坏配置
        except Exception as exc:
            print(f"[gui] 保存设备配置失败：{exc}", flush=True)
            return
        # 同步内存里的配置，引擎启动时会读
        self._cfg.output.setdefault("capture", {})["mic_device"] = mic_name
        if write_fixed:
            self._cfg.output["capture"]["loopback_device"] = loop_name
            self._cfg.output.setdefault("audio", {})["device_name"] = out_name

    # ------------------------------------------------ 输入门限（VRChat 输出侧过滤）

    def _on_gate_change(self, _v=None) -> None:  # noqa: ANN001
        """门限开关 / 滑块变化：立刻更新显示与正在跑的引擎，落盘延后 300ms。

        这个回调要同时当两种用：`tk.Scale` 的 command 每次移动都触发（参数是字符串），
        `tk.Checkbutton` 的 command 不带参数 —— 所以形参默认为 None。
        """
        db = float(self._gate_var.get())
        self._gate_val_lbl.configure(text=f"{db:g} dB")
        self._apply_gate_live()
        # 勾上「启用」→ 立刻开始采电平；取消勾选 → 立刻停并释放设备
        self._sync_gate_level_probe()
        if self._gate_save_job is not None:
            try:
                self._root.after_cancel(self._gate_save_job)
            except Exception:  # noqa: BLE001
                pass
        # 拖动时别每像素写盘：停手 300ms 才落盘（同手腕屏微调的取舍）
        self._gate_save_job = self._root.after(300, self._save_gate_cfg)

    def _apply_gate_live(self) -> None:
        """把界面上的门限热更新到**正在跑**的引擎（不必等下次「开始翻译」）。

        gate 由采集线程读、界面线程写；写进去的是不可变标量（bool / float），
        CPython 里原子且不会读到半截值 —— 不用加锁。正在送的那一块音频不受影响：
        下一次 feed() 才看新阈值。
        """
        en = bool(self._gate_enabled_var.get())
        db = float(self._gate_var.get())
        n = 0
        for e in self._engines:
            g = getattr(e, "input_gate", None)
            if g is None:
                continue
            g.enabled = en
            g.threshold_db = db
            n += 1
        if n:
            print(f"[gui] 输入门限已热更新（{n} 条腿）：{'开' if en else '关'} "
                  f"{db:g} dBFS", flush=True)

    def _save_gate_cfg(self) -> None:
        """把门限写回 config.yaml 的 capture 段（就地改，保住注释与键顺序）。

        只写界面暴露的两项（`gate_enabled` / `gate_db`）：hold / preroll 保持文件里的
        原值 —— 那是手改的精细参数，不该被界面一次次覆盖回默认值。
        """
        self._gate_save_job = None
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        enabled = bool(self._gate_enabled_var.get())
        db = round(float(self._gate_var.get()), 1)
        try:
            text = p.read_text(encoding="utf-8")
            text = _yaml_set_or_create(text, ["capture", "gate_enabled"], _fmt_scalar(enabled))
            text = _yaml_set_or_create(text, ["capture", "gate_db"], _fmt_scalar(db))
            # hold / preroll 是「想细调才动」的旋钮，界面不暴露：
            # 文件里**缺**就补上当前生效值（让人在配置里看得见有这两个旋钮），
            # **已有就一个字都不动** —— 绝不覆盖用户手调过的精细值。
            try:
                cap_now = (yaml.safe_load(text) or {}).get("capture") or {}
            except Exception:  # noqa: BLE001
                cap_now = {}
            for key, val in (("gate_hold_ms", self._gate_hold_ms),
                             ("gate_preroll_ms", self._gate_preroll_ms)):
                if key not in cap_now:
                    text = _yaml_set_or_create(text, ["capture", key], _fmt_scalar(int(val)))
            _write_config_text(p, text)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存输入门限失败：{exc}", flush=True)
            return
        cap = self._cfg.output.setdefault("capture", {})
        cap["gate_enabled"] = enabled
        cap["gate_db"] = db
        print(f"[gui] 输入门限已写入 config.yaml：enabled={enabled} gate_db={db:g}"
              f"（hold {self._gate_hold_ms:g}ms / preroll {self._gate_preroll_ms}ms 沿用配置）",
              flush=True)
        self._set_status("info", t("输入门限已保存：{db} dB", db=f"{db:g}"))

    def _gate_probe_wanted(self) -> bool:
        """独立电平探针**该不该在跑**（四个条件同时成立）。

        ① 没在退出（`_closing`）；② 没有引擎在跑；③ 勾了「启用」；④ 设置窗**可见**。

        ⚠️ 窗口是常驻对象（建好即 withdraw，不是每次销毁重建），所以「开着吗」只能
        判 `winfo_viewable()`，判「对象在不在」永远为真。
        """
        if self._closing or self._headless or self._engines:
            return False
        var = getattr(self, "_gate_enabled_var", None)
        if var is None or not bool(var.get()):
            return False
        win = self._settings_win
        if win is None:
            return False
        try:
            return bool(win.winfo_viewable())
        except Exception:  # noqa: BLE001 — 窗口已销毁：当作「没开着」
            return False

    def _sync_gate_level_probe(self) -> None:
        """把探针对齐到 `_gate_probe_wanted()` 的判定（幂等，可以放心高频调）。

        调用点：开/关设置窗、勾「启用」、引擎启动与停止、`_poll` 每 100ms 兜底一次
        （兜底那次是为了保证「窗口关着时绝不可能还开着采集」—— 万一有别的路径
        把窗口藏起来而没走 `_close_settings`，这里最迟一跳就把它停掉）。

        「目标不在 / 目标变了」由**探针自己在后台低频处理**（`LevelProbe` 里
        每 `RETRY_S` 重试、每 `RECHECK_S` 复查采集目标）—— 界面这里不重建对象：
        这样不会每 100ms 刷一行日志，也不需要界面去懂 PipeWire 的目标集合。
        只有「用户取消勾选 / 关窗 / 开始翻译」才会把探针整个丢掉、设备立刻释放。
        """
        if not self._gate_probe_wanted():
            self._stop_gate_probe()
            return
        if self._gate_probe is not None:
            return                          # 在跑（或在后台等目标）就别动它
        capture_cfg = (self._cfg.output or {}).get("capture") or {}
        name = capture_cfg.get("loopback_device") or None
        probe = LevelProbe(device_name=name)
        self._gate_probe = probe
        probe.start()
        print(f"[level] 设置窗已打开且勾了「启用」→ 开始独立电平采集"
              f"（设备：{name or '自动检测'}；没在翻译，不占用引擎那条腿）", flush=True)

    def _stop_gate_probe(self) -> None:
        """停掉探针并释放设备（`LevelProbe.stop()` 内部会 join 线程 + close 源）。"""
        probe = self._gate_probe
        if probe is None:
            return
        self._gate_probe = None             # 先摘掉：_refresh_gate_level 立刻回到「—」
        was_running = probe.running
        probe.stop()
        if was_running:
            print(f"[level] 已停止独立电平采集并释放设备（本次共采 {probe.chunks} 块）",
                  flush=True)

    def _gate_level_db(self) -> float | None:
        """当前该画出来的电平（dBFS）；**没有可用来源**时返回 None（读数显示「—」）。

        优先级是硬的：**有引擎 → 只用引擎的** `input_gate.level_db`（那条腿本来就在采，
        电平与真正被上送的音频同源）；没引擎 → 用独立探针。两路并用等于白开一路
        loopback 去抢同一个 WASAPI 端点。
        """
        if self._engines:
            lvl = LEVEL_FLOOR_DB
            for e in self._engines:
                g = getattr(e, "input_gate", None)
                if g is not None:
                    lvl = max(lvl, float(g.level_db))
            return lvl
        probe = self._gate_probe
        # 探针开不了设备（在后台低频重试）/ 还没有第一块数据时 `has_data=False` / 读取
        # 异常停下 —— 这几种都显示「—」，绝不拿一个没有可信读数或已经死掉的来源假装有电平。
        if probe is not None and probe.running and probe.has_data:
            return float(probe.level_db)
        return None

    def _refresh_gate_level(self) -> None:
        """刷新设置窗里的实时电平条（每 100ms 一次）。

        横轴 -70 ~ 0 dBFS：门限是白竖线，蓝色填充 = 当前电平已超过门限（这段会被翻译）。
        数据来源见 `_gate_level_db()`：正在翻译时是引擎那条 loopback 腿，没翻译时是
        独立探针（设置窗可见 + 勾了「启用」才在采）；两路都没有就只画门限线、
        读数显示「—」—— 没有真在采集就不假装有数据。
        """
        cv = self._gate_level_canvas
        gvar = getattr(self, "_gate_var", None)
        if cv is None or gvar is None:
            return
        try:
            if not cv.winfo_exists():
                return
            w = max(10, int(cv.winfo_width()))
            h = max(6, int(cv.winfo_height()))
        except Exception:  # noqa: BLE001
            return
        active = bool(self._gate_enabled_var.get())
        thr = max(INPUT_GATE_MIN_DB, min(0.0, float(gvar.get())))
        lo, hi = INPUT_GATE_MIN_DB, 0.0
        x_thr = (thr - lo) / (hi - lo) * w
        cv.delete("all")                       # 每 100ms 重建（2 个图元，开销可忽略）
        cv.create_line(x_thr, 0, x_thr, h, fill=TEXT, width=2)
        lvl = self._gate_level_db()
        if lvl is None:
            self._gate_level_hold = LEVEL_FLOOR_DB
            if self._gate_level_lbl is not None:
                try:
                    self._gate_level_lbl.configure(text="—")
                except Exception:  # noqa: BLE001
                    pass
            return
        # 峰值保持（每 100ms 掉 1.5dB）：逐块读数跳得厉害，直接画会闪成噪声。
        # 只在这一处做 —— 引擎的 level_db 与探针的 level_db 都是「最近一块的瞬时值」，
        # 两条路进来的观感因此完全一致。
        self._gate_level_hold = max(lvl, self._gate_level_hold - 1.5, LEVEL_FLOOR_DB)
        db = max(lo, min(hi, self._gate_level_hold))
        x_lvl = (db - lo) / (hi - lo) * w
        cv.create_rectangle(0, 0, x_lvl, h, outline="",
                            fill=(ACCENT if (active and db >= thr) else SURFACE_HOVER))
        if self._gate_level_lbl is not None:
            self._gate_level_lbl.configure(text=f"{db:.0f} dB")

    # ================================================================ 队列轮询

    def _poll(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "text":
                    self._add_text(item[2], item[3], item[4], who=item[1])
                    self._push_overlay()   # 手腕屏镜像聊天区（同一块屏，两个方向都上）
                    self._push_desktop()   # 桌面字幕同一份内容（PC 桌面模式）
                elif kind == "status":
                    self._set_status(item[1], item[2])
                elif kind == "stats":
                    self._stats.update(item[1])
                    self._refresh_status()
                elif kind == "devices":
                    self._on_device_scan_result(item[1], item[2], item[3])
                elif kind == "devices_error":
                    self._set_status("warn", t("设备扫描失败：{msg}", msg=item[1]))
                    self._device_scan_pending = False
                elif kind == "update_check":
                    self._on_update_check_result(item[1], item[2], item[3], item[4])
                elif kind == "update_progress":
                    self._on_download_progress(item[1], item[2])
                elif kind == "update_download_done":
                    self._on_download_done(item[1])
                elif kind == "update_download_error":
                    self._on_download_error(item[1])
                elif kind == "room":
                    # 远端成员的一句话：进聊天气泡（who=peer:<id> 天然区分不同人），再上手腕屏。
                    # item = ("room", nick, peer_id, text, is_final)
                    self._add_text("", item[3], item[4], who=f"peer:{item[2]}", label=item[1])
                    self._push_overlay()
                elif kind == "room_status":
                    # 房间链路状态（来自房间线程，已入队）：按行首符号定状态栏颜色级别
                    txt = str(item[1])
                    level = "error" if txt.startswith("❌") else (
                        "warn" if txt.startswith("⚠️") else "info")
                    self._set_status(level, txt)
                elif kind == "voice_preview":
                    self._on_voice_preview_done(item[1], item[2], item[3])
                elif kind == "stop_done":
                    # 引擎收尾完成（后台线程入队）→ 恢复「开始翻译」。
                    # item = ("stop_done", 收尾用时, 引擎数, 是否有引擎超时未退出)
                    _, elapsed, n_engines, incomplete = item
                    # 超时也照样放开：收尾线程已经不再等了，一直禁着等于把界面永久锁死
                    # （只能重启应用）。代价是极端情况下可能与还在关设备的旧引擎抢麦克风 ——
                    # 那种失败会在启动路径上如实报错并留痕，比界面锁死可恢复得多。
                    self._start_btn.configure(state=tk.NORMAL)
                    self._stop_btn.configure(state=tk.DISABLED)
                    print(f"[gui] 停止收尾完成：{n_engines} 个引擎，用时 {elapsed:.2f}s"
                          + ("（有引擎超时未退出，仍在关设备）" if incomplete else ""),
                          flush=True)
                    # 引擎失败时状态栏已有 error 消息，别用"已停止"盖掉
                    if self._last_status_level != "error":
                        if incomplete:
                            # 降级路径也要在状态栏如实留痕（本仓库禁静默降级）：
                            # 只写「已停止」会骗人 —— 上一条腿其实还没收完。
                            self._set_status("warn", t("已停止（上一次会话仍在收尾）"))
                        else:
                            self._set_status("info", t("已停止"))
        except queue.Empty:
            pass
        if (self._pending_starts == 0 and self._engines
                and all(not e.running for e in self._engines)):
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            # 引擎失败时状态栏已有 error 消息，别用"已停止"盖掉
            if self._last_status_level != "error":
                self._set_status("info", t("已停止"))
            self._engines = []
            self._engine_dirs = []
        if self._overlay_out is not None:
            self._overlay_out.tick()      # 手腕屏的热重载 / 淡出
        if self._desktop_out is not None:
            self._desktop_out.tick()      # 桌面字幕：跟随目标窗口 / 热重载 / 补画
        # 房间行状态（连接态 + 在线人数）每 0.5s 刷一次：人数变化不经 on_status，只能轮询快照
        _now = time.monotonic()
        if _now >= self._room_status_next:
            self._room_status_next = _now + 0.5
            self._refresh_room_status_label()
        # 输入门限的实时电平条：每 100ms 刷一次（_poll 本身 50ms 一跳）
        self._gate_level_tick += 1
        if self._gate_level_tick % 2 == 0:
            # 顺带兜底同步探针启停：万一有别的路径把设置窗藏起来而没走 _close_settings，
            # 这里最迟一跳就把采集停掉 —— 「窗口关着时不许还开着采集」不能只靠调用点自觉。
            self._sync_gate_level_probe()
            if self._gate_level_canvas is not None:
                self._refresh_gate_level()
        self._root.after(50, self._poll)

    # ================================================================ 聊天气泡

    def _add_text(self, source: str, text: str, is_final: bool, who: str = "mine",
                  label: str = "") -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        cur = self._current.get(who)
        if cur is not None:
            # 流式增量：就地重画同一条气泡（终版只做"封口"，绝不新插第二条）
            cur.source = source
            cur.text = text
            if label:
                cur.label = label      # 远端昵称：partial 就地刷新时也带上（谁在说不能丢）
            if is_final:
                cur.final = True
                self._current.pop(who, None)
            if hasattr(self, "_canvas"):
                self._redraw_current(cur)
            if self._auto_scroll and hasattr(self, "_canvas"):
                self._canvas.yview_moveto(1.0)
            return
        # 没有正在刷新的气泡 → 新建（一句话的第一条就是终版时走这里）
        b = _Bubble(who=who, source=source, text=text, ts=now_str, final=is_final, label=label)
        if not is_final:
            self._current[who] = b
        b.y = (self._bubbles[-1].y + self._bubbles[-1].h + 8) if self._bubbles else 8
        self._bubbles.append(b)
        if hasattr(self, "_canvas"):
            self._draw_bubble(b)
            self._trim()
            self._update_scrollregion()
            if self._auto_scroll:
                self._canvas.yview_moveto(1.0)

    def _draw_bubble(self, b: _Bubble) -> int:
        """按已验证配方画一条气泡，返回下一条气泡的起始 y。

        每条气泡两行：**原文小字（上） + 译文大字（下）**——主次分明，
        你要读的永远是下面那行大字。服务端没返回原文（或原文与译文相同）时
        只画译文，不留空行。
        """
        cv = self._canvas
        w = self._canvas_w if self._canvas_w > 1 else 600
        maxw = int(w * 0.62)                       # width 参数自带自动换行

        # 1) 先量尺寸：两张文字都先画在 (0,0)，量完再挪进气泡
        tid_big = cv.create_text(0, 0, text=b.text, width=maxw,
                                 anchor="nw", font=FONT, fill=COLOR_TEXT)
        bx1, by1, bx2, by2 = cv.bbox(tid_big)
        big_w, big_h = bx2 - bx1, by2 - by1

        tid_small = None
        small_w = small_h = 0
        # 小字行：远端成员优先显示**昵称**（谁在说），本机气泡仍是原文。
        # 为空、或与大字译文相同时不画（省一行空白）。
        small_raw = b.label if (b.label or "").strip() else b.source
        small_key = (small_raw or "").strip()
        if small_key and small_key != (b.text or "").strip():
            tid_small = cv.create_text(0, 0, text=small_raw, width=maxw, anchor="nw",
                                       font=FONT_SMALL,
                                       fill=COLOR_SRC_MINE if b.who == "mine" else COLOR_SRC_THEIRS)
            sx1, sy1, sx2, sy2 = cv.bbox(tid_small)
            small_w, small_h = sx2 - sx1, sy2 - sy1

        gap = 6 if tid_small is not None else 0
        pad_x, pad_y = 12, 9
        bw = max(big_w, small_w) + pad_x * 2       # 气泡贴合内容自适应
        bh = small_h + gap + big_h + pad_y * 2
        y = b.y
        items = [tid_big]
        if tid_small is not None:
            items.append(tid_small)
        if b.ts:
            items.append(cv.create_text(
                w - 18 if b.who == "mine" else 18, y, text=b.ts,
                anchor="ne" if b.who == "mine" else "nw",
                font=FONT_META, fill=COLOR_META))
            y += 14
        bx = w - 18 - bw if b.who == "mine" else 18  # 右 / 左
        fill = COLOR_MINE if b.who == "mine" else COLOR_THEIRS
        rid = round_rect(cv, bx, y, bx + bw, y + bh, 12, fill=fill, outline="")
        # ⚠️ 必须压到最底层（tag_lower 不带第二参数）。若写成
        # `for t in items: cv.tag_lower(rid, t)`，第二次迭代会把矩形挪到
        # 大号文字之上，译文会被气泡背景整条遮住——原型阶段实测踩过这个坑。
        cv.tag_lower(rid)

        ty = y + pad_y
        if tid_small is not None:
            cv.coords(tid_small, bx + pad_x, ty)
            ty += small_h + gap
        cv.coords(tid_big, bx + pad_x, ty)         # 译文大字在下方
        items.append(rid)
        b.items = items
        b.h = y + bh - b.y
        return y + bh + 8

    def _redraw_current(self, b: _Bubble) -> None:
        """流式增量：删掉旧图元，在同一个起始 y 位置重画同一条气泡。"""
        for iid in b.items:
            self._canvas.delete(iid)
        old_h = b.h
        self._draw_bubble(b)
        delta = b.h - old_h
        if delta:
            # 气泡长高时把下面的气泡整体下移，避免重叠（双向同时时会用到）
            idx = self._bubbles.index(b)
            # ⚠️ Canvas.move(tagOrId, x, y) 只接受**一个** tagOrId。
            # 写成 `move(*other.items, 0, delta)` 会在 items 有 ≥2 个图元时抛
            # `TclError: wrong # args`（双行气泡必然有 ≥2 个图元）→ 下面的气泡不移位 → 重叠。
            # 用户实测日志里就抓到了这个 TclError。
            for other in self._bubbles[idx + 1:]:
                other.y += delta
                for iid in other.items:
                    self._canvas.move(iid, 0, delta)
            self._update_scrollregion()

    def _trim(self) -> None:
        """气泡超过上限时删最旧的若干条，剩余图元整体上移。"""
        removed = 0
        while len(self._bubbles) > MAX_BUBBLES:
            old = self._bubbles.pop(0)
            if self._current.get(old.who) is old:
                self._current.pop(old.who, None)
            removed += old.h + 8
            for iid in old.items:
                self._canvas.delete(iid)
        if removed:
            self._canvas.move("all", 0, -removed)
            for b in self._bubbles:
                b.y -= removed

    def _update_scrollregion(self) -> None:
        if not hasattr(self, "_canvas"):
            return
        content = 8
        if self._bubbles:
            last = self._bubbles[-1]
            content = last.y + last.h + 8
        h = self._canvas.winfo_height()
        self._canvas.configure(scrollregion=(0, 0, max(self._canvas_w, 1), max(content, h)))

    def _redraw_all(self) -> None:
        """窗口 resize 后按新宽度全部重画（消息多时也比逐条挪快）。"""
        self._relayout_job = None
        if not hasattr(self, "_canvas"):
            return
        self._canvas.delete("all")
        y = 8
        for b in self._bubbles:
            b.y = y
            y = self._draw_bubble(b)
        self._update_scrollregion()
        if self._auto_scroll:
            self._canvas.yview_moveto(1.0)

    # ================================================================ 状态栏

    def _set_status(self, level: str, msg: str) -> None:
        self._last_status_level = level
        if not hasattr(self, "_status_label"):
            return
        # 状态色走圆点，文字保持中性色——更现代，也不会整行刺眼
        colors = {"info": COLOR_OK, "warn": COLOR_WARN, "error": COLOR_ERROR}
        self._status_dot.configure(fg=colors.get(level, TEXT_MUTED))
        self._status_label.configure(text=t("状态：{msg}", msg=msg))

    def _refresh_status(self) -> None:
        if not hasattr(self, "_stats_label"):
            return
        parts: list[str] = []
        if any(e.running for e in self._engines):
            parts.append(t("运行中"))
            # 常驻提示：状态栏正文会被引擎消息覆盖，这里不会。
            # 用户放英文视频却没选对方向时，症状看着就是「翻译坏了」。
            if self._direction_var.get() == "mine":
                parts.append(t("仅翻译你说的话"))
        if any(e.chatbox is not None for e in self._engines):
            sent = sum(e.chatbox.sent_ok for e in self._engines if e.chatbox is not None)
            parts.append(t("已翻译 {n} 条", n=sent))
        ms = self._stats.get("first_delta_ms") or self._stats.get("connect_ms")
        if ms is not None:
            parts.append(t("首增量 {ms}ms", ms=f"{ms:.0f}"))
        self._stats_label.configure(text=" · ".join(parts))

    # ================================================================ 自检

    def run_self_test(self) -> int:
        pcm_path = BUNDLE_DIR / "testdata" / "zh_test_16k.pcm"
        if not pcm_path.exists():
            print(f"GUI_SELFTEST_FAIL: 测试音频不存在 {pcm_path}", file=sys.stderr)
            return 1

        results: list[tuple] = []

        def on_text(src, txt, final):
            results.append((src, txt, final))

        def on_status(level, msg):
            print(f"[selftest][{level}] {msg}")

        cfg = load_config()
        cfg.directions["mine"].source_lang = "zh"
        cfg.directions["mine"].target_lang = "en"

        events = EngineEvents(on_text=on_text, on_status=on_status)
        engine = Engine(
            cfg=cfg, direction="mine", source=f"pcm:{pcm_path}",
            sinks={"chatbox"}, events=events, dry_run=True,
        )
        engine.start()
        engine.join(timeout=60)
        engine.stop(timeout=5)

        has_source = any(r[0].strip() for r in results)
        has_target = any(r[1].strip() for r in results)
        if has_source and has_target:
            print("GUI_SELFTEST_OK")
            return 0
        print(f"GUI_SELFTEST_FAIL: source={has_source} target={has_target} rows={len(results)}",
              file=sys.stderr)
        return 1

    def run_self_test_dual(self) -> int:
        """双向同时验收：两个测试 PCM 同时驱动两个引擎，左右两侧都必须出气泡。"""
        zh = BUNDLE_DIR / "testdata" / "zh_test_16k.pcm"
        en = BUNDLE_DIR / "testdata" / "en_test_16k.pcm"
        for p in (zh, en):
            if not p.exists():
                print(f"GUI_SELFTEST_DUAL_FAIL: 测试音频不存在 {p}", file=sys.stderr)
                return 1

        cfg = load_config()
        cfg.directions.setdefault("mine", Direction())
        cfg.directions.setdefault("theirs", Direction())
        cfg.directions["mine"].source_lang = "zh"
        cfg.directions["mine"].target_lang = "en"
        cfg.directions["theirs"].source_lang = "en"
        cfg.directions["theirs"].target_lang = "zh"

        engines: list[Engine] = []
        for who, direction, pcm_path in (("mine", "mine", zh), ("theirs", "theirs", en)):
            def on_text(src, txt, final, who=who):
                self._add_text(src, txt, final, who=who)

            events = EngineEvents(
                on_text=on_text,
                on_status=lambda lvl, msg, who=who: print(f"[dualtest][{who}][{lvl}] {msg}"),
            )
            eng = Engine(cfg=cfg, direction=direction, source=f"pcm:{pcm_path}",
                         sinks={"chatbox"}, events=events, dry_run=True)
            engines.append(eng)
            eng.start()
            time.sleep(0.3)      # 与 GUI 一致：错开 300ms，避免同时抢占音频设备
        for eng in engines:
            eng.join(timeout=90)
        for eng in engines:
            eng.stop(timeout=5)

        mine = [b for b in self._bubbles if b.who == "mine"]
        theirs = [b for b in self._bubbles if b.who == "theirs"]
        if mine and theirs:
            print(f"GUI_SELFTEST_DUAL_OK mine={len(mine)} theirs={len(theirs)}")
            return 0
        print(f"GUI_SELFTEST_DUAL_FAIL: mine={len(mine)} theirs={len(theirs)}",
              file=sys.stderr)
        return 1


# ================================================================ 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 - 图形界面")
    ap.add_argument("--self-test", action="store_true", help="自动化验收模式（单方向）")
    ap.add_argument("--self-test-dual", action="store_true",
                    help="双向同时验收：两个测试 PCM 同时驱动两个引擎")
    args = ap.parse_args()

    # 崩溃日志：闪退时窗口一关什么都没了，必须落盘。
    # 在创建界面之前装上，连启动阶段的崩溃也能抓到。
    # 可写目录（打包后是 %APPDATA%\vrchat-livetranslate）：先迁移旧版放在 exe 旁边的
    # 配置、再确保目录存在，最后才装崩溃日志 —— 顺序反了日志就会写到旧位置/临时目录。
    from .paths import ensure_app_dir, migrate_legacy_files

    migrate_legacy_files()
    ensure_app_dir()
    crashlog.install(ROOT / "logs", "gui")
    crashlog.log_startup_info(f"gui {'--self-test-dual' if args.self_test_dual else args.self_test and '--self-test' or ''}")

    if args.self_test_dual:
        gui = TranslationGUI(headless=True)
        return gui.run_self_test_dual()
    if args.self_test:
        gui = TranslationGUI(headless=True)
        return gui.run_self_test()

    gui = TranslationGUI()
    crashlog.install_tk(gui._root)      # 接管 Tk 回调异常（默认只打印不退出）
    try:
        gui._root.mainloop()
    finally:
        crashlog.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
