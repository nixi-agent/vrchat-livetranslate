"""Level changes must persist before updating memory or running audio settings."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vlt.gui as gui_module
import vlt.gui_level as level_module
from vlt.gui_level import LevelMethods

DEFAULT_CONFIG = None
_yaml_write = gui_module._yaml_write


class Holder(LevelMethods):
    def __init__(self, running):
        self._cfg = SimpleNamespace(output={"audio": {"level": {
            "mode": "off", "fixed_gain_db": 0.0, "offset_db": 0.0,
        }}})
        self._level_mode_combo = object()
        self._proxy = SimpleNamespace(set_level_config=Mock()) if running else None
        self._engines = []
        self._set_status = Mock()
        self._sync_level_controls_state = Mock()

    def _level_mode_code(self):
        return "fixed"

    def _level_read_db(self, _name, _lo, _hi, key):
        return -4.5 if key == "fixed_gain_db" else 0.0


class LevelSaveTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(temp) / "config.yaml"
        self.original = "# keep this comment\noutput:\n  audio:\n    buffer_ms: 300\n"
        self.path.write_text(self.original, encoding="utf-8")
        self.enterContext(patch.object(sys.modules[__name__], "DEFAULT_CONFIG", self.path))

    def change(self, holder):
        with contextlib.redirect_stdout(io.StringIO()) as log:
            holder._on_level_change()
        return log.getvalue()

    def assert_failed(self, holder, log):
        self.assertEqual(holder._level_cfg_mem(), {
            "mode": "off", "fixed_gain_db": 0.0, "offset_db": 0.0,
        })
        self.assertEqual(holder._set_status.call_args.args[0], "warn")
        self.assertNotIn("已落盘", log)
        self.assertNotIn("译音音量已更新", log)
        if holder._proxy is not None:
            holder._proxy.set_level_config.assert_not_called()

    def assert_retry(self, holder):
        self.change(holder)
        self.assertEqual(holder._level_cfg_mem()["fixed_gain_db"], -4.5)
        self.assertEqual(holder._set_status.call_args.args[0], "info")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("fixed_gain_db: -4.5", text)
        self.assertIn("# keep this comment", text)
        self.assertIn("buffer_ms: 300", text)
        if holder._proxy is not None:
            holder._proxy.set_level_config.assert_called_once()

    def test_write_failure_keeps_old_settings_and_same_values_retry(self):
        for error in (PermissionError("read-only config"), ValueError("invalid YAML")):
            for running in (False, True):
                with self.subTest(error=type(error).__name__, running=running):
                    self.path.write_text(self.original, encoding="utf-8")
                    holder = Holder(running)
                    with patch.object(gui_module, "_write_config_text", side_effect=error), \
                            patch.object(level_module, "_write_config_text", side_effect=error, create=True):
                        log = self.change(holder)
                    self.assert_failed(holder, log)
                    self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
                    self.assert_retry(holder)

    def test_missing_file_warns_and_retries_when_file_returns(self):
        holder = Holder(False)
        self.path.unlink()
        self.assert_failed(holder, self.change(holder))
        self.assertFalse(self.path.exists())
        self.path.write_text(self.original, encoding="utf-8")
        self.assert_retry(holder)


if __name__ == "__main__":
    unittest.main()
