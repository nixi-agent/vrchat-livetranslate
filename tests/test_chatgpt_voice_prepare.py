"""Prepared native voice owns local resources and never pre-sends a remote offer."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_browser import BrowserBridge
from vlt.session.chatgpt_relay import SubscriptionRelay
from vlt.session.chatgpt_voice import NativeVoice
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_validated import ChatGPTValidatedSession
from chatgpt_voice_fakes import NativeFixture, PCM, TAIL, TEXT, wire


class VoicePreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = NativeFixture()
        self.owner = SimpleNamespace(generation=1, require_usable=Mock(),
            credentials=AsyncMock(return_value=SimpleNamespace(generation=1, account='offline', access='offline')))
        self.relay = SubscriptionRelay(self.owner)
        self.opened = asyncio.Event()
        self.release_open = None
        self.fail_first = False
        self.bridges, self.children, self.profiles, self.servers = [], [], [], []

        async def open_local(bridge, profile):
            self.bridges.append(bridge)
            self.profiles.append(profile)
            server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
            self.servers.append(server)
            bridge.server = server
            self.opened.set()
            if self.release_open is not None:
                await self.release_open.wait()
            if self.fail_first and len(self.bridges) == 1:
                raise OSError('offline local launch failure')
            child = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0))
            child.terminate = Mock(side_effect=lambda: setattr(child, 'returncode', 0))
            self.children.append(child)
            bridge.process = child
            async def activate(raw):
                event = json.loads(raw)
                if event['type'] == 'answer':
                    await self.fixture.deliver_answer(bridge, profile, event['nonce'])
                    return
                assert event['type'] == 'activate' and event['nonce'] == bridge._offer_nonce
                bridge._negotiated = True
                if hasattr(bridge, '_reservation'):
                    self.fixture.steps.append('browser-start')
                    bridge._answering = asyncio.create_task(bridge._hold_offer('v=0\r\n'))
                    return
                await self.fixture.start(bridge, profile)

            async def negotiate():
                try:
                    if bridge._deferred:
                        bridge.socket = SimpleNamespace(send=activate)
                        await bridge._receive_control({'type': 'prepared'})
                        await asyncio.Event().wait()
                    else:
                        await self.fixture.start(bridge, profile)
                except Exception:
                    bridge._capture_error(None)
                    bridge.ready.set()
            bridge.handler = asyncio.create_task(negotiate())

        self.enterContext(patch.object(BrowserBridge, '_open', open_local))
        self.enterContext(patch('vlt.session.chatgpt_relay.request', side_effect=self.fixture.request))
        self.enterContext(patch('websockets.asyncio.client.connect', side_effect=self.fixture.connect))

    async def settled(self):
        await asyncio.wait_for(self.opened.wait(), 1)
        for _ in range(15):
            await asyncio.sleep(0)

    def assert_closed(self):
        for bridge, server in zip(self.bridges, self.servers):
            self.assertTrue(bridge.closing)
            server.close.assert_called_once()
            self.assertTrue(bridge.handler is None or bridge.handler.done())
            self.assertTrue(bridge._open_task is None or bridge._open_task.done())
        for child in self.children:
            self.assertEqual(child.returncode, 0)
            child.terminate.assert_called_once()
        self.assertTrue(all(not Path(p).exists() for p in self.profiles))

    async def test_preparation_opens_once_but_holds_remote_offer_until_generation(self):
        async with self.relay.prepare_voice() as generate:
            await self.settled()
            self.assertEqual(len(self.children), 1)
            self.assertIsNone(self.fixture.offer)
            self.assertIsNone(self.fixture.connect_args)
            self.assertEqual(self.bridges[0].capture._pcm, b'')
            candidate = await generate(TEXT, 'ja')
            self.assertEqual(candidate[1], PCM + TAIL)
            self.assertEqual(len(self.children), 1, 'consume the locally prepared browser')
        self.assertEqual(self.fixture.steps.count('http'), 1)
        self.assertEqual(self.fixture.steps.count('context-append'), 1)
        self.assertIn('seal', self.fixture.steps)
        self.assert_closed()

    async def test_rejected_text_closes_held_offer_without_remote_work(self):
        async with self.relay.prepare_voice():
            await self.settled()
        self.assertIsNone(self.fixture.offer)
        self.assertIsNone(self.fixture.context)
        self.assert_closed()

    async def test_expired_preparation_falls_back_before_any_remote_offer(self):
        async with self.relay.prepare_voice() as generate:
            await self.settled()
            with patch('vlt.session.chatgpt_voice._PREPARE_TTL', 0):
                candidate = await generate(TEXT, 'ja')
            self.assertEqual(candidate[1], PCM + TAIL)
        self.assertEqual(len(self.children), 2)
        self.assertEqual(self.fixture.steps.count('http'), 1)
        self.assert_closed()

    async def test_local_launch_failure_does_not_discard_approved_speech(self):
        self.fail_first = True
        async with self.relay.prepare_voice() as generate:
            await self.settled()
            candidate = await generate(TEXT, 'ja')
            self.assertEqual(candidate[1], PCM + TAIL)
        self.assertEqual(len(self.bridges), 2)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.fixture.steps.count('http'), 1)
        self.assert_closed()

    async def test_generation_change_never_reconnects_under_a_new_account(self):
        async with self.relay.prepare_voice() as generate:
            await self.settled()
            self.owner.generation += 1
            with self.assertRaisesRegex(RuntimeError, 'voice-stale'):
                await generate(TEXT, 'ja')
        self.assertIsNone(self.fixture.offer)
        self.assert_closed()

    async def test_invalid_target_after_prepare_reaps_local_resources(self):
        async with self.relay.prepare_voice() as generate:
            await self.settled()
            with self.assertRaisesRegex(RuntimeError, 'voice-input'):
                await generate(TEXT, 'invalid-target')
        self.assertIsNone(self.fixture.offer)
        self.assert_closed()

    async def test_repeated_cancel_during_open_retains_process_ownership(self):
        self.release_open = asyncio.Event()

        async def worker():
            async with self.relay.prepare_voice():
                await asyncio.Event().wait()

        work = asyncio.create_task(worker())
        try:
            await asyncio.wait_for(self.opened.wait(), 1)
        except TimeoutError:
            await work
            raise
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        try:
            for _ in range(15):
                await asyncio.sleep(0)
            self.assertFalse(work.done(), 'pending local creation must be joined before close')
        finally:
            self.release_open.set()
            await asyncio.gather(work, return_exceptions=True)
        self.assertTrue(work.cancelled())
        self.assertIsNone(self.fixture.offer)
        self.assert_closed()

    async def test_remote_failure_is_not_retried_as_a_preparation_failure(self):
        failed = AsyncMock(side_effect=RuntimeError('voice-request'))
        self.relay._request = failed
        async with self.relay.prepare_voice() as generate:
            await self.settled()
            with patch('vlt.session.chatgpt_voice._DEADLINE', .05):
                with self.assertRaises(RuntimeError):
                    await generate(TEXT, 'ja')
        self.assertEqual(failed.await_count, 1)
        self.assertEqual(len(self.children), 1)
        self.assert_closed()

    async def test_concurrent_close_joins_one_cleanup_owner(self):
        voice = NativeVoice(self.relay)
        voice._cleanup = AsyncMock()
        await asyncio.gather(voice.close(), voice.close())
        voice._cleanup.assert_awaited_once()

    async def test_unrequested_events_or_pcm_before_approval_are_not_cold_retried(self):
        for kind in ('event', 'audio'):
            with self.subTest(kind=kind):
                async with self.relay.prepare_voice() as generate:
                    await self.settled()
                    bridge = self.bridges[-1]
                    if kind == 'event':
                        bridge.receive(wire(self.fixture.primary[0]), 'primary')
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'capture-pcm'):
                            bridge._receive_audio(PCM)
                    with self.assertRaisesRegex(RuntimeError, 'voice-event'):
                        await generate(TEXT, 'ja')
                self.assertIsNone(self.fixture.offer)
        self.assert_closed()

    async def test_account_change_while_discarding_expired_preparation_stays_closed(self):
        async with self.relay.prepare_voice() as generate:
            await self.settled()
            self.servers[0].wait_closed.side_effect = lambda: setattr(self.owner, 'generation', 2)
            with patch('vlt.session.chatgpt_voice._PREPARE_TTL', 0):
                with self.assertRaisesRegex(RuntimeError, 'voice-stale'):
                    await generate(TEXT, 'ja')
        self.assertIsNone(self.fixture.offer)
        self.assertEqual(len(self.children), 1)
        self.assert_closed()

    async def run_session(self, approved):
        verdict = dict(approved=approved, language='ja', meaning=True, numbers=True,
                       negation=True, complete=True, extra_content=False)
        pending = asyncio.get_running_loop().create_future()
        async def check(*_):
            return await pending
        self.relay.source_asr = AsyncMock(return_value='明天九點見。')
        self.relay.translate = AsyncMock(return_value=dict(translation=TEXT, language='ja'))
        self.relay.validate_text = check
        self.relay.audio_asr = AsyncMock(return_value=TEXT)
        self.relay.validate_audio = AsyncMock(return_value=verdict)
        self.enterContext(patch('vlt.session.chatgpt_auth.AUTH_OWNER', self.owner))
        self.enterContext(patch('vlt.session.chatgpt_relay.SubscriptionRelay', return_value=self.relay))
        self.enterContext(patch('vlt.session.chatgpt_validated.tls_context'))
        cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja', output_audio=True)
        cfg._direction = 'mine'
        session = ChatGPTValidatedSession(cfg)
        self.addAsyncCleanup(session.close)
        text, audio = [], []
        await session.start(text.append, audio.append)
        await session.send_audio(b'\xf4\x01' * 4800)
        session.end_input_segment()
        await self.settled()
        self.assertIsNotNone(self.fixture.offer, 'real session reserves a fixed target without source or script')
        self.assertNotIn('initial_items', self.fixture.offer['session'])
        self.assertIsNone(self.fixture.context)
        self.assertEqual(len(self.children), 1)
        pending.set_result(verdict)
        await asyncio.wait_for(asyncio.gather(*tuple(session._pipeline._owned)), 1)
        session.tick()
        await session.close()
        self.assert_closed()
        return session, text, audio

    async def test_real_session_and_relay_keep_unapproved_script_local_then_seal_and_check(self):
        _, text, audio = await self.run_session(True)
        self.assertEqual(audio, [PCM + TAIL])
        self.assertEqual(text[0].confirmed, TEXT)
        self.relay.audio_asr.assert_awaited_once_with(PCM + TAIL, 'ja')
        self.relay.validate_audio.assert_awaited_once_with(TEXT, TEXT, 'ja')
        self.assertEqual(self.fixture.steps.count('http'), 1)

    async def test_real_session_text_rejection_closes_neutral_work_without_remote_script(self):
        session, text, audio = await self.run_session(False)
        self.assertEqual((text, audio), ([], []))
        self.assertIsNotNone(self.fixture.offer)
        self.assertIsNone(self.fixture.context)
        self.relay.audio_asr.assert_not_awaited()
        self.assertEqual(session.diagnostics()['last_reason'], 'text-rejected')

    async def test_closed_voice_never_reopens_or_skips_cleanup_via_cached_close(self):
        voice = NativeVoice(self.relay)
        self.addAsyncCleanup(voice._cleanup)
        await voice.close()
        with self.assertRaisesRegex(RuntimeError, 'voice-stale'):
            await voice.generate(TEXT, 'ja')
        with self.assertRaisesRegex(RuntimeError, 'voice-stale'):
            voice.prepare()
        self.assertIsNone(self.fixture.offer)
        self.assertEqual(self.children, [])

    async def test_escaped_generate_after_preparation_scope_is_closed_cannot_start_cold(self):
        async with self.relay.prepare_voice() as generate:
            await self.settled()
        with self.assertRaisesRegex(RuntimeError, 'voice-stale'):
            await generate(TEXT, 'ja')
        self.assertIsNone(self.fixture.offer)
        self.assertEqual(len(self.children), 1)
        self.assert_closed()


if __name__ == '__main__':
    unittest.main()
