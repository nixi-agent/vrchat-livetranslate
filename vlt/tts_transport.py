"""TTS 直连 HTTP 连接池；只有成功消费完整响应才能复用。"""
from __future__ import annotations

import atexit
import http.client
import io
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

_POOL: dict[tuple[str, str, int], tuple[object, float]] = {}
_POOL_LOCK = threading.Lock()
_POOL_IDLE_MAX_S = 20.0


class _FramingError(http.client.HTTPException):
    """响应已经到达但边界无效；不得重送请求。"""


class _PostOutcomeUnknown(http.client.HTTPException):
    """传送已开始，不能假定服务没有处理请求。"""


class _HTTPResponse(http.client.HTTPResponse):
    explicitly_closed = False
    chunk_complete = False

    def begin(self):
        super().begin()
        lengths = [value.strip(' \t') for header in self.headers.get_all('Content-Length', [])
                   for value in header.split(',')]
        transfers = self.headers.get_all('Transfer-Encoding', [])
        has_body = self.status not in (204, 304) and not 100 <= self.status < 200 and self._method != 'HEAD'
        try:
            if transfers and (lengths or len(transfers) != 1 or transfers[0].strip(' \t').lower() != 'chunked'):
                raise _FramingError('Ambiguous or unsupported Transfer-Encoding')
            if transfers and has_body:
                self.chunked = True
                self.chunk_left = None
                self.length = None
                self.will_close = self._check_close()
            if lengths:
                if any(not value.isascii() or not value.isdecimal() for value in lengths):
                    raise _FramingError('Invalid Content-Length')
                normalized = {value.lstrip('0') or '0' for value in lengths}
                if len(normalized) != 1:
                    raise _FramingError('Conflicting Content-Length')
                try:
                    length = int(normalized.pop())
                except ValueError as exc:
                    raise _FramingError('Unsupported Content-Length') from exc
                if has_body:
                    self.length = length
                    self.will_close = self._check_close()
        except _FramingError:
            self.close()
            raise

    def close(self):
        self.explicitly_closed = True
        super().close()

    def _get_chunk_left(self):
        if self.chunk_left == 0:
            if self._safe_read(2) != b"\r\n":
                raise http.client.IncompleteRead(b"")
            self.chunk_left = None
        return super()._get_chunk_left()

    def _read_next_chunk_size(self):
        line = self.fp.readline(65537)
        if len(line) > 65536:
            raise http.client.LineTooLong("chunk size")
        size, separator, _ = line[:-2].partition(b";")
        if separator:
            size = size.rstrip(b" \t")
        if not line.endswith(b"\r\n") or not size or any(c not in b"0123456789abcdefABCDEF" for c in size):
            raise http.client.IncompleteRead(b"")
        return int(size, 16)

    def _read_and_discard_trailer(self):
        # stdlib tolerates missing final CRLF; that is unsafe for returning a connection to the pool.
        total = 0
        while True:
            line = self.fp.readline(65537)
            total += len(line)
            if total > 65536:
                raise http.client.LineTooLong("trailer")
            if not line.endswith(b"\r\n"):
                raise http.client.IncompleteRead(b"")
            if line == b"\r\n":
                self.chunk_complete = True
                return


def _pool_key(url: str) -> tuple[tuple[str, str, int], str]:
    """把 URL 拆成 (池键=(scheme,host,port), 请求路径)。纯函数，离线可测。"""
    u = urlsplit(url)
    scheme = (u.scheme or "https").lower()
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return (scheme, host, port), path



def _close_quiet(conn: object) -> None:
    try:
        conn.close()                                       # type: ignore[attr-defined]
    except Exception:                                      # noqa: BLE001
        pass



def _open_conn(url: str, timeout: float):
    """取一条连接：优先复用池里那条还新鲜的，否则新建。返回 (conn, path, key)。"""
    key, path = _pool_key(url)
    with _POOL_LOCK:
        held = _POOL.pop(key, None)
    if held is not None:
        conn, used_at = held
        if time.monotonic() - used_at <= _POOL_IDLE_MAX_S:
            return conn, path, key
        _close_quiet(conn)
    scheme, host, port = key
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.response_class = _HTTPResponse
    return conn, path, key



