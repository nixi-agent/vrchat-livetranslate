"""Real localhost bridge boundary; no browser process, credentials or service."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vlt.session.chatgpt_browser import BrowserBridge
from vlt.session.chatgpt_reserved_bridge import ReservedBridge


class HeldOfferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls, self.failures, self.tasks, self.ready_waits = [], [], [], []
        self.opened, self.reserved = asyncio.Event(), asyncio.Event()
        self.reserve_release = None
        self.reserve_cancelled = asyncio.Event()
        self.cancel_release = None

        async def reserve(sdp):
            self.calls.append(sdp)
            self.reserved.set()
            if self.reserve_release is not None:
                try:
                    await self.reserve_release.wait()
                except asyncio.CancelledError:
                    self.reserve_cancelled.set()
                    if self.cancel_release is not None:
                        await self.cancel_release.wait()
                    raise
            return 'v=0\r\nreserved-answer'

        self.bridge = ReservedBridge(reserve, self.failures.append, lambda *_: None)
        async def open_local(bridge, profile):
            bridge.server = await serve(bridge._handle, '127.0.0.1', 0,
                process_request=bridge._http, compression=None, close_timeout=.1)
            bridge.origin = 'http://127.0.0.1:'+str(bridge.server.sockets[0].getsockname()[1])
            self.client = await connect(bridge.origin.replace('http:', 'ws:')+'/'+bridge.token+'/ws',
                                        origin=bridge.origin, compression=None, close_timeout=.1)
            await self.client.send('{"type":"prepared"}')
            self.opened.set()
        wait_ready = BrowserBridge._wait_ready
        async def observe_ready(bridge):
            self.ready_waits.append('90s-ready-budget-start')
            return await wait_ready(bridge)
        self.enterContext(patch.object(BrowserBridge, '_open', open_local))
        self.enterContext(patch.object(BrowserBridge, '_wait_ready', observe_ready))
        self.bridge.prepare('offline-profile')
        await asyncio.wait_for(self.opened.wait(), 1)

    async def asyncTearDown(self):
        if self.reserve_release is not None:
            self.reserve_release.set()
        if self.cancel_release is not None:
            self.cancel_release.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.bridge.close()
        await self.client.close()
        self.assertTrue(self.bridge.handler is None or self.bridge.handler.done())

    async def begin(self):
        begin = getattr(self.bridge, 'begin_reservation', None)
        task = asyncio.create_task(begin() if begin else self.bridge.start('offline-profile'))
        self.tasks.append(task)
        activation = json.loads(await asyncio.wait_for(self.client.recv(), 1))
        self.assertEqual(activation['type'], 'activate')
        await self.client.send(json.dumps(dict(type='offer', nonce=activation['nonce'], sdp='v=0\r\noffer')))
        await asyncio.wait_for(self.reserved.wait(), 1)

    async def assert_answer_held(self):
        with self.assertRaises(asyncio.TimeoutError, msg='RTP answer must remain held until approved start'):
            await asyncio.wait_for(self.client.recv(), .04)

    async def test_neutral_offer_overlaps_without_starting_ready_budget_or_rtp(self):
        await self.begin()
        await self.assert_answer_held()
        self.assertEqual(self.ready_waits, [])
        self.assertEqual(self.bridge.capture._pcm, b'')
        async def finalize(sdp):
            return self.bridge.take(sdp)
        self.bridge.on_offer = finalize
        start = asyncio.create_task(self.bridge.start('offline-profile'))
        self.tasks.append(start)
        answer = json.loads(await asyncio.wait_for(self.client.recv(), 1))
        self.assertEqual(answer['type'], 'answer')
        self.assertEqual(answer['sdp'], 'v=0\r\nreserved-answer')
        self.assertRegex(answer['nonce'], '^[0-9a-f]{32}$')
        await self.client.send(json.dumps(dict(type='generation', nonce=answer['nonce'])))
        await self.client.send(json.dumps(dict(type='ready', nonce=answer['nonce'])))
        await asyncio.wait_for(start, 1)
        pcm = b'\x01\x00'*48
        await self.client.send(pcm)
        for _ in range(50):
            if self.bridge.capture._pcm:
                break
            await asyncio.sleep(.002)
        self.assertEqual(self.bridge.capture._pcm, pcm)
        self.assertEqual(self.calls, ['v=0\r\noffer'])
        self.assertEqual(self.ready_waits, ['90s-ready-budget-start'])
        with self.assertRaises(RuntimeError):
            self.bridge.take('v=0\r\noffer')

    async def test_rejected_text_can_close_without_answer_or_candidate(self):
        await self.begin()
        await self.assert_answer_held()
        await self.bridge.close()
        self.assertEqual(self.ready_waits, [])
        self.assertEqual(self.bridge.capture._pcm, b'')
        self.assertTrue(self.bridge.handler.done())

    async def test_wrong_profile_does_not_release_answer(self):
        await self.begin()
        async def finalize(sdp):
            return self.bridge.take(sdp)
        self.bridge.on_offer = finalize
        with self.assertRaises(RuntimeError):
            await self.bridge.start('different-profile')
        await self.assert_answer_held()

    async def test_close_joins_pending_neutral_request_before_finishing(self):
        self.reserve_release, self.cancel_release = asyncio.Event(), asyncio.Event()
        await self.begin()
        closing = asyncio.create_task(self.bridge.close())
        self.tasks.append(closing)
        await asyncio.wait_for(self.reserve_cancelled.wait(), 1)
        self.assertFalse(closing.done())
        self.cancel_release.set()
        await asyncio.wait_for(closing, 1)
        self.assertTrue(self.bridge.handler.done())

    async def test_pcm_before_approved_start_is_not_captured(self):
        await self.begin()
        await self.assert_answer_held()
        await self.client.send(b'\x01\x00'*48)
        await asyncio.wait_for(self.client.wait_closed(), 1)
        self.assertTrue(self.failures)
        self.assertEqual(self.bridge.capture._pcm, b'')

    async def test_unmarked_old_pcm_cannot_be_accepted_after_activation(self):
        await self.begin()
        async def finalize(sdp):
            return self.bridge.take(sdp)
        self.bridge.on_offer = finalize
        start = asyncio.create_task(self.bridge.start('offline-profile'))
        self.tasks.append(start)
        answer = json.loads(await asyncio.wait_for(self.client.recv(), 1))
        await self.client.send(json.dumps(dict(type='ready', nonce=answer.get('nonce'))))
        await asyncio.wait_for(start, 1)
        await self.client.send(b'\x01\x00'*48)
        await asyncio.wait_for(self.client.wait_closed(), 1)
        self.assertTrue(self.failures)
        self.assertEqual(self.bridge.capture._pcm, b'')


if __name__ == '__main__':
    unittest.main()
