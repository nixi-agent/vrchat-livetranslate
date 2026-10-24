"""Bounded voice failure phases without leaking scripts, credentials or exception payloads."""
from contextlib import redirect_stdout
import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_candidate import CandidateBridge
from vlt.session.chatgpt_relay import SubscriptionRelay
from vlt.session.chatgpt_voice import NativeVoice
from chatgpt_voice_fakes import NativeFixture, PCM, TAIL, TEXT


class VoiceDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, mode):
        fixture = NativeFixture()
        owner = SimpleNamespace(generation=0, require_usable=Mock(), credentials=AsyncMock(
            return_value=SimpleNamespace(generation=0, account='offline-account', access='offline-token')))
        relay = SubscriptionRelay(owner)
        boundary_errors = {
            'handler-os-error': OSError('Bearer credential-test'),
            'handler-timeout': TimeoutError('Bearer credential-test'),
            'handler-handshake': InvalidStatus(Response(403, 'credential-test', Headers(), b'credential-test')),
        }
        async def start(bridge, profile):
            if mode in boundary_errors:
                fixture.profile = profile
                class LocalSocket:
                    def __aiter__(self):
                        async def offers():
                            yield json.dumps(dict(type='offer', sdp='v=0\r\n'))
                        return offers()

                    async def send(self, raw):
                        fixture.steps.append(json.loads(raw)['type'])

                await asyncio.create_task(bridge._handle(LocalSocket()))
            else:
                await fixture.start(bridge, profile)
        connect = fixture.connect
        if mode == 'browser-start':
            fixture.request = Mock(side_effect=RuntimeError('Bearer credential-test'))
        elif mode == 'sideband-connect':
            connect = Mock(side_effect=OSError('Bearer credential-test'))
        elif mode in boundary_errors:
            connect = Mock(side_effect=boundary_errors[mode])
        elif mode == 'turn-complete':
            fixture.secondary.insert(1, dict(type='untrusted-event', payload='credential-test'))
        elif mode == 'turn-shape':
            fixture.secondary[2]['turn'] = dict(fixture.secondary[2]['turn'])
            del fixture.secondary[2]['turn']['role']
        elif mode == 'websocket-close':
            close = fixture.close
            async def invalid_close():
                await close()
                fixture.protocol.close_rcvd = SimpleNamespace(code=1006)
            fixture.close = invalid_close
        output = io.StringIO()
        cleanup = NativeVoice._cleanup
        async def failing_cleanup(voice):
            await cleanup(voice)
            raise OSError('Bearer credential-test')
        with redirect_stdout(output), patch.object(CandidateBridge, 'start', start), \
                patch('vlt.session.chatgpt_relay.request', side_effect=fixture.request), \
                patch('vlt.session.chatgpt_voice.client.connect', side_effect=connect), \
                patch.object(NativeVoice, '_cleanup', failing_cleanup if mode == 'cleanup' else cleanup):
            voice = NativeVoice(relay)
            if mode == 'capture-seal':
                with patch.object(CandidateBridge, 'seal', AsyncMock(side_effect=RuntimeError('capture-ack'))):
                    with self.assertRaises(RuntimeError):
                        await voice.generate(TEXT, 'ja')
            elif mode == 'normal':
                _, pcm = await voice.generate(TEXT, 'ja')
                self.assertEqual(pcm, PCM + TAIL)
            else:
                with self.assertRaises(RuntimeError):
                    await voice.generate(TEXT, 'ja')
        self.assertFalse(Path(fixture.profile).exists())
        if mode in boundary_errors:
            self.assertNotIn('answer', fixture.steps)
            self.assertTrue(voice.connecting.done())
            self.assertTrue(voice.bridge.handler.done())
            self.assertIsNone(voice.reader)
        return output.getvalue()

    async def test_failures_name_actual_phase_without_payloads(self):
        for phase in ('browser-start', 'sideband-connect', 'turn-complete',
                      'websocket-close', 'capture-seal', 'cleanup'):
            with self.subTest(phase=phase):
                log = await self.generate(phase)
                self.assertIn('[subscription-voice] rejected: phase=' + phase, log)
                self.assertNotIn('credential-test', log)
                self.assertNotIn('offline-token', log)
                self.assertNotIn(TEXT, log)
                self.assertNotIn('wss://', log)
                self.assertLess(len(log), 256)

    async def test_normal_voice_is_unchanged_and_quiet(self):
        self.assertEqual(await self.generate('normal'), '')

    async def test_schema_failure_names_the_event_and_check_without_payloads(self):
        log = await self.generate('turn-shape')
        self.assertRegex(log, r'event=turn.created@_fields:\d+')
        self.assertNotIn(TEXT, log)
        self.assertNotIn('offline-token', log)
        self.assertLess(len(log), 256)

    async def test_unknown_event_name_is_redacted(self):
        log = await self.generate('turn-complete')
        self.assertRegex(log, r'event=unknown@consume:\d+')
        self.assertNotIn('untrusted-event', log)
        self.assertNotIn('credential-test', log)
        self.assertLess(len(log), 256)

    async def test_bundled_module_path_keeps_the_actual_check(self):
        with patch('vlt.session.chatgpt_voice_events.__file__', '/bundle/voice_events.py'):
            log = await self.generate('turn-shape')
        self.assertRegex(log, r'event=turn.created@_fields:\d+')

    async def test_first_invalid_payload_diagnostic_survives_later_events(self):
        owner = SimpleNamespace(generation=0, require_usable=Mock())
        voice = NativeVoice(SimpleNamespace(owner=owner))
        voice._receive('{"type":"credential-test",', 'sideband')
        output = io.StringIO()
        with redirect_stdout(output):
            voice._diagnose('turn-complete', RuntimeError('voice-event'))
        log = output.getvalue()
        self.assertRegex(log, r'event=unknown@consume:\d+')
        self.assertNotIn('credential-test', log)
        first = voice.events.rejection
        with self.assertRaisesRegex(RuntimeError, 'voice-event'):
            voice.events.consume('{}', 'primary')
        self.assertEqual(voice.events.rejection, first)
        self.assertFalse(voice.events.complete)
        await voice.close()

    async def test_actual_handler_preserves_one_safe_handshake_cause(self):
        for mode, cause in (('handler-os-error', 'os-error'), ('handler-timeout', 'timeout'),
                            ('handler-handshake', 'handshake')):
            with self.subTest(mode=mode):
                log = await self.generate(mode)
                self.assertIn('phase=sideband-connect cause=' + cause, log)
                self.assertEqual(log.count('[subscription-voice] rejected:'), 1)
                self.assertNotIn('credential-test', log)
                self.assertNotIn('offline-token', log)
                self.assertNotIn(TEXT, log)


if __name__ == '__main__':
    unittest.main()
