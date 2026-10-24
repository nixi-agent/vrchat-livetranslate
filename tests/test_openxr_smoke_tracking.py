"""Diagnostics read worker snapshots without XR calls on the smoke caller thread."""
import contextlib
import io
import itertools
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.output.openxr_overlay import OpenXrOverlay
from vlt.output.overlay import OverlayConfig
from vlt.output import openxr_demo as demo


class SmokeTrackingTests(unittest.TestCase):
    def test_smoke_periodic_and_final_reports_do_not_call_session_xr_methods(self):
        overlay = OpenXrOverlay(OverlayConfig(), dry_run=True)
        overlay._sess = NS(state='FOCUSED', anchor_tracked=Mock(return_value=True))
        overlay.start, overlay.tick, overlay.update, overlay.close = Mock(return_value=True), Mock(), Mock(), Mock()
        clock = itertools.count(0, .6)
        with tempfile.TemporaryDirectory() as folder, patch('vlt.paths.APP_DIR', Path(folder)), patch(
            'vlt.output.openxr_overlay.OpenXrOverlay', return_value=overlay), patch.object(
            demo.time, 'monotonic', side_effect=lambda: next(clock)), patch.object(
            demo.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(demo._smoke(3), 0)
        self.assertIn('会话状态=', output.getvalue())
        overlay._sess.anchor_tracked.assert_not_called()

    def test_tracking_is_observed_on_worker_and_read_as_session_bound_snapshot(self):
        overlay = OpenXrOverlay(OverlayConfig())
        overlay._pending_img = (Image.new('RGBA', (4, 4)), None, overlay._epoch)
        ready, resume = threading.Event(), threading.Event()
        owners, pumps = [], []
        def pump():
            pumps.append(threading.get_ident())
            if len(pumps) > 1:
                ready.set()
                resume.wait(2)
                overlay._frame_stop.set()
        def tracked(*args):
            owners.append(threading.get_ident())
            return True
        session = overlay._sess = NS(state=2, pump_events=pump, submit=Mock(return_value=True),
                                     clear=Mock(), anchor_tracked=tracked)
        fake_xr = NS(SessionState=NS(READY=2, FOCUSED=5))
        with patch.dict(sys.modules, {'xr': fake_xr}):
            worker = threading.Thread(target=overlay._frame_loop)
            worker.start()
            try:
                self.assertTrue(ready.wait(1))
                self.assertIs(getattr(overlay, 'anchor_tracked', None), True)
                self.assertEqual(owners, [worker.ident])
                self.assertNotEqual(worker.ident, threading.get_ident())
                anchor = overlay.cfg.anchor
                overlay.cfg.anchor = 'different-anchor'
                self.assertIsNone(overlay.anchor_tracked)
                overlay.cfg.anchor = anchor
                overlay._sess = object()
                self.assertIsNone(overlay.anchor_tracked)
                overlay._sess = session
                overlay._frame_stop.set()
                self.assertIsNone(overlay.anchor_tracked)
            finally:
                resume.set()
                overlay._frame_stop.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
