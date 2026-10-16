"""Actual Engine admission must reject subscription output without approval."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.engine import Engine, EngineEvents
from vlt.session.base import SessionConfig, TextDelta
from vlt.session.chatgpt_output import scope
from vlt.session.chatgpt_admission import Admission
from test_repeat_guard import _cfg


class AdmissionTests(unittest.TestCase):
    def engine(self, provider='chatgpt', direction='mine'):
        cfg = _cfg(provider=provider)
        cfg.directions['theirs'] = cfg.directions['mine']
        events = EngineEvents(on_text=Mock())
        engine = Engine(cfg, direction, 'mic', {'chatbox'}, events)
        engine._overlay, engine._merger, engine._virtualmic = Mock(), Mock(), Mock()
        return engine

    def test_unapproved_subscription_never_reaches_any_engine_outlet(self):
        for direction in ('mine', 'theirs'):
            with self.subTest(direction=direction):
                engine = self.engine(direction=direction)
                engine._on_text(TextDelta(confirmed='Translation system bypassed', source='source', is_final=True))
                engine._on_audio(bytes(2))
                engine._events.on_text.assert_not_called()
                engine._overlay.update.assert_not_called()
                engine._merger.push.assert_not_called()
                engine._virtualmic.push.assert_not_called()
                self.assertEqual((engine._text_deltas, engine._audio_chunks), (0, 0))
                self.assertFalse(engine._pending_seal)

    def test_approved_theirs_remains_captions_only(self):
        engine = self.engine(direction='theirs')
        engine._session = SimpleNamespace(cfg=engine._cfg.directions['theirs'].to_session_config(engine._cfg.session_base))
        engine._admission = Admission(engine, engine._session)
        with scope(lambda direction=None: direction in (None, 'theirs')):
            engine._admission.wrap(engine._on_text)(TextDelta(confirmed='translation', source='source', is_final=True))
            engine._admission.wrap(engine._on_audio)(bytes(2))
        engine._events.on_text.assert_called_once_with('source', 'translation', True)
        engine._overlay.update.assert_called_once_with('translation', 'source')
        engine._virtualmic.push.assert_not_called()
        engine._merger.push.assert_not_called()

    def test_qwen_legacy_output_does_not_require_subscription_approval(self):
        for provider in ('qianwen', 'qwencloud'):
            with self.subTest(provider=provider):
                engine = self.engine(provider=provider)
                engine._on_text(TextDelta(confirmed='translation', source='source', is_final=True))
                engine._on_audio(bytes(960))
                engine._events.on_text.assert_called_once()
                engine._merger.push.assert_called_once()
                engine._virtualmic.push.assert_called_once()

    def test_changing_provider_selector_cannot_authorize_old_subscription_output(self):
        engine = self.engine()
        engine._session = SimpleNamespace(cfg=SessionConfig(provider='chatgpt'))
        engine._cfg.session_base['provider'] = 'qianwen'
        engine._on_text(TextDelta(confirmed='unapproved', is_final=True))
        engine._on_audio(bytes(2))
        self.assertEqual(engine._events.on_text.call_count, 0)
        self.assertEqual(engine._virtualmic.push.call_count, 0)


if __name__ == '__main__':
    unittest.main()
