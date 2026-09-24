"""崩溃日志：闪退时也要留下证据。

## 为什么需要这个

界面跑在 `run_gui.bat` 的黑窗口里。三类崩溃目前都会"什么都不剩"：

1. **原生崩溃**（段错误 / 访问违规）—— 进程直接被系统干掉，Python 层没有异常，窗口消失、
   控制台内容一起没了（实测踩过 PortAudio/Tk 线程问题导致的段错误）
2. **未捕获异常**（主线程 / 子线程）—— 只在控制台打一行，闪退后窗口一关就看不到
3. **Tk 回调里的异常** —— Tk 默认**只打印不退出**，静默带病运行（但某些情况下会连带崩）

所以这里装上三道钩子，并把 stdout/stderr 一起 tee 进日志文件。
日志落在 `logs/`（已 gitignore），一次运行一个文件。

## 用法

```python
from . import crashlog
log_path = crashlog.install(ROOT / "logs", "gui")     # 启动时调用一次
...
crashlog.install_tk(root)                             # Tk 根窗口建好后
```
"""
from __future__ import annotations

import datetime as _dt
import faulthandler
import sys
import threading
import traceback
from pathlib import Path

_LOG_PATH: Path | None = None
_LOG_FILE = None            # 给 faulthandler 用的真实文件对象（它要求有 fileno()）
_ORIG_STDOUT = None
_ORIG_STDERR = None


class _Tee:
    """把写入同时送到原流和日志文件；日志文件出问题也绝不影响原流程。"""

    def __init__(self, *streams) -> None:
        self._streams = [s for s in streams if s is not None]

    def write(self, data):  # noqa: ANN001
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data) if isinstance(data, str) else len(data)

    def flush(self) -> None:
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self):                       # 有些库会问（比如 faulthandler 的替代路径）
        for s in self._streams:
            try:
                return s.fileno()
            except Exception:
                continue
        raise OSError("no fileno")


def install(log_dir: Path, tag: str = "gui") -> Path:
    """装上崩溃钩子，返回日志文件路径。可重复调用（幂等）。"""
    global _LOG_PATH, _LOG_FILE, _ORIG_STDOUT, _ORIG_STDERR
    if _LOG_PATH is not None:
        return _LOG_PATH

    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    _LOG_PATH = log_dir / f"{tag}_{stamp}.log"

    try:
        # line buffering：崩溃时最后几行必须已经在磁盘上（不能卡在缓冲区里）
        _LOG_FILE = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)
    except Exception:
        _LOG_FILE = None
        return _LOG_PATH

    _ORIG_STDOUT, _ORIG_STDERR = sys.stdout, sys.stderr
    sys.stdout = _Tee(_ORIG_STDOUT, _LOG_FILE)
    sys.stderr = _Tee(_ORIG_STDERR, _LOG_FILE)

    # 1) 原生崩溃：段错误 / 访问违规时 dump 所有线程的 Python 栈
    try:
        faulthandler.enable(file=_LOG_FILE, all_threads=True)
    except Exception:
        pass

    # 2) 未捕获异常（主线程 + 子线程）
    def _excepthook(exc_type, exc, tb):  # noqa: ANN001
        try:
            _write_header("未捕获异常（主线程）")
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        except Exception:
            pass

    def _thread_excepthook(args):  # noqa: ANN001
        try:
            _write_header(f"未捕获异常（线程 {getattr(args.thread, 'name', '?')}）")
            traceback.print_exception(args.exc_type, args.exc_value,
                                      args.exc_traceback, file=sys.stderr)
        except Exception:
            pass

    sys.excepthook = _excepthook
    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass

    print(f"[crashlog] 日志：{_LOG_PATH}")
    return _LOG_PATH


def install_tk(root) -> None:  # noqa: ANN001
    """接管 Tk 回调异常（默认只打印、不退出，容易静默带病运行）。"""
    def _tk_handler(exc_type, exc, tb):  # noqa: ANN001
        _write_header("Tk 回调异常")
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

    try:
        root.report_callback_exception = _tk_handler
    except Exception:
        pass


def _write_header(title: str) -> None:
    print("\n" + "=" * 64, file=sys.stderr)
    print(f"!!! {title}  {_dt.datetime.now().isoformat(timespec='seconds')}", file=sys.stderr)
    print("=" * 64, file=sys.stderr)


def log_startup_info(tag: str = "") -> None:
    """把版本/环境写进日志开头——没有这个，拿到日志也不知道是哪个版本崩的。"""
    import platform
    import subprocess

    print("=" * 64)
    print(f"[startup] {tag}  {_dt.datetime.now().isoformat(timespec='seconds')}")
    print(f"[startup] python {sys.version.split()[0]} | {platform.platform()}")
    print(f"[startup] cwd {Path.cwd()}")
    try:
        root = Path(__file__).resolve().parent.parent
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        print(f"[startup] git HEAD {head or '(不是 git 仓库)'}"
              f"{' + 有未提交改动' if dirty else ''}")
    except Exception as exc:
        print(f"[startup] git 信息读取失败：{exc}")
    try:
        from .config import load_api_key
        k = load_api_key()
        print(f"[startup] API 密钥：{k[:6]}****{k[-4:]}（{len(k)} 字符）")
    except SystemExit as exc:
        print(f"[startup] API 密钥：未配置（{exc}）")
    except Exception as exc:
        print(f"[startup] API 密钥：读取失败 {exc}")
    print("=" * 64)


def log_path() -> Path | None:
    return _LOG_PATH


def close() -> None:
    """正常退出时收尾（幂等）。"""
    global _LOG_FILE
    try:
        if _ORIG_STDOUT is not None:
            sys.stdout = _ORIG_STDOUT
        if _ORIG_STDERR is not None:
            sys.stderr = _ORIG_STDERR
    except Exception:
        pass
    try:
        if _LOG_FILE is not None:
            faulthandler.disable()
            _LOG_FILE.flush()
            _LOG_FILE.close()
    except Exception:
        pass
    _LOG_FILE = None
