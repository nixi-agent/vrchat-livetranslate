"""Cold TLS preparation precedes ready state; no network, credentials or devices."""
import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.base import SessionConfig
from vlt.session import chatgpt_validated as validated


class StartupTLSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = SimpleNamespace(generation=1, require_usable=Mock(),
            credentials=AsyncMock(return_value=SimpleNamespace(generation=1)))
        self.prepare = Mock()
        for name, value in [('vlt.session.chatgpt_auth.AUTH_OWNER', self.owner),
                            ('vlt.session.chatgpt_validated.tls_context', self.prepare)]:
            context = patch(name, value, create=True)
            context.start()
            self.addCleanup(context.stop)

    def session(self, direction='mine'):
        cfg = SessionConfig(provider='chatgpt', source_lang='zh' if direction == 'mine' else 'ja',
            target_lang='ja' if direction == 'mine' else 'zh', output_audio=direction == 'mine')
        cfg._direction = direction
        session = validated.ChatGPTValidatedSession(cfg)
        self.addAsyncCleanup(session.close)
        return session

    async def assert_failed_start(self, session, reason='session-start'):
        with patch.object(validated, 'Coordinator', wraps=validated.Coordinator) as coordinator:
            with self.assertRaisesRegex(RuntimeError, '^' + reason + '$') as raised:
                await session.start(Mock(), Mock())
            coordinator.assert_not_called()
        self.assertIsNone(raised.exception.__cause__)
        self.assertFalse(session.is_alive)
        self.assertTrue(session._closing)
        self.assertIsNone(session._pipeline)
        self.assertIsNone(session.on_text)
        self.assertIsNone(session.on_audio)

    async def test_both_directions_prepare_before_coordinator_and_ready(self):
        for direction in ('mine', 'theirs'):
            with self.subTest(direction=direction):
                session = self.session(direction)
                self.prepare.reset_mock()
                events = []

                def prepare():
                    self.assertGreater(self.owner.credentials.await_count, 0)
                    self.assertFalse(session.is_alive)
                    self.assertIsNone(session._pipeline)
                    events.append('tls')

                original = validated.Coordinator

                def coordinate(*args):
                    events.append('coordinator')
                    return original(*args)

                self.prepare.side_effect = prepare
                with patch.object(validated, 'Coordinator', side_effect=coordinate):
                    await session.start(Mock(), Mock())
                self.assertTrue(session.is_alive)
                self.assertEqual(events, ['tls', 'coordinator'])
                self.prepare.assert_called_once_with()
                await session.close()

    async def test_tls_failure_closes_without_publishing_ready_or_provider_data(self):
        self.prepare.side_effect = RuntimeError('private-ca-error')
        await self.assert_failed_start(self.session())
        self.prepare.assert_called_once_with()

    async def test_auth_failure_never_initializes_tls(self):
        self.owner.credentials.side_effect = RuntimeError('private-auth-error')
        await self.assert_failed_start(self.session())
        self.prepare.assert_not_called()

    async def test_close_during_tls_preparation_cannot_publish_ready(self):
        session = self.session()
        self.prepare.side_effect = lambda: setattr(session, '_closing', True)
        await self.assert_failed_start(session)
        self.prepare.assert_called_once_with()

    async def test_account_change_during_tls_preparation_cannot_publish_ready(self):
        self.prepare.side_effect = lambda: setattr(self.owner, 'generation', 2)
        await self.assert_failed_start(self.session())
        self.prepare.assert_called_once_with()

    async def test_quarantine_during_tls_preparation_cannot_publish_ready(self):
        def quarantine():
            self.owner.require_usable.side_effect = RuntimeError('auth-cleanup')
        self.prepare.side_effect = quarantine
        await self.assert_failed_start(self.session())
        self.prepare.assert_called_once_with()

    async def test_cancellation_during_tls_preparation_propagates_and_closes(self):
        session = self.session()
        self.prepare.side_effect = asyncio.CancelledError()
        with patch.object(validated, 'Coordinator', wraps=validated.Coordinator) as coordinator:
            with self.assertRaises(asyncio.CancelledError):
                await session.start(Mock(), Mock())
            coordinator.assert_not_called()
        self.assertFalse(session.is_alive)
        self.assertTrue(session._closing)
        self.assertIsNone(session._pipeline)
        self.assertIsNone(session.on_text)

    async def test_invalid_input_never_initializes_tls(self):
        session = self.session()
        session.cfg._direction = session._direction = None
        with self.assertRaisesRegex(RuntimeError, '^session-input$'):
            await session.start(Mock(), Mock())
        self.owner.credentials.assert_not_called()
        self.prepare.assert_not_called()

    async def test_cross_thread_cancel_queued_during_tls_never_publishes_ready(self):
        session = self.session()
        loop = asyncio.get_running_loop()
        entered, scheduled = threading.Event(), threading.Event()
        task = asyncio.create_task(session.start(Mock(), Mock()))

        def cancel():
            if entered.wait(2):
                loop.call_soon_threadsafe(task.cancel)
                scheduled.set()

        def prepare():
            entered.set()
            self.assertTrue(scheduled.wait(2))

        self.prepare.side_effect = prepare
        worker = threading.Thread(target=cancel)
        worker.start()
        try:
            with patch.object(validated, 'Coordinator', wraps=validated.Coordinator) as coordinator:
                with self.assertRaises(asyncio.CancelledError):
                    await task
                coordinator.assert_not_called()
        finally:
            worker.join(2)
            self.assertFalse(worker.is_alive())
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(session.is_alive)
        self.assertTrue(session._closing)
        self.assertIsNone(session._pipeline)
        self.assertIsNone(session.on_text)

    async def test_queued_context_changes_after_tls_never_publish_ready(self):
        loop = asyncio.get_running_loop()
        for kind in ('close', 'account', 'quarantine', 'admission'):
            with self.subTest(kind=kind):
                self.owner.generation = 1
                self.owner.require_usable.side_effect = None
                session = self.session()
                session._engine_admission = SimpleNamespace(valid=Mock(return_value=True))

                def invalidate():
                    if kind == 'close':
                        session._closing = True
                    elif kind == 'account':
                        self.owner.generation += 1
                    elif kind == 'quarantine':
                        self.owner.require_usable.side_effect = RuntimeError('auth-cleanup')
                    else:
                        session._engine_admission.valid.return_value = False

                self.prepare.side_effect = lambda: loop.call_soon(invalidate)
                try:
                    await self.assert_failed_start(session,
                        'session-context' if kind == 'admission' else 'session-start')
                finally:
                    await asyncio.sleep(0)


if __name__ == '__main__':
    unittest.main()
