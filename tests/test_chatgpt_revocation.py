"""Previously approved output must remain revocable at Engine and delayed sinks."""
import asyncio
import queue
import sys
import time
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_chatgpt_validated as validated
from test_repeat_guard import _cfg
from vlt.engine import Engine, EngineEvents
from vlt.gui_chat_context import ChatCtx
from vlt.gui_chat_poll import poll
from vlt.session.chatgpt_output import allowed, current
from vlt.output.virtualmic import VirtualMic


class RevocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        validated.ValidatedTests.setUp(self)
        self.services.translate.side_effect = lambda source, target, words: {
            'translation': '翻訳を続けないでください。' if target == 'ja' else '不要停止翻譯。', 'language': target}
        self.services.validate_text.side_effect = lambda source, text, target: dict(self.verdict, language=target)

    feed = validated.ValidatedTests.feed

    async def engine(self, direction='mine', cfg=None):
        cfg = cfg or _cfg(provider='chatgpt')
        cfg.directions['mine'].target_lang = 'ja'
        cfg.directions['mine'].output_audio = True
        if 'theirs' not in cfg.directions:
            from vlt.config import Direction
            cfg.directions['theirs'] = Direction(source_lang='ja', target_lang='zh')
        engine = Engine(cfg, direction, 'mic', {'chatbox'}, EngineEvents(on_text=Mock()))
        engine._virtualmic, engine._overlay, engine._merger = Mock(), Mock(), Mock()
        await engine._create_session(cfg.directions[direction].to_session_config(cfg.session_base))
        self.addAsyncCleanup(engine._session.close)
        await self.feed(engine._session)
        self.assertEqual(len(engine._session._pipeline._ready[direction]), 1)
        return engine

    def change(self, engine, name, restore=False):
        if name == 'stop':
            engine.request_stop()
        elif name == 'provider':
            engine._cfg.session_base['provider'] = 'qianwen'
            if restore:
                engine._cfg.session_base['provider'] = 'chatgpt'
        elif name == 'language':
            engine._cfg.directions[engine._direction].target_lang = 'en'
            if restore:
                engine._cfg.directions[engine._direction].target_lang = 'ja'
        elif name == 'direction':
            engine._direction = 'theirs'
            if restore:
                engine._direction = 'mine'
        elif name == 'account':
            self.owner.generation += 1

    async def test_approved_ready_results_cannot_escape_after_engine_context_changes(self):
        for name in ('stop', 'provider', 'language', 'direction', 'account'):
            for restore in (False, True):
                with self.subTest(name=name, restore=restore):
                    engine = await self.engine()
                    session = engine._session
                    self.change(engine, name, restore)
                    session.tick()
                    engine._events.on_text.assert_not_called()
                    engine._virtualmic.push.assert_not_called()
                    engine._overlay.update.assert_not_called()
                    engine._merger.push.assert_not_called()

    async def test_delayed_gui_and_audio_payloads_retain_engine_revocation(self):
        for name in ('stop', 'provider', 'language', 'direction', 'account'):
            with self.subTest(name=name):
                engine = await self.engine()
                pending, captured = queue.Queue(), []
                def receive(src, text, final):
                    pending.put(('text', 'mine', src, text, final, current()))
                    captured.append(current())
                engine._events.on_text = receive
                engine._virtualmic.push.side_effect = lambda *_: captured.append(current())
                engine._session.tick()
                self.assertEqual(len(captured), 2)
                self.assertTrue(all(allowed(tag) for tag in captured))
                self.change(engine, name, restore=True)
                self.assertTrue(all(not allowed(tag) for tag in captured))
                ctx = ChatCtx(q=pending)
                add = Mock()
                poll(ctx, Mock(), Mock(), add, Mock(), {})
                add.assert_not_called()

    async def test_actual_audio_queue_discards_revoked_pcm_before_device_callback(self):
        for name in ('stop', 'provider', 'language', 'direction', 'account'):
            with self.subTest(name=name):
                engine = await self.engine()
                vm = engine._virtualmic = VirtualMic(0, 'fake', buffer_ms=0)
                engine._session.tick()
                self.assertGreater(vm._buf_bytes, 0)
                self.change(engine, name, restore=True)
                output = bytearray(80)
                vm._audio_callback(output, 20, None, None)
                self.assertEqual(output, bytes(80))
                self.assertEqual(vm._buf_bytes, 0)

    async def test_language_budget_failure_revokes_without_waiting_for_rebuild(self):
        engine = await self.engine()
        engine._loop = Mock()
        engine._connect_ts *= 100
        with patch('asyncio.run_coroutine_threadsafe') as schedule:
            self.assertFalse(engine.set_languages('zh', 'en'))
            engine.set_languages('zh', 'ja')
            engine._session.tick()
        engine._events.on_text.assert_not_called()
        engine._virtualmic.push.assert_not_called()
        schedule.assert_not_called()

    async def test_revoked_engine_context_also_stops_old_paid_work(self):
        engine = await self.engine()
        self.change(engine, 'language', restore=True)
        engine._session.tick()
        self.assertFalse(engine._session.is_alive)
        self.assertEqual(engine._session.fail_reason, 'session-context')
        self.services.source_asr.reset_mock()
        with self.assertRaises(RuntimeError):
            await engine._session.send_audio(validated.SOURCE)
        self.services.source_asr.assert_not_called()

    async def test_stale_configuration_before_start_is_not_admitted(self):
        cfg = _cfg(provider='chatgpt')
        cfg.directions['mine'].target_lang = 'ja'
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents())
        scfg = cfg.direction('mine').to_session_config(cfg.session_base)
        cfg.direction('mine').target_lang = 'en'
        with self.assertRaisesRegex(RuntimeError, '^session-context$'):
            await engine._create_session(scfg)
        self.addAsyncCleanup(engine._session.close)
        self.assertFalse(engine._session.is_alive)
        self.services.source_asr.assert_not_called()

    async def test_each_pending_stage_observes_engine_revocation_without_tick(self):
        names = ('source_asr', 'translate', 'validate_text', 'synthesize', 'audio_asr', 'validate_audio')
        for index, name in enumerate(names):
            for change in ('stop', 'provider', 'language', 'direction', 'glossary'):
                with self.subTest(stage=name, change=change):
                    engine = await self.engine()
                    session = engine._session
                    session._pipeline._ready['mine'].clear()
                    for service in vars(self.services).values():
                        service.reset_mock()
                    service = getattr(self.services, name)
                    original = service.side_effect
                    began, release = asyncio.Event(), asyncio.Event()
                    async def blocked(*args):
                        began.set()
                        await release.wait()
                        return original(*args) if original else service.return_value
                    service.side_effect = blocked
                    try:
                        self.assertTrue(session._pipeline.submit('mine', validated.SOURCE))
                        await began.wait()
                        self.assertTrue(session._pipeline.submit('mine', validated.SOURCE))
                        if change == 'glossary':
                            words = engine._cfg.session_base.setdefault('glossary', {})
                            words['new'] = 'word'
                            del words['new']
                        else:
                            self.change(engine, change, restore=True)
                        release.set()
                        for _ in range(80):
                            await asyncio.sleep(0)
                        for following in names[index + 1:]:
                            getattr(self.services, following).assert_not_awaited()
                        self.assertEqual(len(session._pipeline._ready['mine']), 0)
                        engine._events.on_text.assert_not_called()
                        engine._virtualmic.push.assert_not_called()
                    finally:
                        release.set()
                        await session.close()
                        service.side_effect = original

    async def test_stale_audio_policy_before_or_during_budget_wait_is_not_started(self):
        for during_wait in (False, True):
            for initial, restore in ((True, False), (False, False), (True, True)):
                with self.subTest(wait=during_wait, initial=initial, restore=restore):
                    cfg = _cfg(provider='chatgpt')
                    direction = cfg.direction('mine')
                    direction.target_lang, direction.output_audio = 'ja', initial
                    scfg = direction.to_session_config(cfg.session_base)
                    engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents())
                    engine._virtualmic = Mock()
                    self.owner.credentials.reset_mock()
                    def change_audio():
                        direction.output_audio = not initial
                        if restore:
                            direction.output_audio = initial
                    if during_wait:
                        engine._connect_ts = [time.monotonic()] * 100
                        async def wait(_):
                            change_audio()
                        with patch('vlt.engine.asyncio.sleep', side_effect=wait):
                            with self.assertRaisesRegex(RuntimeError, '^session-context$'):
                                await engine._create_session(scfg)
                    else:
                        change_audio()
                        with self.assertRaisesRegex(RuntimeError, '^session-context$'):
                            await engine._create_session(scfg)
                    self.addAsyncCleanup(engine._session.close)
                    self.assertFalse(engine._session.is_alive)
                    self.owner.credentials.assert_not_awaited()

    async def test_admission_rejection_reaches_initial_rebuild_and_reconnect_callers(self):
        for operation, late in product(('initial', 'rebuild', 'reconnect'), (False, True)):
            with self.subTest(operation=operation, during_credentials=late):
                cfg = _cfg(provider='chatgpt')
                direction = cfg.direction('mine')
                direction.target_lang, direction.output_audio = 'ja', True
                events = EngineEvents(on_status=Mock())
                engine = Engine(cfg, 'mine', 'mic', set(), events)
                engine._virtualmic, engine._feed_audio = Mock(), AsyncMock()
                engine._connect_ts = [] if late else [time.monotonic()] * 100
                self.owner.credentials.reset_mock()
                def change_policy():
                    direction.output_audio = False
                    direction.output_audio = True
                async def credentials():
                    change_policy()
                    self.owner.credentials.side_effect = None
                    return self.owner.credentials.return_value
                self.owner.credentials.side_effect = credentials if late else None
                async def budget_wait(seconds):
                    if seconds > 1:
                        change_policy()
                        engine._connect_ts.clear()
                with patch('vlt.engine.asyncio.sleep', side_effect=budget_wait):
                    if operation == 'initial':
                        await engine._async_run()
                    elif operation == 'rebuild':
                        await engine._rebuild_session()
                    else:
                        await engine._reconnect_loop('session-context')
                self.addAsyncCleanup(engine._session.close)
                messages = [call.args for call in events.on_status.call_args_list]
                engine._feed_audio.assert_not_awaited()
                if operation == 'reconnect':
                    connected = [msg for level, msg in messages if level == 'info' and '已重连' in msg]
                    self.assertEqual(connected, ['已重连（第 2 次）'])
                    self.assertTrue(engine._session.is_alive)
                    self.assertEqual(self.owner.credentials.await_count, 2 if late else 1)
                else:
                    self.assertTrue(any(level == 'error' and 'session-context' in msg for level, msg in messages))
                    self.assertFalse(any(level == 'info' for level, _ in messages))
                    self.assertEqual(self.owner.credentials.await_count, int(late))
                    self.assertFalse(engine._session.is_alive)

    async def test_stop_during_start_skips_initial_capture_and_reconnect_success(self):
        for operation in ('initial', 'reconnect'):
            with self.subTest(operation=operation):
                cfg = _cfg(provider='chatgpt')
                cfg.direction('mine').target_lang = 'ja'
                events = EngineEvents(on_status=Mock())
                engine = Engine(cfg, 'mine', 'mic', set(), events)
                engine._feed_audio = AsyncMock()
                async def stop_at_start():
                    engine.request_stop()
                    return self.owner.credentials.return_value
                self.owner.credentials.side_effect = stop_at_start
                try:
                    with patch('vlt.engine.asyncio.sleep', new=AsyncMock()):
                        if operation == 'initial':
                            await engine._async_run()
                        else:
                            await engine._reconnect_loop('test')
                finally:
                    self.owner.credentials.side_effect = None
                self.addAsyncCleanup(engine._session.close)
                engine._feed_audio.assert_not_awaited()
                self.assertFalse(any(call.args[0] == 'info' for call in events.on_status.call_args_list))

    async def test_reconnect_yields_to_rebuild_before_new_start_completes(self):
        engine = await self.engine()
        engine._events.on_status = Mock()
        old = engine._session
        old_close = old.close
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        starting, finish_start = asyncio.Event(), asyncio.Event()
        calls = 0
        async def close():
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            await old_close()
        async def credentials():
            starting.set()
            await finish_start.wait()
            return self.owner.credentials.return_value
        old.close = close
        self.owner.credentials.reset_mock()
        self.owner.credentials.side_effect = credentials
        rebuild = asyncio.create_task(engine._rebuild_session())
        await asyncio.wait_for(entered[0].wait(), 1)
        try:
            with patch('vlt.engine.asyncio.sleep', new=AsyncMock()):
                reconnect = asyncio.create_task(engine._reconnect_loop('test'))
                await asyncio.wait_for(entered[1].wait(), 1)
                release[0].set()
                await asyncio.wait_for(starting.wait(), 1)
                new = engine._session
                release[1].set()
                await asyncio.wait_for(reconnect, 1)
                self.assertIs(engine._session, new)
                self.assertFalse(new.is_alive)
                self.assertEqual(self.owner.credentials.await_count, 1)
                self.assertFalse(any('已重连' in call.args[1]
                                     for call in engine._events.on_status.call_args_list))
        finally:
            for event in release:
                event.set()
            finish_start.set()
            await asyncio.gather(rebuild, return_exceptions=True)
            self.owner.credentials.side_effect = None
            old.close = old_close
            self.addAsyncCleanup(engine._session.close)
        self.assertTrue(engine._session.is_alive)

    async def test_superseded_start_does_not_report_success_or_start_capture(self):
        for operation in ('initial', 'rebuild', 'reconnect'):
            with self.subTest(operation=operation):
                cfg = _cfg(provider='chatgpt')
                cfg.direction('mine').target_lang = 'ja'
                events = EngineEvents(on_status=Mock())
                engine = Engine(cfg, 'mine', 'mic', set(), events)
                engine._feed_audio = AsyncMock()
                replacement = Mock(close=AsyncMock())
                owned = []
                async def credentials():
                    owned.append(engine._session)
                    engine._session = replacement
                    return self.owner.credentials.return_value
                self.owner.credentials.side_effect = credentials
                try:
                    with patch('vlt.engine.asyncio.sleep', new=AsyncMock()):
                        if operation == 'initial':
                            await engine._async_run()
                        elif operation == 'rebuild':
                            await engine._rebuild_session()
                        else:
                            await engine._reconnect_loop('test')
                    engine._feed_audio.assert_not_awaited()
                    self.assertFalse(any(call.args[0] == 'info'
                                         for call in events.on_status.call_args_list))
                    self.assertEqual(len(owned), 1)
                    self.assertFalse(owned[0].is_alive)
                finally:
                    self.owner.credentials.side_effect = None
                    for session in owned:
                        await session.close()

    async def test_global_glossary_in_place_restore_does_not_revive_approval(self):
        for install in ('constructor', 'assignment', 'update', 'setdefault'):
            with self.subTest(install=install):
                cfg = _cfg(provider='chatgpt', **({'glossary': {'a': 'b'}} if install == 'constructor' else {}))
                if install == 'assignment':
                    cfg.session_base['glossary'] = {'a': 'b'}
                elif install == 'update':
                    cfg.session_base.update(glossary={'a': 'b'})
                elif install == 'setdefault':
                    cfg.session_base.setdefault('glossary', {'a': 'b'})
                engine = await self.engine(cfg=cfg)
                glossary = cfg.session_base['glossary']
                glossary['a'] = 'changed'
                glossary['a'] = 'b'
                engine._session.tick()
                engine._events.on_text.assert_not_called()
                engine._virtualmic.push.assert_not_called()

    async def test_glossary_equal_replacement_then_mutation_cannot_reuse_old_generation(self):
        engine = await self.engine(cfg=_cfg(provider='chatgpt', glossary={'a': 'b'}))
        engine._cfg.session_base['glossary'] = {'a': 'b'}
        engine._cfg.session_base['glossary']['a'] = 'changed'
        engine._cfg.session_base['glossary']['a'] = 'b'
        engine._session.tick()
        engine._events.on_text.assert_not_called()
        engine._virtualmic.push.assert_not_called()

    async def test_new_factory_watchdog_reconnects_and_revokes_old_approved_output(self):
        engine = await self.engine()
        old = engine._session
        old.tick()
        engine._events.on_text.assert_called_once()
        old_approval = []
        engine._events.on_text.side_effect = lambda *_: old_approval.append(current())
        await self.feed(old)
        old.tick()
        self.assertTrue(allowed(old_approval[0]))
        old.abort('session-context')
        engine._dump_diagnostics = Mock()
        await engine._watchdog()
        await engine._reconnect_task
        self.addAsyncCleanup(engine._session.close)
        self.assertIsNot(engine._session, old)
        self.assertTrue(engine._session.is_alive)
        self.assertFalse(allowed(old_approval[0]))
        engine._dump_diagnostics.assert_called_once_with('session-context')
        engine._events.on_text.reset_mock()
        engine._virtualmic.push.reset_mock()
        await self.feed(engine._session)
        engine._session.tick()
        engine._events.on_text.assert_called_once()
        engine._virtualmic.push.assert_called_once()

    async def test_rebuild_and_reconnect_revoke_before_awaiting_old_close(self):
        for operation in ('_rebuild_session', '_reconnect_session'):
            with self.subTest(operation=operation):
                engine = await self.engine()
                session = engine._session
                began, release = asyncio.Event(), asyncio.Event()
                close = session.close
                async def delayed_close():
                    began.set()
                    await release.wait()
                    await close()
                session.close = delayed_close
                task = asyncio.create_task(getattr(engine, operation)())
                await began.wait()
                session.tick()
                release.set()
                await task
                self.addAsyncCleanup(engine._session.close)
                engine._events.on_text.assert_not_called()
                engine._virtualmic.push.assert_not_called()

    async def test_stop_during_rebuild_does_not_start_another_session(self):
        engine = await self.engine()
        session = engine._session
        began, release = asyncio.Event(), asyncio.Event()
        close = session.close
        async def delayed_close():
            began.set()
            await release.wait()
            await close()
        session.close = delayed_close
        task = asyncio.create_task(engine._rebuild_session())
        await began.wait()
        engine.request_stop()
        release.set()
        with patch('vlt.engine.create_session') as create:
            await task
        create.assert_not_called()

    async def test_stop_inside_text_callback_prevents_later_output_effects(self):
        engine = await self.engine()
        engine._events.on_text.side_effect = lambda *_: engine.request_stop()
        engine._session.tick()
        engine._events.on_text.assert_called_once()
        engine._overlay.update.assert_not_called()
        engine._merger.push.assert_not_called()
        engine._virtualmic.push.assert_not_called()

    async def test_one_direction_rebuild_does_not_revoke_the_other(self):
        mine = await self.engine()
        theirs = await self.engine('theirs', mine._cfg)
        await mine._rebuild_session()
        self.addAsyncCleanup(mine._session.close)
        theirs._session.tick()
        theirs._events.on_text.assert_called_once()
        theirs._virtualmic.push.assert_not_called()


if __name__ == '__main__':
    unittest.main()
