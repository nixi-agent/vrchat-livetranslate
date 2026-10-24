"""Offline approved text must remain revocable at actual GUI/OSC consumers."""
import asyncio
import queue
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.gui import TranslationGUI
from vlt.gui_chat import ChatCtx, poll
from vlt.output.chatbox import Chatbox, TokenBucket
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator


class TextQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = 1
        self.cfg = SessionConfig(provider='chatgpt', source_lang='zh', target_lang='ja', output_audio=False)
        verdict = dict(approved=True, language='ja', meaning=True, numbers=True,
                       negation=True, complete=True, extra_content=False)
        services = SimpleNamespace(source_asr=AsyncMock(return_value='來源'),
            translate=AsyncMock(return_value=dict(translation='translation', language='ja')),
            validate_text=AsyncMock(return_value=verdict))
        self.pipeline = Coordinator({'mine': self.cfg}, services, Mock(), lambda: self.account)
        self.addAsyncCleanup(self.pipeline.close)
        self.bucket = Mock(spec=TokenBucket)
        self.bucket.reserve.return_value = None
        self.cb = Chatbox(bucket=self.bucket, dry_run=True)
        self.q = queue.Queue()
        self.gui = SimpleNamespace(_q=self.q, _publish_to_room=Mock())
        self.ctx = ChatCtx(q=self.q, room_status_next=float('inf'))
        self.add_text, self.overlay, self.desktop = Mock(), Mock(), Mock()
        self.ctx.push_overlay_fn, self.ctx.push_desktop_fn = self.overlay, self.desktop

    def text(self, delta):
        self.cb.send(delta.display, True)
        TranslationGUI._on_engine_text(self.gui, 'mine', 'mic', delta.source, delta.display, True)

    async def enqueue(self):
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await asyncio.gather(*tuple(self.pipeline._owned))
        self.assertTrue(self.pipeline.consume('mine', self.text, Mock()))

    def drain(self):
        self.bucket.reserve.return_value = object()
        sent = self.cb.flush_pending()
        poll(self.ctx, Mock(), Mock(), self.add_text, Mock(), {})
        return sent

    async def test_invalidation_discards_pending_before_consumption(self):
        await self.enqueue()
        self.assertEqual((self.cb.pending_count, self.q.qsize()), (1, 1))
        self.pipeline.invalidate()
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.cb.sent_ok, 0)
        self.assertEqual(self.add_text.call_count, 0)
        self.assertEqual((self.overlay.call_count, self.desktop.call_count), (0, 0))

    async def test_account_change_discards_pending(self):
        await self.enqueue()
        self.account += 1
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.add_text.call_count, 0)

    async def test_gui_consumer_rejects_invalidated_section_without_flushing_chatbox(self):
        await self.enqueue()
        self.pipeline.invalidate()
        poll(self.ctx, Mock(), Mock(), self.add_text, Mock(), {})
        self.assertEqual(self.add_text.call_count, 0)
        self.assertEqual((self.overlay.call_count, self.desktop.call_count), (0, 0))

    async def test_language_change_discards_pending(self):
        await self.enqueue()
        self.cfg.target_lang = 'en'
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.add_text.call_count, 0)

    async def test_language_change_back_cannot_revive_unconsumed_approval(self):
        await self.enqueue()
        self.cfg.target_lang = 'en'
        self.cfg.target_lang = 'ja'
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.add_text.call_count, 0)

    async def test_vocabulary_change_back_cannot_revive_unconsumed_approval(self):
        await self.enqueue()
        self.cfg.hotwords['word'] = 'changed'
        del self.cfg.hotwords['word']
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.add_text.call_count, 0)

    async def test_noop_assignments_cannot_reset_vocabulary_generation(self):
        for reset in (lambda: setattr(self.cfg, 'provider', 'chatgpt'),
                      lambda: setattr(self.cfg, 'hotwords', self.cfg.hotwords),
                      lambda: setattr(self.cfg, 'hotwords', dict(self.cfg.hotwords))):
            with self.subTest(reset=reset):
                before = self.add_text.call_count
                await self.enqueue()
                self.cfg.hotwords['word'] = 'changed'
                self.cfg.hotwords.clear()
                reset()
                self.assertEqual(self.drain(), 0)
                self.assertEqual(self.add_text.call_count, before)

    async def test_version_rejection_is_permanent_for_this_approval(self):
        await self.enqueue()
        approval = self.q.queue[0][5]
        with patch('vlt.session.chatgpt_pipeline._VERIFIER_VERSION', 'new-version'):
            self.assertFalse(approval())
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.add_text.call_count, 0)

    async def test_late_revocation_refunds_real_bucket_for_new_message(self):
        self.cb.bucket = TokenBucket(capacity=1)
        dispatch = self.cb._dispatch
        def revoked_dispatch(*args, **kwargs):
            self.pipeline.invalidate()
            return dispatch(*args, **kwargs)
        with patch.object(self.cb, '_dispatch', side_effect=revoked_dispatch):
            await self.enqueue()
        self.assertEqual(self.cb.sent_ok, 0)
        self.assertTrue(self.cb.send('fresh legacy', True))

    async def test_refund_keeps_other_reservations_and_their_spacing(self):
        self.cb.bucket = TokenBucket(capacity=4, window_s=1000, min_gap_s=0.5)
        self.assertTrue(self.cb.bucket.allow(now=90))
        dispatch = self.cb._dispatch
        def revoked_dispatch(*args, **kwargs):
            self.assertTrue(self.cb.bucket.allow(now=101))
            self.pipeline.invalidate()
            return dispatch(*args, **kwargs)
        with patch('vlt.output.chatbox.time.monotonic', return_value=100):
            with patch.object(self.cb, '_dispatch', side_effect=revoked_dispatch):
                await self.enqueue()
        self.assertEqual(len(self.cb.bucket._stamps), 2)
        self.assertFalse(self.cb.bucket.allow(now=101.25))

    async def test_pending_dispatch_refunds_real_bucket_for_new_message(self):
        self.cb.bucket = TokenBucket(capacity=1)
        self.assertTrue(self.cb.bucket.allow(now=0))
        with patch('vlt.output.chatbox.time.monotonic', return_value=0):
            await self.enqueue()
        self.assertEqual(self.cb.pending_count, 1)
        dispatch = self.cb._dispatch
        def revoked_dispatch(*args, **kwargs):
            self.pipeline.invalidate()
            return dispatch(*args, **kwargs)
        with patch('vlt.output.chatbox.time.monotonic', return_value=6):
            with patch.object(self.cb, '_dispatch', side_effect=revoked_dispatch):
                self.assertEqual(self.cb.flush_pending(), 0)
            self.assertTrue(self.cb.send('fresh legacy', True))

    async def test_all_vocabulary_mutators_preserve_revocation(self):
        changes = (
            lambda w: w.update(word='changed'),
            lambda w: w.setdefault('word', 'changed'),
            lambda w: w.__ior__({'word': 'changed'}),
        )
        removals = (lambda w: w.clear(), lambda w: w.pop('word'), lambda w: w.popitem())
        for change in changes:
            for remove in removals:
                with self.subTest(change=change, remove=remove):
                    before = self.add_text.call_count
                    await self.enqueue()
                    change(self.cfg.hotwords)
                    remove(self.cfg.hotwords)
                    self.assertEqual(self.drain(), 0)
                    self.assertEqual(self.add_text.call_count, before)

    def test_qwen_keeps_mutable_vocabulary_alias_and_public_config_shape(self):
        from dataclasses import asdict

        words = {'word': 'value'}
        legacy = SessionConfig(provider='qianwen', hotwords=words)
        self.assertIs(legacy.hotwords, words)
        words['word'] = 'changed'
        self.assertEqual(legacy.hotwords['word'], 'changed')
        legacy.target_lang = 'ja'
        self.assertNotIn('_policy_revision', asdict(legacy))
        self.assertEqual(asdict(self.cfg)['hotwords'], {})

    async def test_close_discards_pending(self):
        await self.enqueue()
        await self.pipeline.close()
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.add_text.call_count, 0)

    async def test_stale_pending_reports_reason(self):
        await self.enqueue()
        self.pipeline.invalidate()
        self.drain()
        self.assertTrue(any(call.args[2] == 'stale-result'
                            for call in self.pipeline._on_reason.call_args_list))

    async def test_changed_verifier_version_revokes_pending(self):
        await self.enqueue()
        with patch('vlt.session.chatgpt_pipeline._VERIFIER_VERSION', 'new-version'):
            self.assertEqual(self.drain(), 0)
            self.assertEqual(self.add_text.call_count, 0)

    async def test_dispatch_rechecks_after_rate_limit_decision(self):
        self.bucket.reserve.side_effect = lambda: self.pipeline.invalidate() or object()
        await self.enqueue()
        self.assertEqual(self.cb.sent_ok, 0)
        poll(self.ctx, Mock(), Mock(), self.add_text, Mock(), {})
        self.assertEqual(self.add_text.call_count, 0)

    async def test_callback_error_restores_legacy_scope(self):
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await asyncio.gather(*tuple(self.pipeline._owned))
        with self.assertRaises(RuntimeError):
            self.pipeline.consume('mine', Mock(side_effect=RuntimeError('callback failed')), Mock())
        await self.pipeline.close()
        self.bucket.reserve.return_value = object()
        self.assertTrue(self.cb.send('legacy', True))

    async def test_stale_head_does_not_spend_token_or_block_legacy_tail(self):
        await self.enqueue()
        self.pipeline.invalidate()
        self.cb.send('legacy', True)
        self.bucket.reserve.reset_mock()
        self.assertEqual(self.drain(), 1)
        self.bucket.reserve.assert_called_once()

    async def test_real_repetitions_are_separate_approved_sections(self):
        for _ in range(3):
            await self.enqueue()
        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.add_text.call_count, 3)
        self.assertEqual(self.cb.sent_ok, 3)
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.add_text.call_count, 3)

    async def test_engine_keeps_independently_approved_repeated_sections(self):
        from vlt.engine import Engine, EngineEvents
        from test_repeat_guard import _cfg

        engine = Engine(cfg=_cfg(), direction='mine', source='mic', sinks=set(),
            events=EngineEvents(on_text=lambda src, text, final: self.text(
                SimpleNamespace(source=src, display=text, is_final=final))))
        for _ in range(3):
            self.assertTrue(self.pipeline.submit('mine', bytes(320)))
            await asyncio.gather(*tuple(self.pipeline._owned))
            self.assertTrue(self.pipeline.consume('mine', engine._on_text, Mock()))
        self.assertEqual(self.drain(), 3)
        self.assertEqual(self.add_text.call_count, 3)

    async def test_engine_rejects_approval_for_other_direction(self):
        from vlt.engine import Engine, EngineEvents
        from test_repeat_guard import _cfg

        cfg = _cfg()
        cfg.directions['theirs'] = cfg.directions['mine']
        emitted = Mock()
        engine = Engine(cfg=cfg, direction='theirs', source='loopback', sinks=set(),
                        events=EngineEvents(on_text=emitted))
        self.assertTrue(self.pipeline.submit('mine', bytes(320)))
        await asyncio.gather(*tuple(self.pipeline._owned))
        self.pipeline.consume('mine', engine._on_text, Mock())
        emitted.assert_not_called()

    def test_legacy_unscoped_text_remains_supported(self):
        self.cb.send('legacy', True)
        self.q.put(('text', 'mine', 'source', 'legacy', True))
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.add_text.call_count, 1)


if __name__ == '__main__':
    unittest.main()
