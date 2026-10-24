"""Handwritten native voice fixtures; no recordings, credentials or services."""
import asyncio
import base64
import json
from types import SimpleNamespace
from websockets.protocol import State

TEXT = '明日九時に会いましょう。'
PCM = b'\x01\x00' * 480
TAIL = b'\x02\x00' * 17


def wire(event):
    return json.dumps(event).encode()


def usage():
    return dict(type='session.usage.updated',
                usage=dict(audio_duration_ms=20000, backend_model_usage=[]),
                usage_limit=dict(status=None, reset_seconds=None))


def events():
    turn = dict(id='turn_1', role='assistant', start_ms=0, end_ms=20, transcript=TEXT)
    return [dict(type='session.started', session=dict(id='session_1', status='active', expires_at=2000000000)),
            dict(type='output_transcript.added', item=dict(id='turn_1', type='output_transcript', text=TEXT),
                 start_ms=0, end_ms=20),
            dict(type='turn.created', turn=turn),
            dict(type='turn.delta', turn_id='turn_1', delta=TEXT, start_ms=0, end_ms=20),
            dict(type='turn.done', turn=turn)]


class NativeFixture:
    def __init__(self):
        self.steps = []
        self.bridge = None
        self.queue = asyncio.Queue()
        self.closed = False
        self.protocol = SimpleNamespace(close_rcvd=None)
        self.primary = events()
        self.secondary = events()
        self.closed_event = dict(type='session.closed', reason='client_request', usage={})
        self.offer = None
        self.context = None
        self.profile = None
        self.connect_args = None

    @property
    def state(self):
        return State.CLOSED if self.closed else State.OPEN

    async def start(self, bridge, profile):
        self.profile, self.bridge = profile, bridge
        self.steps.append('browser-start')
        answer = await bridge.on_offer('v=0\r\n')
        assert answer.startswith('v=0')
        await self.deliver_answer(bridge, profile)

    async def deliver_answer(self, bridge, profile, nonce=None):
        self.profile = profile
        self.bridge = bridge
        if nonce is not None:
            await bridge._receive_control(dict(type='generation',nonce=nonce))
        bridge.socket = SimpleNamespace(send=self.local_send)
        bridge._receive_audio(PCM)
        for event in self.primary[:1]:
            accepted = await bridge._receive_control(dict(type='live-event', data=wire(event).decode()))
            assert accepted is True
        bridge.ready.set()

    async def local_send(self, raw):
        event = json.loads(raw)
        if event['type'] == 'seal':
            assert self.closed, 'sideband must be closed before local seal'
            self.steps.append('seal')
            self.bridge._receive_audio(TAIL)
            await self.bridge._receive_control(dict(type='sealed', seal_id=event['seal_id'], samples=497))
        elif event['type'] == 'close':
            self.steps.append('browser-close')

    def connect(self, *args, **kwargs):
        self.connect_args = (args, kwargs)
        fixture = self
        class Connection:
            def __await__(self):
                async def open_socket():
                    fixture.steps.append('sideband-connect')
                    assert args[0] == 'wss://api.openai.com/v1/live/call_1'
                    for event in fixture.secondary[:1]:
                        fixture.queue.put_nowait(wire(event))
                    return fixture
                return open_socket().__await__()
        return Connection()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        await asyncio.sleep(0)
        if item is None:
            raise StopAsyncIteration()
        return item

    async def send(self, raw):
        event = json.loads(raw)
        if event['type'] == 'session.context.append':
            assert self.context is None, 'each candidate has one explicit speech request'
            self.context = event
            self.steps.append('context-append')
            ack = dict(type='session.context.appended', start_ms=200, end_ms=400)
            for message in [ack, *self.primary[1:]]:
                await self.bridge._receive_control(dict(type='live-event', data=wire(message).decode()))
            for message in [ack, *self.secondary[1:]]:
                self.queue.put_nowait(wire(message))
            return
        assert event['type'] == 'session.close'
        self.steps.append('native-close')
        if self.bridge is not None:
            await self.bridge._receive_control(dict(type='live-event', data=wire(self.closed_event).decode()))
        self.queue.put_nowait(wire(self.closed_event))

    async def close(self):
        if not self.closed:
            self.steps.append('ws-close')
            self.closed = True
            self.protocol.close_rcvd = SimpleNamespace(code=1000)
            self.queue.put_nowait(None)

    def request(self, operation, body, headers):
        assert operation == 'live'
        self.offer = json.loads(body)
        self.steps.append('http')
        return b'v=0\r\n', 'wss://api.openai.com/v1/live/call_1'


def reflected():
    return dict(type='session.output_audio.delta', delta=base64.b64encode(PCM).decode(),
                start_ms=0, end_ms=20)
