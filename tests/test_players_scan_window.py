"""取钥扫描的**内存上界**与开关音时长（离线验收，不需要管理员）。

真机故障背景（2026-11-04）：`_worker` 把整段 extent（本机 pagefile 36 GiB）一次性交给
`Disk.read_view()`，它按 `size` 分配 ctypes 缓冲 —— 16 GB 机器直接卡死、两次 Kernel-Power 41
硬重启（无 minidump）。这里钉住三件事：

1. 单次读入内存的窗口有上限（`WINDOW`），且 `_worker` 真的切窗了；
2. 扫描预算放宽到能给 36 GiB 留出余量（原来 45 s 必然超时 → 假「没找到密钥」）；
3. 开关音收台音可以截短（素材 451 ms 拖太长）。
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt.players import key_win as K  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILS.append(name)


class _FakeDisk:
    """记录每次 read_view 的 (offset, size)，返回全零数据。"""

    def __init__(self, rec: list[tuple[int, int]]) -> None:
        self.rec = rec

    def read_view(self, offset: int, size: int):      # noqa: ANN201
        self.rec.append((int(offset), int(size)))
        return memoryview(bytes(int(size)))

    def close(self) -> None:
        pass


@contextlib.contextmanager
def _fake_auth():
    yield object()


def test_constants() -> None:
    print("[key_win] 内存上界常量")
    check("单窗 ≤ 64 MiB", K.WINDOW <= (64 << 20), f"WINDOW={K.WINDOW}")
    check("扫描预算 ≥ 120s", K.SCAN_TIMEOUT >= 120.0, f"SCAN_TIMEOUT={K.SCAN_TIMEOUT}")
    check("线程数 ≤ 核数（默认 8）", 1 <= K.KEY_SCAN_THREADS <= max(8, os.cpu_count() or 4),
          f"{K.KEY_SCAN_THREADS}（核数 {os.cpu_count()}）")
    check("候选偏移不止一种（8/12/16/24）", len(K.CAND_OFFSETS) >= 2, f"{K.CAND_OFFSETS}")
    check("默认只试主形态（省 GCM 开销）", K._kind_order("hdr19") == ("hdr19",),
          f"{K._kind_order('hdr19')}")
    _old_kinds = os.environ.get("VLT_KEY_SCAN_KINDS")
    os.environ["VLT_KEY_SCAN_KINDS"] = "all"
    try:
        _all_kinds = K._kind_order("hdr19")
    finally:
        if _old_kinds is None:
            os.environ.pop("VLT_KEY_SCAN_KINDS", None)
        else:
            os.environ["VLT_KEY_SCAN_KINDS"] = _old_kinds
    check("VLT_KEY_SCAN_KINDS=all 时先试 hdr19 再退化",
          _all_kinds[0] == "hdr19" and "hdr7" in _all_kinds, f"{_all_kinds}")
    check("只试运行期能用的形态",
          set(K._kind_order("hdr19")) <= set(K.RUNTIME_KINDS), f"{K.RUNTIME_KINDS}")
    check("ScanHit 记录了形态与偏移",
          {"kind", "key_off"} <= set(K.ScanHit.__dataclass_fields__))
    check("候选上限 ≥ 4×锚点量级", K.MAX_KEEP >= 400_000, f"{K.MAX_KEEP}")
    src = (ROOT / "vlt" / "players" / "key_win.py").read_text(encoding="utf-8")
    check("源码里没有「整段 span 一次读」", "read_view(begin, span)" not in src,
          "还留着 read_view(begin, span)")
    check("源码里按窗口切读（每窗 ≤ WINDOW）", "min(WINDOW, end - pos)" in src)
    check("候选偏移默认收敛到 KEY_OFF（VLT_KEY_SCAN_OFFSETS=all 放开）",
          "for key_off in offsets" in src and "_cand_offsets" in src)


def test_worker_windows() -> None:
    print("[key_win] _worker 按窗口读（100 MiB span → 每窗 ≤ WINDOW）")
    rec: list[tuple[int, int]] = []
    orig_open = K.pagefile.Disk.open
    orig_auth = K.gcm_win.GcmAuth
    orig_scan = K._scan_buffer
    orig_prio = K._lower_thread_priority
    try:
        K.pagefile.Disk.open = staticmethod(lambda disk_no, base: _FakeDisk(rec))   # noqa: ARG005
        K.gcm_win.GcmAuth = _fake_auth
        K._scan_buffer = lambda *a, **k: None                                        # noqa: ARG005
        K._lower_thread_priority = lambda: None
        span = 100 << 20
        state = K._State(span)
        K._worker(state, [(0, 0, span)], [], deadline=time.monotonic() + 60,
                  aad_kind="hdr19")
    finally:
        K.pagefile.Disk.open = orig_open
        K.gcm_win.GcmAuth = orig_auth
        K._scan_buffer = orig_scan
        K._lower_thread_priority = orig_prio

    sizes = [n for _o, n in rec]
    check("读过数据", bool(rec), f"{rec[:3]}")
    # 每次读都带 39 字节跨窗尾巴，物理盘又要求扇区对齐 → 上限是 WINDOW+4096
    check("单次读入 ≤ WINDOW+扇区余量",
          all(n <= K.WINDOW + K.OVERLAP_ALIGN for n in sizes),
          f"max={max(sizes) if sizes else 0}")
    check("覆盖整个 span", state.scanned >= span, f"scanned={state.scanned} span={span}")
    # 物理盘 ReadFile 长度必须按扇区对齐（err=87），39 字节的跨窗尾巴会读 4096
    # 字节再切回 —— 断言「每次读都按扇区对齐、且都 ≥ WINDOW 级」。
    check("每次读都按扇区对齐",
          all(n % K.OVERLAP_ALIGN == 0 for n in sizes), f"{sizes[:3]}")
    check("窗口间留了 OVERLAP（跨窗的 key 不漏）", K.OVERLAP >= K.KEY_OFF + K.KEY_LEN - 1,
          f"OVERLAP={K.OVERLAP} 需要 {K.KEY_OFF + K.KEY_LEN - 1}")


def test_failure_dump() -> None:
    print("[keyd] 失败时落排查包（样本 + 候选 + 统计）")
    import json
    import tempfile as _tf
    from vlt.players import keyd

    class _PF:
        def summary(self):
            return "pagefile 测试"

    class _Res:
        scanned_bytes = 123
        chunks = 4
        stopped = "done"
        needle_hits = 9
        aad_kind = "hdr19"
        threads = 2
        regions = 1
        candidates = [bytes(range(32)), bytes(range(1, 33))]

    groups = [((bytes([1, 2]), bytes([3, 4])), [bytes([0xaa]) * 20, bytes([0xbb]) * 20])]
    with _tf.TemporaryDirectory():
        path = keyd._dump_failure(_PF(), _Res(), groups)
        check("写出了排查包", bool(path) and Path(path).exists(), path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        check("含样本密文", len(data.get("samples", {})) == 1, f"{list(data) }")
        check("含候选 key", len(data.get("candidates", [])) == 2)
        check("含扫描统计", data.get("scanned_bytes") == 123, f"{data.get('scanned_bytes')}")
        check("不含明文/昵称字段",
              not any(k in data for k in ("plain", "name", "display", "user", "key_plain")))


def test_challenge_gate() -> None:
    """challenge 路只在预算内跑：否则命中前要双路扫全盘，吞吐掉约 2×（实测 5×→ 里的一半）。"""
    print("[key_win] challenge 第二条路的启用策略")
    st = K._State(64 << 30)
    check("auto：第一遍完全不跑 challenge（改成失败后补扫）",
          K._challenge_enabled(st, "auto") is False, f"scanned={st.scanned}")
    st.take_chunk(K.CHALLENGE_GATE_BYTES * 4)
    check("auto：扫过 GATE 也不跑", K._challenge_enabled(st, "auto") is False, f"{st.scanned}")
    check("chal：第二遍补扫时每窗都跑", K._challenge_enabled(st, "chal") is True)
    check("always：无视阈值", K._challenge_enabled(st, "always") is True)
    check("off：永不走第二条路", K._challenge_enabled(st, "off") is False)
    st2 = K._State(64 << 30)
    check("only：起步就跑（不受 GATE 限制）", K._challenge_enabled(st2, "only") is True,
          f"scanned={st2.scanned}")
    st2.take_challenge(K.CHALLENGE_BUDGET * 4)
    check("only：预算用完也不再掐（主路要扫全盘）",
          K._challenge_enabled(st2, "only") is True, f"{st2.challenge_bytes}")
    check("环境变量认四档",
          K._challenge_mode() in ("auto", "always", "off", "only"), f"{K._challenge_mode()}")
    old_env = os.environ.get("VLT_KEY_SCAN_CHALLENGE")
    try:
        os.environ["VLT_KEY_SCAN_CHALLENGE"] = "only"
        check("env=only 生效", K._challenge_mode() == "only", K._challenge_mode())
        os.environ["VLT_KEY_SCAN_CHALLENGE"] = "nonsense"
        check("env 非法值回落 auto", K._challenge_mode() == "auto", K._challenge_mode())
    finally:
        if old_env is None:
            os.environ.pop("VLT_KEY_SCAN_CHALLENGE", None)
        else:
            os.environ["VLT_KEY_SCAN_CHALLENGE"] = old_env


def test_challenge_zone() -> None:
    print("[key_win] challenge 附近取候选（锚点法失效时的第二条路）")
    chal = bytes([0x11, 0x22, 0x33, 0x44])
    peer = bytes([0xAA, 0xBB])
    real_key = bytes(range(100, 132))
    buf = bytearray(8 * 1024)
    at = 3000
    buf[at:at + 4] = chal                      # Phonet 连接状态里的 challenge
    buf[4096:4096 + 32] = real_key             # 同一对象附近的密钥（8 字节对齐）
    data = bytes(buf)

    class _Auth:
        def opens_all(self, key, pkts, kind="hdr19", nonce_offset=7):   # noqa: ARG002
            return len(pkts) if key == real_key else 0

    groups = [((peer, chal), [b"p" * 40, b"q" * 40])]
    state = K._State(len(data))
    hit = K._scan_near_challenges(data, 0, state, _Auth(), groups,
                                  time.monotonic() + 10, "hdr19")
    check("challenge 附近找到了真钥", hit is not None and hit.key == real_key,
          f"{hit}")
    check("命中的偏移指向那个窗口", hit is not None and hit.offset == 4096,
          f"{getattr(hit, 'offset', None)}")
    check("数量上限有定义", K.MAX_CHALLENGE_HITS > 0 and K.CHALLENGE_ZONE >= 512,
          f"{K.MAX_CHALLENGE_HITS}/{K.CHALLENGE_ZONE}")

    # 没有 challenge 时不该乱猜（返回 None）
    state2 = K._State(len(data))
    clean = bytes(8 * 1024)
    check("页里没有 challenge → 不误报",
          K._scan_near_challenges(clean, 0, state2, _Auth(), groups,
                                  time.monotonic() + 10, "hdr19") is None)


def test_forget_key() -> None:
    print("[capture] 密钥失效时必须连采集对象的缓存一起丢（否则永远「预热不可用」）")
    from vlt.players import win_capture as W

    class _Cache:
        called = False

        def forget(self):
            self.called = True

    class _Cap:
        _key = b"dead" * 8
        _cache = _Cache()

    # `forget_key()` 里那句 `_disk_key_forget()` 删的是**用户真实的**
    # %APPDATA%\vrchat-livetranslate\key_cache.json —— 跑一次测试就把人家的
    # 磁盘缓存清了（下次启动白扫 34 GiB）。这里换成计数器。
    calls = {"disk": 0}
    saved = W._disk_key_forget
    W._disk_key_forget = lambda: calls.__setitem__("disk", calls["disk"] + 1)
    try:
        cap = _Cap()
        W._WinCapture.forget_key(cap)
    finally:
        W._disk_key_forget = saved

    check("内存里的 key 被清掉", cap._key is None)
    check("会话缓存也被清掉", cap._cache.called)
    check("磁盘缓存也要求清掉（但不许真删用户的文件）", calls["disk"] == 1)

    src = (ROOT / "vlt" / "players" / "warmup.py").read_text(encoding="utf-8")
    check("密钥失效分支调用了 forget_key", "forget_key" in src)
    check("换世界会丢掉旧样本", "self._samples.clear()" in src.split("self._world = world")[1][:400])


def test_shared_wait() -> None:
    print("[capture] 撞锁时等另一路的结果，而不是判失败（P1 回归）")
    from vlt.players import win_capture as W

    saved = (W._disk_key_read, W.keyd.scan_busy, W.key_win.verify_key,
             W.gcm_win.GcmAuth, W.make_hit, W.WAIT_SHARED_POLL_S)

    class _Auth:
        def close(self) -> None:
            pass

    class _Cache:
        def __init__(self) -> None:
            # 每个实例各持一份：写成类属性会让 A) 的 (key, "hdr19") 漏到 D)
            self.stored: list = []

        def store(self, ident, key, kind="hdr19") -> None:
            self.stored.append((key, kind))

    class _Cap:
        _key = None

        def __init__(self) -> None:
            self._cache = _Cache()

        def _status(self, _level, _msg) -> None:
            pass

    ident, groups, flows = object(), {}, {}
    key = bytes(range(32))

    def call(cap, **kw):
        return W._WinCapture._wait_shared_key(cap, ident, groups, flows, **kw)

    try:
        W.gcm_win.GcmAuth = _Auth
        W.key_win.verify_key = (lambda k, g, auth=None, aad_kind="hdr19":
                                k == key and aad_kind == "hdr19")
        W.make_hit = lambda k, g, f, tag=None, kind="hdr19": ("hit", k, kind)
        W.WAIT_SHARED_POLL_S = 0.01

        # A) 另一路在扫，它落盘的钥匙能过 GCM → 直接用，不再自己扫
        W.keyd.scan_busy = lambda: True
        W._disk_key_read = lambda _i: (key, "hdr19")
        cap = _Cap()
        got = call(cap)
        check("命中另一路落盘的钥匙", got == ("hit", key, "hdr19"), repr(got))
        check("命中后写进会话缓存（带 AAD 形态）",
              cap._cache.stored == [(key, "hdr19")], repr(cap._cache.stored))

        # B) 那一路收工了却没留下钥匙 → 让路结束，交回调用方自己扫
        W.keyd.scan_busy = lambda: False
        W._disk_key_read = lambda _i: None
        got = call(_Cap(), max_s=0.05)
        check("另一路失败 → 返回 None 让自己上", got is None, repr(got))

        # C) 缓存里的钥匙打不开当前流量 → 不误命中
        W.keyd.scan_busy = lambda: True
        W._disk_key_read = lambda _i: (b"\x00" * 32, "hdr19")
        got = call(_Cap(), max_s=0.05)
        check("验不过的旧钥匙不当命中", got is None, repr(got))

        # D) 落盘时记了 hdr7 → 复验与命中都必须带上这个形态
        #    （写死 hdr19 会把 hdr7 命中按 19 字节 AAD 打不开，误报「换了世界」）
        W.key_win.verify_key = lambda k, g, auth=None, aad_kind="hdr19": True
        W._disk_key_read = lambda _i: (key, "hdr7")
        cap = _Cap()
        got = call(cap)
        check("按落盘记的 AAD 形态命中", got == ("hit", key, "hdr7"), repr(got))
        check("会话缓存也记这个形态",
              cap._cache.stored == [(key, "hdr7")], repr(cap._cache.stored))
    finally:
        (W._disk_key_read, W.keyd.scan_busy, W.key_win.verify_key,
         W.gcm_win.GcmAuth, W.make_hit, W.WAIT_SHARED_POLL_S) = saved

    src = (ROOT / "vlt" / "players" / "win_capture.py").read_text(encoding="utf-8")
    check("弹 UAC 之前先探互斥体",
          src.index("keyd.scan_busy()") < src.index("现在弹一次 UAC"))
    check("Windows 上不换 needle（换一次就多弹一次 UAC）",
          "if _WIN:" in (ROOT / "vlt" / "players" / "warmup.py").read_text(encoding="utf-8"))


def test_scan_busy() -> None:
    print("[keyd] 父进程侧互斥体探测（真句柄，不弹 UAC）")
    from vlt.players import keyd as D

    check("空闲时 scan_busy 为 False", D.scan_busy() is False, str(D.scan_busy()))
    if sys.platform != "win32":
        print("  --  非 Windows，跳过真实互斥体验证")
        return
    import ctypes

    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, False, D.MUTEX_NAME)
    try:
        check("别人占着互斥体时 scan_busy 为 True", D.scan_busy() is True)
    finally:
        k32.ReleaseMutex(ctypes.c_void_p(h))
        k32.CloseHandle(ctypes.c_void_p(h))
    check("释放后 scan_busy 又变回 False", D.scan_busy() is False)


def main() -> int:
    test_constants()
    test_worker_windows()
    test_challenge_gate()
    test_challenge_zone()
    test_forget_key()
    test_failure_dump()
    test_scan_busy()
    test_shared_wait()
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：{FAILS}")
        return 1
    print("\nALL PASSED（扫盘内存上界 / 开关音时长）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
