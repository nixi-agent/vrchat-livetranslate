"""Long speech can close at a real pause without slicing the following onset."""
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


def pcm(value, samples):
    return struct.pack('<h', value) * samples


def feed(segmenter, data, chunk=3200):
    return [row for at in range(0, len(data), chunk)
            for row in segmenter.feed(data[at:at + chunk])]


class PauseSegmentsTests(unittest.TestCase):
    def test_four_second_section_can_close_at_an_existing_confirmed_pause(self):
        for samples in (58400, 64000, 80000):
            with self.subTest(samples=samples):
                s = Segmenter()
                first = pcm(800, samples) + pcm(100, 5600)
                self.assertEqual(feed(s, first + pcm(900, 320)), [])
                rows = feed(s, pcm(900, 160))
                self.assertEqual(len(rows), 1, 'a completed 4 s section need not wait until 6 s')
                self.assertTrue(rows[0].pcm == first)
                self.assertTrue(s.finish().pcm == pcm(900, 480))

    def test_capture_stall_length_pause_stays_with_the_resumed_speech(self):
        data = pcm(800, 96000) + pcm(100, 4800) + pcm(900, 16000) + pcm(0, 8000)
        for chunk in (258, 3200, 31998, 32000):
            with self.subTest(chunk=chunk):
                rows = feed(Segmenter(), data, chunk)
                self.assertEqual(len(rows), 1, '300 ms capture gap is not an early boundary')
                self.assertTrue(rows[0].pcm == data)

    def test_long_speech_closes_at_pause_after_next_onset_is_confirmed(self):
        for quiet in (5600, 7840):
            with self.subTest(quiet=quiet):
                s = Segmenter()
                first = pcm(800, 96000) + pcm(100, quiet)
                self.assertEqual(feed(s, first + pcm(900, 320)), [])
                rows = feed(s, pcm(900, 160))
                self.assertEqual(len(rows), 1, 'do not wait for the next 500 ms silence')
                self.assertTrue(rows[0].pcm == first, 'cut only in the completed quiet gap')
                rows.extend(feed(s, pcm(900, 16000) + pcm(0, 8000)))
                self.assertEqual(len(rows), 2)
                self.assertTrue(b''.join(r.pcm for r in rows) ==
                                first + pcm(900, 16480) + pcm(0, 8000))
                self.assertIsNone(s.finish())

    def test_short_pause_short_utterance_and_unconfirmed_restart_stay_together(self):
        for samples, quiet, resumed in ((96000, 5440, 480), (58240, 5600, 480),
                                        (96000, 5600, 320)):
            with self.subTest(samples=samples, quiet=quiet, resumed=resumed):
                s = Segmenter()
                data = pcm(800, samples) + pcm(100, quiet) + pcm(900, resumed)
                self.assertEqual(feed(s, data), [])
                self.assertTrue(s.finish().pcm == data)

    def test_interrupted_restart_keeps_the_cut_at_the_completed_pause(self):
        s = Segmenter()
        first = pcm(800, 96000) + pcm(100, 5600)
        second = pcm(900, 320) + pcm(0, 160) + pcm(900, 16000)
        rows = feed(s, first + second)
        self.assertEqual(len(rows), 1, 'a brief dip in the next onset must not erase a real pause')
        self.assertTrue(rows[0].pcm == first)
        self.assertTrue(s.finish().pcm == second)

    def test_pause_cut_is_independent_of_capture_chunks(self):
        first = pcm(800, 96000) + pcm(100, 5600)
        second = pcm(900, 80101)
        for chunk in (258, 3200, 31998, 32000):
            with self.subTest(chunk=chunk):
                s = Segmenter()
                rows = feed(s, first + second, chunk)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].pcm == first)
                self.assertTrue(s.finish().pcm == second)

    def test_finish_reset_and_invalid_input_clear_pending_pause(self):
        for action in ('finish', 'reset', 'invalid'):
            with self.subTest(action=action):
                s = Segmenter()
                original = pcm(800, 96000) + pcm(100, 5600) + pcm(900, 320)
                self.assertEqual(feed(s, original), [])
                if action == 'invalid':
                    with self.assertRaisesRegex(RuntimeError, '^pcm-invalid$'):
                        s.feed(b'x')
                elif action == 'finish':
                    self.assertTrue(s.finish().pcm == original)
                else:
                    s.reset()
                fresh = pcm(1000, 480) + pcm(0, 8000)
                rows = feed(s, fresh)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].pcm == fresh)

    def test_size_limit_clears_pending_pause_without_losing_resumed_speech(self):
        s = Segmenter()
        data = pcm(800, 474080) + pcm(100, 5600) + pcm(900, 640)
        rows = feed(s, data)
        rows.append(s.finish())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not r.reason and 0 < len(r.pcm) <= 960000 for r in rows))
        self.assertTrue(b''.join(r.pcm for r in rows) == data)

    def test_backpressure_clears_a_pause_waiting_for_onset_confirmation(self):
        s = Segmenter()
        data = pcm(800, 96000) + pcm(100, 5600) + pcm(900, 320)
        self.assertEqual(feed(s, data), [])
        self.assertEqual(s.feed(pcm(900, 160), allow_pause=False), [])
        self.assertEqual(s.feed(pcm(900, 480)), [])
        self.assertTrue(s.finish().pcm == data + pcm(900, 640))


class PauseBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconsumed_outputs_do_not_make_an_early_cut_drop_source(self):
        for direction in ('mine', 'theirs'):
            for ready_count in (1, 2):
                with self.subTest(direction=direction, ready_count=ready_count):
                    verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                                   negation=True, complete=True, extra_content=False)
                    services = SimpleNamespace(
                        source_asr=AsyncMock(side_effect=lambda raw, _: str(len(raw))),
                        translate=AsyncMock(return_value=dict(translation='翻訳', language='ja')),
                        validate_text=AsyncMock(return_value=verdict),
                        synthesize=AsyncMock(return_value=('voice_1', bytes(960))),
                        audio_asr=AsyncMock(return_value='翻訳'),
                        validate_audio=AsyncMock(return_value=verdict))
                    cfg = SessionConfig(provider='chatgpt', source_lang='en', target_lang='ja',
                                        output_audio=direction == 'mine')
                    reasons, texts, audio = Mock(), [], []
                    pipeline = Coordinator({direction: cfg}, services, reasons, lambda: 1)
                    async def idle():
                        for task in list(pipeline._active.values()):
                            await asyncio.wait_for(task, 1)
                    def consume():
                        while pipeline.consume(direction, texts.append, audio.append):
                            pass
                    try:
                        seed = pcm(800, 480)
                        for _ in range(ready_count):
                            self.assertTrue(pipeline.submit(direction, seed))
                            await idle()
                        data = pcm(900, 64000) + pcm(100, 5600) + pcm(1000, 480)
                        for at in range(0, len(data), 3200):
                            pipeline.feed(direction, data[at:at + 3200])
                        await idle()
                        if ready_count == 2:
                            consume()  # Consumer recovers before the ordinary 500 ms boundary.
                        pipeline.feed(direction, pcm(0, 8000))
                        await idle()
                        consume()
                        reasons.assert_not_called()
                        self.assertEqual([text.source for text in texts],
                                         [str(len(seed))] * ready_count + [str(len(data) + 16000)])
                        uploaded = [call.args[0] for call in services.source_asr.await_args_list]
                        self.assertTrue(uploaded[-1] == data + pcm(0, 8000))
                        self.assertEqual(len(audio), ready_count + 1 if direction == 'mine' else 0)
                    finally:
                        await pipeline.close()

    async def test_busy_other_direction_does_not_block_an_idle_direction(self):
        started, blocked = asyncio.Event(), asyncio.Event()
        async def source(*_):
            started.set()
            await blocked.wait()
        cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja')
        services = SimpleNamespace(source_asr=AsyncMock(side_effect=source))
        reasons = Mock()
        pipeline = Coordinator({'mine': cfg, 'theirs': cfg}, services, reasons, lambda: 1)
        try:
            pipeline.submit('mine', pcm(800, 480))
            await asyncio.wait_for(started.wait(), 1)
            first = pcm(900, 64000) + pcm(100, 5600)
            data = first + pcm(1000, 480)
            accepted = sum(pipeline.feed('theirs', data[at:at + 3200])
                           for at in range(0, len(data), 3200))
            self.assertEqual(accepted, 1)
            started.clear()
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(services.source_asr.await_args_list[-1].args[0] == first)
            reasons.assert_not_called()
        finally:
            await pipeline.close()

    async def test_busy_direction_retains_audio_until_the_normal_boundary(self):
        for direction in ('mine', 'theirs'):
            with self.subTest(direction=direction):
                started, blocked = asyncio.Event(), asyncio.Event()
                async def source(*_):
                    started.set()
                    await blocked.wait()
                cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja',
                                    output_audio=direction == 'mine')
                services = SimpleNamespace(source_asr=AsyncMock(side_effect=source))
                reasons = Mock()
                pipeline = Coordinator({direction: cfg}, services, reasons, lambda: 1)
                try:
                    first = pcm(800, 480) + pcm(0, 8000)
                    pipeline.feed(direction, first)
                    await asyncio.wait_for(started.wait(), 1)
                    second = pcm(900, 64000) + pcm(100, 5600) + pcm(1000, 480)
                    for at in range(0, len(second), 3200):
                        pipeline.feed(direction, second[at:at + 3200])
                    self.assertEqual(len(pipeline._pending[direction]), 0,
                                     'early pause must not add work behind a busy service')
                    self.assertEqual(pipeline.finish(direction), 1)
                    self.assertTrue(pipeline._pending[direction][0].pcm == second)
                    reasons.assert_not_called()
                finally:
                    await pipeline.close()


if __name__ == '__main__':
    unittest.main()
