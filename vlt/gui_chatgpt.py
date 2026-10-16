"""ChatGPT 登入與訂閱語音的 GUI 狀態；Qwen 控制項沿用原實作。"""
import asyncio
import threading

from . import endpoints, gui_settings, gui_update
from .i18n import t


def uses_chatgpt(gui):
    return gui._provider() == endpoints.PROVIDER_CHATGPT


def refresh_key_status(gui):
    chatgpt = uses_chatgpt(gui)
    for name in ('_key_entry', '_key_save_btn', '_key_clear_btn', '_speech_preview_btn', '_tts_preview_btn'):
        widget = getattr(gui, name, None)
        if widget is not None:
            widget.configure(state='disabled' if chatgpt else 'normal')
    for name in ('_speech_voice_combo', '_tts_voice_combo'):
        widget = getattr(gui, name, None)
        if widget is not None:
            widget.configure(state='disabled' if chatgpt else 'normal')
    voice = getattr(gui, '_speech_voice_var', None)
    if voice is not None:
        voice.set('juniper' if chatgpt else gui._effective_speech_voice())
    if not chatgpt:
        return gui_update.refresh_key_status(gui)
    if not getattr(gui, '_chatgpt_check_started', False) and hasattr(gui, '_root'):
        _run_auth(gui, login=False)
    authenticated = getattr(gui, '_chatgpt_authenticated', False)
    if getattr(gui, '_chatgpt_login_busy', False):
        message = t('正在确认 ChatGPT 登录状态…')
    elif authenticated:
        message = t('ChatGPT 已登录（此工具专用），无需 API key。\n选择中文 → 日本语与「我说」即可开始翻译。')
    else:
        message = t('ChatGPT 订阅语音使用此工具专属登录，不影响本机 Codex。\n请先登录 ChatGPT，再选择中文 → 日本语与「我说」。')
    gui._key_status.configure(text=message)
    if hasattr(gui, '_key_btn'):
        gui._key_btn.configure(text=t('切换 ChatGPT 帐户 ▸') if authenticated else t('登录 ChatGPT ▸'))
        gui._key_chip.pack_forget()
        if not gui._key_btn.winfo_manager():
            gui._key_btn.pack()
    if hasattr(gui, '_set_text_input_enabled'):
        gui._set_text_input_enabled(False)


def build_settings_dialog(gui):
    gui_settings.build_settings_dialog(gui)
    gui._provider_save_btn.configure(command=gui._on_save_provider)
    refresh_key_status(gui)


def build_provider_section(gui, body):
    gui_settings.build_provider_section(gui, body)
    gui._provider_save_btn.configure(command=gui._on_save_provider)


def open_signup(gui):
    if not uses_chatgpt(gui):
        return gui_update.open_qianwen_signup(gui)
    if (any(e.running for e in getattr(gui, '_engines', []))
            or getattr(gui, '_pending_starts', 0) or getattr(gui, '_power_state', 'idle') != 'idle'):
        gui._set_status('warn', t('当前线路需要先停止翻译，改完再重新开始'))
        return
    if getattr(gui, '_chatgpt_login_busy', False):
        gui._set_status('info', t('ChatGPT 登录仍在进行，请完成浏览器中的登录。'))
        return
    _run_auth(gui, login=True)


def _finish_auth(gui):
    if gui._chatgpt_login_cancel.is_set():
        return
    if gui._chatgpt_login_busy:
        gui._root.after(100, lambda: _finish_auth(gui))
        return
    gui._chatgpt_authenticated = gui._chatgpt_auth_result
    if uses_chatgpt(gui):
        refresh_key_status(gui)


def _run_auth(gui, *, login):
    gui._chatgpt_check_started = True
    gui._chatgpt_login_busy = True
    gui._chatgpt_auth_result = False
    gui._chatgpt_login_cancel = threading.Event()
    if login:
        gui._set_status('info', t('请在浏览器完成 ChatGPT 登录；完成后可开始翻译。'))

    async def authenticate():
        from .session.codex_rpc import login_chatgpt, chatgpt_logged_in

        async def run():
            if login and not await login_chatgpt():
                return False
            return await chatgpt_logged_in()

        task = asyncio.create_task(run())
        gui._chatgpt_login_task = (asyncio.get_running_loop(), task)
        if gui._chatgpt_login_cancel.is_set():
            task.cancel()
        try:
            return await task
        finally:
            gui._chatgpt_login_task = None

    def work():
        try:
            ok = asyncio.run(authenticate())
            gui._chatgpt_auth_result = ok
            if login and not gui._chatgpt_login_cancel.is_set():
                gui._q.put(('status', 'info' if ok else 'error',
                            t('ChatGPT 登录完成，可以开始翻译。') if ok else t('登录未完成，请重新登录 ChatGPT。')))
        except asyncio.CancelledError:
            pass
        except Exception:
            if not gui._chatgpt_login_cancel.is_set():
                gui._q.put(('status', 'error', t('无法启动登录，请确认已安装官方 Codex CLI。')))
        finally:
            gui._chatgpt_login_busy = False
    gui._chatgpt_login_thread = threading.Thread(target=work, daemon=False, name='vlt-chatgpt-login')
    gui._chatgpt_login_thread.start()
    if hasattr(gui, '_root'):
        gui._root.after(100, lambda: _finish_auth(gui))


def cancel_login(gui):
    cancel = getattr(gui, '_chatgpt_login_cancel', None)
    if cancel is None or cancel.is_set():
        return
    cancel.set()
    owner = getattr(gui, '_chatgpt_login_task', None)
    if owner:
        loop, task = owner
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass  # 登入已完成並關閉其 event loop。


def on_save_provider(gui):
    if (any(e.running for e in gui._engines) or getattr(gui, '_pending_starts', 0)
            or getattr(gui, '_power_state', 'idle') != 'idle'):
        gui._set_status('warn', t('当前线路需要先停止翻译，改完再重新开始'))
        return
    gui_settings.on_save_provider(gui)
    refresh_key_status(gui)
