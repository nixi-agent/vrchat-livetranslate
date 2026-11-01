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
    samples = {(b"\x01\x02", b"\x01\x02\x03\x04"): [pkt, pkt]}

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

    if os.name != "nt":                      # 纯逻辑，Linux 上也应该能跑
        print("（非 Windows：只跑了映射逻辑，未触碰任何设备）")
    if FAILS:
        print(f"\nFAILED: {len(FAILS)} 项 → {FAILS}")
        return 1
    print("\nALL PASSED（pagefile 卷→物理盘映射离线验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
