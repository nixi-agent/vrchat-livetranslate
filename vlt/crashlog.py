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
import re
import sys
import threading
import traceback
from pathlib import Path

_LOG_PATH: Path | None = None
_LOG_FILE = None            # 给 faulthandler 用的真实文件对象（它要求有 fileno()）
_ORIG_STDOUT = None
_ORIG_STDERR = None


# 日志体积上限：单文件超了就切段，总量超了就删最旧的（永远保留当前这段）。
# 实测单次会话 0.6~50KB，5MB ≈ 数百次会话 —— 够用，且绝不会把用户磁盘吃爆。
MAX_LOG_TOTAL_BYTES = 5 * 1024 * 1024
MAX_LOG_FILE_BYTES = 2 * 1024 * 1024


def _prune_logs(log_dir: Path, keep: set[Path] | None = None,
                max_total: int = MAX_LOG_TOTAL_BYTES) -> list[Path]:
    """按总量滚动删除**最旧**的日志（当前正在写的那份永不删）。返回被删列表。"""
    keep = {Path(p) for p in (keep or set())}
    try:
        files = [p for p in Path(log_dir).glob("*.log*") if p.is_file() and p not in keep]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)   # 最旧在前
    total = sum(p.stat().st_size for p in files) + sum(
        p.stat().st_size for p in keep if p.exists())
    removed: list[Path] = []
    while files and total > max_total:
        victim = files.pop(0)
        try:
            size = victim.stat().st_size
            victim.unlink()
        except OSError:
            continue
        total -= size
        removed.append(victim)
    return removed


