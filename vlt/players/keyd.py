"""**提权取钥子进程**（Windows）：一次性替主程序扫 pagefile 找那把 32 字节 key，然后退出。

## 为什么必须有这个进程

主程序是普通用户，做不了「读物理磁盘」这件事：`\\\\.\\PhysicalDriveN` 的
`GENERIC_READ` 只给管理员。而 `pagefile.sys` 本身被系统独占持有（文件级
`CreateFile` 恒 `err=32`），所以只能绕过文件系统、直接读卷所在的那几段扇区。

对位 Linux 的 `priv_helper.py`，但 Windows **更简单**：

* 抓包走 Npcap（`dgram.py`），**不需要提权**；
* 只有「扫 pagefile」要提权，而且 key 是**进程级**的（换房间不换，只有重启游戏才换），
  所以一次运行最多弹一次 UAC。

因此这里不存在常驻特权服务、没有计划任务、没有任何持久化：进程扫完就把 key 交回
主程序并立刻退出。特权代码只做「读磁盘 + 算 GCM 认证」，不写文件、不碰游戏进程内存
（EAC 也不让碰 —— 见 `key_win.py` 的 docstring）。

## 通信：为什么是 loopback TCP 而不是文件或命名管道

UAC 提权会换 token，父进程的 stdio 句柄**跨不过去**，所以 `subprocess.PIPE` 拿不到
子进程的输出。三条候选：

* 临时文件 —— key 会真的落一次盘，谁都不想要；
* 命名管道 —— 要处理 DACL（默认只有创建者能连，跨完整性级别还得额外放权）；
* **loopback TCP** —— 不落盘、无 DACL、天然有超时与断开检测，语义最接近 Linux 的
  unix socket。端口由主程序 `bind(("127.0.0.1", 0))` 随机取，外加 128 位随机令牌，
  局域网连不进来，本机别的进程连上也过不了令牌校验。

协议是「换行分隔 JSON」（与 Linux `helper._recv_json` 同形）：

    → {"v":1,"stage":"hello","token":…,"pid":…,"created":…}   子进程连上先自报
    ← {"cmd":"job","token":…,"pid":…,"hint_mft":…,"samples":[…]}  主程序下发任务
    → {"stage":"log","message":…}                              进度（可多条）
    → {"stage":"done","ok":true,"key":"<hex>",…}               或 {"stage":"error",…}
    ← {"cmd":"bye"}                                            主程序收完主动收摊

`samples` 走 socket 而不是命令行：40 个包 base64 后约 20 KB，远超命令行舒适区，
也避免密文出现在进程列表里（Windows 的命令行对同用户进程可见）。

## 硬性边界

* 只接受**管理员**身份运行（否则直接退，`EXIT_NOT_ADMIN`）；
* `--pid` 与任务里下发的 pid、`--created` 与下发的时间戳都必须一致 —— 防 PID 复用
  指向一个不相干的新进程（Linux 用 `starttime`，这里用 `GetProcessTimes` 的创建时间）；
* 只读磁盘：不存在写盘/写内存的代码路径；
* 不把 key 写日志、不打印 key（只回指纹与 offset）；日志走 socket 回主程序；
* 存活上限 `--lifetime`（默认 240 s），到点自退；主程序异常退出（EOF）也会立刻退。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import secrets
import socket
import sys
import tempfile
import time

from pathlib import Path

from . import key_win, pagefile

# 退出码：与 Linux `priv_helper.py` 的 EXIT_NOT_ROOT/EXIT_BAD_TARGET/EXIT_BAD_SOCKET 对齐。
EXIT_NOT_ADMIN = 3
EXIT_BAD_TARGET = 4
EXIT_BAD_CONNECT = 5
EXIT_NO_KEY = 6

PROTO = 1
MAX_LINE = 8 << 20
# 冻结产物用这个开关把自己重新拉起来当助手（`run_gui.py` 在导入 GUI 之前拦）。
KEYD_SWITCH = "--vlt-keyd"
# 扫一遍 31 GiB 实测 21 s，加 trim + 定位，240 s 是「绝不会误杀」的量级。
DEFAULT_LIFETIME = 240.0
# 主程序等 UAC + 子进程启动的时间窗。用户可能慢慢点确认。
ACCEPT_TIMEOUT = 90.0
# 子进程连回主程序的超时：提权进程从 ShellExecute 到解释器就绪一般 < 3 s。
CONNECT_TIMEOUT = 15.0
#: 无人值守开关：置 1 时绝不拉起提权子进程，直接报「需要管理员」。
#: 自动化测试 / CI / 人不在电脑前时用它 —— 否则 `ShellExecuteW` 会弹 UAC，
#: 没人点就把整条流水线吊到 `ACCEPT_TIMEOUT` 才失败。产品正常运行不设它。
NO_ELEVATE_ENV = "VLT_NO_ELEVATE"
_YES = ("1", "true", "yes", "on")

FROZEN = bool(getattr(sys, "frozen", False))


class KeyDenied(RuntimeError):
    """提权这条路走不通（用户点了取消、或 exe 起不来）。再试一次可能有用。"""


# ---------------------------------------------------------------- 管理员判定

def is_admin() -> bool:
    """当前进程是否已在管理员组（提权后）。"""
    try:
        shl = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:
        return False
    shl.IsUserAnAdmin.restype = ctypes.c_int
    shl.IsUserAnAdmin.argtypes = []
    try:
        return bool(shl.IsUserAnAdmin())
    except OSError:
        return False


def elevation_blocked() -> bool:
    """环境是否要求「别提权」（`VLT_NO_ELEVATE=1`）。

    只读环境变量，不碰系统状态：自动化路径靠它避开 UAC 弹窗，而正常使用不受影响。
    """
    raw = os.environ.get(NO_ELEVATE_ENV, "")
    return raw.strip().lower() in _YES


def _shell_execute(operation: str, file: str, params: str, cwd: str,
                   show: int = 0) -> int:
    """`ShellExecuteW`；返回 ≤32 表示失败（`GetLastError` 给原因）。"""
    try:
        shl = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:
        return 0
    shl.ShellExecuteW.restype = ctypes.c_void_p
    shl.ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    return int(shl.ShellExecuteW(None, operation, file, params, cwd, show) or 0)


def _root() -> Path:
    """仓库根（源码运行时的 cwd 基准；冻结后无意义但无害）。"""
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------- 命令行

def command_args(*, port: int, token: str, pid: int, created: int,
                 lifetime: float) -> list[str]:
    """拉起提权子进程的命令行。单独抽出来是为了**能被测试替换**（不需要真的弹 UAC）。

    两种形态（与 Linux `helper.helper_command` 同构）：
      * 源码：`<python> -m vlt.players.keyd …` —— 要求解释器就是当前这个（venv 里
        已经装齐依赖，系统 python 可能没有）；
      * 冻结：`<主程序> --vlt-keyd …` —— 不要求宿主机装 Python。
    """
    head = ([sys.executable, KEYD_SWITCH] if FROZEN
            else [sys.executable, "-m", "vlt.players.keyd"])
    return [
        *head,
        "--port", str(port),
        "--token", token,
        "--pid", str(pid),
        "--created", str(created),
        "--lifetime", f"{lifetime:g}",
    ]


# ---------------------------------------------------------------- 主程序侧

def _encode_samples(samples) -> list[dict]:
    """`{(peer, chal): [pkt, …]}` → JSON 可传的形状。"""
    out: list[dict] = []
    for tag, pkts in samples.items():
        peer, chal = tag
        out.append({
            "peer": bytes(peer).hex(),
            "chal": bytes(chal).hex(),
            "pkts": [base64.b64encode(bytes(p)).decode("ascii") for p in pkts],
        })
    return out


def _accept(server: socket.socket, *, deadline: float, hint: str) -> socket.socket:
    """等提权子进程连回来。用户点取消时 `ShellExecuteW` 已经报错了，这里是兜底。"""
    while time.monotonic() < deadline:
        server.settimeout(0.5)
        try:
            conn, _addr = server.accept()
            return conn
        except socket.timeout:
            continue
        except OSError as exc:
            raise KeyDenied(f"等提权子进程时连接出错：{exc}") from exc
    raise KeyDenied(
        "提权子进程一直没有连回来（UAC 里可能被拒绝了，或者子进程启动就失败了）。\n"
        f"  可以手动跑一次看报错：{hint}")


def run_scan(*, pid: int, created: int, samples, hint_mft: int = 0,
             hint_serial: int = 0, lifetime_s: float = DEFAULT_LIFETIME,
             on_log=None, timeout_s: float = ACCEPT_TIMEOUT) -> dict:
    """拉起一次性提权子进程扫 pagefile，返回它交回的原始结果字典。

    `on_log(message)` 收子进程的进度文本（定位 pagefile / trim / 扫描进度）。
    返回值形如 `{"ok": True, "key": "<hex>", "flow_peer": …, "flow_chal": …, …}`
    或 `{"ok": False, "error": …, "fatal": True}`。
    """
    # 唯一的提权出口：设了 `VLT_NO_ELEVATE` 就绝不调 `ShellExecuteW`。
    # 抛 `KeyDenied`（而不是别的异常）好让 `win_capture` 走既有的优雅降级分支。
    if not is_admin() and elevation_blocked():
        raise KeyDenied(
            f"已设置 {NO_ELEVATE_ENV}，跳过自动提权（读 pagefile 需要管理员）。"
            " 需要取钥时去掉这个环境变量，或在管理员终端里直接跑。")
    token = secrets.token_hex(16)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = int(server.getsockname()[1])

        argv = command_args(port=port, token=token, pid=pid, created=created,
                            lifetime=lifetime_s)
        hint = " ".join(argv)
        t_pre = time.monotonic()
        ret = _shell_execute("runas", argv[0],
                             _join_argv(argv[1:]), str(_root()), 0)
        if on_log is not None:
            try:
                on_log(f"ShellExecuteW 返回 {ret}（{time.monotonic() - t_pre:.1f}s）")
            except Exception:                     # noqa: BLE001 - 日志不影响取钥
                pass
        if ret <= 32:
            err = ctypes.get_last_error()
            # 1223 = ERROR_CANCELLED：用户在 UAC 弹窗上点了「否」。
            if err == 1223:
                raise KeyDenied("已取消提权（UAC 弹窗点了「否」）")
            raise KeyDenied(
                f"拉起提权子进程失败（ShellExecuteW={ret}, errno={err}）\n"
                f"  可以手动跑一次看报错：{hint}")

        t_fire = time.monotonic()
        conn = _accept(server, deadline=t_fire + timeout_s, hint=hint)
        if on_log is not None:
            try:
                on_log(f"已连接（UAC 等待 {time.monotonic() - t_fire:.1f}s，之后是读盘扫描）")
            except Exception:                     # noqa: BLE001 - 日志不影响取钥
                pass
        try:
            return _drive(conn, token=token, pid=pid, created=created,
                          hint_serial=hint_serial,
                          hint_mft=hint_mft, samples=samples, on_log=on_log,
                          deadline=time.monotonic() + lifetime_s)
        finally:
            try:
                conn.close()
            except OSError:
                pass
    finally:
        server.close()


def _join_argv(argv: list[str]) -> str:
    """把参数拼成 `ShellExecuteW` 要的单个字符串（自己加引号，别用 list2cmdline
    的 `\\` 转义规则 —— 这里的参数全是 hex/数字，只有路径可能含空格）。"""
    out = []
    for a in argv:
        out.append(f'"{a}"' if (" " in a or "\t" in a) else a)
    return " ".join(out)


def _drive(conn: socket.socket, *, token: str, pid: int, created: int,
           hint_mft: int, hint_serial: int, samples, on_log,
           deadline: float) -> dict:
    """令牌握手 → 下发任务 → 收日志直到 done/error → bye。"""
    buf = bytearray()          # 粘包时多读的尾巴（见 `_recv`）
    hello = _recv(conn, timeout=CONNECT_TIMEOUT, buf=buf)
    if not hello:
        raise KeyDenied("提权子进程连上了但没自报身份就退出了")
    if hello.get("token") != token:
        raise KeyDenied("提权子进程令牌不符（可能有别的进程在抢这个端口）")

    _send(conn, {
        "cmd": "job",
        "token": token,
        "pid": int(pid),
        "created": int(created),
        "hint_mft": int(hint_mft),
        "hint_serial": int(hint_serial),
        "samples": _encode_samples(samples),
    })

    result: dict | None = None
    while time.monotonic() < deadline:
        msg = _recv(conn, timeout=max(1.0, deadline - time.monotonic()), buf=buf)
        if msg is None:
            break
        stage = msg.get("stage")
        if stage == "log":
            if on_log is not None:
                try:
                    on_log(str(msg.get("message") or ""))
                except Exception:                    # noqa: BLE001 - 日志不影响取钥
                    pass
        elif stage == "done":
            result = {k: v for k, v in msg.items() if k != "stage"}
            break
        elif stage == "error":
            result = {"ok": False,
                      "error": str(msg.get("error") or "未知错误"),
                      "fatal": bool(msg.get("fatal", True))}
            break
    try:
        _send(conn, {"cmd": "bye"})
    except OSError:
        pass
    if result is None:
        raise KeyDenied("提权子进程没有交出结果就断开了（可能超时或被系统结束）")
    return result


# ---------------------------------------------------------------- 收发

def _send(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def _recv(conn: socket.socket, *, timeout: float = 30.0,
          buf: bytearray | None = None) -> dict | None:
    """读一行 JSON。对端关闭/超时/超长都返回 None（调用方按「没结果」处理）。

    ⚠️ **必须按行切，不能拿「整段以换行结尾」当读完了**：TCP 是流，子进程定位完
    pagefile 会连发三条 log（内存压缩状态 / MFT / pagefile 摘要），实测必然被合并
    进同一次 recv。老实现拿到 `"...a\\n...b\\n"` 就 `json.loads` 整段 → `Extra data:
    line 2 column 1` → 返回 None → 父进程报「提权子进程没有交出结果就断开了」，
    而子进程其实还在扫（本机实测又活了 120 秒，**并且真的找到了密钥**，结果没人收）。

    多读进来的半截留在 `buf` 里，下一次调用接着用（同一条连接必须传同一个 buf）。
    """
    conn.settimeout(timeout)
    data = buf if buf is not None else bytearray()
    while True:
        nl = data.find(b"\n")
        if nl >= 0:
            line = bytes(data[:nl])
            del data[:nl + 1]
            break
        if len(data) > MAX_LINE:              # 单行超过上限：协议已乱，放弃
            return None
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            return None
        except OSError:
            return None
        if not chunk:
            return None
        data.extend(chunk)
    try:
        parsed = json.loads(line.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------- 子进程侧

def _decode_samples(raw) -> dict:
    groups: dict[tuple[bytes, bytes], list[bytes]] = {}
    for item in raw or []:
        try:
            tag = (bytes.fromhex(str(item["peer"])), bytes.fromhex(str(item["chal"])))
            pkts = [base64.b64decode(str(p)) for p in item.get("pkts") or []]
        except (KeyError, ValueError, TypeError):
            continue
        if len(pkts) >= 2:
            groups[tag] = pkts
    return groups


#: 全机只允许一个提权扫描（命名互斥体）。本机实测：GUI 里多个采集实例/多次重试
#: 会并行拉起 6 个以上的子进程同时扫 34 GiB —— 日志里六条进度交错、IO 打满，
#: 而且谁都扫不完（互相拖慢到超时）。这里在**操作系统层面**保证串行。
MUTEX_NAME = "vrchat_livetranslate_keyd_scan"        # 会话内命名互斥体（无需 Global 前缀）


def _acquire_scan_mutex():
    """拿到 → 返回句柄；拿不到（已有扫描在跑）→ 返回 None。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not h:
            return None
        ERROR_ALREADY_EXISTS = 183
        if k32.GetLastError() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(h)
            return None
        return h
    except (AttributeError, OSError):
        return None   # 非 Windows / 拿不到互斥体：不拦，照旧扫


