import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_capture import Capture


class CaptureTests(unittest.TestCase):
    def seal(self, capture, **fields):
        capture.request_seal('0123456789abcdef')
        event = {'type': 'sealed', 'seal_id': '0123456789abcdef', 'samples': 768}
        event.update(fields)
        return capture.acknowledge(event)

    def test_tail_is_part_of_same_frozen_pcm(self):
        capture = Capture()
        capture.append(b'\x01\x00' * 480)
        capture.request_seal('0123456789abcdef')
        capture.append(b'\x02\x00' * 288)
        result = capture.acknowledge({'type': 'sealed', 'seal_id': '0123456789abcdef', 'samples': 768})
        self.assertEqual(result, b'\x01\x00' * 480 + b'\x02\x00' * 288)
        with self.assertRaisesRegex(RuntimeError, '^capture-sealed$'):
            capture.append(b'\x03\x00')

    def test_count_id_and_ack_order_must_match(self):
        for fields in ({'samples': 769}, {'samples': True}, {'seal_id': 'other'}, {'type': 'done'}):
            capture = Capture()
            capture.append(bytes(1536))
            with self.assertRaisesRegex(RuntimeError, '^capture-ack$'):
                self.seal(capture, **fields)
        with self.assertRaisesRegex(RuntimeError, '^capture-ack$'):
            Capture().acknowledge({'type': 'sealed', 'samples': 0})

    def test_malformed_and_oversized_audio_invalidate_candidate(self):
        for pcm in (b'x', bytes(48002)):
            capture = Capture()
            with self.assertRaisesRegex(RuntimeError, '^capture-pcm$'):
                capture.append(pcm)
            with self.assertRaises(RuntimeError):
                self.seal(capture, samples=0)
        capture = Capture()
        with self.assertRaisesRegex(RuntimeError, '^capture-size$'):
            for _ in range(91):
                capture.append(bytes(48000))

    def test_seal_is_required_even_when_connection_goes_quiet(self):
        capture = Capture()
        capture.append(bytes(1536))
        with self.assertRaisesRegex(RuntimeError, '^capture-ack$'):
            capture.acknowledge({'type': 'sealed', 'seal_id': '0123456789abcdef', 'samples': 768})


if __name__ == '__main__':
    unittest.main()
