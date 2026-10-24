"""Offline room approval boundaries; fake writers and local WebSocket only."""
import asyncio
import inspect
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.room.client import RoomClient
from vlt.room.model import RoomConfig
from vlt.room.protocol import FRAME_PONG
from vlt.session.chatgpt_output import allowed, current, scope
from vlt.gui_room import publish_to_room
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator


class Approval:
    def __init__(self, direction='mine'):
        self.valid = True
        self.direction = direction

    def __call__(self, direction=None):
        return self.valid and (direction is None or direction == self.direction)


class RoomApprovalTests(unittest.IsolatedAsyncioTestCase):
    def room(self):
        return RoomClient(RoomConfig(nickname='offline-test'), Mock())

    def schedule(self, room, scheduled):
        room._loop = asyncio.get_running_loop()
        room._ws = SimpleNamespace(send=AsyncMock())
        def capture(coro, loop):
            scheduled.append(coro)
            return Mock()
        return patch('asyncio.run_coroutine_threadsafe', side_effect=capture)

    async def test_replay_drops_stale_and_preserves_fresh(self):
        room, stale = self.room(), Approval()
        with scope(stale):
            room.publish('old', 1, 'old source', True)
        with scope(Approval()):
            room.publish('fresh', 1, 'fresh source', True)
        stale.valid = False
        ws = SimpleNamespace(send=AsyncMock())
        await room._replay_pending(ws)
        self.assertEqual(ws.send.await_count, 1)
        self.assertIn('fresh source', ws.send.call_args.args[0])
        self.assertNotIn('old', room._replay)

    async def test_scheduled_send_rechecks_outside_publish_scope(self):
        room, approval, scheduled = self.room(), Approval(), []
        with self.schedule(room, scheduled), scope(approval):
            room.publish('old', 1, 'old source', True)
        approval.valid = False
        await scheduled[0]
        room._ws.send.assert_not_awaited()
        self.assertNotIn('old', room._replay)



    async def test_other_direction_does_not_enter_replay(self):
        room = self.room()
        with scope(Approval('theirs')):
            room.publish('other', 1, 'remote source', True)
        self.assertEqual(room._replay, {})

    async def test_old_scheduled_candidate_cannot_delete_fresh_same_utterance(self):
        room, stale, scheduled = self.room(), Approval(), []
        with self.schedule(room, scheduled):
            with scope(stale):
                room.publish('same', 1, 'old source', True)
            with scope(Approval()):
                room.publish('same', 2, 'fresh source', True)
        stale.valid = False
        await scheduled[0]
        await scheduled[1]
        self.assertEqual(room._ws.send.await_count, 1)
        self.assertEqual(room._replay['same'].text, 'fresh source')

    async def test_stop_prevents_already_scheduled_send(self):
        room, scheduled = self.room(), []
        with self.schedule(room, scheduled), scope(Approval()):
            room.publish('old', 1, 'source', True)
        room._stop_event.set()
        await scheduled[0]
        room._ws.send.assert_not_awaited()

    async def test_revocation_while_serializing_replay_is_checked_before_send(self):
        room, approval = self.room(), Approval()
        with scope(approval):
            room.publish('old', 1, 'source', True)
        transport = inspect.getmodule(room._replay_pending)
        original = transport.encode_frame
        def revoke(*args, **kwargs):
            frame = original(*args, **kwargs)
            approval.valid = False
            return frame
        ws = SimpleNamespace(send=AsyncMock())
        with patch.object(transport, 'encode_frame', side_effect=revoke):
            await room._replay_pending(ws)
        ws.send.assert_not_awaited()

    async def test_fresh_approved_send_once_and_legacy_control_unchanged(self):
        room, scheduled = self.room(), []
        with self.schedule(room, scheduled):
            with scope(Approval()):
                room.publish('fresh', 1, 'source', True)
            room._send(FRAME_PONG, ts=1)
        for coro in scheduled:
            await coro
        self.assertEqual(room._ws.send.await_count, 2)
        self.assertEqual(room._sent, 2)

    async def test_real_local_writer_receives_fresh_only_after_scheduling(self):
        import websockets

        received = []
        async def handler(ws):
            async for raw in ws:
                received.append(raw)
        async with websockets.serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            room = RoomClient(RoomConfig(server_url=f'ws://127.0.0.1:{port}/ws'), Mock())
            room._loop = asyncio.get_running_loop()
            ws = await room._open_ws(room._connect_url())
            self.addAsyncCleanup(ws.close)
            room._ws = ws
            original = asyncio.run_coroutine_threadsafe
            futures = []
            def schedule(coro, loop):
                future = original(coro, loop)
                futures.append(future)
                return future
            stale = Approval()
            with patch('asyncio.run_coroutine_threadsafe', side_effect=schedule):
                with scope(stale):
                    room.publish('old', 1, 'old source', True)
                stale.valid = False
                with scope(Approval()):
                    room.publish('fresh', 1, 'fresh source', True)
            await asyncio.gather(*(asyncio.wrap_future(f) for f in futures))
            await ws.close()
            self.assertEqual(len(received), 1)
            self.assertIn('fresh source', received[0])

    async def test_actual_coordinator_approval_survives_gui_and_revokes_room_replay(self):
        from vlt.gui_room import publish_to_room
        from vlt.room.publisher import SourcePublisher
        from vlt.session.base import SessionConfig
        from vlt.session.chatgpt_pipeline import Coordinator

        verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                       negation=True, complete=True, extra_content=False)
        services = SimpleNamespace(source_asr=AsyncMock(return_value='source'),
            translate=AsyncMock(return_value=dict(translation='translation', language='ja')),
            validate_text=AsyncMock(return_value=verdict))
        pipe = Coordinator({'mine': SessionConfig(provider='chatgpt', source_lang='zh',
            target_lang='ja', output_audio=False)}, services, Mock(), lambda: 1)
        self.addAsyncCleanup(pipe.close)
        room = self.room()
        room.cfg.enabled = True
        publisher = SourcePublisher()
        self.assertTrue(pipe.submit('mine', bytes(320)))
        await asyncio.gather(*tuple(pipe._owned))
        def text(delta):
            publish_to_room(room, room.cfg, publisher, 'mic', delta.source, delta.is_final)
        self.assertTrue(pipe.consume('mine', text, Mock()))
        self.assertEqual(len(room._replay), 1)
        self.assertFalse(pipe.consume('mine', text, Mock()))
        pipe.invalidate()
        ws = SimpleNamespace(send=AsyncMock())
        await room._replay_pending(ws)
        ws.send.assert_not_awaited()
        self.assertEqual(room._replay, {})


