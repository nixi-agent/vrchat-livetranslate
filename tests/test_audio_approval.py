"""Offline audio approval at actual Windows/Linux consumers; no devices or services."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from audio_test_fakes import no_audio_device

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_output import scope
from vlt.output.virtualmic import VirtualMic, PwCatVirtualMic, resample_24k_mono_to_48k_stereo
from vlt.output.micproxy import MicProxy, MODE_TRANSLATED, MODE_PASSTHROUGH, _TranslatedBuffer
from vlt.output.micproxy_linux import LinuxMicProxy

BLOCK = 3840
PCM = b'\x11\x22' * (BLOCK // 2)


class Approval:
    def __init__(self, direction='mine'):
        self.valid = True
        self.direction = direction

    def __call__(self, direction=None):
        return self.valid and (direction is None or direction == self.direction)


class AudioApprovalTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(no_audio_device())

    def consumers(self):
        vm = VirtualMic(device_index=0, device_name='unused', buffer_ms=1)
        def drain_vm():
            out = bytearray(BLOCK)
            vm._audio_callback(out, BLOCK // 4, None, None)
            return bytes(out)
        yield 'VirtualMic', vm, drain_vm
        for cls in (MicProxy, LinuxMicProxy):
            proxy = cls(audio_cfg={})
            proxy._mode = MODE_TRANSLATED
            proxy._translated = _TranslatedBuffer(device_index=0, device_name='unused', buffer_ms=1)
            def drain(proxy=proxy):
                out = bytearray(BLOCK)
                proxy._out_callback(out, BLOCK // 4, None, None)
                return bytes(out)
            yield cls.__name__, proxy._translated, drain

    def test_each_consumer_discards_revoked_remaining_audio(self):
        for name, vm, drain in self.consumers():
            with self.subTest(consumer=name):
                approval = Approval()
                with scope(approval):
                    vm.push(PCM * 2)
                    vm.end_sentence()
                self.assertEqual(drain(), PCM)
                approval.valid = False
                self.assertEqual(drain(), bytes(BLOCK))
                self.assertEqual(vm._buf_bytes, 0)
                self.assertFalse(vm._head_started)

    def test_invalid_head_does_not_discard_fresh_approved_tail(self):
        for name, vm, drain in self.consumers():
            with self.subTest(consumer=name):
                stale, fresh = Approval(), Approval()
                with scope(stale):
                    vm.push(PCM)
                    vm.end_sentence()
                with scope(fresh):
                    vm.push(b'\x33\x44' * (BLOCK // 2))
                    vm.end_sentence()
                stale.valid = False
                self.assertEqual(drain(), b'\x33\x44' * (BLOCK // 2))
                self.assertEqual(drain(), bytes(BLOCK))

    def test_other_direction_never_enters_virtual_mic(self):
        for name, vm, drain in self.consumers():
            with self.subTest(consumer=name):
                with scope(Approval('theirs')):
                    vm.push(PCM)
                self.assertEqual(vm._buf_bytes, 0)
                self.assertEqual(drain(), bytes(BLOCK))

    def test_pipe_writer_discards_before_actual_fake_pipe_write(self):
        vm = PwCatVirtualMic(target='offline-test', buffer_ms=1)
        approval = Approval()
        with scope(approval):
            vm.push(PCM)
            vm.end_sentence()
        approval.valid = False
        writes = []
        def write(data):
            writes.append(data)
            vm._stop.set()
            return len(data)
        vm._proc = SimpleNamespace(stdin=SimpleNamespace(write=write, flush=Mock()))
        vm._writer_loop()
        self.assertEqual(writes, [bytes(vm._chunk)])
        self.assertEqual(vm._buf_bytes, 0)

    def test_close_releases_started_sentence_state(self):
        for name, vm, drain in self.consumers():
            with self.subTest(consumer=name):
                vm.push(PCM * 2)
                self.assertEqual(drain(), PCM)
                self.assertTrue(vm._head_started)
                vm.close()
                self.assertFalse(vm._head_started)
                self.assertEqual(vm._buf_bytes, 0)

    def test_engine_rejects_audio_approval_for_another_direction(self):
        from vlt.engine import Engine, EngineEvents
        from test_repeat_guard import _cfg

        cfg = _cfg()
        cfg.directions['theirs'] = cfg.directions['mine']
        engine = Engine(cfg=cfg, direction='theirs', source='loopback', sinks=set(), events=EngineEvents())
        engine._virtualmic = Mock()
        with scope(Approval('mine')):
            engine._on_audio(PCM)
        engine._virtualmic.push.assert_not_called()
        self.assertEqual(engine._audio_chunks, 0)

    def test_direct_pipe_close_releases_started_sentence_state(self):
        vm = PwCatVirtualMic(target='offline-test', buffer_ms=1)
        vm.push(PCM * 2)
        vm.end_sentence()
        out = bytearray(BLOCK)
        vm._audio_callback(out, BLOCK // 4, None, None)
        self.assertEqual(bytes(out), PCM)
        self.assertTrue(vm._head_started)
        self.assertEqual(vm._buf_bytes, BLOCK)
        vm.close()
        self.assertFalse(vm._head_started)
        self.assertEqual(vm._buf_bytes, 0)
        self.assertFalse(vm._primed)
        vm.close()
        self.assertFalse(vm._head_started)


class CoordinatorAudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from vlt.session.base import SessionConfig
        from vlt.session.chatgpt_pipeline import Coordinator

        verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                       negation=True, complete=True, extra_content=False)
        self.pcm = b'\x11\x22' * 1000
        services = SimpleNamespace(source_asr=AsyncMock(return_value='source'),
            translate=AsyncMock(return_value=dict(translation='translation', language='ja')),
            validate_text=AsyncMock(return_value=verdict),
            synthesize=AsyncMock(return_value=('candidate', self.pcm)),
            audio_asr=AsyncMock(return_value='translation'),
            validate_audio=AsyncMock(return_value=verdict))
        self.pipeline = Coordinator({'mine': SessionConfig(provider='chatgpt', source_lang='zh',
            target_lang='ja', output_audio=True)}, services, Mock(), lambda: 1)
        self.addAsyncCleanup(self.pipeline.close)
        self.vm = VirtualMic(device_index=0, device_name='unused', buffer_ms=1)
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await asyncio.gather(*tuple(self.pipeline._owned))

    def audio(self, pcm):
        self.vm.push(resample_24k_mono_to_48k_stereo(pcm))

    def drain(self):
        out = bytearray(BLOCK)
        self.vm._audio_callback(out, BLOCK // 4, None, None)
        return bytes(out)

    async def test_actual_coordinator_closure_revokes_buffered_pcm(self):
        self.assertTrue(self.pipeline.consume('mine', Mock(), self.audio))
        self.assertEqual(self.vm._buf_bytes, len(self.pcm) * 4)
        self.pipeline.invalidate()
        self.assertEqual(self.drain(), bytes(BLOCK))
        self.assertEqual(self.vm._buf_bytes, 0)

    async def test_approved_pcm_is_consumed_once_without_losing_short_tail(self):
        text = Mock()
        self.assertTrue(self.pipeline.consume('mine', text, self.audio))
        self.vm.end_sentence()
        rendered = resample_24k_mono_to_48k_stereo(self.pcm)
        blocks = (len(rendered) + BLOCK - 1) // BLOCK
        actual = b''.join(self.drain() for _ in range(blocks))
        self.assertEqual(actual, rendered + bytes(blocks * BLOCK - len(rendered)))
        self.assertFalse(self.pipeline.consume('mine', text, self.audio))
        text.assert_called_once()
        self.assertEqual(self.drain(), bytes(BLOCK))

    async def test_text_callback_invalidation_prevents_audio_enqueue(self):
        self.pipeline.consume('mine', lambda _: self.pipeline.invalidate(), self.audio)
        self.assertEqual(self.vm._buf_bytes, 0)
        self.assertEqual(self.drain(), bytes(BLOCK))


class CompleteCandidatePlaybackTests(unittest.TestCase):
    setUp = AudioApprovalTests.setUp

    def consumers(self):
        yield from AudioApprovalTests.consumers(self)
        vm = PwCatVirtualMic(target='offline-test', buffer_ms=1)
        def drain():
            out = bytearray(BLOCK)
            vm._audio_callback(out, BLOCK//4, None, None)
            return bytes(out)
        yield 'PwCatVirtualMic', vm, drain

    def engine(self, sink):
        from test_repeat_guard import _cfg
        from vlt.engine import Engine, EngineEvents
        from vlt.session.chatgpt_admission import Admission
        cfg = _cfg(provider='chatgpt')
        cfg.directions['mine'].output_audio = True
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents())
        engine._virtualmic = sink
        engine._session = SimpleNamespace(cfg=cfg.directions['mine'].to_session_config(cfg.session_base))
        engine._admission = Admission(engine, engine._session)
        return engine

    def emit(self, engine, pcm):
        from vlt.session.base import TextDelta
        with scope(Approval()):
            engine._admission.wrap(engine._on_text)(TextDelta(confirmed='translation', is_final=True))
            engine._admission.wrap(engine._on_audio)(pcm)

    def test_full_candidate_preserves_started_sentence_under_backlog(self):
        first, queued = b'\x11\x22' * (24000 * 12), b'\x33\x44' * (24000 * 12)
        rendered = resample_24k_mono_to_48k_stereo(first)
        for name, vm, drain in self.consumers():
            with self.subTest(consumer=name):
                engine = self.engine(vm)
                self.emit(engine, first)
                self.assertEqual(drain(), rendered[:BLOCK])
                self.emit(engine, queued)
                # A complete queued sentence may be dropped; the playing sentence must finish.
                remaining = b''.join(drain() for _ in range(len(rendered) // BLOCK - 1))
                self.assertEqual(remaining, rendered[BLOCK:])
                self.assertEqual(drain(), bytes(BLOCK))
                self.assertEqual(vm._buf_bytes, 0)
                # A genuine later repetition remains a new, playable segment.
                self.emit(engine, first)
                self.assertEqual(b''.join(drain() for _ in range(len(rendered)//BLOCK)), rendered)

    def test_short_complete_candidate_drains_tail_without_idle_timeout(self):
        raw = b'\x11\x22' * 1000
        rendered = resample_24k_mono_to_48k_stereo(raw)
        vm = VirtualMic(device_index=0, device_name='unused', buffer_ms=300)
        self.emit(self.engine(vm), raw)
        # The exact finalized packet is shorter than both the priming and callback sizes.
        with patch('vlt.output.virtualmic.time.monotonic', return_value=vm._last_push_ts):
            out = bytearray(96000)
            vm._audio_callback(out, len(out)//4, None, None)
        self.assertEqual(bytes(out), rendered + bytes(len(out)-len(rendered)))
        self.assertEqual(vm._buf_bytes, 0)

    def test_started_complete_sentence_survives_unsealed_overload_until_boundary(self):
        raw = b'\x11\x22' * (24000 * 12)
        rendered = resample_24k_mono_to_48k_stereo(raw)
        partial = b'\x33\x44' * (BLOCK // 2)
        for name, vm, drain in self.consumers():
            with self.subTest(consumer=name):
                engine = self.engine(vm)
                self.emit(engine, raw)
                self.assertEqual(drain(), rendered[:BLOCK])
                for _ in range(32):
                    vm.push(partial)
                    self.assertEqual(vm._buf_bytes, len(rendered) - BLOCK)
                self.assertEqual(b''.join(drain() for _ in range(len(rendered)//BLOCK-1)),
                                 rendered[BLOCK:])
                vm.push(partial)
                self.assertEqual(drain(), bytes(BLOCK))
                vm.end_sentence()
                vm.push(partial)
                vm.end_sentence()
                self.assertEqual(drain(), partial)
                self.emit(engine, raw)
                self.assertEqual(drain(), rendered[:BLOCK])
                vm.push(partial)
                engine._revoke_output()
                self.assertEqual(drain(), bytes(BLOCK))
                vm.push(partial)
                vm.end_sentence()
                self.assertEqual(drain(), partial)

    def test_new_complete_candidate_ends_discarded_partial_sentence(self):
        vm = VirtualMic(device_index=0, device_name='unused', buffer_ms=1)
        engine = self.engine(vm)
        raw = b'\x11\x22' * (24000 * 12)
        self.emit(engine, raw)
        out = bytearray(BLOCK)
        vm._audio_callback(out, BLOCK//4, None, None)
        vm.push(b'\x33\x44' * (BLOCK//2))
        for _ in range(len(raw)*4//BLOCK-1):
            vm._audio_callback(out, BLOCK//4, None, None)
        vm.push(b'\x33\x44' * (BLOCK//2))
        vm._audio_callback(out, BLOCK//4, None, None)
        self.assertEqual(bytes(out), bytes(BLOCK))
        short = b'\x55\x66' * 1000
        self.emit(engine, short)
        expected = resample_24k_mono_to_48k_stereo(short)
        out = bytearray(len(expected))
        vm._audio_callback(out, len(out)//4, None, None)
        self.assertEqual(bytes(out), expected)

        self.emit(engine, raw)
        out = bytearray(BLOCK)
        vm._audio_callback(out, BLOCK//4, None, None)
        vm.push(b'\x33\x44' * (BLOCK//2))
        vm.close()
        vm.push(expected)
        vm.end_sentence()
        out = bytearray(len(expected))
        vm._audio_callback(out, len(out)//4, None, None)
        self.assertEqual(bytes(out), expected)

    def test_mode_switch_recovers_legacy_partial_after_overload(self):
        for cls in (MicProxy, LinuxMicProxy):
            with self.subTest(proxy=cls.__name__):
                proxy = cls(audio_cfg={})
                proxy._translated = _TranslatedBuffer(device_index=0, device_name='unused', buffer_ms=1)
                proxy.set_translation_active(True)
                self.assertTrue(proxy.set_mode(MODE_TRANSLATED))
                self.emit(self.engine(proxy.translated_sink), b'\x11\x22'*(24000*12))
                out = bytearray(BLOCK)
                proxy._out_callback(out, BLOCK//4, None, None)
                partial = b'\x33\x44'*(BLOCK//2)
                proxy.translated_sink.push(partial)
                self.assertTrue(proxy.set_mode(MODE_PASSTHROUGH))
                self.assertTrue(proxy.set_mode(MODE_TRANSLATED))
                proxy.translated_sink.push(partial)
                proxy.translated_sink.end_sentence()
                proxy._out_callback(out, BLOCK//4, None, None)
                self.assertEqual(bytes(out), partial)

    def test_direct_pipe_close_clears_partial_discard_state(self):
        vm = PwCatVirtualMic(target='offline-test', buffer_ms=1)
        self.emit(self.engine(vm), b'\x11\x22'*(24000*12))
        out = bytearray(BLOCK)
        vm._audio_callback(out, BLOCK//4, None, None)
        vm.push(b'\x33\x44'*(BLOCK//2))
        vm.close()
        self.assertFalse(vm._dropping_sentence)

    def test_complete_candidate_limit_and_revocation(self):
        for count, accepted in ((2_160_000, True), (2_160_001, False)):
            with self.subTest(samples=count):
                raw = b'\x11\x22' * count
                vm = VirtualMic(device_index=0, device_name='unused', buffer_ms=1)
                engine = self.engine(vm)
                self.emit(engine, raw)
                self.assertEqual(vm._buf_bytes, len(raw)*4 if accepted else 0)
                engine._revoke_output()
                out = bytearray(BLOCK)
                vm._audio_callback(out, BLOCK//4, None, None)
                self.assertEqual(bytes(out), bytes(BLOCK))
                self.assertEqual(vm._buf_bytes, 0)

    def test_partial_pushes_of_one_approved_packet_keep_streaming_boundaries(self):
        raw = b'\x11\x22' * 1000
        vm = VirtualMic(device_index=0, device_name='unused', buffer_ms=300)
        engine = self.engine(vm)
        rendered = resample_24k_mono_to_48k_stereo(raw)
        def parts(pcm):
            stereo = resample_24k_mono_to_48k_stereo(pcm)
            vm.push(stereo[:len(stereo)//2])
            vm.push(stereo[len(stereo)//2:])
        with scope(Approval()):
            engine._admission.wrap(parts)(raw)
        self.assertFalse(any(end for _, end, _ in vm._buf))
        vm.end_sentence()
        out = bytearray(len(rendered))
        vm._audio_callback(out, len(out)//4, None, None)
        self.assertEqual(bytes(out), rendered)


if __name__ == "__main__":
    unittest.main()
