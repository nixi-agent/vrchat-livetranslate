"""Offline OpenXR queue and native submission approvals; no runtime or device."""
import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_output import scope
from vlt.output.overlay import OverlayConfig
from vlt.output.openxr_overlay import OpenXrOverlay, XrOverlaySession, _GlBackend


class Approval:
    def __init__(self):
        self.valid = True
    def __call__(self, direction=None):
        return self.valid


class QueueTests(unittest.TestCase):
    def xr(self, approval, *, shown=False):
        out = OpenXrOverlay(OverlayConfig())
        img = Image.new('RGBA', (4, 4))
        with patch('vlt.output.openxr_overlay.render_conversation', return_value=img), scope(approval):
            out.update_entries([('mine', 'source', 'translation', '')])
        if shown:
            out._shown_img, out._pending_img = out._pending_img, None
        submitted = []
        def submit(image, *args):
            submitted.append(image)
            out._frame_stop.set()
        out._sess = SimpleNamespace(state=2, pump_events=Mock(), submit=submit, clear=Mock())
        return out, submitted


    def drive(self, out):
        fake_xr = SimpleNamespace(SessionState=SimpleNamespace(READY=2, FOCUSED=5, STOPPING=6))
        with patch.dict(sys.modules, {'xr': fake_xr}), patch(
                'vlt.output.openxr_overlay.time.sleep', side_effect=lambda _: out._frame_stop.set()):
            out._frame_loop()


    def test_xr_revoked_pending_frame_never_reaches_submit(self):
        approval = Approval()
        out, submitted = self.xr(approval)
        approval.valid = False
        self.drive(out)
        self.assertEqual(submitted, [])
        out._sess.clear.assert_called_once()


    def test_xr_revoked_shown_cache_is_not_resubmitted(self):
        approval = Approval()
        out, submitted = self.xr(approval, shown=True)
        approval.valid = False
        self.drive(out)
        self.assertEqual(submitted, [])
        out._sess.clear.assert_called_once()


    def test_xr_approved_pending_frame_reaches_submit(self):
        out, submitted = self.xr(Approval())
        self.drive(out)
        self.assertEqual(len(submitted), 1)
        out._sess.clear.assert_not_called()

    def test_stopping_session_does_not_submit_or_wait_for_clearing(self):
        out, submitted = self.xr(Approval())
        out._sess.state = 6
        self.drive(out)
        self.assertEqual(submitted, [])
        out._sess.clear.assert_not_called()

    def test_clear_failure_uses_existing_recovery_without_claiming_delivery(self):
        approval = Approval()
        out, submitted = self.xr(approval)
        approval.valid = False
        out._sess.clear.side_effect = RuntimeError('clear failure')
        out._escalate = Mock()
        self.drive(out)
        out._escalate.assert_called_once()
        self.assertEqual(out._fails, 1)
        self.assertEqual(out.frames_updated, 0)
        self.assertEqual(submitted, [])


class ClearTests(unittest.TestCase):
    def test_clear_balances_empty_frame_without_pixels_or_swapchain_acquisition(self):
        gl = SimpleNamespace(upload=Mock())
        sess = XrOverlaySession(gl, (4, 4))
        sess.session = 1
        sess.pump_events = Mock()
        clear = getattr(sess, 'clear', None)
        self.assertTrue(callable(clear), 'An empty XR frame must be explicitly submitted')
        xr = SimpleNamespace(wait_frame=Mock(return_value=SimpleNamespace(predicted_display_time=1)),
            begin_frame=Mock(), end_frame=Mock(), acquire_swapchain_image=Mock(),
            FrameEndInfo=lambda **kwargs: SimpleNamespace(**kwargs),
            EnvironmentBlendMode=SimpleNamespace(OPAQUE=1))
        with patch.dict(sys.modules, {'xr': xr}):
            self.assertTrue(clear())
        xr.begin_frame.assert_called_once_with(1)
        xr.end_frame.assert_called_once()
        self.assertEqual(xr.end_frame.call_args.args[1].layer_count, 0)
        xr.acquire_swapchain_image.assert_not_called()
        gl.upload.assert_not_called()


