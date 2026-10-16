"""只用臨時假憑證驗證 owner，不讀真實登入或呼叫服務。"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session import chatgpt_auth as auth

_REAL_AUTH_HOME = auth.auth_home


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / 'codex'
        self.home.mkdir()
        self.path = self.home / 'auth.json'
        self.data = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None, 'tokens': {
            'account_id': 'test-account', 'id_token': 'test-id',
            'access_token': 'test-access', 'refresh_token': 'test-refresh'}}
        self.write()
        self.owner = auth.AuthOwner()
        override = patch.object(auth, 'auth_home', return_value=self.home)
        override.start()
        self.addCleanup(override.stop)

    def write(self):
        self.path.write_text(json.dumps(self.data), encoding='utf-8')

    def test_application_namespace_redirection_uses_checked_canonical_home(self):
        logical = self.home.parent / 'logical-app-data' / 'codex'
        logical.mkdir(parents=True)
        canonical = self.home
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            return canonical if path == logical else original(path, *args, **kwargs)

        # MSIX can redirect AppData without marking logical ancestors as reparse points.
        with patch.object(auth.paths, '_user_data_dir', return_value=logical.parent), \
                patch.object(Path, 'resolve', resolve), patch.object(Path, 'samefile', return_value=True):
            self.assertEqual(_REAL_AUTH_HOME(), canonical)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), self.data)

    def test_redirected_canonical_ancestor_reparse_is_rejected(self):
        logical = self.home.parent / 'logical-app-data' / 'codex'
        logical.mkdir(parents=True)
        original_resolve, original_stat = Path.resolve, Path.lstat

        def resolve(path, *args, **kwargs):
            return self.home if path == logical else original_resolve(path, *args, **kwargs)

        def lstat(path, *args, **kwargs):
            if path == self.home.parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_stat(path, *args, **kwargs)

        with patch.object(auth.paths, '_user_data_dir', return_value=logical.parent), \
                patch.object(Path, 'resolve', resolve), patch.object(Path, 'lstat', lstat), \
                patch.object(Path, 'samefile', return_value=True):
            with self.assertRaisesRegex(RuntimeError, '^auth-home$'):
                _REAL_AUTH_HOME()

    def test_parent_traversal_in_application_home_is_rejected(self):
        with patch.object(auth.paths, '_user_data_dir', return_value=self.home / '..'):
            with self.assertRaisesRegex(RuntimeError, '^auth-home$'):
                _REAL_AUTH_HOME()

    async def test_hardlinked_credentials_are_rejected_before_read_or_chmod(self):
        external = self.home / 'fake-global-auth.json'
        self.path.rename(external)
        os.link(external, self.path)
        before = external.read_bytes(), external.stat().st_mode
        with self.assertRaisesRegex(RuntimeError, '^auth-invalid$'):
            await self.owner.credentials()
        self.assertEqual((external.read_bytes(), external.stat().st_mode), before)

    async def test_snapshot_reads_only_tool_store_and_hides_secrets(self):
        result = await self.owner.credentials()
        self.assertIsNotNone(result, 'owner must return an authenticated subscription snapshot')
        self.assertEqual(result.account, 'test-account')
        self.assertEqual(result.access, 'test-access')
        self.assertNotIn('test-access', repr(result))
        self.assertNotIn('test-refresh', repr(result))

    async def test_api_auth_and_bad_schema_fail_closed_without_leaking(self):
        for data in ({'auth_mode': 'apikey', 'OPENAI_API_KEY': 'private-secret'},
                     {'auth_mode': 'chatgpt', 'tokens': []},
                     {'auth_mode': 'chatgpt', 'tokens': {'access_token': 'secret\nheader'}}):
            self.path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, '^auth-invalid$'):
                await self.owner.credentials()

    async def test_account_change_invalidates_prior_snapshot(self):
        first = await self.owner.credentials()
        self.data['tokens']['account_id'] = 'other-account'
        self.write()
        second = await self.owner.credentials()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreater(second.generation, first.generation)

    async def test_concurrent_401s_refresh_once_and_preserve_schema(self):
        # Mock only the fixed OAuth operation; assertions never expose actual credentials.
        refreshed = {'access_token': 'next-access', 'refresh_token': 'next-refresh'}
        with patch.object(auth, 'refresh_tokens', return_value=refreshed, create=True) as refresh:
            credentials = await self.owner.credentials()
            results = await asyncio.gather(self.owner.renew(credentials),
                                           self.owner.renew(credentials))
        self.assertTrue(all(r is not None for r in results), 'refresh must return usable credentials')
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual([r.access for r in results], ['next-access'] * 2)
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['tokens']['refresh_token'], 'next-refresh')
        self.assertEqual(saved['tokens']['account_id'], 'test-account')
        self.assertEqual(saved['auth_mode'], 'chatgpt')
        if os.name == 'posix':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)

    async def test_failed_atomic_replace_retains_previous_credentials_and_cleans_temp(self):
        before = self.path.read_bytes()
        with patch.object(auth, 'refresh_tokens', return_value={'access_token': 'next'}, create=True), \
             patch('os.replace', side_effect=OSError('private-secret')):
            with self.assertRaisesRegex(RuntimeError, '^auth-write$'):
                await self.owner.renew(await self.owner.credentials())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.home.iterdir()), [self.path])

    async def test_cancelled_waiter_does_not_steal_lock(self):
        async with self.owner.exclusive():
            waiter = asyncio.create_task(self.owner.credentials())
            await asyncio.sleep(0.03)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        self.assertIsNotNone(await asyncio.wait_for(self.owner.credentials(), 0.5))

    async def test_login_invalidates_even_when_cancelled(self):
        before = self.owner.generation
        async with self.owner.exclusive(login=True):
            self.assertGreater(self.owner.generation, before)

    async def test_ambiguous_refresh_is_not_sent_again(self):
        with patch.object(auth, 'refresh_tokens', side_effect=RuntimeError('auth-refresh')) as refresh:
            credentials = await self.owner.credentials()
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await self.owner.renew(credentials)
        self.assertEqual(refresh.call_count, 1, 'unknown OAuth result must not reuse a rotated refresh token')

    async def test_cancelled_refresh_persists_before_next_login(self):
        entered, release = threading.Event(), threading.Event()

        def refresh(_):
            entered.set()
            release.wait(2)
            return {'access_token': 'next-access', 'refresh_token': 'next-refresh'}

        with patch.object(auth, 'refresh_tokens', side_effect=refresh):
            task = asyncio.create_task(self.owner.renew(await self.owner.credentials()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            task.cancel()
            waiter = asyncio.create_task(self.owner.credentials())
            try:
                await asyncio.sleep(0.04)
                self.assertFalse(waiter.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual((await waiter).access, 'next-access')

    async def test_quarantine_invalidates_pending_refresh_before_atomic_commit(self):
        entered, release = threading.Event(), threading.Event()
        before = self.path.read_bytes()

        def refresh(_):
            entered.set()
            release.wait(2)
            return {'access_token': 'next-access'}

        with patch.object(auth, 'refresh_tokens', side_effect=refresh):
            task = asyncio.create_task(self.owner.renew(await self.owner.credentials()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.owner.quarantine()
            release.set()
            with self.assertRaisesRegex(RuntimeError, '^auth-cleanup$'):
                await task
        self.assertEqual(self.path.read_bytes(), before, 'quarantined refresh must never replace credentials')

    async def test_quarantine_and_atomic_commit_are_ordered_across_threads(self):
        committing, quarantining, done = threading.Event(), threading.Event(), threading.Event()
        save = self.owner._save

        def quarantine():
            if committing.wait(2):
                quarantining.set()
                self.owner.quarantine()
                done.set()

        def commit(data):
            committing.set()
            self.assertTrue(quarantining.wait(1))
            self.assertFalse(done.wait(.04), 'quarantine cannot complete before the in-progress commit')
            save(data)

        worker = threading.Thread(target=quarantine)
        worker.start()
        try:
            with patch.object(auth, 'refresh_tokens', return_value={'access_token': 'next-access'}), \
                 patch.object(self.owner, '_save', side_effect=commit):
                await self.owner.renew(await self.owner.credentials())
        finally:
            committing.set()
            await asyncio.to_thread(worker.join, 2)
        self.assertTrue(done.is_set())
        with self.assertRaisesRegex(RuntimeError, '^auth-cleanup$'):
            await self.owner.credentials()

    async def test_old_account_or_login_generation_cannot_refresh_or_get_new_account(self):
        before = await self.owner.credentials()
        async with self.owner.exclusive(login=True):
            pass
        with patch.object(auth, 'refresh_tokens', return_value={'access_token': 'new-access'}) as refresh:
            with self.assertRaisesRegex(RuntimeError, '^auth-stale$'):
                await self.owner.renew(before)
            refresh.assert_not_called()
        before = await self.owner.credentials()
        self.data['tokens'].update(account_id='other-account', access_token='other-access')
        self.write()
        with patch.object(auth, 'refresh_tokens') as refresh:
            with self.assertRaisesRegex(RuntimeError, '^auth-stale$'):
                await self.owner.renew(before)
            refresh.assert_not_called()

    async def test_owner_is_exclusive_across_real_threads_and_event_loops(self):
        entered, release = threading.Event(), threading.Event()

        async def hold():
            async with self.owner.exclusive():
                entered.set()
                await asyncio.to_thread(release.wait, 2)

        worker = threading.Thread(target=lambda: asyncio.run(hold()))
        worker.start()
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            waiter = asyncio.create_task(self.owner.credentials())
            await asyncio.sleep(0.04)
            self.assertFalse(waiter.done())
            release.set()
            self.assertIsNotNone(await waiter)
        finally:
            release.set()
            await asyncio.to_thread(worker.join, 2)
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
