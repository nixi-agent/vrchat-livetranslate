"""Offline desktop cache/queue approval checks; no native window or service."""
import sys
import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.output.desktop_overlay import DesktopOverlay, DesktopOverlayConfig
from vlt.session.chatgpt_output import combined, current, scope


class Approval:
    def __init__(self):
        self.valid = True

    def __call__(self, direction=None):
        return self.valid


class DesktopApprovalTests(unittest.TestCase):
    def output(self):
        out = DesktopOverlay(DesktopOverlayConfig(attach_to_game=False))
        out._native = Mock()
        return out

    def entries(self, out, approval, text='translation', force=False):
        with scope(approval):
            out.update_entries([('mine', 'source', text, '')], force=force)

    def test_revoked_dirty_frame_never_reaches_native_sink(self):
        out, approval = self.output(), Approval()
        self.entries(out, approval)
        approval.valid = False
        out._redraw()
        out._native.set_panel.assert_not_called()

    def test_approved_dirty_frame_reaches_native_sink_once_with_identity(self):
        out, approval = self.output(), Approval()
        seen = []
        out._native.set_panel.side_effect = lambda *args: seen.append(current())
        self.entries(out, approval)
        out._redraw()
        self.assertEqual(seen, [approval])

    def test_early_revoked_input_does_not_replace_good_frame(self):
        out, good, bad = self.output(), Approval(), Approval()
        self.entries(out, good)
        bad.valid = False
        self.entries(out, bad, 'bad')
        out._redraw()
        self.assertEqual(out._latest[0], 'translation')
        out._native.set_panel.assert_called_once()

    def test_render_revocation_is_checked_before_native_call(self):
        out, approval = self.output(), Approval()
        self.entries(out, approval)
        def render(*args):
            approval.valid = False
            return Image.new('RGBA', (4, 4))
        with patch.object(out, '_render', side_effect=render):
            out._redraw()
        out._native.set_panel.assert_not_called()

    def test_independent_approved_repeated_sentence_is_not_deduplicated(self):
        out = self.output()
        for approval in (Approval(), Approval()):
            self.entries(out, approval)
            out.tick() if out._started else out._redraw()
            self.assertTrue(out._last_sig is not None)
        self.assertEqual(out._native.set_panel.call_count, 2)

    def test_legacy_repeated_sentence_still_deduplicates(self):
        out = self.output()
        self.entries(out, None)
        out._redraw()
        self.entries(out, None)
        self.assertFalse(out._dirty)

    def test_repeated_new_identity_marks_frame_dirty(self):
        out = self.output()
        self.entries(out, Approval())
        out._redraw()
        self.entries(out, Approval())
        self.assertTrue(out._dirty)

    def test_same_rows_with_fresh_combined_wrapper_still_deduplicate(self):
        out, approval = self.output(), Approval()
        self.entries(out, combined(((approval, 'mine'),)))
        out._redraw()
        self.entries(out, combined(((approval, 'mine'),)))
        self.assertFalse(out._dirty)

    def test_closing_during_row_capture_clears_the_old_update(self):
        out, approval = self.output(), Approval()
        def rows():
            out.close()
            yield ('mine', 'source', 'old', '')
        with scope(approval):
            out.update_entries(rows())
        self.assertEqual(out._entries, [])
        self.assertEqual(out._latest, ('', ''))
        self.assertFalse(out._dirty)

    def test_force_redraw_retains_revoked_identity(self):
        out, approval = self.output(), Approval()
        self.entries(out, approval)
        out._redraw()
        out._native.reset_mock()
        approval.valid = False
        out._last_sig, out._dirty = None, True
        out._redraw()
        out._native.set_panel.assert_not_called()

    def test_close_clears_cached_content_and_pending_redraw(self):
        out = self.output()
        self.entries(out, Approval())
        out.close()
        self.assertEqual(out._entries, [])
        self.assertEqual(out._latest, ('', ''))
        self.assertFalse(out._dirty)

    def test_concurrent_update_during_render_keeps_content_and_identity_paired(self):
        out, first, second = self.output(), Approval(), Approval()
        self.entries(out, first, 'first')
        seen = []
        out._native.set_panel.side_effect = lambda *args: seen.append(current())
        def render(text, source, cfg):
            self.assertEqual(text, 'first')
            self.entries(out, second, 'second')
            return Image.new('RGBA', (4, 4))
        out.cfg.mode = 'latest'
        with patch('vlt.output.desktop_overlay.render_panel', side_effect=render):
            out._redraw()
        self.assertEqual(seen, [first])
        self.assertTrue(out._dirty)
        out._redraw()
        self.assertEqual(seen, [first, second])

    def test_interleaved_updates_cannot_relabel_another_segments_content(self):
        out, first, second = self.output(), Approval(), Approval()
        entered, release = threading.Event(), threading.Event()
        original, errors = out._touch, []
        def pause(*args):
            if current() is first:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('second update did not release first')
            original(*args)
        def worker():
            try:
                self.entries(out, first, 'first')
            except Exception as exc:
                errors.append(exc)
        with patch.object(out, '_touch', side_effect=pause):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.entries(out, second, 'second')
            finally:
                release.set()
                thread.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        out.cfg.mode = 'latest'
        with patch('vlt.output.desktop_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))) as render:
            out._redraw()
        self.assertEqual(render.call_args.args[0], 'first')

    def test_close_and_new_window_during_render_drops_old_frame(self):
        out, approval, replacement = self.output(), Approval(), Mock()
        self.entries(out, approval)
        def render(*args):
            out.close()
            out._native = replacement
            return Image.new('RGBA', (4, 4))
        with patch.object(out, '_render', side_effect=render):
            out._redraw()
        replacement.set_panel.assert_not_called()

    def test_tk_composite_revocation_never_creates_photo(self):
        out, approval = DesktopOverlay(DesktopOverlayConfig()), Approval()
        out._win, out._label = Mock(), Mock()
        self.entries(out, approval)
        def composite(*args):
            approval.valid = False
            return Image.new('RGB', (4, 4))
        with patch.object(out, '_composite', side_effect=composite), patch('PIL.ImageTk.PhotoImage') as photo:
            out._redraw()
        photo.assert_not_called()

    def test_tick_hides_revoked_native_frame(self):
        out, approval = self.output(), Approval()
        out._started = True
        self.entries(out, approval)
        out._redraw()
        approval.valid = False
        out.tick()
        out._native.set_alpha.assert_called_with(0.0)

    def test_alpha_change_cannot_reveal_old_revoked_native_frame(self):
        out, approval = self.output(), Approval()
        self.entries(out, approval)
        out._redraw()
        approval.valid = False
        out.set_alpha(0.6)
        out._native.set_alpha.assert_called_with(0.0)
        out._apply_alpha_only()
        out._native.set_alpha.assert_called_with(0.0)

    def test_tk_photo_creation_revocation_never_configures_label(self):
        out, approval = DesktopOverlay(DesktopOverlayConfig(attach_to_game=False)), Approval()
        out._win, out._label = Mock(), Mock()
        self.entries(out, approval)
        def photo(*args, **kwargs):
            approval.valid = False
            return object()
        with patch('PIL.ImageTk.PhotoImage', side_effect=photo):
            out._redraw()
        out._label.configure.assert_not_called()

    def test_tk_tick_hides_old_frame_and_fresh_frame_restores_alpha(self):
        out, approval = DesktopOverlay(DesktopOverlayConfig(attach_to_game=False)), Approval()
        out._win, out._label = Mock(), Mock()
        out._started = True
        self.entries(out, approval)
        with patch('PIL.ImageTk.PhotoImage', return_value=object()):
            out._redraw()
            approval.valid = False
            out.tick()
            out._win.attributes.assert_called_with('-alpha', 0.0)
            self.entries(out, Approval(), 'fresh')
            out._redraw()
            out._win.attributes.assert_called_with('-alpha', out.cfg.alpha)

    def test_failed_native_hide_does_not_stop_actual_gui_poll(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        out, approval = self.output(), Approval()
        out._started = True
        self.entries(out, approval)
        out._redraw()
        approval.valid = False
        native, root = out._native, Mock()
        native.set_alpha.side_effect = RuntimeError('hide failed')
        ctx = ChatCtx(q=queue.Queue(), desktop_out_fn=lambda: out)
        failure = None
        try:
            poll(ctx, root, Mock(), Mock(), Mock(), {})
        except Exception as exc:
            failure = exc
        self.assertIsNone(failure)
        root.after.assert_called_once()
        native.close.assert_called_once()
        self.assertFalse(out.available)
        self.assertFalse(out._dirty)

    def test_manual_native_alpha_failure_is_contained(self):
        out, approval = self.output(), Approval()
        self.entries(out, approval)
        out._redraw()
        native = out._native
        native.set_alpha.side_effect = RuntimeError('alpha failed')
        failure = None
        try:
            out.set_alpha(0.6)
        except Exception as exc:
            failure = exc
        self.assertIsNone(failure)
        native.close.assert_called_once()

    def test_close_during_native_delivery_does_not_restore_old_state(self):
        out = self.output()
        self.entries(out, Approval())
        replacement = Mock()
        def deliver(*args):
            out.close()
            out._native = replacement
        out._native.set_panel.side_effect = deliver
        out._redraw()
        self.assertIsNone(out._displayed_approval)
        self.assertIsNone(out._content)
        self.assertEqual(out.frame_count, 0)
        replacement.set_panel.assert_not_called()

    def test_close_during_tk_delivery_does_not_restore_photo_or_count(self):
        out = DesktopOverlay(DesktopOverlayConfig(attach_to_game=False))
        out._win, out._label = Mock(), Mock()
        self.entries(out, Approval())
        out._label.configure.side_effect = lambda **kwargs: out.close()
        with patch('PIL.ImageTk.PhotoImage', return_value=object()):
            out._redraw()
        self.assertIsNone(out._displayed_approval)
        self.assertIsNone(out._photo)
        self.assertEqual(out.frame_count, 0)

    def mixed(self, mode, *, legacy_last=False):
        from vlt.gui_sinks import push_desktop
        out = self.output()
        out._started, out.cfg.mode = True, mode
        old, new = Approval(), None if legacy_last else Approval()
        bubbles = [SimpleNamespace(who='theirs', source='old', text='old', label='', approval=old),
                   SimpleNamespace(who='mine', source='fresh', text='fresh', label='', approval=new)]
        push_desktop(SimpleNamespace(bubbles=bubbles, desktop_out=out))
        return out, old, new

    def test_latest_visible_frame_is_not_hidden_by_old_unshown_row(self):
        out, old, new = self.mixed('latest')
        out._redraw()
        old.valid = False
        out.tick()
        self.assertTrue(new.valid)
        out._native.set_alpha.assert_not_called()

    def test_latest_can_render_when_old_row_revoked_before_delivery(self):
        out, old, new = self.mixed('latest')
        old.valid = False
        out._redraw()
        out._native.set_panel.assert_called_once()

    def test_latest_legacy_row_does_not_inherit_prior_chatgpt_approval(self):
        out, old, _ = self.mixed('latest', legacy_last=True)
        out._redraw()
        old.valid = False
        out.tick()
        out._native.set_alpha.assert_not_called()

    def test_conversation_redraw_removes_invalid_row_and_keeps_fresh_row(self):
        out, old, _ = self.mixed('conversation')
        out._redraw()
        old.valid = False
        with patch('vlt.output.desktop_overlay.render_conversation',
                   return_value=Image.new('RGBA', (4, 4))) as render:
            out.tick()
        render.assert_called_once()
        self.assertEqual(render.call_args.args[0], [('mine', 'fresh', 'fresh', '')])
        self.assertTrue(out._displayed_approval())

    def test_failed_native_delivery_allows_next_same_legal_input_to_retry(self):
        out, approval = self.output(), Approval()
        out._native.set_panel.side_effect = [RuntimeError('first failed'), None]
        self.entries(out, approval)
        out._redraw()
        self.entries(out, approval)
        if out._dirty:
            out._redraw()
        self.assertEqual(out._native.set_panel.call_count, 2)
        self.entries(out, approval)
        self.assertFalse(out._dirty)

    def test_empty_legacy_bubble_does_not_shift_other_rows_approvals(self):
        from vlt.gui_sinks import push_desktop
        out = self.output()
        bubbles = [SimpleNamespace(who='theirs', source='partial', text='', label='', approval=None),
                   SimpleNamespace(who='mine', source='fresh', text='fresh', label='', approval=Approval())]
        push_desktop(SimpleNamespace(bubbles=bubbles, desktop_out=out))
        out._redraw()
        self.assertEqual(out._latest, ('fresh', 'fresh'))
        out._native.set_panel.assert_called_once()



if __name__ == '__main__':
    unittest.main()
