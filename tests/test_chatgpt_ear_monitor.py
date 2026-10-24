"""Delayed sidetone must retain and recheck each subscription audio approval."""
import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.output.earmonitor import EarMonitor
from vlt.output.micproxy import MicProxy, _TranslatedBuffer
from vlt.session.chatgpt_output import scope


class MonitorApprovalTests(unittest.TestCase):
    def setUp(self):
        self.monitor = EarMonitor(volume=100)
        self.monitor._opened = True
        self.valid = True
        self.approval = lambda direction=None: self.valid and direction in (None, 'mine')

    def play(self, frames):
        result = np.zeros((frames, 2), dtype=np.int16)
        self.monitor._callback(result, frames, None, None)
        return result.tobytes()

    def test_revoked_queued_audio_is_silenced_without_dropping_legacy_neighbor(self):
        with scope(self.approval):
            self.monitor.push(b'\x11\x00' * 4)
        self.monitor.push(b'\x22\x00' * 4)
        self.valid = False
        self.assertEqual(self.play(4), b'\x00' * 8 + b'\x22\x00' * 4)

    def test_overflow_keeps_approval_boundaries_aligned(self):
        self.monitor._cap_bytes = 12
        with scope(self.approval):
            self.monitor.push(b'\x11\x00' * 4)
        self.monitor.push(b'\x22\x00' * 4)
        self.valid = False
        self.assertEqual(self.play(3), b'\x00' * 4 + b'\x22\x00' * 4)
        self.monitor.close()
        self.monitor._opened = True
        self.monitor.push(b'\x33\x00' * 2)
        self.assertEqual(self.play(1), b'\x33\x00' * 2)

    def test_actual_playout_tap_retains_approval_until_monitor_callback(self):
        proxy = MicProxy(audio_cfg={'monitor': {'enabled': False}})
        proxy._translated = _TranslatedBuffer(None, 'offline', buffer_ms=0)
        proxy._monitor = self.monitor
        chunk = b'\x11\x00' * 960
        with scope(self.approval):
            proxy.translated_sink.push(chunk)
            proxy.translated_sink.end_sentence()
        self.assertEqual(proxy._drain_translated(len(chunk)), chunk)
        self.valid = False
        self.assertEqual(self.play(480), b'\x00' * len(chunk))

    def test_revocation_before_playout_never_enqueues_monitor_content(self):
        proxy = MicProxy(audio_cfg={'monitor': {'enabled': False}})
        proxy._translated = _TranslatedBuffer(None, 'offline', buffer_ms=0)
        proxy._monitor = self.monitor
        chunk = b'\x11\x00' * 960
        with scope(self.approval):
            proxy.translated_sink.push(chunk)
            proxy.translated_sink.end_sentence()
        self.valid = False
        self.assertEqual(proxy._drain_translated(len(chunk)), b'\x00' * len(chunk))
        self.assertEqual(self.play(480), b'\x00' * len(chunk))


if __name__ == '__main__':
    unittest.main()
