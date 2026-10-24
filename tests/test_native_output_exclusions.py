"""Offline Windows packaging boundaries for Linux renderer implementation files."""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_platform_purity import FORBIDDEN


class NativeOutputExclusionTests(unittest.TestCase):
    def test_every_windows_wrist_module_is_forbidden_on_linux(self):
        paths = list((ROOT / 'vlt/output').glob('wrist_*.py'))
        self.assertGreaterEqual(len(paths), 4)
        for path in paths:
            name = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
            with self.subTest(module=name):
                self.assertIn(name, FORBIDDEN['linux']['modules'])

    def test_every_linux_renderer_module_is_excluded_and_forbidden_on_windows(self):
        tree = ast.parse((ROOT / 'scripts/build_exe.py').read_text(encoding='utf-8'))
        excluded = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == 'EXCLUDE_WIN' for t in n.targets))
        paths = list((ROOT / 'vlt/output').glob('openxr_*.py'))
        paths += [ROOT / 'vlt/platform' / 'x11.py', ROOT / 'vlt/platform' / 'x11_bindings.py']
        paths += list((ROOT / 'vlt/platform').glob('wayland*.py'))
        self.assertGreaterEqual(len(paths), 13)
        for path in paths:
            name = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
            with self.subTest(module=name):
                self.assertIn(name, excluded)
                self.assertIn(name, FORBIDDEN['windows']['modules'])


if __name__ == '__main__':
    unittest.main()
