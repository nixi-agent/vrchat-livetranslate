#!/usr/bin/env python
"""pagefile 取钥链路的**卷→物理盘**映射（Windows，离线验收）。

## 为什么

`pagefile.py` 里所有 offset 都是**卷内**偏移，`Disk` 内部加上「分区在盘上的
起点」换成盘上偏移。真实的机器上分区起点属于**某一块具体物理盘**，而
`volume_extents()` 返回的 `DiskExtent.disk_number` 以前被直接丢掉、调用方
一律 `Disk.open(0, …)`。

后果（2026-11 真机）：系统盘是 NVMe 的 `PhysicalDrive3`，`PhysicalDrive0`
是挂 F: 的数据盘 —— 拿 C: 的分区起点（120 GiB）去读 disk0，读到的是 F: 的
文件数据，boot sector 判不出 NTFS，取钥恒失败（`[warmup] ⚠️ 扫描失败`，
每 10 秒重试，卡在「正在获取密钥」）。

## 这里钉住

1. `Layout` 保留**盘号**，`resolve()` 把卷内区间翻成 `(盘号, 盘上偏移, 长度)`；
2. 跨段的区间要按段切开（卷可能跨多块盘），不许一段读错整条；
3. `open_volume()` 必须用 `extents[0].disk_number` 开局，**不许写死 0**；
4. `key_win._phys_tasks()` 交给扫描线程的任务带的是盘上绝对偏移；
5. 查询失败要抛 `PagefileError`，不许静默退回 disk0。

跑法：python tests/test_players_pagefile.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.players import key_win, pagefile  # noqa: E402

FAILS: list[str] = []

# 真机口径（2026-11 实测）：C: 在 disk3，起点 120 GiB。
NVME = [pagefile.DiskExtent(3, 128849018880, 830714215424)]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


class _Patched:
    """临时替换 `pagefile.volume_extents` / `pagefile.Disk`，退出即还原。"""

    def __init__(self, extents=None, disk=None, raise_exc=None) -> None:
        self._ext = extents if extents is not None else []
        self._disk = disk
        self._raise = raise_exc
        self._old_ext = None
        self._old_disk = None

    def __enter__(self):
        self._old_ext = pagefile.volume_extents
        pagefile.volume_extents = self._fake_extents
        if self._disk is not None:
            self._old_disk = pagefile.Disk
            pagefile.Disk = self._disk
        return self

    def _fake_extents(self, drive: str = "C"):
        if self._raise is not None:
            raise self._raise
        return list(self._ext)

    def __exit__(self, *exc):
        pagefile.volume_extents = self._old_ext
        if self._old_disk is not None:
            pagefile.Disk = self._old_disk
        return False


class _FakeDisk:
    """记录 `Disk.open()` 收到的参数（真盘要管理员，测试里不需要）。"""

    opened: list[tuple[int, int]] = []

    def __init__(self, disk_no: int, base: int) -> None:
        self.disk_no = disk_no
        self.base_offset = base
        type(self).opened.append((disk_no, base))

    @classmethod
    def open(cls, disk_no: int = 0, base_offset: int = 0):
        return cls(disk_no, base_offset)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def t_layout_keeps_disk_number() -> None:
    print("[layout] 盘号不能丢")
    with _Patched(NVME):
        lay = pagefile.Layout.of("C")
    check("disk_no = 3", lay.disk_no == 3, f"{lay.disk_no}")
    check("base = 120 GiB", lay.base_offset == 128849018880, f"{lay.base_offset}")
    check("total = 单段长度", lay.total_bytes == 830714215424, f"{lay.total_bytes}")


def t_resolve_single_extent() -> None:
    print("[layout] 单段：卷内 → 盘上")
    lay = pagefile.Layout(tuple(NVME))
    got = lay.resolve(0, 4096)
    check("单段整块翻到 disk3", got == [(3, 128849018880, 4096)], f"{got}")
    got = lay.resolve(8 << 20, 1 << 20)
    check("偏移累加正确",
          got == [(3, 128849018880 + (8 << 20), 1 << 20)], f"{got}")


def t_resolve_crosses_extents() -> None:
    print("[layout] 跨段：按段切开")
    lay = pagefile.Layout((
        pagefile.DiskExtent(3, 1000, 4096),
        pagefile.DiskExtent(5, 9000, 4096),
    ))
    got = lay.resolve(4000, 200)
    check("切成两段", len(got) == 2, f"{got}")
    check("第一段在 disk3 尾巴", got[0] == (3, 1000 + 4000, 96), f"{got[0] if got else None}")
    check("第二段在 disk5 开头", got[1] == (5, 9000, 104), f"{got[1] if len(got) > 1 else None}")
    check("总长不变", sum(n for _d, _o, n in got) == 200, f"{got}")


def t_open_volume_uses_extent_disk() -> None:
    print("[open_volume] 用 extents[0].disk_number，不许写死 0")
    _FakeDisk.opened.clear()
    with _Patched(NVME, disk=_FakeDisk):
        disk, lay = pagefile.open_volume("C")
    check("开的是 disk3（不是 0）", _FakeDisk.opened == [(3, 128849018880)],
          f"{_FakeDisk.opened}")
    check("返回的 Layout 也是 disk3", lay.disk_no == 3, f"{lay.disk_no}")
    check("句柄带正确盘号", getattr(disk, "disk_no", None) == 3,
          f"{getattr(disk, 'disk_no', None)}")


def t_open_volume_propagates_error() -> None:
    print("[open_volume] 查不到卷：抛 PagefileError")
    err = pagefile.PagefileError("打不开卷 \\\\.\\C: err=5")
    raised = None
    try:
        with _Patched(raise_exc=err):
            pagefile.open_volume("C")
    except pagefile.PagefileError as exc:
        raised = exc
    check("抛 PagefileError", raised is not None)


def t_phys_tasks_absolute() -> None:
    print("[key_win] 扫描任务带盘上与绝对偏移")
    lay = pagefile.Layout(tuple(NVME))
    tasks = [(0, 8 << 20), (8 << 20, 4096)]
    got = key_win._phys_tasks(tasks, lay)
    want = [
        (3, 128849018880, 8 << 20),
        (3, 128849018880 + (8 << 20), 4096),
    ]
    check("盘号全部是 3", all(d == 3 for d, _o, _n in got), f"{got}")
    check("偏移 = 卷内偏移 + 盘上起点", got == want, f"{got}")


def t_phys_tasks_splits() -> None:
    print("[key_win] 跨段任务被切开，长度守恒")
    lay = pagefile.Layout((
        pagefile.DiskExtent(3, 0, 1 << 20),
        pagefile.DiskExtent(5, 0, 1 << 20),
    ))
    got = key_win._phys_tasks([((1 << 20) - 10, 100)], lay)
    check("切成两块", len(got) == 2, f"{got}")
    check("总长守恒", sum(n for _d, _o, n in got) == 100, f"{got}")


def t_legacy_layout_covers_spans() -> None:
    print("[key_win] 老口径（无 layout）合成段要覆盖 pagefile 的高位偏移")
    gib = 1 << 30
    spans = [(5 * gib, 3 * gib)]          # pagefile 从卷内 5 GiB 才开始
    lay = key_win._layout_from_one_extent(spans, 0, 1000)
    got = key_win._phys_tasks([(5 * gib, 1 << 20)], lay)
    check("任务没被段外裁掉", len(got) == 1, f"{got}")
    check("偏移 = 盘上起点 + 卷内偏移",
          bool(got) and got[0] == (0, 1000 + 5 * gib, 1 << 20), f"{got}")


def t_layout_validates() -> None:
    print("[layout] 空/零长 extent 直接拒绝")
    for bad, name in (((), "空 extents"),
                      ((pagefile.DiskExtent(3, 0, 0),), "零长 extent")):
        raised = None
        try:
            pagefile.Layout(bad)
        except pagefile.PagefileError as exc:
            raised = exc
        check(f"{name} → PagefileError", raised is not None)
    raised = None
    try:
        pagefile.Layout(tuple(NVME)).resolve(-1, 4096)
    except ValueError as exc:
        raised = exc
    check("负偏移 → ValueError", raised is not None)


def t_open_volume_rejects_multi_extent() -> None:
    print("[open_volume] 跨盘卷：明确拒绝（NTFS 解析没有段路由）")
    exts = [pagefile.DiskExtent(3, 0, 1 << 20), pagefile.DiskExtent(5, 0, 1 << 20)]
    raised = None
    with _Patched(exts, disk=_FakeDisk):
        try:
            pagefile.open_volume("C")
        except pagefile.PagefileError as exc:
            raised = exc
    check("抛 PagefileError", raised is not None)
    check("消息里点出 spanned", "跨了 2 块物理盘" in str(raised), f"{raised}")


def t_phys_tasks_rejects_drops() -> None:
    print("[key_win] 映射丢字节 → 抛错，不许静默少扫")
    lay = pagefile.Layout((pagefile.DiskExtent(3, 0, 4096),))
    raised = None
    try:
        key_win._phys_tasks([(0, 4096), (8192, 4096)], lay)   # 第二个任务落在段外
    except ValueError as exc:
        raised = exc
    check("抛 ValueError", raised is not None)
    check("消息里带丢了多少字节", "4096" in str(raised), f"{raised}")


def t_callsites_use_open_volume() -> None:
    print("[回归] 取钥调用点不许再写死 disk 0")
    players = Path(ROOT) / "vlt" / "players"
    offenders = []
    for py in sorted(players.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            # 跳过注释与 docstring 里的行内代码（``Disk.open(0, …)`` 是「不要这样写」的说明）
            if "Disk.open(0" in line and not line.strip().startswith("#") and "`" not in line:
                offenders.append(f"{py.name}:{i}")
    check("vlt/players 里没有 Disk.open(0, …)", not offenders, ",".join(offenders))
    for name in ("keyd.py", "win_capture.py"):
        text = (players / name).read_text(encoding="utf-8")
        check(f"{name} 走 open_volume()", "open_volume(" in text)


class _FakeAuth:
    """替掉 gcm_win.GcmAuth（Linux 上 CNG 不可用，这层只测任务分派）。"""

    def __init__(self, *a, **kw) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def opens_all(self, key, packets, aad_kind="hdr19", nonce_offset=None):
        return 0


class _DeadDisk:
    """任何一次 open 都失败：用来验证「读不到」会被记成 unread，而不是「扫过了没命中」。"""

    @classmethod
    def open(cls, disk_no=0, base_offset=0):
        raise OSError(f"模拟 PhysicalDrive{disk_no} 打不开")


def t_worker_records_unread() -> None:
    print("[worker] 盘打不开 → unread_bytes / stopped=partial")
    vol = pagefile.Volume(serial=1, bytes_per_sector=512, bytes_per_cluster=4096,
                          record_size=1024, mft_start_lcn=0, mft2_start_lcn=0,
                          total_clusters=1024)
    pf = pagefile.Pagefile(mft_index=42, name="pagefile.sys",
                           extents=(pagefile.Extent(0, 16),), real_bytes=65536,
                           volume=vol)
    pkt = bytes([1, 2, 0x01, 3, 4]) + bytes(range(12)) + bytes(22)
    # 真实头是 [peerID:2][0x01][challenge:4][nonce:12]：challenge 从 index 3 起。
    # 以前这里写 b"\x01\x02\x03\x04"（按 payload[1:5] 的口径），会把「tag 取错字节」
    # 这个 bug 盖住（见 #136 review 第 1 条）。
    samples = {(bytes(pkt[0:2]), bytes(pkt[3:7])): [pkt, pkt]}

    old_disk, old_auth = pagefile.Disk, key_win.gcm_win.GcmAuth
    pagefile.Disk = _DeadDisk
    key_win.gcm_win.GcmAuth = _FakeAuth
    try:
        res = key_win.scan_key(pf, samples, layout=pagefile.Layout(tuple(NVME)),
                               threads=1, deadline_s=3.0)
    finally:
        pagefile.Disk, key_win.gcm_win.GcmAuth = old_disk, old_auth

    check("hit 为 None", res.hit is None)
    check("unread_bytes 覆盖全部字节", res.unread_bytes == res.region_bytes,
          f"{res.unread_bytes} vs {res.region_bytes}")
    check("stopped=partial", res.stopped == "partial", res.stopped)
    check("failed_disks=[3]", res.failed_disks == [3], f"{res.failed_disks}")


def t_samples_group_by_header() -> None:
    """分组键必须按 Photon 头取 `(peerID, challenge[3:7])`，不是 `payload[1:5]`。

    #136 review 第 1 条：`payload[2]` 是固定 `0x01` 标志位，把它算进 challenge 会让
    分组键漂移；`_scan_near_challenges()` 拼的锚是 `peer + 0x01 + chal`，取错字节后
    几乎每个包都会被当成锚点，白烧 CPU。这里钉住口径，防止再改回去。
    """
    if sys.platform != "win32":
        print("  --  [capture] 非 Windows，跳过分组口径验证")
        return
    print("[capture] 分组键 = (payload[0:2], payload[3:7])")

    from vlt.players import gcm, win_capture as W  # noqa: PLC0415 - 平台相关，晚导入

    flow = gcm.Flow.of(("10.0.0.1", 50000), ("10.0.0.2", 5056))

    def pkt(chal: bytes, nonce: bytes, flag: bytes = b"\x01") -> bytes:
        # [peerID:2][0x01][challenge:4][nonce:12][ciphertext+tag]
        return b"\xaa\xbb" + flag + bytes(chal) + bytes(nonce) + b"\x00" * 16

    a = pkt(b"\x11\x22\x33\x44", bytes(range(12)))
    b = pkt(b"\x11\x22\x33\x44", bytes(range(12, 24)))
    bad = pkt(b"\x11\x22\x33\x44", bytes(range(12)), flag=b"\x02")
    groups, flows = W.samples_to_groups([(flow, a), (flow, b), (flow, bad)])

    tag = (b"\xaa\xbb", b"\x11\x22\x33\x44")
    check("同 peer/challenge 的两包归一组", list(groups) == [tag], f"{list(groups)}")
    check("标志位不是 0x01 的包被丢掉", len(groups[tag]) == 2, f"{len(groups[tag])}")
    check("反查表带回 Flow", flows.get(tag) == flow, f"{flows.get(tag)}")

    hit = W.make_hit(b"\x01" * 32, groups, flows, kind="hdr7")
    check("hdr7 命中按 7 字节 AAD 布局", hit.layout == "hdr", hit.layout)
    hit19 = W.make_hit(b"\x01" * 32, groups, flows)
    check("默认仍是 hdr+nonce", hit19.layout == gcm.LAYOUTS[0], hit19.layout)


def t_from_cache_reads_disk() -> None:
    """会话缓存空着也必须读磁盘缓存（#136 review 第 2 条）。

    以前 `_from_cache()` 开头 `if self._cache.peek() is None: return None` 直接早退，
    `_disk_key_read(ident)` 永远跑不到 —— 新进程永远用不上 `key_cache.json`，
    磁盘缓存等于没做。这里钉住「缓存空 → 读盘 → 带 AAD 形态复验」。
    """
    if sys.platform != "win32":
        print("  --  [capture] 非 Windows，跳过磁盘缓存回读验证")
        return
    print("[capture] 会话缓存空 → 读磁盘缓存（带 AAD 形态）")

    from vlt.players import win_capture as W  # noqa: PLC0415 - 平台相关，晚导入

    class _Leg:
        def on_status(self, _level, _msg) -> None:
            pass

    class _Auth:
        def close(self) -> None:
            pass

    key = bytes(range(32))
    tag = (b"\xaa\xbb", b"\x11\x22\x33\x44")
    groups = {tag: [b"p", b"p"]}
    flows = {tag: "flow"}
    saved = (W._disk_key_read, W._disk_key_forget, W.key_win.verify_key, W.gcm_win.GcmAuth)
    calls = {"read": 0, "forget": 0}
    try:
        W.gcm_win.GcmAuth = _Auth
        W._disk_key_read = lambda _ident: (calls.__setitem__("read", calls["read"] + 1)
                                           or (key, "hdr7"))
        W._disk_key_forget = lambda: calls.__setitem__("forget", calls["forget"] + 1)
        W.key_win.verify_key = (lambda k, g, auth=None, aad_kind="hdr19":
                                k == key and aad_kind == "hdr7")

        cap = object.__new__(W._WinCapture)
        cap._leg = _Leg()
        cap._cache = W.key_win.SessionCache()
        hit = W._WinCapture._from_cache(cap, object(), groups, flows)
        check("盘上有钥匙就命中（不再被 peek() 早退挡住）", hit is not None)
        check("确实读了一次磁盘", calls["read"] == 1, str(calls))
        check("按落盘的 AAD 形态取布局", getattr(hit, "layout", None) == "hdr",
              getattr(hit, "layout", None))
        check("命中后写进会话缓存", cap._cache.peek() == key)

        # 盘上的钥匙打不开当前流量 → 当没命中，并丢掉这条缓存
        W.key_win.verify_key = lambda k, g, auth=None, aad_kind="hdr19": False
        cap2 = object.__new__(W._WinCapture)
        cap2._leg = _Leg()
        cap2._cache = W.key_win.SessionCache()
        check("验不过的磁盘缓存不当命中",
              W._WinCapture._from_cache(cap2, object(), groups, flows) is None)
        check("验不过就丢掉磁盘缓存", calls["forget"] == 1, str(calls))
    finally:
        (W._disk_key_read, W._disk_key_forget,
         W.key_win.verify_key, W.gcm_win.GcmAuth) = saved


def main() -> int:
    t_layout_keeps_disk_number()
    t_resolve_single_extent()
    t_resolve_crosses_extents()
    t_layout_validates()
    t_open_volume_uses_extent_disk()
    t_open_volume_propagates_error()
    t_open_volume_rejects_multi_extent()
    t_phys_tasks_absolute()
    t_phys_tasks_splits()
    t_phys_tasks_rejects_drops()
    t_legacy_layout_covers_spans()
    t_callsites_use_open_volume()
    t_worker_records_unread()
    t_samples_group_by_header()
    t_from_cache_reads_disk()

    if os.name != "nt":                      # 纯逻辑，Linux 上也应该能跑
        print("（非 Windows：只跑了映射逻辑，未触碰任何设备）")
    if FAILS:
        print(f"\nFAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("\nALL PASSED（pagefile 卷→物理盘映射离线验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
