"""Approval checks at the Xlib consumer; fake native calls, no display required."""
import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.platform.x11 import ArgbWindow
from vlt.session.chatgpt_output import scope


class Approval:
    valid = True

    def __call__(self, direction=None):
        return self.valid


class X11ApprovalTests(unittest.TestCase):
    def window(self, approval):
        out = ArgbWindow.__new__(ArgbWindow)
        out._dead, out._panel, out._alpha, out._dirty = False, None, 1.0, False
        out._img, out._win, out._size = 1, 1, (4, 4)
        out._buf = ctypes.create_string_buffer(64)
        out._x11, out._dpy, out._gc = Mock(), 1, 1
        out._has_compositor, out._cmap = True, 0
        with patch.object(out, '_draw'), scope(approval):
            out.set_panel(Image.new('RGBA', (4, 4), 'red'))
        return out

    def guarded(self, callback=None):
        def call(lib, dpy, fn):
            if callback:
                callback()
            return fn()
        return patch('vlt.platform.x11._guarded', side_effect=call)

    def test_revoked_cache_not_uploaded(self):
        approval = Approval()
        out = self.window(approval)
        approval.valid = False
        with self.guarded():
            out._draw()
        out._x11.XPutImage.assert_not_called()

    def test_valid_and_legacy_frames_still_upload(self):
        for approval in (Approval(), None):
            with self.subTest(approval=approval):
                out = self.window(approval)
                with self.guarded():
                    out._draw()
                out._x11.XPutImage.assert_called_once()

    def test_pixel_conversion_revocation(self):
        approval = Approval()
        out = self.window(approval)
        def pixels(*args):
            approval.valid = False
            return bytes(64)
        with patch('vlt.platform.x11.premultiplied_bgra', side_effect=pixels), self.guarded():
            out._draw()
        out._x11.XPutImage.assert_not_called()

    def test_buffer_copy_revocation(self):
        approval = Approval()
        out = self.window(approval)
        def copy(*args):
            approval.valid = False
        with patch('vlt.platform.x11.ctypes.memmove', side_effect=copy), self.guarded():
            out._draw()
        out._x11.XPutImage.assert_not_called()

    def test_x_error_guard_sync_revocation(self):
        approval = Approval()
        out = self.window(approval)
        with self.guarded(lambda: setattr(approval, 'valid', False)):
            out._draw()
        out._x11.XPutImage.assert_not_called()

    def test_bad_incoming_panel_preserves_good_cache(self):
        out, bad = self.window(Approval()), Approval()
        original = out._panel
        bad.valid = False
        with scope(bad), patch.object(out, '_draw'):
            out.set_panel(Image.new('RGBA', (4, 4), 'blue'))
        self.assertIs(out._panel, original)

    def test_alpha_update_does_not_relabel_old_panel(self):
        approval = Approval()
        out = self.window(approval)
        approval.valid = False
        with scope(Approval()), self.guarded():
            out.set_alpha(0.8)
        out._x11.XPutImage.assert_not_called()

    def test_revoked_panel_can_only_be_cleared_with_zero_alpha(self):
        approval = Approval()
        out = self.window(approval)
        approval.valid = False
        with self.guarded():
            out.set_alpha(0)
        out._x11.XPutImage.assert_called_once()
        self.assertEqual(out._buf.raw, bytes(64))

    def test_close_clears_cache_and_rejects_late_input(self):
        out = self.window(Approval())
        with self.guarded():
            out.close()
        self.assertIsNone(out._panel)
        self.assertFalse(out._dirty)
        with scope(Approval()), patch.object(out, '_draw') as draw:
            out.set_panel(Image.new('RGBA', (4, 4)))
        self.assertIsNone(out._panel)
        draw.assert_not_called()

    def test_caller_cannot_mutate_the_cached_image(self):
        out = self.window(Approval())
        image = Image.new('RGBA', (4, 4), 'red')
        with scope(Approval()), patch.object(out, '_draw'):
            out.set_panel(image)
        image.paste('blue', (0, 0, 4, 4))
        self.assertEqual(out._panel.getpixel((0, 0)), (255, 0, 0, 255))

    def test_copy_revocation_preserves_previous_cache(self):
        out, approval = self.window(Approval()), Approval()
        original = out._panel
        def copy(image):
            approval.valid = False
            return Image.new('RGBA', (4, 4), 'blue')
        with scope(approval), patch('PIL.Image.Image.copy', copy), patch.object(out, '_draw'):
            out.set_panel(Image.new('RGBA', (4, 4), 'blue'))
        self.assertIs(out._panel, original)


    def test_native_failure_reports_unavailable_and_clears_cached_frame(self):
        out = self.window(Approval())
        out.available = True
        out._dirty = True
        out._mark_dead('test failure')
        self.assertFalse(out.available)
        self.assertFalse(out._dirty)
        self.assertIsNone(out._panel)
        self.assertIsNone(out._panel_approval)


if __name__ == '__main__':
    unittest.main()
