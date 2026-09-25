"""把界面打包成**单文件、无控制台**的 Windows exe。

用法：
    .venv/Scripts/python.exe scripts/build_exe.py            # 打包 + 自检
    .venv/Scripts/python.exe scripts/build_exe.py --no-verify  # 只打包不跑自检

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
PY = REPO / ".venv" / "Scripts" / "python.exe"
ENTRY = REPO / "run_gui.py"
APP_NAME = "VRChatLiveTranslate"          # 文件名用 ASCII：跨工具链更省事
DIST = REPO / "dist"
BUILD = REPO / "build"
ICON = REPO / "assets" / "app.ico"        # exe 图标（16/24/32/48/64/128/256 多尺寸）

# 按需导入的库（静态分析看不到）→ 显式声明
HIDDEN = [
    "pyaudiowpatch", "pycaw", "comtypes", "openvr", "sounddevice", "miniaudio",
    "pythonosc", "websockets", "yaml", "PIL", "numpy",
    "vlt", "vlt.paths", "vlt.config", "vlt.credentials", "vlt.crashlog",
    "vlt.devices", "vlt.engine", "vlt.gui", "vlt.app",
    "vlt.session", "vlt.session.base", "vlt.session.qwen38", "vlt.session.qwen35",
    "vlt.output", "vlt.output.chatbox", "vlt.output.overlay", "vlt.output.virtualmic",
]
# 带二进制/数据文件的库 → 连数据一起收
COLLECT_ALL = ["pyaudiowpatch", "sounddevice", "comtypes", "openvr", "pythonosc", "pycaw"]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, cwd=str(REPO), **kw)


def ensure_pyinstaller() -> None:
    have = run([str(PY), "-m", "PyInstaller", "--version"], capture_output=True, text=True)
    if have.returncode == 0:
        print(f"PyInstaller 已就绪：{have.stdout.strip()}", flush=True)
        return
    print("未安装 PyInstaller，正在装入 .venv ...", flush=True)
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


def build() -> Path:
    for d in (DIST, BUILD):
        shutil.rmtree(d, ignore_errors=True)
    cmd = [str(PY), "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile",            # 单文件
           "--noconsole",          # 无控制台窗口（GUI 程序）
           "--name", APP_NAME,
           "--paths", str(REPO),
           "--add-data", f"{REPO / 'config.example.yaml'}{';'}.",   # 首次运行要生成 config.yaml
           "--add-data", f"{REPO / 'testdata'}{';'}testdata",        # --self-test 用
           "--add-data", f"{REPO / 'assets'}{';'}assets",            # 图标 + 赞助弹窗的两张收款码
           ] + (["--icon", str(ICON)] if ICON.exists() else [])
    if not ICON.exists():
        print(f"[!] 没找到图标 {ICON}，本次打包不带自定义图标", flush=True)
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for c in COLLECT_ALL:
        cmd += ["--collect-all", c]
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
        raise SystemExit(f"找不到虚拟环境解释器：{PY}\n先按 README 建 .venv 并装依赖")
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
