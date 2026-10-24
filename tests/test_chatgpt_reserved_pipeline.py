"""Production coordinator/factory approval and owned remote preparation; offline."""
import asyncio
import unittest
from unittest.mock import AsyncMock
from chatgpt_reserved_fakes import _Harness, PCM, TAIL, TEXT
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator
from vlt.session.chatgpt_http import _RequestNotSent


class CoordinatorTests(_Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                            negation=True, complete=True, extra_content=False)
        self.text_check = asyncio.get_running_loop().create_future()
        self.entered_check = asyncio.Event()
        async def check(*_):
            self.entered_check.set()
            return await self.text_check
        self.base.source_asr = AsyncMock(return_value='明天九點見。')
        self.base.translate = AsyncMock(return_value=dict(translation=TEXT, language='ja'))
        self.base.validate_text = check
        self.base.audio_asr = AsyncMock(return_value=TEXT)
        self.base.validate_audio = AsyncMock(return_value=self.verdict)
        self.reasons, self.text, self.audio = [], [], []
        cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja', output_audio=True)
        self.pipeline = Coordinator({'mine': cfg}, self.base,
            lambda *args:self.reasons.append(args[-1]), lambda:self.owner.generation,
            prepare_voice=self.base.prepare_voice)

    async def prepare_pending(self):
        self.assertTrue(self.pipeline.submit('mine', b'\x01\x00'*160))
        await asyncio.wait_for(self.entered_check.wait(), 1)
        for _ in range(40):
            await asyncio.sleep(.002)
        self.assertEqual(len(self.requests), 1, 'reserve without speech during text validation')
        self.assertIsNone(self.fixture.context)
        self.assertEqual(self.timers, [])

    async def test_real_coordinator_preserves_all_gates_and_both_voice_deadlines(self):
        await self.prepare_pending()
        self.text_check.set_result(self.verdict)
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertTrue(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual(self.audio, [PCM+TAIL])
        self.assertEqual(self.text[0].confirmed, TEXT)
        self.assertEqual(self.reasons, [])
        self.assertEqual(self.timers, [('generation', 120), ('generation', 120), ('ready', 90)])
        self.base.audio_asr.assert_awaited_once_with(PCM+TAIL, 'ja')
        self.base.validate_audio.assert_awaited_once_with(TEXT, TEXT, 'ja')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.fixture.steps.count('context-append'), 1)

    async def test_real_coordinator_rejection_closes_unused_remote_without_generation(self):
        await self.prepare_pending()
        self.text_check.set_result(dict(self.verdict, approved=False))
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual((self.text, self.audio), ([], []))
        self.assertEqual(self.reasons, ['text-rejected'])
        self.assertIsNone(self.fixture.context)
        self.assertTrue(self.fixture.closed)
        self.assertEqual(self.timers, [])

    async def test_invalid_translation_closes_preparation_without_sending_script(self):
        async def invalid_translation(*_):
            await self.requested.wait()
            return dict(translation=TEXT, language='en')
        self.base.translate.side_effect = invalid_translation
        self.assertTrue(self.pipeline.submit('mine', b'\x01\x00'*160))
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(self.reasons, ['translation-invalid'])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.entered_check.is_set())
        self.assertIsNone(self.fixture.context)
        self.assertTrue(self.fixture.closed)
        self.assertEqual(self.timers, [])
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_expired_inflight_cleanup_precedes_both_original_120s_clocks(self):
        self.request_release = asyncio.Event()
        await self.prepare_pending()
        prepared, = self.preparations
        prepared.voice._prepared_at -= 20
        self.text_check.set_result(self.verdict)
        for _ in range(100):
            if prepared.voice.bridge.closing:
                break
            await asyncio.sleep(.002)
        self.assertTrue(prepared.voice.bridge.closing)
        self.assertEqual(self.timers, [])
        self.assertFalse(self.request_cancelled.is_set())
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.request_release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertTrue(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual(self.audio, [PCM+TAIL])
        self.assertEqual(self.timers, [('generation', 120), ('generation', 120), ('ready', 90)])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(sum(f.context is not None for f in self.fixtures), 1)
        self.assertTrue(all(f.closed for f in self.fixtures))

    async def test_revocation_during_text_validation_never_requests_speech(self):
        await self.prepare_pending()
        self.owner.generation += 1
        self.text_check.set_result(self.verdict)
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual(self.reasons, ['stale-result'])
        self.assertIsNone(self.fixture.context)
        self.assertTrue(self.fixture.closed)
        self.assertEqual(self.timers, [])

    async def assert_inflight_failure_is_not_retried(self, failure):
        self.request_release = asyncio.Event()
        self.request_failure = failure
        await self.prepare_pending()
        prepared, = self.preparations
        prepared.voice._prepared_at -= 20
        self.text_check.set_result(self.verdict)
        for _ in range(100):
            if prepared.voice.bridge.closing:
                break
            await asyncio.sleep(.002)
        self.assertTrue(prepared.voice.bridge.closing)
        self.request_release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(len(self.requests), 1, 'failed neutral work must never start a cold live request')
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual((self.text, self.audio), ([], []))
        self.assertTrue(all(f.context is None for f in self.fixtures))
        self.assertEqual(self.timers, [])

    async def test_malformed_reply_during_expiry_cleanup_never_retries(self):
        await self.assert_inflight_failure_is_not_retried('malformed')

    async def test_uncertain_http_during_expiry_cleanup_never_retries(self):
        await self.assert_inflight_failure_is_not_retried('uncertain')

    async def test_unknown_cancelled_http_during_expiry_cleanup_never_retries(self):
        await self.assert_inflight_failure_is_not_retried('cancelled')

    async def assert_proven_unsent_recovers(self, expire, revoke=False):
        self.request_failure = 'unsent'
        if expire:
            self.request_release = asyncio.Event()
        await self.prepare_pending()
        prepared, = self.preparations
        if expire:
            prepared.voice._prepared_at -= 20
        if revoke:
            self.owner.generation += 1
        self.text_check.set_result(self.verdict)
        if expire:
            for _ in range(100):
                if prepared.voice.bridge.closing:
                    break
                await asyncio.sleep(.002)
            self.assertTrue(prepared.voice.bridge.closing)
            self.assertEqual(self.timers, [])
            self.request_release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        if revoke:
            self.assertEqual(len(self.requests), 1)
            self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
            self.assertEqual(self.timers, [])
            self.assertTrue(all(f.context is None for f in self.fixtures))
        else:
            self.assertEqual(len(self.requests), 2, 'unsent neutral request permits one actual cold generation')
            self.assertTrue(self.pipeline.consume('mine', self.text.append, self.audio.append))
            self.assertEqual(self.audio, [PCM+TAIL])
            self.assertEqual(sum(f.context is not None for f in self.fixtures), 1)
            self.assertEqual(self.timers, [('generation', 120), ('generation', 120), ('ready', 90)])

    async def test_proven_unsent_neutral_failure_preserves_cold_recovery(self):
        await self.assert_proven_unsent_recovers(False)

    async def test_proven_unsent_during_expiry_cleanup_preserves_cold_recovery(self):
        await self.assert_proven_unsent_recovers(True)

    async def test_proven_unsent_cannot_override_owner_revocation(self):
        await self.assert_proven_unsent_recovers(False, revoke=True)

    async def test_proven_unsent_cannot_override_unsolicited_pcm(self):
        self.request_failure = 'unsent'
        self.request_release = asyncio.Event()
        await self.prepare_pending()
        prepared, = self.preparations
        await self.clients[0].send(PCM)
        for _ in range(100):
            if prepared.voice.bridge.capture.failure:
                break
            await asyncio.sleep(.002)
        self.assertEqual(prepared.voice.bridge.capture.failure, 'capture-pcm')
        self.request_release.set()
        self.text_check.set_result(self.verdict)
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual(self.timers, [])

    async def test_proven_unsent_rejected_text_never_starts_cold_generation(self):
        self.request_failure = 'unsent'
        await self.prepare_pending()
        self.text_check.set_result(dict(self.verdict, approved=False))
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual(self.timers, [])

    async def test_cold_generation_failure_has_no_recursive_recovery(self):
        self.request_failure = 'unsent-always'
        await self.prepare_pending()
        self.text_check.set_result(self.verdict)
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertTrue(all(f.context is None for f in self.fixtures))

    async def test_later_credentials_failure_is_not_proof_that_live_post_was_unsent(self):
        self.owner.credentials.side_effect = _RequestNotSent('other-operation')
        await self.prepare_pending()
        self.text_check.set_result(self.verdict)
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(len(self.requests), 1, 'proof must belong to the live HTTP request itself')
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual(self.timers, [])



if __name__ == '__main__':
    unittest.main()
