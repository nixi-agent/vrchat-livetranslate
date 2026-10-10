"""Privacy-safe direction progress and capture failure visibility; no real devices."""
import contextlib
import io
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.engine import Engine, EngineEvents
from vlt.platform.win import PyaudioLoopbackSource
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_live import ChatGPTLiveSession


class ProgressTests(unittest.TestCase):
    def test_session_reports_received_events_even_when_late_done_is_not_emitted(self):
        s = ChatGPTLiveSession(SessionConfig(provider='chatgpt'))
        s.bridge = SimpleNamespace(ready=SimpleNamespace(is_set=lambda: True),
                                   _pending_pcm=3200, _consumed_pcm=6400)
        with patch('vlt.session.chatgpt_live.time.perf_counter', return_value=100):
            s._transcript({'role':'user', 'delta':'private-source'}, False)
            s._transcript({'role':'assistant', 'delta':'private-translation'}, False)
            s._finalized = True
            s._transcript({'role':'user', 'text':'private-late'}, True)
        with patch('vlt.session.chatgpt_live.time.perf_counter', return_value=102):
            d = s.diagnostics()
        self.assertEqual(d['source_events'], 2)
        self.assertEqual(d['translation_events'], 1)
        self.assertEqual(d['source_age_s'], 2)
        self.assertEqual(d['bridge_consumed_bytes'], 6400)
        self.assertEqual(d['bridge_pending_bytes'], 3200)
        self.assertNotIn('private', json.dumps(d))

    def test_direction_progress_is_bounded_and_does_not_copy_arbitrary_diagnostics(self):
        cfg = SimpleNamespace(session_base={'provider':'chatgpt'}, output={})
        with contextlib.redirect_stdout(io.StringIO()):
            e = Engine(cfg, 'theirs', 'loopback', set(), EngineEvents())
        e._session = SimpleNamespace(diagnostics=lambda:{'source_events':2,
            'translation_events':1, 'token':'private-credential', 'source':'private-speech'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            e._log_audio_progress(now=100)
            e._log_audio_progress(now=101)
            e._log_audio_progress(now=130)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('[theirs][progress]', lines[0])
        self.assertNotIn('unavailable', lines[0])
        self.assertEqual(json.loads(lines[0].split('] ',1)[1])['source_events'], 2)
        self.assertNotIn('private', out.getvalue())
        self.assertNotIn('token', out.getvalue())

    def test_non_chatgpt_progress_does_not_query_provider_diagnostics(self):
        cfg = SimpleNamespace(session_base={'provider':'qianwen'}, output={})
        with contextlib.redirect_stdout(io.StringIO()):
            e = Engine(cfg, 'mine', 'mic', set(), EngineEvents())
        diagnostics = Mock()
        e._session = SimpleNamespace(diagnostics=diagnostics)
        e._log_audio_progress(now=100)
        diagnostics.assert_not_called()

    def test_diagnostic_failure_cannot_stop_pump(self):
        cfg = SimpleNamespace(session_base={'provider':'chatgpt'}, output={})
        with contextlib.redirect_stdout(io.StringIO()):
            e = Engine(cfg, 'theirs', 'loopback', set(), EngineEvents())
        e._session = SimpleNamespace(diagnostics=Mock(side_effect=RuntimeError('private')))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            e._log_audio_progress(now=100)
        self.assertNotIn('private', out.getvalue())


class LoopbackFailureTests(unittest.TestCase):
    def test_poll_and_read_failures_are_visible_without_private_exception_details(self):
        for stage in ('poll', 'read'):
            with self.subTest(stage=stage):
                source = object.__new__(PyaudioLoopbackSource)
                source._chunk_max = 4800
                source._stream = Mock()
                source._stream.get_read_available.return_value = 4800
                failure = OSError(-9999, 'private-driver-detail')
                if stage == 'poll':
                    source._stream.get_read_available.side_effect = failure
                else:
                    source._stream.read.side_effect = failure
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    source._pump(threading.Event())
                self.assertIn('loopback', out.getvalue())
                self.assertIn('OSError', out.getvalue())
                self.assertIn('-9999', out.getvalue())
                self.assertNotIn('private-driver-detail', out.getvalue())

    def test_requested_stop_is_quiet(self):
        source = object.__new__(PyaudioLoopbackSource)
        stop = threading.Event()
        stop.set()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            source._pump(stop)
        self.assertFalse(out.getvalue())


if __name__ == '__main__':
    unittest.main()
