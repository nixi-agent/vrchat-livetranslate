"""运行期目录解析：源码运行 vs PyInstaller 单文件 exe。

单文件 exe 里 `Path(__file__).parent` 指向**解包用的临时目录**（`sys._MEIPASS`），
进程一退就被删掉 —— 往那里写日志/配置，用户会发现"什么都没留下"，
而排查时又找不到任何文件。所以要分清两类目录：

- `APP_DIR`：**可写、要留存**的东西（config.yaml、logs/、out/）。
  exe 里 = exe 所在目录；源码里 = 仓库根。
- `BUNDLE_DIR`：**只读、随程序分发**的资源（config.example.yaml、testdata/）。
  exe 里 = `_MEIPASS`；源码里 = 仓库根。

新增代码请不要再用 `Path(__file__).resolve().parent.parent` 直接算路径。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """可写目录：exe 所在目录（打包后）/ 仓库根（源码运行）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _repo_root()


def bundle_dir() -> Path:
    """只读资源目录：`_MEIPASS`（打包后）/ 仓库根（源码运行）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return _repo_root()


APP_DIR = app_dir()
BUNDLE_DIR = bundle_dir()
