"""纯内存 HTTP framing 回归：关闭不代表完整响应，合法响应仍须复用。"""
import http.client
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt import tts_transport as transport  # noqa: E402


class Socket:
    def __init__(self, data):
        self.data = data

    def makefile(self, *args):
        return io.BufferedReader(io.BytesIO(self.data))


class FramingTests(unittest.TestCase):
    def run_response(self, data, consume):
        conn, _, key = transport._open_conn('http://offline', 1)
        response = conn.response_class(Socket(data), method='POST')
        response.begin()
        with patch.object(transport, '_release_conn') as release:
            try:
                with transport._Response(response, conn, key) as body:
                    consume(body)
            except http.client.HTTPException:
                pass
            finally:
                conn.close()
        self.assertEqual(release.call_count, 1)
        return release.call_args.kwargs['reusable']

    def test_manual_close_discards_both_framings(self):
        for headers, payload in ((b'Content-Length: 6', b'abcdef'),
                                 (b'Transfer-Encoding: chunked', b'6\r\nabcdef\r\n0\r\n\r\n')):
            with self.subTest(headers=headers):
                def close_early(body):
                    self.assertEqual(body.read(1), b'a')
                    body.close()
                self.assertFalse(self.run_response(b'HTTP/1.1 200 OK\r\n' + headers + b'\r\n\r\n' + payload, close_early))

    def test_truncated_length_iteration_discards(self):
        self.assertFalse(self.run_response(b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nabc', list))

    def test_ambiguous_framing_rejected_before_body(self):
        for headers in (b'Content-Length: 0\r\nContent-Length: 6',
                        b'Content-Length: 0, 6', b'Content-Length: +6',
                        b'Content-Length: -6', b'Content-Length: six',
                        b'Content-Length: 6\r\nTransfer-Encoding: chunked',
                        b'Transfer-Encoding: chunked\r\nTransfer-Encoding: gzip'):
            with self.subTest(headers=headers):
                conn, _, _ = transport._open_conn('http://offline', 1)
                response = conn.response_class(Socket(b'HTTP/1.1 200 OK\r\n' + headers + b'\r\n\r\nabcdef'), method='POST')
                try:
                    with self.assertRaises(http.client.HTTPException):
                        response.begin()
                    self.assertTrue(response.isclosed())
                finally:
                    response.close()
                    conn.close()

    def test_invalid_chunk_framing_discards(self):
        for payload in (b'6\r\nabcdef\r\n0\r\n', b'6\r\nabcdefXX0\r\n\r\n',
                        b'6\nabcdef\r\n0\r\n\r\n', b'6\r\nabc',
                        b'6\r\nabcdef\r\n0\r\nTrailer: x\r\n'):
            with self.subTest(payload=payload):
                self.assertFalse(self.run_response(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + payload, list))

    def test_complete_responses_remain_reusable(self):
        for headers, payload in ((b'Content-Length: 6', b'abcdef'),
                                 (b'Content-Length: 6\r\nContent-Length: 06', b'abcdef'),
                                 (b'Content-Length: 6, 6', b'abcdef'),
                                 (b'Transfer-Encoding: chunked', b'6\r\nabcdef\r\n0\r\n\r\n'),
                                 (b'Transfer-Encoding: Chunked \t', b'6\r\nabcdef\r\n0\r\n\r\n'),
                                 (b'Transfer-Encoding: chunked', b'3;foo=bar\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n'),
                                 (b'Transfer-Encoding: chunked', b'3 ;foo=bar\r\nabc\r\n3\t;foo=bar\r\ndef\r\n0 ;done=yes\r\n\r\n'),
                                 (b'Transfer-Encoding: chunked', b'6\r\nabcdef\r\n0\r\nTrailer: x\r\n\r\n')):
            with self.subTest(headers=headers, payload=payload):
                def read_all(body):
                    self.assertEqual(body.read(), b'abcdef')
                self.assertTrue(self.run_response(b'HTTP/1.1 200 OK\r\n' + headers + b'\r\n\r\n' + payload, read_all))


if __name__ == '__main__':
    unittest.main()