#: `OpenMutexW` 只要 SYNCHRONIZE 就够了（我们只问「存不存在」，不要改它）。
_SYNCHRONIZE = 0x00100000


def scan_busy() -> bool:
    """现在是否已有另一个提权扫描在跑（**父进程侧**探测，不弹 UAC）。

    为什么要在父进程探：子进程撞锁时已经「连上 → 收到 job → 才回 busy」，
    那时 UAC 窗口早弹过了。实测一轮重试里连弹 5 次 UAC，用户看到的就是
    「一直弹窗但一直拿不到钥匙」。提前探到就能直接等那一路的结果，
    一个窗口都不用弹。

    非 Windows / 探测失败一律返回 False（宁可多扫一次，也别卡死在「等」上）。
    """
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenMutexW.restype = ctypes.c_void_p      # 句柄是 64 位，别被截成 int32
        h = k32.OpenMutexW(ctypes.c_uint32(_SYNCHRONIZE), False, MUTEX_NAME)
        if not h:
            return False
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    except (AttributeError, OSError):
        return False


def _release_scan_mutex(h) -> None:
    if not h:
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.ReleaseMutex(h)
        k32.CloseHandle(h)
    except (AttributeError, OSError):
        pass

def _child_log_path() -> "Path":
    """子进程自己的日志（在 %TEMP%，和排查包放一起）。"""
    return Path(tempfile.gettempdir()) / "vlt_keyd.log"


