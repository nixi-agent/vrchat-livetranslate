"""工具專屬憑證 owner；跨 GUI/Engine event loop 共用，不讀全域 Codex。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import paths
from .chatgpt_http import request


def auth_home():
    try:
        home = paths._user_data_dir() / 'codex'
        if not home.is_absolute() or '..' in home.parts:
            raise ValueError()
        canonical = home.resolve()
        if os.path.normcase(str(home)) != os.path.normcase(str(canonical)):
            anchor = next(part for part in (home, *home.parents) if part.exists())
            resolved_anchor = anchor.resolve()
            if (not anchor.samefile(resolved_anchor) or os.path.normcase(str(canonical)) !=
                    os.path.normcase(str(resolved_anchor / home.relative_to(anchor)))):
                raise ValueError()
        # MSIX AppData 虛擬化不是 reparse；原路徑與實體路徑仍須逐層拒絕連結。
        for candidate in (home, canonical):
            for part in (candidate, *candidate.parents):
                if part.exists() or part.is_symlink():
                    info = part.lstat()
                    if (stat.S_ISLNK(info.st_mode) or
                            getattr(info, 'st_file_attributes', 0) & 0x400):
                        raise ValueError()
        return canonical
    except Exception:
        raise RuntimeError('auth-home') from None


def prepare_home():
    home = auth_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = auth_home()
    if os.name == 'posix':
        if home.stat().st_uid != os.getuid():
            raise RuntimeError('auth-home')
        home.chmod(0o700)
    file = home / 'auth.json'
    if file.exists() or file.is_symlink():
        info = file.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or (os.name == 'posix' and info.st_uid != os.getuid())):
            raise RuntimeError('auth-home')
        if os.name == 'posix':
            file.chmod(0o600)
    return home


@dataclass(frozen=True)
class Credentials:
    account: str = field(repr=False)
    access: str = field(repr=False)
    generation: int


def refresh_tokens(refresh):
    body = json.dumps({'client_id': 'app_EMoamEEZ73f0CkXaXp7hrann',
                       'grant_type': 'refresh_token', 'refresh_token': refresh}).encode()
    try:
        value = json.loads(request('refresh', body, {'Content-Type': 'application/json'}))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception:
        raise RuntimeError('auth-refresh') from None


def _token(value):
    return isinstance(value, str) and 0 < len(value) <= 16384 and not any(c.isspace() for c in value)


def _account_from_id(value):
    try:
        claim = value.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(claim + '=' * (-len(claim) % 4)))
        return payload['https://api.openai.com/auth']['chatgpt_account_id']
    except Exception:
        raise RuntimeError('auth-invalid') from None


class AuthOwner:
    def __init__(self):
        self._lock = threading.Lock()
        self._commit_lock = threading.Lock()
        self.generation = 0
        self._account = None
        self._uncertain_access = None
        self._uncertain_writer = False

    def quarantine(self):
        # 與短暫的檔案提交同步；不等待持鎖 HTTP future，避免跨 loop 死鎖。
        with self._commit_lock:
            self._uncertain_writer = True
            self.generation += 1

    def require_usable(self):
        if self._uncertain_writer:
            raise RuntimeError('auth-cleanup')

    async def credentials(self):
        async with self.exclusive():
            _, tokens = self._load()
            return self._snapshot(tokens)

    async def renew(self, credentials):
        # OAuth 可能已接受更新。即使呼叫端取消，仍完成寫入並持有 owner 鎖。
        task = asyncio.create_task(self._renew(credentials))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            task.exception() if not task.cancelled() else None
            raise asyncio.CancelledError()
        return task.result()

    async def _renew(self, credentials):
        async with self.exclusive():
            data, tokens = self._load()
            if (not isinstance(credentials, Credentials) or credentials.generation != self.generation
                    or credentials.account != tokens['account_id']):
                raise RuntimeError('auth-stale')
            rejected_access = credentials.access
            if tokens['access_token'] != rejected_access:
                return self._snapshot(tokens)
            if self._uncertain_access == rejected_access:
                raise RuntimeError('auth-refresh-uncertain')
            self._uncertain_access = rejected_access
            try:
                refreshed = await asyncio.to_thread(refresh_tokens, tokens['refresh_token'])
                if not isinstance(refreshed, dict) or not _token(refreshed.get('access_token')):
                    raise RuntimeError('auth-refresh')
                updated = dict(tokens)
                for name in ('access_token', 'refresh_token', 'id_token'):
                    if name in refreshed:
                        if not _token(refreshed[name]):
                            raise RuntimeError('auth-refresh')
                        updated[name] = refreshed[name]
                if ('id_token' in refreshed and _account_from_id(updated['id_token']) != tokens['account_id']):
                    self.generation += 1
                    raise RuntimeError('auth-account-changed')
                if refreshed.get('account_id', tokens['account_id']) != tokens['account_id']:
                    self.generation += 1
                    raise RuntimeError('auth-account-changed')
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError('auth-refresh') from None
            with self._commit_lock:
                self.require_usable()
                if credentials.generation != self.generation:
                    raise RuntimeError('auth-stale')
                # 拒絕外部 CLI/其他 owner 在更新期間替換的檔案，不覆蓋新登入。
                current, _ = self._load()
                if current != data:
                    self.generation += 1
                    raise RuntimeError('auth-account-changed')
                data['tokens'] = updated
                data['last_refresh'] = datetime.now(timezone.utc).isoformat()
                self._save(data)
                self._uncertain_access = None
                return self._snapshot(updated)

    def _load(self):
        try:
            home = prepare_home()
            file = home / 'auth.json'
            if home.is_symlink() or file.is_symlink():
                raise ValueError()
            with file.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            data = json.loads(raw)
            tokens = data.get('tokens')
            if (data.get('auth_mode') not in (None, 'chatgpt') or data.get('OPENAI_API_KEY')
                    or not isinstance(tokens, dict)
                    or not all(_token(tokens.get(k)) for k in
                               ('account_id', 'access_token', 'refresh_token', 'id_token'))):
                raise ValueError()
            if self._account is not None and self._account != tokens['account_id']:
                self.generation += 1
            self._account = tokens['account_id']
            return data, tokens
        except Exception:
            self.generation += 1
            self._account = None
            raise RuntimeError('auth-invalid') from None

    def _snapshot(self, tokens):
        return Credentials(tokens['account_id'], tokens['access_token'], self.generation)

    def _save(self, data):
        temporary = None
        try:
            home = prepare_home()
            if home.is_symlink() or (home / 'auth.json').is_symlink():
                raise ValueError()
            if os.name == 'posix':
                home.chmod(0o700)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=home, delete=False) as stream:
                temporary = Path(stream.name)
                if os.name == 'posix':
                    temporary.chmod(0o600)
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, home / 'auth.json')
        except Exception:
            self.generation += 1
            raise RuntimeError('auth-write') from None
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    @asynccontextmanager
    async def exclusive(self, *, login=False, cleanup=False):
        # 不把阻塞 acquire 丟給背景 thread：取消後它仍可能取鎖而永遠不放。
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.02)
        try:
            if not cleanup:
                self.require_usable()
            if login:
                self.generation += 1
            yield
        finally:
            self._lock.release()


AUTH_OWNER = AuthOwner()
