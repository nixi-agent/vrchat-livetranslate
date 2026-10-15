"""平台抽象层。

共享代码只从这里拿平台能力，**不要**直接 `import vlt.platform.win` /
`vlt.platform.linux` —— 那是平台独占模块，打包时会被 `--exclude-module` 剔除，
直接 import 会让另一个平台的产物在启动时就 ImportError。

    ✅ from ..platform import IS_WINDOWS, open_path
    ✅ from ..platform import device_backend
    ❌ from ..platform import linux          # 只有 linux.py 自己内部可以这么写

平台模块的加载是**惰性的**：只有真正用到时才 import，所以缺哪一侧的实现都
不会影响「另一侧 + 共享代码」的启动。这也让 Windows 产物的字节码里
根本不含 Linux 实现的任何字样（结构性保证，见 vlt/platform/base.py 的说明）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

__all__ = [
    "IS_WINDOWS", "IS_LINUX",
    "backend", "device_backend",
    "child_env", "open_path", "find_cjk_font", "find_thai_font", "find_korean_font",
    "first_openable_font", "detect_ui_language",
    # 桌面叠加窗（issue #11）：缺失实现的平台会拿到下面的安全默认值
    "desktop_window_backend", "find_game_window", "window_client_rect", "is_window",
    "set_click_through", "set_tool_window", "set_window_shape",
    "top_level_hwnd", "screen_work_area",
    "monitor_work_area", "create_desktop_window",
]

_backend: Any = None


def backend() -> Any:
    """返回当前平台的实现模块（`vlt/platform/win.py` 或 `vlt/platform/linux.py`）。

    惰性导入 + 缓存。不支持的平台在这里就报清楚，而不是等某处神秘 AttributeError。
    """
    global _backend
    if _backend is not None:
        return _backend

    if IS_WINDOWS:
        from . import win as mod
    elif IS_LINUX:
        from . import linux as mod
    else:
        raise RuntimeError(
            f"不支持的平台：{sys.platform!r}（本项目只做 Windows 与 Linux）")

    _backend = mod
    return mod


def device_backend() -> Any:
    """设备枚举后端（`query_devices` / `query_loopback_devices`）。"""
    return backend()


def capture_backend() -> Any:
    """采集后端（`open_mic` / `open_loopback` → `AudioSource`）。

    与 `device_backend()` 取的是同一个模块，分开命名只是为了让调用点自解释：
    「我要枚举设备」还是「我要开一路采集」。
    """
    return backend()


def output_device_fallbacks(name: str, exclude: int | None = None) -> list[int]:
    """同名**输出**设备在其它 host API 下的索引 —— 首选打不开时按序回落。

    为什么需要（2026-10-02 测试者真机）：设备表收敛到 WASAPI 之后，个别虚拟声卡的 WASAPI
    端点打不开（Voicemeeter：`-9999 Unanticipated host error … WdmSyncIoctl … GLE = 0x490`），
    而它的 MME 条目能正常打开 —— 有候选就不至于把「译音输出」这条腿直接判死。

    平台差异：只有 Windows 实现了 `same_name_fallbacks`（同名设备在 MME/DirectSound/WASAPI/
    WDM-KS 下各有一条，索引可互换）；其它平台返回空列表（PipeWire 的索引不是这样互换的），
    调用方看到的语义是「没有候选」，行为与改动前一致。
    """
    fn = getattr(device_backend(), "same_name_fallbacks", None)
    if fn is None:
        return []
    try:
        return list(fn(name, "output", exclude=exclude))
    except Exception:                       # noqa: BLE001 — 回落候选拿不到不该影响主流程
        return []


class _NullWristOverlay:
    """`overlay.backend: null` 时用的空实现（接口与真后端一致，永远不可用）。"""

    available = False

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        self.cfg = None

    def start(self) -> bool:
        return False

    def update(self, *_a: Any, **_kw: Any) -> None: ...

    def update_entries(self, *_a: Any, **_kw: Any) -> None: ...

    def tick(self) -> None: ...

    def close(self) -> None: ...


def create_wrist_overlay(cfg: Any, config_path: Any = None, dry_run: bool = False) -> Any:
    """手腕屏后端（`start/update/update_entries/tick/close/available` 两端一致）。

    `cfg.backend`：
      * `auto`（默认）→ 按平台选（Windows: pyopenvr；Linux: 自建 OpenXR）
      * `null`        → 禁用（配置层的总开关，不用改 `enabled` 也能关掉）

    ⚠️ 不提供强行指定 openvr/openxr —— 那会让平台隔离破功：
        Windows 产物里会出现 pyopenxr 的字样。真要强制，请在对应平台的
        模块里做，而不是让共享代码引用另一个平台的实现。
    """
    backend_name = str(getattr(cfg, "backend", "auto") or "auto").lower()
    if backend_name == "null":
        return _NullWristOverlay(cfg)
    if backend_name not in ("auto", ""):
        import logging
        logging.getLogger(__name__).warning(
            "[overlay] 配置里的 backend=%r 不支持（只认 auto/null）→ 按 auto 处理", backend_name)
    return backend().create_wrist_overlay(cfg, config_path=config_path, dry_run=dry_run)


def open_audio_out(audio_cfg: dict, on_status: Any) -> Any:
    """译音输出（虚拟声卡 + 写入端）。失败返回 None，由调用方决定怎么提示。

    目前**只有 Linux 需要这个入口**：它得先「运行时声明」一对 PipeWire 节点
    （无配置文件、无重启、不改任何全局状态），再把 PCM 写进去。

    Windows 侧不需要声明 —— 虚拟声卡（VB-Cable / VoiceMeeter）是用户事先装好的设备，
    引擎直接按设备名用 PortAudio 打开即可，那条路径留在 `vlt/engine.py` 里没动。
    """
    return backend().open_audio_out(audio_cfg, on_status)


def create_mic_proxy(audio_cfg: dict, mic_name: str | None = None,
                     on_status: Any = None) -> Any:
    """麦克风代理（原声 / 译音一键切）。两端各有一份实现，接口与语义对齐。

    * Windows → `vlt/output/micproxy.py`：`sounddevice` 输出流，虚拟声卡是用户自备设备
    * Linux  → `vlt/output/micproxy_linux.py`：`pw-cat` 写管道，虚拟声卡**运行时声明**

    生命周期归调用方（GUI）：`start()` 失败只降级、不抛异常；`close()` 幂等。
    引擎侧只认 `proxy.translated_sink`（鸭子类型垫片），不感知代理在哪。
    """
    if on_status is None:
        on_status = lambda *_a: None  # noqa: E731
    return backend().create_mic_proxy(audio_cfg, mic_name, on_status)


def child_env() -> dict[str, str]:
    """给「宿主子进程」用的环境变量：还原被 PyInstaller 改过的动态库搜索路径。

    ## 为什么必须这么做（2026-10 官方 AppImage 真机故障）

    frozen（AppImage / 单文件 exe）时，PyInstaller 引导器会把包内目录**前置**进
    `LD_LIBRARY_PATH`（原值存进 `LD_LIBRARY_PATH_ORIG`），所有子进程都会继承。
    宿主程序（`xdg-open` 拉起的 thunar、PipeWire 的 `pw-*` 工具等）于是先加载到
    **包内自带的另一套系统库**，按构建机的版本顶掉宿主的：

      * 官方 AppImage 在 Ubuntu 上构建 → 包内 `libfontconfig` 2.15 缺
        `FcConfigSetDefaultSubstitute`，而 Arch 的 `libpangoft2` 需要它 →
        宿主 thunar 秒退（`symbol lookup error`）→ 点「打开日志文件夹」没反应；
        本地构建因包内库就是从本机收的，才侥幸没事。
      * `libstdc++.so.6` / `libgcc_s.so.1` 等同理（构建机 GLIBCXX 版本较旧的场合）。

    口径（PyInstaller 官方文档的推荐做法）：

      * 有 `LD_LIBRARY_PATH_ORIG` → 还原为它（原值为空则删掉变量）；
      * 没有 ORIG 且 `sys.frozen` → 删除 `LD_LIBRARY_PATH`（引导器刚设的）；
      * 源码运行 / Windows → 原样返回副本（用户自己设的路径必须保留）。
    """
    env = dict(os.environ)
    if IS_WINDOWS:
        return env
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        return env
    if getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


# `open_path` 启动后等一小段：xdg-open 的 generic 分支会在前台等文件管理器退出
# （还活着 = 已交接），失败分支（找不到默认程序 / 子进程加载即崩）在毫秒级就退出。
# 实测失败退出 < 0.15s；0.6s 是给慢机器的余量，也是这次点击对界面的最大阻塞。
_OPEN_PATH_GRACE_S = 0.6


def open_path(p: str | Path) -> None:
    """用系统默认程序打开一个文件/目录。失败抛异常，由调用方决定怎么提示。

    Windows：`os.startfile`（不产生子进程）；Linux：`xdg-open`。
    打不开**不许静默**（`gui._on_open_log_folder` 的做法：状态栏 + 日志都留痕）：

      * 子进程必须带 `child_env()` —— 绝不能继承 PyInstaller 包内的库搜索路径，
        否则宿主文件管理器加载即崩、stderr 又被 /dev/null 吞掉，用户只看到
        「点了没反应」（详见 `child_env` 注释）；
      * 不再把 stderr 丢进 DEVNULL 且不看退出码：启动后短暂等待，已经退出且
        非零（例如「no method available」）就连同 stderr 一起抛出去；
        还在跑说明已交给文件管理器，正常返回。
    """
    target = str(p)
    if IS_WINDOWS:
        os.startfile(target)                      # type: ignore[attr-defined]
        return

    errfile = tempfile.TemporaryFile(prefix="vlt-open-path-")
    try:
        proc = subprocess.Popen(
            ["xdg-open", target],
            stdout=subprocess.DEVNULL, stderr=errfile,
            start_new_session=True,                   # 别跟着我们一起被杀
            env=child_env(),
        )
        try:
            rc = proc.wait(timeout=_OPEN_PATH_GRACE_S)
        except subprocess.TimeoutExpired:
            return                                # 还活着：已交接给文件管理器
        if rc != 0:
            errfile.seek(0)
            err = errfile.read(4096).decode("utf-8", "replace").strip()
            detail = f"退出码 {rc}" + (f"：{err}" if err else "")
            raise RuntimeError(f"xdg-open 打不开 {target}（{detail}）")
    finally:
        errfile.close()


def find_cjk_font() -> str | None:
    """找一个能画中日韩的字体文件路径；找不到返回 None（由调用方回落）。

    配置里 `overlay.font` 默认不再写死 `C:/Windows/Fonts/msyh.ttc` ——
    那是 Windows 专有路径，在别的机器上会让渲染直接抛 `OSError: cannot open resource`
    （Linux 上 `test_wrap.py` 就是这么挂的）。
    """
    return backend().find_cjk_font()


def first_openable_font(candidates: Iterable[str]) -> str | None:
    """按序返回第一份**真能打开**的字体文件（不存在 / Pillow 打不开的候选直接跳过）。

    为什么这一步放在平台层：字体候选表要么来自 `fc-list`（Linux，它给的只是**扫描顺序**，
    不是最佳匹配 —— 实测可能是位图字体，Pillow 打不开），要么是固定路径表（Windows，
    可能被用户删过）。把一个打不开的路径交给上层，症状是「静默回落主字体 = 一排豆腐块」；
    在候选表里多试一份更便宜，也顺便让上层的降级留痕说的是真原因。

    ⚠️ 「这份字体是否**真含目标书写系统**的字形」不在这里判 —— 那需要渲染侧的样本字符，
    见 `vlt/output/overlay.py::font_covers`（判据：渲染样本 vs 渲染缺字位，位图相同就是没有）。
    本函数只保证「能交给 Pillow 开」，不保证「能画那门语言」。
    """
    from PIL import ImageFont
    for cand in candidates or ():
        path = (cand or "").strip()
        if not path or not Path(path).exists():
            continue
        try:
            ImageFont.truetype(path, 16)
        except Exception:  # noqa: BLE001 — 打不开就试下一个候选
            continue
        return path
    return None


def find_thai_font() -> str | None:
    """找一个含泰文字形的字体文件路径；找不到返回 None（由调用方回落）。

    与 `find_cjk_font()` 是**两条独立**的探测：CJK 字体（雅黑 / Noto Sans CJK 等）
    不含泰文字形，泰文字体（Leelawadee UI / Noto Sans Thai 等）不含中日韩字形 ——
    混排时必须按书写系统切 run、各用各的字体画，否则会出豆腐块。

    Windows 侧在 `win.py` 的 `_THAI_FONT_CANDIDATES`（LeelawUI.ttf → tahoma.ttf → …），
    Linux 侧在 `linux.py` 用 `fc-list :lang=th` + 已知路径兜底。
    """
    return backend().find_thai_font()


def find_korean_font() -> str | None:
    """找一个含韩文字形的字体文件路径；找不到返回 None（由调用方回落）。

    与 `find_cjk_font()` / `find_thai_font()` 是**三条独立**的探测：CJK 字体不一定含韩文字形
    （Windows 的默认 CJK 字体 msyh.ttc 实测**不含** → 韩文全是方框，用户报过的现象），
    韩文字体（Malgun Gothic 等）不含中日文字形 —— 混排时必须按书写系统切 run、各用各的字体画。

    Windows 侧在 `win.py` 的 `_KOREAN_FONT_CANDIDATES`（malgun.ttf → gulim.ttc → …），
    Linux 侧在 `linux.py` 用 `fc-list :lang=ko` + 已知路径兜底。
    """
    return backend().find_korean_font()


def detect_ui_language() -> str:
    """探测系统界面语言，返回本项目支持的代码（zh/en/ja/ko/ru）。

    两端口径必须一致：**探测不到 / 不支持的语言一律回落 "en"** ——
    外国用户按英文接待远比按中文合理。

    `vlt/i18n.py: detect_system_language()` 就是转调这里（界面启动时用它定语言），
    所以这条口径只有一处实现：Windows 在 `win.py`（读 LANGID），Linux 在 `linux.py`
    （读 `LC_ALL`/`LC_MESSAGES`/`LANG`）。
    """
    return backend().detect_ui_language()


# ---------------------------------------------------------------- 桌面叠加窗（issue #11）
#
# 桌面模式的字幕窗（`vlt/output/desktop_overlay.py`，**共享模块**）需要几件平台事实：
# 找游戏窗口、拿它的客户区、给自己的窗口打上鼠标穿透/不抢焦点、知道屏幕工作区多大，
# 以及在 Linux 上要一个**原生窗**（Wayland layer-shell，逐像素透明/协议级穿透）。
# Windows 侧在 `win.py` 用 Win32 扩展样式实现；Linux 侧在 `linux.py`
# （X11 直调 libX11/libXext；Wayland 直调 libwayland-client，见 `wayland.py`）。
#
# ⚠️ 门面在这里**兜底**而不是让共享模块去 import 平台独占模块：
#   * 缺失的实现 → 每个函数返回安全默认值（None / False / 原值 / (0,0,1920,1080)；
#     `create_desktop_window` 返回 None 让调用方回落 Tk）；
#   * 降级（回落 Tk、没找到窗口等）由调用方/后端打日志说明，门面自己不吭声；
#     测试进程里建原生窗会被 `linux.py` 的防呆拒绝（不许碰用户会话的合成器）。
#
# 判定「有没有桌面窗口能力」用 `find_window_by_title` 这一个属性作探针：它是这套能力里
# 最核心的一个，缺了它其余几个也没有意义。

_DEFAULT_WORK_AREA = (0, 0, 1920, 1080)


def desktop_window_backend() -> Any:
    """有桌面窗口能力的后端模块；没有（或平台不支持）返回 None。"""
    try:
        mod = backend()
    except Exception:  # noqa: BLE001 — 不支持的平台：桌面字幕整条腿禁用，别把进程带崩
        return None
    if getattr(mod, "find_window_by_title", None) is None:
        return None
    return mod


def _desktop_call(name: str, default: Any, *args: Any) -> Any:
    """转调后端同名函数；后端没有 / 调用抛异常 → 返回 `default`。

    调用点都在 50ms 一跳的 tick 里，任何异常冒出去都会打断翻译腿。
    """
    mod = desktop_window_backend()
    fn = getattr(mod, name, None) if mod is not None else None
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return default


def find_game_window(title: str, exclude: Any = ()) -> int | None:
    """按标题（子串、不区分大小写）找目标游戏窗口的顶层句柄；找不到返回 None。

    `exclude` = 要排除的句柄（通常是本程序自己的主窗与字幕窗）—— 本程序主窗标题
    「VRChat 实时同传」也含 "VRChat"，不排除的话 VRChat 没起来时会贴到我们自己界面上。
    """
    hwnd = _desktop_call("find_window_by_title", None, title, exclude)
    try:
        return int(hwnd) if hwnd else None
    except (TypeError, ValueError):
        return None


def window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """目标窗口客户区的屏幕矩形 (left, top, right, bottom)；取不到返回 None。"""
    rect = _desktop_call("window_client_rect", None, hwnd)
    try:
        left, top, right, bottom = (int(v) for v in rect)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def is_window(hwnd: int) -> bool:
    """句柄还是一个真窗口吗（游戏关掉/换场景后变 False，调用方据此重新找窗口）。"""
    return bool(_desktop_call("is_window", False, hwnd))


def set_click_through(hwnd: int, on: bool) -> bool:
    """开/关鼠标穿透。返回是否真的设上了（False = 本平台没这能力，调用方要留一行日志）。"""
    return bool(_desktop_call("set_click_through", False, hwnd, on))


def set_tool_window(hwnd: int) -> bool:
    """标记为「不抢焦点 + 不进 alt-tab」的工具窗。返回是否设上了。"""
    return bool(_desktop_call("set_tool_window", False, hwnd))


def set_window_shape(hwnd: int, mask: bytes, width: int, height: int) -> bool:
    """给窗口设/换 1 位形状蒙版（蒙版外的像素不画、也不吃鼠标）。返回是否设上了。

    Linux 的 **Tk 回落路径**用它抠掉面板外的键色底（Tk 没有 Windows 那种
    `-transparentcolor`）：每帧把 RGBA 面板转成蒙版字节（口径见 `linux.set_window_shape`
    与 `desktop_overlay.alpha_mask_bits`），尺寸对不上/句柄失效会被后端拒绝 → False，
    调用方降级（留一行日志，不再重试刷屏）。本平台没实现（Windows）→ 同样 False。
    """
    return bool(_desktop_call("set_window_shape", False, hwnd, mask, width, height))


def top_level_hwnd(widget_id: int) -> int:
    """Tk 的 `winfo_id()` 给的是子窗口句柄 → 换成能设扩展样式的顶层句柄。

    拿不到就返回原值（`widget_id` 本身可能就是顶层）。
    """
    hwnd = _desktop_call("top_level_hwnd", None, widget_id)
    try:
        return int(hwnd) if hwnd else int(widget_id)
    except (TypeError, ValueError):
        return int(widget_id)


def _as_work_area(rect: Any) -> tuple[int, int, int, int] | None:
    """把后端给的 (left, top, right, bottom) 校验成合法工作区；不合法返回 None。

    ⚠️ **不做 `max(0, ...)` 之类的钳制**：多屏时副屏在主屏左侧/上方，坐标本来就是负的，
    钳一下就把副屏工作区改成了错的东西（字幕会被夹到主屏里去）。
    """
    try:
        left, top, right, bottom = (int(v) for v in rect)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def screen_work_area() -> tuple[int, int, int, int]:
    """主屏工作区（不含任务栏）；取不到给 1920x1080 的保守值。

    不能返回 (0,0,0,0)：调用方拿它做夹取，那会把字幕夹成左上角一个点。
    """
    return _as_work_area(_desktop_call("screen_work_area", None)) or _DEFAULT_WORK_AREA


def monitor_work_area(hwnd: int) -> tuple[int, int, int, int]:
    """`hwnd` **所在那块显示器**的工作区；取不到就回落主屏工作区（绝不抛）。

    多显示器时贴窗必须用这一条：`screen_work_area()` 只有主屏那一份，副屏
    （尤其坐标为负的左侧副屏）上的字幕会被它夹回主屏。没有窗口句柄（自由模式）
    或本平台没有这项能力（Linux）时，回落到主屏工作区就还是原来的行为。
    """
    return (_as_work_area(_desktop_call("monitor_work_area", None, hwnd))
            or screen_work_area())


def create_desktop_window(size: tuple[int, int], alpha: float = 1.0,
                          click_through: bool = True, on_drag_end: Any = None,
                          backend: str = "auto") -> Any:
    """本平台的**原生桌面叠加窗**（Linux：Wayland layer-shell / X11 ARGB 覆盖窗）。

    返回 `None` = 本平台/本会话没有原生实现（Windows、没有 layer-shell 的合成器、
    强制 `backend=tk` 等），调用方（`vlt/output/desktop_overlay.py`）回落 Tk 那条腿。

    `backend` 是上层配置透传的选择（`auto|native|tk|wayland|x11`，见
    `vlt/output/desktop_overlay.py:BACKENDS`）—— **怎么挑、挑不到为什么**由后端
    模块（`linux.py`）自己打日志，门面不吭声、也不兜异常（调用方接了异常）。

    返回对象必须满足 `vlt/platform/base.py:DesktopWindow` 的窗口契约。
    """
    mod = desktop_window_backend()
    fn = getattr(mod, "create_desktop_window", None) if mod is not None else None
    if fn is None:
        return None
    return fn(size=size, alpha=alpha, click_through=click_through,
              on_drag_end=on_drag_end, backend=backend)
