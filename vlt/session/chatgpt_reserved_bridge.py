"""Own local negotiation until a content-free reservation is approved."""
import asyncio
import json
import secrets
from websockets.exceptions import ConnectionClosed

from .chatgpt_voice import _Bridge, voice_page
from .chatgpt_http import _RequestNotSent


def reserved_page():
    page = voice_page(deferred=True)
    changes = {
        'let peer, context, socket, input, sink, capture, sealed=false;':
            'let peer, context, socket, input, sink, capture, sealed=false, activationNonce=null;',
        "if(message.type==='answer')await peer.setRemoteDescription({type:'answer',sdp:message.sdp});":
            "if(message.type==='answer'){if(activationNonce||typeof message.nonce!=='string'||"
            "!/^[0-9a-f]{32}$/.test(message.nonce))throw Error();activationNonce=message.nonce;"
            "socket.send(JSON.stringify({type:'generation',nonce:activationNonce}));"
            "await peer.setRemoteDescription({type:'answer',sdp:message.sdp});}",
        "socket.send(JSON.stringify({type:'ready'}));":
            "socket.send(JSON.stringify({type:'ready',nonce:activationNonce}));",
    }
    for before, after in changes.items():
        if page.count(before) != 1:
            raise RuntimeError('voice-page')
        page = page.replace(before, after)
    return page


class ReservedBridge(_Bridge):
    def __init__(self, reserve, failure, receive):
        super().__init__(self._hold_offer, failure, receive)
        self.reserve = reserve
        self._negotiating = self._reservation = None
        self._reserved_sdp = None
        self._approved = asyncio.Event()
        self._taken = False
        self._answering = None
        self._activation_nonce = None
        self._audio_armed = False

    def prepare(self, profile):
        super().prepare(profile)
        self._page = reserved_page()

    async def _handle(self, socket):
        if self.socket is not None:
            await socket.close(code=1008)
            return
        self.socket, self.handler = socket, asyncio.current_task()
        try:
            async for message in socket:
                if self.closing:
                    break
                if isinstance(message, bytes):
                    self._receive_audio(message)
                    continue
                event = json.loads(message)
                if not isinstance(event, dict):
                    raise RuntimeError('capture-transport')
                kind = event.get('type')
                if kind == 'offer':
                    if (set(event) != {'type', 'nonce', 'sdp'} or self._negotiated
                            or not self._offer_nonce or event['nonce'] != self._offer_nonce
                            or not isinstance(event['sdp'], str) or not event['sdp'].startswith('v=0')
                            or len(event['sdp'].encode()) > 262144):
                        raise RuntimeError('capture-transport')
                    self._negotiated = True
                    # Keep reading frames while the independent neutral request waits.
                    self._answering = asyncio.create_task(self._hold_offer(event['sdp']))
                elif kind == 'ready':
                    if (set(event) != {'type', 'nonce'} or self._activation_nonce is None
                            or event['nonce'] != self._activation_nonce or self.ready.is_set()):
                        raise RuntimeError('capture-transport')
                    self.ready.set()
                elif kind == 'error':
                    raise RuntimeError('capture-transport')
                elif kind == 'consumed':
                    size = event.get('bytes')
                    if type(size) is not int or size <= 0 or size % 2 or size > self._pending_pcm:
                        raise RuntimeError('capture-transport')
                    self._pending_pcm -= size
                    self._consumed_pcm += size
                    self._drained.set()
                elif not await self._receive_control(event):
                    raise RuntimeError('capture-transport')
        except Exception as error:
            self._protocol_failed = (not isinstance(error, ConnectionClosed)
                or any(frame is not None and frame.code not in (1000, 1001, 1005, 1006, 1011, 1012, 1013)
                       for frame in (error.rcvd, error.sent)))
            self._capture_error(None)
        finally:
            self._send_failed = True
            self._drained.set()
            if not self.closing:
                self._capture_error(None)
                self.ready.set()

    async def begin_reservation(self):
        if self.closing or not self._deferred:
            raise RuntimeError('capture-transport')
        await self._start_negotiation()

    async def _start_negotiation(self):
        if self._negotiating is None:
            self._negotiating = asyncio.create_task(super()._start_negotiation())
        await asyncio.shield(self._negotiating)

    async def _hold_offer(self, sdp):
        if self._reservation is not None or self.closing:
            raise RuntimeError('capture-transport')
        self._reserved_sdp = sdp
        self._reservation = asyncio.create_task(self.reserve(sdp))
        try:
            await asyncio.shield(self._reservation)
            await self._approved.wait()
            if self.closing or self.capture.failure or self.on_offer == self._hold_offer:
                raise RuntimeError('capture-transport')
            answer = await self.on_offer(sdp)
            if self.closing or self.capture.failure:
                raise RuntimeError('capture-transport')
            self._activation_nonce = secrets.token_hex(16)
            await self.socket.send(json.dumps(dict(type='answer', sdp=answer, nonce=self._activation_nonce)))
        except asyncio.CancelledError:
            raise
        except _RequestNotSent:
            self._capture_error(None)
            await self.socket.close(code=1000)
        except Exception:
            self._protocol_failed = True
            self._capture_error(None)
            await self.socket.close(code=1008)

    async def start(self, profile):
        if (self.closing or self._starting or self._profile != profile
                or not callable(self.on_offer) or self.on_offer == self._hold_offer):
            raise RuntimeError('capture-transport')
        # Only the approved generation path may release the RTP answer.
        self._approved.set()
        await super().start(profile)

    def take(self, sdp):
        if (self.closing or not self._approved.is_set() or self._taken
                or sdp != self._reserved_sdp or self._reservation is None
                or not self._reservation.done() or self._reservation.cancelled()
                or self._reservation.exception() is not None):
            raise RuntimeError('capture-transport')
        self._taken = True
        return self._reservation.result()

    def _receive_audio(self, pcm):
        if not self._audio_armed:
            self.capture.fail('capture-pcm')
        super()._receive_audio(pcm)

    async def _receive_control(self, event):
        if event.get('type') == 'generation':
            if (set(event) != {'type', 'nonce'} or self._activation_nonce is None or self._audio_armed
                    or event['nonce'] != self._activation_nonce):
                raise RuntimeError('capture-transport')
            self._audio_armed = True
            return True
        if event.get('type') == 'live-event' and not self._audio_armed:
            raise RuntimeError('capture-transport')
        return await super()._receive_control(event)

    async def _close(self, caller):
        try:
            self._approved.set()
            tasks = [task for task in (self._negotiating, self._reservation, self._answering)
                     if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await super()._close(caller)
