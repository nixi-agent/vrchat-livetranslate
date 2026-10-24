"""TTS 失败警告不能被同一次打字发送的成功状态覆盖。"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_textin import _mk_engine, FakeChatbox, FakeOverlay, FakeVirtualMic  # noqa: E402
from vlt import engine as engine_mod  # noqa: E402
from vlt.engine import EngineEvents  # noqa: E402
from vlt.tts import TtsError  # noqa: E402


class StatusTests(unittest.TestCase):
    def engine(self, stream):
        engine = _mk_engine(tts={'stream': stream})
        engine._virtualmic, engine._chatbox, engine._overlay = FakeVirtualMic(), FakeChatbox(), FakeOverlay()
        statuses, texts = [], []
        engine._events = EngineEvents(on_status=lambda *args: statuses.append(args),
                                     on_text=lambda *args: texts.append(args))
        return engine, statuses, texts

    def test_failure_retains_warning_and_delivers_text_once(self):
        for stream in (True, False):
            for error in (TtsError('结果不明'), RuntimeError('unexpected failure')):
                with self.subTest(stream=stream, error=type(error).__name__):
                    engine, statuses, texts = self.engine(stream)
                    target = 'synthesize_stream' if stream else 'synthesize'
                    with patch.object(engine_mod, 'translate_text', return_value='translated'), \
                            patch.object(engine_mod, target, side_effect=error):
                        asyncio.run(engine._async_send_text('source'))
                    self.assertEqual(texts, [('source', 'translated', True)])
                    self.assertEqual(engine._chatbox.sent, [('translated', True)])
                    self.assertEqual(engine._overlay.updates, [('translated', 'source')])
                    self.assertEqual(engine._virtualmic.pushed, [])
                    self.assertEqual(statuses[-1][0], 'warn')
                    self.assertIn(str(error), statuses[-1][1])
                    self.assertFalse(engine._speak_lock().locked())

    def test_normal_tts_retains_success(self):
        pcm = bytes(48)
        for stream in (True, False):
            with self.subTest(stream=stream):
                engine, statuses, texts = self.engine(stream)
                with patch.object(engine_mod, 'translate_text', return_value='translated'), \
                        patch.object(engine_mod, 'synthesize', return_value=pcm), \
                        patch.object(engine_mod, 'synthesize_stream', return_value=iter((pcm,))):
                    asyncio.run(engine._async_send_text('source'))
                self.assertEqual(statuses[-1][0], 'info')
                self.assertEqual(len(texts), 1)
                self.assertEqual(len(engine._virtualmic.pushed), 1)
                self.assertEqual(engine._virtualmic.sentences, 1)


if __name__ == '__main__':
    unittest.main()
