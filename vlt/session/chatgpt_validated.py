"""Subscription session: only coordinator-approved captions and frozen PCM leave this boundary."""
import asyncio

from . import chatgpt_auth, chatgpt_relay
from .base import LiveTranslateSession
from .chatgpt_http import tls_context
from .chatgpt_pipeline import Coordinator


class ChatGPTValidatedSession(LiveTranslateSession):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._direction = getattr(cfg, '_direction', None)
        self._owner = chatgpt_auth.AUTH_OWNER
        self._pipeline = None
        self._started = self._alive = self._closing = False
        self._failure = ''
        self._close_task = None
        self._rejected = self._finalized = 0
        self._last_reason = ''
        self.on_rejection = None

    async def start(self, on_text, on_audio=None, on_usage=None):
        if (self._started or self._closing or self._direction not in ('mine', 'theirs')
                or self.cfg.provider != 'chatgpt' or not callable(on_text)
                or (self.cfg.output_audio and (self._direction != 'mine' or not callable(on_audio)))):
            raise RuntimeError('session-input')
        self._started = True
        self.on_text, self.on_audio, self.on_usage = on_text, on_audio, on_usage
        failure = 'session-start'
        try:
            async with asyncio.timeout(35):
                credentials = await self._owner.credentials()
            if self._closing:
                raise RuntimeError('session-closed')
            self._owner.require_usable()
            # Load native CAs before Engine starts input processing, not during an utterance.
            tls_context()
            await asyncio.sleep(0)
            if self._closing or credentials.generation != self._owner.generation:
                raise RuntimeError('session-closed')
            self._owner.require_usable()
            admission = getattr(self, '_engine_admission', None)
            if admission is not None and not admission.valid():
                failure = 'session-context'
                raise RuntimeError('session-context')
            self._pipeline = Coordinator({self._direction: self.cfg},
                chatgpt_relay.SubscriptionRelay(self._owner), self._reason,
                lambda: self._owner.generation,
                lambda _: admission is None or admission.valid())
            self._alive = True
        except BaseException as error:
            self._failure = failure
            await self.close()
            if isinstance(error, asyncio.CancelledError) or not isinstance(error, Exception):
                raise
            raise RuntimeError(failure) from None

    def _usable(self):
        if not self.is_alive:
            return False
        try:
            self._owner.require_usable()
            admission = getattr(self, '_engine_admission', None)
            if admission is not None and not admission.valid():
                raise RuntimeError('engine-context')
            if getattr(self.cfg, '_direction', None) != self._direction:
                raise RuntimeError('direction-changed')
            return True
        except Exception:
            self.abort('session-context')
            return False

    async def send_audio(self, pcm16_16k):
        if not self._usable():
            raise RuntimeError('session-not-ready')
        self._pipeline.feed(self._direction, pcm16_16k)

    def end_input_segment(self):
        if self._usable():
            self._pipeline.finish(self._direction)

    def tick(self):
        if not self._usable():
            return
        try:
            if self._pipeline.consume(self._direction, self.on_text, self.on_audio):
                self._finalized += 1
        except Exception:
            self.abort('session-output')

    def _reason(self, direction, segment_id, reason):
        self._rejected += 1
        self._last_reason = reason
        if callable(self.on_rejection):
            try:
                self.on_rejection(reason)
            except Exception:
                pass

    def abort(self, reason):
        self._alive = False
        self._failure = reason if reason in ('session-context', 'session-output') else 'session-aborted'
        if self._pipeline is not None:
            self._pipeline.invalidate()

    @property
    def is_alive(self):
        return self._alive and not self._closing

    @property
    def fail_reason(self):
        return self._failure

    def diagnostics(self):
        return {'transport': 'subscription-validated', 'alive': self.is_alive,
                'phase': 'ready' if self.is_alive else 'closed', 'finalized': self._finalized,
                'rejected': self._rejected, 'last_reason': self._last_reason}

    async def close(self):
        self._alive = False
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            self._close_task.exception() if not self._close_task.cancelled() else None
            raise asyncio.CancelledError()
        self._close_task.result()

    async def _close(self):
        if self._pipeline is not None:
            await self._pipeline.close()
        self.on_text = self.on_audio = self.on_usage = self.on_rejection = None
