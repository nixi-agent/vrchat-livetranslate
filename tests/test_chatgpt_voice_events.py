"""Bounded native event lifecycle; hand-written fixtures only."""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_voice_events import VoiceEvents
from chatgpt_voice_fakes import events, reflected, wire, usage


class NativeEventsTests(unittest.TestCase):
    def test_transcript_item_identity_is_separate_from_turn_identity(self):
        state = VoiceEvents()
        for channel in ('primary', 'sideband'):
            sequence = events()
            sequence[1]['item']['id'] = 'item_1'
            for event in sequence:
                state.consume(wire(event), channel)
        self.assertTrue(state.complete, 'separate item and turn IDs must remain paired within one session')

    def complete(self, state):
        for channel in ('primary', 'sideband'):
            for event in events():
                state.consume(wire(event), channel)

    def test_matching_two_channel_completion_then_owned_close(self):
        state = VoiceEvents()
        self.complete(state)
        self.assertTrue(state.complete)
        self.assertFalse(state.closed)
        state.request_close()
        for channel in ('primary', 'sideband'):
            state.consume(wire(dict(type='session.closed', reason='client_request', usage={})), channel)
        self.assertEqual(state.closed, {'primary', 'sideband'})

    def test_usage_and_reflection_never_establish_completion(self):
        state = VoiceEvents()
        state.consume(wire(events()[0]), 'sideband')
        state.consume(wire(usage()), 'sideband')
        self.assertEqual(state.consume(wire(reflected()), 'sideband').kind, 'audio-observation')
        self.assertFalse(state.complete)

    def test_observed_nested_usage_envelope_does_not_poison_completion(self):
        state = VoiceEvents()
        for channel in ('primary', 'sideband'):
            sequence = events()
            state.consume(wire(sequence[0]), channel)
            state.consume(wire(usage()), channel)
            for event in sequence[1:]:
                state.consume(wire(event), channel)
        self.assertTrue(state.complete)

    def test_usage_unknown_or_control_fields_still_poison(self):
        cases = []
        for root in (dict(unknown=1), dict(tools=[]), dict(handoff=None)):
            cases.append(dict(usage(), **root))
        for limit in (dict(status='limited', reset_seconds=1), dict(status=None),
                      dict(status=None, reset_seconds=None, unknown=0)):
            cases.append(dict(usage(), usage_limit=limit))
        for counters in (dict(audio_duration_ms=True, backend_model_usage=[]),
                         dict(audio_duration_ms=1, backend_model_usage=[{}]),
                         dict(audio_duration_ms=1, backend_model_usage=[], unknown=1)):
            cases.append(dict(usage(), usage=counters))
        for event in cases:
            with self.subTest(event=event):
                state = VoiceEvents()
                state.consume(wire(events()[0]), 'sideband')
                with self.assertRaises(RuntimeError):
                    state.consume(wire(event), 'sideband')
                self.assertFalse(state.complete)

    def test_observed_coalesced_input_is_not_candidate_pcm(self):
        state = VoiceEvents()
        state.consume(wire(events()[0]), 'sideband')
        event = dict(type='session.input_audio.append', audio=base64.b64encode(bytes(86400)).decode())
        self.assertEqual(state.consume(wire(event), 'sideband').kind, 'audio-observation')
        self.assertFalse(state.complete)

    def test_input_observation_total_is_independent_of_capture_budget(self):
        state = VoiceEvents()
        state.consume(wire(events()[0]), 'sideband')
        frame = dict(type='session.input_audio.append', audio=base64.b64encode(bytes(180000)).decode())
        for _ in range(32):
            state.consume(wire(frame), 'sideband')
        self.assertEqual(state.consume(wire(reflected()), 'sideband').kind, 'audio-observation')
        with self.assertRaises(RuntimeError):
            state.consume(wire(dict(frame, audio='AAA=')), 'sideband')
        self.assertFalse(state.complete)

    def test_coalesced_input_still_rejects_corruption_and_oversize_frames(self):
        for encoded in ('!', 'AQ==', base64.b64encode(bytes(192002)).decode()):
            state = VoiceEvents()
            state.consume(wire(events()[0]), 'sideband')
            with self.assertRaises(RuntimeError):
                state.consume(wire(dict(type='session.input_audio.append', audio=encoded)), 'sideband')
            self.assertFalse(state.complete)

    def test_closed_sideband_accepts_only_bounded_input_observation(self):
        state = VoiceEvents()
        self.complete(state)
        state.request_close()
        state.consume(wire(dict(type='session.closed', reason='client_request', usage={})), 'sideband')
        self.assertEqual(state.consume(wire(dict(type='session.input_audio.append', audio='AAA=')),
                                       'sideband').kind, 'audio-observation')
        self.assertEqual(state.closed, {'sideband'}, 'input echo cannot stand in for the primary close')
        self.assertTrue(state.complete)
        with self.assertRaises(RuntimeError):
            state.consume(wire(reflected()), 'sideband')
        self.assertFalse(state.complete)

    def test_coalesced_input_limits_do_not_relax_output_observations(self):
        state = VoiceEvents()
        state.consume(wire(events()[0]), 'sideband')
        with self.assertRaises(RuntimeError):
            state.consume(wire(dict(reflected(), delta=base64.b64encode(bytes(86400)).decode())), 'sideband')

    def test_observed_native_close_usage_empty_backend_list_is_metadata(self):
        state = VoiceEvents()
        self.complete(state)
        state.request_close()
        for channel in ('primary', 'sideband'):
            state.consume(wire(dict(type='session.closed', reason='client_request',
                usage=dict(audio_duration_ms=12500, backend_model_usage=[]))), channel)
        self.assertEqual(state.closed, {'primary', 'sideband'})

    def test_rejected_shapes_poison_the_candidate(self):
        cases = [dict(type='unknown'), dict(type='handoff', handoff=None),
                 dict(type='session.usage.updated', handoff={}),
                 dict(type='session.usage.updated', tools=[]),
                 dict(type='session.usage.updated', input_tokens='perform work'),
                 dict(type='session.usage.updated', input_tokens=True),
                 dict(type='session.usage.updated', backend_model_usage=[{}]),
                 dict(type='session.usage.updated', unknown_list=[]),
                 dict(type='session.closed', reason='client_request', usage={}),
                 dict(type='turn.delta', turn=dict(id='turn_1', role='assistant', delta='invented'))]
        for event in cases:
            with self.subTest(event=event):
                state = VoiceEvents()
                state.consume(wire(events()[0]), 'primary')
                with self.assertRaisesRegex(RuntimeError, 'voice-event'):
                    state.consume(wire(event), 'primary')
                self.assertTrue(state.invalid)
                self.assertFalse(state.complete)
                with self.assertRaises(RuntimeError):
                    state.consume(wire(events()[1]), 'primary')

    def test_mismatched_session_and_turn_are_rejected(self):
        for field in ('session', 'turn'):
            state = VoiceEvents()
            for event in events():
                state.consume(wire(event), 'primary')
            other = events()
            if field == 'session':
                other[0]['session']['id'] = 'session_2'
            else:
                other[2]['turn'] = dict(other[2]['turn'], id='turn_2')
            with self.assertRaises(RuntimeError):
                for event in other:
                    state.consume(wire(event), 'sideband')
            self.assertFalse(state.complete)

    def test_wrong_role_terminal_text_or_timing_never_completes(self):
        for updates in (dict(role='user'), dict(transcript='違う内容'), dict(end_ms=21)):
            state = VoiceEvents()
            for event in events():
                state.consume(wire(event), 'primary')
            other = events()
            other[-1]['turn'] = dict(other[-1]['turn'], **updates)
            with self.assertRaises(RuntimeError):
                for event in other:
                    state.consume(wire(event), 'sideband')
            self.assertFalse(state.complete)

    def test_delta_size_and_order_are_bounded(self):
        for update in (dict(delta='x' * 8193), dict(turn_id='wrong'), dict(start_ms=-1)):
            state = VoiceEvents()
            for event in events()[:3]:
                state.consume(wire(event), 'primary')
            event = dict(events()[3], **update)
            with self.assertRaises(RuntimeError):
                state.consume(wire(event), 'primary')
        state = VoiceEvents()
        for event in events()[:3]:
            state.consume(wire(event), 'primary')
        delta = dict(events()[3], delta='x' * 4097)
        state.consume(wire(delta), 'primary')
        with self.assertRaises(RuntimeError):
            state.consume(wire(delta), 'primary')

    def test_duplicate_terminal_and_events_after_close_are_rejected(self):
        state = VoiceEvents()
        self.complete(state)
        with self.assertRaises(RuntimeError):
            state.consume(wire(events()[-1]), 'primary')
        state = VoiceEvents()
        self.complete(state)
        state.request_close()
        closed = dict(type='session.closed', reason='client_request', usage={})
        state.consume(wire(closed), 'primary')
        with self.assertRaises(RuntimeError):
            state.consume(wire(closed), 'primary')

    def test_reflected_audio_is_bounded_and_not_accepted_on_primary(self):
        for channel, value in (('primary', reflected()), ('sideband', dict(reflected(), delta='!!')),
                               ('sideband', dict(reflected(), delta='AQ=='))):
            state = VoiceEvents()
            state.consume(wire(events()[0]), channel)
            with self.assertRaises(RuntimeError):
                state.consume(wire(value), channel)


if __name__ == '__main__':
    unittest.main()
