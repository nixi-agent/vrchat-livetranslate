#!/usr/bin/env python3
"""真机验收：**客户端调用链**能不能从主源（Cloudflare R2）拿到并校验更新。

与 `scripts/publish_r2.py verify` 的分工（两者都要跑，视角不同）：

  * `publish_r2.py verify` = **发布侧**视角 —— 我传上去的东西，公开域名上是不是那些字节；
  * 本脚本 = **客户端**视角 —— 走 `vlt/update_check` 的真实入口
    （`check_for_updates` → `fetch_latest_release` → `download_and_verify`），拿真实的「当前版本」
    端到端跑一遍；下载物落在临时沙箱里，**不碰任何安装目录**。

用法：

    .venv\\Scripts\\python.exe scripts/verify/verify_update_source.py --local-version 0.11.1
    .venv\\Scripts\\python.exe scripts/verify/verify_update_source.py --local-version 0.11.1 --all

判据（缺一不可）：

  1. 日志里出现 `[update] 更新源：Cloudflare`，即这次真的走主源、没有回落
     （评审 §6 之前这里只打印不断言，主源全挂而 GitHub 可用时脚本照样 exit 0 = 假绿）；
  2. 拿到的版本 == 公开域清单里的版本（给了 `--expect` 就对，不给只打印）；
  3. 真下载 + sha256 校验通过，并写出 `update_pending.json`（GUI「立即更新」走的就是这条路）；
  4. 本机形态要的是正确的附件（Windows → exe；Linux → AppImage）；`--all` 时两份都要过。

判据 1 只有一种豁免：`--allow-fallback`（明确接受「主源这次不可用、护栏按预期回落了」）。

另外会**报告**（不算失败）兜底源 GitHub 现在通不通 —— 大陆出口常被限流/墙，这条信息对
「用户到底能不能收到更新」很关键。

退出码：0 = 全部通过；1 = 有判据不成立；2 = 主源都拿不到（网络或域名问题）。
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # 不写死本机路径
sys.path.insert(0, str(ROOT))

from vlt import update_check as uc                  # noqa: E402

PLATFORM_ASSETS = (("windows", uc.EXE_ASSET_NAME), ("linux", uc.APPIMAGE_ASSET_NAME))


def main() -> int:
    ap = argparse.ArgumentParser(description="客户端视角的更新链路真机验收")
    ap.add_argument("--local-version", default="0.0.1",
                    help="假装自己正在跑这个版本（要低于线上版本才有「发现新版本」）")
    ap.add_argument("--expect", default="", help="期望的最新版本（给了就断言一致）")
    ap.add_argument("--all", action="store_true", help="两个平台的产物都下载校验")
    ap.add_argument("--allow-fallback", action="store_true",
                    help="主源本次不可用、护栏按预期回落时不算失败（判据 1 的唯一豁免）")
    ap.add_argument("--keep", action="store_true", help="保留下载物（默认复核完删掉，省磁盘）")
    args = ap.parse_args()

    sandbox = Path(tempfile.mkdtemp(prefix="vlt_update_source_"))
    print(f"[验收] 更新链路（客户端视角）  沙箱={sandbox}")
    print(f"[验收] 本机形态：{uc.update_mode()}；假装当前版本 v{args.local_version}")

    # ① 真跑 GUI 用的那个入口（真网络：主源 → 必要时兜底），并把判据 ① 落到实处：
    #    「走的是不是主源」必须**断言**，不能只看打印 —— 否则主源全挂时脚本照样绿。
    ok = True
    buf = io.StringIO()
    with redirect_stdout(buf):
        status, info = uc.check_for_updates(args.local_version, sandbox / "config.yaml")
    log = buf.getvalue()
    print(log, end="")                       # 日志照旧给用户看
    if f"更新源：Cloudflare（{uc.MANIFEST_HOST}）" in log:
        print(f"[验收] ✅ 判据 1：本次检查走的是主源（{uc.MANIFEST_HOST}）")
    elif args.allow_fallback:
        print("[验收] ⚠️ 判据 1 放宽（--allow-fallback）：本次没走主源/发生了回落")
    else:
        print(f"[验收] ❌ 判据 1：本次检查**没有走主源**（日志里没有「更新源：Cloudflare"
              f"（{uc.MANIFEST_HOST}）」那一行）；确实主源不可用就加 --allow-fallback")
        ok = False
    if info is None:
        print(f"[验收] ❌ 检查更新失败（status={status}）：连清单都拿不到，先看网络/域名")
        print(f"[验收]    主源地址：{uc.MANIFEST_URL}")
        return 2
    print(f"[验收] 检查结论：{status}，线上版本 v{info.version}（取自 {info.asset_url}）")
    if args.expect and info.version != args.expect:
        print(f"[验收] ❌ 线上版本 v{info.version} != 期望 v{args.expect}")
        ok = False

    # ② 各平台形态：真下载 + sha256 校验 + pending 落盘
    targets = PLATFORM_ASSETS if args.all else ((uc.update_mode(), uc.asset_name_for()),)
    for platform, asset_name in targets:
        try:
            got = uc.fetch_latest_release(asset_name=asset_name)
        except Exception as exc:  # noqa: BLE001 — 报清楚是哪一步挂的，别抛栈
            print(f"[验收] ❌ {platform}：取 {asset_name} 的更新信息失败：{type(exc).__name__}: {exc}")
            ok = False
            continue
        try:
            path = uc.download_and_verify(got, sandbox)
        except Exception as exc:  # noqa: BLE001
            print(f"[验收] ❌ {platform}：下载/校验失败：{type(exc).__name__}: {exc}")
            ok = False
            continue
        digest = uc.file_sha256(path)
        pending = json.loads((sandbox / uc.PENDING_JSON).read_text(encoding="utf-8"))
        good = (path.name == asset_name + uc.NEW_SUFFIX
                and digest == got.asset_digest
                and pending == {"version": got.version, "sha256": digest})
        print(f"[验收] {'✅' if good else '❌'} {platform}：{asset_name} 下载 {path.stat().st_size:,} 字节，"
              f"sha256 校验通过，pending 已落盘（v{pending.get('version')}）")
        ok = ok and good
        if not args.keep:
            path.unlink(missing_ok=True)
            (sandbox / uc.PENDING_JSON).unlink(missing_ok=True)

    # ③ 兜底源现状（只看，不影响结论）
    print("[验收] 顺带看一眼兜底源（GitHub）：", end="", flush=True)
    try:
        gh = uc.fetch_latest_release_github()
        print(f"可用，最新 v{gh.version}")
    except Exception as exc:  # noqa: BLE001
        print(f"不可用（{exc}）—— 主源在，用户照常收更新；这也正是主源存在的理由")

    if not args.keep:
        shutil.rmtree(sandbox, ignore_errors=True)
    print("[验收] " + ("✅ 全部通过" if ok else "❌ 有判据不成立"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
