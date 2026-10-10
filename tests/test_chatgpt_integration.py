"""ChatGPT 訂閱路徑：設定、工廠與 GUI 不可要求或轉送 Qwen API key。"""
import sys
import asyncio
import contextlib
import io
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.config import load_config
from vlt.session.base import SessionConfig, create_session
from vlt import gui_engine, ui_state
from vlt.engine import Engine, EngineEvents
from vlt import crashlog
from vlt.session.chatgpt_live import ChatGPTLiveSession


class IntegrationTests(unittest.TestCase):
    def test_watchdog_reconnects_failed_subscription_and_preserves_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.yaml'
            path.write_text('session:\n  provider: chatgpt\n', encoding='utf-8')
            cfg = load_config(path)
        engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents())
        session = ChatGPTLiveSession(SessionConfig(provider='chatgpt'))
        engine._session = session
        engine._dump_diagnostics = Mock()
        from unittest.mock import AsyncMock
        engine._reconnect_loop = AsyncMock()
        async def check():
            session._alive = True
            await engine._watchdog()
            self.assertIsNone(engine._reconnect_task)
            session._fail('bridge failed')
            await engine._watchdog()
            self.assertIsNotNone(engine._reconnect_task, 'dead ChatGPT session never reconnects')
            await engine._reconnect_task
        asyncio.run(check())
        engine._dump_diagnostics.assert_called_once_with('bridge failed')
        engine._reconnect_loop.assert_awaited_once_with('bridge failed')

    def test_engine_subscription_never_derives_or_calls_qwen_http(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.yaml'
            path.write_text('session:\n  provider: chatgpt\n', encoding='utf-8')
            cfg = load_config(path)
        with patch('vlt.endpoints.chat_url') as chat, patch('vlt.endpoints.multimodal_url') as tts:
            engine = Engine(cfg, 'mine', 'mic', set(), EngineEvents())
        chat.assert_not_called()
        tts.assert_not_called()
        self.assertIsNone(engine._chat_endpoint)
        self.assertIsNone(engine._tts_endpoint)
        engine._loop, engine._stopping = Mock(), False
        engine._thread = SimpleNamespace(is_alive=lambda: True)
        with patch('asyncio.run_coroutine_threadsafe') as schedule, patch('vlt.engine.translate_text') as translate:
            self.assertFalse(engine.send_text('hello'))
            asyncio.run(engine._async_send_text('hello'))
        schedule.assert_not_called()
        translate.assert_not_called()

    def test_startup_key_diagnostic_only_reads_the_selected_qwen_slot(self):
        for provider in ('chatgpt', 'qianwen', 'qwencloud'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'config.yaml'
                path.write_text('session:\n  provider: ' + provider + '\n', encoding='utf-8')
                output = io.StringIO()
                with patch('vlt.config.DEFAULT_CONFIG', path), patch('vlt.config.load_api_key', return_value='sk-test-only-123456789') as loader, \
                     patch('subprocess.run', return_value=SimpleNamespace(stdout='')), contextlib.redirect_stdout(output):
                    crashlog.log_startup_info('test')
                if provider == 'chatgpt':
                    loader.assert_not_called()
                else:
                    loader.assert_called_once_with(slot=provider)
                    self.assertIn('API 密钥：sk-****6789', output.getvalue())
                self.assertNotIn('sk-test-only-123456789', output.getvalue())

    def test_subscription_config_never_loads_api_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.yaml'
            path.write_text('session:\n  provider: chatgpt\ndirections:\n  mine:\n    source_lang: zh\n    target_lang: ja\n', encoding='utf-8')
            with patch('vlt.config.load_api_key', return_value='qwen-secret') as key_loader:
                cfg = load_config(path, require_key=True)
            self.assertEqual(cfg.session_base['api_key'], '')
            self.assertEqual(cfg.session_base['provider'], 'chatgpt')
            key_loader.assert_not_called()
            scfg = cfg.direction('mine').to_session_config(cfg.session_base)
            self.assertEqual(scfg.target_lang, 'ja')
            self.assertEqual(type(create_session(scfg)).__name__, 'ChatGPTLiveSession')

    def test_factory_uses_subscription_provider(self):
        cfg = SessionConfig(provider='chatgpt', target_lang='ja')
        self.assertEqual(type(create_session(cfg)).__name__, 'ChatGPTLiveSession')

    def test_refresh_clears_api_key_without_reading_credentials(self):
        cfg = SimpleNamespace(session_base={'provider': 'chatgpt', 'api_key': 'qwen-secret'})
        with patch('vlt.config.load_api_key') as loader:
            ui_state.refresh_api_key_in_cfg(cfg)
        self.assertEqual(cfg.session_base['api_key'], '')
        loader.assert_not_called()

    def test_gui_subscription_starts_without_key_and_disables_typed_qwen(self):
        variable = lambda value: SimpleNamespace(get=lambda: value)
        ctx = gui_engine.EngineCtx(
            cfg=SimpleNamespace(session_base={'provider': 'chatgpt', 'api_key': ''}, directions={}, output={}),
            direction_var=variable('mine'), chatbox_var=variable(True),
            lang_pair={'source': 'zh', 'target': 'ja'}, root=SimpleNamespace(after=lambda *_: None),
            set_text_input_enabled_fn=lambda value: typed.append(value),
        )
        typed = []
        with patch.object(gui_engine, 'start_engine') as start_engine, \
             patch.object(gui_engine, 'start_overlay'), patch.object(gui_engine, 'start_desktop'):
            self.assertFalse(gui_engine.start(ctx))
            start_engine.assert_called_once_with(ctx, 0)
        self.assertEqual(typed, [False])

    def test_subscription_dual_starts_two_directions_with_separate_outputs(self):
        variable = lambda value: SimpleNamespace(get=lambda: value)
        messages, typed = [], []
        root = Mock()
        ctx = gui_engine.EngineCtx(
            cfg=SimpleNamespace(session_base={'provider': 'chatgpt', 'api_key': ''}, directions={}, output={}),
            direction_var=variable('dual'), chatbox_var=variable(True), vmic_var=variable(True),
            lang_pair={'source': 'zh', 'target': 'ja'}, root=root, q=queue.Queue(),
            on_engine_text_fn=lambda *args: messages.append(args),
            set_text_input_enabled_fn=typed.append,
        )
        with patch.object(gui_engine, 'Engine') as engine, \
             patch.object(gui_engine, 'start_overlay'), patch.object(gui_engine, 'start_desktop'):
            self.assertFalse(gui_engine.start(ctx))
            self.assertEqual(engine.call_count, 1, 'first direction never starts')
            root.after.call_args.args[1]()  # 既有 300ms 排程啟動第二個方向。
            self.assertEqual(engine.call_count, 2)
        self.assertEqual(ctx.engine_dirs, ['mine', 'theirs'])
        self.assertEqual(ctx.pending_starts, 0)
        mine, theirs = (call.kwargs for call in engine.call_args_list)
        self.assertEqual((mine['source'], theirs['source']), ('mic', 'loopback'))
        self.assertEqual(mine['sinks'], {'chatbox'})
        self.assertEqual(theirs['sinks'], set())
        self.assertEqual((ctx.cfg.directions['mine'].source_lang, ctx.cfg.directions['mine'].target_lang), ('zh', 'ja'))
        self.assertEqual((ctx.cfg.directions['theirs'].source_lang, ctx.cfg.directions['theirs'].target_lang), ('ja', 'zh'))
        self.assertTrue(ctx.cfg.directions['mine'].output_audio)
        self.assertFalse(ctx.cfg.directions['theirs'].output_audio)
        mine['events'].on_text('中文', '日本語', True)
        theirs['events'].on_text('日本語', '繁體中文', True)
        self.assertEqual([entry[:2] for entry in messages], [('mine', 'mic'), ('theirs', 'loopback')])
        self.assertEqual(typed, [False])


if __name__ == '__main__':
    unittest.main()