class SubmitTests(unittest.TestCase):
    def submit(self, revoke=None, configure=None):
        approval = Approval()
        gl = NS(upload=Mock())
        sess = XrOverlaySession(gl, (4, 4))
        sess.session, sess.swapchain, sess.textures = 1, 2, [3]
        sess.pump_events, sess.anchor_space = Mock(), Mock(return_value=4)
        def construct(*args, **kwargs):
            return NS(**kwargs)
        xr = NS(wait_frame=Mock(return_value=NS(predicted_display_time=1)),
                begin_frame=Mock(), acquire_swapchain_image=Mock(return_value=0),
                wait_swapchain_image=Mock(), release_swapchain_image=Mock(), end_frame=Mock(),
                SwapchainImageWaitInfo=construct, SwapchainSubImage=construct,
                Rect2Di=construct, Offset2Di=construct, Extent2Di=construct,
                Extent2Df=construct, Posef=construct, Quaternionf=construct, Vector3f=construct,
                CompositionLayerQuad=lambda **kwargs: ctypes.c_uint(0),
                CompositionLayerBaseHeader=ctypes.c_uint, FrameEndInfo=construct,
                EyeVisibility=NS(BOTH=2), EnvironmentBlendMode=NS(OPAQUE=1))
        if revoke == 'wait':
            def wait(*args):
                approval.valid = False
                return NS(predicted_display_time=1)
            xr.wait_frame.side_effect = wait
        elif revoke == 'prepare':
            def prepare(*args):
                approval.valid = False
                return bytes(64)
            sess._prepared = prepare
        elif revoke == 'release':
            xr.release_swapchain_image.side_effect = lambda *args: setattr(approval, 'valid', False)
        if configure:
            configure(approval, gl, xr, sess)
        with patch.dict(sys.modules, {'xr': xr}), patch(
                'vlt.output.openxr_session.layer_alpha_flags', return_value=3), scope(approval):
            sess.submit(Image.new('RGBA', (4, 4)), OverlayConfig(curvature=0))
        return gl, xr

    def test_normal_frame_uploads_and_submits_one_layer(self):
        gl, xr = self.submit()
        gl.upload.assert_called_once()
        xr.release_swapchain_image.assert_called_once()
        self.assertEqual(xr.end_frame.call_args.args[1].layer_count, 1)

    def test_revoked_during_wait_never_uploaded(self):
        gl, xr = self.submit('wait')
        gl.upload.assert_not_called()
        self.assertEqual(xr.end_frame.call_args.args[1].layer_count, 0)

    def test_revoked_during_prepare_never_uploaded(self):
        gl, xr = self.submit('prepare')
        gl.upload.assert_not_called()
        xr.release_swapchain_image.assert_called_once()
        self.assertEqual(xr.end_frame.call_args.args[1].layer_count, 0)

    def test_revoked_after_upload_never_submitted_as_visible_layer(self):
        gl, xr = self.submit('release')
        gl.upload.assert_called_once()
        self.assertEqual(xr.end_frame.call_args.args[1].layer_count, 0)

    def test_revocation_while_building_end_info_submits_zero_layers(self):
        def configure(approval, gl, xr, sess):
            def info(**kwargs):
                if kwargs['layer_count']:
                    approval.valid = False
                return NS(**kwargs)
            xr.FrameEndInfo = info
        gl, xr = self.submit(configure=configure)
        self.assertEqual(xr.end_frame.call_args.args[1].layer_count, 0)
        xr.release_swapchain_image.assert_called_once()

    def test_upload_error_releases_and_balances_frame_once(self):
        captured = []
        def configure(approval, gl, xr, sess):
            gl.upload.side_effect = RuntimeError('upload failure')
            captured.append(xr)
        with self.assertRaises(RuntimeError):
            self.submit(configure=configure)
        xr = captured[0]
        xr.release_swapchain_image.assert_called_once()
        xr.end_frame.assert_called_once()
        self.assertEqual(xr.end_frame.call_args.args[1].layer_count, 0)

    def test_unknown_release_failure_is_not_retried(self):
        captured = []
        def configure(approval, gl, xr, sess):
            xr.release_swapchain_image.side_effect = RuntimeError('release failure')
            captured.append(xr)
        with self.assertRaises(RuntimeError):
            self.submit(configure=configure)
        captured[0].release_swapchain_image.assert_called_once()
        self.assertEqual(captured[0].end_frame.call_args.args[1].layer_count, 0)