def export_logs(dest_zip: Path, log_dir: Path | None = None,
                extra_files: list[Path] | None = None) -> tuple[Path, int, int, list[str]]:
    """把日志打包成 zip，方便用户直接发给排查者。

    返回 `(zip 路径, 打包文件数, 原始字节数, 脱敏条目说明)`。

    ⚠️ **打包前逐文件脱敏**：日志里本不该出现完整密钥（写日志的地方都做了打码），
    但这个包是要通过聊天工具发出去的，多一道保险 —— 扫到 `sk-xxxx` 形状就替换掉。
    脱敏过的文件会在返回的说明里列出来，避免"默默改了内容"。
    """
    import zipfile

    from .paths import app_dir

    log_dir = Path(log_dir) if log_dir else (app_dir() / "logs")
    dest_zip = Path(dest_zip)
    if dest_zip.suffix.lower() != ".zip":
        dest_zip = dest_zip.with_suffix(".zip")
    dest_zip.parent.mkdir(parents=True, exist_ok=True)

    redact_re = re.compile(r"sk-[A-Za-z0-9_\-]{10,}")
    redacted: list[str] = []
    added: set[str] = set()
    n_files = 0
    n_bytes = 0

    def _add(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
        nonlocal n_files, n_bytes
        if arcname in added:          # 同名只收一次（用户指定了 config.yaml，又自动带了一份）
            return
        try:
            raw = path.read_bytes()
        except OSError:
            return
        added.add(arcname)
        n_bytes += len(raw)
        text = raw.decode("utf-8", errors="replace")
        if redact_re.search(text):
            text = redact_re.sub("sk-****（已脱敏）", text)
            redacted.append(arcname)
        zf.writestr(arcname, text.encode("utf-8", errors="replace"))
        n_files += 1

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(log_dir.glob("*.log*")):
            if p.is_file():
                _add(zf, p, f"logs/{p.name}")
        # 配置也带上：设备名/语言偏好对排查"哪条腿没起来"经常是关键（不含 key）
        for p in [*(extra_files or []), app_dir() / "config.yaml",
                  app_dir() / "config.yaml.broken"]:
            if p and Path(p).is_file():
                _add(zf, Path(p), Path(p).name)
        zf.writestr("说明.txt",
                    f"VRChat 实时同传 日志包\n"
                    f"导出时间：{_dt.datetime.now().isoformat(timespec='seconds')}（本机本地时间）\n"
                    f"日志目录：{log_dir}\n"
                    f"包含 {n_files} 个文件，原始大小 {n_bytes} 字节\n"
                    f"脱敏文件：{', '.join(redacted) if redacted else '无'}\n")
    return dest_zip, n_files, n_bytes, redacted


class _Tee:
    """把写入同时送到原流和日志文件；日志文件出问题也绝不影响原流程。

    每条日志**行首**自动加 `HH:MM:SS.mmm` 时间戳（本机本地时间 = UTC+8）。
    没有时间戳时，"跑多久断的""两次事件间隔多少"全靠猜 —— 排查真实故障时这是硬伤。
    只在行首加：半行写入（没有换行结尾）不会被打断，下一行才补戳。
    """

    def __init__(self, *streams, log_path: Path | None = None,
                 log_dir: Path | None = None) -> None:
        self._streams = [s for s in streams if s is not None]
        self._at_line_start = True
        self._lock = threading.Lock()
        self._log_path = Path(log_path) if log_path else None
        self._log_dir = Path(log_dir) if log_dir else (self._log_path.parent if self._log_path else None)
        self._bytes = 0
        self._writes = 0
        self._seg = 0

    _stamp_warned = False        # 加戳失败只报一次（避免刷屏）
    _rotate_warned = False

    def _stamp(self, data: str) -> str:
        out: list[str] = []
        for i, part in enumerate(data.split("\n")):
            if i:                     # split 出来的后续片段 = 新的一行
                out.append("\n")
                self._at_line_start = True
            if part and self._at_line_start:
                out.append(_dt.datetime.now().strftime("%H:%M:%S.%f")[:-3] + " ")
                self._at_line_start = False
            out.append(part)
        return "".join(out)

    def write(self, data):  # noqa: ANN001
        try:
            if isinstance(data, str) and data:
                with self._lock:
                    data = self._stamp(data)
        except Exception as exc:  # noqa: BLE001
            # 加戳失败不能影响主流程，但**必须留痕**（直接写原 stderr，避免递归）
            if not _Tee._stamp_warned:
                _Tee._stamp_warned = True
                try:
                    sys.__stderr__.write(f"[crashlog] ⚠️ 日志时间戳失败（后续不加戳）："
                                         f"{type(exc).__name__}: {exc}\n")
                except Exception:
                    pass
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        # 日志体积控制（每写一次记一次，超过阈值才真正动作）
        if self._log_path is not None:
            try:
                self._bytes += len(data) if isinstance(data, str) else len(data or b"")
                self._writes += 1
                if self._bytes >= MAX_LOG_FILE_BYTES:
                    self._rotate()
                elif self._writes % 500 == 0:
                    _prune_logs(self._log_dir, keep={self._log_path})
            except Exception as exc:  # noqa: BLE001
                if not _Tee._rotate_warned:
                    _Tee._rotate_warned = True
                    try:
                        sys.__stderr__.write(f"[crashlog] ⚠️ 日志滚动失败（继续写当前文件）："
                                             f"{type(exc).__name__}: {exc}\n")
                    except Exception:
                        pass
        return len(data) if isinstance(data, str) else len(data)

    def _rotate(self) -> None:
        """当前日志写满一段（MAX_LOG_FILE_BYTES）→ 关掉、开新段、顺手清最旧的。

        为什么要切段而不是停止写入：日志**末尾**才是出问题的地方，
        直接不写等于把最关键的证据丢掉。切段后老段仍在，只是不再追加。
        """
        self._seg += 1
        old = self._streams[-1] if self._streams else None
        try:
            if old is not None:
                old.flush()
                old.close()
        except Exception:
            pass
        new_path = self._log_path.with_name(f"{self._log_path.stem}.{self._seg}.log")
        try:
            new_file = open(new_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            return
        if self._streams:
            self._streams[-1] = new_file
        self._bytes = 0
        try:
            faulthandler.enable(file=new_file, all_threads=True)   # 段错误仍要落到当前段
        except Exception:
            pass
        _prune_logs(self._log_dir, keep={self._log_path, new_path})
        try:
            new_file.write(f"[crashlog] 日志已切段（上一段 {self._log_path.name}，"
                           f"单文件上限 {MAX_LOG_FILE_BYTES // 1024 // 1024}MB）\n")
        except Exception:
            pass

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

    # 启动时先按总量滚动清理（新文件还没建，不会被误删）
    removed = _prune_logs(log_dir)
    if removed:
        print(f"[crashlog] 日志总量超过 {MAX_LOG_TOTAL_BYTES // 1024 // 1024}MB，"
              f"已删除 {len(removed)} 个最旧的日志文件", flush=True)

    _ORIG_STDOUT, _ORIG_STDERR = sys.stdout, sys.stderr
    # 只有 stdout 那个 Tee 负责"体积控制"：两个 Tee 共享同一个日志文件，
    # 若都去切段/关文件，会互相把对方手里的句柄关掉。stderr 量极小，不单独计。
    sys.stdout = _Tee(_ORIG_STDOUT, _LOG_FILE, log_path=_LOG_PATH, log_dir=log_dir)
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
    try:
        from . import __version__ as _ver

        _how = "打包 exe" if getattr(sys, "frozen", False) else "源码运行"
        print(f"[startup] 版本 v{_ver}（{_how}）")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 版本读取失败：{exc}")
    print(f"[startup] cwd {Path.cwd()}")
    print("[startup] 日志时间戳为行首 HH:MM:SS.mmm（本机本地时间 = UTC+8）")
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