class GuiRoomApprovalTests(unittest.TestCase):
    def publish(self, room, publisher, *, source='mic', final=True):
        from vlt.gui_room import publish_to_room
        publish_to_room(room, RoomConfig(enabled=True), publisher, source, 'source', final)

    def test_revoked_and_wrong_direction_never_change_publisher(self):
        for approval in (Approval('theirs'), Approval()):
            if approval.direction == 'mine':
                approval.valid = False
            room, publisher = Mock(), Mock()
            with scope(approval):
                self.publish(room, publisher)
            publisher.feed.assert_not_called()
            publisher.reset.assert_not_called()
            room.publish.assert_not_called()

    def test_approved_segments_keep_real_repeats_and_do_not_relabel_previous_partial(self):
        from vlt.room.publisher import SourcePublisher
        publisher = SourcePublisher(new_utt=iter(('old', 'one', 'two', 'three')).__next__)
        publisher.feed('previous partial', False, now_ms=0)
        room = Mock()
        for _ in range(3):
            with scope(Approval()):
                self.publish(room, publisher)
        self.assertEqual(room.publish.call_count, 3)
        self.assertEqual([c.args[0] for c in room.publish.call_args_list], ['one', 'two', 'three'])
        self.assertTrue(all(c.args[2:] == ('source', True) for c in room.publish.call_args_list))

    def test_unsealed_approved_text_does_not_reach_publisher(self):
        room, publisher = Mock(), Mock()
        with scope(Approval()):
            self.publish(room, publisher, final=False)
        publisher.feed.assert_not_called()

    def test_legacy_duplicate_filter_and_loopback_exclusion_unchanged(self):
        from vlt.room.publisher import SourcePublisher
        room, publisher = Mock(), SourcePublisher()
        self.publish(room, publisher)
        self.publish(room, publisher)
        self.assertEqual(room.publish.call_count, 1)
        publisher = Mock()
        with scope(Approval('theirs')):
            self.publish(room, publisher, source='loopback')
        publisher.reset.assert_not_called()
        publisher.feed.assert_not_called()


