"""Connection handoff preserves the current owner; fake services, no devices."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_chatgpt_revocation as revocation
from test_repeat_guard import _cfg
from vlt.engine import Engine, EngineEvents


class ConnectionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    setUp = revocation.RevocationTests.setUp
    feed = revocation.RevocationTests.feed
    engine = revocation.RevocationTests.engine

    async def test_initial_run_waits_for_rebuild_then_keeps_capturing_until_stop(self):
        cfg = _cfg(provider='chatgpt')
        cfg.direction('mine').target_lang = 'ja'
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents(on_status=Mock()))
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        feeding, finish_feed = asyncio.Event(), asyncio.Event()
        calls = 0
        async def credentials():
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            return self.owner.credentials.return_value
        async def capture():
            feeding.set()
            await finish_feed.wait()
        self.owner.credentials.side_effect = credentials
        engine._feed_audio = AsyncMock(side_effect=capture)
        initial = asyncio.create_task(engine._async_run())
        rebuild = None
        try:
            await asyncio.wait_for(entered[0].wait(), 1)
            rebuild = asyncio.create_task(engine._rebuild_session())
            await asyncio.wait_for(entered[1].wait(), 1)
            new = engine._session
            release[0].set()
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(initial.done(), 'capture owner must wait for the successor')
            self.assertFalse(new._closing, 'old initial cleanup must not close the successor')
            engine._feed_audio.assert_not_awaited()
            release[1].set()
            await asyncio.wait_for(feeding.wait(), 1)
            self.assertIs(engine._session, new)
            self.assertTrue(new.is_alive)
            engine.request_stop()
            finish_feed.set()
            await asyncio.wait_for(initial, 1)
            self.assertFalse(new.is_alive)
        finally:
            engine.request_stop()
            for event in release:
                event.set()
            finish_feed.set()
            await asyncio.gather(initial, *([rebuild] if rebuild else []), return_exceptions=True)
            self.owner.credentials.side_effect = None
            if engine._session is not None:
                await engine._session.close()

    async def test_backoff_does_not_close_or_revoke_rebuilt_owner(self):
        engine = await self.engine()
        engine._events.on_status = Mock()
        engine._cfg.session_base['reconnect_backoff'] = [2]
        waiting, release = asyncio.Event(), asyncio.Event()
        sleep = asyncio.sleep
        async def backoff(seconds):
            if seconds == 2:
                waiting.set()
                await release.wait()
            else:
                await sleep(0)
        with patch('vlt.engine.asyncio.sleep', side_effect=backoff):
            task = asyncio.create_task(engine._reconnect_loop('test'))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                await engine._rebuild_session()
                new, gate = engine._session, engine._admission
                new.close = AsyncMock(wraps=new.close)
                self.owner.credentials.reset_mock()
                release.set()
                await asyncio.wait_for(task, 1)
                new.close.assert_not_awaited()
                self.assertIs(engine._session, new)
                self.assertTrue(gate.valid())
                self.owner.credentials.assert_not_awaited()
                self.assertFalse(any('已重连' in call.args[1]
                                     for call in engine._events.on_status.call_args_list))
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await engine._session.close()

    async def test_watchdog_binds_target_before_reconnect_task_begins(self):
        engine = await self.engine()
        old = engine._session
        old.abort('session-context')
        await old.close()
        engine._dump_diagnostics = Mock()
        await engine._watchdog()
        task = engine._reconnect_task
        await engine._rebuild_session()
        new, gate = engine._session, engine._admission
        new.close = AsyncMock(wraps=new.close)
        self.owner.credentials.reset_mock()
        try:
            with patch('vlt.engine.asyncio.sleep', new=AsyncMock()):
                await asyncio.wait_for(task, 1)
            new.close.assert_not_awaited()
            self.assertIs(engine._session, new)
            self.assertTrue(gate.valid())
            self.owner.credentials.assert_not_awaited()
        finally:
            await new.close()
            await engine._session.close()

    async def test_budget_wait_cannot_replace_a_newer_connection(self):
        engine = await self.engine()
        engine._connect_ts = [time.monotonic()] * 100
        waiting, release = asyncio.Event(), asyncio.Event()
        sleep = asyncio.sleep
        async def budget(seconds):
            if seconds > 1:
                waiting.set()
                await release.wait()
            else:
                await sleep(0)
        cfg = engine._cfg.direction('mine').to_session_config(engine._cfg.session_base)
        with patch('vlt.engine.asyncio.sleep', side_effect=budget):
            task = asyncio.create_task(engine._create_session(cfg))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                engine._connect_ts.clear()
                await engine._rebuild_session()
                new, gate = engine._session, engine._admission
                self.owner.credentials.reset_mock()
                release.set()
                await asyncio.wait_for(task, 1)
                self.assertIs(engine._session, new)
                self.assertTrue(gate.valid())
                self.owner.credentials.assert_not_awaited()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await engine._session.close()

    async def test_active_pump_does_not_revoke_a_pending_successor(self):
        engine = await self.engine()
        entered, release, watched = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def credentials():
            entered.set()
            await release.wait()
            return self.owner.credentials.return_value
        self.owner.credentials.side_effect = credentials
        engine._events.on_status = Mock()
        engine._dump_diagnostics = engine._log_audio_progress = Mock()
        watchdog = engine._watchdog
        async def observe():
            await watchdog()
            watched.set()
        engine._watchdog = observe
        rebuild = asyncio.create_task(engine._rebuild_session())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            new, gate = engine._session, engine._admission
            engine._pump_task = asyncio.create_task(engine._pump_loop())
            await asyncio.wait_for(watched.wait(), 1)
            self.assertTrue(gate.valid(), 'starting successor must retain admission')
            self.assertIsNone(engine._reconnect_task)
            release.set()
            await asyncio.wait_for(rebuild, 1)
            self.assertTrue(new.is_alive)
        finally:
            engine.request_stop()
            release.set()
            if engine._reconnect_task:
                engine._reconnect_task.cancel()
                await asyncio.gather(engine._reconnect_task, return_exceptions=True)
            await asyncio.gather(rebuild, return_exceptions=True)
            await engine._cleanup()

    async def test_qwen_pending_start_is_not_reconnected_by_watchdog(self):
        cfg = _cfg()
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents(on_status=Mock()))
        entered, release = asyncio.Event(), asyncio.Event()
        session = SimpleNamespace(is_alive=False, fail_reason='', close=AsyncMock())
        async def start(**kwargs):
            entered.set()
            await release.wait()
            session.is_alive = True
        session.start = AsyncMock(side_effect=start)
        with patch('vlt.engine.create_session', return_value=session):
            task = asyncio.create_task(engine._create_session(
                cfg.direction('mine').to_session_config(cfg.session_base)))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                engine._dump_diagnostics = Mock()
                await engine._watchdog()
                self.assertIsNone(engine._reconnect_task)
                release.set()
                self.assertTrue(await asyncio.wait_for(task, 1))
                self.assertTrue(session.is_alive)
            finally:
                release.set()
                if engine._reconnect_task:
                    engine._reconnect_task.cancel()
                    await asyncio.gather(engine._reconnect_task, return_exceptions=True)
                await asyncio.gather(task, return_exceptions=True)
                await engine._cleanup()

    async def handoff_failure(self, outcome):
        cfg = _cfg(provider='chatgpt')
        cfg.direction('mine').target_lang = 'ja'
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents(on_status=Mock()))
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0
        async def credentials():
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            if index == 1 and outcome == 'failure':
                raise RuntimeError('fake credentials failure')
            return self.owner.credentials.return_value
        self.owner.credentials.side_effect = credentials
        engine._feed_audio = AsyncMock()
        initial = asyncio.create_task(engine._async_run())
        rebuild = None
        try:
            await asyncio.wait_for(entered[0].wait(), 1)
            rebuild = asyncio.create_task(engine._rebuild_session())
            await asyncio.wait_for(entered[1].wait(), 1)
            new = engine._session
            release[0].set()
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(initial.done())
            if outcome == 'stop':
                engine.request_stop()
                done, _ = await asyncio.wait({initial, rebuild}, timeout=.5)
                self.assertEqual(done, {initial, rebuild}, 'stop must not wait for credentials')
            elif outcome == 'cancel-main':
                initial.cancel()
                done, _ = await asyncio.wait({initial, rebuild}, timeout=.5)
                self.assertEqual(done, {initial, rebuild}, 'cancelled owner must reap startup')
                self.assertTrue(initial.cancelled())
            else:
                if outcome == 'cancel-successor':
                    rebuild.cancel()
                else:
                    release[1].set()
                await asyncio.wait_for(asyncio.gather(initial, rebuild, return_exceptions=True), 1)
                self.assertTrue(any(call.args[0] == 'error' and '运行错误' in call.args[1]
                    for call in engine._events.on_status.call_args_list), 'failed startup must clearly terminate')
            self.assertTrue(new._closing)
            self.assertIsNone(engine._pump_task)
            engine._feed_audio.assert_not_awaited()
        finally:
            engine.request_stop()
            for event in release:
                event.set()
            await asyncio.gather(initial, *([rebuild] if rebuild else []), return_exceptions=True)
            await engine._cleanup()

    async def test_stop_during_handoff_does_not_wait_for_successor_credentials(self):
        await self.handoff_failure('stop')

    async def test_cancelled_main_reaps_the_pending_successor(self):
        await self.handoff_failure('cancel-main')

    async def test_failed_successor_clearly_terminates_initial_startup(self):
        await self.handoff_failure('failure')

    async def test_cancelled_successor_clearly_terminates_initial_startup(self):
        await self.handoff_failure('cancel-successor')

    async def test_stop_during_initial_credentials_is_bounded(self):
        cfg = _cfg(provider='chatgpt')
        cfg.direction('mine').target_lang = 'ja'
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents(on_status=Mock()))
        entered, release = asyncio.Event(), asyncio.Event()
        async def credentials():
            entered.set()
            await release.wait()
            return self.owner.credentials.return_value
        self.owner.credentials.side_effect = credentials
        engine._feed_audio = AsyncMock()
        task = asyncio.create_task(engine._async_run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            engine.request_stop()
            done, _ = await asyncio.wait({task}, timeout=.5)
            self.assertEqual(done, {task}, 'initial credential wait must observe stop')
            engine._feed_audio.assert_not_awaited()
            self.assertTrue(engine._session._closing)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await engine._cleanup()

    async def test_stop_during_initial_rpm_budget_is_bounded(self):
        cfg = _cfg(provider='chatgpt')
        cfg.direction('mine').target_lang = 'ja'
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents(on_status=Mock()))
        engine._connect_ts = [time.monotonic()] * 100
        waiting, release = asyncio.Event(), asyncio.Event()
        sleep = asyncio.sleep
        async def budget(seconds):
            if seconds > 1:
                waiting.set()
                await release.wait()
            else:
                await sleep(seconds)
        engine._feed_audio = AsyncMock()
        with patch('vlt.engine.asyncio.sleep', side_effect=budget):
            task = asyncio.create_task(engine._async_run())
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                engine.request_stop()
                done, _ = await asyncio.wait({task}, timeout=.5)
                self.assertEqual(done, {task}, 'RPM wait must observe stop')
                self.owner.credentials.assert_not_awaited()
                engine._feed_audio.assert_not_awaited()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await engine._cleanup()

    async def test_initial_owner_waits_through_successor_budget_before_a_session_exists(self):
        cfg = _cfg(provider='chatgpt')
        cfg.direction('mine').target_lang = 'ja'
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents(on_status=Mock()))
        first, release_first = asyncio.Event(), asyncio.Event()
        budget_entered, release_budget = asyncio.Event(), asyncio.Event()
        feeding, finish_feed = asyncio.Event(), asyncio.Event()
        calls = 0
        async def credentials():
            nonlocal calls
            calls += 1
            if calls == 1:
                first.set()
                await release_first.wait()
            return self.owner.credentials.return_value
        async def capture():
            feeding.set()
            await finish_feed.wait()
        sleep = asyncio.sleep
        async def budget(seconds):
            if seconds > 1:
                budget_entered.set()
                await release_budget.wait()
            else:
                await sleep(seconds)
        self.owner.credentials.side_effect = credentials
        engine._feed_audio = AsyncMock(side_effect=capture)
        with patch('vlt.engine.asyncio.sleep', side_effect=budget):
            initial = asyncio.create_task(engine._async_run())
            rebuild = None
            try:
                await asyncio.wait_for(first.wait(), 1)
                engine._connect_ts = [time.monotonic()] * 100
                rebuild = asyncio.create_task(engine._rebuild_session())
                await asyncio.wait_for(budget_entered.wait(), 1)
                release_first.set()
                await sleep(.15)
                self.assertFalse(initial.done(), 'RPM-limited successor still owns startup')
                self.assertIsNone(engine._session)
                engine._feed_audio.assert_not_awaited()
                engine._connect_ts.clear()
                release_budget.set()
                await asyncio.wait_for(feeding.wait(), 1)
                self.assertTrue(engine._session.is_alive)
                engine.request_stop()
                finish_feed.set()
                await asyncio.wait_for(initial, 1)
            finally:
                engine.request_stop()
                release_first.set()
                release_budget.set()
                finish_feed.set()
                await asyncio.gather(initial, *([rebuild] if rebuild else []), return_exceptions=True)
                await engine._cleanup()


if __name__ == '__main__':
    unittest.main()
