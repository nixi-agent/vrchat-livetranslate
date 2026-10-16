"""Native ownership under injected allocation/cleanup failures; no libraries or devices."""
import ctypes
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.output.openxr_gl import EglGlContext, _GlBackend
from vlt.platform import wayland as W, x11 as X
from vlt.platform.x11_bindings import _XImage


class EglOwnershipTests(unittest.TestCase):
    def libraries(self, failure=None):
        events = []
        wl, egl = Mock(), Mock()
        wl.wl_display_connect.return_value = 0x100000100
        egl.eglGetPlatformDisplay.return_value = 0x200000200
        egl.eglCreateContext.return_value = 0x300000300
        for name in ('eglInitialize', 'eglBindAPI', 'eglMakeCurrent'):
            getattr(egl, name).return_value = 1
        def choose(display, attrs, config, count, number):
            config._obj.value, number._obj.value = 0x400000400, 1
            return 1
        egl.eglChooseConfig.side_effect = choose
        egl.eglGetError.return_value = 0x3001
        if failure == 'choose':
            egl.eglChooseConfig.side_effect = lambda *a: 0
        elif failure:
            getattr(egl, failure).return_value = 0
        for obj, name, label in ((egl, 'eglDestroyContext', 'destroy'),
                                  (egl, 'eglTerminate', 'terminate'),
                                  (wl, 'wl_display_disconnect', 'disconnect')):
            getattr(obj, name).side_effect = lambda *a, label=label: events.append((label, a))
        def current(*args):
            if args[-1] is None:
                events.append(('unbind', args))
            return 0 if failure == 'eglMakeCurrent' else 1
        egl.eglMakeCurrent.side_effect = current
        return wl, egl, events

    def create(self, wl, egl):
        with patch.object(_GlBackend, '__init__', return_value=None), patch(
                'vlt.output.openxr_gl.ctypes.CDLL', side_effect=[wl, egl]):
            return EglGlContext()

    def test_normal_close_releases_every_resource_once_in_reverse_order(self):
        wl, egl, events = self.libraries()
        context = self.create(wl, egl)
        context.close()
        self.assertEqual([e[0] for e in events], ['unbind', 'destroy', 'terminate', 'disconnect'])
        egl.eglDestroyContext.assert_called_once_with(0x200000200, 0x300000300)
        egl.eglTerminate.assert_called_once_with(0x200000200)
        wl.wl_display_disconnect.assert_called_once_with(0x100000100)
        context.close()
        self.assertEqual(len(events), 4)

    def test_each_partial_initialization_failure_releases_acquired_resources(self):
        for fail, labels in (
            ('wl_display_connect', []), ('eglGetPlatformDisplay', ['disconnect']),
            ('eglInitialize', ['terminate', 'disconnect']),
            ('eglBindAPI', ['terminate', 'disconnect']), ('choose', ['terminate', 'disconnect']),
            ('eglCreateContext', ['terminate', 'disconnect']),
            ('eglMakeCurrent', ['unbind', 'destroy', 'terminate', 'disconnect'])):
            with self.subTest(failure=fail):
                wl, egl, events = self.libraries(fail if fail != 'wl_display_connect' else None)
                if fail == 'wl_display_connect':
                    wl.wl_display_connect.return_value = 0
                with self.assertRaises(RuntimeError):
                    self.create(wl, egl)
                self.assertEqual([e[0] for e in events], labels)

    def test_cleanup_error_does_not_skip_later_resources_or_retry_stale_handles(self):
        wl, egl, events = self.libraries()
        context = self.create(wl, egl)
        egl.eglMakeCurrent.side_effect = RuntimeError('unbind')
        egl.eglDestroyContext.side_effect = RuntimeError('destroy')
        context.close()
        egl.eglTerminate.assert_called_once()
        wl.wl_display_disconnect.assert_called_once()
        context.close()
        self.assertEqual(egl.eglDestroyContext.call_count, 1)
        self.assertEqual(wl.wl_display_disconnect.call_count, 1)

    def test_cleanup_abi_preserves_native_pointer_width(self):
        wl, egl, events = self.libraries()
        context = self.create(wl, egl)
        self.assertEqual(egl.eglDestroyContext.argtypes, [ctypes.c_void_p, ctypes.c_void_p])
        self.assertEqual(egl.eglTerminate.argtypes, [ctypes.c_void_p])
        self.assertIs(egl.eglDestroyContext.restype, ctypes.c_uint)
        self.assertIs(egl.eglTerminate.restype, ctypes.c_uint)
        context.close()


