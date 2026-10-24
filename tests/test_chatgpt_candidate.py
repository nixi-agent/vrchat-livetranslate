"""候選封存只走假的 loopback control；不啟動瀏覽器或服務。"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_candidate import CandidateBridge


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.errors = Mock()
        self.bridge = CandidateBridge(AsyncMock(), self.errors)
        self.sent = asyncio.Event()

        async def send(raw):
            self.control = json.loads(raw)
            self.sent.set()

        self.bridge.socket = Mock(send=send)

    async def test_tail_ack_returns_exact_candidate_without_output_callback(self):
        self.bridge._receive_audio(b'\x01\x00' * 480)
        task = asyncio.create_task(self.bridge.seal())
        await self.sent.wait()
        self.assertFalse(task.done())
        self.bridge._receive_audio(b'\x02\x00' * 288)
        await self.bridge._receive_control({'type': 'sealed', 'seal_id': self.control['seal_id'], 'samples': 768})
        self.assertEqual(await task, b'\x01\x00' * 480 + b'\x02\x00' * 288)
        self.assertIsNone(self.bridge.on_audio)
        self.errors.assert_not_called()

    async def test_cancelled_seal_invalidates_candidate(self):
        self.bridge._receive_audio(bytes(960))
        task = asyncio.create_task(self.bridge.seal())
        await self.sent.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(RuntimeError):
            self.bridge._receive_audio(bytes(576))

    async def test_close_releases_pending_seal_without_pcm(self):
        self.bridge._receive_audio(bytes(960))
        task = asyncio.create_task(self.bridge.seal())
        await self.sent.wait()
        await self.bridge.close()
        with self.assertRaisesRegex(RuntimeError, '^capture-closed$'):
            await task

    async def test_backpressure_error_discards_candidate(self):
        self.bridge._receive_audio(bytes(960))
        self.bridge._capture_error('private-event')
        with self.assertRaisesRegex(RuntimeError, '^capture-transport$'):
            await self.bridge.seal()
        self.errors.assert_called_once_with('capture-transport')

    async def test_repeated_close_cancel_keeps_browser_reap_owned(self):
        cleaning, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock())

        async def wait():
            cleaning.set()
            await release.wait()
            process.returncode = 0
            return 0

        process.wait = wait
        self.bridge.process = process
        closing = asyncio.create_task(self.bridge.close())
        await cleaning.wait()
        try:
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(closing.done(), 'browser process must be reaped before close returns')
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
        self.assertEqual(process.returncode, 0)
        process.terminate.assert_called_once()

    async def test_handler_can_close_without_waiting_on_itself(self):
        self.bridge.server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        joining, release = asyncio.Event(), asyncio.Event()
        gather = asyncio.gather

        async def join(task, **kwargs):
            joining.set()
            await release.wait()

        with patch('vlt.session.chatgpt_browser.asyncio.gather', side_effect=join):
            handler = asyncio.create_task(self.bridge.close())
            self.bridge.handler = handler
            try:
                done, _ = await asyncio.wait([handler], timeout=.1)
                self.assertFalse(joining.is_set(), 'close cannot join its own calling handler')
                self.assertIn(handler, done)
                self.bridge.server.wait_closed.assert_not_awaited()
            finally:
                release.set()
                await gather(handler, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
