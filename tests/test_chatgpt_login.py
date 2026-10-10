"""登入途中關窗的離線清理契約，不執行真實登入。"""
import asyncio
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt import gui_chatgpt, gui as gui_mod
from vlt.session.codex_rpc import login_chatgpt


class GuiLoginTests(unittest.TestCase):
    def test_successful_login_updates_auth_controls_on_ui_thread(self):
        from vlt import i18n
        i18n.set_language('zh')
        gui = SimpleNamespace(_provider=lambda: 'chatgpt', _set_status=Mock(), _q=queue.Queue(),
                              _root=Mock(), _key_status=Mock(), _key_btn=Mock(), _key_chip=Mock())
        with patch('vlt.session.codex_rpc.login_chatgpt', new=AsyncMock(return_value=True)), \
             patch('vlt.session.codex_rpc.chatgpt_logged_in', new=AsyncMock(return_value=True), create=True):
            gui_chatgpt.open_signup(gui)
            gui._chatgpt_login_thread.join(2)
            self.assertFalse(gui._chatgpt_login_thread.is_alive())
            self.assertIsNotNone(gui._root.after.call_args, 'login completion never schedules a UI refresh')
            callback = gui._root.after.call_args.args[1]
            callback()
        self.assertTrue(gui._chatgpt_authenticated)
        self.assertEqual(gui._key_btn.configure.call_args.kwargs['text'], '切换 ChatGPT 帐户 ▸')
        self.assertNotIn('先登入', gui._key_status.configure.call_args.kwargs['text'])

    def test_startup_checks_saved_tool_login_and_updates_ui(self):
        gui = SimpleNamespace(_provider=lambda: 'chatgpt', _set_status=Mock(), _q=queue.Queue(),
                              _root=Mock(), _key_status=Mock(), _key_btn=Mock(), _key_chip=Mock())
        with patch('vlt.session.codex_rpc.chatgpt_logged_in', new=AsyncMock(return_value=True), create=True):
            gui_chatgpt.refresh_key_status(gui)
            self.assertTrue(hasattr(gui, '_chatgpt_login_thread'), 'startup never checks cached login')
            gui._chatgpt_login_thread.join(2)
            gui._root.after.call_args.args[1]()
        self.assertTrue(gui._chatgpt_authenticated)

    def test_account_switch_is_blocked_while_translation_runs(self):
        gui = SimpleNamespace(_provider=lambda: 'chatgpt', _set_status=Mock(),
                              _engines=[SimpleNamespace(running=True)])
        with patch('threading.Thread') as worker:
            gui_chatgpt.open_signup(gui)
        worker.assert_not_called()

    def test_translation_start_is_blocked_while_subscription_login_runs(self):
        gui = SimpleNamespace(_provider=lambda: 'chatgpt', _chatgpt_login_busy=True,
                              _set_status=Mock(), _sync_engine_ctx=Mock(), _engine_ctx=None,
                              _unsync_engine_ctx=Mock(), _refresh_voice_mode_btn=Mock(),
                              _engines=[], _pending_starts=0)
        with patch.object(gui_mod.gui_engine, 'start', return_value=False) as start:
            gui_mod.TranslationGUI._start(gui)
        start.assert_not_called()
        self.assertEqual(gui._pending_starts, 0)
        self.assertEqual(gui._engines, [])

    def test_repeated_close_does_not_interrupt_login_cleanup_again(self):
        loop, task = Mock(), Mock()
        gui = SimpleNamespace(_chatgpt_login_cancel=threading.Event(), _chatgpt_login_task=(loop, task))
        gui_chatgpt.cancel_login(gui)
        gui_chatgpt.cancel_login(gui)
        loop.call_soon_threadsafe.assert_called_once_with(task.cancel)

    def test_window_close_cancels_owned_login(self):
        started, closed = threading.Event(), threading.Event()
        owner = []

        async def login():
            owner.append((asyncio.get_running_loop(), asyncio.current_task()))
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        gui = SimpleNamespace(_provider=lambda: 'chatgpt', _set_status=Mock(), _q=queue.Queue(),
                              _close_proxy=Mock(), _sync_engine_ctx=Mock(), _engine_ctx=None)
        with patch('vlt.session.codex_rpc.login_chatgpt', side_effect=login), \
             patch.object(gui_mod.gui_engine, 'on_close'):
            try:
                gui_chatgpt.open_signup(gui)
                self.assertTrue(started.wait(2))
                gui_mod.TranslationGUI._on_close(gui)
                self.assertTrue(closed.wait(2), 'window close left login running')
            finally:
                if owner and not closed.is_set():
                    loop, task = owner[0]
                    loop.call_soon_threadsafe(task.cancel)
                closed.wait(2)
                worker = getattr(gui, '_chatgpt_login_thread', None)
                if worker:
                    worker.join(2)
        if worker:
            self.assertFalse(worker.is_alive())
            self.assertFalse(worker.daemon)


class LoginProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='vlt-login-test-')
        self.addCleanup(folder.cleanup)
        store = patch('vlt.paths._user_data_dir', return_value=Path(folder.name))
        store.start()
        self.addCleanup(store.stop)

    async def test_cached_login_status_requires_chatgpt_and_uses_isolated_store(self):
        from vlt.session import codex_rpc
        checker = getattr(codex_rpc, 'chatgpt_logged_in', None)
        self.assertTrue(callable(checker), 'cached ChatGPT login status is never checked')
        process = SimpleNamespace(returncode=0)
        for output, expected in ((b'Logged in using ChatGPT', True),
                                 (b'Logged in using an API key', False),
                                 (b'ChatGPT login failed', False),
                                 (b'Not logged in using ChatGPT', False)):
            process.communicate = AsyncMock(return_value=(output, b''))
            with patch('vlt.session.codex_rpc.codex_command', return_value=['fake-codex']), \
                 patch('asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)) as spawn:
                self.assertEqual(await checker(), expected)
            self.assertEqual(spawn.call_args.args[-2:], ('login', 'status'))
            self.assertIn('cli_auth_credentials_store="file"', spawn.call_args.args)

    async def test_unresponsive_login_is_killed_during_cancel(self):
        started, killed = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(side_effect=killed.set))

        async def wait():
            started.set()
            await killed.wait()
            process.returncode = 0
            return 0

        process.wait = wait
        with patch('vlt.session.codex_rpc.codex_command', return_value=['fake-codex']), \
             patch('asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            task = asyncio.create_task(login_chatgpt())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
        process.kill.assert_called_once()

    async def test_cancel_terminates_and_reaps_login_process(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None, terminate=Mock(side_effect=stopped.set))

        async def wait():
            started.set()
            await stopped.wait()
            process.returncode = 0
            return 0

        process.wait = wait
        with patch('vlt.session.codex_rpc.codex_command', return_value=['fake-codex']), \
             patch('asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            task = asyncio.create_task(login_chatgpt())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        self.assertEqual(process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
