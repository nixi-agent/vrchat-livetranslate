"""Desktop fatal-native failures through actual GUI lifecycle callers."""
import sys
import queue
import unittest
from tkinter import TclError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_desktop_approval as cases
Approval = cases.Approval


class DesktopLifecycleTests(unittest.TestCase):
    output = cases.DesktopApprovalTests.output
    entries = cases.DesktopApprovalTests.entries

    def gui_owner(self, out):
        from vlt.gui import TranslationGUI, _E2G, _G2E
        from vlt.gui_engine_context import EngineCtx
        engine = EngineCtx(desktop_out=out, desktop_dragging=True, sinks={'desktop'},
                           desktop_var=Mock(), set_status_fn=Mock(), root=Mock(),
                           q=queue.Queue(), cfg=SimpleNamespace(desktop_overlay={}))
        gui = object.__new__(TranslationGUI)
        gui._engine_ctx = engine
        for name in _E2G + _G2E:
            setattr(gui, '_' + name, getattr(engine, name))
        gui._lang_pair, gui._bubbles = {}, engine.bubbles
        gui._root, gui._q, gui._cfg = engine.root, engine.q, engine.cfg
        return gui

    def test_real_gui_owner_clears_once_and_can_restart_after_fatal_poll(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        out = self.output()
        out._started = True
        out._native.tick.side_effect = RuntimeError('native tick failure')
        gui = self.gui_owner(out)
        engine = gui._engine_ctx
        ctx = ChatCtx(q=gui._q, desktop_out_fn=lambda: gui._desktop_out,
                      push_desktop_fn=gui._push_desktop)
        for _ in range(3):
            poll(ctx, gui._root, engine.set_status_fn, Mock(), Mock(), {})
        self.assertEqual(gui._root.after.call_count, 3)
        self.assertIsNone(gui._desktop_out)
        self.assertIsNone(engine.desktop_out)
        self.assertFalse(gui._desktop_dragging)
        self.assertNotIn('desktop', gui._sinks)
        engine.set_status_fn.assert_called_once()
        engine.desktop_var.set.assert_called_once_with(False)
        fresh = Mock(available=True)
        fresh.start.return_value = True
        with patch('vlt.output.desktop_overlay.DesktopOverlay', return_value=fresh):
            self.assertTrue(gui._start_desktop(force=True))
        self.assertIs(gui._desktop_out, fresh)
        poll(ctx, gui._root, engine.set_status_fn, Mock(), Mock(), {})
        fresh.tick.assert_called_once()
        engine.set_status_fn.assert_called_once()

    def test_drag_setter_failure_disables_both_states_and_allows_restart(self):
        for dragging in (False, True):
            with self.subTest(dragging=dragging):
                out = self.output()
                out._started, out._dragging = True, dragging
                native = out._native
                native.set_draggable.side_effect = RuntimeError('drag failure')
                gui = self.gui_owner(out)
                gui._desktop_dragging = dragging
                gui._toggle_desktop_drag()
                self.assertFalse(out.available)
                self.assertFalse(out._started)
                self.assertFalse(out._dragging)
                self.assertFalse(gui._desktop_dragging)
                native.close.assert_called_once()
                gui._push_desktop()
                gui._push_desktop()
                self.assertIsNone(gui._desktop_out)
                gui._engine_ctx.set_status_fn.assert_called_once()
                fresh = self.output()
                fresh._started = True
                with patch('vlt.output.desktop_overlay.DesktopOverlay', return_value=fresh), \
                        patch.object(fresh, 'start', return_value=True):
                    self.assertTrue(gui._start_desktop(force=True))
                gui._toggle_desktop_drag()
                self.assertTrue(gui._desktop_dragging)
                self.assertTrue(fresh._dragging)
                fresh._native.set_draggable.assert_called_once_with(True)

    def test_tk_drag_style_failure_disables_output_and_clears_drag_state(self):
        out = self.output()
        out._native, out._win, out._started = None, Mock(), True
        win = out._win
        with patch.object(out, '_apply_exstyles', side_effect=RuntimeError('style failure')):
            with self.assertRaises(RuntimeError):
                out.set_draggable(True)
        self.assertFalse(out.available)
        self.assertFalse(out._started)
        self.assertFalse(out._dragging)
        win.destroy.assert_called_once()

    def test_tk_drag_false_result_clears_real_gui_owner_once_and_can_restart(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        for dragging in (False, True):
            with self.subTest(dragging=dragging):
                out = self.output()
                out._native, out._win, out._started = None, Mock(), True
                out._hwnd, out._dragging = 123, dragging
                win = out._win
                gui = self.gui_owner(out)
                gui._desktop_dragging = dragging
                with patch('vlt.platform.set_tool_window', return_value=True), \
                        patch('vlt.platform.set_click_through', return_value=False) as setter:
                    gui._toggle_desktop_drag()
                setter.assert_called_once_with(123, dragging)
                self.assertFalse(out.available)
                self.assertFalse(out._started)
                self.assertFalse(out._dragging)
                self.assertFalse(gui._desktop_dragging)
                win.destroy.assert_called_once()
                ctx = ChatCtx(q=gui._q, desktop_out_fn=lambda: gui._desktop_out,
                              push_desktop_fn=gui._push_desktop)
                for _ in range(3):
                    poll(ctx, gui._root, gui._engine_ctx.set_status_fn, Mock(), Mock(), {})
                self.assertIsNone(gui._desktop_out)
                self.assertEqual(gui._root.after.call_count, 3)
                gui._engine_ctx.set_status_fn.assert_called_once()
                fresh = Mock(available=True)
                fresh.start.return_value = True
                with patch('vlt.output.desktop_overlay.DesktopOverlay', return_value=fresh):
                    self.assertTrue(gui._start_desktop(force=True))
                self.assertIs(gui._desktop_out, fresh)

    def test_tk_style_capability_failure_keeps_non_drag_fallback_available(self):
        out = self.output()
        out._native, out._win, out._started, out._hwnd = None, Mock(), True, 123
        with patch('vlt.platform.set_tool_window', return_value=False), \
                patch('vlt.platform.set_click_through', return_value=False):
            out._apply_exstyles()
        self.assertTrue(out.available)
        self.assertTrue(out._started)
        out._win.destroy.assert_not_called()

    def test_tk_revoked_hide_failure_closes_and_clears_gui_owner(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        for entry in ('tick', 'set_alpha', 'reload'):
            with self.subTest(entry=entry):
                out, approval = self.output(), Approval()
                out._native, out._win, out._started = None, Mock(), True
                win = out._win
                self.entries(out, approval)
                out._photo = object()
                out._displayed_approval = approval
                approval.valid = False
                win.attributes.side_effect = TclError('alpha unsupported')
                gui = self.gui_owner(out)
                ctx = ChatCtx(q=gui._q, desktop_out_fn=lambda: gui._desktop_out,
                              push_desktop_fn=gui._push_desktop)
                if entry == 'set_alpha':
                    out.set_alpha(0.6)
                elif entry == 'reload':
                    out.config_path = Mock()
                    out.config_path.stat.return_value.st_mtime = 1
                    out.config_path.read_text.return_value = 'desktop_overlay:\n  alpha: 0.6\n'
                    out._reload_config()
                if entry != 'tick':
                    self.assertFalse(out.available)
                for _ in range(3):
                    poll(ctx, gui._root, gui._engine_ctx.set_status_fn, Mock(), Mock(), {})
                self.assertFalse(out.available)
                self.assertFalse(out._started)
                self.assertIsNone(out._photo)
                self.assertIsNone(out._content)
                self.assertFalse(out._dirty)
                win.destroy.assert_called_once()
                self.assertIsNone(gui._desktop_out)
                self.assertIsNone(gui._engine_ctx.desktop_out)
                self.assertEqual(gui._root.after.call_count, 3)
                gui._engine_ctx.set_status_fn.assert_called_once()
                fresh = Mock(available=True)
                fresh.start.return_value = True
                with patch('vlt.output.desktop_overlay.DesktopOverlay', return_value=fresh):
                    self.assertTrue(gui._start_desktop(force=True))
                self.assertIs(gui._desktop_out, fresh)

    def test_tk_valid_alpha_failure_preserves_existing_fallback(self):
        for entry in ('set_alpha', '_apply_alpha_only'):
            with self.subTest(entry=entry):
                out = self.output()
                out._native, out._win, out._started = None, Mock(), True
                out._displayed_approval = Approval()
                out._win.attributes.side_effect = TclError('alpha unsupported')
                if entry == 'set_alpha':
                    out.set_alpha(0.6)
                else:
                    out._apply_alpha_only()
                self.assertTrue(out.available)
                self.assertTrue(out._started)
                out._win.destroy.assert_not_called()

    def test_native_alpha_reload_failure_keeps_actual_gui_poll_running(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        out, approval = self.output(), Approval()
        out._started = True
        self.entries(out, approval)
        out._redraw()
        native, root = out._native, Mock()
        native.set_alpha.side_effect = RuntimeError('config alpha failure')
        out.config_path = Mock()
        out.config_path.stat.return_value.st_mtime = 1
        out.config_path.read_text.return_value = 'desktop_overlay:\n  alpha: 0.6\n  attach_to_game: false\n'
        failure = None
        try:
            poll(ChatCtx(q=queue.Queue(), desktop_out_fn=lambda: out), root, Mock(), Mock(), Mock(), {})
        except Exception as exc:
            failure = exc
        self.assertIsNone(failure)
        root.after.assert_called_once()
        self.assertFalse(out._dirty)
        native.tick.assert_not_called()

    def test_native_tick_failure_keeps_actual_gui_poll_running(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        out, approval = self.output(), Approval()
        out._started = True
        self.entries(out, approval)
        out._redraw()
        native, root = out._native, Mock()
        native.tick.side_effect = RuntimeError('native tick failure')
        failure = None
        try:
            poll(ChatCtx(q=queue.Queue(), desktop_out_fn=lambda: out), root, Mock(), Mock(), Mock(), {})
        except Exception as exc:
            failure = exc
        self.assertIsNone(failure)
        root.after.assert_called_once()
        native.close.assert_called_once()
        self.assertFalse(out.available)

    def test_gui_rebuilds_failed_desktop_and_reports_real_start_result(self):
        from vlt.gui_sinks import start_desktop
        from vlt.gui_engine_context import EngineCtx
        for success in (True, False):
            with self.subTest(success=success):
                out = self.output()
                native = out._native
                native.set_alpha.side_effect = RuntimeError('alpha failure')
                out.set_alpha(0.6)
                ctx = EngineCtx(desktop_out=out, cfg=SimpleNamespace(desktop_overlay={}), root=Mock())
                fresh = Mock()
                fresh.start.return_value = success
                with patch('vlt.output.desktop_overlay.DesktopOverlay', return_value=fresh) as ctor:
                    result = start_desktop(ctx, force=True)
                self.assertIs(result, success)
                ctor.assert_called_once()
                self.assertIs(ctx.desktop_out, fresh if success else None)

    def test_explicit_close_marks_desktop_unavailable(self):
        out = self.output()
        out.close()
        self.assertFalse(out.available)


    def test_clickthrough_reload_failure_keeps_actual_poll_running(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        out, root = self.output(), Mock()
        out._started = True
        native = out._native
        native.set_click_through.side_effect = RuntimeError('click-through failure')
        out.config_path = Mock()
        out.config_path.stat.return_value.st_mtime = 1
        out.config_path.read_text.return_value = 'desktop_overlay:\n  click_through: false\n  attach_to_game: false\n'
        failure = None
        try:
            poll(ChatCtx(q=queue.Queue(), desktop_out_fn=lambda: out), root, Mock(), Mock(), Mock(), {})
        except Exception as exc:
            failure = exc
        self.assertIsNone(failure)
        root.after.assert_called_once()
        native.close.assert_called_once()
        self.assertFalse(out.available)
        self.assertFalse(out._dirty)

    def test_terminal_close_rejects_direct_new_content(self):
        out = self.output()
        out.close()
        out.update('late', 'source')
        self.entries(out, Approval(), 'late')
        self.assertFalse(out._dirty)
        self.assertIsNone(out._content)
        self.assertEqual(out._latest, ('', ''))

    def test_fatal_close_then_actual_gui_text_keeps_terminal_state_and_syncs_ui(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        from vlt.gui_engine_context import EngineCtx
        from vlt.gui_sinks import push_desktop
        out, root, status = self.output(), Mock(), Mock()
        out._started = True
        out._native.tick.side_effect = RuntimeError('native tick failure')
        engine = EngineCtx(desktop_out=out, desktop_var=Mock(), set_status_fn=status)
        q = queue.Queue()
        ctx = ChatCtx(q=q, desktop_out_fn=lambda: engine.desktop_out,
                      push_desktop_fn=lambda: push_desktop(engine))
        def add(source_id, source, text, who):
            engine.bubbles.append(SimpleNamespace(who=who, source=source, text=text,
                                                label='', approval=Approval()))
        poll(ctx, root, status, add, Mock(), {})
        q.put(('text', 'mine', 'sourceid', 'source', 'new', Approval()))
        poll(ctx, root, status, add, Mock(), {})
        self.assertEqual(root.after.call_count, 2)
        self.assertFalse(out._dirty)
        self.assertIsNone(out._content)
        self.assertEqual(out._entries, [])
        self.assertIsNone(engine.desktop_out)
        engine.desktop_var.set.assert_called_once_with(False)
        status.assert_called_once()

    def test_silent_native_disable_is_synchronized_by_real_poll(self):
        from vlt.gui_chat_context import ChatCtx
        from vlt.gui_chat_poll import poll
        out, root = self.output(), Mock()
        native = out._native
        out._started = True
        native.available = True
        native.tick.side_effect = lambda: setattr(native, 'available', False)
        poll(ChatCtx(q=queue.Queue(), desktop_out_fn=lambda: out), root, Mock(), Mock(), Mock(), {})
        root.after.assert_called_once()
        self.assertFalse(out.available)
        native.close.assert_called_once()

    def test_gui_does_not_report_success_when_initial_push_disables_output(self):
        from vlt.gui_sinks import start_desktop
        from vlt.gui_engine_context import EngineCtx
        fresh = Mock()
        fresh.start.return_value = True
        fresh.available = False
        ctx = EngineCtx(cfg=SimpleNamespace(desktop_overlay={}), root=Mock())
        with patch('vlt.output.desktop_overlay.DesktopOverlay', return_value=fresh):
            self.assertFalse(start_desktop(ctx, force=True))
        self.assertIsNone(ctx.desktop_out)


if __name__ == "__main__":
    unittest.main()
