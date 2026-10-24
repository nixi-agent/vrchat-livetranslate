"""Approved output waits for actual audio capacity; no devices or services."""
import asyncio
import struct
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_test_fakes import no_audio_device
from test_repeat_guard import _cfg
from vlt.engine import Engine, EngineEvents
from vlt.output.micproxy import MicProxy, MODE_TRANSLATED, MODE_PASSTHROUGH
from vlt.output.micproxy_linux import LinuxMicProxy
from vlt.output.micproxy_buffers import _TranslatedBuffer
from vlt.output.virtualmic import VirtualMic, PwCatVirtualMic, resample_24k_mono_to_48k_stereo
from vlt.session.chatgpt_admission import Admission
from vlt.session.chatgpt_validated import ChatGPTValidatedSession

BLOCK = 3840


class BackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(no_audio_device())
        self.owner = SimpleNamespace(generation=1, require_usable=Mock(),
            credentials=AsyncMock(return_value=SimpleNamespace(generation=1)))
        self.enterContext(patch('vlt.session.chatgpt_auth.AUTH_OWNER', self.owner))
        self.enterContext(patch('vlt.session.chatgpt_validated.tls_context', Mock()))

    def consumer(self, kind):
        proxy = None
        if kind in ('MicProxy', 'LinuxMicProxy'):
            cls = MicProxy if kind == 'MicProxy' else LinuxMicProxy
            proxy = cls(audio_cfg={})
            proxy._mode = MODE_TRANSLATED
            proxy._translated = _TranslatedBuffer(0, 'offline', buffer_ms=1)
            sink, buf = proxy.translated_sink, proxy._translated
        else:
            buf = (VirtualMic(0, 'offline', buffer_ms=1) if kind == 'VirtualMic'
                   else PwCatVirtualMic('offline', buffer_ms=1))
            sink = buf

        def drain():
            block = bytearray(BLOCK)
            if proxy is None:
                buf._audio_callback(block, BLOCK // 4, None, None)
            else:
                proxy._out_callback(block, BLOCK // 4, None, None)
            return bytes(block)
        return SimpleNamespace(sink=sink, buffer=buf, proxy=proxy, drain=drain)

    async def session(self, consumer, count=2, audio=True):
        cfg = _cfg(provider='chatgpt')
        cfg.directions['mine'].source_lang = cfg.directions['mine'].target_lang = 'en'
        cfg.directions['mine'].output_audio = audio
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents(on_text=Mock()))
        engine._virtualmic = consumer.sink
        scfg = cfg.directions['mine'].to_session_config(cfg.session_base)
        scfg._direction = 'mine'
        session = ChatGPTValidatedSession(scfg)
        engine._session = session
        engine._admission = Admission(engine, session)
        session._engine_admission = engine._admission
        verdict = dict(approved=True, language='en', meaning=True, numbers=True,
                       negation=True, complete=True, extra_content=False)
        pcm = {f'line{i}': struct.pack('<h', i * 1000) * 72000 for i in range(1, count + 1)}
        services = SimpleNamespace(
            source_asr=AsyncMock(side_effect=lambda raw, _: f'line{raw[0]}'),
            translate=AsyncMock(side_effect=AssertionError('same-language changed')),
            validate_text=AsyncMock(side_effect=lambda *_: dict(verdict)),
            synthesize=AsyncMock(side_effect=lambda text, _: ('candidate_' + text, pcm[text])),
            audio_asr=AsyncMock(side_effect=lambda raw, _: f'line{struct.unpack("<h", raw[:2])[0] // 1000}'),
            validate_audio=AsyncMock(side_effect=lambda text, heard, _: dict(verdict,
                approved=text == heard, meaning=text == heard)))
        self.enterContext(patch('vlt.session.chatgpt_relay.SubscriptionRelay', return_value=services))
        self.addAsyncCleanup(session.close)
        await session.start(engine._admission.wrap(engine._on_text),
                            engine._admission.wrap(engine._on_audio) if audio else None)
        for i in range(1, count + 1):
            self.assertTrue(session._pipeline.submit('mine', bytes([i, 0]) * 160))
        await self.settle()
        return session, engine, services, pcm

    async def settle(self):
        for _ in range(60):
            await asyncio.sleep(0)

    def drain_candidate(self, consumer, pcm):
        expected = resample_24k_mono_to_48k_stereo(pcm)
        actual = b''.join(consumer.drain() for _ in range(len(expected) // BLOCK))
        self.assertEqual(actual, expected)
        self.assertEqual(consumer.buffer._buf_bytes, 0)

    async def test_approved_successor_waits_and_both_whole_sentences_play(self):
        for kind in ('VirtualMic', 'PwCatVirtualMic', 'MicProxy', 'LinuxMicProxy'):
            with self.subTest(consumer=kind):
                out = self.consumer(kind)
                session, engine, services, pcm = await self.session(out)
                session.tick()
                expected = resample_24k_mono_to_48k_stereo(pcm['line1'])
                self.assertEqual(out.drain(), expected[:BLOCK])
                before = out.buffer._buf_bytes
                session.tick()
                self.assertEqual(session.diagnostics()['finalized'], 1,
                    'a fully approved successor must stay private until the sink has capacity')
                self.assertEqual(len(session._pipeline._ready['mine']), 1)
                self.assertEqual(out.buffer._buf_bytes, before)
                self.assertEqual(engine._text_deltas, 1)
                self.assertEqual(b''.join(out.drain() for _ in range(len(expected)//BLOCK-1)), expected[BLOCK:])
                session.tick()
                self.drain_candidate(out, pcm['line2'])
                self.assertEqual((session.diagnostics()['finalized'], session.diagnostics()['rejected']), (2, 0))
                self.assertEqual(engine._text_deltas, 2)
                self.assertEqual(services.synthesize.call_count, 2, 'waiting must not regenerate audio')
                await session.close()

    async def test_passthrough_does_not_block_captions_on_inaudible_buffer(self):
        for kind in ('MicProxy', 'LinuxMicProxy'):
            out = self.consumer(kind)
            out.proxy._mode = MODE_PASSTHROUGH
            session, engine, _, _ = await self.session(out)
            session.tick()
            session.tick()
            self.assertEqual(session.diagnostics()['finalized'], 2)
            self.assertEqual(engine._text_deltas, 2)
            self.assertTrue(session.is_alive)

    async def test_corrupt_waiting_approval_is_discarded_before_capacity_query(self):
        out = self.consumer('VirtualMic')
        session, engine, _, _ = await self.session(out)
        session.tick()
        approval = session._pipeline._ready['mine'][0]
        object.__setattr__(approval, 'pcm', b'\x00\x00')
        out.sink.can_accept_complete = Mock(side_effect=AssertionError('unverified candidate'))
        session.tick()
        self.assertEqual(session.diagnostics()['rejected'], 1)
        self.assertEqual(session.diagnostics()['last_reason'], 'stale-result')
        out.sink.can_accept_complete.assert_not_called()
        self.assertEqual(engine._text_deltas, 1)

    async def test_capacity_callback_invalidation_cannot_publish_or_abort_on_empty_queue(self):
        out = self.consumer('VirtualMic')
        session, engine, _, _ = await self.session(out, count=1)
        def invalidate(_):
            session._pipeline.invalidate()
            return True
        out.sink.can_accept_complete = invalidate
        session.tick()
        self.assertEqual(engine._text_deltas, 0)
        self.assertEqual(out.buffer._buf_bytes, 0)
        self.assertTrue(session.is_alive)

    async def test_capacity_error_fails_closed_without_exposing_the_caption(self):
        out = self.consumer('VirtualMic')
        session, engine, _, _ = await self.session(out, count=1)
        out.sink.can_accept_complete = Mock(side_effect=RuntimeError('output unavailable'))
        session.tick()
        self.assertFalse(session.is_alive)
        self.assertEqual(session.fail_reason, 'session-output')
        self.assertEqual(engine._text_deltas, 0)
        self.assertEqual(out.buffer._buf_bytes, 0)

    async def test_caption_only_and_legacy_sink_callbacks_keep_working(self):
        out = self.consumer('VirtualMic')
        out.sink.can_accept_complete = Mock(side_effect=AssertionError('no audio expected'))
        session, engine, services, _ = await self.session(out, audio=False)
        session.tick()
        session.tick()
        self.assertEqual(engine._text_deltas, 2)
        services.synthesize.assert_not_called()
        out.sink.can_accept_complete.assert_not_called()
        legacy = SimpleNamespace(sink=SimpleNamespace(push=Mock(), end_sentence=Mock()))
        other, engine, _, _ = await self.session(legacy, count=1)
        other.tick()
        self.assertEqual(engine._text_deltas, 1)
        legacy.sink.push.assert_called_once()

    async def test_capacity_and_publication_exclude_other_thread_rebinding(self):
        out = self.consumer('VirtualMic')
        session, engine, _, _ = await self.session(out, count=1)
        acquired = []
        def probe_lock(*_):
            def contender():
                got = engine._sink_lock.acquire(False)
                acquired.append(got)
                if got:
                    engine._sink_lock.release()
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            return True
        out.sink.can_accept_complete = probe_lock
        engine._events.on_text = probe_lock
        session.tick()
        self.assertEqual(acquired, [False, False])
        self.assertEqual(session.diagnostics()['finalized'], 1)

    async def test_full_ready_queue_waiter_is_joined_on_close(self):
        out = self.consumer('VirtualMic')
        session, engine, services, _ = await self.session(out, count=3)
        pipeline = session._pipeline
        self.assertEqual(len(pipeline._ready['mine']), 2)
        self.assertEqual(len(pipeline._owned), 1)
        self.assertEqual(services.validate_audio.call_count, 3)
        await asyncio.wait_for(session.close(), 1)
        self.assertFalse(pipeline._owned)
        self.assertFalse(pipeline._ready['mine'])
        self.assertFalse(pipeline._pending['mine'])
        self.assertEqual(engine._text_deltas, 0)
        self.assertEqual(out.buffer._buf_bytes, 0)

    async def test_account_change_cannot_release_a_full_queue_waiter(self):
        out = self.consumer('VirtualMic')
        session, engine, _, _ = await self.session(out, count=3)
        self.owner.generation += 1
        session.tick()
        await self.settle()
        session.tick()
        self.assertEqual(session.diagnostics()['rejected'], 3)
        self.assertFalse(session._pipeline._owned)
        self.assertEqual(engine._text_deltas, 0)
        self.assertEqual(out.buffer._buf_bytes, 0)

    async def test_capacity_boundary_does_not_grow_or_modify_the_buffer(self):
        out = self.consumer('VirtualMic')
        session, _, _, pcm = await self.session(out, count=1)
        for size in (0, -4, 3, True, 17_280_004):
            self.assertFalse(out.sink.can_accept_complete(size))
        self.assertTrue(out.sink.can_accept_complete(17_280_000))
        session.tick()
        before = out.buffer._buf_bytes
        self.assertFalse(out.sink.can_accept_complete(4))
        self.assertEqual(out.buffer._buf_bytes, before)
        # Once one second remains, a one-second successor fits the existing two-second limit.
        for _ in range(100):
            out.drain()
        self.assertTrue(out.sink.can_accept_complete(192_000))
        self.assertFalse(out.sink.can_accept_complete(192_004))
        self.assertEqual(out.buffer._buf_bytes, 192_000)


if __name__ == '__main__':
    unittest.main()
