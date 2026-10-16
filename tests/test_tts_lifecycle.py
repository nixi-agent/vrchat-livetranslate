"""离线验证响应未读完时不复用连接，以及 fallback 保留调用设置。"""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
import http.client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt import tts  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        self.do_POST()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.requests.append(self)
        if self.path in ("/drop", "/bad-status", "/long-header") or (self.path == "/drop-once" and len(self.requests) == 1):
            if self.path == "/bad-status":
                self.wfile.write(b"not HTTP\r\n\r\n")
            elif self.path == "/long-header":
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Large: " + b"a" * 65537 + b"\r\nContent-Length: 0\r\n\r\n")
            self.close_connection = True
            return
        if self.path == "/stream":
            body = b"data: " + json.dumps({"output": {"audio": {"data": base64.b64encode(bytes(32)).decode()}}}).encode() + b"\n\nremaining\n"
        elif self.path == "/omni":
            obj = {"choices": [{"delta": {"audio": {"data": base64.b64encode(bytes(32)).decode()}}}]}
            body = b"data: " + json.dumps(obj).encode() + b"\n\ndata: [DONE]\n\nremaining\n"
        else:
            body = b"error detail" if self.path == "/error" else b"response body"
        if self.path == "/conflict":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"abcdef")
            self.close_connection = True
            return
        self.send_response(400 if self.path == "/error" else 200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding" if self.path == "/chunked" else "Content-Length",
                         "chunked" if self.path == "/chunked" else str(len(body)))
        self.end_headers()
        self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
                         if self.path == "/chunked" else body)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        Handler.requests.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        tts._close_pooled()
        self.server.shutdown()
        self.server.server_close()

    def request(self, path):
        return tts._open_response(Request(self.url + path, data=b"{}"), 2, reuse_conn=True)

    def assert_next_request_is_clean(self):
        log = io.StringIO()
        with redirect_stdout(log), self.request("/ok") as response:
            self.assertEqual(response.read(), b"response body")
        self.assertEqual(len(Handler.requests), 2, "不能重复发送第二次 POST")
        self.assertIsNot(Handler.requests[0], Handler.requests[1], "未读完的响应必须丢弃连接")
        self.assertEqual(log.getvalue(), "", "正常下一次请求不应发生重试/降级")

    def test_unread_error_does_not_poison_next_post(self):
        try:
            self.request("/error")
        except HTTPError as error:
            self.assert_next_request_is_clean()
            self.assertEqual(error.read(), b"error detail", "下一请求不能破坏错误详情")
            error.close()
        else:
            self.fail("应抛出 HTTPError")

    def test_early_exit_does_not_reuse_unread_body(self):
        with self.request("/early") as response:
            self.assertEqual(response.read(1), b"r")
        self.assert_next_request_is_clean()

    def test_conflicting_lengths_never_retry_or_fallback(self):
        with self.assertRaises(tts.TtsError):
            self.request("/conflict")
        self.assertEqual(len(Handler.requests), 1, "已收到响应的 POST 不能自动重送")
        self.assert_next_request_is_clean()

    def test_post_response_failures_never_retry_or_fallback(self):
        for path in ("/drop", "/bad-status", "/long-header"):
            with self.subTest(path=path):
                Handler.requests.clear()
                with self.assertRaises(Exception) as error:
                    self.request(path)
                self.assertEqual(len(Handler.requests), 1, "结果不明的 POST 不能重送或转直连")
                self.assertIsInstance(error.exception, tts.TtsError)
                self.assertIn("重送", str(error.exception))
                self.assert_next_request_is_clean()

    def test_connect_failure_retries_before_post_is_sent(self):
        original = http.client.HTTPConnection.connect
        for failures in (1, 2):
            with self.subTest(failures=failures):
                tts._close_pooled()
                Handler.requests.clear()
                attempts = []
                def connect(conn):
                    attempts.append(conn)
                    if len(attempts) <= failures:
                        raise ConnectionRefusedError("before send")
                    original(conn)
                with patch.object(http.client.HTTPConnection, "connect", connect):
                    with self.request("/ok") as response:
                        self.assertEqual(response.read(), b"response body")
                self.assertEqual(len(attempts), failures + 1)
                self.assertEqual(len(Handler.requests), 1)

    def test_idempotent_get_can_retry(self):
        with tts._open_response(Request(self.url + "/drop-once"), 2, reuse_conn=True) as response:
            self.assertEqual(response.read(), b"response body")
        self.assertEqual(len(Handler.requests), 2)
        self.assertIsNot(Handler.requests[0], Handler.requests[1])

    def test_exception_does_not_return_connection(self):
        with self.assertRaises(ValueError):
            with self.request("/early") as response:
                response.read(1)
                raise ValueError("cancel")
        self.assert_next_request_is_clean()

    def test_omni_done_does_not_reuse_unread_tail(self):
        self.assertEqual(tts.synthesize_omni("测试", api_key="offline", endpoint=self.url + "/omni"), bytes(32))
        self.assert_next_request_is_clean()

    def test_stream_generator_cancellation_discards_connection(self):
        stream = tts.synthesize_stream("测试", api_key="offline", endpoint=self.url + "/stream")
        self.assertEqual(next(stream), bytes(32))
        stream.close()
        self.assert_next_request_is_clean()

    def test_complete_chunked_body_reuses_socket(self):
        for _ in range(2):
            with self.request("/chunked") as response:
                self.assertEqual(response.read(), b"response body")
        self.assertEqual(len(Handler.requests), 2)
        self.assertIs(Handler.requests[0], Handler.requests[1])


class FallbackTests(unittest.TestCase):
    def test_fallback_preserves_disabled_reuse(self):
        for http_error in (True, False):
            with self.subTest(http_error=http_error):
                response = io.BytesIO(b"data: [DONE]\n\n")
                response.headers = {"Content-Type": "text/event-stream"}
                with patch.object(tts, "_open_response") as opened, patch.object(tts, "synthesize", return_value=b"pcm") as synth:
                    if http_error:
                        opened.side_effect = HTTPError("http://offline", 415, "unsupported", {}, io.BytesIO(b"detail"))
                    else:
                        opened.return_value = response
                    self.assertEqual(list(tts.synthesize_stream("测试", api_key="offline", reuse_conn=False)), [b"pcm"])
                    self.assertIs(synth.call_args.kwargs.get("reuse_conn"), False)


if __name__ == "__main__":
    unittest.main()
