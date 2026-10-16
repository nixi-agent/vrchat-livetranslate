"""假的訂閱 stages 驗證編排與放行；不證明模型能抵抗注入。"""
import asyncio
import sys
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator
from vlt.session import chatgpt_pipeline as pipeline_mod


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.verdict = {'approved': True, 'language': 'ja', 'meaning': True, 'numbers': True,
                        'negation': True, 'complete': True, 'extra_content': False}
        self.services = SimpleNamespace(
            source_asr=AsyncMock(return_value='明天九點見。'),
            translate=AsyncMock(return_value={'translation': '明日九時に会いましょう。', 'language': 'ja'}),
            validate_text=AsyncMock(side_effect=lambda *args: dict(self.verdict)),
            synthesize=AsyncMock(return_value=('candidate_1', bytes(960))),
            audio_asr=AsyncMock(return_value='明日九時に会いましょう。'),
            validate_audio=AsyncMock(side_effect=lambda *args: dict(self.verdict)))
        self.cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja', output_audio=True)
        self.account = 1
        self.reasons = Mock()
        self.pipeline = Coordinator({'mine': self.cfg}, self.services, self.reasons, lambda: self.account)
        self.addAsyncCleanup(self.pipeline.close)
        self.text, self.audio = [], []

    async def settle(self):
        # Fixed fake stages do not use real-time timeouts or an external server.
        for _ in range(40):
            await asyncio.sleep(0)

    def consume(self, direction='mine'):
        return self.pipeline.consume(direction, self.text.append, self.audio.append)

    async def test_normal_whole_pcm_and_caption_appear_once_after_all_checks(self):
        pending = asyncio.Future()

        async def check(*args):
            return await pending

        self.services.validate_audio.side_effect = check
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await self.settle()
        self.assertFalse(self.consume(), 'unchecked PCM and text must both remain private')
        self.assertEqual((self.text, self.audio), ([], []))
        pending.set_result(self.verdict)
        await self.settle()
        self.assertTrue(self.consume(), 'normal validated segments must not be dropped')
        self.assertEqual(self.text[0].confirmed, '明日九時に会いましょう。')
        self.assertEqual(self.audio, [bytes(960)])
        self.assertFalse(self.consume())
        self.assertEqual(len(self.text), 1)
        self.services.audio_asr.assert_awaited_once_with(bytes(960), 'ja')

    async def test_text_only_does_not_generate_or_transcribe_unused_audio(self):
        self.cfg.output_audio = False
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await self.settle()
        self.assertTrue(self.consume())
        self.assertEqual(self.audio, [])
        self.services.synthesize.assert_not_called()
        self.services.audio_asr.assert_not_called()
        self.services.validate_audio.assert_not_called()

    async def test_same_language_preserves_source_and_still_checks_text_and_voice(self):
        self.cfg.source_lang = self.cfg.target_lang = 'zh'
        self.verdict['language'] = 'zh'
        source = '那這個本地用戶認證，明天九點再處理。'
        self.services.source_asr.return_value = source
        self.services.translate.return_value = {'translation': '明天處理驗證。', 'language': 'zh'}
        self.services.audio_asr.return_value = source
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertTrue(self.consume())
        self.assertEqual(self.text[0].confirmed, source)
        self.services.translate.assert_not_awaited()
        self.services.validate_text.assert_awaited_once_with(source, source, 'zh')
        self.services.synthesize.assert_awaited_once_with(source, 'zh')
        self.services.validate_audio.assert_awaited_once_with(source, source, 'zh')

    async def test_same_language_cannot_bypass_text_rejection(self):
        self.cfg.source_lang = self.cfg.target_lang = 'zh'
        self.verdict.update(language='zh', approved=False)
        source = '保留原句。'
        self.services.source_asr.return_value = source
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertFalse(self.consume())
        self.assertEqual((self.text, self.audio), ([], []))
        self.services.validate_text.assert_awaited_once_with(source, source, 'zh')
        self.services.synthesize.assert_not_awaited()

    async def test_empty_source_asr_is_counted_separately_and_never_published(self):
        self.services.source_asr.side_effect = RuntimeError('asr-empty')
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertFalse(self.consume())
        self.assertEqual((self.text, self.audio), ([], []))
        self.assertEqual(self.reasons.call_args.args[-1], 'source-asr-empty')
        self.services.translate.assert_not_awaited()
        self.services.synthesize.assert_not_awaited()

    async def test_uncertain_audio_asr_is_rejected_before_audio_check_and_callbacks(self):
        self.services.audio_asr.side_effect = RuntimeError('asr-uncertain')
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertFalse(self.consume())
        self.assertEqual(self.reasons.call_args.args[-1], 'audio-asr-uncertain')
        self.assertEqual((self.text, self.audio), ([], []))
        self.services.validate_audio.assert_not_awaited()

    async def test_empty_voice_asr_and_non_asr_errors_do_not_release_outputs(self):
        self.services.audio_asr.side_effect = RuntimeError('asr-empty')
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertFalse(self.consume())
        self.assertEqual(self.reasons.call_args.args[-1], 'audio-asr-empty')
        self.assertEqual((self.text, self.audio), ([], []))
        self.services.audio_asr.side_effect = None
        self.services.translate.side_effect = RuntimeError('asr-empty')
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertFalse(self.consume())
        self.assertEqual(self.reasons.call_args.args[-1], 'translation-failed')

    async def test_same_language_converts_script_without_replacing_regional_terms(self):
        self.cfg.source_lang = self.cfg.target_lang = 'zh'
        self.cfg.output_audio = False
        self.verdict['language'] = 'zh'
        self.services.source_asr.return_value = '本地用户认证和软件。'
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertTrue(self.consume())
        self.assertEqual(self.text[-1].confirmed, '本地用戶認證和軟件。')
        self.assertEqual(self.text[-1].source, self.text[-1].confirmed)

    async def test_glossary_and_unknown_source_language_keep_the_translation_stage(self):
        self.cfg.output_audio = False
        self.cfg.target_lang = 'zh'
        self.verdict['language'] = 'zh'
        self.services.translate.return_value = {'translation': '術語。', 'language': 'zh'}
        for source_lang, vocabulary, source in (('zh', {'詞條': '術語'}, '明天詞條九點見。'),
                                                (None, {}, '明天九點見。')):
            with self.subTest(source_lang=source_lang):
                self.cfg.source_lang = source_lang
                self.cfg.hotwords = vocabulary
                self.services.source_asr.return_value = source
                self.services.translate.reset_mock()
                self.pipeline.submit('mine', bytes(320))
                await self.settle()
                self.assertTrue(self.consume())
                self.services.translate.assert_awaited_once_with(source, 'zh', vocabulary)

    async def test_unrelated_global_terms_do_not_rewrite_same_language(self):
        self.cfg.source_lang = self.cfg.target_lang = 'zh'
        self.cfg.hotwords = {'VRChat': 'VRChat', '逆袭': 'Nixi'}
        self.cfg.output_audio = False
        self.verdict['language'] = 'zh'
        source = '那這個本地用戶認證，明天九點再處理。'
        self.services.source_asr.return_value = source
        self.services.translate.return_value = {'translation': '明天處理驗證。', 'language': 'zh'}
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        self.assertTrue(self.consume())
        self.assertEqual(self.text[-1].confirmed, source)
        self.services.translate.assert_not_awaited()
        self.services.validate_text.assert_awaited_once_with(source, source, 'zh')

    async def test_related_terms_match_chinese_script_and_case(self):
        self.cfg.source_lang = self.cfg.target_lang = 'zh'
        self.cfg.output_audio = False
        self.verdict['language'] = 'zh'
        self.services.translate.return_value = {'translation': '術語。', 'language': 'zh'}
        for source, terms in (('逆襲今天到了。', {'逆袭': 'Nixi'}),
                              ('今天在vrchat測試。', {'VRChat': 'VRChat'})):
            with self.subTest(source=source):
                self.cfg.hotwords = terms
                self.services.source_asr.return_value = source
                self.services.translate.reset_mock()
                self.pipeline.submit('mine', bytes(320))
                await self.settle()
                self.assertTrue(self.consume())
                self.services.translate.assert_awaited_once_with(source, 'zh', terms)

    async def test_real_relay_parsers_reject_then_recover_without_early_caption(self):
        from vlt.session.chatgpt_auth import Credentials
        from vlt.session.chatgpt_relay import SubscriptionRelay
        from test_chatgpt_text import response, verdict

        owner = SimpleNamespace(generation=1, require_usable=lambda: None,
                                credentials=AsyncMock(return_value=Credentials('fake-account', 'fake-access', 1)))
        self.pipeline._services = SubscriptionRelay(owner)
        self.cfg.output_audio = False
        for approved in (False, True):
            replies = [b'{"text":"source"}',
                       response({'translation': 'nine', 'language': 'ja'}), response(verdict(approved))]
            with patch('vlt.session.chatgpt_relay.request', side_effect=replies) as call:
                self.assertTrue(self.pipeline.submit('mine', bytes(320)))
                self.assertFalse(self.consume(), 'even a valid source request cannot publish early')
                await asyncio.gather(*tuple(self.pipeline._owned))
                self.assertEqual(self.consume(), approved)
                self.assertEqual(call.call_count, 3)
        self.assertEqual(len(self.text), 1)
        self.assertEqual(self.audio, [])
        self.assertIn('text-rejected', [call.args[2] for call in self.reasons.call_args_list])

    async def test_bad_verdict_no_source_or_extra_answer_never_reaches_callbacks(self):
        for bad in ({**self.verdict, 'extra_content': True},
                    {**self.verdict, 'numbers': False}, {'approved': True},
                    {**self.verdict, 'language': 'en'}):
            self.verdict = bad
            self.assertTrue(self.pipeline.submit('mine', bytes(320)))
            await self.settle()
            self.assertFalse(self.consume())
        self.services.source_asr.return_value = ''
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await self.settle()
        self.assertFalse(self.consume())
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_two_pending_limit_and_true_repetition(self):
        hold = asyncio.Event()

        async def asr(*args):
            await hold.wait()
            hold.clear()
            return '明天九點見。'

        self.services.source_asr.side_effect = asr
        for _ in range(3):
            self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        self.assertFalse(self.pipeline.submit('mine', bytes(320)))
        # Consume between finished jobs to keep output backpressure meaningful.
        for _ in range(3):
            hold.set()
            await self.settle()
            self.assertTrue(self.consume())
        self.assertEqual(len(self.text), 3, 'real repeated source sections must each be released')
        self.assertEqual(len(self.audio), 3)
        self.assertTrue(any(c.args[-1] == 'queue-overload' for c in self.reasons.call_args_list))

    async def test_completed_output_queue_is_bounded_without_a_consumer(self):
        for _ in range(3):
            self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await asyncio.gather(*tuple(self.pipeline._owned))
        self.assertEqual(len(self.pipeline._ready['mine']), 2)
        self.assertIn('output-overload', [call.args[2] for call in self.reasons.call_args_list])
        self.assertTrue(self.consume())
        self.assertTrue(self.consume())
        self.assertFalse(self.consume())

    async def test_old_queued_results_account_and_language_changes_are_dropped(self):
        for change in (lambda: setattr(self, 'account', self.account + 1),
                       self.pipeline.invalidate,
                       lambda: setattr(self.cfg, 'target_lang', 'en')):
            self.cfg.target_lang = 'ja'
            self.assertTrue(self.pipeline.submit('mine', bytes(320)))
            await self.settle()
            change()
            self.assertFalse(self.consume())
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_stop_discards_late_result_and_releases_owned_tasks(self):
        entered = asyncio.Event()

        async def late(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return '明天九點見。'

        self.services.source_asr.side_effect = late
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await entered.wait()
        await self.pipeline.close()
        self.assertFalse(self.consume())
        self.assertFalse(self.pipeline.submit('mine', bytes(320)))
        self.services.translate.assert_not_called()

    async def test_both_directions_do_not_block_or_cross_deliver(self):
        other = SessionConfig(provider='chatgpt', source_lang='ja', target_lang='ja', output_audio=False)
        pipeline = Coordinator({'mine': self.cfg, 'theirs': other}, self.services,
                               self.reasons, lambda: self.account)
        self.addAsyncCleanup(pipeline.close)
        entered, release = asyncio.Event(), asyncio.Event()

        async def asr(pcm, language):
            if language == 'zh':
                entered.set()
                await release.wait()
            return '明天九點見。'

        self.services.source_asr.side_effect = asr
        pipeline.submit('mine', bytes(320))
        await entered.wait()
        pipeline.submit('theirs', bytes(640))
        await self.settle()
        theirs_text, theirs_audio = [], []
        self.assertTrue(pipeline.consume('theirs', theirs_text.append, theirs_audio.append))
        self.assertEqual(len(theirs_text), 1)
        self.assertEqual(theirs_audio, [])
        self.assertFalse(pipeline.consume('mine', self.text.append, self.audio.append))
        release.set()

    async def test_timeout_and_pcm_corruption_never_release(self):
        async def delayed(*args):
            await asyncio.Event().wait()

        original_stage = self.pipeline._stage

        async def timed_stage(section, name, operation, *args):
            with patch.object(pipeline_mod, '_STAGE_TIMEOUT', .05 if name == 'text-check' else 35):
                return await original_stage(section, name, operation, *args)

        self.services.validate_text.side_effect = delayed
        rejected = asyncio.Event()
        self.reasons.side_effect = lambda *args: rejected.set()
        with patch.object(self.pipeline, '_stage', side_effect=timed_stage):
            self.pipeline.submit('mine', bytes(320))
            await asyncio.wait_for(rejected.wait(), 2)
        self.assertFalse(self.consume())
        self.assertTrue(any(c.args[-1] == 'text-check-timeout' for c in self.reasons.call_args_list))
        self.services.validate_text.side_effect = lambda *args: self.verdict
        self.pipeline.submit('mine', bytes(320))
        await self.settle()
        approval = self.pipeline._ready['mine'][0]
        object.__setattr__(approval, 'pcm', b'\x01\x00')
        self.assertFalse(self.consume(), 'same approved PCM hash is checked again at dequeue')

    async def test_stage_cannot_turn_timeout_cancellation_into_an_approved_result(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def overdue(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return '明天九點見。'

        self.services.source_asr.side_effect = overdue
        loop = asyncio.get_running_loop()
        # A coarse Windows clock may dispatch timers before their nominal deadline.
        with patch.object(loop, '_clock_resolution', .05), patch.object(pipeline_mod, '_STAGE_TIMEOUT', .02):
            self.pipeline.submit('mine', bytes(320))
            await entered.wait()
            await cancelled.wait()
            await asyncio.gather(*tuple(self.pipeline._owned))
            self.assertEqual(self.services.translate.call_count, 0, 'late source must not start translation')
            self.assertIn('source-asr-timeout', [call.args[2] for call in self.reasons.call_args_list])
        self.assertFalse(self.consume())

    async def test_stop_between_caption_and_audio_prevents_late_playback(self):
        self.pipeline.submit('mine', bytes(320))
        await self.settle()

        def caption(delta):
            self.text.append(delta)
            self.pipeline.invalidate()

        self.assertTrue(self.pipeline.consume('mine', caption, self.audio.append))
        self.assertEqual(len(self.text), 1)
        self.assertEqual(self.audio, [])

    def test_qwen_configuration_cannot_enter_subscription_pipeline(self):
        with self.assertRaisesRegex(ValueError, '^provider-invalid$'):
            Coordinator({'mine': SessionConfig(provider='qianwen')}, self.services,
                        self.reasons, lambda: self.account)

    async def test_source_feed_waits_for_boundary_and_preserves_whole_pcm(self):
        voiced = struct.pack('<h', 500) * 1600
        self.assertEqual(self.pipeline.feed('mine', voiced), 0)
        await self.settle()
        self.services.source_asr.assert_not_called()
        self.assertEqual(self.pipeline.feed('mine', bytes(16000)), 1)
        await self.settle()
        self.services.source_asr.assert_awaited_once_with(voiced + bytes(16000), 'zh')
        self.assertTrue(self.consume())

    async def test_language_change_discards_partial_source_before_new_frame(self):
        self.pipeline.feed('mine', struct.pack('<h', 500) * 100)
        self.cfg.target_lang = 'en'
        self.pipeline.feed('mine', bytes(16000))
        await self.settle()
        self.assertEqual(self.services.source_asr.call_count, 0, 'old partial PCM must be discarded')

    async def test_other_direction_language_change_invalidates_ready_result(self):
        other = SessionConfig(provider='chatgpt', source_lang='ja', target_lang='ja')
        pipeline = Coordinator({'mine': self.cfg, 'theirs': other}, self.services,
                               self.reasons, lambda: self.account)
        self.addAsyncCleanup(pipeline.close)
        pipeline.submit('mine', bytes(320))
        await self.settle()
        other.target_lang = 'en'
        self.assertFalse(pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_reset_cannot_start_another_request_until_cancelled_stage_finishes(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def asr(*args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return '明天九點見。'

        self.services.source_asr.side_effect = asr
        self.pipeline.submit('mine', bytes(320))
        await entered.wait()
        self.pipeline.invalidate()
        await cancelled.wait()
        try:
            self.assertFalse(self.pipeline.submit('mine', bytes(320)), 'cancelled request still owns the direction')
            self.assertEqual(self.services.source_asr.call_count, 1)
        finally:
            release.set()
            await self.settle()
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await self.settle()
        self.assertTrue(self.consume(), 'new normal segment must recover after reset')

    async def test_cancel_before_worker_starts_does_not_leave_direction_busy(self):
        self.pipeline.submit('mine', bytes(320))
        self.pipeline.invalidate()
        await self.settle()
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await self.settle()
        self.assertTrue(self.consume())

    async def test_repeated_cancel_during_close_waits_for_owned_stage_cleanup(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def asr(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        self.services.source_asr.side_effect = asr
        self.pipeline.submit('mine', bytes(320))
        await entered.wait()
        closing = asyncio.create_task(self.pipeline.close())
        await cleaning.wait()
        try:
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(closing.done(), 'close must reap owned work before propagating cancellation')
            self.assertFalse(self.consume())
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
        self.assertFalse(self.pipeline._owned)


if __name__ == '__main__':
    unittest.main()
