# -*- coding: utf-8 -*-
"""跑一段一次性 Node 脚本的共享助手（房间服务端用例用）。

## 为什么需要它

`test_room_server.py` / `test_room_worker_auth.py` 都要起一个 node 子进程，把真正的
`server/src/*.js`（Cloudflare Worker / Durable Object）当被测对象跑。脚本本身在本机与
CI 都只跑几十毫秒，但 **Windows 共享 runner 上 node.EXE 冷启动**（Defender 扫新进程、
磁盘慢、机器负载）偶尔会拖到 10s 以上 —— 曾把一次 PR 的 `windows-latest` 测试假红
（`subprocess.TimeoutExpired`，脚本里的断言一条都没跑到，纯墙钟超时）。

所以把两件事固定在这里，两个用例共用同一份口径：

- **超时给宽**（`NODE_TIMEOUT_S`）：脚本正常几十毫秒，宽超时只影响"真卡了"的报错延迟；
- **超时后重试一次**：把"一次性环境抖动"和"脚本真的卡死"分开 —— 重试仍超时才判失败，
  真坏了照样红，只是慢一点。脚本无副作用（用假 state / socket），重跑安全。

## 用法

    from _node_script import run_node
    result = run_node(script, uri)          # uri = Path(...).as_uri()
    assert result.returncode == 0, result.stderr
"""
from __future__ import annotations

import json
import shutil
import subprocess

#: node 子进程超时（秒）。脚本正常只跑几十毫秒；10s 在 Windows runner 上实测太紧。
NODE_TIMEOUT_S = 60
#: 总尝试次数：首次 + 超时后重试一次。
_ATTEMPTS = 2


def run_node(script: str, uri: str) -> subprocess.CompletedProcess:
    """在 node 里跑 *script*，`process.argv[1]` = *uri* 的 JSON；超时则重试一次。

    返回最后一次的 `CompletedProcess`；连最后一次都超时则抛该 `TimeoutExpired`（由调用方
    断言/红掉）。node 不在 PATH 时由调用方用 `unittest.skipUnless(shutil.which('node'), ...)`
    兜住（这里不重复判断，免得两处口径漂移）。
    """
    last_exc: subprocess.TimeoutExpired | None = None
    for _ in range(_ATTEMPTS):
        try:
            return subprocess.run(
                [shutil.which("node"), "--input-type=module", "-e", script, json.dumps(uri)],
                text=True, capture_output=True, timeout=NODE_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc
