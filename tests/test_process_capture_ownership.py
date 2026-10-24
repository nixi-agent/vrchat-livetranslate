"""Offline COM ownership at each process-capture activation boundary."""
import ctypes
from contextlib import ExitStack
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.platform import win


FAILURE = -2147467259


class NativeBoundary:
    def __init__(self, failure=None, initialized=1):
        self.failure = failure
        self.created, self.closed, self.released = [], [], []
        self.initialized, self.uninitialized = [], []
        self.native_handler = None
        self.kernel = SimpleNamespace(CreateEventW=Mock(side_effect=self.create_event),
            CloseHandle=Mock(side_effect=lambda h:self.closed.append(h.value)),
            WaitForSingleObject=Mock(return_value=258 if failure == 'timeout' else 0),
            SetEvent=Mock(return_value=1))
        self.ole = SimpleNamespace(CoInitializeEx=Mock(side_effect=lambda *a:
            self.initialized.append(threading.get_ident()) or initialized),
            CoUninitialize=Mock(side_effect=lambda:self.uninitialized.append(threading.get_ident())),
            CoCreateFreeThreadedMarshaler=Mock(side_effect=self.marshal))
        self.mm = SimpleNamespace(ActivateAudioInterfaceAsync=Mock(side_effect=self.activate))

    def create_event(self, *args):
        value = 10 + len(self.created)
        if self.failure == ('handler-event' if not self.created else 'capture-event'):
            return None
        self.created.append(value)
        return value

    def marshal(self, outer, output):
        if self.failure == 'marshal':
            return FAILURE
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 100
        return 0

    def activate(self, path, iid, params, handler, output):
        if self.failure == 'activate':
            return FAILURE
        self.native_handler = handler.value
        win._h_addref(self.native_handler)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 200
        if self.failure != 'timeout':
            win._h_activate_completed(self.native_handler, 200)
        return 0

    def vcall(self, ptr, index, result, types, *args):
        value = ptr.value if hasattr(ptr, 'value') else ptr
        if index == 2:
            self.released.append(value)
            if value == 200 and self.failure != 'timeout':
                win._h_release(self.native_handler)
            return 0
        if value == 200 and index == 3:
            ctypes.cast(args[0], ctypes.POINTER(win._HRESULT)).contents.value = 0
            ctypes.cast(args[1], ctypes.POINTER(ctypes.c_void_p)).contents.value = 300
            return FAILURE if self.failure == 'result' else 0
        if value == 300 and index == 14:
            ctypes.cast(args[1], ctypes.POINTER(ctypes.c_void_p)).contents.value = 400
            return FAILURE if self.failure == 'service' else 0
        stages = {3:'initialize',13:'set-event',10:'start'}
        return FAILURE if value == 300 and stages.get(index) == self.failure else 0

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.object(ctypes, 'WinDLL', side_effect=lambda name,**k:
            self.ole if name.lower() == 'ole32' else self.mm if name.lower() == 'mmdevapi.dll' else self.kernel,
            create=True))
        stack.enter_context(patch.object(win, '_vcall', side_effect=self.vcall))
        stack.enter_context(patch.object(win, 'process_loopback_available', return_value=(True,'fake')))
        stack.enter_context(patch.object(win, 'pid_alive', return_value=True))
        stack.enter_context(patch.object(win.ProcessLoopbackSource, 'start', Mock()))
        return stack


class CaptureOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = set(win._HANDLER_BY_ADDR)

    def tearDown(self):
        for key in set(win._HANDLER_BY_ADDR) - self.original:
            win._HANDLER_BY_ADDR.pop(key, None)

    async def test_each_activation_failure_releases_only_acquired_resources(self):
        for stage in ('handler-event','marshal','activate','result','initialize',
                      'service','capture-event','set-event','start'):
            with self.subTest(stage=stage):
                fake = NativeBoundary(stage)
                source = None
                with fake.patches():
                    try:
                        with self.assertRaises(RuntimeError):
                            source = win.open_process_loopback(123)
                    finally:
                        if source is not None:
                            source.close()
                    self.assertCountEqual(fake.closed, fake.created)
                    self.assertEqual(fake.initialized, fake.uninitialized)
                    self.assertEqual(set(win._HANDLER_BY_ADDR), self.original)
                    self.assertEqual(len(fake.released), len(set(fake.released)), 'no double Release')
                    acquired = ([] if stage in ('handler-event', 'marshal') else
                        [100] if stage == 'activate' else
                        [100,200,300] if stage in ('result','initialize') else [100,200,300,400])
                    self.assertCountEqual(fake.released, acquired)

    async def test_success_transfers_resources_and_close_is_idempotent(self):
        for initialized in (0, 1):
            with self.subTest(initialized=initialized):
                fake = NativeBoundary(initialized=initialized)
                with fake.patches():
                    source = win.open_process_loopback(123)
                    self.assertEqual(fake.uninitialized, [])
                    source.close()
                    source.close()
                    self.assertCountEqual(fake.closed, fake.created)
                    self.assertCountEqual(fake.released, [100,200,300,400])
                    self.assertEqual(fake.initialized, fake.uninitialized)
                    self.assertEqual(set(win._HANDLER_BY_ADDR), self.original)

    async def test_failed_com_initialization_is_not_uninitialized(self):
        fake = NativeBoundary(initialized=FAILURE)
        with fake.patches():
            source = None
            try:
                with self.assertRaises(RuntimeError):
                    source = win.open_process_loopback(123)
            finally:
                if source is not None:
                    source.close()
            self.assertEqual(fake.uninitialized, [])
            self.assertEqual(fake.created, [])

    async def test_timed_out_handler_lives_until_native_callback_and_release(self):
        fake = NativeBoundary('timeout')
        with fake.patches():
            source = None
            try:
                with self.assertRaises(RuntimeError):
                    source = win.open_process_loopback(123)
            finally:
                if source is not None:
                    source.close()
            self.assertIn(fake.native_handler, win._HANDLER_BY_ADDR)
            self.assertEqual(fake.closed, [], 'the callback still owns its event and vtable')
            self.assertEqual(fake.released, [200])
            win._h_activate_completed(fake.native_handler, 200)
            self.assertTrue(fake.kernel.SetEvent.called)
            win._h_release(fake.native_handler)
            self.assertCountEqual(fake.closed, fake.created)
            self.assertCountEqual(fake.released, [100,200])
            self.assertEqual(set(win._HANDLER_BY_ADDR), self.original)
            self.assertEqual(fake.initialized, fake.uninitialized)

    async def test_query_interface_owns_a_reference_after_local_close(self):
        fake = NativeBoundary()
        with fake.patches():
            handler = win._CompletionHandler()
            address = handler._addr
            output = (ctypes.c_void_p * 1)()
            self.assertEqual(win._h_query_interface(address, ctypes.pointer(win._IID_IUNKNOWN), output), 0)
            self.assertEqual(output[0], address)
            handler.close()
            handler.close()
            self.assertIn(address, win._HANDLER_BY_ADDR)
            self.assertEqual(fake.closed, [])
            self.assertEqual(win._h_release(address), 0)
            self.assertCountEqual(fake.closed, fake.created)
            self.assertEqual(fake.released, [100])


if __name__ == '__main__':
    unittest.main()
