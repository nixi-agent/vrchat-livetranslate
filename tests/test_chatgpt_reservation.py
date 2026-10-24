"""Target-aware production factory, real NativeVoice and local wire; offline."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from chatgpt_reserved_fakes import _Harness, PCM, TAIL, TEXT


class ReservationTests(_Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.preparation_scope = self.base.prepare_voice('ja')
        self.reservation = await self.preparation_scope.__aenter__()

    async def test_reserves_without_source_or_script_then_reuses_one_request(self):
        await self.settle_open()
        self.assertEqual(len(self.requests), 1, 'neutral reservation should overlap translation/checks')
        body = json.loads(self.requests[0][1])
        self.assertEqual(set(body), {'sdp', 'session'})
        self.assertNotIn(TEXT, self.requests[0][1].decode())
        self.assertNotIn('initial_items', body['session'])
        self.assertEqual(self.timers, [])
        self.assertIsNone(self.fixture.context)
        await self.reservation.activate()
        candidate, pcm = await self.reservation.generate(TEXT, 'ja')
        self.assertTrue(candidate)
        self.assertEqual(pcm, PCM+TAIL)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.fixture.steps.count('context-append'), 1)
        self.assertEqual(self.timers, [('generation', 120), ('ready', 90)])

    async def test_rejection_closes_without_any_speech_request(self):
        await self.settle_open()
        self.assertEqual(len(self.requests), 1)
        await self.reservation.close()
        self.assertIsNone(self.fixture.context)
        self.assertEqual(self.timers, [])
        self.assertTrue(self.fixture.closed, 'unused remote reservation must have an owned closed transport')

    async def test_owner_revocation_prevents_activation(self):
        await self.settle_open()
        self.owner.generation += 1
        with self.assertRaisesRegex(RuntimeError, 'voice-stale'):
            await self.reservation.activate()
        self.assertIsNone(self.fixture.context)

    async def test_malformed_neutral_reply_cannot_be_hidden_by_expiry(self):
        self.base._request = AsyncMock(return_value=(b'invalid SDP', 'wss://api.openai.com/v1/live/call_1'))
        await self.settle_open()
        self.reservation.voice._prepared_at -= 20
        with self.assertRaises(RuntimeError):
            await self.reservation.activate()
        self.base._request.assert_awaited_once()
        self.assertIsNone(self.fixture.context)
        self.assertEqual(self.timers, [])

    async def test_unused_local_transport_loss_keeps_original_cold_recovery(self):
        await self.settle_open()
        self.clients[0].transport.abort()
        await asyncio.wait_for(self.clients[0].wait_closed(), 1)
        await asyncio.wait_for(self.reservation.voice.bridge.handler, 1)
        await self.reservation.activate()
        _, pcm = await self.reservation.generate(TEXT, 'ja')
        self.assertEqual(pcm, PCM+TAIL)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(sum(f.context is not None for f in self.fixtures), 1)

    async def test_close_keeps_http_result_owned_until_unused_remote_is_closed(self):
        self.request_release = asyncio.Event()
        await self.settle_open()
        closing = asyncio.create_task(self.reservation.close())
        try:
            for _ in range(60):
                await asyncio.sleep(0)
            self.assertFalse(self.request_cancelled.is_set(), 'do not lose an allocated Location by cancelling HTTP')
            self.assertFalse(closing.done())
        finally:
            self.request_release.set()
            await asyncio.wait_for(closing, 3)
        self.assertTrue(self.fixture.closed)
        self.assertIsNone(self.fixture.context)

    async def test_expired_preparation_is_joined_before_generation_clock(self):
        await self.settle_open()
        warm = self.reservation.voice
        warm._prepared_at -= 20
        await self.reservation.activate()
        self.assertTrue(warm.bridge.closing, 'discard neutral preparation before timed generation')
        self.assertTrue(warm.bridge.handler.done())
        self.assertEqual(self.timers, [])
        _, pcm = await self.reservation.generate(TEXT, 'ja')
        self.assertEqual(pcm, PCM+TAIL)
        self.assertEqual(self.timers, [('generation', 120), ('ready', 90)])
        self.assertEqual(len(self.requests), 2, 'one unused neutral reservation, one actual generation')
        self.assertEqual(self.fixture.steps.count('context-append'), 1)

    async def test_committed_preparation_does_not_expire_between_stage_boundaries(self):
        await self.settle_open()
        await self.reservation.activate()
        with patch('vlt.session.chatgpt_voice._PREPARE_TTL', 0):
            _, pcm = await self.reservation.generate(TEXT, 'ja')
        self.assertEqual(pcm, PCM+TAIL)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.fixture.steps.count('context-append'), 1)



if __name__ == '__main__':
    unittest.main()
