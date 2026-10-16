"""Remove only excess digital-zero lead after the complete native capture is sealed."""
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
from chatgpt_voice_fakes import NativeFixture, TEXT


class VoicePaddingTests(unittest.IsolatedAsyncioTestCase):
    async def render(self, pcm, tail, *, wrong_count=False):
        fixture = NativeFixture()
        owner = SimpleNamespace(generation=0, require_usable=Mock(), credentials=AsyncMock(
            return_value=SimpleNamespace(generation=0, account='offline-account', access='offline-token')))
        relay = SubscriptionRelay(owner)
        captured = []
        seal = CandidateBridge.seal

        async def observed_seal(bridge):
            raw = await seal(bridge)
            captured.append(raw)
            return raw

        async def local_send(raw):
            event = json.loads(raw)
            if event['type'] == 'seal':
                self.assertTrue(fixture.closed)
                fixture.steps.append('seal')
                fixture.bridge._receive_audio(tail)
                await fixture.bridge._receive_control(dict(type='sealed', seal_id=event['seal_id'],
                    samples=(len(pcm) + len(tail)) // 2 + int(wrong_count)))
            elif event['type'] == 'close':
                fixture.steps.append('browser-close')

        fixture.local_send = local_send
        async def start(bridge, profile):
            await fixture.start(bridge, profile)

        with patch('chatgpt_voice_fakes.PCM', pcm), \
                patch.object(CandidateBridge, 'start', start), \
                patch.object(CandidateBridge, 'seal', observed_seal), \
                patch('vlt.session.chatgpt_relay.request', side_effect=fixture.request), \
                patch('vlt.session.chatgpt_voice.client.connect', side_effect=fixture.connect):
            voice = NativeVoice(relay)
            if wrong_count:
                with self.assertRaisesRegex(RuntimeError, '^voice-failed$'):
                    await voice.generate(TEXT, 'ja')
                self.assertEqual(captured, [])
                output = None
            else:
                _, output = await voice.generate(TEXT, 'ja')
                self.assertEqual(captured, [pcm + tail], 'seal must acknowledge every original sample')
                self.assertLess(fixture.steps.index('ws-close'), fixture.steps.index('seal'))
                with patch.object(relay, '_transcribe', AsyncMock(return_value=TEXT)) as transcribe:
                    self.assertEqual(await relay.audio_asr(output, 'ja'), TEXT)
                self.assertEqual(transcribe.await_count, 3)
                for call in transcribe.await_args_list:
                    self.assertEqual(call.args[:3], (output, 'ja', 24000))
        self.assertFalse(Path(fixture.profile).exists())
        self.assertTrue(voice.reader.done())
        return output

    async def test_excess_zero_lead_keeps_guard_every_nonzero_sample_and_full_tail(self):
        tail = b'\x01\x00' * 17 + bytes(100)
        for first in (b'\x01\x00', b'\x00\x01', b'\xff\xff'):
            with self.subTest(first=first):
                speech = first + bytes(700) + b'\x01\x00'
                raw = bytes(24000) + speech
                result = await self.render(raw, tail)
                self.assertEqual(result, bytes(9600) + speech + tail)

    async def test_short_lead_quiet_speech_and_all_zero_capture_are_unchanged(self):
        for raw in (bytes(4800) + b'\x01\x00', b'\x01\x00' + bytes(24000), bytes(24000)):
            with self.subTest(raw_bytes=len(raw), first=raw[:2]):
                tail = bytes(34)
                self.assertEqual(await self.render(raw, tail), raw + tail)

    async def test_padding_never_bypasses_full_seal_count(self):
        await self.render(bytes(24000) + b'\x01\x00', b'\x01\x00', wrong_count=True)


if __name__ == '__main__':
    unittest.main()
