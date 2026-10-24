"""Source ASR canonicalizes digital padding without altering frozen speech or voice checks."""
import asyncio
from email import policy
from email.parser import BytesParser
import hashlib
import io
import struct
import sys
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_chatgpt_asr as fixtures
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator


class SourcePaddingTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ASRTests.setUp

    def request_pcm(self, call, rate=16000):
        name, body, headers = call.args
        self.assertEqual(name, 'transcribe')
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + headers['Content-Type'] + '\r\n\r\n').encode() + body)
        parts = list(message.iter_parts())
        self.assertEqual([p.get_param('name', header='Content-Disposition') for p in parts], ['file', 'language'])
        with wave.open(io.BytesIO(parts[0].get_payload(decode=True)), 'rb') as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (rate, 1, 2))
            return audio.readframes(audio.getnframes())

    async def test_source_removes_only_complete_leading_zero_samples(self):
        for first in (1, 256, -256, -32768, 32767):
            speech = struct.pack('<h', first) + b'\0\0' + b'\x12\x34' * 400 + bytes(3200)
            for padding in (0, 1, 80, 8000):
                with self.subTest(first=first, padding=padding), patch(
                        'vlt.session.chatgpt_relay.request', return_value=b'{"text":"speech"}') as call:
                    self.assertEqual(await self.asr.source_asr(bytes(padding*2) + speech, 'en'), 'speech')
                self.assertEqual(call.call_count, 1, 'no retry or reference-text request')
                self.assertEqual(self.request_pcm(call.call_args), speech,
                    'quiet samples, zero low bytes, internal zeros and the entire tail must survive')

    async def test_source_padding_removal_does_not_bypass_original_input_limits(self):
        invalid = [bytes(960000) + b'\x01\x00', b'\0', b'', bytearray(b'\x01\x00'), 'not PCM']
        for pcm in invalid:
            with self.subTest(kind=type(pcm).__name__, size=len(pcm)), patch(
                    'vlt.session.chatgpt_relay.request') as call:
                with self.assertRaisesRegex(RuntimeError, '^asr-input$'):
                    await self.asr.source_asr(pcm, 'en')
                call.assert_not_called()

    async def test_all_zero_input_stays_under_the_existing_asr_contract(self):
        pcm = bytes(16000)
        with patch('vlt.session.chatgpt_relay.request', return_value=b'{"text":"same"}') as call:
            await self.asr.source_asr(pcm, 'en')
        self.assertEqual(self.request_pcm(call.call_args), pcm)

    async def test_blind_voice_verification_keeps_every_original_sample_in_all_three_reads(self):
        pcm = bytes(24000) + b'\x00\x01\x00\x00\x11\x00' * 100 + bytes(4000)
        with patch('vlt.session.chatgpt_relay.request', return_value=b'{"text":"same"}') as call:
            self.assertEqual(await self.asr.audio_asr(pcm, 'en'), 'same')
        self.assertEqual(call.call_count, 3)
        for request in call.call_args_list:
            self.assertEqual(self.request_pcm(request, rate=24000), pcm)

    async def test_normalized_asr_request_keeps_original_section_and_hash_at_release(self):
        cfg = SessionConfig(provider='chatgpt', source_lang='en', target_lang='en', output_audio=False)
        self.asr.validate_text = AsyncMock(return_value=dict(approved=True, language='en',
            meaning=True, numbers=True, negation=True, complete=True, extra_content=False))
        reasons, captions = Mock(), []
        pipeline = Coordinator({'theirs': cfg}, self.asr, reasons, lambda: self.owner.generation)
        self.addAsyncCleanup(pipeline.close)
        speech = b'\x00\x01' * 3200 + bytes(1600)
        original = bytes(16000) + speech
        with patch('vlt.session.chatgpt_relay.request', return_value=b'{"text":"same"}') as call:
            self.assertTrue(pipeline.submit('theirs', original))
            await asyncio.gather(*tuple(pipeline._owned))
        self.assertEqual(self.request_pcm(call.call_args), speech)
        approved = pipeline._ready['theirs'][0]
        self.assertEqual(approved.section.pcm, original)
        self.assertEqual(approved.section.source_hash, hashlib.sha256(original).hexdigest())
        self.assertTrue(pipeline.consume('theirs', captions.append, Mock()))
        self.assertEqual([caption.source for caption in captions], ['same'])
        reasons.assert_not_called()


if __name__ == '__main__':
    unittest.main()
