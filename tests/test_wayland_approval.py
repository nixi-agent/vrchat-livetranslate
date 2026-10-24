"""Wayland cached frames and actual marshal calls without a compositor."""
import io
import sys
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.platform import wayland as W
from vlt.session.chatgpt_output import allowed, scope


class Approval:
    valid = True

    def __call__(self, direction=None):
        return self.valid


class WaylandApprovalTests(unittest.TestCase):
    def window(self, approval, real_requests=False):
        out = W.LayerShellWindow.__new__(W.LayerShellWindow)
        out._dead, out._panel, out._alpha, out._dirty = False, None, 1.0, False
        out._configured, out._surface = True, 1
        out._surface_approval = None
        out._buffers = [{'busy': False, 'proxy': 2}, {'busy': False, 'proxy': 3}]
        out._next_buf, out._buf_dims, out._stride = 0, (4, 4), 16
        out._shm_map, out._flush = io.BytesIO(), Mock()
        out._output = None
        out._lib = SimpleNamespace(wl_proxy_get_version=Mock(return_value=4),
                                   wl_proxy_marshal_array_flags=Mock(return_value=None))
        out._req = MethodType(W.LayerShellWindow._req, out) if real_requests else Mock()
        with patch.object(out, '_draw'), scope(approval):
            out.set_panel(Image.new('RGBA', (4, 4), 'red'))
        return out

    def test_invalid_cache_does_not_attach_or_commit(self):
        approval = Approval()
        out = self.window(approval)
        approval.valid = False
        out._draw()
        out._req.assert_not_called()

    def test_valid_and_legacy_frames_commit_once(self):
        for approval in (Approval(), None):
            with self.subTest(approval=approval):
                out = self.window(approval)
                out._draw()
                self.assertEqual(sum(c.args[1] == W._WL_SURFACE_COMMIT
                                     for c in out._req.call_args_list), 1)

    def test_pixel_conversion_revocation(self):
        approval = Approval()
        out = self.window(approval)
        def pixels(*args):
            approval.valid = False
            return bytes(64)
        with patch('vlt.platform.wayland.premultiplied_bgra', side_effect=pixels):
            out._draw()
        out._req.assert_not_called()

    def test_shared_memory_write_revocation(self):
        approval = Approval()
        out = self.window(approval)
        class Buffer(io.BytesIO):
            def write(self, data):
                size = super().write(data)
                approval.valid = False
                return size
        out._shm_map = Buffer()
        out._draw()
        out._req.assert_not_called()

    def test_version_lookup_revocation(self):
        approval = Approval()
        out = self.window(approval)
        def version(*args):
            approval.valid = False
            return 4
        out._lib.wl_proxy_get_version.side_effect = version
        out._draw()
        out._req.assert_not_called()

    def test_bad_incoming_panel_preserves_good_cache(self):
        out, bad = self.window(Approval()), Approval()
        original = out._panel
        bad.valid = False
        with scope(bad), patch.object(out, '_draw'):
            out.set_panel(Image.new('RGBA', (4, 4), 'blue'))
        self.assertIs(out._panel, original)

    def test_alpha_update_does_not_relabel_cached_frame(self):
        approval = Approval()
        out = self.window(approval)
        approval.valid = False
        with scope(Approval()):
            out.set_alpha(0.8)
        out._req.assert_not_called()

    def test_hide_revoked_frame_even_when_both_buffers_busy(self):
        approval = Approval()
        out = self.window(approval)
        out._buffers[0]['busy'] = out._buffers[1]['busy'] = True
        approval.valid = False
        out.set_alpha(0)
        self.assertGreaterEqual(out._req.call_count, 2)
        self.assertEqual(out._req.call_args_list[0].args,
                         (1, W._WL_SURFACE_ATTACH, [('o', None)]))
        self.assertEqual(out._req.call_args_list[-1].args, (1, W._WL_SURFACE_COMMIT, []))

    def test_close_clears_cache_and_rejects_late_input(self):
        out = self.window(Approval())
        out._display = None
        with patch.object(out, '_teardown'):
            out.close()
        self.assertIsNone(out._panel)
        self.assertFalse(out._dirty)
        with scope(Approval()), patch.object(out, '_draw') as draw:
            out.set_panel(Image.new('RGBA', (4, 4)))
        draw.assert_not_called()

    def test_caller_cannot_mutate_cached_pixels(self):
        out = self.window(Approval())
        image = Image.new('RGBA', (4, 4), 'red')
        with scope(Approval()), patch.object(out, '_draw'):
            out.set_panel(image)
        image.paste('blue', (0, 0, 4, 4))
        self.assertEqual(out._panel.getpixel((0, 0)), (255, 0, 0, 255))

    def test_revocation_after_attach_cannot_commit_candidate(self):
        approval = Approval()
        out = self.window(approval, real_requests=True)
        events, attached = [], [None]
        def marshal(proxy, opcode, interface, version, flags, args):
            if opcode == W._WL_SURFACE_ATTACH:
                attached[0] = args[0].o
                if attached[0]:
                    approval.valid = False
            if opcode == W._WL_SURFACE_COMMIT:
                events.append(attached[0])
        out._lib.wl_proxy_marshal_array_flags.side_effect = marshal
        out._draw()
        self.assertNotIn(2, events)
        with scope(Approval()):
            out._req(1, W._WL_SURFACE_COMMIT, [])
        self.assertEqual(events[-1], None)

    def test_marshalling_rechecks_immediately_before_native_commit(self):
        approval = Approval()
        out = self.window(approval, real_requests=True)
        out._surface_approval = approval
        class Opcode:
            def __int__(self):
                approval.valid = False
                return W._WL_SURFACE_COMMIT
            def __eq__(self, other):
                return other == W._WL_SURFACE_COMMIT
        out._req(1, Opcode(), [])
        out._lib.wl_proxy_marshal_array_flags.assert_not_called()

    def test_protocol_facade_reads_live_interface_globals(self):
        from vlt.platform import wayland_protocol as P
        with patch.object(P, '_core_iface', return_value=123):
            W._build_layer_shell(Mock())
            W._build_relative_pointer(Mock())
            W._build_xdg_output(Mock())
        self.assertIs(W.iface_layer_shell, P.iface_layer_shell)
        self.assertEqual(W.read_interface(W.iface_layer_shell)['methods'][0],
                         ('get_layer_surface', 'no?ous'))
        self.assertEqual(W.read_interface(W.iface_rel_pointer)['events'],
                         [('relative_motion', 'uuffff')])
        self.assertEqual(W.read_interface(W.iface_xdg_manager)['methods'][1],
                         ('get_xdg_output', 'no'))

    def test_rejected_commit_does_not_exhaust_buffers_for_next_valid_segment(self):
        out = self.window(Approval(), real_requests=True)
        original = out._req
        def commit(proxy, opcode, args, *extra, **kwargs):
            if opcode == W._WL_SURFACE_COMMIT:
                out._panel_approval.valid = False
            return original(proxy, opcode, args, *extra, **kwargs)
        out._req = commit
        out._draw()
        self.assertFalse(any(b['busy'] for b in out._buffers))
        out._req = original
        with scope(Approval()):
            out.set_panel(Image.new('RGBA', (4, 4), 'blue'))
        self.assertTrue(any(b['busy'] for b in out._buffers))

    def test_close_during_buffer_write_does_not_use_destroyed_state(self):
        out = self.window(Approval())
        class Buffer(io.BytesIO):
            def write(self, data):
                size = super().write(data)
                out._dead, out._buffers, out._surface = True, [], None
                return size
        out._shm_map = Buffer()
        failure = None
        try:
            out._draw()
        except Exception as exc:
            failure = exc
        self.assertIsNone(failure)
        out._req.assert_not_called()

    def test_commit_rejected_during_marshalling_does_not_reserve_buffer(self):
        out = self.window(Approval(), real_requests=True)
        original = out._req
        class Opcode:
            def __int__(self):
                out._panel_approval.valid = False
                return W._WL_SURFACE_COMMIT
            def __eq__(self, other):
                return other == W._WL_SURFACE_COMMIT
        def request(proxy, opcode, args, *extra, **kwargs):
            return original(proxy, Opcode() if opcode == W._WL_SURFACE_COMMIT else opcode,
                            args, *extra, **kwargs)
        out._req = request
        out._draw()
        self.assertFalse(any(b['busy'] for b in out._buffers))
        out._req = original
        with scope(Approval()):
            out.set_panel(Image.new('RGBA', (4, 4), 'blue'))
        self.assertTrue(any(b['busy'] for b in out._buffers))

    def test_buffer_committed_before_revocation_stays_owned_by_compositor(self):
        approval = Approval()
        out = self.window(approval, real_requests=True)
        def marshal(proxy, opcode, interface, version, flags, args):
            if opcode == W._WL_SURFACE_COMMIT:
                approval.valid = False
        out._lib.wl_proxy_marshal_array_flags.side_effect = marshal
        out._draw()
        self.assertTrue(out._buffers[0]['busy'])
        out._on_buffer_release(0)
        self.assertFalse(out._buffers[0]['busy'])

    def test_failed_detach_preserves_invalid_attachment_identity(self):
        approval = Approval()
        out = self.window(approval, real_requests=True)
        out._surface_approval = approval
        approval.valid = False
        out._lib.wl_proxy_marshal_array_flags.side_effect = RuntimeError('detachment failed')
        with self.assertRaises(RuntimeError):
            out._req(1, W._WL_SURFACE_COMMIT, [])
        self.assertIs(out._surface_approval, approval)

    def test_failed_new_attachment_cannot_authorize_an_old_pending_frame(self):
        out = self.window(Approval(), real_requests=True)
        out._lib.wl_proxy_marshal_array_flags.side_effect = RuntimeError('attachment unknown')
        with self.assertRaises(RuntimeError):
            out._draw()
        self.assertFalse(allowed(out._surface_approval))
        attached, commits = [2], []
        def marshal(proxy, opcode, interface, version, flags, args):
            if opcode == W._WL_SURFACE_ATTACH:
                attached[0] = args[0].o
            if opcode == W._WL_SURFACE_COMMIT:
                commits.append(attached[0])
        out._lib.wl_proxy_marshal_array_flags.side_effect = marshal
        out._req(1, W._WL_SURFACE_COMMIT, [])
        self.assertEqual(commits, [None])


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
