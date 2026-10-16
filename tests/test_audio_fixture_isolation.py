"""Offline fixtures must guard audio without importing optional device drivers."""
import importlib
import sys
import unittest
from pathlib import Path
from audio_test_fakes import no_audio_device

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FixtureIsolationTests(unittest.TestCase):
    def test_audio_guards_replace_and_restore_optional_driver(self):
        for module, name in (
            ('test_audio_approval', 'AudioApprovalTests'),
            ('test_audio_output_tail', 'PlaybackTailTests'),
            ('test_chatgpt_validated', 'ValidatedTests'),
        ):
            cls = getattr(importlib.import_module(module), name)
            class GuardedFixture(cls):
                def runTest(self):
                    sys.modules['_audio_guard_lazy_import'] = original
                    driver = importlib.import_module('sounddevice')
                    driver.RawOutputStream.assert_not_called()
                    with self.assertRaisesRegex(AssertionError, 'device opened'):
                        driver.RawOutputStream()

            missing = object()
            for original in (missing, None, object()):
                with self.subTest(fixture=name, driver_present=original is not None):
                    with no_audio_device():
                        sys.modules['sounddevice'] = original
                        if original is missing:
                            sys.modules.pop('sounddevice')
                        result = unittest.TestResult()
                        GuardedFixture('runTest').run(result)
                        self.assertTrue(result.wasSuccessful(),
                                        'offline fixture must supply and clean up a guarded driver')
                        self.assertIs(sys.modules.pop('_audio_guard_lazy_import'), original)
                        if original is missing:
                            self.assertNotIn('sounddevice', sys.modules)
                        else:
                            self.assertIs(sys.modules['sounddevice'], original)
        package = importlib.import_module('vlt.session')
        for name in ('chatgpt_auth', 'chatgpt_relay', 'chatgpt_validated', 'chatgpt_live'):
            self.assertIs(getattr(package, name), importlib.import_module('vlt.session.' + name))


if __name__ == '__main__':
    unittest.main()
