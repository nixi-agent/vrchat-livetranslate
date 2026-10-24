"""明確的一次 speakable 請求；ACK 不得取代雙通道生成完成。"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_candidate import CandidateBridge
from vlt.session.chatgpt_relay import SubscriptionRelay
from vlt.session.chatgpt_voice import NativeVoice
from vlt.session.chatgpt_voice_events import VoiceEvents
from chatgpt_voice_fakes import NativeFixture, PCM, TAIL, TEXT, events, wire


ACK = dict(type='session.context.appended', start_ms=200, end_ms=400)


class ContextEventsTests(unittest.TestCase):
    def begin(self):
        state = VoiceEvents()
        for channel in ('primary', 'sideband'):
            state.consume(wire(events()[0]), channel)
        self.assertTrue(callable(getattr(state, 'request_context', None)),
                        'explicit one-context admission is required')
        state.request_context()
        return state

    def test_two_matching_acks_then_matching_turns_are_required(self):
        state = self.begin()
        for channel in ('primary', 'sideband'):
            state.consume(wire(ACK), channel)
        self.assertTrue(state.context_acknowledged)
        self.assertFalse(state.complete)
        for channel in ('primary', 'sideband'):
            for event in events()[1:]:
                state.consume(wire(event), channel)
        self.assertTrue(state.complete)

    def test_unsolicited_duplicate_and_mismatched_acks_poison_generation(self):
        state = VoiceEvents()
        state.consume(wire(events()[0]), 'primary')
        with self.assertRaises(RuntimeError):
            state.consume(wire(ACK), 'primary')
        for duplicate in (True, False):
            state = self.begin()
            state.consume(wire(ACK), 'primary')
            with self.assertRaises(RuntimeError):
                state.consume(wire(ACK if duplicate else dict(ACK, end_ms=401)),
                              'primary' if duplicate else 'sideband')
            self.assertFalse(state.complete)

    def test_malformed_acks_and_turns_without_channel_ack_are_rejected(self):
        for event in (dict(ACK, tools=[]), dict(ACK, start_ms=True), dict(ACK, end_ms=199),
                      dict(ACK, end_ms=float('nan')), dict(type='session.context.appended')):
            state = self.begin()
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                state.consume(wire(event), 'primary')
            self.assertFalse(state.complete)
        state = self.begin()
        state.consume(wire(ACK), 'primary')
        with self.assertRaises(RuntimeError):
            state.consume(wire(events()[2]), 'sideband')
        self.assertFalse(state.complete)

    def test_context_admission_is_once_after_both_starts_and_before_any_turn(self):
        state = self.begin()
        with self.assertRaises(RuntimeError):
            state.request_context()
        for with_turn in (False, True):
            state = VoiceEvents()
            state.consume(wire(events()[0]), 'primary')
            if with_turn:
                state.consume(wire(events()[0]), 'sideband')
                state.consume(wire(events()[2]), 'primary')
            self.assertTrue(callable(getattr(state, 'request_context', None)))
            with self.assertRaises(RuntimeError):
                state.request_context()


class ContextVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = NativeFixture()
        self.owner = SimpleNamespace(generation=0, require_usable=Mock(),
            credentials=AsyncMock(return_value=SimpleNamespace(generation=0,
                                 account='offline-account', access='offline-token')))
        self.voice = NativeVoice(SubscriptionRelay(self.owner))

        async def start(bridge, profile):
            await self.fixture.start(bridge, profile)

        for replacement in (
            patch.object(CandidateBridge, 'start', start),
            patch('vlt.session.chatgpt_relay.request', side_effect=lambda *a: self.fixture.request(*a)),
            patch('websockets.asyncio.client.connect', side_effect=lambda *a, **k: self.fixture.connect(*a, **k))):
            replacement.start()
            self.addCleanup(replacement.stop)

    async def test_script_is_sent_once_after_start_without_initial_item_trigger(self):
        _, pcm = await self.voice.generate(TEXT, 'ja')
        self.assertNotIn('initial_items', self.fixture.offer['session'],
                         'initial history must not also trigger the same script')
        self.assertEqual(self.fixture.context, dict(type='session.context.append', channel='speakable',
                         content=[dict(type='input_text', text=json.dumps({'script': TEXT}, ensure_ascii=False))]))
        self.assertEqual(self.fixture.steps.count('context-append'), 1)
        self.assertLess(self.fixture.steps.index('context-append'), self.fixture.steps.index('native-close'))
        self.assertEqual(pcm, PCM + TAIL)
        self.assertFalse(Path(self.fixture.profile).exists())

    async def test_cancellation_while_waiting_for_acks_cleans_without_sealing(self):
        entered = asyncio.Event()
        send = self.fixture.send

        async def hold_context(raw):
            if json.loads(raw)['type'] == 'session.context.append':
                entered.set()
                return
            await send(raw)

        self.fixture.send = hold_context
        task = asyncio.create_task(self.voice.generate(TEXT, 'ja'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            self.assertNotIn('seal', self.fixture.steps)
            self.assertFalse(Path(self.fixture.profile).exists())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