class LifecycleTests(unittest.TestCase):
    def out(self):
        return OpenXrOverlay(OverlayConfig(enabled=True))

    def push(self, out, approval, text='translation', render=None):
        with scope(approval), patch('vlt.output.openxr_overlay.render_panel',
                side_effect=render, return_value=Image.new('RGBA', (4, 4))):
            out.update(text, 'source')

    def test_invalid_content_is_not_rendered_or_cached(self):
        out, approval = self.out(), Approval()
        approval.valid = False
        with scope(approval), patch('vlt.output.openxr_overlay.render_panel') as render:
            out.update('rejected', 'source')
        render.assert_not_called()
        self.assertIsNone(out._last_render_cached)

    def test_new_segment_same_words_is_not_deduplicated(self):
        out = self.out()
        self.push(out, Approval())
        first = out._pending_img
        self.push(out, Approval())
        self.assertIsNot(out._pending_img, first)

    def test_failed_render_allows_next_same_legal_push(self):
        out, approval = self.out(), Approval()
        def fail(*args):
            raise RuntimeError('render failure')
        try:
            self.push(out, approval, render=fail)
        except RuntimeError:
            pass
        self.push(out, approval)
        self.assertIsNotNone(out._pending_img)

    def test_close_during_render_does_not_restore_queue_or_cache(self):
        out = self.out()
        def render(*args):
            out.close()
            return Image.new('RGBA', (4, 4))
        self.push(out, Approval(), render=render)
        self.assertIsNone(out._pending_img)
        self.assertIsNone(out._last_render_cached)

    def test_close_clears_old_content_and_frame_caches(self):
        out = self.out()
        self.push(out, Approval())
        out._shown_img = out._pending_img
        out.close()
        self.assertIsNone(out._pending_img)
        self.assertIsNone(out._shown_img)
        self.assertIsNone(out._last_render_cached)
        self.assertIsNone(out._last_entries_cached)

    def test_blocked_old_worker_is_retained_and_cannot_restart(self):
        out, worker = self.out(), Mock()
        worker.is_alive.return_value = True
        out._frame_thread = worker
        out.close()
        self.assertIs(out._frame_thread, worker)
        with patch('vlt.output.openxr_overlay._test_process_guard', return_value=None), patch(
                'vlt.output.openxr_overlay.threading.Thread') as factory:
            self.assertFalse(out.start())
        factory.assert_not_called()
        self.assertTrue(out._frame_stop.is_set())

    def test_rebuild_preserves_cached_owner_under_new_scope(self):
        out, old, fresh = self.out(), Approval(), Approval()
        self.push(out, old)
        old.valid = False
        out._gl = SimpleNamespace(REQUIRED_EXTENSIONS=())
        with scope(fresh), patch('vlt.output.openxr_overlay.XrOverlaySession'), patch.object(
                out, '_extensions', return_value=[]), patch(
                'vlt.output.openxr_overlay.render_panel') as render:
            out._bring_up(rebuild_gl=False)
        render.assert_not_called()

    def test_close_during_submit_does_not_restore_counter(self):
        driver = QueueTests()
        out, submitted = driver.xr(Approval())
        out._sess.submit = lambda *args: out.close()
        driver.drive(out)
        self.assertEqual(out.frames_updated, 0)

    def test_initialization_timeout_clears_old_content_and_retains_worker(self):
        out, worker, done = self.out(), Mock(), Mock()
        self.push(out, Approval())
        worker.is_alive.return_value = True
        done.wait.return_value = False
        with patch('vlt.output.openxr_overlay._test_process_guard', return_value=None), patch(
                'vlt.output.openxr_overlay.threading.Thread', return_value=worker), patch(
                'vlt.output.openxr_overlay.threading.Event', return_value=done):
            self.assertFalse(out.start())
        self.assertIsNone(out._pending_img)
        self.assertIsNone(out._last_render_cached)
        self.assertIs(out._frame_thread, worker)
        self.assertTrue(out._frame_stop.is_set())

    def test_successful_same_original_segment_remains_deduplicated(self):
        out, approval = self.out(), Approval()
        self.push(out, approval)
        first = out._pending_img
        self.push(out, approval)
        self.assertIs(out._pending_img, first)

    def test_old_render_cannot_overwrite_new_segment(self):
        out = self.out()
        def render(*args):
            self.push(out, Approval(), text='new')
            return Image.new('RGBA', (4, 4), 'blue')
        self.push(out, Approval(), text='old', render=render)
        self.assertEqual(out._last_render_cached, ('new', 'source'))
        self.assertNotEqual(out._pending_img[0].getpixel((0, 0)), (0, 0, 255, 255))

    def test_queued_pixels_are_immutable_snapshot(self):
        out, image = self.out(), Image.new('RGBA', (4, 4), 'red')
        with scope(Approval()):
            out._queue_frame(image)
        image.putpixel((0, 0), (0, 0, 255, 255))
        self.assertEqual(out._pending_img[0].getpixel((0, 0)), (255, 0, 0, 255))


