"""登入途中關窗的離線清理契約，不執行真實登入。"""
import asyncio
import os
import queue
import sys
import subprocess
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
                              _close_proxy=Mock(), _close_proxy_hint=Mock(),
                              _sync_engine_ctx=Mock(), _engine_ctx=None)
        with patch('vlt.session.codex_rpc.login_chatgpt', side_effect=login), \
             patch.object(gui_mod.gui_engine, 'on_close'):
            try:
                gui_chatgpt.open_signup(gui)
                self.assertTrue(started.wait(2))
                gui_mod.TranslationGUI._on_close(gui)
                gui._close_proxy_hint.assert_called_once_with()
                gui._close_proxy.assert_called_once_with()
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
        from vlt.session.chatgpt_auth import AuthOwner
        owner = patch('vlt.session.codex_rpc.AUTH_OWNER', AuthOwner())
        owner.start()
        self.addCleanup(owner.stop)
        folder = tempfile.TemporaryDirectory(prefix='vlt-login-test-')
        self.addCleanup(folder.cleanup)
        store = patch('vlt.paths._user_data_dir', return_value=Path(folder.name))
        store.start()
        self.addCleanup(store.stop)

    async def test_relative_data_root_is_rejected_before_cli_spawn(self):
        from vlt import paths
        with patch.object(paths, '_user_data_dir', return_value=Path('relative-data')), \
             patch('asyncio.create_subprocess_exec', new=AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, '^auth-home$'):
                await login_chatgpt()
        spawn.assert_not_called()

    async def test_hardlinked_credentials_are_rejected_before_cli_spawn(self):
        from vlt.session.chatgpt_auth import auth_home
        home = auth_home()
        home.mkdir()
        external = home.parent / 'fake-global-auth.json'
        external.write_bytes(b'fake-credentials')
        os.link(external, home / 'auth.json')
        with patch('asyncio.create_subprocess_exec', new=AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, '^auth-home$'):
                await login_chatgpt()
        spawn.assert_not_called()
        self.assertEqual(external.read_bytes(), b'fake-credentials')

    async def test_repeated_cancel_keeps_owner_until_cli_reaped(self):
        from vlt.session.codex_rpc import AUTH_OWNER
        started, cleaning, reaped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock())

        async def wait():
            if not process.terminate.called:
                started.set()
                await asyncio.Event().wait()
            cleaning.set()
            await reaped.wait()
            process.returncode = 0
            return 0

        process.wait = wait
        other = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
        with patch('vlt.session.codex_rpc._spawn_codex', new=AsyncMock(side_effect=[process, other])) as spawn:
            first = asyncio.create_task(login_chatgpt())
            second = None
            try:
                await started.wait()
                first.cancel()
                await cleaning.wait()
                first.cancel()
                second = asyncio.create_task(login_chatgpt())
                await asyncio.sleep(.06)
                self.assertTrue(AUTH_OWNER._lock.locked(), 'owner released before child was reaped')
                self.assertEqual(spawn.call_count, 1)
            finally:
                reaped.set()
                await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertTrue(first.cancelled())
        self.assertEqual(process.returncode, 0)
        process.terminate.assert_called_once()

    async def test_unconfirmed_cli_cleanup_blocks_further_credential_access(self):
        from vlt.session.codex_rpc import AUTH_OWNER, chatgpt_logged_in
        process = SimpleNamespace(returncode=None,
                                  terminate=Mock(side_effect=PermissionError('private-terminate')),
                                  kill=Mock(side_effect=PermissionError('private-kill')),
                                  wait=AsyncMock(side_effect=OSError('private-wait')))
        generation = AUTH_OWNER.generation
        with patch('vlt.session.codex_rpc._spawn_codex', new=AsyncMock(return_value=process)) as spawn:
            with self.assertRaisesRegex(RuntimeError, '^process-cleanup$'):
                await login_chatgpt()
            for operation in (login_chatgpt, chatgpt_logged_in, AUTH_OWNER.credentials):
                with self.assertRaisesRegex(RuntimeError, '^auth-cleanup$'):
                    await operation()
        self.assertEqual(spawn.call_count, 1, 'unreaped writer must prevent another CLI or token read')
        self.assertGreater(AUTH_OWNER.generation, generation)
        process.kill.assert_called_once()

    async def test_terminate_failure_falls_back_to_kill_and_reap(self):
        started = asyncio.Event()
        process = SimpleNamespace(returncode=None,
                                  terminate=Mock(side_effect=PermissionError('private-terminate')))

        def kill():
            process.returncode = -1

        async def wait():
            if process.returncode is not None:
                return process.returncode
            if process.terminate.called:
                raise OSError('private-wait')
            started.set()
            await asyncio.Event().wait()

        process.kill, process.wait = Mock(side_effect=kill), wait
        with patch('vlt.session.codex_rpc._spawn_codex', new=AsyncMock(return_value=process)):
            task = asyncio.create_task(login_chatgpt())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        self.assertEqual(process.returncode, -1)

    async def test_linked_home_is_rejected_before_cli_spawn(self):
        with patch('pathlib.Path.resolve', return_value=Path('C:/other-store')), \
             patch('asyncio.create_subprocess_exec', new=AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, '^auth-home$'):
                await login_chatgpt()
        spawn.assert_not_called()

    async def test_actual_link_or_windows_junction_cannot_redirect_login(self):
        from vlt.session.chatgpt_auth import auth_home
        home = auth_home()
        target = home.parent / 'fake-global-codex'
        target.mkdir()
        if os.name == 'nt':
            created = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(home), str(target)],
                                     capture_output=True, timeout=10)
            self.assertEqual(created.returncode, 0, 'temporary junction could not be created')
        else:
            home.symlink_to(target, target_is_directory=True)
        try:
            with patch('asyncio.create_subprocess_exec', new=AsyncMock()) as spawn:
                with self.assertRaisesRegex(RuntimeError, '^auth-home$'):
                    await login_chatgpt()
            spawn.assert_not_called()
            self.assertEqual(list(target.iterdir()), [])
        finally:
            if os.name == 'nt':
                os.rmdir(home)
            else:
                home.unlink()

    @unittest.skipUnless(sys.platform != 'win32', 'POSIX permission contract')
    async def test_existing_private_home_is_tightened_before_cli_spawn(self):
        from vlt.session.chatgpt_auth import auth_home
        home = auth_home()
        home.mkdir(mode=0o755)
        home.chmod(0o755)
        process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

        async def spawn(*args, **kwargs):
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            return process

        with patch('vlt.session.codex_rpc.codex_command', return_value=['fake-codex']), \
             patch('asyncio.create_subprocess_exec', side_effect=spawn):
            await login_chatgpt()

    async def test_parallel_logins_have_one_credential_writer(self):
        first_started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def spawn(*args, **kwargs):
            nonlocal count
            count += 1
            process = SimpleNamespace(returncode=0)

            async def wait():
                first_started.set()
                await release.wait()
                return 0

            process.wait = wait
            return process

        with patch('vlt.session.codex_rpc._spawn_codex', side_effect=spawn):
            first = asyncio.create_task(login_chatgpt())
            await first_started.wait()
            second = asyncio.create_task(login_chatgpt())
            try:
                await asyncio.sleep(0.06)
                self.assertEqual(count, 1, 'two CLI processes can replace the same credentials')
            finally:
                release.set()
                await asyncio.gather(first, second)

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
