#!/usr/bin/env python
"""名字表与翻译腿解耦：预热器常驻、翻译腿只租借。

## 这里钉住的事

1. `players.prewarm` 这个键已经废弃（分玩家必须预热），写它也只会被忽略并告警 ——
   不能悄悄让「配了 prewarm=off」的旧配置把名字表缓存关掉；
2. `leg._WarmupCapture.parse` 在 Windows 上**恒等**（lease 底下是 `_WinCapture`，
   交出来的已经是 `Datagram`），POSIX 仍然解析原始帧；
3. `_WarmupCapture.set_local_ports` 把端口转给 lease（Windows 判方向要靠它）；
4. `PlayerWarmup.set_local_ports` 只在底层抓包对象有这个口子时转发，且不许抛；
5. `PlayerLeg._run_once` 在**任何平台**上都先试租借，拿得到就不开一次性抓包 ——
   这正是「停止翻译之后名字表还在」的实现基础；租不到才退回本平台的一次性路径
   （Windows 回落 `_WinCapture`，POSIX 直接报"未就绪"让外层退避）。

跑法：E:/Temp/pyc/python.exe tests/test_players_prewarm.py
（`.venv/Scripts/python.exe` 在本机枚举进程会被过滤，跑 GUI/测试一律用外部解释器）
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "playersprewarm0123456789")

import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

from _cfgbox import sandbox_config  # noqa: E402

CONFIG = sandbox_config(reset=True)

from vlt.players import leg, warmup  # noqa: E402
from vlt.players_settings import MODE_DECRYPT, parse_players  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


# ---------------------------------------------------------------- 1) 废弃键


def test_prewarm_key_is_gone() -> None:
    print("[players-prewarm] players.prewarm 已废弃")
    on = parse_players({"mode": "decrypt"})
    check("模式本身仍然正确", on.mode == MODE_DECRYPT, on.mode)
    check("不再有 prewarm 字段", not hasattr(on, "prewarm"), str(getattr(on, "prewarm", None)))

    warned: list[str] = []
    kept = parse_players({"mode": "decrypt", "prewarm": "off"}, log=warned.append)
    check("旧配置里的 prewarm=off 不会把模式关掉",
          kept.mode == MODE_DECRYPT, kept.mode)
    check("旧键至少告警一次", any("prewarm" in w for w in warned), str(warned))


# ---------------------------------------------------------------- 2) 抓包壳


class _Lease:
    """假的 `WarmupLease`：只记调用，不碰网络。"""

    def __init__(self, *, supports_ports: bool = True) -> None:
        self.ports: list = []
        self.released = 0
        self.key = "KEY"
        self.adopted = None
        self.provider = None
        if supports_ports:
            self.set_local_ports = self._record_ports   # 实例属性，覆盖成真方法
        # 不支持时保持类里没有该属性 → `getattr(lease, "set_local_ports", None)` 为 None

    def _record_ports(self, ports) -> None:
        self.ports.append(ports)

    def drain(self, timeout: float = 0.2):
        return []

    def names_copy(self):
        return "names"

    def recent_cipher(self):
        return ["cipher"]

    def bind_status(self, provider) -> None:
        self.provider = provider

    def adopt(self, table) -> None:
        self.adopted = table

    def release(self) -> None:
        self.released += 1


def test_capture_adapter() -> None:
    print("[players-prewarm] _WarmupCapture 的平台差异")
    lease = _Lease()
    cap = leg._WarmupCapture(lease)

    check("端口默认 27002（lease 没有 port 属性）", cap._port == 27002, str(cap._port))

    cap.set_local_ports({27002, 49352})
    check("端口转给了 lease", lease.ports == [{27002, 49352}], str(lease.ports))

    bare = leg._WarmupCapture(_Lease(supports_ports=False))
    try:
        bare.set_local_ports({1})
        ok = True
    except Exception:                       # noqa: BLE001 - 就是要它别抛
        ok = False
    check("lease 没有该口子也不抛", ok)

    if os.name == "nt":
        payload = object()
        check("Windows 下 parse 恒等", cap.parse(payload) is payload)

    check("key 透传", cap.cached_key() == "KEY", str(cap.cached_key()))
    check("名字表透传", cap.initial_names() == "names")
    check("回放透传", cap.recent_cipher() == ["cipher"])
    cap.adopt_names("table")
    check("收养结果回给 lease", lease.adopted == "table", str(lease.adopted))

    lease.key = None                    # 预热还没拿到密钥：腿只能报"未就绪"去退避
    try:
        cap.acquire_key([])
        check("取钥失败要抛", False, "没抛")
    except leg._KeyUnavailable as exc:
        check("取钥只读预热缓存：报「还没有密钥」而不是自己去扫",
              "预热还没有密钥" in str(exc), str(exc))
        check("取钥失败是「可重试」（非 fatal）", exc.fatal is False, str(exc))


# ---------------------------------------------------------------- 3) 预热器转发


def test_warmup_forwards_ports() -> None:
    print("[players-prewarm] PlayerWarmup.set_local_ports")
    got: list = []

    class _Sock:
        def set_local_ports(self, ports) -> None:
            got.append(ports)

    obj = warmup.PlayerWarmup.__new__(warmup.PlayerWarmup)
    obj._lock = threading.RLock()

    obj._socket = _Sock()
    obj.set_local_ports({27002})
    check("有口子就转发", got == [{27002}], str(got))

    obj._socket = object()                  # POSIX 助手会话：没有 set_local_ports
    obj.set_local_ports({27002})
    check("没口子静默跳过", got == [{27002}], str(got))

    class _Boom:
        def set_local_ports(self, ports) -> None:
            raise OSError("端口表炸了")

    obj._socket = _Boom()
    obj.set_local_ports({1})                # 不该外溢
    check("底层抛错不外溢", True)

    obj._socket = None
    obj.set_local_ports({1})
    check("还没 socket 也不抛", True)

    obj._socket = _Sock()
    warmup.WarmupLease(obj).set_local_ports({9})
    check("lease 转给预热器", got[-1] == {9}, str(got[-1]))


# ---------------------------------------------------------------- 4) 租借优先


class _FakeWarmup:
    def __init__(self, lease, *, boom: bool = False) -> None:
        self._lease = lease
        self._boom = boom
        self.lease_calls = 0

    def lease(self):
        self.lease_calls += 1
        if self._boom:
            raise RuntimeError("预热器炸了")
        return self._lease


def _leg_with(fake) -> tuple[leg.PlayerLeg, dict]:
    seen: dict = {}
    obj = leg.PlayerLeg.__new__(leg.PlayerLeg)
    obj.warmup = fake
    obj.port = 27002
    obj.on_status = lambda *a: seen.setdefault("status", []).append(a)

    def _decode(host, log_names, cap) -> None:
        seen["cap"] = cap

    obj._decode = _decode                   # 实例属性遮蔽类方法，不碰真抓包
    return obj, seen


def test_run_once_prefers_lease() -> None:
    print("[players-prewarm] _run_once 先试租借")
    lease = _Lease()
    fake = _FakeWarmup(lease)
    obj, seen = _leg_with(fake)
    obj._run_once(object(), {})

    check("用了租借而不是一次性抓包",
          isinstance(seen.get("cap"), leg._WarmupCapture), str(type(seen.get("cap"))))
    check("lease 问过一次", fake.lease_calls == 1, str(fake.lease_calls))
    check("租借被还回去", lease.released == 1, str(lease.released))
    check("状态里报了「复用预热」",
          any("复用预热" in str(v) for v in seen.get("status", [])), str(seen.get("status")))


def test_no_lease_falls_back() -> None:
    print("[players-prewarm] 租不到时退回本平台一次性路径")
    obj, seen = _leg_with(_FakeWarmup(None))
    if os.name == "nt":
        # Windows 回落 `_WinCapture`：这里只要确认它没走租借、并且没抛。
        try:
            obj._run_once(object(), {})
            raised = None
        except Exception as exc:            # noqa: BLE001 - 假 host，抓包必然失败
            raised = exc
        check("Windows 回落不再用 _WarmupCapture",
              not isinstance(seen.get("cap"), leg._WarmupCapture), str(type(seen.get("cap"))))
        check("Windows 回落不报「未就绪」", "预热器尚未就绪" not in str(raised), str(raised))
    else:
        try:
            obj._run_once(object(), {})
            check("POSIX 没预热要报错", False, "没抛")
        except RuntimeError as exc:
            check("POSIX 报「预热器尚未就绪」", "预热器尚未就绪" in str(exc), str(exc))


def test_warmup_exception_does_not_block() -> None:
    print("[players-prewarm] 预热器抛错也不拖垮翻译")
    obj, seen = _leg_with(_FakeWarmup(None, boom=True))
    try:
        obj._run_once(object(), {})
        ok = True
    except Exception as exc:                # noqa: BLE001 - 假 host，正式路径必然失败
        # 只允许「未就绪」，不能是预热那层抛出来的 RuntimeError("预热器炸了")
        ok = "预热器炸了" not in str(exc)
    check("预热异常被降级成「常规取钥」告警",
          any("预热不可用" in str(v) for v in seen.get("status", [])), str(seen.get("status")))
    check("没有把预热异常直接抛给外层", ok, str(seen.get("status")))


def main() -> int:
    test_prewarm_key_is_gone()
    test_capture_adapter()
    test_warmup_forwards_ports()
    test_run_once_prefers_lease()
    test_no_lease_falls_back()
    test_warmup_exception_does_not_block()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 → {', '.join(FAILS)}")
        return 1
    print("ALL PASSED（预热与翻译解耦）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
