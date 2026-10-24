"""Local preparation overlaps inference without releasing unapproved speech."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_validated import ChatGPTValidatedSession


class Prepared:
    def __init__(self, generate, activate):
        self.generate, self.activate = generate, activate

    async def __call__(self, *args):
        return await self.generate(*args)


class SessionPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events, self.text, self.audio = [], [], []
        self.owner = SimpleNamespace(generation=1, require_usable=Mock(),
            credentials=AsyncMock(return_value=SimpleNamespace(generation=1)))
        self.verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                            negation=True, complete=True, extra_content=False)
        self.synthesize = AsyncMock(return_value=('prepared', b'\x01\x00' * 480))
        self.cleanup_wait = None
        self.activation_wait = None
        self.prepared_targets = []

        async def activate():
            self.events.append('activate')
            if self.activation_wait is not None:
                await self.activation_wait.wait()

        @asynccontextmanager
        async def preparation(*targets):
            self.events.append('prepare')
            self.prepared_targets.append(targets)
            try:
                yield Prepared(self.synthesize, activate)
            finally:
                self.events.append('cleanup')
                if self.cleanup_wait is not None:
                    await self.cleanup_wait.wait()
                self.events.append('closed')

        async def source(*_):
            self.events.append('source-asr')
            return '明天九點見。'

        self.services = SimpleNamespace(source_asr=AsyncMock(side_effect=source),
            translate=AsyncMock(return_value=dict(translation='明日九時に会いましょう。', language='ja')),
            validate_text=AsyncMock(side_effect=lambda *_: dict(self.verdict)),
            synthesize=AsyncMock(side_effect=AssertionError('cold synthesis despite healthy preparation')),
            audio_asr=AsyncMock(return_value='明日九時に会いましょう。'),
            validate_audio=AsyncMock(side_effect=lambda *_: dict(self.verdict)),
            prepare_voice=preparation)
        for name, value in [('vlt.session.chatgpt_auth.AUTH_OWNER', self.owner),
                            ('vlt.session.chatgpt_validated.tls_context', Mock()),
                            ('vlt.session.chatgpt_relay.SubscriptionRelay', Mock(return_value=self.services))]:
            self.enterContext(patch(name, value))

    async def start(self, direction='mine', audio=True):
        cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja', output_audio=audio)
        cfg._direction = direction
        session = ChatGPTValidatedSession(cfg)
        self.addAsyncCleanup(session.close)
        await session.start(self.text.append, self.audio.append if audio else None)
        return session

    async def feed(self, session):
        await session.send_audio(b'\xf4\x01' * 4800)
        session.end_input_segment()
        for _ in range(45):
            await asyncio.sleep(0)

    async def test_prepares_during_text_validation_and_checks_frozen_candidate_before_outputs(self):
        pending = asyncio.get_running_loop().create_future()
        async def check(*_):
            return await pending

        self.services.validate_audio.side_effect = check
        session = await self.start()
        await self.feed(session)
        self.assertEqual(self.events[:2], ['source-asr', 'prepare'])
        self.synthesize.assert_awaited_once_with('明日九時に会いましょう。', 'ja')
        self.services.audio_asr.assert_awaited_once_with(b'\x01\x00' * 480, 'ja')
        session.tick()
        self.assertEqual((self.text, self.audio), ([], []))
        pending.set_result(self.verdict)
        for _ in range(30):
            await asyncio.sleep(0)
        session.tick()
        self.assertEqual(self.audio, [b'\x01\x00' * 480])
        self.assertEqual(len(self.text), 1)
        self.assertEqual(self.events[-2:], ['cleanup', 'closed'])

    async def test_rejection_closes_local_preparation_without_synthesis(self):
        self.verdict['approved'] = False
        session = await self.start()
        await self.feed(session)
        self.assertEqual(self.events, ['source-asr', 'prepare', 'cleanup', 'closed'])
        self.synthesize.assert_not_awaited()
        self.services.audio_asr.assert_not_awaited()
        session.tick()
        self.assertEqual((self.text, self.audio), ([], []))
        self.assertEqual(session.diagnostics()['last_reason'], 'text-rejected')

    async def test_text_only_directions_do_not_prepare_unused_audio(self):
        for direction in ('mine', 'theirs'):
            session = await self.start(direction, False)
            await self.feed(session)
            session.tick()
        self.assertEqual(self.events, ['source-asr', 'source-asr'])
        self.assertEqual(len(self.text), 2)
        self.assertEqual(self.audio, [])

    async def test_cancel_waits_for_preparation_cleanup_and_never_synthesizes(self):
        entered = asyncio.Event()

        async def pending(*_):
            entered.set()
            await asyncio.Event().wait()

        self.services.validate_text.side_effect = pending
        self.cleanup_wait = asyncio.Event()
        session = await self.start()
        await self.feed(session)
        await entered.wait()
        closing = asyncio.create_task(session.close())
        try:
            for _ in range(15):
                await asyncio.sleep(0)
            self.assertFalse(closing.done(), 'close must retain ownership of preparation cleanup')
            self.assertEqual(self.events, ['source-asr', 'prepare', 'cleanup'])
        finally:
            self.cleanup_wait.set()
            await closing
        self.assertEqual(self.events[-1], 'closed')
        self.synthesize.assert_not_awaited()
        self.assertFalse(session._pipeline._owned)

    async def test_changed_account_during_asr_closes_preparation_without_voice(self):
        async def changed(*_):
            self.owner.generation += 1
            return '明天九點見。'

        self.services.source_asr.side_effect = changed
        session = await self.start()
        await self.feed(session)
        self.assertEqual(self.events, [])
        self.synthesize.assert_not_awaited()
        session.tick()
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_two_queued_sections_keep_pcm_order_and_close_each_preparation(self):
        session = await self.start()
        first, second = b'\xf4\x01' * 4800, b'\xf5\x01' * 4800
        for pcm in (first, second):
            await session.send_audio(pcm)
            session.end_input_segment()
        await asyncio.gather(*tuple(session._pipeline._owned))
        session.tick()
        session.tick()
        self.assertEqual([call.args[0] for call in self.services.source_asr.await_args_list], [first, second])
        self.assertEqual(len(self.audio), 2)
        self.assertEqual(session.diagnostics()['rejected'], 0)
        self.assertEqual(self.events.count('prepare'), 2)
        self.assertEqual(self.events.count('closed'), 2)

    async def test_source_failure_skips_prepare_and_invalid_translation_closes_it_without_speech(self):
        self.services.source_asr.side_effect = RuntimeError('asr-empty')
        session = await self.start()
        await self.feed(session)
        self.assertEqual(self.events, [])
        self.assertEqual(session.diagnostics()['last_reason'], 'source-asr-empty')
        self.services.source_asr.side_effect = None
        self.services.source_asr.return_value = '明天九點見。'
        self.services.translate.return_value = {'translation': '', 'language': 'ja'}
        await self.feed(session)
        self.assertEqual(self.events, ['prepare', 'cleanup', 'closed'])
        self.synthesize.assert_not_awaited()
        self.services.audio_asr.assert_not_awaited()
        self.assertEqual(session.diagnostics()['last_reason'], 'translation-invalid')

    async def test_prepares_only_fixed_target_while_translation_is_pending(self):
        pending = asyncio.get_running_loop().create_future()
        async def translate(*_):
            return await pending
        self.services.translate.side_effect = translate
        session = await self.start()
        try:
            await self.feed(session)
            self.assertEqual(self.prepared_targets, [('ja',)])
            self.assertEqual(self.events, ['source-asr', 'prepare'])
            self.synthesize.assert_not_awaited()
        finally:
            pending.set_result(dict(translation='明日九時に会いましょう。', language='ja'))

    async def test_activation_completes_before_timed_synthesis_or_outputs(self):
        self.activation_wait = asyncio.Event()
        session = await self.start()
        await self.feed(session)
        self.assertIn('activate', self.events)
        self.synthesize.assert_not_awaited()
        session.tick()
        self.assertEqual((self.text, self.audio), ([], []))
        self.activation_wait.set()
        await asyncio.gather(*tuple(session._pipeline._owned))
        session.tick()
        self.assertEqual(self.audio, [b'\x01\x00' * 480])


if __name__ == '__main__':
    unittest.main()
