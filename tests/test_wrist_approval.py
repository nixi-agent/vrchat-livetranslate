"""Offline wrist render/cache approval checks; fake native calls only."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.output.openvr_overlay import WristOverlay
from vlt.output.overlay import OverlayConfig
from vlt.session.chatgpt_output import combined, scope


class Approval:
    def __init__(self):
        self.valid = True
    def __call__(self, direction=None):
        return self.valid


class WristApprovalTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('vlt.output.openvr_overlay.D3D11Texture',
                                side_effect=AssertionError('real GPU opened')))

    def wrist(self):
        out = WristOverlay(OverlayConfig())
        out.available, out._overlay, out._handle = True, Mock(), 1
        del out._overlay.setOverlayTexture
        return out

    def repaint(self, out):
        with patch.object(out, '_resolve_anchor', return_value=0), patch.object(out, '_apply_transform'):
            self.assertTrue(out._recreate_overlay())

    def test_legacy_repeated_text_still_deduplicated(self):
        out = self.wrist()
        with patch('vlt.output.openvr_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))):
            out.update('text')
            out.update('text')
        out._overlay.setOverlayRaw.assert_called_once()

    def test_invalid_input_never_uploaded_or_cached_for_rebuild(self):
        out, approval = self.wrist(), Approval()
        approval.valid = False
        with scope(approval):
            out.update('invalid')
            out.update_entries([('mine', '', 'invalid', '')])
        self.repaint(out)
        out._overlay.setOverlayRaw.assert_not_called()

    def test_revoked_during_pixel_conversion_never_uploaded(self):
        out, approval = self.wrist(), Approval()
        img = Mock(mode='RGBA', size=(4, 4))
        def pixels():
            approval.valid = False
            return bytes(64)
        img.tobytes.side_effect = pixels
        with scope(approval):
            out._upload(img)
        out._overlay.setOverlayRaw.assert_not_called()

    def test_text_mode_rebuild_keeps_its_own_approval_and_content(self):
        out, old, new = self.wrist(), Approval(), Approval()
        with patch('vlt.output.openvr_overlay.render_conversation',
                   return_value=Image.new('RGBA', (4, 4))), scope(old):
            out.update_entries([('mine', '', 'old', '')])
        with patch('vlt.output.openvr_overlay.render_panel',
                   return_value=Image.new('RGBA', (4, 4))) as render:
            with scope(new):
                out.update('new')
            old.valid = False
            render.reset_mock()
            out._overlay.reset_mock()
            self.repaint(out)
        self.assertEqual(render.call_args.args[0], 'new')
        out._overlay.setOverlayRaw.assert_called_once()

    def test_hard_reinit_does_not_relabel_revoked_content(self):
        out, approval = self.wrist(), Approval()
        with patch('vlt.output.openvr_overlay.render_conversation',
                   return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update_entries([('mine', '', 'text', '')])
        approval.valid = False
        out._overlay.reset_mock()
        vr = SimpleNamespace(shutdown=Mock(), init=Mock(), VRApplication_Background=3,
                             IVROverlay=Mock(return_value=out._overlay))
        with patch.dict(sys.modules, {'openvr': vr}), patch.object(out, '_resolve_anchor',
                return_value=0), patch.object(out, '_apply_transform'):
            self.assertTrue(out._hard_reinit())
        out._overlay.setOverlayRaw.assert_not_called()

    def test_hot_reload_does_not_relabel_revoked_content(self):
        out, approval = self.wrist(), Approval()
        out.HEARTBEAT_S = float('inf')
        with patch('vlt.output.openvr_overlay.render_conversation',
                   return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update_entries([('mine', '', 'text', '')])
        approval.valid = False
        out._overlay.reset_mock()
        with tempfile.TemporaryDirectory() as tmp:
            out.config_path = Path(tmp) / 'config.yaml'
            out.config_path.write_text('overlay:\n  font_size: 100\n', encoding='utf-8')
            out.tick()
        out._overlay.setOverlayRaw.assert_not_called()

    def test_close_clears_cached_approval_before_rebuild(self):
        out, approval = self.wrist(), Approval()
        with patch('vlt.output.openvr_overlay.render_conversation',
                   return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update_entries([('mine', '', 'text', '')])
        out.close()
        out._overlay.reset_mock()
        self.repaint(out)
        out._overlay.setOverlayRaw.assert_not_called()

    def test_heartbeat_does_not_reshow_revoked_native_texture(self):
        out, approval = self.wrist(), Approval()
        with patch('vlt.output.openvr_overlay.render_panel',
                   return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update('text')
        approval.valid = False
        out._overlay.reset_mock()
        out._overlay.isOverlayVisible.return_value = False
        out._log_heartbeat()
        out._overlay.showOverlay.assert_not_called()

    def test_revoked_native_texture_hidden_even_with_fade_disabled(self):
        out, approval = self.wrist(), Approval()
        out.cfg.fade_after_s, out.HEARTBEAT_S = 0, float('inf')
        with patch('vlt.output.openvr_overlay.render_panel',
                   return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update('text')
        approval.valid = False
        out._overlay.reset_mock()
        out.tick()
        out._overlay.setOverlayAlpha.assert_called_once_with(out._handle, 0.0)

    def test_fresh_frame_recovers_visibility_after_revocation_with_fade_disabled(self):
        out, old, new = self.wrist(), Approval(), Approval()
        out.cfg.fade_after_s, out.HEARTBEAT_S = 0, float('inf')
        with patch('vlt.output.openvr_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))):
            with scope(old):
                out.update('old')
            old.valid = False
            out.tick()
            out._overlay.setOverlayAlpha.assert_called_with(out._handle, 0.0)
            out._overlay.reset_mock()
            with scope(new):
                out.update('fresh')
            out.tick()
        out._overlay.setOverlayRaw.assert_called_once()
        out._overlay.setOverlayAlpha.assert_called_with(out._handle, out.cfg.alpha)

    def test_openvr_rechecks_revocation_after_render_before_upload(self):
        out = WristOverlay(OverlayConfig())
        out.available = True
        out._overlay = Mock()
        out._handle = 1
        approval = Approval()
        def render(*args):
            approval.valid = False
            return Image.new('RGBA', (4, 4))
        with patch('vlt.output.openvr_overlay.render_conversation', side_effect=render), scope(approval):
            out.update_entries([('mine', 'source', 'translation', '')])
        out._overlay.setOverlayRaw.assert_not_called()


    def test_openvr_rebuild_does_not_relabel_revoked_cached_entries(self):
        out = WristOverlay(OverlayConfig())
        out.available = True
        out._overlay = Mock()
        out._handle = 1
        approval = Approval()
        with patch('vlt.output.openvr_overlay.render_conversation',
                   return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update_entries([('mine', 'source', 'translation', '')])
        self.assertEqual(out._overlay.setOverlayRaw.call_count, 1)
        out._overlay.reset_mock()
        approval.valid = False
        with patch.object(out, '_resolve_anchor', return_value=0), patch.object(out, '_apply_transform'):
            self.assertTrue(out._recreate_overlay())
        out._overlay.setOverlayRaw.assert_not_called()


    def test_openvr_independent_repeated_segment_uploads_again(self):
        out = WristOverlay(OverlayConfig())
        out.available, out._overlay, out._handle = True, Mock(), 1
        old, new = Approval(), Approval()
        with patch('vlt.output.openvr_overlay.render_conversation',
                   return_value=Image.new('RGBA', (4, 4))):
            with scope(old):
                out.update_entries([('mine', 'source', 'translation', '')])
            out._overlay.reset_mock()
            old.valid = False
            with scope(new):
                out.update_entries([('mine', 'source', 'translation', '')])
        out._overlay.setOverlayRaw.assert_called_once()

    def test_hot_reload_never_reveals_previously_hidden_revoked_texture(self):
        out, approval = self.wrist(), Approval()
        out.HEARTBEAT_S = float('inf')
        with patch('vlt.output.openvr_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update('text')
        approval.valid = False
        out.tick()
        out._overlay.setOverlayAlpha.assert_called_with(out._handle, 0.0)
        out._overlay.reset_mock()
        with tempfile.TemporaryDirectory() as tmp:
            out.config_path = Path(tmp) / 'config.yaml'
            out.config_path.write_text('overlay:\n  anchor: left_hand\n', encoding='utf-8')
            with patch.object(out, '_resolve_anchor', return_value=0), patch(
                    'vlt.output.openvr_overlay.build_matrix', return_value=object()):
                out.tick()
        self.assertTrue(out._overlay.setOverlayAlpha.called)
        self.assertTrue(all(call.args[1] == 0.0 for call in out._overlay.setOverlayAlpha.call_args_list))

    def test_fresh_alpha_failure_is_retried_with_fade_disabled(self):
        out, old, fresh = self.wrist(), Approval(), Approval()
        out.cfg.fade_after_s, out.HEARTBEAT_S = 0, float('inf')
        with patch('vlt.output.openvr_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))):
            with scope(old):
                out.update('old')
            old.valid = False
            out.tick()
            out._overlay.reset_mock()
            out._overlay.setOverlayAlpha.side_effect = [RuntimeError('temporary'), None]
            with scope(fresh):
                out.update('fresh')
            out.tick()
        self.assertEqual(out._overlay.setOverlayAlpha.call_count, 2)
        out._overlay.setOverlayAlpha.assert_called_with(out._handle, out.cfg.alpha)

    def test_same_gui_rows_keep_dedupe_across_combined_wrappers(self):
        from vlt.gui_sinks import push_overlay
        out, approval = self.wrist(), Approval()
        row = SimpleNamespace(who='mine', source='source', text='translation',
                              label='', approval=approval)
        ctx = SimpleNamespace(bubbles=[row], overlay_out=out)
        with patch('vlt.output.openvr_overlay.render_conversation', return_value=Image.new('RGBA', (4, 4))):
            push_overlay(ctx)
            push_overlay(ctx)
        out._overlay.setOverlayRaw.assert_called_once()

    def test_combined_new_segment_same_text_is_not_deduplicated(self):
        out = self.wrist()
        with patch('vlt.output.openvr_overlay.render_conversation', return_value=Image.new('RGBA', (4, 4))):
            for approval in (Approval(), Approval()):
                with scope(combined(((approval, 'mine'),))):
                    out.update_entries([('mine', 'source', 'translation', '')])
        self.assertEqual(out._overlay.setOverlayRaw.call_count, 2)

    def test_render_uses_the_captured_rows_even_if_callers_list_changes(self):
        from vlt.output.overlay import _unpack_entry
        out, approval = self.wrist(), Approval()
        original = [('mine', 'source', 'approved', '')]
        def unpack(item):
            result = _unpack_entry(item)
            original[0] = ('mine', 'other', 'unapproved', '')
            return result
        with patch('vlt.output.openvr_overlay._unpack_entry', side_effect=unpack), patch(
                'vlt.output.openvr_overlay.render_conversation', return_value=Image.new('RGBA', (4, 4))) as render, scope(approval):
            out.update_entries(original)
        self.assertEqual(tuple(render.call_args.args[0]), (('mine', 'source', 'approved', ''),))

    def test_close_and_restart_during_render_cannot_upload_to_new_handle(self):
        out, approval, replacement = self.wrist(), Approval(), Mock()
        def render(*args):
            out.close()
            out.available, out._overlay, out._handle = True, replacement, 2
            return Image.new('RGBA', (4, 4))
        with patch('vlt.output.openvr_overlay.render_panel', side_effect=render), scope(approval):
            out.update('old')
        replacement.setOverlayRaw.assert_not_called()

    def test_close_and_restart_during_pixel_conversion_cannot_upload(self):
        out, approval, replacement = self.wrist(), Approval(), Mock()
        image = Mock(mode='RGBA', size=(4, 4))
        def pixels():
            out.close()
            out.available, out._overlay, out._handle = True, replacement, 2
            return bytes(64)
        image.tobytes.side_effect = pixels
        with scope(approval):
            out._upload(image)
        replacement.setOverlayRaw.assert_not_called()

    def test_close_during_row_capture_cannot_revive_old_content_on_repaint(self):
        from vlt.output.overlay import _unpack_entry
        out, approval, replacement = self.wrist(), Approval(), Mock()
        def unpack(item):
            out.close()
            out.available, out._overlay, out._handle = True, replacement, 2
            return _unpack_entry(item)
        with patch('vlt.output.openvr_overlay._unpack_entry', side_effect=unpack), patch(
                'vlt.output.openvr_overlay.render_conversation', return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update_entries([('mine', 'source', 'old', '')])
        with patch('vlt.output.openvr_overlay.render_conversation', return_value=Image.new('RGBA', (4, 4))):
            out._repaint()
        replacement.setOverlayRaw.assert_not_called()

    def test_same_gui_frame_retries_failed_raw_upload_then_deduplicates_success(self):
        from vlt.gui_sinks import push_overlay
        out, approval = self.wrist(), Approval()
        row = SimpleNamespace(who='mine', source='source', text='translation', label='', approval=approval)
        ctx = SimpleNamespace(bubbles=[row], overlay_out=out)
        out._overlay.setOverlayRaw.side_effect = [RuntimeError('transient'), None]
        with patch('vlt.output.openvr_overlay.render_conversation', return_value=Image.new('RGBA', (4, 4))):
            for _ in range(3):
                push_overlay(ctx)
        self.assertEqual(out._overlay.setOverlayRaw.call_count, 2)
        self.assertEqual(out.frames_updated, 1)
        self.assertEqual(out._upload_fails, 0)

    def test_same_text_retries_failed_raw_upload_then_deduplicates_success(self):
        out, approval = self.wrist(), Approval()
        out._overlay.setOverlayRaw.side_effect = [RuntimeError('transient'), None]
        with patch('vlt.output.openvr_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))), scope(approval):
            for _ in range(3):
                out.update('translation')
        self.assertEqual(out._overlay.setOverlayRaw.call_count, 2)
        self.assertEqual(out.frames_updated, 1)

    def test_failed_raw_frame_cannot_retry_after_revocation(self):
        out, approval = self.wrist(), Approval()
        out._overlay.setOverlayRaw.side_effect = [RuntimeError('transient'), None]
        with patch('vlt.output.openvr_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))), scope(approval):
            out.update('translation')
            approval.valid = False
            out.update('translation')
            out._repaint()
        self.assertEqual(out._overlay.setOverlayRaw.call_count, 1)
        self.assertEqual(out.frames_updated, 0)

    def test_old_upload_error_after_close_does_not_recover_the_new_window(self):
        out, approval, replacement = self.wrist(), Approval(), Mock()
        def late_error(*args):
            out.close()
            out.available, out._overlay, out._handle = True, replacement, 2
            raise RuntimeError('old window failed')
        out._overlay.setOverlayRaw.side_effect = late_error
        with patch('vlt.output.openvr_overlay.render_panel', return_value=Image.new('RGBA', (4, 4))), patch.object(
                out, '_escalate') as recover, scope(approval):
            out.update('old')
        self.assertEqual(out._upload_fails, 0)
        recover.assert_not_called()



if __name__ == '__main__':
    unittest.main()