class ShmOwnershipTests(unittest.TestCase):
    def window(self, failure=None):
        out = W.LayerShellWindow.__new__(W.LayerShellWindow)
        out._shm, out._output, out._size = 7, None, (4, 4)
        out._buffers, out._buffer_listeners, out._buf_dims = [], [], (0, 0)
        out._shm_fd = out._shm_map = out._pool = None
        out._lib = NS(wl_proxy_destroy=Mock())
        counter = [0]
        def request(proxy, opcode, *a, **k):
            if proxy == 7:
                if failure == 'pool-error':
                    raise RuntimeError('pool request')
                return None if failure == 'pool-null' else 20
            if proxy == 20 and opcode == W.wl._WL_SHM_POOL_CREATE_BUFFER:
                counter[0] += 1
                if failure == f'buffer-{counter[0]}':
                    return None
                return 29 + counter[0]
        out._req = Mock(side_effect=request)
        out._attach_listener = Mock(side_effect=RuntimeError('listener') if failure == 'listener' else None)
        return out

    def test_all_partial_allocations_are_released_and_can_rebuild(self):
        for failure in ('truncate', 'map', 'pool-error', 'pool-null', 'buffer-1', 'buffer-2', 'listener'):
            with self.subTest(failure=failure):
                out = self.window(failure)
                mapping = io.BytesIO(bytes(128))
                with patch.object(W.os, 'memfd_create', return_value=10, create=True), patch.object(
                    W.os, 'ftruncate', side_effect=OSError('truncate') if failure == 'truncate' else None), patch.object(
                    W.mmap, 'mmap', side_effect=OSError('map') if failure == 'map' else None,
                    return_value=mapping), patch.object(W.os, 'close') as close, patch.object(
                    W.wl, '_core_iface', return_value=1):
                    with self.assertRaises((RuntimeError, OSError)):
                        out._rebuild_buffers()
                    close.assert_called_once_with(10)
                    if failure not in ('truncate', 'map'):
                        self.assertTrue(mapping.closed)
                    destroyed = [call.args[0].value for call in out._lib.wl_proxy_destroy.call_args_list]
                    expected = ([30, 20] if failure in ('buffer-2', 'listener') else
                                [20] if failure == 'buffer-1' else [])
                    self.assertEqual(destroyed, expected)
                    self.assertEqual(out._buffers, [])
                    self.assertEqual(out._buffer_listeners, [])
                    self.assertIsNone(out._shm_fd)
                    self.assertIsNone(out._shm_map)
                    self.assertIsNone(out._pool)
                fresh = self.window()
                out._req, out._attach_listener = fresh._req, fresh._attach_listener
                good = io.BytesIO(bytes(128))
                with patch.object(W.os, 'memfd_create', return_value=11, create=True), patch.object(
                    W.os, 'ftruncate'), patch.object(W.mmap, 'mmap', return_value=good), patch.object(
                    W.os, 'close') as close, patch.object(W.wl, '_core_iface', return_value=1):
                    out._rebuild_buffers()
                    self.assertEqual(len(out._buffers), 2)
                    out._release_buffers()
                    self.assertTrue(good.closed)
                    close.assert_called_once_with(11)

    def test_destroy_request_failure_still_releases_local_proxies_mapping_and_fd(self):
        out = self.window()
        mapping = io.BytesIO(bytes(128))
        with patch.object(W.os, 'memfd_create', return_value=10, create=True), patch.object(
            W.os, 'ftruncate'), patch.object(W.mmap, 'mmap', return_value=mapping), patch.object(
            W.os, 'close') as close, patch.object(W.wl, '_core_iface', return_value=1):
            out._rebuild_buffers()
            out._req.side_effect = RuntimeError('connection lost')
            out._release_buffers()
            self.assertEqual([c.args[0].value for c in out._lib.wl_proxy_destroy.call_args_list], [30, 31, 20])
            self.assertTrue(mapping.closed)
            close.assert_called_once_with(10)
            out._release_buffers()
            self.assertEqual(close.call_count, 1)

    def test_output_removal_destroys_native_addresses_not_registry_ids(self):
        out = W.LayerShellWindow.__new__(W.LayerShellWindow)
        addr, xdg = 0x100000100, 0x200000200
        out._outputs = {42:dict(addr=addr, xdg_addr=xdg)}
        out._output_proxies, out._listeners = {addr:42}, []
        out._lib = NS(wl_proxy_destroy=Mock())
        listener = out._registry_listener()
        callback = getattr(listener, listener._fields_[1][0])
        callback(None, None, 42)
        callback(None, None, 42)
        self.assertEqual([c.args[0].value for c in out._lib.wl_proxy_destroy.call_args_list], [xdg, addr])
        self.assertEqual(out._outputs, {})
        self.assertEqual(out._output_proxies, {})


class XImageOwnershipTests(unittest.TestCase):
    def test_unsupported_layout_frees_struct_and_next_valid_image_recovers(self):
        for order, bits in ((1, 32), (0, 16)):
            with self.subTest(byte_order=order, bits=bits):
                out = X.ArgbWindow.__new__(X.ArgbWindow)
                out._img = out._buf = None
                out._dpy, out._visual = 1, NS(visual=2)
                bad, good = _XImage(byte_order=order, bits_per_pixel=bits), _XImage(byte_order=0, bits_per_pixel=32)
                out._x11 = NS(XCreateImage=Mock(side_effect=[ctypes.pointer(bad), ctypes.pointer(good)]), XFree=Mock())
                with self.assertRaises(RuntimeError):
                    out._make_image(4, 4)
                out._x11.XFree.assert_called_once()
                self.assertIsNone(out._img)
                self.assertIsNone(out._buf)
                out._make_image(4, 4)
                self.assertIsNotNone(out._img)
                out._free_image()
                self.assertEqual(out._x11.XFree.call_count, 2)


if __name__ == '__main__':
    unittest.main()