def _dump_failure(pf, result, groups, *, log=None) -> str:
    """失败时把「扫到了什么」落一份 JSON：样本报文 + 候选 key + 统计。

    没有这份东西，「候选 5 万个全不通过」只能靠猜（内存压缩？偏移不对？样本不对？）。
    有了它可以在**不重新提权**的情况下离线复验每一种 AAD 形态/候选偏移。
    只写 `%TEMP%`，只含密文与候选（没有明文、没有用户名）。
    """
    try:
        import tempfile
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = Path(tempfile.gettempdir()) / f"vlt_keyd_dump_{ts}.json"
        items = groups.items() if isinstance(groups, dict) else groups
        samples = {}
        for tag, pkts in items:
            try:
                peer, chal = tag
            except (TypeError, ValueError):
                peer, chal = b"", b""
            samples[f"{bytes(peer).hex()}|{bytes(chal).hex()}"] = [
                bytes(x).hex() for x in pkts[:8]]
        payload = {
            "time": ts,
            "pagefile": getattr(pf, "summary", lambda: "")(),
            "scanned_bytes": result.scanned_bytes,
            "chunks": result.chunks,
            "stopped": result.stopped,
            "needle_hits": result.needle_hits,
            "aad_kind_default": result.aad_kind,
            "threads": result.threads,
            "regions": result.regions,
            "markers": getattr(result, "markers", {}),
            # 全量落盘：上一轮只留 10 万个，真钥可能正好在被截掉的那一半里
            "candidates": [c.hex() for c in result.candidates],
            "samples": samples,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if log is not None:
            log(f"排查包已保存：{path}（{len(payload['candidates'])} 个候选）")
        return str(path)
    except Exception as exc:                                   # noqa: BLE001 - 只影响排查
        if log is not None:
            log(f"排查包写不出来：{type(exc).__name__}: {exc}")
        return ""


def _memory_compression_state() -> bool | None:
    """Windows 内存压缩开关：True=开 / False=关 / None=问不出来。

    只有管理员能查（`Get-MMAgent` 走 CIM）。子进程本来就是提权起来的，所以这里能问到；
    查失败**不影响**扫描（None 当「不知道」处理，不拦）。
    """
    try:
        import subprocess
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-MMAgent).MemoryCompression"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "").strip().lower()
        if out in ("true", "false"):
            return out == "true"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _persist_key(pid: int, created: int, key: bytes, *, log=None) -> None:
    """子进程扫到密钥后自己落一份盘（与主程序 `win_capture.KEY_CACHE_FILE` 同文件）。

    为什么要子进程写：扫一次 2~4 分钟，结果**只能靠那条 socket 交回**，而连接
    可能先断（本机实测：粘包让主程序把「定位完 pagefile」误判成断开，子进程又
    老老实实扫了 120 秒、真找到了钥匙，却没人收 —— 整轮白扫）。
    落到同一个缓存文件后，主程序下一次 `acquire_key()` 直接命中，不用重扫。

    写失败不影响返回：缓存是加速，不是正确性依赖。
    """
    if not pid or len(key) != 32:
        return
    try:
        from ..paths import _user_data_dir
        path = _user_data_dir() / "key_cache.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": int(pid), "created": int(created),
            "key": key.hex(), "ts": int(time.time()),
            "note": "vrchat livetranslate session key cache; delete this file to force a rescan",
        }, ensure_ascii=False), encoding="utf-8")
        if log is not None:
            log(f"密钥已落盘缓存：{path}")
    except (OSError, ImportError, ValueError):
        pass