class GlTests(unittest.TestCase):
    def gl(self):
        out = _GlBackend.__new__(_GlBackend)
        out.gl = Mock()
        out.gl.glGetError.return_value = 0
        return out

    def test_revoked_scope_never_uploads_gl_pixels(self):
        out, approval = self.gl(), Approval()
        approval.valid = False
        with scope(approval):
            out.upload(3, 4, 4, bytes(64))
        out.gl.glTexSubImage2D.assert_not_called()

    def test_buffer_conversion_revocation_prevents_gl_upload(self):
        out, approval = self.gl(), Approval()
        original = ctypes.create_string_buffer
        def buffer(*args):
            approval.valid = False
            return original(*args)
        with scope(approval), patch('ctypes.create_string_buffer', side_effect=buffer):
            out.upload(3, 4, 4, bytes(64))
        out.gl.glTexSubImage2D.assert_not_called()

    def test_bind_revocation_prevents_actual_gl_upload(self):
        out, approval = self.gl(), Approval()
        out.gl.glBindTexture.side_effect = lambda *args: setattr(approval, 'valid', False)
        with scope(approval):
            out.upload(3, 4, 4, bytes(64))
        out.gl.glTexSubImage2D.assert_not_called()

    def test_normal_and_legacy_gl_uploads_remain_valid(self):
        for approval in (Approval(), None):
            with self.subTest(approval=approval):
                out = self.gl()
                with scope(approval):
                    out.upload(3, 4, 4, bytes(64))
                out.gl.glTexSubImage2D.assert_called_once()
                out.gl.glFinish.assert_called_once()

    def test_gl_error_reaches_recovery_instead_of_silent_spin(self):
        out = self.gl()
        out.gl.glGetError.return_value = 0x502
        with self.assertRaises(RuntimeError):
            out.upload(3, 4, 4, bytes(64))
        out.gl.glFinish.assert_called_once()


if __name__ == '__main__':
    unittest.main()
