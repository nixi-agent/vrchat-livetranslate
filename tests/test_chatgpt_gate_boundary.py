"""Actual gate/proxy/session handoff; fake inference, no hardware or network."""
import asyncio
import contextlib
import io
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_chatgpt_validated as validated
from vlt.engine import _LevelGate, _SessionProxy, _send_gated


class GateBoundaryTests(unittest.IsolatedAsyncioTestCase):
    setUp = validated.ValidatedTests.setUp
    session = validated.ValidatedTests.session
    on_text = validated.ValidatedTests.on_text
    on_audio = validated.ValidatedTests.on_audio

    def proxy(self, session):
        engine = SimpleNamespace(_session=session,
            _cfg=SimpleNamespace(session_base={'silence_gate_enabled': False}),
            _audio_in_chunks=0, _silent_chunks=0, _last_loud_ts=0)
        return engine, _SessionProxy(engine)

    async def capture(self, proxy, gate, chunks, offset=0):
        with contextlib.redirect_stdout(io.StringIO()):
            for i, chunk in enumerate(chunks):
                with patch('vlt.engine.time.monotonic', return_value=offset + i * .101):
                    await _send_gated(proxy, None, 'loopback', chunk, gate)

    async def settle(self):
        for _ in range(80):
            await asyncio.sleep(0)

    async def test_gate_close_seals_recorded_pcm_without_waiting_for_the_next_speaker(self):
        session = self.session('theirs', audio=False)
        await session.start(self.on_text)
        _, proxy = self.proxy(session)
        gate = _LevelGate(True, -45, 500, 250)
        speech = struct.pack('<h', 1000) * 1600
        quiet = bytes(3200)
        recorded = speech * 3 + quiet * 4
        await self.capture(proxy, gate, [speech] * 3 + [quiet] * 30)
        await self.settle()
        self.services.source_asr.assert_awaited_once_with(recorded, 'zh')
        self.assertFalse(gate.is_open)
        session.tick()
        self.assertEqual(len(self.text), 1)
        self.assertEqual(self.audio, [])
        self.assertEqual(proxy.sent_bytes, len(recorded))

    async def test_mine_boundary_still_waits_for_voice_and_audio_approval(self):
        session = self.session('mine', audio=True)
        await session.start(self.on_text, self.on_audio)
        _, proxy = self.proxy(session)
        check = asyncio.Future()
        async def approve(*_):
            return await check
        self.services.validate_audio.side_effect = approve
        speech, quiet = struct.pack('<h', 1000) * 1600, bytes(3200)
        await self.capture(proxy, _LevelGate(True, -45, 500, 250), [speech] * 3 + [quiet] * 20)
        await self.settle()
        session.tick()
        self.assertEqual((self.text, self.audio), ([], []))
        self.services.synthesize.assert_awaited_once()
        self.services.audio_asr.assert_awaited_once()
        self.services.validate_audio.assert_awaited_once()
        check.set_result(self.verdict)
        await self.settle()
        session.tick()
        self.assertEqual(len(self.text), 1)
        self.assertEqual(self.audio, [validated.VOICE])
        session.tick()
        self.assertEqual(len(self.audio), 1)

    async def test_rejected_check_is_not_bypassed_by_input_boundary(self):
        session = self.session('theirs', audio=False)
        await session.start(self.on_text)
        _, proxy = self.proxy(session)
        self.verdict['approved'] = False
        speech, quiet = struct.pack('<h', 1000) * 1600, bytes(3200)
        await self.capture(proxy, _LevelGate(True, -45, 500, 250), [speech] * 3 + [quiet] * 20)
        await self.settle()
        session.tick()
        self.services.source_asr.assert_awaited_once()
        self.assertEqual((self.text, self.audio), ([], []))
        self.assertEqual(session.diagnostics()['last_reason'], 'text-rejected')

    async def test_reopening_and_duplicate_boundary_do_not_replay_the_previous_section(self):
        session = self.session('theirs', audio=False)
        await session.start(self.on_text)
        _, proxy = self.proxy(session)
        gate = _LevelGate(True, -45, 500, 250)
        first, second, quiet = struct.pack('<h', 1000) * 1600, struct.pack('<h', 2000) * 1600, bytes(3200)
        await self.capture(proxy, gate, [first] * 3 + [quiet] * 20)
        session.end_input_segment()
        await self.settle()
        session.tick()
        await self.capture(proxy, gate, [second] * 3 + [quiet] * 20, offset=4)
        session.end_input_segment()
        await self.settle()
        session.tick()
        self.assertEqual(self.services.source_asr.await_count, 2)
        self.assertEqual(self.services.source_asr.await_args_list[0].args[0], first * 3 + quiet * 4)
        self.assertEqual(self.services.source_asr.await_args_list[1].args[0], quiet * 2 + second * 3 + quiet * 4)
        self.assertEqual(len(self.text), 2)

    async def test_account_change_at_gate_close_does_not_submit_old_pcm(self):
        session = self.session('theirs', audio=False)
        await session.start(self.on_text)
        _, proxy = self.proxy(session)
        gate = _LevelGate(True, -45, 500, 250)
        speech, quiet = struct.pack('<h', 1000) * 1600, bytes(3200)
        await self.capture(proxy, gate, [speech] * 3)
        self.owner.generation += 1
        await self.capture(proxy, gate, [quiet] * 20, offset=.303)
        await self.settle()
        self.services.source_asr.assert_not_awaited()
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_replacement_session_does_not_receive_the_previous_owner_pcm(self):
        old = self.session('theirs', audio=False)
        new = self.session('theirs', audio=False)
        await old.start(self.on_text)
        await new.start(self.on_text)
        engine, proxy = self.proxy(old)
        gate = _LevelGate(True, -45, 500, 250)
        speech, quiet = struct.pack('<h', 1000) * 1600, bytes(3200)
        await self.capture(proxy, gate, [speech] * 3)
        engine._session = new
        await self.capture(proxy, gate, [quiet] * 20, offset=.303)
        await self.settle()
        self.services.source_asr.assert_not_awaited()
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_closed_session_cannot_be_flushed_into_an_output(self):
        session = self.session('theirs', audio=False)
        await session.start(self.on_text)
        _, proxy = self.proxy(session)
        gate = _LevelGate(True, -45, 500, 250)
        speech = struct.pack('<h', 1000) * 1600
        await self.capture(proxy, gate, [speech] * 3)
        await session.close()
        await self.capture(proxy, gate, [bytes(3200)] * 20, offset=.303)
        await self.settle()
        self.services.source_asr.assert_not_awaited()
        self.assertEqual((self.text, self.audio), ([], []))

    async def test_other_provider_keeps_its_exact_existing_audio_stream(self):
        other = SimpleNamespace(cfg=SimpleNamespace(provider='bailian'), send_audio=AsyncMock(),
                                note_voice=Mock(), end_input_segment=Mock())
        _, proxy = self.proxy(other)
        speech, quiet = struct.pack('<h', 1000) * 1600, bytes(3200)
        await self.capture(proxy, _LevelGate(True, -45, 500, 250), [speech] * 3 + [quiet] * 20)
        self.assertEqual(b''.join(call.args[0] for call in other.send_audio.await_args_list), speech * 3 + quiet * 4)
        other.end_input_segment.assert_not_called()


if __name__ == '__main__':
    unittest.main()
