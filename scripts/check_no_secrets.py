#!/usr/bin/env python3
"""提交前扫描暂存区，发现凭据就拦下来。

用法（两处都装，双保险）：
  1) .git/hooks/pre-commit  ← git 提交时自动跑（install_secret_guard.bat 装）
  2) 手动: python scripts/check_no_secrets.py --staged

为什么需要：API key 一旦进了 git 历史，就算下一个 commit 删掉，历史里还在，
必须 rewrite 历史才能清干净。宁可在提交前拦。
"""
from __future__ import annotations

import subprocess
import sys
import re

PATTERNS = {
    "阿里云/百炼 API key": re.compile(r"sk-(?:ws-)?[A-Za-z0-9]{16,}"),
    "GitHub token":        re.compile(r"(?:gho_|ghp_|ghs_|ghu_|github_pat_)[A-Za-z0-9_]{20,}"),
    "AWS access key":      re.compile(r"AKIA[0-9A-Z]{16}"),
    "Bearer 令牌":         re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    "硬编码密钥赋值":      re.compile(
        r"(?i)(api[_-]?key|apikey|secret|passwd|password|access[_-]?token)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']"),
}

# 允许出现的白名单（占位符/变量名，不是真 key）
ALLOW = re.compile(r"sk-你的key|sk-xxx|YOUR_KEY|<key>|DASHSCOPE_API_KEY")


def staged_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         capture_output=True, text=True, encoding="utf-8").stdout
    return [f for f in out.splitlines() if f.strip()]


def main() -> int:
    once = "--once" in sys.argv          # 扫整个工作区（含未提交改动）
    if once:
        files = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                               encoding="utf-8").stdout.splitlines()
    else:
        files = staged_files()
    if not files:
        return 0

    bad = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                raw = fh.read()
        except (OSError, IsADirectoryError):
            continue
        if b"\x00" in raw[:4096]:        # 二进制（pcm/png）跳过
            continue
        text = raw.decode("utf-8", "replace")
        for name, pat in PATTERNS.items():
            for m in pat.finditer(text):
                if ALLOW.search(m.group()):
                    continue
                bad.append((f, name, m.group()[:40]))

    if bad:
        print("\n" + "=" * 62)
        print("❌ 提交被拦下：检测到疑似凭据")
        print("=" * 62)
        for f, name, tok in bad:
            print(f"  [{name}] {f}")
            print(f"      命中片段: {tok}")
        print("\n处理方式：")
        print("  1) 把 key 移出文件（改成读环境变量 DASHSCOPE_API_KEY）")
        print("  2) 确认是误报后：git commit --no-verify")
        print("=" * 62 + "\n")
        return 1
    print(f"✅ 凭据扫描通过（检查了 {len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