def _scan_job_inner(job: dict, *, pid: int, conn: socket.socket,
                    deadline: float, info: dict) -> dict:
    """真正的特权工作：定位 pagefile → 驱逐脏页 → 扫 key → 交回（不含明文日志）。

    ``info`` 是**出参**：把定位到的 pagefile 记录号/卷序列号塞进去，好让
    ``_scan_job`` 在失败时也能带回去（下次就不用再扫一遍 MFT 了）。
    """
    groups = _decode_samples(job.get("samples"))
    if not groups:
        return {"ok": False, "error": "没有可用的 Photon 加密包样本", "fatal": True}

    last = [0.0]

    def log(message: str) -> None:
        # 先落盘再发：进程被硬杀时，磁盘上至少还有最后一步是什么
        try:
            _fh = globals().get("_CHILD_FH")
            if _fh is not None:
                _fh.write(f"{time.strftime('%H:%M:%S')} {message}" + chr(10))
                _fh.flush()
        except (OSError, ValueError):
            pass
        try:
            _send(conn, {"stage": "log", "message": message})
        except OSError:
            pass

    def progress(done: int, elapsed: float) -> None:
        if elapsed - last[0] < 2.0:
            return
        last[0] = elapsed
        log(f"扫 pagefile：{done / 2**30:.1f} GiB / {elapsed:.1f}s")

    try:
        disk, layout = pagefile.open_volume("C")
    except pagefile.PagefileError as exc:
        return {"ok": False, "fatal": True,
                "error": f"打不开系统盘所在的物理盘：{exc}"}
    segs = f"，{len(layout.extents)} 段" if len(layout.extents) > 1 else ""
    log(f"系统盘在 PhysicalDrive{layout.disk_no}"
        f"（盘上起点 {layout.base_offset / 2**30:.1f} GiB{segs}）")
    compression = _memory_compression_state()
    if compression:
        # 压缩开着时 pagefile 里存的是 XPRESS 压缩块，不是原始堆页 —— 扫描注定失败
        # （上游实测 9,109 个候选全部认证失败）。这里必须在**扫之前**就说清楚，
        # 否则用户只会看到「没找到能通过 GCM 认证的密钥」，完全没法归因。
        msg = ("内存压缩开着（MemoryCompression=On）→ pagefile 里不是原始堆页，"
               "扫描注定失败。修法（管理员，需重启）：Disable-MMAgent -MemoryCompression")
        log("⚠️ " + msg)
        return {"ok": False, "fatal": True, "error": msg,
                "memory_compression": True}
    if compression is False:
        log("内存压缩：关（扫描前提满足）")

    with disk:
        try:
            volume = pagefile.read_boot(disk)
        except pagefile.PagefileError as exc:
            return {"ok": False, "fatal": True,
                    "error": (f"PhysicalDrive{layout.disk_no} 的 "
                              f"{layout.base_offset} 处读不到 NTFS 引导扇区：{exc}")}
        # 记录号缓存按卷序列号校验：换了系统盘/重装了系统时，同一个记录号会指向
        # 另一个完全不相干的文件，直取会拿到错的 extent 区间。序列号不符就丢掉 hint
        # 走一次全表扫描，并由主程序把新记录号写回缓存。
        hint = int(job.get("hint_mft") or 0)
        want = int(job.get("hint_serial") or 0)
        if hint and want and int(getattr(volume, "serial", 0)) != want:
            log(f"缓存的 pagefile 记录号属于另一块盘（serial {want} != "
                f"{getattr(volume, 'serial', 0)}），忽略，改走全表扫描")
            hint = 0
        pf = pagefile.find_pagefile(disk, volume, hint_mft=hint, on_log=log)
        if pf is None:
            return {"ok": False, "fatal": True, "error": "没在 MFT 里定位到 pagefile.sys"}
        log(pf.summary())
        # 无论后面成不成，这个记录号都值得带回去缓存：hint 过期时正是靠这一次
        # 全表扫描把它纠正过来的，失败也照样写回。
        info["mft"] = pf.mft_index
        info["serial"] = int(getattr(volume, "serial", 0) or 0)
        if not pf.real_bytes:
            return {"ok": False, "fatal": True,
                    "error": "pagefile 是空的：取钥前要先进一次世界"}

        _t = time.monotonic()
        stats = key_win.prepare(pid, on_log=log)
        log(f"prepare 用时 {time.monotonic() - _t:.1f}s：{stats}")
        if stats.get("trim_err"):
            return {"ok": False, "fatal": True,
                    "error": f"驱逐脏页失败（errno={stats['trim_err']}）"}

        _t = time.monotonic()
        result = key_win.scan_key(pf, groups, layout=layout, aad_kind="hdr19",
                                 deadline_s=max(5.0, deadline - time.monotonic() - 10.0),
                                 on_progress=progress)
        log(f"scan_key 用时 {time.monotonic() - _t:.1f}s（stopped={result.stopped}）")

    if result.hit is None:
        dump = _dump_failure(pf, result, groups, log=log)
        unread = ""
        if result.unread_bytes:
            unread = (f"；另有 {result.unread_bytes / 2**20:.1f} MiB 根本没读到"
                      f"（PhysicalDrive{result.failed_disks} 打不开/读失败），"
                      "先解决读盘再判断密钥")
        return {
            "ok": False,
            "error": (f"扫了 {result.scanned_bytes / 2**30:.1f} GiB"
                      f"（{result.chunks} 块）、候选 {len(result.candidates)} 个，"
                      "都不通过 GCM 认证。最可能的原因：内存压缩又开着了"
                      f"（Disable-MMAgent -MemoryCompression 后重启）{unread}"
                      f"，pagefile 里的游戏堆路标：{getattr(result, 'markers', {})}"
                      + (f"；排查包：{dump}" if dump else "")),
        }
    peer, chal = result.hit.flow
    # 自己先落盘一份：子进程扫一次要 2~4 分钟，交回结果要靠**连接**——而连接
    # 可能因为各种原因（粘包误判、主程序重启、UAC 被撤销）先断掉。断了还白扫
    # 就没法接受；写进同一个缓存文件，主程序下次 `acquire_key()` 直接命中。
    # 写失败不影响返回（缓存是加速，不是正确性依赖）。
    _persist_key(int(job.get("pid") or pid), int(job.get("created") or 0),
                 result.hit.key, log=log)
    return {
        "ok": True,
        "key": result.hit.key.hex(),
        "flow_peer": bytes(peer).hex(),
        "flow_chal": bytes(chal).hex(),
        "ok_count": result.hit.ok,
        "total": result.hit.total,
        "offset": result.hit.offset,
        "aad_kind": result.hit.kind,
        "key_off": result.hit.key_off,
        "fingerprint": key_win.key_fingerprint(result.hit.key),
        "scanned_bytes": result.scanned_bytes,
        "chunks": result.chunks,
        "candidates": len(result.candidates),
        "elapsed_ms": result.elapsed_ms,
        "threads": result.threads,
        # 回传给主程序缓存下来：下次直接按记录号取，省掉 324 万条整表扫描（67 s）。
        "pagefile_mft": pf.mft_index,
        "volume_serial": int(getattr(volume, "serial", 0) or 0),
    }


