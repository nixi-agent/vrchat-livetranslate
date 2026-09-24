"""日志时间戳验收：每行行首一个 `HH:MM:SS.mmm`。

## 为什么必须加

用户实测日志（`gui_20260924_225601.log`）：同一份文件里有 **4 次会话**，
分别跑 113s / 69s / 81s / ~100 帧 后被服务端 1011 掐断。
但整份日志**一行时间戳都没有** —— 连"跑多久断的""两次事件隔了多久"都只能靠
`[loopback] 采集结束，共 N bytes ≈ Ns` 这种间接信息反推。
排查真实故障时这是硬伤，所以给 `_Tee` 的每一行加戳。

关键细节：**只在行首加**。`print(..., end="")` 这种半行写入不能被戳打断，
否则中间会插进一个时间戳、把内容切碎。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} ")


def _tee() -> tuple[object, io.StringIO]:
    from vlt.crashlog import _Tee

    buf = io.StringIO()
    return _Tee(buf), buf


def test_single_line_gets_stamp() -> None:
    tee, buf = _tee()
    tee.write("hello\n")
    line = buf.getvalue()
    assert STAMP.match(line), f"行首没有时间戳：{line!r}"
    assert line.endswith("hello\n"), f"内容被破坏：{line!r}"
    print(f"  单行加戳 OK：{line.strip()}")


def test_each_line_in_multiline_blob() -> None:
    """一次写入多行（如 print 多行诊断块）→ 每行都要有戳。"""
    tee, buf = _tee()
    tee.write("a\nb\nc\n")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 3, f"行数不对：{lines}"
    bad = [ln for ln in lines if not STAMP.match(ln)]
    assert not bad, f"这些行没有时间戳：{bad}"
    print(f"  多行块逐行加戳 OK（{len(lines)} 行）")


def test_partial_line_not_split() -> None:
    """半行写入不能被戳打断：先 "hel" 再 "lo\\n" → 只能有一个戳。"""
    tee, buf = _tee()
    tee.write("hel")
    tee.write("lo\n")
    out = buf.getvalue()
    assert out.count(":") == 2, f"时间戳出现次数不对（应只 1 个）：{out!r}"
    assert out.endswith("hello\n"), f"半行被切碎：{out!r}"
    assert STAMP.match(out), f"整行格式不对：{out!r}"
    print(f"  半行写入不被打断 OK：{out.strip()}")


def test_consecutive_blank_lines_ok() -> None:
    """空行不加戳，也不影响后续行。"""
    tee, buf = _tee()
    tee.write("x\n\ny\n")
    lines = buf.getvalue().splitlines()
    assert lines[1] == "", f"空行应保持为空：{lines!r}"
    assert STAMP.match(lines[0]) and STAMP.match(lines[2]), f"相邻行丢戳：{lines!r}"
    print("  空行处理 OK")


def test_diagnostic_block_shape() -> None:
    """模拟真实诊断块（黑匣子那种多行 print）逐行可读。"""
    tee, buf = _tee()
    tee.write("[diag] 会话存活 113s | 发送输入音频 112.8s\n")
    tee.write("[diag] 断开前最近 12 条译文本：\n")
    for i in range(3):
        tee.write(f"[diag]   t-{i:4.1f}s  文本{i}\n")
    lines = buf.getvalue().splitlines()
    assert all(STAMP.match(ln) for ln in lines), f"诊断块有行丢戳：{lines}"
    print(f"  诊断块逐行可读 OK（{len(lines)} 行）")


if __name__ == "__main__":
    print("test_crashlog_stamp:")
    test_single_line_gets_stamp()
    test_each_line_in_multiline_blob()
    test_partial_line_not_split()
    test_consecutive_blank_lines_ok()
    test_diagnostic_block_shape()
    print("ALL PASSED")
