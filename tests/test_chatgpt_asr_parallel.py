"""三次盲聽並行，但仍等候全部結果及已送出的 HTTP 清理。"""
import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_auth import Credentials
from vlt.session.chatgpt_relay import SubscriptionRelay


class ParallelASRTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = SimpleNamespace(generation=2, require_usable=lambda: None,
            credentials=AsyncMock(return_value=Credentials('fake-account', 'fake-access', 2)))
        self.relay = SubscriptionRelay(self.owner)

    async def test_three_reads_overlap_and_majority_waits_for_last_result(self):
        entered = [asyncio.Event() for _ in range(3)]
        release = [asyncio.Event() for _ in range(3)]
        calls = 0

        async def read(*args):
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            return ('same', 'same', 'different')[index]

        with patch.object(self.relay, '_transcribe', side_effect=read) as call:
            task = asyncio.create_task(self.relay.audio_asr(b'\x00\x00', 'en'))
            try:
                await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)), .2)
                release[0].set()
                release[1].set()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(task.done(), 'two agreeing results do not bypass the third read')
                release[2].set()
                self.assertEqual(await task, 'same')
                self.assertEqual(call.await_count, 3)
            finally:
                for event in release:
                    event.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_read_cannot_escape_while_other_http_workers_are_owned(self):
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, finished = 0, 0

        def request(*args):
            nonlocal calls, finished
            with lock:
                calls += 1
                index = calls
                if calls == 3:
                    entered.set()
            try:
                if index == 1:
                    entered.wait(2)
                    return b'{"text":"same","tool":{}}'
                release.wait(2)
                return b'{"text":"same"}'
            finally:
                with lock:
                    finished += 1

        with patch('vlt.session.chatgpt_relay.request', side_effect=request):
            task = asyncio.create_task(self.relay.audio_asr(b'\x00\x00', 'en'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, .5), 'all three HTTP requests must start')
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
            finally:
                release.set()
                result = (await asyncio.gather(task, return_exceptions=True))[0]
            self.assertIsInstance(result, RuntimeError)
            self.assertEqual(str(result), 'asr-result')
            self.assertEqual((calls, finished), (3, 3))

    async def test_repeated_parent_cancellation_reaps_all_three_http_workers(self):
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, finished = 0, 0

        def request(*args):
            nonlocal calls, finished
            with lock:
                calls += 1
                if calls == 3:
                    entered.set()
            try:
                release.wait(2)
                return b'{"text":"must-not-release"}'
            finally:
                with lock:
                    finished += 1

        with patch('vlt.session.chatgpt_relay.request', side_effect=request):
            task = asyncio.create_task(self.relay.audio_asr(b'\x00\x00', 'en'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, .5), 'all three HTTP requests must start')
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(.01)
                self.assertFalse(task.done(), 'cancelled direction still owns in-flight requests')
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            self.assertEqual((calls, finished), (3, 3))

    async def test_already_cancelled_parent_cannot_start_an_upload(self):
        async def cancelled_parent():
            asyncio.current_task().cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            return await self.relay.audio_asr(b'\x00\x00', 'en')

        with patch.object(self.relay, '_transcribe') as read:
            task = asyncio.create_task(cancelled_parent())
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            read.assert_not_called()

    async def test_account_change_while_reads_are_inflight_rejects_whole_group(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls, finished = 0, 0

        async def read(*args):
            nonlocal calls, finished
            calls += 1
            if calls == 3:
                entered.set()
            try:
                await release.wait()
                return 'same'
            finally:
                finished += 1

        with patch.object(self.relay, '_transcribe', side_effect=read):
            task = asyncio.create_task(self.relay.audio_asr(b'\x00\x00', 'en'))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                self.owner.generation += 1
            finally:
                release.set()
                result = (await asyncio.gather(task, return_exceptions=True))[0]
            self.assertIsInstance(result, RuntimeError)
            self.assertEqual(str(result), 'asr-stale')
            self.assertEqual((calls, finished), (3, 3))


if __name__ == '__main__':
    unittest.main()
