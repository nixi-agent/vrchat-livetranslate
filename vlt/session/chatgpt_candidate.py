"""只封存候選的 BrowserBridge；不呼叫文字或播放出口。"""
import asyncio
import json
import secrets

from .chatgpt_browser import BrowserBridge
from .chatgpt_capture import Capture


class CandidateBridge(BrowserBridge):
    def __init__(self, on_offer, on_error):
        super().__init__(on_offer, None, self._capture_error)
        self._notify_error = on_error
        self.capture = Capture()
        self._sealed = asyncio.Event()
        self._sealed_pcm = None

    def _receive_audio(self, pcm):
        self.capture.append(pcm)

    def _capture_error(self, _):
        self._sealed_pcm = None
        try:
            self.capture.fail('capture-transport')
        except RuntimeError:
            pass
        self._sealed.set()
        self._notify_error('capture-transport')

    async def _receive_control(self, event):
        if event.get('type') != 'sealed':
            return False
        self._sealed_pcm = self.capture.acknowledge(event)
        self._sealed.set()
        return True

    async def seal(self):
        if not self.socket or self.closing or self._send_failed:
            raise RuntimeError('capture-transport')
        seal_id = secrets.token_hex(8)
        self.capture.request_seal(seal_id)
        try:
            await self.socket.send(json.dumps({'type': 'seal', 'seal_id': seal_id}))
            await asyncio.wait_for(self._sealed.wait(), 5)
        except asyncio.CancelledError:
            self._capture_error(None)
            raise
        except Exception:
            self._capture_error(None)
            raise RuntimeError('capture-transport') from None
        if self.capture.failure or self._sealed_pcm is None:
            raise RuntimeError(self.capture.failure or 'capture-transport')
        return self._sealed_pcm

    async def close(self):
        self._sealed_pcm = None
        try:
            self.capture.fail('capture-closed')
        except RuntimeError:
            pass
        self._sealed.set()
        await super().close()