class ReviewFindings(unittest.IsolatedAsyncioTestCase):
    def room(self):
        return RoomClient(RoomConfig(enabled=True, nickname='offline-test',
            room_code='ABCD1234', server_url='ws://127.0.0.1:8765/ws'), Mock())

    async def test_legitimate_theirs_is_not_revoked_by_room_exclusion(self):
        verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                       negation=True, complete=True, extra_content=False)
        services = SimpleNamespace(source_asr=AsyncMock(return_value='source'),
            translate=AsyncMock(return_value=dict(translation='translation', language='ja')),
            validate_text=AsyncMock(return_value=verdict))
        pipe = Coordinator({'theirs': SessionConfig(provider='chatgpt', source_lang='zh',
            target_lang='ja', output_audio=False)}, services, Mock(), lambda: 1)
        self.addAsyncCleanup(pipe.close)
        room, publisher, approvals = self.room(), Mock(), []
        def text(delta):
            approvals.append(current())
            publish_to_room(room, room.cfg, publisher, 'loopback', delta.source, delta.is_final)
        pipe.submit('theirs', bytes(320))
        await asyncio.gather(*tuple(pipe._owned))
        self.assertTrue(pipe.consume('theirs', text, Mock()))
        publisher.feed.assert_not_called()
        self.assertEqual(room._replay, {})
        self.assertTrue(allowed(approvals[0], 'theirs'))

    def restart_without_network(self, room):
        fake_thread = Mock()
        fake_thread.is_alive.return_value = False
        fake_thread.start.side_effect = room._ready.set
        with patch('threading.Thread', return_value=fake_thread):
            room.start()
        room._loop = asyncio.get_running_loop()
        room._ws = SimpleNamespace(send=AsyncMock())

    async def test_stop_then_restart_does_not_replay_old_final(self):
        room = self.room()
        with scope(lambda direction=None: True):
            room.publish('old', 1, 'source', True)
        room.stop()
        self.restart_without_network(room)
        await room._replay_pending(room._ws)
        room._ws.send.assert_not_awaited()

    async def test_old_scheduled_send_does_not_target_new_connection_after_restart(self):
        room, scheduled = self.room(), []
        room._loop = asyncio.get_running_loop()
        room._ws = SimpleNamespace(send=AsyncMock())
        with patch('asyncio.run_coroutine_threadsafe',
                   side_effect=lambda coro, loop: scheduled.append(coro) or Mock()):
            with scope(lambda direction=None: True):
                room.publish('old', 1, 'source', True)
        room.stop()
        self.restart_without_network(room)
        await scheduled[0]
        room._ws.send.assert_not_awaited()


    async def test_restart_preserves_legacy_replay_but_removes_approved_replay(self):
        room = self.room()
        room.publish('legacy', 1, 'legacy source', True)
        with scope(lambda direction=None: True):
            room.publish('approved', 1, 'approved source', True)
        room.stop()
        self.restart_without_network(room)
        await room._replay_pending(room._ws)
        self.assertEqual(room._ws.send.await_count, 1)
        self.assertIn('legacy source', room._ws.send.call_args.args[0])
        self.assertEqual(set(room._replay), {'legacy'})


    async def test_restart_rejects_old_schedule_and_accepts_fresh_approved_final(self):
        room, scheduled = self.room(), []
        room._loop = asyncio.get_running_loop()
        room._ws = SimpleNamespace(send=AsyncMock())
        capture = lambda coro, loop: scheduled.append(coro) or Mock()
        with patch('asyncio.run_coroutine_threadsafe', side_effect=capture):
            with scope(lambda direction=None: True):
                room.publish('old', 1, 'old source', True)
        room.stop()
        self.restart_without_network(room)
        with patch('asyncio.run_coroutine_threadsafe', side_effect=capture):
            with scope(lambda direction=None: True):
                room.publish('fresh', 1, 'fresh source', True)
        for coro in scheduled:
            await coro
        self.assertEqual(room._ws.send.await_count, 1)
        self.assertIn('fresh source', room._ws.send.call_args.args[0])
        self.assertEqual(set(room._replay), {'fresh'})
        self.assertNotIn('old', room._replay)


    async def test_stop_restart_during_final_check_never_targets_new_writer(self):
        room = self.room()
        with scope(lambda direction=None: True):
            room.publish('old', 1, 'source', True)
        pending = room._replay['old']
        old_writer = SimpleNamespace(send=AsyncMock())
        room._ws = old_writer
        reached, resume, restarted = threading.Event(), threading.Event(), threading.Event()
        errors, replacement = [], []
        real_is_set = room._stop_event.is_set
        def pause_sender():
            value = real_is_set()
            if threading.current_thread().name == 'old-room-sender':
                reached.set()
                if not resume.wait(3):
                    raise AssertionError('sender barrier timed out')
            return value
        def send():
            try:
                asyncio.run(room._ws_send('old-frame', pending.approval, pending))
            except BaseException as exc:
                errors.append(exc)
        def restart():
            try:
                room.stop()
                fake_thread = Mock()
                fake_thread.is_alive.return_value = False
                fake_thread.start.side_effect = room._ready.set
                with patch('threading.Thread', return_value=fake_thread):
                    room.start()
                room._ws = SimpleNamespace(send=AsyncMock())
                replacement.append(room._ws)
            except BaseException as exc:
                errors.append(exc)
            finally:
                restarted.set()
        sender = threading.Thread(target=send, name='old-room-sender')
        stopper = threading.Thread(target=restart, name='room-restarter')
        with patch.object(room._stop_event, 'is_set', side_effect=pause_sender):
            sender.start()
            try:
                self.assertTrue(reached.wait(3))
                # Determine a safe barrier order without blocking stop on a held lock.
                unlocked = room._lock.acquire(blocking=False)
                if unlocked:
                    room._lock.release()
                stopper.start()
                if unlocked:
                    self.assertTrue(restarted.wait(3))
                else:
                    self.assertTrue(room._stop_event.wait(3))
            finally:
                resume.set()
                sender.join(3)
                if stopper.ident is not None:
                    stopper.join(3)
        self.assertFalse(sender.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(replacement), 1)
        replacement[0].send.assert_not_awaited()

    async def test_publish_paused_before_admission_cannot_reinsert_after_stop(self):
        room = self.room()
        reached, resume = threading.Event(), threading.Event()
        errors = []
        calls = 0
        def approval(direction=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                reached.set()
                if not resume.wait(3):
                    raise AssertionError('publisher barrier timed out')
            return True
        def publish():
            try:
                with scope(approval):
                    room.publish('old', 1, 'source', True)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=publish)
        worker.start()
        try:
            self.assertTrue(reached.wait(3))
            room.stop()
            self.restart_without_network(room)
            room._loop = None
        finally:
            resume.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(room._replay, {})

if __name__ == '__main__':
    unittest.main()
