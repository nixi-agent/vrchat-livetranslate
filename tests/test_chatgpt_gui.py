"""實際 Tk 控制項切換訂閱／Qwen；不開啟麥克風或登入。"""
import sys
import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _cfgbox import sandbox_config
sandbox_config(reset=True)
from vlt import gui as gui_mod, crashlog, i18n


class GuiTests(unittest.TestCase):
    def test_provider_choices_explain_codex_quota_in_every_ui_language(self):
        from vlt.ui_text import _provider_choices
        expected = {
            'zh': 'ChatGPT 订阅语音（消耗 Codex 额度）',
            'en': 'ChatGPT subscription voice (uses Codex quota)',
            'ja': 'ChatGPT サブスクリプション音声（Codex の利用枠を消費）',
            'ko': 'ChatGPT 구독 음성 (Codex 사용 한도 소모)',
            'ru': 'Голос по подписке ChatGPT (расходует квоту Codex)',
        }
        for lang, label in expected.items():
            with self.subTest(lang=lang):
                i18n.set_language(lang)
                choices = _provider_choices()
                self.assertEqual([pid for _, pid in choices], ['qianwen', 'qwencloud', 'chatgpt'])
                self.assertEqual(dict((pid, name) for name, pid in choices)['chatgpt'], label)
        i18n.set_language('zh')

    def test_chinese_subscription_catalog_matches_simplified_base_language(self):
        from opencc import OpenCC
        from vlt.locales.chatgpt import STRINGS
        convert = OpenCC('t2s').convert
        for key in STRINGS['en']:
            self.assertEqual(key, convert(key).replace('登入', '登录'))

    def setUp(self):
        sandbox_config(reset=True)
        i18n.set_language('zh')
        # 此檔只驗 Tk 控制項；原生登入快取探測由登入／RPC 測試涵蓋。
        checker = patch('vlt.gui_chatgpt._run_auth')
        checker.start()
        self.addCleanup(checker.stop)

    def test_saved_subscription_startup_never_reads_qwen_credentials(self):
        path = sandbox_config()
        path.write_text('session:\n  provider: chatgpt\nui:\n  lang: zh\n', encoding='utf-8')
        with patch.object(gui_mod, '_is_test_process', return_value=True), \
             patch('vlt.config.load_api_key', side_effect=SystemExit('API key required')) as loader, \
             contextlib.redirect_stdout(io.StringIO()):
            crashlog.log_startup_info('test')
            gui = gui_mod.TranslationGUI()
            try:
                loader.assert_not_called()
                self.assertNotEqual(gui._last_status_level, 'error')
                self.assertEqual(gui._key_btn.cget('text'), '登录 ChatGPT ▸')
            finally:
                gui._root.destroy()

    def test_active_dual_provider_change_keeps_runtime_and_disk_unchanged(self):
        with patch.object(gui_mod, '_is_test_process', return_value=True), \
             patch('vlt.config.load_api_key', side_effect=SystemExit('no key')):
            gui = gui_mod.TranslationGUI()
            try:
                gui._root.withdraw()
                original = sandbox_config().read_bytes()
                gui._engines = [SimpleNamespace(running=True), SimpleNamespace(running=True)]
                gui._provider_var.set(gui._provider_id_to_name['chatgpt'])
                gui._provider_save_btn.invoke()
                self.assertEqual(gui._cfg.session_base['provider'], 'qianwen')
                self.assertEqual(sandbox_config().read_bytes(), original)
                for name in ('mine', 'theirs'):
                    self.assertEqual(gui._cfg.direction(name).to_session_config(gui._cfg.session_base).provider, 'qianwen')
                gui._engines = []
                gui._power_state = 'stopping'
                gui._provider_save_btn.invoke()
                self.assertEqual(gui._cfg.session_base['provider'], 'qianwen')
                gui._power_state = 'idle'
                gui._provider_save_btn.invoke()
                self.assertEqual(gui._cfg.session_base['provider'], 'chatgpt')
            finally:
                gui._root.destroy()

    def test_subscription_controls_follow_all_ui_languages(self):
        from vlt.ui_text import _provider_choices
        from vlt.gui_chatgpt import refresh_key_status
        from unittest.mock import Mock
        expected = {'en': 'Log in to ChatGPT ▸', 'ja': 'ChatGPT にログイン ▸',
                    'ko': 'ChatGPT 로그인 ▸', 'ru': 'Войти в ChatGPT ▸'}
        for lang in ('en', 'ja', 'ko', 'ru'):
            i18n.set_language(lang)
            self.assertNotEqual(dict((pid, name) for name, pid in _provider_choices())['chatgpt'], 'ChatGPT 订阅语音')
            gui = SimpleNamespace(_provider=lambda: 'chatgpt', _key_status=Mock(), _key_btn=Mock(), _key_chip=Mock())
            refresh_key_status(gui)
            self.assertEqual(gui._key_btn.configure.call_args.kwargs['text'], expected[lang])
        i18n.set_language('zh')

    def test_saved_provider_updates_auth_voice_and_text_controls(self):
        for lang, _ in i18n.available_languages():
            sandbox_config(reset=True)
            with self.subTest(lang=lang), \
                 patch('vlt.i18n.detect_system_language', return_value=lang), \
                 patch.object(gui_mod, '_is_test_process', return_value=True), \
                 patch('vlt.config.load_api_key', side_effect=SystemExit('no key')):
                gui = gui_mod.TranslationGUI()
                try:
                    gui._root.withdraw()
                    self.assertEqual(i18n.current_language(), lang)
                    label = i18n.t('ChatGPT 订阅语音（消耗 Codex 额度）')
                    self.assertIn(label, gui._provider_combo.cget('values'))
                    self.assertEqual(gui._provider_name_to_id[label], 'chatgpt')
                    gui._provider_var.set(gui._provider_id_to_name['chatgpt'])
                    gui._provider_save_btn.invoke()
                    self.assertEqual(gui._provider(), 'chatgpt')
                    self.assertEqual(gui._cfg.session_base['api_key'], '')
                    self.assertEqual(gui._key_btn.cget('text'), i18n.t('登录 ChatGPT ▸'))
                    self.assertEqual(str(gui._key_entry.cget('state')), 'disabled')
                    self.assertEqual(str(gui._speech_voice_combo.cget('state')), 'disabled')
                    self.assertTrue(gui._chat_ctx.text_entry.instate(['disabled']))
                    gui._provider_var.set(gui._provider_id_to_name['qianwen'])
                    gui._provider_save_btn.invoke()
                    self.assertEqual(gui._provider(), 'qianwen')
                    self.assertEqual(str(gui._key_entry.cget('state')), 'normal')
                    self.assertEqual(str(gui._speech_voice_combo.cget('state')), 'normal')
                finally:
                    gui._root.destroy()


if __name__ == '__main__':
    unittest.main()
