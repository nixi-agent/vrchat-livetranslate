"""Security regressions with synthetic tokens and isolated local files only."""
import asyncio
import base64
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vlt import credentials
from vlt.room.client import RoomClient
from vlt.room.model import RoomConfig


class BoundaryTests(unittest.TestCase):
    def test_only_loopback_may_use_plaintext(self):
        for host in ('example.invalid', '192.168.1.1', 'localhost.evil.invalid'):
            self.assertIsNotNone(RoomConfig(server_url=f'ws://{host}/ws',
                                           room_code='ABCD1234').unavailable_reason())
        for host in ('127.0.0.1', '127.0.0.2', '[::1]', 'localhost'):
            self.assertIsNone(RoomConfig(server_url=f'ws://{host}/ws',
                                        room_code='ABCD1234').unavailable_reason())

    def test_legacy_query_token_is_migrated_out_of_url(self):
        cfg = RoomConfig(server_url='wss://example.invalid/ws?room=ABCD1234&k=synthetic-token&x=1',
                         room_code='ABCD1234')
        client = RoomClient(cfg, lambda _: None)
        self.assertEqual(client._connect_url(), 'wss://example.invalid/ws?room=ABCD1234&x=1')
        self.assertEqual(client._auth_token(), 'synthetic-token')

    def test_legacy_query_credentials_are_absent_from_startup_logs_and_status(self):
        token = 'synthetic-startup-credential'
        for key in ('k', 'tok', 'token', '%6b', '%74ok', '%74oken'):
            with self.subTest(key=key):
                cfg = RoomConfig(server_url=f'wss://example.invalid/ws?{key}={token}&x=1',
                                 room_code='ABCD1234', nickname='測試')
                status = []
                client = RoomClient(cfg, lambda _: None, status.append)
                captured = io.StringIO()
                with patch('vlt.room.client.threading.Thread'), \
                     patch.object(client._ready, 'wait', return_value=True), \
                     contextlib.redirect_stdout(captured):
                    client.start()
                self.assertNotIn(token, captured.getvalue())
                self.assertNotIn(token, '\n'.join(status))
                self.assertIn('example.invalid/ws', captured.getvalue())
                self.assertIn('x=1', captured.getvalue())
                self.assertEqual(client._auth_token(), token)
                self.assertNotIn(token, client._connect_url())

    def test_invalid_endpoint_does_not_echo_credentials_in_error_state(self):
        token = 'synthetic-invalid-endpoint-credential'
        cfg = RoomConfig(server_url=f'https://example.invalid/ws?token={token}',
                         room_code='ABCD1234')
        status = []
        client = RoomClient(cfg, lambda _: None, status.append)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            client.start()
        self.assertNotIn(token, cfg.unavailable_reason())
        self.assertNotIn(token, client.state().last_error)
        self.assertNotIn(token, captured.getvalue())
        self.assertNotIn(token, '\n'.join(status))
        self.assertIn('ws://', cfg.unavailable_reason())

    def test_invalid_unicode_endpoint_is_rejected_without_startup_exception(self):
        self.assertIsNone(RoomConfig(server_url='wss://example.invalid/路徑?名稱=測試日本語😀',
                                     room_code='ABCD1234').unavailable_reason())
        cfg = RoomConfig(server_url='wss://example.invalid/ws?x=' + chr(0xD800),
                         room_code='ABCD1234')
        client = RoomClient(cfg, lambda _: None)
        with patch('vlt.room.client.threading.Thread') as thread, \
             contextlib.redirect_stdout(io.StringIO()):
            client.start()
        thread.assert_not_called()
        self.assertEqual(cfg.unavailable_reason(), 'server_url 格式无效')
        self.assertEqual(client.state().last_error, cfg.unavailable_reason())

    def test_scanner_blocks_secret_without_printing_it(self):
        spec = importlib.util.spec_from_file_location('scanner', ROOT/'scripts/check_no_secrets.py')
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        fake = 'sk-' + 'A'*32
        captured = io.StringIO()
        with patch.object(scanner, 'staged_files', return_value=['synthetic.txt']), \
             patch('builtins.open', mock_open(read_data=fake.encode())), \
             patch.object(sys, 'argv', ['scanner']), contextlib.redirect_stdout(captured):
            self.assertEqual(scanner.main(), 1)
        self.assertNotIn(fake, captured.getvalue())
        self.assertNotIn('A'*8, captured.getvalue())
        self.assertIn('synthetic.txt', captured.getvalue())

    def test_failed_key_replacement_preserves_old_key_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            key = storage/'api_key.txt'
            key.write_text('old-synthetic-key', encoding='utf-8')
            with patch.object(credentials, '_storage_dir_override', lambda: storage), \
                 patch.object(credentials.os, 'replace', side_effect=OSError('replacement failed')):
                with self.assertRaises(OSError):
                    credentials.save_api_key('new-synthetic-key')
            self.assertEqual(key.read_text(encoding='utf-8'), 'old-synthetic-key')
            self.assertEqual(list(storage.iterdir()), [key])

    def test_release_action_is_pinned_and_only_publisher_can_write(self):
        workflow = yaml.safe_load((ROOT/'.github/workflows/release.yml').read_text(encoding='utf-8'))
        self.assertEqual(workflow['permissions']['contents'], 'read')
        jobs = workflow['jobs']
        self.assertEqual(jobs['release']['permissions']['contents'], 'write')
        self.assertNotEqual(jobs['build'].get('permissions', {}).get('contents'), 'write')
        action = next(s['uses'] for s in jobs['release']['steps']
                      if s.get('uses', '').startswith('softprops/action-gh-release@'))
        self.assertRegex(action, r'@[0-9a-f]{40}$')


class HeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_uses_header_and_redirects_are_disabled(self):
        client = RoomClient(RoomConfig(server_url='wss://example.invalid/ws', token='synthetic-token'),
                            lambda _: None)
        connection = asyncio.get_running_loop().create_future()
        connection.set_result(None)
        connection.process_redirect = Mock()
        with patch('websockets.connect', return_value=connection) as connect:
            await client._open_ws(client._connect_url())
        self.assertEqual(connect.call_args.kwargs['additional_headers'],
                         {'Authorization': 'Bearer synthetic-token'})
        error = RuntimeError('redirect')
        self.assertIs(connection.process_redirect(error), error)

    async def test_unicode_tokens_roundtrip_through_ascii_header(self):
        token = '合成令牌 テスト'
        client = RoomClient(RoomConfig(server_url='wss://example.invalid/ws', token=token), lambda _: None)
        connection = asyncio.get_running_loop().create_future()
        connection.set_result(None)
        with patch('websockets.connect', return_value=connection) as connect:
            await client._open_ws(client._connect_url())
        header = connect.call_args.kwargs['additional_headers']['Authorization']
        self.assertTrue(header.isascii())
        self.assertEqual(header.split()[0], 'VLT')
        self.assertEqual(base64.urlsafe_b64decode(header.split()[1]).decode('utf-8'), token)


if __name__ == '__main__':
    unittest.main()
