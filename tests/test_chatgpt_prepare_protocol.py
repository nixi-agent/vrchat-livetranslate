"""Real localhost WebSocket boundary for unapproved native preparation."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_browser import BrowserBridge
from vlt.session.chatgpt_relay import SubscriptionRelay


class PreparedProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        owner = SimpleNamespace(generation=1, require_usable=Mock(),
            credentials=AsyncMock(return_value=SimpleNamespace(generation=1, account='offline', access='offline')))
        self.relay = SubscriptionRelay(owner)
        self.relay._request = AsyncMock(side_effect=RuntimeError('voice-request'))
        self.clients, self.bridges, self.profiles = [], [], []
        self.first_open = True

        async def open_local(bridge, profile):
            self.bridges.append(bridge)
            self.profiles.append(profile)
            bridge.server = await serve(bridge._handle, '127.0.0.1', 0,
                process_request=bridge._http, close_timeout=.1, max_size=256000, compression=None)
            port = bridge.server.sockets[0].getsockname()[1]
            bridge.origin = f'http://127.0.0.1:{port}'
            client = await connect(f'ws://127.0.0.1:{port}/{bridge.token}/ws', origin=bridge.origin,
                                   close_timeout=.1, compression=None)
            self.clients.append(client)
            if not self.first_open:
                await client.send(json.dumps(dict(type='offer', sdp='v=0\r\n')))
            self.first_open = False

        self.enterContext(patch.object(BrowserBridge, '_open', open_local))

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.assertTrue(all(bridge.closing for bridge in self.bridges))
        self.assertTrue(all(bridge.handler is None or bridge.handler.done() for bridge in self.bridges))
        self.assertTrue(all(not Path(profile).exists() for profile in self.profiles))

    async def inject_before_approval(self, messages, ttl=10, drained=False):
        self.first_open = True
        before = len(self.bridges)
        async with self.relay.prepare_voice() as generate:
            for _ in range(60):
                if len(self.clients) > before:
                    break
                await asyncio.sleep(.01)
            self.assertGreater(len(self.clients), before)
            client = self.clients[before]
            for message in messages:
                await client.send(message)
            if drained:
                await asyncio.wait_for(client.wait_closed(), 1)
                await asyncio.wait_for(asyncio.shield(self.bridges[before].handler), 1)
            else:
                await asyncio.sleep(.02)
            with patch('vlt.session.chatgpt_voice._PREPARE_TTL', ttl), \
                    patch('vlt.session.chatgpt_voice._DEADLINE', .5):
                with self.assertRaises(RuntimeError):
                    await asyncio.wait_for(generate('Read exactly this sentence.', 'en'), 2)
        self.assertEqual(self.relay._request.await_count, 0, 'pre-approval protocol failure cannot cause Live HTTP')
        self.assertEqual(len(self.bridges) - before, 1, 'protocol failure must not cause a cold retry')

    async def test_malformed_json_before_approval_is_not_transport_fallback(self):
        await self.inject_before_approval(['{bad-json'])

    async def test_odd_pcm_before_approval_is_not_transport_fallback(self):
        await self.inject_before_approval([b'x'])

    async def test_unknown_control_before_approval_is_not_transport_fallback(self):
        await self.inject_before_approval(['{"type":"unknown"}'])

    async def test_bad_event_envelope_before_approval_is_not_transport_fallback(self):
        await self.inject_before_approval(['{"type":"live-event","data":0}'])

    async def test_oversized_websocket_frame_is_not_transport_fallback(self):
        await self.inject_before_approval([bytes(256002)], drained=True)

    async def test_offer_then_queued_pcm_cannot_hide_until_approval(self):
        await self.inject_before_approval(['{"type":"offer","sdp":"v=0\\r\\n"}', b'\x01\x00'])

    async def test_expired_offer_with_queued_event_cannot_be_cold_retried(self):
        await self.inject_before_approval(['{"type":"offer","sdp":"v=0\\r\\n"}',
            '{"type":"live-event","data":"{}"}'], ttl=0)

    async def activation(self, correct):
        async with self.relay.prepare_voice() as generate:
            for _ in range(60):
                if self.clients:
                    break
                await asyncio.sleep(.01)
            client = self.clients[0]
            await client.send('{"type":"prepared"}')
            await asyncio.sleep(.02)
            self.relay._request.assert_not_awaited()
            with patch('vlt.session.chatgpt_voice._DEADLINE', 1):
                generating = asyncio.create_task(generate('Read exactly this sentence.', 'en'))
                try:
                    command = json.loads(await asyncio.wait_for(client.recv(), 1))
                    self.assertEqual(command['type'], 'activate')
                    self.assertRegex(command['nonce'], '^[0-9a-f]{32}$')
                    self.relay._request.assert_not_awaited()
                    await client.send(json.dumps(dict(type='offer', sdp='v=0\r\n',
                        nonce=command['nonce'] if correct else '0' * 32)))
                    with self.assertRaises(RuntimeError):
                        await asyncio.wait_for(generating, 2)
                finally:
                    generating.cancel()
                    await asyncio.gather(generating, return_exceptions=True)
        self.assertEqual(self.relay._request.await_count, int(correct))
        self.assertEqual(len(self.bridges), 1)

    async def test_only_matching_post_approval_activation_nonce_can_start_live(self):
        await self.activation(True)

    async def test_wrong_activation_nonce_never_starts_live_or_cold_retry(self):
        await self.activation(False)

    async def test_protocol_failure_recorded_during_expired_cleanup_prevents_retry(self):
        async with self.relay.prepare_voice() as generate:
            for _ in range(60):
                if self.clients:
                    break
                await asyncio.sleep(.01)
            client, bridge = self.clients[0], self.bridges[0]
            await client.send('{"type":"prepared"}')
            await asyncio.sleep(.02)
            close = bridge.close

            async def close_with_pending_error():
                try:
                    await client.send('{bad-json')
                    await asyncio.wait_for(client.wait_closed(), 1)
                    await asyncio.wait_for(asyncio.shield(bridge.handler), 1)
                    self.assertTrue(bridge._protocol_failed)
                finally:
                    await close()

            bridge.close = close_with_pending_error
            with patch('vlt.session.chatgpt_voice._PREPARE_TTL', 0), \
                    patch('vlt.session.chatgpt_voice._DEADLINE', .5):
                with self.assertRaises(RuntimeError) as failure:
                    await generate('Read exactly this sentence.', 'en')
        self.relay._request.assert_not_awaited()
        self.assertEqual(len(self.bridges), 1)
        self.assertEqual(str(failure.exception), 'voice-event')


if __name__ == '__main__':
    unittest.main()
