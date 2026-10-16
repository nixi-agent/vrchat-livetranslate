"""JSON-RPC 的有界等待、工具拒絕及隱私邊界。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.codex_rpc import CodexRPC, login_chatgpt


class RPCTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from vlt.session.chatgpt_auth import AuthOwner
        self.owner = AuthOwner()
        guard = patch('vlt.session.codex_rpc.AUTH_OWNER', self.owner)
        guard.start()
        self.addCleanup(guard.stop)

    async def test_pending_rpc_cleanup_blocks_new_login_until_reap(self):
        entered, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None,
                                  terminate=Mock(side_effect=PermissionError('private-term')),
                                  kill=Mock(side_effect=PermissionError('private-kill')))
        count = 0

        async def wait():
            nonlocal count
            count += 1
            if count == 1:
                raise OSError('private-wait')
            entered.set()
            await release.wait()
            process.returncode = 0
            return 0

        process.wait = wait
        rpc = CodexRPC(lambda _: None)
        rpc.process = process
        other = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
        with patch('vlt.session.codex_rpc._spawn_codex', new=AsyncMock(return_value=other)) as spawn:
            closing = asyncio.create_task(rpc.close())
            await entered.wait()
            login = asyncio.create_task(login_chatgpt())
            try:
                await asyncio.sleep(.06)
                self.assertEqual(spawn.call_count, 0, 'unreaped RPC still owns credential cleanup')
            finally:
                release.set()
                await asyncio.gather(closing, login, return_exceptions=True)
        self.assertTrue(login.result(), 'normal login must recover after confirmed RPC reap')

    async def test_closed_rpc_rejects_start_waiting_for_owner(self):
        rpc = CodexRPC(lambda _: None)
        rpc._read, rpc.request, rpc._send = AsyncMock(), AsyncMock(), AsyncMock()
        with patch('vlt.session.codex_rpc._spawn_codex', new=AsyncMock()) as spawn:
            async with self.owner.exclusive():
                starting = asyncio.create_task(rpc.start('unused'))
                await asyncio.sleep(.02)
                closing = asyncio.create_task(rpc.close())
                await asyncio.sleep(.02)
            try:
                with self.assertRaisesRegex(RuntimeError, '^rpc-closed$'):
                    await starting
                await closing
                await rpc.close()
                spawn.assert_not_called()
            finally:
                await asyncio.gather(starting, closing, return_exceptions=True)

    async def test_duplicate_start_preserves_original_process(self):
        rpc = CodexRPC(lambda _: None)
        rpc._read, rpc.request, rpc._send = AsyncMock(), AsyncMock(), AsyncMock()
        child = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0))
        child.terminate = Mock(side_effect=lambda: setattr(child, 'returncode', 0))
        with patch('vlt.session.codex_rpc._spawn_codex', new=AsyncMock(return_value=child)) as spawn:
            await rpc.start('unused')
            try:
                with self.assertRaisesRegex(RuntimeError, '^rpc-started$'):
                    await rpc.start('unused')
                self.assertIs(rpc.process, child)
                self.assertEqual(spawn.call_count, 1)
            finally:
                await rpc.close()
        child.terminate.assert_called_once()

    async def test_close_during_spawn_reaps_child_without_starting_reader(self):
        rpc = CodexRPC(lambda _: None)
        rpc._read, rpc.request, rpc._send = AsyncMock(), AsyncMock(), AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        child = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0))
        child.terminate = Mock(side_effect=lambda: setattr(child, 'returncode', 0))

        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return child

        with patch('vlt.session.codex_rpc._spawn_codex', new=spawn):
            starting = asyncio.create_task(rpc.start('unused'))
            await entered.wait()
            closing = asyncio.create_task(rpc.close())
            await asyncio.sleep(0)
            release.set()
            try:
                with self.assertRaisesRegex(RuntimeError, '^rpc-closed$'):
                    await starting
            finally:
                await asyncio.gather(starting, closing, return_exceptions=True)
        child.terminate.assert_called_once()
        rpc._read.assert_not_called()
        rpc.request.assert_not_called()

    async def test_quarantine_blocks_rpc_start_but_allows_owned_cleanup(self):
        self.owner.quarantine()
        rpc = CodexRPC(lambda _: None)
        rpc._read, rpc.request, rpc._send = AsyncMock(), AsyncMock(), AsyncMock()
        with patch('vlt.session.codex_rpc._spawn_codex', new=AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, '^auth-cleanup$'):
                await rpc.start('unused')
        spawn.assert_not_called()
        rpc.process = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0))
        rpc.process.terminate = Mock(side_effect=lambda: setattr(rpc.process, 'returncode', 0))
        await rpc.close()
        rpc.process.terminate.assert_called_once()

    async def test_cancelled_rpc_close_waiting_for_owner_still_reaps(self):
        rpc = CodexRPC(lambda _: None)
        rpc.process = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0))
        rpc.process.terminate = Mock(side_effect=lambda: setattr(rpc.process, 'returncode', 0))
        async with self.owner.exclusive():
            closing = asyncio.create_task(rpc.close())
            await asyncio.sleep(.02)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(closing.done(), 'owner acquisition remains owned during repeated cancellation')
        with self.assertRaises(asyncio.CancelledError):
            await closing
        rpc.process.terminate.assert_called_once()
        self.assertEqual(rpc.process.returncode, 0)

    async def test_unkillable_process_has_bounded_reap_and_quarantines_owner(self):
        release = asyncio.Event()
        rpc = CodexRPC(lambda _: None)
        rpc.process = SimpleNamespace(returncode=None,
                                      terminate=Mock(side_effect=PermissionError('private-term')),
                                      kill=Mock(side_effect=PermissionError('private-kill')))

        async def wait():
            await release.wait()
            return 0

        rpc.process.wait = wait
        with patch('vlt.session.codex_rpc._REAP_TIMEOUT', .02, create=True):
            closing = asyncio.create_task(rpc.close())
            try:
                done, _ = await asyncio.wait([closing], timeout=.2)
                self.assertIn(closing, done, 'failed kill must not wait indefinitely')
                with self.assertRaisesRegex(RuntimeError, '^process-cleanup$'):
                    closing.result()
                with self.assertRaisesRegex(RuntimeError, '^auth-cleanup$'):
                    async with self.owner.exclusive():
                        self.fail('uncertain writer must block new owner entries')
            finally:
                release.set()
                await asyncio.gather(closing, return_exceptions=True)

    async def test_untrusted_rpc_error_shapes_do_not_leak_or_kill_reader(self):
        for error in ({'code': 'private-secret'}, 'private-secret', [], {'code': True}):
            rpc = CodexRPC(lambda _: None)
            reader = asyncio.StreamReader()
            rpc.process = SimpleNamespace(stdout=reader)
            rpc.pending[1] = asyncio.get_running_loop().create_future()
            rpc.pending[2] = asyncio.get_running_loop().create_future()
            reader.feed_data((json.dumps({'id': 1, 'error': error}) + '\n' +
                              json.dumps({'id': 2, 'result': {'ok': True}}) + '\n').encode())
            reader.feed_eof()
            await rpc._read()
            with self.assertRaisesRegex(RuntimeError, 'unknown') as caught:
                await rpc.pending[1]
            self.assertNotIn('private-secret', str(caught.exception))
            self.assertEqual(await rpc.pending[2], {'ok': True})

    async def test_login_and_voice_isolate_credentials_without_mutating_parent(self):
        from vlt import paths
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared = root / 'shared'
            shared.mkdir()
            sentinel = shared / 'auth.json'
            sentinel.write_text('parent credentials', encoding='utf-8')
            inherited = dict(os.environ, CODEX_HOME=str(shared),
                             OPENAI_API_KEY='parent-api', CODEX_API_KEY='parent-codex',
                             CODEX_ACCESS_TOKEN='parent-access')
            process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
            spawn = AsyncMock(return_value=process)
            with patch.dict(os.environ, inherited, clear=True), \
                 patch.object(paths, '_user_data_dir', return_value=root / 'app'), \
                 patch('vlt.session.codex_rpc.codex_command', return_value=['fake-codex']), \
                 patch('asyncio.create_subprocess_exec', spawn):
                await login_chatgpt()
                rpc = CodexRPC(lambda _: None)
                rpc._read = AsyncMock()
                rpc.request = AsyncMock()
                rpc._send = AsyncMock()
                await rpc.start(str(root))
                await rpc.close()
                self.assertEqual(dict(os.environ), inherited)
            for call in spawn.call_args_list:
                self.assertIn('cli_auth_credentials_store="file"', call.args)
                self.assertIn('forced_login_method="chatgpt"', call.args)
                env = call.kwargs.get('env', {})
                self.assertTrue(Path(env['CODEX_HOME']).samefile(root / 'app' / 'codex'))
                for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'):
                    self.assertNotIn(name, env)
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'parent credentials')
            self.assertTrue((root / 'app' / 'codex').is_dir())

    async def test_timeout_names_the_failed_method_and_clears_pending(self):
        rpc = CodexRPC(lambda _: None)
        rpc._send = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'account/read'):
            await rpc.request('account/read', timeout=0.01)
        self.assertFalse(rpc.pending)

    async def test_server_tools_are_rejected_and_errors_do_not_expose_tokens(self):
        rpc = CodexRPC(lambda _: None)
        reader = asyncio.StreamReader()
        rpc.process = SimpleNamespace(stdout=reader)
        rpc._send = AsyncMock()
        future = asyncio.get_running_loop().create_future()
        rpc.pending[2] = future
        messages = [
            {'id': 1, 'method': 'item/tool/call', 'params': {'command': 'dangerous'}},
            {'id': 2, 'error': {'code': -32000, 'message': 'private token should not appear'}},
        ]
        for message in messages:
            reader.feed_data((json.dumps(message) + '\n').encode())
        reader.feed_eof()
        await rpc._read()
        sent = rpc._send.call_args.args[0]
        self.assertEqual(sent['error']['code'], -32601)
        with self.assertRaisesRegex(RuntimeError, '-32000') as caught:
            await future
        self.assertNotIn('private token', str(caught.exception))

    async def test_eof_rejects_inflight_requests(self):
        events = []
        rpc = CodexRPC(events.append)
        reader = asyncio.StreamReader(); reader.feed_eof()
        rpc.process = SimpleNamespace(stdout=reader)
        future = asyncio.get_running_loop().create_future()
        rpc.pending[1] = future
        await rpc._read()
        with self.assertRaises(ConnectionError):
            await future
        self.assertEqual(events[-1]['method'], 'transport/closed')


    async def test_malformed_lines_do_not_drop_valid_reply_or_tool_denial(self):
        events = []
        rpc = CodexRPC(events.append)
        stream = asyncio.StreamReader()
        rpc.process = SimpleNamespace(stdout=stream)
        rpc._send = AsyncMock()
        future = asyncio.get_running_loop().create_future()
        rpc.pending[1] = future
        stream.feed_data(b'not JSON\n[]\n{"id":[]}\n'
                         b'{"id":2,"method":"tool/execute"}\n'
                         b'{"id":1,"result":{"ok":true}}\n')
        stream.feed_eof()
        await rpc._read()
        self.assertEqual(await future, {'ok': True})
        self.assertEqual(rpc._send.await_args.args[0]['error']['code'], -32601)
        self.assertEqual(events, [{'method': 'transport/closed', 'params': {}}])


if __name__ == '__main__':
    unittest.main()
