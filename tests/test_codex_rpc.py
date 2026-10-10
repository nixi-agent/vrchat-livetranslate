"""JSON-RPC 的有界等待、工具拒絕及隱私邊界。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.codex_rpc import CodexRPC, login_chatgpt


class RPCTests(unittest.IsolatedAsyncioTestCase):
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
                self.assertEqual(env.get('CODEX_HOME'), str(root / 'app' / 'codex'))
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
