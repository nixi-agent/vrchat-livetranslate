"""Offline GUI history must retain each segment's output authorization."""
import asyncio
import queue
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.gui_chat import add_text, draw_bubble, redraw_all, redraw_current, trim
from vlt.gui_chat_context import ChatCtx
from vlt.gui_sinks import push_desktop, push_overlay
from vlt.session.chatgpt_output import allowed, current, scope
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator


class Approval:
    def __init__(self, direction='mine'):
        self.direction, self.valid = direction, True

    def __call__(self, direction=None):
        if direction is not None and direction != self.direction:
            self.valid = False
        return self.valid


class GuiApprovalTests(unittest.TestCase):
    def setUp(self):
        self.chat = ChatCtx()
        self.frames = []
        def receive(entries, **kwargs):
            self.frames.append((entries, current()))
        sink = SimpleNamespace(update_entries=receive, available=True)
        self.ctx = SimpleNamespace(bubbles=self.chat.bubbles,
                                   overlay_out=sink, desktop_out=sink)

    def add(self, approval, text='translation', final=True, who='mine'):
        with scope(approval):
            add_text(self.chat, 'source', text, final, who)

    def push(self):
        push_overlay(self.ctx, force=True)
        push_desktop(self.ctx, force=True)

    def test_revoked_history_not_republished_by_forced_refresh(self):
        approval = Approval()
        self.add(approval)
        approval.valid = False
        self.push()
        self.assertEqual([entries for entries, _ in self.frames], [[], []])
        self.assertEqual(len(self.chat.bubbles), 1)  # Stored history is not deleted.

    def test_room_queue_updates_both_sinks_without_later_text_event(self):
        from vlt.gui_chat_poll import poll
        for final in (False, True):
            with self.subTest(final=final):
                chat, frames = ChatCtx(q=queue.Queue()), []
                ctx = SimpleNamespace(bubbles=chat.bubbles)
                for name in ('overlay_out', 'desktop_out'):
                    setattr(ctx, name, SimpleNamespace(update_entries=lambda entries, name=name, **kwargs:
                        frames.append((name, entries, current())), available=True))
                chat.push_overlay_fn = lambda: push_overlay(ctx)
                chat.push_desktop_fn = lambda: push_desktop(ctx)
                chat.q.put(('room', 'Nickname', 'peer-id', 'room sentence', final))
                poll(chat, Mock(), Mock(), lambda source, text, is_final, **kwargs:
                     add_text(chat, source, text, is_final, **kwargs), Mock(), {})
                self.assertEqual([name for name, _, _ in frames], ['overlay_out', 'desktop_out'])
                for _, entries, approval in frames:
                    self.assertEqual(entries, [('peer:peer-id', '', 'room sentence', 'Nickname')])
                    self.assertIsNone(approval)

    def test_each_direction_keeps_its_own_approval(self):
        mine, theirs = Approval(), Approval('theirs')
        self.add(mine, 'mine')
        self.add(theirs, 'theirs', who='theirs')
        self.push()
        for entries, approval in self.frames:
            self.assertEqual([entry[2] for entry in entries], ['mine', 'theirs'])
            self.assertTrue(allowed(approval))
        self.assertTrue(mine.valid and theirs.valid)

    def test_captured_frame_remains_revocable_after_push_returns(self):
        approval = Approval()
        self.add(approval)
        self.push()
        approval.valid = False
        for _, captured in self.frames:
            self.assertFalse(allowed(captured))

    def test_revoked_mine_does_not_remove_theirs_or_room_rows(self):
        mine, theirs = Approval(), Approval('theirs')
        self.add(mine, 'mine')
        self.add(theirs, 'theirs', who='theirs')
        with scope(None):
            add_text(self.chat, 'room source', 'room', True, 'peer', 'nickname')
        mine.valid = False
        self.push()
        for entries, approval in self.frames:
            self.assertEqual([entry[2] for entry in entries], ['theirs', 'room'])
            self.assertEqual(entries[-1][3], 'nickname')
            self.assertTrue(allowed(approval))

    def test_legacy_only_frame_does_not_inherit_outer_approval(self):
        self.add(None)
        unrelated = Approval('theirs')
        with scope(unrelated):
            self.push()
        for _, captured in self.frames:
            self.assertIsNone(captured)
        self.assertTrue(unrelated.valid)

    def test_new_segment_does_not_relabel_old_partial(self):
        old, new = Approval(), Approval()
        self.add(old, 'old partial', final=False)
        self.add(new, 'new final')
        old.valid = False
        self.push()
        self.assertEqual(len(self.chat.bubbles), 2)
        for entries, approval in self.frames:
            self.assertEqual([entry[2] for entry in entries], ['new final'])
            self.assertTrue(allowed(approval))

    def test_legacy_partial_cannot_be_absorbed_by_new_approved_segment(self):
        self.add(None, 'legacy partial', final=False)
        self.add(Approval(), 'approved final')
        self.assertEqual([b.text for b in self.chat.bubbles],
                         ['legacy partial', 'approved final'])

    def test_same_legacy_stream_still_updates_one_bubble(self):
        self.add(None, 'partial', final=False)
        self.add(None, 'final')
        self.assertEqual(len(self.chat.bubbles), 1)
        self.assertEqual(self.chat.bubbles[0].text, 'final')
        self.assertEqual(self.chat.current, {})

    def test_invalid_input_never_enters_history(self):
        approval = Approval()
        approval.valid = False
        self.add(approval)
        self.assertEqual(self.chat.bubbles, [])

    def test_wrong_direction_never_enters_history(self):
        self.add(Approval('theirs'))
        self.assertEqual(self.chat.bubbles, [])

    def test_revoked_bubble_not_drawn_on_resize(self):
        approval = Approval()
        self.add(approval)
        approval.valid = False
        self.chat.canvas = Mock()
        self.chat.canvas.bbox.return_value = (0, 0, 100, 20)
        self.chat.canvas.winfo_height.return_value = 100
        redraw_all(self.chat)
        self.chat.canvas.create_text.assert_not_called()
        self.assertEqual(self.chat.bubbles[0].h, 0)

    def test_revoked_bubble_not_drawn_directly(self):
        approval = Approval()
        self.add(approval)
        approval.valid = False
        self.chat.canvas = Mock()
        self.chat.canvas.bbox.return_value = (0, 0, 100, 20)
        draw_bubble(self.chat, self.chat.bubbles[0])
        self.chat.canvas.create_text.assert_not_called()

    def test_approved_bubble_draws_normally(self):
        self.add(Approval())
        cv = self.chat.canvas = Mock()
        cv.bbox.return_value = (0, 0, 100, 20)
        self.assertGreater(draw_bubble(self.chat, self.chat.bubbles[0]), 8)
        self.assertGreater(cv.create_text.call_count, 0)

    def test_invalid_newer_rows_do_not_displace_valid_history(self):
        self.add(Approval(), 'mine')
        self.add(Approval('theirs'), 'theirs', who='theirs')
        with scope(None):
            add_text(self.chat, 'room source', 'room', True, 'peer', 'nickname')
        for _ in range(8):
            approval = Approval()
            self.add(approval, 'invalid')
            approval.valid = False
        self.push()
        for entries, approval in self.frames:
            self.assertEqual([entry[2] for entry in entries], ['mine', 'theirs', 'room'])
            self.assertEqual(entries[-1][3], 'nickname')
            self.assertTrue(allowed(approval))

    def test_valid_history_window_still_limits_to_last_eight(self):
        for i in range(10):
            self.add(None, str(i))
        self.push()
        for entries, approval in self.frames:
            self.assertEqual([entry[2] for entry in entries], list(map(str, range(2, 10))))
            self.assertIsNone(approval)

    def canvas(self):
        cv = self.chat.canvas = Mock()
        cv.bbox.return_value = (0, 0, 100, 20)
        cv.winfo_height.return_value = 100
        self.chat.canvas_w = 600
        return cv

    def invalid_history(self, count):
        for _ in range(count):
            approval = Approval()
            self.add(approval)
            approval.valid = False

    def test_hidden_history_has_no_height_or_gap_before_valid_row(self):
        self.invalid_history(100)
        self.add(None, 'valid')
        cv = self.canvas()
        redraw_all(self.chat)
        self.assertEqual(self.chat.bubbles[-1].y, 8)
        self.assertLess(cv.configure.call_args.kwargs['scrollregion'][3], 150)
        self.assertEqual(len(self.chat.bubbles), 101)

    def test_all_hidden_history_has_no_blank_scroll_area(self):
        self.invalid_history(100)
        cv = self.canvas()
        redraw_all(self.chat)
        cv.create_text.assert_not_called()
        self.assertEqual(cv.configure.call_args.kwargs['scrollregion'], (0, 0, 600, 100))

    def test_trimming_hidden_history_does_not_move_visible_rows(self):
        self.invalid_history(1)
        self.add(None, 'valid')
        cv = self.canvas()
        redraw_all(self.chat)
        original_y = self.chat.bubbles[-1].y
        cv.reset_mock()
        with patch('vlt.gui_chat.MAX_BUBBLES', 1):
            trim(self.chat)
        self.assertEqual(len(self.chat.bubbles), 1)
        self.assertEqual(self.chat.bubbles[0].y, original_y)
        cv.move.assert_not_called()

    def test_current_revocation_removes_its_height_and_gap(self):
        approval = Approval()
        self.add(approval, final=False)
        self.add(None, 'other', who='theirs')
        first, other = self.chat.bubbles
        first.y, first.h, first.items = 8, 20, [1]
        other.y, other.h = 36, 20
        self.canvas()
        approval.valid = False
        redraw_current(self.chat, first)
        self.assertEqual(other.y, 8)


class CoordinatorHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_two_direction_closures_survive_refresh_and_remain_revocable(self):
        configs = {who: SessionConfig(provider='chatgpt', source_lang='zh',
                   target_lang='ja', output_audio=False) for who in ('mine', 'theirs')}
        verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                       negation=True, complete=True, extra_content=False)
        services = SimpleNamespace(source_asr=AsyncMock(return_value='來源'),
            translate=AsyncMock(return_value=dict(translation='translation', language='ja')),
            validate_text=AsyncMock(return_value=verdict))
        pipeline = Coordinator(configs, services, Mock(), lambda: 1)
        self.addAsyncCleanup(pipeline.close)
        chat, frames = ChatCtx(), []
        for who in configs:
            self.assertTrue(pipeline.submit(who, bytes(320)))
        await asyncio.gather(*tuple(pipeline._owned))
        for who in configs:
            def display(delta, who=who):
                add_text(chat, delta.source, delta.display, True, who)
            self.assertTrue(pipeline.consume(who, display, Mock()))
        sink = SimpleNamespace(update_entries=lambda entries, **kwargs:
                               frames.append((entries, current())))
        ctx = SimpleNamespace(bubbles=chat.bubbles, overlay_out=sink)
        push_overlay(ctx, force=True)
        entries, approval = frames[-1]
        self.assertEqual([row[0] for row in entries], ['mine', 'theirs'])
        self.assertTrue(allowed(approval))
        self.assertFalse(allowed(chat.bubbles[0].approval, 'theirs'))
        self.assertFalse(allowed(approval))
        push_overlay(ctx, force=True)
        entries, approval = frames[-1]
        self.assertEqual([row[0] for row in entries], ['theirs'])
        self.assertTrue(allowed(approval))
        pipeline.invalidate()
        self.assertFalse(allowed(approval))


if __name__ == '__main__':
    unittest.main()
