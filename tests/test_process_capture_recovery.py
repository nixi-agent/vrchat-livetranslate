"""A live VRChat PID must not hide a failed process capture worker."""
import asyncio
import ctypes
import threading
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt import engine
from vlt.platform import win


class CaptureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_failure_drains_already_captured_pcm_before_signalling_eof(self):
        source = win.ProcessLoopbackSource(asyncio.get_running_loop(),pid=123)
        raw = b'\x11\x00' * 1920
        def pump(stop):
            source._emit(raw)
            source._emit(raw)
            raise RuntimeError('fake capture failure')
        source._pump = pump
        source._run()
        await asyncio.sleep(0)
        self.assertFalse(source.finished, 'queued PCM must be delivered before EOF')
        session = SimpleNamespace(send_audio=AsyncMock(),end_input_segment=Mock())
        with patch.object(win,'pid_alive',return_value=True):
            await engine._pump_capture(session,None,0,None,'loopback',lambda:source,
                                       keep_alive=lambda:not source.finished)
        self.assertEqual(session.send_audio.await_count,2)
        self.assertEqual(b''.join(c.args[0] for c in session.send_audio.await_args_list),
                         engine.to_16k_mono(raw,48000,2)*2)
        session.end_input_segment.assert_called_once()
        self.assertTrue(source.finished)
        self.assertTrue(source._closed)

    async def test_failed_worker_reopens_with_backoff_and_stop_closes_the_replacement(self):
        stop = threading.Event()
        ended = SimpleNamespace(rate=48000,channels=2,finished=True,
            process_gone=lambda:False,read=AsyncMock(),close=Mock())
        async def read(**kwargs):
            stop.set()
        live = SimpleNamespace(rate=48000,channels=2,finished=False,
            process_gone=lambda:False,read=AsyncMock(side_effect=read),close=Mock())
        with patch.object(engine, '_wait_for_vrchat_process', AsyncMock(return_value=123)), \
             patch.object(engine.platform, 'open_process_loopback', side_effect=[ended,live]) as open_source, \
             patch.object(engine.asyncio, 'sleep', AsyncMock()) as sleep:
            await engine._run_loopback_process(SimpleNamespace(),None,stop_event=stop)
        self.assertEqual(open_source.call_count,2)
        ended.read.assert_not_awaited()
        live.read.assert_awaited_once()
        sleep.assert_awaited_once_with(2.0)
        ended.close.assert_called_once()
        live.close.assert_called_once()

    async def test_native_packet_errors_end_source_and_exit_actual_capture_consumer(self):
        for fail_at in ('packet', 'buffer'):
            with self.subTest(fail_at=fail_at):
                kernel = SimpleNamespace(WaitForSingleObject=Mock(return_value=0), CloseHandle=Mock())
                source = win.ProcessLoopbackSource(asyncio.get_running_loop(), pid=123,
                    client=ctypes.c_void_p(11), capture=ctypes.c_void_p(12), event=7)

                def call(ptr, index, _result, _types, *args):
                    if index == 5 and fail_at == 'buffer':
                        ctypes.cast(args[0], ctypes.POINTER(ctypes.c_ulong)).contents.value = 1
                        return 0
                    return -2004287484 if index in (3, 5) else 0

                with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                     patch.object(win, '_vcall', side_effect=call), \
                     patch.object(win, 'pid_alive', return_value=True):
                    source._run()
                    await asyncio.sleep(0)
                    self.assertFalse(source.process_gone(), 'the target process remains alive')
                    self.assertTrue(getattr(source, 'finished', False), 'worker termination must reach its consumer')
                    source.read = AsyncMock(return_value=None)
                    with patch.object(engine, '_wait_for_vrchat_process', AsyncMock(return_value=123)), \
                         patch.object(engine.platform, 'open_process_loopback', return_value=source):
                        await engine._run_loopback_process(SimpleNamespace(), None, seconds=.02)
                    source.read.assert_not_awaited()
                    self.assertTrue(source._closed)


if __name__ == '__main__':
    unittest.main()
