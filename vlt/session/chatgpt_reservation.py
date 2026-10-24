"""Content-free GPT-Live preparation with owned approval and cleanup."""
import asyncio
import json
import tempfile
from websockets.protocol import State

from .chatgpt_events import bounded_json
from .chatgpt_http import _RequestNotSent
from .chatgpt_voice import NativeVoice, _READ, _READ_JA, _PREPARE_TTL
from ..ui_text import TARGET_LANGS
from .chatgpt_reserved_bridge import ReservedBridge


class _CommittedVoice(NativeVoice):
    def _prepared_usable(self):
        if not self.relay.active:
            return super()._prepared_usable()
        self._check()
        bridge = self.bridge
        opening = bridge._open_task if bridge is not None else None
        if (bridge is None or bridge.closing or bridge.capture.failure or opening is None
                or opening.cancelled() or (opening.done() and opening.exception() is not None)):
            raise RuntimeError('voice-transport')
        return True


class VoiceReservation:
    def __init__(self, relay, target):
        if target not in TARGET_LANGS.values():
            raise RuntimeError('voice-input')
        self.base, self.owner, self.target = relay, relay.owner, target
        self.generation = self.owner.generation
        self._check()
        self._body = None
        self._cancelled = False
        self._reserved_ok = False
        self._request_unsent = False
        self._headers = {'Content-Type': 'application/json', 'originator': 'codex_cli_rs',
                         'openai-alpha': 'quicksilver=v2'}
        self.transport = NativeVoice(relay)
        self.voice = _CommittedVoice(self)
        self.voice._connect = self._adopt
        self.voice.profile = tempfile.TemporaryDirectory(prefix='vlt-reserved-')
        self.voice._prepared_at = asyncio.get_running_loop().time()
        self.voice.bridge = ReservedBridge(self._reserve, lambda _: self.voice._fail(), self.voice._receive)
        self.voice.bridge.prepare(self.voice.profile.name)
        self.begin = asyncio.create_task(self.voice.bridge.begin_reservation())
        self.selected = self.voice
        self.active = False

    def _check(self):
        self.owner.require_usable()
        if self.generation != self.owner.generation:
            raise RuntimeError('voice-stale')

    async def _finish(self, task):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                self._cancelled = True
            except Exception:
                break
        return task.result()

    async def _reserve(self, sdp):
        result = await self._reserve_owned(sdp)
        self._reserved_ok = True
        if self._cancelled:
            raise asyncio.CancelledError()
        return result

    async def _reserve_owned(self, sdp):
        self._check()
        session = {'model': 'gpt-live-1-codex',
                   'instructions': _READ_JA if self.target == 'ja' else _READ.format(target=self.target),
                   'audio': {'output': {'voice': 'juniper'}},
                   'delegation': {'type': 'client', 'ack_filler': False}}
        self._body = json.dumps({'sdp': sdp, 'session': session}, ensure_ascii=False).encode()
        try:
            result = await self._finish(asyncio.create_task(self.base._request('live', self._body, self._headers)))
        except _RequestNotSent:
            self._request_unsent = True
            raise
        self._check()
        raw, self._url = result
        answer = bounded_json(raw)['sdp'] if raw.lstrip().startswith(b'{') else raw.decode()
        if not isinstance(answer, str) or not answer.startswith('v=0'):
            raise RuntimeError('voice-sdp')
        credentials = await self._finish(asyncio.create_task(self.owner.credentials()))
        self._check()
        if credentials.generation != self.generation:
            raise RuntimeError('voice-stale')
        self.transport.connecting = asyncio.create_task(self.transport._connect(self._url, credentials))
        await self._finish(self.transport.connecting)
        self._check()
        return result

    async def _adopt(self, url, credentials):
        self._check()
        socket = self.transport.socket
        if (url != self._url or credentials.generation != self.generation
                or self.transport._close_task is not None or socket is None
                or socket.state is not State.OPEN):
            raise RuntimeError('voice-transport')
        self.voice.socket, self.transport.socket = socket, None

    async def _request(self, operation, body, headers):
        self._check()
        if operation != 'live' or body != self._body or headers != self._headers:
            raise RuntimeError('voice-input')
        return self.voice.bridge.take(json.loads(body)['sdp'])

    async def _stop_warm(self):
        try:
            await self.voice.close()
        finally:
            try:
                await self.transport.close()
            finally:
                self.begin.cancel()
                await asyncio.gather(self.begin, return_exceptions=True)

    def _proven_unsent(self):
        task = self.voice.bridge._reservation
        return (self._request_unsent and task is not None and task.done() and not task.cancelled()
                and isinstance(task.exception(), _RequestNotSent))

    async def activate(self):
        self._check()
        if self.active or self.voice._close_task is not None:
            raise RuntimeError('voice-stale')
        bridge = self.voice.bridge
        reservation = bridge._reservation
        age = asyncio.get_running_loop().time()-self.voice._prepared_at
        if reservation is not None and not reservation.done() and age < _PREPARE_TTL:
            # Settle preparatory HTTP before committing the original generation clocks.
            try:
                await asyncio.shield(reservation)
            except _RequestNotSent:
                pass
            self._check()
        if (reservation is not None and reservation.done()
                and (reservation.cancelled() or reservation.exception() is not None)
                and not self._proven_unsent()):
            raise RuntimeError('voice-transport')
        if (bridge._protocol_failed or self.voice.events._count or bridge.capture._pcm
                or bridge.capture.failure not in (None, 'capture-transport')):
            raise RuntimeError('voice-event')
        age = asyncio.get_running_loop().time()-self.voice._prepared_at
        if self._proven_unsent() or age >= _PREPARE_TTL or not self.voice._prepared_usable():
            # Dispose unused neutral work before the original voice-stage clock starts.
            await self._stop_warm()
            self._check()
            if (bridge._protocol_failed
                    or (self._body is not None and not self._reserved_ok and not self._proven_unsent())
                    or (reservation is not None and reservation.done()
                    and not reservation.cancelled() and reservation.exception() is not None
                    and not self._proven_unsent())):
                raise RuntimeError('voice-event')
            self.selected = NativeVoice(self.base)
        self.active = True

    async def generate(self, text, target):
        self._check()
        if not self.active or target != self.target:
            raise RuntimeError('voice-input')
        return await self.selected.generate(text, target)

    async def close(self):
        try:
            await self._stop_warm()
        finally:
            if self.selected is not self.voice:
                await self.selected.close()
