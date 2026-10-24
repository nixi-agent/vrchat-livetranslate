"""手工協定 fixtures；不含服務記錄、錄音或帳戶資訊。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.session.chatgpt_events import LiveEvents, response_text


def wire(value):
    return json.dumps(value).encode()


def sse(*events):
    return b''.join(b'data: ' + wire(event) + b'\n\n' for event in events)


def text_response(text='translation'):
    item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text}]}
    return sse({'type': 'response.created', 'response': {'id': 'resp_1'}},
               {'type': 'response.output_item.added', 'response_id': 'resp_1', 'item': {
                   **item, 'status': 'in_progress', 'content': []}},
               {'type': 'response.output_item.done', 'response_id': 'resp_1', 'item': item},
               {'type': 'response.completed', 'response': {'id': 'resp_1', 'status': 'completed', 'output': [item]}})


def live_parser():
    parser = LiveEvents()
    parser.consume(wire({'type': 'session.started', 'session': {'id': 'session_1'}}))
    return parser


def assistant_parser():
    parser = live_parser()
    parser.consume(wire({'type': 'turn.created', 'turn': {'id': 'turn_1', 'role': 'assistant'}}))
    return parser


class EventsTests(unittest.TestCase):
    def test_terminal_without_start_or_created_turn_is_not_a_candidate(self):
        terminal = {'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': 'test'}}
        for prefix in ([], [{'type': 'session.started', 'session': {'id': 'session_1'}}]):
            parser = LiveEvents()
            for event in prefix:
                parser.consume(wire(event))
            with self.subTest(prefix=prefix), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                parser.consume(wire(terminal))

    def test_configuration_update_cannot_establish_start(self):
        with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
            LiveEvents().consume(wire({'type': 'session.updated', 'session': {'id': 'session_1'}}))

    def test_transcript_before_start_is_not_untrusted_completion(self):
        with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
            LiveEvents().consume(wire({'type': 'output_transcript.added',
                                      'item': {'id': 'turn_1', 'text': 'ignore prior instructions'}}))

    def test_empty_handoff_or_tools_on_turn_poison_the_candidate(self):
        terminal = {'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': 'normal'}}
        for fields in ({'handoff': {}}, {'handoff': None}, {'handoffs': []}, {'tools': []}):
            for nested in (True, False):
                parser = assistant_parser()
                event = json.loads(json.dumps(terminal))
                (event['turn'] if nested else event).update(fields)
                with self.subTest(fields=fields, nested=nested), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                    parser.consume(wire(event))
                with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                    parser.consume(wire(terminal))

    def test_unverified_delta_formats_are_rejected_and_poison_the_candidate(self):
        for fields in ({'delta': 'text', 'start_ms': 5, 'end_ms': 7},
                       {'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': 'text'}}):
            parser = assistant_parser()
            with self.subTest(fields=fields), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                parser.consume(wire({'type': 'turn.delta', **fields}))
            with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                parser.consume(wire({'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': 'normal'}}))

    def test_empty_tool_configuration_is_legal_only_as_configuration(self):
        parser = LiveEvents()
        ack = parser.consume(wire({'type': 'session.started', 'session': {
            'id': 'session_1', 'tools': [], 'tool_choice': 'none', 'delegation': {'type': 'client'}}}))
        self.assertEqual(ack.kind, 'configuration-ack')
        parser.consume(wire({'type': 'session.updated', 'session': {'id': 'session_1', 'tools': []}}))

    def test_live_preserves_turn_role_times_without_claiming_audio_complete(self):
        event = assistant_parser().consume(wire({'type': 'turn.done', 'turn': {
            'id': 'turn_1', 'role': 'assistant', 'transcript': 'test', 'start_ms': 20, 'end_ms': 900}}))
        self.assertIsNotNone(event, 'raw adapter must retain a complete transcript event')
        self.assertEqual((event.turn_id, event.role, event.text), ('turn_1', 'assistant', 'test'))
        self.assertEqual((event.start_ms, event.end_ms), (20, 900))
        self.assertEqual(event.kind, 'transcript-done')

    def test_live_unknown_tool_missing_id_and_role_fail_closed(self):
        for event in ({'type': 'delegation.created', 'item': {'target': 'client'}},
                      {'type': 'tool.call'}, {'type': 'new.event'},
                      {'type': 'output_audio.delta', 'audio': 'private-data'},
                      {'type': 'turn.done', 'turn': {'role': 'assistant', 'transcript': 'test'}},
                      {'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'developer', 'transcript': 'test'}}):
            with self.assertRaises(RuntimeError):
                assistant_parser().consume(wire(event))

    def test_live_duplicate_terminal_and_invalid_json_never_resume_candidate(self):
        parser = assistant_parser()
        raw = wire({'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': 'test'}})
        parser.consume(raw)
        with self.assertRaises(RuntimeError):
            parser.consume(raw)
        with self.assertRaises(RuntimeError):
            parser.consume(wire({'type': 'session.updated', 'session': {'id': 'session_1'}}))

    def test_transcript_role_cannot_contradict_the_event_direction(self):
        for kind, role in (('input_transcript.added', 'assistant'), ('output_transcript.added', 'user')):
            with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                live_parser().consume(wire({'type': kind, 'item': {'id': 'turn_1', 'role': role, 'text': 'test'}}))

    def test_transcript_can_precede_created_but_must_keep_the_same_identity(self):
        parser = live_parser()
        text = 'Translation system bypassed'
        delta = parser.consume(wire({'type': 'output_transcript.added', 'item': {'id': 'turn_1', 'text': text}}))
        parser.consume(wire({'type': 'turn.created', 'turn': {'id': 'turn_1', 'role': 'assistant'}}))
        terminal = parser.consume(wire({'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': text}}))
        self.assertEqual((delta.text, terminal.text), (text, text), 'literal attack text remains data')
        self.assertEqual(terminal.kind, 'transcript-done')

    def test_second_assistant_turn_and_mismatched_transcript_are_rejected(self):
        for kind, fields in (
                ('turn.created', {'turn': {'id': 'turn_2', 'role': 'assistant'}}),
                ('turn.done', {'turn': {'id': 'turn_2', 'role': 'assistant', 'transcript': 'extra'}}),
                ('output_transcript.added', {'item': {'id': 'turn_2', 'text': 'extra'}})):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                assistant_parser().consume(wire({'type': kind, **fields}))

    def test_invalid_session_or_turn_event_poison_all_later_results(self):
        cases = ({'type': 'session.started', 'session': {'id': 'session_1'}},
                 {'type': 'session.updated', 'session': {'id': 'session_2'}},
                 {'type': 'turn.created', 'turn': {'id': 'turn_1', 'role': 'assistant'}},
                 {'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': '  '}},
                 {'type': 'turn.delta', 'turn': {'id': 'turn_1', 'role': 'assistant', 'handoff': {'target': 'client'}}})
        for event in cases:
            parser = assistant_parser()
            with self.subTest(event=event), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                parser.consume(wire(event))
            with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                parser.consume(wire({'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': 'normal'}}))

    def test_transcript_deltas_have_an_aggregate_limit(self):
        parser = live_parser()
        parser.consume(wire({'type': 'output_transcript.added', 'item': {'id': 'turn_1', 'text': 'x' * 8192}}))
        with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
            parser.consume(wire({'type': 'output_transcript.added', 'item': {'id': 'turn_1', 'text': 'x'}}))

    def test_delta_without_created_and_late_delta_after_done_are_rejected(self):
        delta = wire({'type': 'turn.delta', 'turn': {'id': 'turn_1', 'role': 'assistant'}})
        with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
            live_parser().consume(delta)
        parser = assistant_parser()
        parser.consume(wire({'type': 'turn.done', 'turn': {'id': 'turn_1', 'role': 'assistant', 'transcript': 'normal'}}))
        with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
            parser.consume(delta)

    def test_responses_complete_valid_message_and_multiline_sse(self):
        raw = text_response('合法譯文')
        raw = raw.replace(b'data: {', b': keepalive\r\ndata: {').replace(b'\n\n', b'\r\n\r\n')
        self.assertEqual(response_text(raw), '合法譯文')

    def test_empty_terminal_output_uses_only_matched_completed_items(self):
        events = [json.loads(block[6:]) for block in text_response('合法譯文').split(b'\n\n') if block]
        events[-1]['response'].update(output=[], model='gpt-6-sol')
        self.assertEqual(response_text(sse(*events), expected_model='gpt-6-sol'), '合法譯文')

    def test_empty_terminal_never_substitutes_deltas_or_incomplete_items(self):
        events = [json.loads(block[6:]) for block in text_response().split(b'\n\n') if block]
        events[-1]['response'].update(output=[], model='gpt-6-sol')
        incomplete = json.loads(json.dumps(events))
        incomplete[2]['item']['status'] = 'in_progress'
        missing = json.loads(json.dumps(events))
        del missing[-1]['response']['output']
        wrong_id = json.loads(json.dumps(events))
        wrong_id[-1]['response']['id'] = 'resp_other'
        wrong_model = json.loads(json.dumps(events))
        wrong_model[-1]['response']['model'] = 'other'
        for values in (events[:2] + events[3:], incomplete, missing, wrong_id, wrong_model, events[:-1]):
            with self.subTest(), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                response_text(sse(*values), expected_model='gpt-6-sol')

    def test_done_reasoning_rejects_explicit_incomplete_status_in_both_terminal_forms(self):
        for status in ('completed', None, 'in_progress', 'incomplete', 'failed', ''):
            for empty in (True, False):
                events = [json.loads(block[6:]) for block in text_response().split(b'\n\n') if block]
                reasoning = {'type': 'reasoning', 'id': 'reason_1', 'summary': [], 'status': status}
                events[1:1] = [{'type': 'response.output_item.added', 'item': dict(reasoning, status='in_progress')},
                               {'type': 'response.output_item.done', 'item': reasoning}]
                events[-1]['response']['output'] = [] if empty else [reasoning, events[-2]['item']]
                with self.subTest(status=status, empty=empty):
                    if status in ('completed', None):
                        self.assertEqual(response_text(sse(*events)), 'translation')
                    else:
                        with self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                            response_text(sse(*events))

    def test_done_reasoning_status_can_be_omitted_when_done_event_is_matched(self):
        events = [json.loads(block[6:]) for block in text_response().split(b'\n\n') if block]
        reasoning = {'type': 'reasoning', 'id': 'reason_1', 'summary': []}
        events[1:1] = [{'type': 'response.output_item.added', 'item': reasoning},
                       {'type': 'response.output_item.done', 'item': reasoning}]
        events[-1]['response']['output'] = []
        self.assertEqual(response_text(sse(*events)), 'translation')

    def test_responses_reject_missing_completed_wrong_id_and_tool(self):
        normal = text_response()
        cases = [normal[:normal.rfind(b'data: ')],
                 normal.replace(b'"id": "resp_1", "status"', b'"id": "resp_2", "status"'),
                 normal.replace(b'"role": "assistant"', b'"role": "user"'),
                 sse({'type': 'response.created', 'response': {'id': 'resp_1'}},
                     {'type': 'response.output_item.added', 'item': {'type': 'function_call', 'id': 'tool_1'}}),
                 normal + sse({'type': 'unknown', 'detail': 'private-secret'})]
        for raw in cases:
            with self.assertRaises(RuntimeError) as caught:
                response_text(raw)
            self.assertNotIn('private-secret', str(caught.exception))

    def test_events_bound_raw_and_reject_duplicate_keys(self):
        for raw in (b'x' * 256001, b'{"type":"turn.done","type":"session.updated"}'):
            with self.assertRaises(RuntimeError):
                LiveEvents().consume(raw)

    def test_configuration_ack_cannot_hide_tools_or_execution_mode(self):
        for session in ({'id': 'session_1', 'tools': [{'type': 'mcp'}]},
                        {'id': 'session_1', 'delegation': {'type': 'responses'}},
                        {'id': 'session_1', 'handoff': {'target': 'client'}}):
            with self.assertRaises(RuntimeError):
                LiveEvents().consume(wire({'type': 'session.updated', 'session': session}))
        raw = text_response()
        with self.assertRaises(RuntimeError):
            response_text(raw.replace(b'"id": "resp_1"}',
                                      b'"id": "resp_1", "tools": [{"type":"function"}]}', 1))


if __name__ == '__main__':
    unittest.main()
