"""把界面打包成**单文件、无控制台**的 Windows exe。

用法：
    .venv/Scripts/python.exe scripts/build_exe.py            # 打包 + 自检
    .venv/Scripts/python.exe scripts/build_exe.py --no-verify  # 只打包不跑自检

    # 没建项目 venv 也能跑（CI / conda / 系统 Python）：自动退回当前解释器
    python scripts/build_exe.py --no-verify

产物：`dist/VRChatLiveTranslate.exe`（单文件，双击即用，无控制台窗口）

## 为什么要这么多 --collect-all / --hidden-import

这个项目的第三方库大多是**在函数里按需导入**的（为了在没有对应硬件/驱动的机器上
也能启动），PyInstaller 的静态分析看不到它们，漏了就是运行时报
`ModuleNotFoundError`。而 pyaudiowpatch / sounddevice / openvr 还各自带**二进制**
（PortAudio、SteamVR 接口），必须 `--collect-all` 连数据一起收。

## 打完之后必须真跑一遍

单文件 exe 冷启动会解包到临时目录，很多问题（缺数据文件、路径指向临时目录）
只有真跑起来才暴露。所以默认会拿 `--self-test` 跑一次，并从 exe 旁的
`logs/` 日志里确认结果 —— 而不是只看"构建成功"。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
# 没建项目 venv 也能跑（CI / conda / 系统 Python 直接调）：退回**当前解释器**。
# 唯一前提是「PyInstaller 与 requirements 装在它里面」——CI 就是靠这条，
# 以前这里写死 .venv 路径，一上 CI 直接报「找不到虚拟环境解释器」。
PY = _VENV_PY if _VENV_PY.exists() else Path(sys.executable)
ENTRY = REPO / "run_gui.py"
APP_NAME = "VRChatLiveTranslate"          # 文件名用 ASCII：跨工具链更省事
DIST = REPO / "dist"
BUILD = REPO / "build"
ICON = REPO / "assets" / "app.ico"        # exe 图标（16/24/32/48/64/128/256 多尺寸）

# 按需导入的库（静态分析看不到）→ 显式声明
HIDDEN = [
    "pyaudiowpatch", "openvr", "sounddevice", "miniaudio",
    "pythonosc", "websockets", "yaml", "PIL", "numpy",
    "vlt", "vlt.paths", "vlt.version", "vlt.config", "vlt.credentials", "vlt.crashlog",
    "vlt.devices", "vlt.engine", "vlt.audio_dsp", "vlt.gui", "vlt.app",
    "vlt.session", "vlt.session.base", "vlt.session.qwen38", "vlt.session.qwen35",
    "vlt.output", "vlt.output.chatbox", "vlt.output.overlay", "vlt.output.virtualmic",
    "vlt.output.openvr_overlay",        # Windows/SteamVR 手腕屏后端（Linux 产物里没有它）
    "vlt.output.win_d3d11",             # 持久 D3D11 贴图（SetOverlayTexture 上传；Windows 独占）
    "vlt.platform", "vlt.platform.base", "vlt.platform.win",
]
# 带二进制/数据文件的库 → 连数据一起收
COLLECT_ALL = ["pyaudiowpatch", "sounddevice", "openvr", "pythonosc"]

# ⚠️ Windows 产物里**不允许**出现 Linux 独占实现 —— 这条要求不能靠人记，靠构建排除：
#   * vlt.platform.linux       —— PipeWire 设备枚举（pw-dump）+ 采集/虚拟声卡
#   * vlt.output.openxr_overlay —— 自建 OpenXR 手腕屏（pyopenxr + EGL/Wayland）
#   * xr                        —— pyopenxr 本身（排掉它，顺带排掉它拖进来的 PyOpenGL/glfw）
# 排除掉之后，exe 的字节码里连 "pipewire" / "XR_EXTX_overlay" 这些字样都不会有。
# 自动断言见 scripts/check_platform_purity.py（CI 里跑，红灯门禁）。
#
# 注意：**只排除平台独占模块**。`vlt/output/overlay.py` 是**共享的**（只有
# OverlayConfig + 渲染函数），两个平台都收；手腕屏后端各自独占一个模块：
#   * Windows → vlt/output/openvr_overlay.py（SteamVR 接口；**必须**收进 Windows 产物）
#   * Linux   → vlt/output/openxr_overlay.py（上面排掉）
# Linux 产物则由 AppImage 构建脚本反向删掉 openvr_overlay.py，
# 两边都由 scripts/check_platform_purity.py 断言。
EXCLUDE_WIN = [
    "vlt.platform.linux",
    "vlt.platform.wayland",    # 原生 layer-shell 桌面叠加窗（libwayland-client）
    "vlt.platform.x11",        # 原生 ARGB 桌面叠加窗（libX11）
    "vlt.output.openxr_overlay",
    "vlt.output.micproxy_linux",   # Linux 麦克风代理（pw-cat 管道 + 运行时声明虚拟麦）
    "xr",                      # pyopenxr
]



def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, cwd=str(REPO), **kw)


def ensure_pyinstaller() -> None:
    have = run([str(PY), "-m", "PyInstaller", "--version"], capture_output=True, text=True)
    if have.returncode == 0:
        print(f"PyInstaller 已就绪：{have.stdout.strip()}", flush=True)
        return
    print(f"未安装 PyInstaller，正在装入 {PY} ...", flush=True)
    # ⚠️ 这个 venv 是 `uv venv` 建的，**不带 pip** —— `python -m pip` 会直接
    # 报 `No module named pip`。所以先试 uv（本项目一贯的装法），失败再退回 pip。
    attempts = [
        ["uv", "pip", "install", "--python", str(PY), "pyinstaller>=6.6"],
        [str(PY), "-m", "pip", "install", "-q", "pyinstaller>=6.6"],
    ]
    errs: list[str] = []
    for cmd in attempts:
        try:
            res = run(cmd, text=True)
        except FileNotFoundError as exc:
            errs.append(f"{cmd[0]}：{exc}")
            continue
        if res.returncode == 0:
            print("PyInstaller 安装完成 ✓", flush=True)
            return
        errs.append(f"{' '.join(cmd)} → 退出码 {res.returncode}")
    raise SystemExit(
        "PyInstaller 装不上。手动执行：\n"
        f"  uv pip install --python \"{PY}\" pyinstaller\n"
        "已尝试：\n  " + "\n  ".join(errs)
    )


def gen_buildinfo() -> Path:
    """构建期烘焙 git 元数据 → `build/buildinfo.json`（冻结产物里没有 .git，见 vlt/version.py）。"""
    out = BUILD / "buildinfo.json"
    res = run([str(PY), str(REPO / "scripts" / "gen_buildinfo.py"), str(out)], text=True)
    if res.returncode != 0 or not out.exists():
        raise SystemExit("生成 build/buildinfo.json 失败（版本溯源需要它）")
    return out


def build() -> Path:
    for d in (DIST, BUILD):
        shutil.rmtree(d, ignore_errors=True)
    buildinfo = gen_buildinfo()
    cmd = [str(PY), "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile",            # 单文件
           "--noconsole",          # 无控制台窗口（GUI 程序）
           "--name", APP_NAME,
           "--paths", str(REPO),
           "--add-data", f"{REPO / 'config.example.yaml'}{';'}.",   # 首次运行要生成 config.yaml
           "--add-data", f"{REPO / 'testdata'}{';'}testdata",        # --self-test 用
           "--add-data", f"{REPO / 'assets'}{';'}assets",            # 图标 + 赞助弹窗的两张收款码
           "--add-data", f"{buildinfo}{';'}.",                       # 版本溯源（git describe，见 vlt/version.py）
           ] + (["--icon", str(ICON)] if ICON.exists() else [])
    if not ICON.exists():
        print(f"[!] 没找到图标 {ICON}，本次打包不带自定义图标", flush=True)
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for c in COLLECT_ALL:
        cmd += ["--collect-all", c]
    for e in EXCLUDE_WIN:
        cmd += ["--exclude-module", e]
    cmd.append(str(ENTRY))

    res = run(cmd, text=True)
    if res.returncode != 0:
        raise SystemExit("打包失败，见上面 PyInstaller 的输出")
    exe = DIST / f"{APP_NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"打包结束但没找到产物：{exe}")
    return exe


def frozen_app_dir(exe: Path) -> Path:
    """打包后的**可写目录**：`%APPDATA%\\vrchat-livetranslate`。

    与 `vlt/paths.app_dir()` 保持一致（那边跑在 exe 内部，这里跑在构建脚本里）。
    例外：exe 旁边有 `portable.txt` → 用 exe 所在目录（绿色版）。
    """
    if (exe.parent / "portable.txt").exists():
        return exe.parent
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "vrchat-livetranslate"


def verify(exe: Path, timeout_s: int = 180) -> bool:
    """真跑一遍 exe，并从它自己写的日志里确认结果（无控制台，看不到 stdout）。"""
    log_dir = frozen_app_dir(exe) / "logs"
    for old in log_dir.glob("gui_*.log"):
        old.unlink(missing_ok=True)
    print(f"\n开始自检：{exe.name} --self-test（最多等 {timeout_s}s）", flush=True)
    t0 = time.time()
    proc = subprocess.run([str(exe), "--self-test"], cwd=str(exe.parent),
                          timeout=timeout_s, capture_output=True,
                          # ⚠️ 不要 text=True：exe 在 Windows 控制台下的输出不是 UTF-8，
                          # 解码会在读取线程里抛 UnicodeDecodeError（实测噪音很大）。
                          encoding="utf-8", errors="replace")
    cost = time.time() - t0
    print(f"退出码 = {proc.returncode}（耗时 {cost:.1f}s）", flush=True)

    logs = sorted(log_dir.glob("gui_*.log"))
    text = logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""
    if logs:
        print(f"日志：{logs[-1]}", flush=True)
        for line in text.splitlines():
            if any(k in line for k in ("GUI_SELFTEST", "会话已建立", "chatbox", "API key", "❌", "Traceback")):
                print("   ", line.strip(), flush=True)
    ok = proc.returncode == 0 and "GUI_SELFTEST_OK" in text
    print(("✅ 自检通过" if ok else "❌ 自检未通过") + "（判定依据：退出码 + 日志里的 GUI_SELFTEST_OK）",
          flush=True)
    if not ok and not logs:
        print("   ⚠️ 可写目录的 logs/ 里没有日志 —— 说明连崩溃日志都没写出来，"
              "多半是启动阶段就挂了", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 VRChat 实时同传为单文件 exe（无控制台）")
    ap.add_argument("--no-verify", action="store_true", help="只打包，不跑 --self-test 自检")
    args = ap.parse_args()

    if not PY.exists():
        raise SystemExit(f"找不到可用的解释器：{PY}")
    if not _VENV_PY.exists():
        print(f"[i] 没找到项目 venv（{_VENV_PY}）→ 用当前解释器打包：{PY}", flush=True)
    ensure_pyinstaller()
    exe = build()
    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"\n✅ 产物：{exe}\n   大小：{size_mb:.1f} MB（单文件、无控制台）", flush=True)
    print(f"   配置与日志写在：{frozen_app_dir(exe)}"
          "（exe 放在只读目录也能跑；想改成绿色版——配置跟 exe 走——"
          "在 exe 旁边放一个 portable.txt 即可）", flush=True)

    if args.no_verify:
        return 0
    return 0 if verify(exe) else 1


if __name__ == "__main__":
    raise SystemExit(main())
