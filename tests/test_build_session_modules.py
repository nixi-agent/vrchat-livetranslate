"""Both packages must retain the subscription compatibility session module."""
from __future__ import annotations

import re
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_exe as windows


class BuildSessionModulesTests(unittest.TestCase):
    def test_subscription_session_is_collected_on_both_platforms(self):
        shell = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
        hidden = re.search(r"^HIDDEN=\((.*?)^\)", shell, re.M | re.S)
        self.assertIsNotNone(hidden)
        for platform, modules in (
            ("windows", windows.HIDDEN),
            ("linux", shlex.split(hidden.group(1), comments=True)),
        ):
            with self.subTest(platform=platform):
                self.assertIn("vlt.session.chatgpt_live", modules)

    def test_windows_first_party_hidden_imports_exist(self):
        for module in windows.HIDDEN:
            if module.startswith("vlt."):
                with self.subTest(module=module):
                    path = ROOT.joinpath(*module.split("."))
                    self.assertTrue(path.with_suffix(".py").is_file()
                                    or (path / "__init__.py").is_file(), module)


if __name__ == "__main__":
    unittest.main()
