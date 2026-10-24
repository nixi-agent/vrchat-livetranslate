"""Approval cutover with an owned HTTP task still pending; offline only."""
import asyncio
import sys
import unittest
from test_chatgpt_reserved_pipeline import CoordinatorTests, PCM, TAIL


class ApprovalBoundaryTests(CoordinatorTests):
    async def pending_approval(self, failure):
        self.request_failure = failure
        self.request_release = asyncio.Event()
        await self.prepare_pending()
        self.text_check.set_result(self.verdict)
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_late_unsent_recovers_with_full_original_clocks(self):
        await self.pending_approval('unsent')
        self.assertEqual(self.timers, [], 'decide unused reservation before voice clocks')
        self.request_release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.pipeline.consume('mine', self.text.append, self.audio.append))
        self.assertEqual(self.audio, [PCM+TAIL])
        self.assertEqual(self.timers, [('generation', 120), ('generation', 120), ('ready', 90)])

    async def test_cancel_while_joining_never_starts_cold_generation(self):
        await self.pending_approval('unsent')
        closing = asyncio.create_task(self.pipeline.close())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(closing.done(), 'close must join owned pending HTTP')
            self.assertEqual(self.timers, [])
        finally:
            self.request_release.set()
            await asyncio.wait_for(closing, 3)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all(f.context is None for f in self.fixtures))
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))

    async def test_owner_change_while_joining_never_starts_cold_generation(self):
        await self.pending_approval('unsent')
        self.owner.generation += 1
        self.request_release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(self.timers, [])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all(f.context is None for f in self.fixtures))
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))

    async def test_late_unknown_failure_never_recovers(self):
        await self.pending_approval('uncertain')
        self.request_release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.pipeline._owned)), 3)
        self.assertEqual(self.timers, [])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all(f.context is None for f in self.fixtures))
        self.assertFalse(self.pipeline.consume('mine', self.text.append, self.audio.append))


if __name__ == '__main__':
    names = [name for name in ApprovalBoundaryTests.__dict__ if name.startswith('test_')]
    suite = unittest.TestSuite(ApprovalBoundaryTests(name) for name in names)
    sys.exit(not unittest.TextTestRunner().run(suite).wasSuccessful())
