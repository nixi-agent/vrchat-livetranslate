"""訂閱轉錄的離線請求契約；只有假憑證與合成 PCM，不呼叫服務。"""
import asyncio
from email import policy
from email.message import Message
from email.parser import BytesParser
import io
import json
import sys
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_relay import SubscriptionRelay
from vlt.session.chatgpt_auth import Credentials


class ASRTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = SimpleNamespace(generation=2, require_usable=lambda: None,
                                     credentials=AsyncMock(return_value=Credentials('fake-account', 'fake-access', 2)))
        self.asr = SubscriptionRelay(self.owner)

    async def test_unpadded_source_and_blind_audio_send_exact_full_pcm_and_only_language(self):
        for operation, rate in ((self.asr.source_asr, 16000), (self.asr.audio_asr, 24000)):
            pcm = b'\x12\x00' * 768
            with patch('vlt.session.chatgpt_relay.request', return_value=b'{"text":"\u539f\u6587"}') as call:
                self.assertEqual(await operation(pcm, 'zh'), '原文')
            self.assertEqual(call.call_count, 1 if rate == 16000 else 3)
            for request_args in call.call_args_list:
                name, body, headers = request_args.args
                self.assertEqual(name, 'transcribe')
                self.assertEqual(headers['Authorization'], 'Bearer fake-access')
                self.assertEqual(headers['ChatGPT-Account-Id'], 'fake-account')
                message = BytesParser(policy=policy.default).parsebytes(
                    ('Content-Type: ' + headers['Content-Type'] + '\r\n\r\n').encode() + body)
                parts = list(message.iter_parts())
                self.assertEqual([p.get_param('name', header='Content-Disposition') for p in parts], ['file', 'language'])
                self.assertEqual(parts[1].get_payload(decode=True), b'zh')
                with wave.open(io.BytesIO(parts[0].get_payload(decode=True)), 'rb') as audio:
                    self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (rate, 1, 2))
                    self.assertEqual(audio.readframes(audio.getnframes()), pcm)

    async def test_automatic_source_language_is_omitted_and_no_transcript_hint_exists(self):
        with patch('vlt.session.chatgpt_relay.request', return_value=b'{"text":"text"}') as call:
            await self.asr.source_asr(b'\x00\x00', None)
        body = call.call_args.args[1]
        self.assertNotIn(b'name="language"', body)
        self.assertNotIn(b'prompt', body)
        self.assertNotIn(b'expected', body)
        with self.assertRaises(TypeError):
            await self.asr.audio_asr(b'\x00\x00', 'ja', expected='answer')

    async def test_blind_audio_uses_fixed_three_reads_and_exact_transcript_majority(self):
        texts = ['本地用戶對象。', '本地用戶認證。', '本地用戶認證。']
        with patch('vlt.session.chatgpt_relay.request',
                   side_effect=[json.dumps({'text': text}).encode() for text in texts]) as call:
            self.assertEqual(await self.asr.audio_asr(b'\x12\x00' * 768, 'zh'), texts[1])
        self.assertEqual(call.call_count, 3)
        for args in call.call_args_list:
            self.assertNotIn(b'prompt', args.args[1])
            self.assertNotIn(b'expected', args.args[1])

    async def test_blind_audio_disagreement_does_not_merge_numbers_negation_or_script(self):
        for texts in (['9', '09', '９'], ['不去', '去', '不要去'], ['幹', '乾', '干']):
            with self.subTest(texts=texts), patch('vlt.session.chatgpt_relay.request',
                    side_effect=[json.dumps({'text': text}).encode() for text in texts]) as call:
                with self.assertRaisesRegex(RuntimeError, '^asr-uncertain$'):
                    await self.asr.audio_asr(b'\x00\x00', 'zh')
            self.assertEqual(call.call_count, 3)

    async def test_blind_audio_malformed_third_result_cannot_release_an_early_majority(self):
        with patch('vlt.session.chatgpt_relay.request', side_effect=[
                b'{"text":"same"}', b'{"text":"same"}', b'{"text":"same","tool":{}}']) as call:
            with self.assertRaisesRegex(RuntimeError, '^asr-result$'):
                await self.asr.audio_asr(b'\x00\x00', 'en')
        self.assertEqual(call.call_count, 3)

    async def test_blind_audio_generation_change_between_reads_prevents_the_next_upload(self):
        async def switched(*args):
            self.owner.generation += 1
            return 'same'

        with patch.object(self.asr, '_transcribe', side_effect=switched) as call:
            with self.assertRaisesRegex(RuntimeError, '^asr-stale$'):
                await self.asr.audio_asr(b'\x00\x00', 'en')
        self.assertEqual(call.call_count, 1)

    async def test_blind_audio_cancellation_between_reads_prevents_the_next_upload(self):
        async def cancelled(*args):
            asyncio.current_task().cancel()
            return 'same'

        with patch.object(self.asr, '_transcribe', side_effect=cancelled) as call:
            task = asyncio.create_task(self.asr.audio_asr(b'\x00\x00', 'en'))
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertEqual(call.call_count, 1)

    async def test_blind_audio_group_timeout_cancels_all_inflight_reads(self):
        from vlt.session.base import SessionConfig
        from vlt.session import chatgpt_pipeline as pipeline

        cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja', output_audio=True)
        coordinator = pipeline.Coordinator({'mine': cfg}, self.asr, Mock(), lambda: self.owner.generation)
        pcm = b'\x00\x00'
        section = pipeline._Section('mine', 'test', coordinator._epoch, self.owner.generation,
                                    pipeline._policy(cfg), pcm, pipeline._hash(pcm))
        cancelled = asyncio.Event()
        calls = 0
        cancellations = 0

        async def slow_read(*args):
            nonlocal calls, cancellations
            calls += 1
            if calls == 1:
                return 'same'
            try:
                await asyncio.Event().wait()
            finally:
                cancellations += 1
                cancelled.set()

        try:
            with patch.object(self.asr, '_transcribe', side_effect=slow_read), \
                 patch.object(pipeline, '_STAGE_TIMEOUT', .02):
                with self.assertRaisesRegex(pipeline._Rejected, '^audio-asr-timeout$'):
                    await coordinator._stage(section, 'audio-asr', self.asr.audio_asr, pcm, 'ja')
            self.assertEqual(calls, 3)
            self.assertEqual(cancellations, 2)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(coordinator.consume('mine', Mock(), Mock()))
        finally:
            await coordinator.close()

    async def test_asr_uses_production_fixed_transport_without_launching_an_agent(self):
        response = Mock(status=200, length=None, headers=Message(), will_close=True)
        response.read1.side_effect = [b'{"text":"text"}', b'']
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('http.client.HTTPSConnection', return_value=connection) as connect, \
             patch('asyncio.create_subprocess_exec') as spawn:
            self.assertEqual(await self.asr.source_asr(b'\x00\x00', 'ja'), 'text')
        self.assertEqual(connect.call_args.args, ('chatgpt.com',))
        method, path = connection.request.call_args.args
        self.assertEqual((method, path), ('POST', '/backend-api/transcribe'))
        self.assertEqual(connection.request.call_count, 1)
        spawn.assert_not_called()

    async def test_invalid_pcm_language_and_size_are_rejected_before_credentials(self):
        for pcm, language in ((b'', 'zh'), (b'\x00', 'zh'), (bytearray(b'\x00\x00'), 'zh'),
                              (b'\x00\x00', 'https://other'), (b'\x00' * 960002, 'zh')):
            with self.assertRaisesRegex(RuntimeError, '^asr-input$'):
                await self.asr.source_asr(pcm, language)
        with self.assertRaisesRegex(RuntimeError, '^asr-input$'):
            await self.asr.audio_asr(b'\x00\x00', None)
        self.owner.credentials.assert_not_called()

    async def test_malformed_extra_tool_and_oversized_results_fail_without_raw_error(self):
        for raw in (b'{"text":"ok","tool":{}}', b'{"text":"a","text":"b"}',
                    b'not-json-private-secret', json.dumps({'text': 'x' * 8193}).encode()):
            with patch('vlt.session.chatgpt_relay.request', return_value=raw):
                with self.assertRaisesRegex(RuntimeError, '^asr-result$'):
                    await self.asr.source_asr(b'\x00\x00', 'zh')

    async def test_empty_transcript_is_distinct_from_a_protocol_error(self):
        for raw, reason in ((b'{"text":""}', 'asr-empty'),
                            (b'{"text":" \\n"}', 'asr-empty'),
                            (b'{"text":"","tool":{}}', 'asr-result')):
            with self.subTest(reason=reason), patch('vlt.session.chatgpt_relay.request', return_value=raw):
                with self.assertRaisesRegex(RuntimeError, '^' + reason + '$'):
                    await self.asr.source_asr(b'\x00\x00', 'zh')

    async def test_failed_asr_notes_empty_text_and_duration_without_private_response(self):
        for raw, state in ((b'{"text":""}', 'empty'),
                           (b'{"text":"private-speech","tool":"private-secret"}', 'nonempty'),
                           (b'private-invalid-json', 'unparsed')):
            with self.subTest(state=state), patch('vlt.session.chatgpt_relay.request', return_value=raw), \
                    patch('builtins.print') as note:
                with self.assertRaisesRegex(RuntimeError, '^' + ('asr-empty' if state == 'empty' else 'asr-result') + '$'):
                    await self.asr.source_asr(b'\x00\x00' * 8000, 'zh')
            self.assertEqual(note.call_count, 1)
            message = note.call_args.args[0]
            self.assertIn('pcm_ms=500', message)
            self.assertIn('text_state=' + state, message)
            self.assertNotIn('private', message)
            self.assertNotIn('fake-access', message)

    async def test_bounded_asset_metadata_is_discarded_without_fetching(self):
        response = {'text': '明天九點見。', 'asset_pointer': 'sediment://test-only',
                    'asset_ttl': '2026-10-11T00:00:00Z', 'asset_format': 'wav'}
        with patch('vlt.session.chatgpt_relay.request', return_value=json.dumps(response).encode()) as request:
            self.assertEqual(await self.asr.source_asr(b'\x00\x00', 'zh'), response['text'])
        self.assertEqual(request.call_count, 1, 'asset fields must never trigger another request')

    async def test_partial_invalid_or_unrecognized_asset_schema_is_rejected(self):
        metadata = {'asset_pointer': 'sediment://test-only', 'asset_ttl': 'soon', 'asset_format': 'wav'}
        variants = ({'asset_pointer': 'x'}, dict(metadata, asset_pointer=[]),
                    dict(metadata, asset_ttl='x' * 2049), dict(metadata, asset_format='wav\n'),
                    dict(metadata, tools=[]))
        for fields in variants:
            with self.subTest(fields=list(fields)), \
                    patch('vlt.session.chatgpt_relay.request', return_value=json.dumps({'text': 'ok', **fields}).encode()):
                with self.assertRaisesRegex(RuntimeError, '^asr-result$'):
                    await self.asr.source_asr(b'\x00\x00', 'zh')

    async def test_changed_account_or_unknown_request_is_discarded_without_retry(self):
        def changed(*args):
            self.owner.generation += 1
            return b'{"text":"text"}'

        for call in (changed, RuntimeError('private-secret')):
            self.owner.generation = 2
            with patch('vlt.session.chatgpt_relay.request', side_effect=call) as request:
                with self.assertRaisesRegex(RuntimeError, '^asr-(stale|request)$'):
                    await self.asr.source_asr(b'\x00\x00', 'ja')
                self.assertEqual(request.call_count, 1)

    async def test_repeated_cancellation_awaits_http_worker_and_discards_result(self):
        entered, release = threading.Event(), threading.Event()

        def request(*args):
            entered.set()
            release.wait(2)
            return b'{"text":"must-not-release"}'

        with patch('vlt.session.chatgpt_relay.request', side_effect=request) as call:
            task = asyncio.create_task(self.asr.source_asr(b'\x00\x00', 'zh'))
            if not await asyncio.to_thread(entered.wait, .5):
                await asyncio.gather(task, return_exceptions=True)
                self.fail('transcription must reach the bounded HTTP worker')
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            try:
                self.assertFalse(task.done(), 'direction remains occupied until the owned request ends')
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            self.assertEqual(call.call_count, 1)

    async def test_account_change_before_http_worker_starts_prevents_request(self):
        async def delayed_worker(operation, *args):
            self.owner.generation += 1
            return operation(*args)

        with patch('vlt.session.chatgpt_relay.asyncio.to_thread', new=delayed_worker), \
             patch('vlt.session.chatgpt_relay.request') as call:
            with self.assertRaisesRegex(RuntimeError, '^asr-stale$'):
                await self.asr.source_asr(b'\x00\x00', 'zh')
            call.assert_not_called()

    async def test_cancelled_request_not_yet_sent_is_never_started(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_worker(operation, *args):
            entered.set()
            await release.wait()
            return operation(*args)

        with patch('vlt.session.chatgpt_relay.asyncio.to_thread', new=delayed_worker), \
             patch('vlt.session.chatgpt_relay.request', return_value=b'{"text":"text"}') as call:
            task = asyncio.create_task(self.asr.source_asr(b'\x00\x00', 'zh'))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            call.assert_not_called()

    async def test_login_change_while_waiting_for_credentials_does_not_send_old_audio_to_new_account(self):
        async def switched():
            self.owner.generation += 1
            return Credentials('other-fake-account', 'other-fake-access', self.owner.generation)

        self.owner.credentials = switched
        with patch('vlt.session.chatgpt_relay.request', return_value=b'{"text":"text"}') as call:
            with self.assertRaisesRegex(RuntimeError, '^asr-stale$'):
                await self.asr.source_asr(b'\x00\x00', 'zh')
            call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
