"""运行期目录解析：源码运行 vs PyInstaller 单文件 exe。

单文件 exe 里 `Path(__file__).parent` 指向**解包用的临时目录**（`sys._MEIPASS`），
进程一退就被删掉 —— 往那里写日志/配置，用户会发现"什么都没留下"，
排查时又找不到任何文件。所以要分清三类目录：

- `APP_DIR`（可写、要留存）：`config.yaml`、`logs/`、`out/`
  - 源码运行：仓库根（开发时配置就在眼前，测试也依赖这一点）
  - 打包后：`%APPDATA%\\vrchat-livetranslate`（Windows 惯例；这样 exe 即使放在
    `Program Files` 这类**只读目录**也能正常跑）
  - 例外：exe 旁边放一个 `portable.txt` → 用 exe 所在目录（绿色版，
    整个文件夹拷走配置跟着走）
- `BUNDLE_DIR`（只读、随程序分发）：`config.example.yaml`、`testdata/`
  - 打包后 = `_MEIPASS`；源码 = 仓库根

新增代码请不要再用 `Path(__file__).resolve().parent.parent` 直接算路径。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "vrchat-livetranslate"
PORTABLE_MARKER = "portable.txt"


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def is_portable() -> bool:
    """绿色版模式：exe 旁边有 portable.txt。"""
    return is_frozen() and (Path(sys.executable).resolve().parent / PORTABLE_MARKER).exists()


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _roaming_dir() -> Path:
    """`%APPDATA%`；读不到就退回家目录下的约定位置。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / "AppData" / "Roaming" / APP_NAME


def app_dir() -> Path:
    """可写目录（详见模块说明）。"""
    if not is_frozen():
        return _repo_root()
    exe_dir = Path(sys.executable).resolve().parent
    if (exe_dir / PORTABLE_MARKER).exists():
        return exe_dir
    return _roaming_dir()


def bundle_dir() -> Path:
    """只读资源目录：`_MEIPASS`（打包后）/ 仓库根（源码运行）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return _repo_root()


def ensure_app_dir() -> Path:
    """确保可写目录存在；建不出来（权限/盘符问题）就退回 exe 所在目录并说明。"""
    d = app_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError as exc:
        if not is_frozen():
            raise
        fallback = Path(sys.executable).resolve().parent
        print(f"[paths] ⚠️ 创建 {d} 失败（{exc}）→ 改用 {fallback}（配置/日志将与 exe 同目录）",
              file=sys.stderr, flush=True)
        return fallback


def migrate_legacy_files() -> list[str]:
    """把"老版本放在 exe 旁边"的 config.yaml / logs 一次性搬到新位置。

    上一版把可写文件放在 exe 旁边；如果升级后不做搬迁，用户会看到
    "配置突然变回默认、日志也不见了"，很难自行判断。
    规则：**目标位置已存在则不覆盖**（新位置为准），并且只搬一次（搬完源文件留着，
    万一用户想回退也还在）。
    """
    if not is_frozen():
        return []
    new = app_dir()
    old = Path(sys.executable).resolve().parent
    if new == old:
        return []                       # 绿色版模式，本来就在一起
    moved: list[str] = []
    try:
        new.mkdir(parents=True, exist_ok=True)
        for name in ("config.yaml", "config.yaml.broken"):
            src, dst = old / name, new / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                moved.append(f"{name} → {dst}")
        old_logs, new_logs = old / "logs", new / "logs"
        if old_logs.is_dir() and not new_logs.exists():
            shutil.copytree(old_logs, new_logs)
            moved.append(f"logs/ → {new_logs}")
    except OSError as exc:
        print(f"[paths] ⚠️ 迁移旧配置失败（不影响启动）：{exc}", file=sys.stderr, flush=True)
    if moved:
        print("[paths] 已把旧版本放在 exe 旁边的文件搬到新位置（%APPDATA%）：", flush=True)
        for m in moved:
            print(f"[paths]   {m}", flush=True)
    return moved


APP_DIR = app_dir()
BUNDLE_DIR = bundle_dir()
