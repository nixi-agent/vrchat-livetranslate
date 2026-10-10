"""本機橋接信任邊界與中斷協商清理，不使用外部帳戶或瀏覽器。"""
import asyncio
import json
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_browser import BrowserBridge, browser_command


class BrowserDiscoveryTests(unittest.TestCase):
    def test_linux_stable_browser_names(self):
        for name in ('google-chrome-stable', 'microsoft-edge-stable'):
            with self.subTest(browser=name), patch('shutil.which', side_effect=lambda candidate: '/usr/bin/' + name if candidate == name else None):
                self.assertEqual(browser_command(), '/usr/bin/' + name)

    def test_missing_windows_environment_never_searches_current_directory(self):
        with patch('shutil.which', return_value=None), patch.dict('os.environ', {}, clear=True), \
             patch('pathlib.Path.is_file', return_value=True) as probe:
            with self.assertRaises(RuntimeError):
                browser_command()
        probe.assert_not_called()


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.audio, self.errors = [], []
        self.bridge = BrowserBridge(AsyncMock(return_value='v=0\r\nanswer'), self.audio.append, self.errors.append)
        self.bridge.server = await serve(self.bridge._handle, '127.0.0.1', 0,
                                         process_request=self.bridge._http, close_timeout=0.1)
        port = self.bridge.server.sockets[0].getsockname()[1]
        self.bridge.origin = f'http://127.0.0.1:{port}'
        self.url = f'ws://127.0.0.1:{port}/{self.bridge.token}/ws'

    async def asyncTearDown(self):
        await self.bridge.close()

    async def test_origin_and_secret_path_are_required(self):
        for url, origin, status in ((self.url, 'https://evil.example', 403),
                                    (self.url, None, 403),
                                    (self.url.replace(self.bridge.token, 'wrong'), self.bridge.origin, 404)):
            with self.assertRaises(InvalidStatus) as caught:
                async with connect(url, origin=origin):
                    self.fail('untrusted socket admitted')
            self.assertEqual(caught.exception.response.status_code, status)
        reader, writer = await asyncio.open_connection('127.0.0.1', self.bridge.server.sockets[0].getsockname()[1])
        writer.write(f'GET /{self.bridge.token} HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n'.encode())
        await writer.drain()
        self.assertIn(b'403', await reader.readline())
        writer.close()
        await writer.wait_closed()

    async def test_offer_ready_audio_and_single_owner(self):
        async with connect(self.url, origin=self.bridge.origin) as socket:
            await socket.send(json.dumps({'type': 'offer', 'sdp': 'v=0\r\noffer'}))
            self.assertEqual(json.loads(await socket.recv())['sdp'], 'v=0\r\nanswer')
            await socket.send('{"type":"ready"}')
            await asyncio.wait_for(self.bridge.ready.wait(), 1)
            with self.assertRaises(InvalidStatus):
                async with connect(self.url, origin=self.bridge.origin):
                    self.fail('second owner admitted')
            await self.bridge.send(b'\x01\x00')
            self.assertEqual(await socket.recv(), b'\x01\x00')
            await socket.send(b'\x02\x00')
            await socket.send('{"type":"offer","sdp":"v=0"}')
            await asyncio.wait_for(socket.wait_closed(), 1)
        self.assertEqual(self.audio, [b'\x02\x00'])
        self.bridge.on_offer.assert_awaited_once()
        self.assertTrue(self.errors)

    async def test_invalid_pcm_is_rejected(self):
        async with connect(self.url, origin=self.bridge.origin) as socket:
            await socket.send(b'x')
            await asyncio.wait_for(socket.wait_closed(), 1)
        self.assertFalse(self.audio)
        self.assertEqual(self.errors, ['背景 WebRTC 音訊橋接失敗。'])

    async def test_preroll_waits_for_worklet_consumption_without_dropping_audio(self):
        async with connect(self.url, origin=self.bridge.origin) as socket:
            pcm = b'\x01\x00' * 17600
            sending = asyncio.create_task(self.bridge.send(pcm))
            received = b''.join([await asyncio.wait_for(socket.recv(), 1) for _ in range(10)])
            self.assertEqual(len(received), 32000)
            self.assertFalse(sending.done(), 'The live block must wait for capacity')
            await socket.send('{"type":"consumed","bytes":3200}')
            received += await asyncio.wait_for(socket.recv(), 1)
            await asyncio.wait_for(sending, 1)
            self.assertEqual(received, pcm)
            self.assertEqual(self.bridge._consumed_pcm, 3200)
            self.assertFalse(self.errors)

    async def test_close_releases_blocked_audio_sender(self):
        async with connect(self.url, origin=self.bridge.origin):
            await self.bridge.send(bytes(32000))
            blocked = asyncio.create_task(self.bridge.send(bytes(3200)))
            await asyncio.sleep(.01)
            self.assertFalse(blocked.done())
            await self.bridge.close()
            with self.assertRaises(ConnectionError):
                await asyncio.wait_for(blocked, 1)

    async def test_worklet_overflow_has_a_sanitized_failure_category(self):
        async with connect(self.url, origin=self.bridge.origin) as socket:
            await socket.send('{"type":"error","reason":"input-overflow"}')
            await asyncio.wait_for(socket.wait_closed(), 1)
        self.assertEqual(self.errors, ['背景 WebRTC 音訊橋接失敗（輸入緩衝溢位）。'])

    async def test_arbitrary_browser_error_details_are_not_exposed(self):
        async with connect(self.url, origin=self.bridge.origin) as socket:
            await socket.send('{"type":"error","reason":"private-untrusted-detail"}')
            await asyncio.wait_for(socket.wait_closed(), 1)
        self.assertEqual(self.errors, ['背景 WebRTC 音訊橋接失敗。'])

    async def test_unexpected_disconnect_reports_closed_once(self):
        async with connect(self.url, origin=self.bridge.origin):
            pass
        await asyncio.wait_for(self.bridge.ready.wait(), 1)
        self.assertEqual(self.errors, ['背景 WebRTC 音訊橋接已關閉。'])

    async def test_close_cancels_pending_negotiation(self):
        started = asyncio.Event()

        async def pending(_):
            started.set()
            await asyncio.Event().wait()

        self.bridge.on_offer = pending
        async with connect(self.url, origin=self.bridge.origin) as socket:
            await socket.send('{"type":"offer","sdp":"v=0"}')
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(self.bridge.close(), 1)
        self.assertTrue(self.bridge.handler.done())
        self.assertFalse(self.errors)

    async def test_startup_budget_allows_both_negotiation_phases(self):
        bridge = BrowserBridge(AsyncMock(), Mock(), Mock())
        server = SimpleNamespace(sockets=[SimpleNamespace(getsockname=lambda: ('127.0.0.1', 12345))],
                                 close=Mock(), wait_closed=AsyncMock())
        async def budget(wait, timeout):
            bridge.ready.set()
            await wait
            self.assertGreaterEqual(timeout, 75, '40s request + 30s SDP + browser ICE must fit')
        with patch('websockets.asyncio.server.serve', new=AsyncMock(return_value=server)), \
             patch('asyncio.create_subprocess_exec', new=AsyncMock(return_value=SimpleNamespace(returncode=0))), \
             patch('vlt.session.chatgpt_browser.browser_command', return_value='fake-browser'), \
             patch('asyncio.wait_for', side_effect=budget):
            try:
                await bridge.start('unused-profile')
            finally:
                await bridge.close()


if __name__ == '__main__':
    unittest.main()
