"""TTS 连接复用：离线验证（本地假 TTS 服务器，不花钱、不出网）。

要证三件事：
  ① 开复用时，连续两次合成**只建一条 TCP/HTTP 连接**（这正是每段省一次 DNS+TCP+TLS 的来源）；
  ② 关复用时，每次都是新连接 —— 与改动前的行为**完全一致**；
  ③ POST 已被接收后断线，明确报错、不重送；下一次新请求仍能正常执行。
"""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt import tts                                                # noqa: E402

results: list[tuple[str, bool]] = []
CONNS: list[object] = []       # 保留实例引用，避免已释放 handler 的 id 被复用
DROP_NEXT = [False]            # 让下一条连接在响应前被掐断


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {name}  {'OK' if ok else '✗'}")


PCM = bytes(320)               # 一小段假音频（base64 传到客户端后按 24k 单声道处理）


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # 关键：允许 keep-alive

    def log_message(self, *a):             # 静音
        pass

    def do_POST(self) -> None:             # noqa: N802
        CONNS.append(self)
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if DROP_NEXT[0]:
            DROP_NEXT[0] = False
            self.close_connection = True
            try:
                self.connection.close()          # 真·断连（不是 5xx 答复）—— 模拟 keep-alive 连接失效
            except Exception:                    # noqa: BLE001
                pass
            return
        body = (b"data: " + json.dumps({"output": {"audio": {"data": base64.b64encode(PCM).decode()}}}).encode()
                + b"\n\n" + b"data: [DONE]\n\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def synth(port: int, *, reuse: bool) -> int:
    url = f"http://127.0.0.1:{port}/v1/tts"
    total = 0
    for chunk in tts.synthesize_stream("测试", api_key="sk-test", endpoint=url, reuse_conn=reuse):
        total += len(chunk)
    return total


def main() -> int:
    srv, port = serve()
    try:
        # ① 复用：两次合成 → 1 条连接
        CONNS.clear()
        a = synth(port, reuse=True)
        b = synth(port, reuse=True)
        check("复用时两次合成都有产出", a > 0 and b > 0)
        check("复用时只建了 1 条连接（这就是省下的握手）", len(set(CONNS)) == 1)

        # ② 不复用：两次 → 2 条连接（与改动前一致）
        CONNS.clear()
        synth(port, reuse=False)
        synth(port, reuse=False)
        check("关复用时两次建 2 条连接（与改动前一致）", len(set(CONNS)) == 2)

        # ③ 结果不明：已读完 POST 后断线，不能假定服务没有执行
        CONNS.clear()
        DROP_NEXT[0] = True
        buf = io.StringIO()
        error = ""
        with redirect_stdout(buf):
            try:
                synth(port, reuse=True)
            except tts.TtsError as exc:
                error = str(exc)
            failed_requests = len(CONNS)
            got2 = synth(port, reuse=True)
        check("断线的 POST 只发送一次，后续新请求仍成功", failed_requests == 1 and len(CONNS) == 2 and got2 > 0)
        check("向调用端明确说明结果不明且没有重送", "结果不明" in error and "重送" in error and buf.getvalue() == "")
    finally:
        srv.shutdown()

    bad = [n for n, ok in results if not ok]
    print(f"\n{'ALL PASSED' if not bad else '失败：' + ', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
