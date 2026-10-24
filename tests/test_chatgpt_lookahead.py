"""One private successor may overlap; publication and revocation remain ordered."""
import asyncio
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_chatgpt_backpressure as fixtures
from vlt.session.base import SessionConfig
from vlt.session.chatgpt_pipeline import Coordinator


class LookaheadTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BackpressureTests.asyncSetUp
    consumer = fixtures.BackpressureTests.consumer
    session = fixtures.BackpressureTests.session
    settle = fixtures.BackpressureTests.settle

    async def setup_pipeline(self):
        session, engine, services, _ = await self.session(self.consumer('VirtualMic'), count=0)
        services.synthesize.side_effect = self.candidate
        return session, engine, services

    def candidate(self, text, _):
        return 'candidate_' + text, struct.pack('<h', int(text[-1]) * 1000) * 7200

    def submit(self, session, index):
        return session._pipeline.submit('mine', bytes([index, 0]) * 160)

    async def release_outputs(self, session):
        for _ in range(8):
            session.tick()
            await self.settle()

    async def test_only_one_successor_overlaps_and_cannot_overtake_the_head(self):
        session, engine, services = await self.setup_pipeline()
        release = asyncio.Event()
        self.addCleanup(release.set)
        calls, captions = [], []
        async def voice(text, target):
            calls.append(text)
            if text == 'line1':
                await release.wait()
            return self.candidate(text, target)
        services.synthesize.side_effect = voice
        emit = session.on_text
        session.on_text = lambda delta: (captions.append(delta.confirmed), emit(delta))
        for index in (1, 2, 3):
            self.assertTrue(self.submit(session, index))
        self.assertFalse(self.submit(session, 4), 'the existing admission bound must not grow')
        await self.settle()
        self.assertEqual(services.source_asr.call_count, 2,
            'successor ASR must run before the first voice finishes, but the third must wait')
        self.assertEqual(calls, ['line1', 'line2'])
        session.tick()
        self.assertEqual((engine._text_deltas, captions), (0, []))
        release.set()
        await self.release_outputs(session)
        self.assertEqual(captions, ['line1', 'line2', 'line3'])
        self.assertEqual(services.synthesize.call_count, 3)
        self.assertEqual(session._last_reason, 'queue-overload')

    async def test_failed_head_does_not_discard_or_publish_successor_early(self):
        session, engine, services = await self.setup_pipeline()
        release = asyncio.Event()
        self.addCleanup(release.set)
        completed = []
        async def voice(text, target):
            if text == 'line1':
                await release.wait()
                raise RuntimeError('failed head')
            completed.append(text)
            return self.candidate(text, target)
        services.synthesize.side_effect = voice
        self.submit(session, 1)
        self.submit(session, 2)
        await self.settle()
        self.assertEqual(completed, ['line2'])
        session.tick()
        self.assertEqual(engine._text_deltas, 0)
        release.set()
        await self.release_outputs(session)
        self.assertEqual((engine._text_deltas, session._rejected), (1, 1))
        self.assertEqual(session._last_reason, 'voice-failed')
        self.assertEqual(services.synthesize.call_count, 2)

    async def test_submit_when_last_prefetched_result_finishes_is_not_lost(self):
        session, engine, services = await self.setup_pipeline()
        release = asyncio.Event()
        self.addCleanup(release.set)
        async def voice(text, target):
            if text == 'line1':
                await release.wait()
            return self.candidate(text, target)
        services.synthesize.side_effect = voice
        self.submit(session, 1)
        self.submit(session, 2)
        await self.settle()
        async def newcomer():
            while len(session._pipeline._ready['mine']) < 2:
                await asyncio.sleep(0)
            self.assertTrue(self.submit(session, 3))
        arriving = asyncio.create_task(newcomer())
        release.set()
        await asyncio.wait_for(arriving, 1)
        await self.release_outputs(session)
        self.assertEqual(engine._text_deltas, 3, 'newly admitted work must survive worker completion')

    async def test_reset_keeps_direction_owned_until_both_stage_cleanups_finish(self):
        session, engine, services = await self.setup_pipeline()
        releases = [asyncio.Event(), asyncio.Event()]
        for release in releases:
            self.addCleanup(release.set)
        entered, cleaning = [], []
        async def asr(raw, _):
            index = raw[0] - 1
            if index < 2:
                entered.append(index)
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.append(index)
                    await releases[index].wait()
            return 'line3'
        services.source_asr.side_effect = asr
        self.submit(session, 1)
        self.submit(session, 2)
        await self.settle()
        self.assertEqual(entered, [0, 1])
        session._pipeline.invalidate()
        await self.settle()
        self.assertEqual(set(cleaning), {0, 1})
        releases[0].set()
        await self.settle()
        self.assertFalse(self.submit(session, 3), 'the cancelled successor still owns resources')
        self.assertEqual(services.source_asr.call_count, 2)
        releases[1].set()
        await self.settle()
        self.assertTrue(self.submit(session, 3))
        await self.release_outputs(session)
        self.assertEqual(engine._text_deltas, 1)
        self.assertFalse(session._pipeline._owned)

    async def test_close_waits_for_both_voices_despite_repeated_close_cancellation(self):
        session, engine, services = await self.setup_pipeline()
        release = asyncio.Event()
        self.addCleanup(release.set)
        entered, cleaning = [], []
        async def voice(text, target):
            entered.append(text)
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.append(text)
                await release.wait()
        services.synthesize.side_effect = voice
        self.submit(session, 1)
        self.submit(session, 2)
        await self.settle()
        self.assertEqual(entered, ['line1', 'line2'])
        closing = asyncio.create_task(session.close())
        try:
            await self.settle()
            self.assertEqual(set(cleaning), {'line1', 'line2'})
            closing.cancel()
            await self.settle()
            closing.cancel()
            await self.settle()
            self.assertFalse(closing.done())
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
        self.assertFalse(session._pipeline._owned)
        self.assertEqual(engine._text_deltas, 0)

    async def test_unconsumed_failed_successor_is_reaped_without_unhandled_exception(self):
        session, engine, services = await self.setup_pipeline()
        release = asyncio.Event()
        self.addCleanup(release.set)
        calls, errors = [], []
        loop = asyncio.get_running_loop()
        handler = loop.get_exception_handler()
        self.addCleanup(loop.set_exception_handler, handler)
        loop.set_exception_handler(lambda _, context: errors.append(context))
        async def voice(text, target):
            calls.append(text)
            if text == 'line1':
                await release.wait()
                return self.candidate(text, target)
            raise RuntimeError('failed private successor')
        services.synthesize.side_effect = voice
        self.submit(session, 1)
        self.submit(session, 2)
        await self.settle()
        self.assertEqual(calls, ['line1', 'line2'])
        await session.close()
        await self.settle()
        self.assertFalse(errors)
        self.assertFalse(session._pipeline._owned)
        self.assertEqual(engine._text_deltas, 0)

    async def test_cancel_before_worker_entry_reaps_prefetch_and_allows_new_input(self):
        session, engine, services = await self.setup_pipeline()
        self.submit(session, 1)
        self.submit(session, 2)
        session._pipeline.invalidate()
        await self.settle()
        self.assertEqual(services.source_asr.call_count, 0)
        self.assertFalse(session._pipeline._owned)
        self.assertFalse(session._pipeline._lookahead)
        self.assertTrue(self.submit(session, 3))
        await self.release_outputs(session)
        self.assertEqual(engine._text_deltas, 1)

    async def test_failed_prefetched_source_is_reported_once_after_the_head(self):
        session, engine, services = await self.setup_pipeline()
        release = asyncio.Event()
        self.addCleanup(release.set)
        async def voice(text, target):
            await release.wait()
            return self.candidate(text, target)
        async def source(raw, _):
            if raw[0] == 2:
                raise RuntimeError('failed source')
            return 'line1'
        services.source_asr.side_effect = source
        services.synthesize.side_effect = voice
        self.submit(session, 1)
        self.submit(session, 2)
        await self.settle()
        self.assertEqual(services.source_asr.call_count, 2)
        self.assertEqual((engine._text_deltas, session._rejected), (0, 0))
        release.set()
        await self.release_outputs(session)
        self.assertEqual((engine._text_deltas, session._rejected), (1, 1))
        self.assertEqual(session._last_reason, 'source-asr-failed')
        self.assertEqual(services.synthesize.call_count, 1)

    async def test_blocked_mine_pair_does_not_block_theirs_pair(self):
        session, _, services = await self.setup_pipeline()
        release = asyncio.Event()
        self.addCleanup(release.set)
        async def voice(text, target):
            if text == 'line1':
                await release.wait()
            return self.candidate(text, target)
        services.synthesize.side_effect = voice
        theirs = SessionConfig(provider='chatgpt', source_lang='en', target_lang='en', output_audio=False)
        pipeline = Coordinator({'mine': session.cfg, 'theirs': theirs}, services, lambda *_: None, lambda: 1)
        self.addAsyncCleanup(pipeline.close)
        try:
            for direction, ids in [('mine', (1, 2)), ('theirs', (3, 4))]:
                for index in ids:
                    self.assertTrue(pipeline.submit(direction, bytes([index, 0]) * 160))
            await self.settle()
            mine_text, their_text, audio = [], [], []
            self.assertFalse(pipeline.consume('mine', mine_text.append, audio.append))
            while pipeline.consume('theirs', their_text.append, audio.append):
                pass
            self.assertEqual([delta.confirmed for delta in their_text], ['line3', 'line4'])
            self.assertEqual((mine_text, audio), ([], []))
            self.assertEqual(services.source_asr.call_count, 4)
            self.assertEqual(services.synthesize.call_count, 2)
        finally:
            release.set()
            await pipeline.close()


if __name__ == '__main__':
    unittest.main()
