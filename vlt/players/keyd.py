"""**提权取钥子进程**（Windows）：一次性替主程序扫 pagefile 找那把 32 字节 key，然后退出。

## 为什么必须有这个进程

主程序是普通用户，做不了「读物理磁盘」这件事：`\\\\.\\PhysicalDrive0` 的
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
    hello = _recv(conn, timeout=CONNECT_TIMEOUT)
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
        msg = _recv(conn, timeout=max(1.0, deadline - time.monotonic()))
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


def _recv(conn: socket.socket, *, timeout: float = 30.0) -> dict | None:
    """读一行 JSON；对端关闭/超时/超长都返回 None（调用方按「没结果」处理）。"""
    conn.settimeout(timeout)
    data = b""
    while not data.endswith(b"\n"):
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            return None
        except OSError:
            return None
        if not chunk:
            return None
        data += chunk
        if len(data) > MAX_LINE:
            return None
    try:
        parsed = json.loads(data.decode("utf-8", "replace"))
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


def _scan_job(job: dict, *, pid: int, conn: socket.socket,
              deadline: float) -> dict:
    """真正的特权工作：定位 pagefile → 驱逐脏页 → 扫 key → 交回（不含明文日志）。"""
    groups = _decode_samples(job.get("samples"))
    if not groups:
        return {"ok": False, "error": "没有可用的 Photon 加密包样本", "fatal": True}

    last = [0.0]

    def log(message: str) -> None:
        try:
            _send(conn, {"stage": "log", "message": message})
        except OSError:
            pass

    def progress(done: int, elapsed: float) -> None:
        if elapsed - last[0] < 2.0:
            return
        last[0] = elapsed
        log(f"扫 pagefile：{done / 2**30:.1f} GiB / {elapsed:.1f}s")

    base = pagefile.base_offset_for("C")
    if base is None:
        return {"ok": False, "fatal": True,
                "error": "查不到系统盘所在的物理偏移（读分区表失败）"}
    try:
        disk = pagefile.Disk.open(0, base)
    except pagefile.PagefileError as exc:
        return {"ok": False, "fatal": True, "error": f"打不开物理磁盘：{exc}"}

    with disk:
        volume = pagefile.read_boot(disk)
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
        result = key_win.scan_key(pf, groups, base_offset=base, aad_kind="hdr19",
                                 deadline_s=max(5.0, deadline - time.monotonic() - 10.0),
                                 on_progress=progress)
        log(f"scan_key 用时 {time.monotonic() - _t:.1f}s（stopped={result.stopped}）")

    if result.hit is None:
        return {
            "ok": False,
            "error": (f"扫了 {result.scanned_bytes / 2**30:.1f} GiB"
                      f"（{result.chunks} 块）、候选 {len(result.candidates)} 个，"
                      "都不通过 GCM 认证。最可能的原因：内存压缩又开着了"
                      "（Disable-MMAgent -MemoryCompression 后重启）"),
        }
    peer, chal = result.hit.flow
    return {
        "ok": True,
        "key": result.hit.key.hex(),
        "flow_peer": bytes(peer).hex(),
        "flow_chal": bytes(chal).hex(),
        "ok_count": result.hit.ok,
        "total": result.hit.total,
        "offset": result.hit.offset,
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

    if not is_admin():
        print("[keyd] 必须以管理员运行（由主程序提权拉起）", file=sys.stderr, flush=True)
        return EXIT_NOT_ADMIN

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
        job = _recv(conn, timeout=CONNECT_TIMEOUT)
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

        try:
            result = _scan_job(job, pid=args.pid, conn=conn, deadline=deadline)
        except Exception as exc:                      # noqa: BLE001 - 必须回报给主程序
            result = {"ok": False, "fatal": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        try:
            _send(conn, {"stage": "done", **result})
        except OSError:
            return EXIT_BAD_CONNECT

        # 等主程序收完说 bye；它异常退出（EOF）也立刻收摊，不空转到 lifetime。
        while time.monotonic() < deadline:
            msg = _recv(conn, timeout=max(1.0, min(5.0, deadline - time.monotonic())))
            if msg is None or msg.get("cmd") == "bye":
                break
        return 0 if result.get("ok") else EXIT_NO_KEY
    finally:
        try:
            conn.close()
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