def _scan_job(job: dict, *, pid: int, conn: socket.socket,
              deadline: float) -> dict:
    """``_scan_job_inner`` 的薄包装：**任何异常都要变成一条回复**。

    子进程直接抛异常退出时，主程序只读到 EOF，界面显示的是「提权子进程没有
    交出结果就断开了」—— 真实原因就此丢掉（实测踩过，只能靠猜）。这里兜住一切
    异常，并保证成功失败都带回 pagefile 记录号。
    """
    info: dict = {}
    try:
        out = _scan_job_inner(job, pid=pid, conn=conn, deadline=deadline, info=info)
    except Exception as exc:                    # noqa: BLE001 - 兜住一切，别静默退出
        out = {"ok": False, "fatal": False,
               "error": f"提权子进程内部出错：{type(exc).__name__}: {exc}"}
    out.setdefault("pagefile_mft", int(info.get("mft") or 0))
    out.setdefault("volume_serial", int(info.get("serial") or 0))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vlt.players.keyd",
        description="一次性提权的取钥子进程（由主程序用 ShellExecuteW runas 拉起）")
    parser.add_argument("--port", type=int, required=True,
                        help="主程序监听的 loopback 端口")
    parser.add_argument("--token", required=True, help="握手令牌")
    parser.add_argument("--pid", type=int, required=True, help="VRChat 进程号")
    parser.add_argument("--created", type=int, default=0,
                        help="VRChat 进程创建时间（防 PID 复用）")
    parser.add_argument("--lifetime", type=float, default=DEFAULT_LIFETIME,
                        help="存活上限秒数（到点自退）")
    args = parser.parse_args(argv)

    # 子进程没有控制台（ShellExecuteW runas 起的），出事先死在没人看得见的地方。
    # 自己留一份日志 + faulthandler：硬崩（访问越界/栈溢出）也能看到当时的栈。
    log_path = _child_log_path()
    try:
        _fh = open(log_path, "a", encoding="utf-8", buffering=1)   # noqa: SIM115 - 进程内不关
        import faulthandler
        faulthandler.enable(file=_fh)
        _fh.write(f"--- keyd {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"pid={os.getpid()} lifetime={args.lifetime} "
                  f"port={args.port} pid_arg={args.pid}" + chr(10))
        _fh.flush()
    except OSError:
        _fh = None
    globals()["_CHILD_FH"] = _fh
    if _fh is not None:
        _fh.write(f"argv={sys.argv[1:]}" + chr(10))
        _fh.flush()

    if not is_admin():
        print("[keyd] 必须以管理员运行（由主程序提权拉起）", file=sys.stderr, flush=True)
        return EXIT_NOT_ADMIN
    # 后台任务：整机不能被扫盘拖垮。降到 BELOW_NORMAL（线程内再降到 LOWEST）。
    try:
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS
    except (AttributeError, OSError):
        pass

    deadline = time.monotonic() + max(30.0, args.lifetime)
    try:
        conn = socket.create_connection(("127.0.0.1", args.port),
                                       timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        print(f"[keyd] 连不上主程序（127.0.0.1:{args.port}）：{exc}",
              file=sys.stderr, flush=True)
        return EXIT_BAD_CONNECT

    try:
        _send(conn, {"v": PROTO, "stage": "hello", "token": args.token,
                     "pid": args.pid, "created": args.created})
        buf = bytearray()      # 粘包时多读的尾巴（见 `_recv`）
        job = _recv(conn, timeout=CONNECT_TIMEOUT, buf=buf)
        if not job or job.get("cmd") != "job":
            _send(conn, {"stage": "error", "fatal": True,
                         "error": "没有收到任务下发"})
            return EXIT_BAD_TARGET
        if job.get("token") != args.token:
            _send(conn, {"stage": "error", "fatal": True, "error": "令牌不符"})
            return EXIT_BAD_TARGET
        # 双保险：任务里的 pid/created 必须与命令行一致（命令行被改写时立刻拒）。
        if int(job.get("pid") or 0) != args.pid:
            _send(conn, {"stage": "error", "fatal": True,
                         "error": "任务里的 pid 与命令行不一致"})
            return EXIT_BAD_TARGET
        if args.created and int(job.get("created") or 0) != args.created:
            _send(conn, {"stage": "error", "fatal": True,
                         "error": "任务里的进程创建时间与命令行不一致（PID 可能被复用）"})
            return EXIT_BAD_TARGET

        mutex = _acquire_scan_mutex()
        if mutex is None and os.name == "nt":
            if _fh is not None:
                _fh.write("skip: 已有另一个提权扫描在跑（互斥体占用）" + chr(10))
                _fh.flush()
            _send(conn, {"stage": "error", "fatal": False,
                         "error": "已有另一个取钥扫描在进行，本次跳过（避免两路同时读盘）"})
            return 0
        try:
            result = _scan_job(job, pid=args.pid, conn=conn, deadline=deadline)
        except Exception as exc:                      # noqa: BLE001 - 必须回报给主程序
            if _fh is not None:
                import traceback
                _fh.write(f"!! _scan_job 抛异常：{type(exc).__name__}: {exc}" + chr(10))
                _fh.write(traceback.format_exc())
                _fh.flush()
            result = {"ok": False, "fatal": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        try:
            _send(conn, {"stage": "done", **result})
        except OSError:
            _release_scan_mutex(mutex)
            return EXIT_BAD_CONNECT
        _release_scan_mutex(mutex)

        # 等主程序收完说 bye；它异常退出（EOF）也立刻收摊，不空转到 lifetime。
        while time.monotonic() < deadline:
            msg = _recv(conn, timeout=max(1.0, min(5.0, deadline - time.monotonic())),
                        buf=buf)
            if msg is None or msg.get("cmd") == "bye":
                break
        return 0 if result.get("ok") else EXIT_NO_KEY
    finally:
        try:
            conn.close()
        except OSError:
            pass
        if _fh is not None:
            try:
                _fh.write(f"--- keyd 退出 pid={os.getpid()}" + chr(10))
                _fh.flush()
            except OSError:
                pass


def maybe_run_from_argv(argv: list[str] | None = None) -> bool:
    """冻结产物在入口最前面调用：看见 `--vlt-keyd` 就**只**跑助手，不导入 GUI。

    源码运行时也有用（`python -m vlt.players.keyd` 走 `main`，这里返回 False）。
    """
    args = list(sys.argv if argv is None else argv)
    if KEYD_SWITCH not in args:
        return False
    idx = args.index(KEYD_SWITCH)
    raise SystemExit(main(args[idx + 1:]))


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(main())
