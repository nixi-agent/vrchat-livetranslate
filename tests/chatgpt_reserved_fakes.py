import asyncio
import json
from pathlib import Path
import sys
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_browser import BrowserBridge
from vlt.session.chatgpt_relay import SubscriptionRelay
from chatgpt_voice_fakes import NativeFixture, PCM, TAIL, TEXT as TEXT, wire


class _Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = NativeFixture()
        self.fixtures = [self.fixture]
        self.owner = SimpleNamespace(generation=1, require_usable=Mock(),
            credentials=AsyncMock(return_value=SimpleNamespace(generation=1, account='offline', access='offline')))
        self.base = SubscriptionRelay(self.owner)
        self.preparations = []
        original_prepare = self.base.prepare_voice
        @asynccontextmanager
        async def track(target):
            async with original_prepare(target) as prepared:
                self.preparations.append(prepared)
                yield prepared
        self.base.prepare_voice = track
        self.requests, self.clients, self.peers, self.profiles, self.bridges, self.timers = [], [], [], [], [], []
        self.opened, self.requested = asyncio.Event(), asyncio.Event()
        self.request_release = self.cleanup_release = None
        self.request_failure = None
        self.request_cancelled = asyncio.Event()
        async def request(operation, body, headers):
            if self.requests:
                self.fixture = NativeFixture()
                self.fixtures.append(self.fixture)
            self.requests.append((operation, body, headers))
            self.requested.set()
            if self.request_release is not None:
                try:
                    await self.request_release.wait()
                except asyncio.CancelledError:
                    self.request_cancelled.set()
                    if self.cleanup_release is not None:
                        await self.cleanup_release.wait()
                    raise
            if self.request_failure == 'uncertain':
                raise RuntimeError('voice-request')
            if (self.request_failure in ('unsent', 'unsent-always')
                    and (len(self.requests) == 1 or self.request_failure == 'unsent-always')):
                connection = Mock()
                connection.connect.side_effect = OSError('offline-before-POST')
                with patch('vlt.session.chatgpt_http.tls_context'), \
                        patch('http.client.HTTPSConnection', return_value=connection), patch('threading.Timer'):
                    return await SubscriptionRelay._request(self.base, operation, body, headers)
            if self.request_failure == 'cancelled':
                raise asyncio.CancelledError()
            if self.request_failure == 'malformed':
                return b'invalid SDP', 'wss://api.openai.com/v1/live/call_1'
            return self.fixture.request(operation, body, headers)
        self.base._request = request
        async def open_local(bridge, profile):
            self.bridges.append(bridge)
            self.profiles.append(profile)
            bridge.server = await serve(bridge._handle, '127.0.0.1', 0,
                process_request=bridge._http, compression=None, close_timeout=.1)
            bridge.origin = 'http://127.0.0.1:'+str(bridge.server.sockets[0].getsockname()[1])
            client = await connect(bridge.origin.replace('http:', 'ws:')+'/'+bridge.token+'/ws',
                                   origin=bridge.origin, compression=None, close_timeout=.1)
            self.clients.append(client)
            async def peer():
                await client.send('{"type":"prepared"}')
                command = json.loads(await client.recv())
                if command['type'] == 'close':
                    return
                await client.send(json.dumps(dict(type='offer', nonce=command['nonce'], sdp='v=0\r\n')))
                reply = json.loads(await client.recv())
                if reply['type'] == 'close':
                    return
                self.fixture.bridge, self.fixture.profile = bridge, profile
                marker = reply.get('nonce', '').encode('ascii')
                if marker:
                    await client.send(json.dumps(dict(type='generation', nonce=marker.decode('ascii'))))
                await client.send(PCM)
                await client.send(json.dumps(dict(type='live-event', data=wire(self.fixture.primary[0]).decode())))
                ready = dict(type='ready')
                if marker:
                    ready['nonce'] = marker.decode('ascii')
                await client.send(json.dumps(ready))
                async for raw in client:
                    message = json.loads(raw)
                    if message['type'] == 'seal':
                        assert self.fixture.closed
                        await client.send(TAIL)
                        await client.send(json.dumps(dict(type='sealed', seal_id=message['seal_id'], samples=497)))
                    elif message['type'] == 'close':
                        break
            self.peers.append(asyncio.create_task(peer()))
            self.opened.set()
        self.enterContext(patch.object(BrowserBridge, '_open', open_local))
        self.enterContext(patch('websockets.asyncio.client.connect', side_effect=lambda *a, **k:self.fixture.connect(*a, **k)))
        timeout, wait_for = asyncio.timeout, asyncio.wait_for
        def measured_timeout(delay):
            if delay == 120:
                self.timers.append(('generation', delay))
            return timeout(delay)
        def measured_wait_for(operation, delay):
            if delay == 90:
                self.timers.append(('ready', delay))
            return wait_for(operation, delay)
        self.enterContext(patch('asyncio.timeout', measured_timeout))
        self.enterContext(patch('asyncio.wait_for', measured_wait_for))
        self.reservation = self.pipeline = self.preparation_scope = None

    async def asyncTearDown(self):
        if self.request_release is not None:
            self.request_release.set()
        if self.cleanup_release is not None:
            self.cleanup_release.set()
        if self.pipeline is not None:
            await self.pipeline.close()
        if self.preparation_scope is not None:
            await self.preparation_scope.__aexit__(None, None, None)
        elif self.reservation is not None:
            await self.reservation.close()
        for client in self.clients:
            await client.close()
        await asyncio.gather(*self.peers, return_exceptions=True)
        self.assertTrue(all(not Path(profile).exists() for profile in self.profiles))
        self.assertTrue(all(bridge.handler is None or bridge.handler.done() for bridge in self.bridges))

    async def settle_open(self):
        await asyncio.wait_for(self.opened.wait(), 1)
        for _ in range(40):
            await asyncio.sleep(.002)
