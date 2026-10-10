"""用隔離的背景 Chromium 管理 WebRTC；localhost 通道限制來源與隨機路徑。"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
from pathlib import Path

from .chatgpt_browser_page import PAGE


def browser_command():
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
                 'microsoft-edge-stable', 'msedge', 'chrome'):
        executable = shutil.which(name)
        if executable:
            return executable
    if os.name == 'nt':
        for root in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            if not os.environ.get(root):
                continue
            base = Path(os.environ[root])
            for relative in ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'):
                executable = base / relative
                if executable.is_file():
                    return str(executable)
    raise RuntimeError('訂閱語音需要已安裝的 Chrome 或 Edge。')


class BrowserBridge:
    def __init__(self, on_offer, on_audio, on_error):
        self.on_offer, self.on_audio, self.on_error = on_offer, on_audio, on_error
        self.token = secrets.token_urlsafe(32)
        self.server = self.process = self.socket = None
        self.origin = None
        self.ready = asyncio.Event()
        self.closing = False
        self.handler = None
        self._pending_pcm = 0
        self._consumed_pcm = 0
        self._drained = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._send_failed = False

    async def _http(self, connection, request):
        if request.headers.get('Host') != self.origin.removeprefix('http://'):
            return connection.respond(403, 'Forbidden')
        if request.path == '/' + self.token:
            response = connection.respond(200, PAGE)
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'unsafe-inline' blob:; worker-src blob:; connect-src 'self'; media-src blob:"
            return response
        if request.path != '/' + self.token + '/ws' or self.socket is not None:
            return connection.respond(404, 'Not found')
        if request.headers.get('Origin') != self.origin:
            return connection.respond(403, 'Forbidden')
        return None

    async def _handle(self, socket):
        if self.socket is not None:
            await socket.close(code=1008)
            return
        self.socket = socket
        self.handler = asyncio.current_task()
        negotiated = False
        failure = '背景 WebRTC 音訊橋接已關閉。'
        try:
            async for message in socket:
                if isinstance(message, bytes):
                    if len(message) % 2 or len(message) > 48000:
                        raise ValueError('Invalid PCM')
                    self.on_audio(message)
                    continue
                event = json.loads(message)
                if event.get('type') == 'offer':
                    if negotiated or not isinstance(event.get('sdp'), str) or not event['sdp'].startswith('v=0'):
                        raise ValueError('Duplicate offer')
                    negotiated = True
                    answer = await self.on_offer(event['sdp'])
                    await socket.send(json.dumps({'type': 'answer', 'sdp': answer}))
                elif event.get('type') == 'ready':
                    if not negotiated:
                        raise ValueError('Missing offer')
                    self.ready.set()
                elif event.get('type') == 'error':
                    failure = {'input-overflow': '背景 WebRTC 音訊橋接失敗（輸入緩衝溢位）。',
                               'output-backpressure': '背景 WebRTC 音訊橋接失敗（輸出回壓）。'}.get(
                                   event.get('reason'), '背景 WebRTC 音訊橋接失敗。')
                    raise RuntimeError('Chromium audio failed')
                elif event.get('type') == 'consumed':
                    size = event.get('bytes')
                    if type(size) is not int or size <= 0 or size % 2 or size > self._pending_pcm:
                        raise ValueError('Invalid consumption acknowledgment')
                    self._pending_pcm -= size
                    self._consumed_pcm += size
                    self._drained.set()
        except Exception:
            if failure == '背景 WebRTC 音訊橋接已關閉。':
                failure = '背景 WebRTC 音訊橋接失敗。'
        finally:
            self._send_failed = True
            self._drained.set()
            if not self.closing:
                self.on_error(failure)
                self.ready.set()

    async def start(self, profile):
        from websockets.asyncio.server import serve
        self.server = await serve(self._handle, '127.0.0.1', 0, process_request=self._http,
                                  compression=None,
                                  max_size=256000, max_queue=16, close_timeout=2)
        port = self.server.sockets[0].getsockname()[1]
        self.origin = f'http://127.0.0.1:{port}'
        self.process = await asyncio.create_subprocess_exec(
            browser_command(), '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
            '--autoplay-policy=no-user-gesture-required', f'--user-data-dir={profile}',
            self.origin + '/' + self.token, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            **({'creationflags': 0x08000000} if os.name == 'nt' else {}),
        )
        await asyncio.wait_for(self.ready.wait(), 90)  # Covers 40s request + 30s SDP + peer setup.

    async def send(self, pcm):
        if not self.socket or self.closing or self._send_failed:
            raise ConnectionError('背景音訊橋接不可用。')
        if len(pcm) % 2:
            raise ValueError('PCM 必須是完整的 16-bit samples。')
        async with self._send_lock:
            for offset in range(0, len(pcm), 3200):
                block = pcm[offset:offset + 3200]
                while self._pending_pcm + len(block) > 32000:
                    self._drained.clear()
                    try:
                        await asyncio.wait_for(self._drained.wait(), 1.5)
                    except asyncio.TimeoutError:
                        self._send_failed = True
                        self.on_error('背景音訊橋接未消耗輸入音訊。')
                        raise ConnectionError('背景音訊橋接未消耗輸入音訊。') from None
                    if self.closing or self._send_failed:
                        raise ConnectionError('背景音訊橋接不可用。')
                if self.closing or self._send_failed:
                    raise ConnectionError('背景音訊橋接不可用。')
                self._pending_pcm += len(block)
                await self.socket.send(block)

    async def close(self):
        self.closing = True
        self._drained.set()
        if self.socket:
            try:
                await self.socket.send('{"type":"close"}')
            except Exception:
                pass
        if self.server:
            self.server.close()
            if self.handler and self.handler is not asyncio.current_task():
                self.handler.cancel()
                await asyncio.gather(self.handler, return_exceptions=True)
            await self.server.wait_closed()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
