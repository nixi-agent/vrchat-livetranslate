"""合成 PCM 的分段邊界；不使用音檔、ASR、音訊裝置或網路。"""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_segments import Segmenter


def pcm(value, samples):
    return struct.pack('<h', value) * samples


def feed(segmenter, data):
    result = []
    for start in range(0, len(data), 3200):
        result.extend(segmenter.feed(data[start:start + 3200]))
    return result


class SegmentsTests(unittest.TestCase):
    def test_explicit_boundary_preserves_the_partial_frame_and_is_idempotent(self):
        s = Segmenter()
        data = pcm(800, 480) + pcm(0, 101)
        self.assertEqual(feed(s, data), [])
        sealed = s.finish()
        self.assertEqual(sealed.reason, '')
        self.assertTrue(sealed.pcm == data)
        self.assertIsNone(s.finish())
        next_data = pcm(900, 480) + pcm(0, 8000)
        self.assertEqual(feed(s, next_data)[0].pcm, next_data)

    def test_boundary_discards_unconfirmed_onsets_instead_of_creating_noise_sections(self):
        for samples in (100, 160, 320, 420):
            with self.subTest(samples=samples):
                s = Segmenter()
                feed(s, pcm(800, samples))
                self.assertIsNone(s.finish())
                self.assertEqual(feed(s, pcm(900, 160) + pcm(0, 8000)), [])

    def test_boundary_seals_the_bounded_continuation_and_allows_a_fresh_utterance(self):
        s = Segmenter()
        source = pcm(500, 480001)
        sections = feed(s, source)
        sections.append(s.finish())
        self.assertTrue(all(not r.reason and len(r.pcm) <= 960000 for r in sections))
        self.assertTrue(b''.join(r.pcm for r in sections) == source)
        data = pcm(900, 480) + pcm(0, 8000)
        self.assertEqual(feed(s, data)[0].pcm, data)

    def test_isolated_onset_bursts_do_not_create_asr_candidates(self):
        for frames in (1, 2):
            with self.subTest(frames=frames):
                noise = pcm(800, 160 * frames) + pcm(0, 8000)
                self.assertEqual(len(feed(Segmenter(), noise * 4)), 0)

    def test_confirmed_onset_preserves_short_speech_and_preroll(self):
        data = pcm(100, 800) + pcm(800, 480) + pcm(0, 8000)
        results = feed(Segmenter(), data)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].pcm == data, 'onset confirmation must not trim source PCM')

    def test_onset_confirmation_survives_partial_frame_and_feed_boundary(self):
        s = Segmenter()
        self.assertEqual(s.feed(pcm(800, 320) + pcm(800, 100)), [])
        results = feed(s, pcm(800, 60) + pcm(0, 8000))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].pcm == pcm(800, 480) + pcm(0, 8000))

    def test_silence_never_becomes_an_utterance(self):
        self.assertEqual(feed(Segmenter(), pcm(219, 40000)), [])

    def test_sparse_clicks_below_window_energy_do_not_start_an_utterance(self):
        noise = pcm(1000, 1) + pcm(0, 159)
        self.assertEqual(len(feed(Segmenter(), noise * 3300)), 0)

    def test_low_energy_clicks_allow_speech_to_close_without_losing_pcm(self):
        speech = pcm(800, 1600)
        pause = (pcm(1000, 1) + pcm(0, 159)) * 50
        results = feed(Segmenter(), speech + pause)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].reason, '')
        self.assertTrue(results[0].pcm == speech + pause, 'sealed PCM must remain byte-identical')

    def test_noise_allows_the_bounded_continuation_to_close_without_losing_pcm(self):
        s = Segmenter()
        source = pcm(500, 480160)
        sections = feed(s, source)
        noise = (pcm(1000, 1) + pcm(0, 159)) * 50
        sections.extend(feed(s, noise))
        self.assertTrue(all(not r.reason and len(r.pcm) <= 960000 for r in sections))
        self.assertTrue(b''.join(r.pcm for r in sections) == source + noise)
        normal = feed(s, pcm(800, 480) + pcm(0, 8000))
        self.assertEqual(len(normal), 1)
        self.assertEqual(normal[0].reason, '')

    def test_window_classification_is_independent_of_feed_boundaries(self):
        data = pcm(800, 480) + (pcm(1000, 1) + pcm(0, 159)) * 50
        s = Segmenter()
        pieces = [s.feed(data[start:start + 2]) for start in range(0, len(data), 2)]
        results = [result for piece in pieces for result in piece]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].reason, '')
        self.assertTrue(results[0].pcm == data, 'chunk boundaries must not change sealed PCM')

    def test_sealing_keeps_the_partial_window_of_the_next_utterance(self):
        s = Segmenter()
        first = pcm(800, 480) + pcm(0, 8000)
        results = s.feed(first + pcm(900, 100))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].pcm == first)
        following = feed(s, pcm(900, 380) + pcm(0, 8000))
        self.assertEqual(len(following), 1)
        self.assertTrue(following[0].pcm == pcm(900, 480) + pcm(0, 8000))

    def test_threshold_exact_silence_and_preroll_are_sample_bound(self):
        s = Segmenter()
        self.assertEqual(feed(s, pcm(0, 32000)), [])
        self.assertEqual(feed(s, pcm(220, 480)), [])
        self.assertEqual(feed(s, pcm(0, 7999)), [])
        results = s.feed(pcm(0, 1))
        self.assertEqual(len(results), 1, '500 ms silence must close the voiced section')
        self.assertTrue(results[0].pcm == pcm(0, 15680) + pcm(220, 480) + pcm(0, 8000))

    def test_closed_samples_are_never_reused_as_preroll(self):
        results = feed(Segmenter(), pcm(400, 480) + pcm(0, 8000) +
                       pcm(600, 480) + pcm(0, 8000))
        self.assertEqual(len(results), 2, 'two actual utterances must remain separate')
        self.assertTrue(results[1].pcm == pcm(600, 480) + pcm(0, 8000))

    def test_limit_preserves_the_continuation_without_waiting_for_silence(self):
        s = Segmenter()
        source = pcm(500, 496000)
        results = feed(s, source)
        self.assertTrue(results and results[0].pcm, 'the input limit must end a bounded candidate')
        results.extend(feed(s, pcm(0, 8000)))
        self.assertTrue(all(not r.reason and len(r.pcm) <= 960000 for r in results))
        self.assertTrue(b''.join(r.pcm for r in results) == source + pcm(0, 8000))
        recovered = feed(s, pcm(700, 480) + pcm(0, 8000))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].reason, '')
        self.assertTrue(recovered[0].pcm == pcm(700, 480) + pcm(0, 8000))

    def test_reset_discards_partial_section(self):
        for samples in (100, 320, 480):
            with self.subTest(samples=samples):
                s = Segmenter()
                feed(s, pcm(500, samples))
                s.reset()
                results = feed(s, pcm(900, 480) + pcm(0, 8160))
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].pcm == pcm(900, 480) + pcm(0, 8000),
                                'reset must remove both pending and active PCM')

    def test_size_limit_at_silence_boundary_preserves_both_utterances(self):
        s = Segmenter()
        first = pcm(500, 472160) + pcm(0, 8000)
        next_data = pcm(700, 480) + pcm(0, 8000)
        sections = feed(s, first)
        sections.extend(feed(s, next_data))
        self.assertEqual(len(sections), 3)
        self.assertTrue(all(not r.reason and len(r.pcm) <= 960000 for r in sections))
        self.assertTrue(b''.join(r.pcm for r in sections) == first + next_data)

    def test_bad_pcm_is_rejected_without_retaining_partial_speech(self):
        for samples in (100, 320, 480):
            with self.subTest(samples=samples):
                s = Segmenter()
                feed(s, pcm(500, samples))
                with self.assertRaisesRegex(RuntimeError, '^pcm-invalid$'):
                    s.feed(b'x')
                results = feed(s, pcm(900, 480) + pcm(0, 8160))
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].pcm == pcm(900, 480) + pcm(0, 8000),
                                'invalid input must not contaminate the next section')

    def test_reset_and_invalid_pcm_clear_unconfirmed_onset(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                s = Segmenter()
                s.feed(pcm(800, 320))
                if invalid:
                    with self.assertRaisesRegex(RuntimeError, '^pcm-invalid$'):
                        s.feed(b'x')
                else:
                    s.reset()
                self.assertEqual(len(feed(s, pcm(900, 160) + pcm(0, 8000))), 0)


if __name__ == '__main__':
    unittest.main()