def _release_conn(key: tuple[str, str, int], conn: object, *, reusable: bool) -> None:
    """把连接放回池（只留一条/主机）；不可复用就关掉。"""
    if not reusable:
        _close_quiet(conn)
        return
    with _POOL_LOCK:
        old = _POOL.pop(key, None)
        _POOL[key] = (conn, time.monotonic())
    if old is not None:
        _close_quiet(old[0])



def _post_pooled(req, timeout: float, _note):
    """用池化连接发一个 urllib Request（GET/POST 都行），返回 (resp, conn, key)。

    连接建立失败或幂等读取异常 → 丢连接、重连一次并留痕；
    POST 传送已开始后结果不明 → 丢连接并报错，不重送；
    ≥400 是服务端的明确答复、**不当成连接问题**：转成 urllib 的 `HTTPError` 抛出去 ——
    调用方本来就是 `except HTTPError` + `exc.read()` 取详情，这里必须保持同一口径。
    """
    url = req.full_url
    method = req.get_method()
    body = req.data
    headers = {k: v for k, v in req.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
    last: Exception | None = None
    for attempt in (1, 2):
        conn, path, key = _open_conn(url, timeout)
        sending = False
        try:
            if conn.sock is None:
                conn.connect()
            sending = True
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            if resp.status >= 400:
                try:
                    detail = resp.read()
                except (http.client.HTTPException, OSError):
                    detail = b""
                    _note("错误响应读取失败，丢弃连接")
                finally:
                    _close_quiet(resp)
                    _release_conn(key, conn, reusable=False)
                raise HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(detail))
            return resp, conn, key
        except HTTPError:
            raise
        except _FramingError:
            _release_conn(key, conn, reusable=False)
            raise
        except (http.client.HTTPException, OSError) as exc:
            last = exc
            _release_conn(key, conn, reusable=False)
            if sending and method not in ("GET", "HEAD"):
                raise _PostOutcomeUnknown(f"POST 结果不明，未自动重送（{type(exc).__name__}）") from exc
            if attempt == 1:
                _note(f"复用连接失效（{type(exc).__name__}: {exc}）→ 重连一次")
                continue
            raise
    raise last if last else OSError("连接失败")



class _Response:
    """`with _open_response(...) as r` 的包装：用完把连接放回池。

    ⚠️ 刻意**不用 generator**（`@contextlib.contextmanager`）：实测过 generator 版本会把
    进程收尾的时序搅乱 —— 在跑 Tk 的用例（`tests/test_textin.py`）里表现为退出码非 0，
    而所有断言其实全过（`Tcl_AsyncDelete: async handler deleted by the wrong thread`）。
    写成普通对象后该用例恢复全绿（同机 6/6）。
    """

    __slots__ = ("_resp", "_conn", "_key", "_pooled")

    def __init__(self, resp, conn=None, key=None) -> None:   # noqa: ANN001
        self._resp = resp
        self._conn = conn
        self._key = key
        self._pooled = key is not None

    def __enter__(self):                                     # noqa: ANN204
        return self._resp

    def __exit__(self, *exc) -> bool:                        # noqa: ANN002
        if self._pooled:
            reusable = (exc[0] is None and self._resp.isclosed()
                        and not self._resp.will_close and not self._resp.explicitly_closed
                        and (self._resp.chunk_complete if self._resp.chunked else self._resp.length == 0))
            _close_quiet(self._resp)
            _release_conn(self._key, self._conn, reusable=reusable)
        else:
            try:                                             # 与原来的 `with opener.open()` 一致
                self._resp.close()
            except Exception:                                # noqa: BLE001
                pass
        return False



def _close_pooled() -> None:
    """进程退出时把池里的连接关掉。

    不关的后果实测过：连接会一直留到解释器收尾阶段，把收尾顺序搅乱 ——
    在跑 Tk 的用例里表现为 `Tcl_AsyncDelete: async handler deleted by the wrong thread`
    （退出码非 0，而所有断言其实全过）。退出前主动关掉，顺序就确定了。
    """
    with _POOL_LOCK:
        held = list(_POOL.values())
        _POOL.clear()
    for conn, _ in held:
        _close_quiet(conn)



atexit.register(_close_pooled)
