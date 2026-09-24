#!/usr/bin/env python
"""osc_listen.py —— 本地 OSC 监听器：验证 chatbox 报文格式（类型标签必须是 ,sTT）。

用途：VRChat 没开时，用它顶替 127.0.0.1:9000 的接收端，
     确认「bool 走类型标签 T 而不是 int32 payload」这条硬约束真的被满足。

用法：python scripts/osc_listen.py [--port 9000] [--seconds 60]
"""
from __future__ import annotations

import argparse
import socket
import struct
import time


def parse_osc(data: bytes) -> tuple[str, list[str], list[object]]:
    """极简 OSC 解析，只够用来核对 typetags 与字符串内容。"""
    # address
    end = data.index(b"\x00")
    address = data[:end].decode("utf-8", "replace")
    i = (end + 4) & ~3
    # typetags
    end = data.index(b"\x00", i)
    tags = data[i:end].decode("ascii", "replace")
    i = (end + 4) & ~3
    args: list[object] = []
    for t in tags[1:]:                      # 跳过开头的 ','
        if t == "s":
            end = data.index(b"\x00", i)
            args.append(data[i:end].decode("utf-8", "replace"))
            i = (end + 4) & ~3
        elif t == "i":
            args.append(struct.unpack(">i", data[i:i + 4])[0])
            i += 4
        elif t in ("T", "F"):
            args.append(t == "T")           # 类型标签自带值，无 payload
        elif t == "f":
            args.append(struct.unpack(">f", data[i:i + 4])[0])
            i += 4
        else:
            args.append(f"<{t}>")
            break
    return address, list(tags), args


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--seconds", type=float, default=60.0)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.settimeout(1.0)
    print(f"[osc] 监听 {args.host}:{args.port}（{args.seconds:.0f}s）…")
    end = time.time() + args.seconds
    n = 0
    while time.time() < end:
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except KeyboardInterrupt:
            break
        n += 1
        try:
            address, tags, parsed = parse_osc(data)
        except Exception as exc:  # noqa: BLE001
            print(f"[osc] #{n} 解析失败 {type(exc).__name__}: {exc} | raw={data[:80]!r}")
            continue
        typetag = "".join(tags).replace(",", ",")
        flag = "✅" if typetag == ",sTT" else "⚠️"
        print(f"[osc] #{n} {len(data)}B  address={address}  typetags={typetag} {flag}  args={parsed}")
    print(f"[osc] 共收到 {n} 条")


if __name__ == "__main__":
    main()
