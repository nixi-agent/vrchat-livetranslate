"""Continuous source PCM stays bounded, ordered and complete without a quiet gap."""
import asyncio
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_segments import Segmenter
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator


def feed(segmenter, data, chunk=3200):
    return [section for at in range(0, len(data), chunk)
            for section in segmenter.feed(data[at:at + chunk])]


class ContinuousSegmentsTests(unittest.TestCase):
    def test_445_seconds_without_silence_remain_available_as_bounded_candidates(self):
        data = struct.pack('<hhh', 800, -600, 1000) * (16000 * 445 // 3) + b'\x21\x03'
        s = Segmenter()
        sections = feed(s, data)
        self.assertGreaterEqual(len([r for r in sections if r.pcm]), 14,
                                'continuous source must not disappear until the next quiet gap')
        tail = s.finish()
        if tail is not None:
            sections.append(tail)
        self.assertTrue(all(not r.reason and 0 < len(r.pcm) <= 960000 for r in sections))
        self.assertTrue(b''.join(r.pcm for r in sections) == data,
                        'no source bytes may be omitted, repeated, padded or reordered')
        self.assertIsNone(s.finish())

    def test_limit_uses_the_center_of_a_low_energy_window_without_editing_pcm(self):
        loud = struct.pack('<h', 800)
        low = struct.pack('<h', 300)  # Still above the existing voiced threshold.
        data = loud * 457600 + low * 3200 + loud * 35200
        s = Segmenter()
        sections = feed(s, data)
        self.assertTrue(sections and sections[0].pcm)
        self.assertEqual(len(sections[0].pcm), 459200 * 2)
        sections.append(s.finish())
        self.assertTrue(b''.join(r.pcm for r in sections) == data)

    def test_equal_energy_prefers_the_latest_window_and_keeps_the_tail(self):
        data = struct.pack('<h', 800) * 480101
        s = Segmenter()
        sections = feed(s, data)
        self.assertTrue(sections and sections[0].pcm)
        self.assertEqual(len(sections[0].pcm), 478400 * 2)
        sections.append(s.finish())
        self.assertTrue(b''.join(r.pcm for r in sections) == data)

    def test_limit_is_independent_of_capture_chunk_boundaries(self):
        data = struct.pack('<hhh', 300, -900, 700) * 330001
        reference = None
        for chunk in (258, 3200, 31998, 32000):
            s = Segmenter()
            sections = feed(s, data, chunk)
            tail = s.finish()
            if tail is not None:
                sections.append(tail)
            self.assertTrue(all(not r.reason and len(r.pcm) <= 960000 for r in sections))
            self.assertTrue(b''.join(r.pcm for r in sections) == data)
            contents = [r.pcm for r in sections]
            if reference is None:
                reference = contents
            else:
                self.assertTrue(contents == reference)

    def test_reset_removes_the_retained_continuation(self):
        s = Segmenter()
        feed(s, struct.pack('<h', 800) * 480160)
        s.reset()
        data = struct.pack('<h', 900) * 480 + bytes(16000)
        sections = feed(s, data)
        self.assertEqual(len(sections), 1)
        self.assertTrue(sections[0].pcm == data)

    def test_quiet_at_the_limit_does_not_become_a_new_silence_only_candidate(self):
        s = Segmenter()
        data = struct.pack('<h', 800) * 473600 + bytes(12800)
        sections = feed(s, data)
        self.assertEqual(len(sections), 1)
        sections.append(s.finish())
        self.assertTrue(all(any(r.pcm) and len(r.pcm) <= 960000 for r in sections))
        self.assertTrue(b''.join(r.pcm for r in sections) == data)

    def test_invalid_capture_still_resets_the_retained_continuation(self):
        s = Segmenter()
        feed(s, struct.pack('<h', 800) * 480160)
        with self.assertRaisesRegex(RuntimeError, '^pcm-invalid$'):
            s.feed(bytes(32002))
        data = struct.pack('<h', 900) * 480 + bytes(16000)
        self.assertTrue(feed(s, data)[0].pcm == data)

    def test_limit_during_short_quiet_preserves_closure_and_short_resumed_speech(self):
        for rest in (bytes(160), struct.pack('<h', 900) * 320 + bytes(16000)):
            with self.subTest(resumed=any(rest)):
                data = struct.pack('<h', 800) * 472160 + bytes(15680) + rest
                s = Segmenter()
                sections = feed(s, data)
                tail = s.finish()
                if tail is not None:
                    sections.append(tail)
                self.assertTrue(all(not r.reason and 0 < len(r.pcm) <= 960000
                                    for r in sections))
                self.assertTrue(b''.join(r.pcm for r in sections) == data,
                                'limit must not reset onset within an active utterance')


class ContinuousPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_directions_keep_full_source_and_wait_for_required_checks(self):
        data = struct.pack('<h', 800) * 976000 + bytes(16000)
        await self.check_both_directions(data, 3)

    async def test_both_directions_validate_earlier_pause_sections_in_order(self):
        data = (struct.pack('<h', 800) * 64000 + bytes(11200) +
                struct.pack('<h', 900) * 16000 + bytes(16000))
        await self.check_both_directions(data, 2)

    async def check_both_directions(self, data, expected):
        for direction in ('mine', 'theirs'):
            with self.subTest(direction=direction):
                verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                               negation=True, complete=True, extra_content=False)
                gate = asyncio.Event()
                async def check(*_):
                    await gate.wait()
                    await asyncio.sleep(0)
                    return verdict
                services = SimpleNamespace(source_asr=AsyncMock(return_value='完整來源'),
                    translate=AsyncMock(return_value=dict(translation='原文の翻訳', language='ja')),
                    validate_text=AsyncMock(side_effect=check),
                    synthesize=AsyncMock(return_value=('voice_1', bytes(960))),
                    audio_asr=AsyncMock(return_value='原文の翻訳'),
                    validate_audio=AsyncMock(return_value=verdict))
                cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja',
                                    output_audio=direction == 'mine')
                reasons, texts, audio = Mock(), [], []
                pipeline = Coordinator({direction: cfg}, services, reasons, lambda: 1)
                try:
                    for at in range(0, len(data), 3200):
                        pipeline.feed(direction, data[at:at + 3200])
                        await asyncio.sleep(0)
                        self.assertFalse(pipeline.consume(direction, texts.append, audio.append))
                    self.assertEqual((texts, audio), ([], []))
                    gate.set()
                    for _ in range(120):
                        await asyncio.sleep(0)
                        pipeline.consume(direction, texts.append, audio.append)
                    uploaded = [call.args[0] for call in services.source_asr.await_args_list]
                    self.assertEqual(len(uploaded), expected)
                    self.assertTrue(all(0 < len(pcm) <= 960000 for pcm in uploaded))
                    self.assertTrue(b''.join(uploaded) == data)
                    self.assertEqual(len(texts), expected)
                    self.assertEqual(services.validate_text.await_count, expected)
                    if direction == 'mine':
                        self.assertEqual(audio, [bytes(960)] * expected)
                        self.assertEqual(services.audio_asr.await_count, expected)
                        self.assertEqual(services.validate_audio.await_count, expected)
                    else:
                        self.assertEqual(audio, [])
                        services.synthesize.assert_not_awaited()
                        services.audio_asr.assert_not_awaited()
                    reasons.assert_not_called()
                finally:
                    await pipeline.close()


if __name__ == '__main__':
    unittest.main()
