"""手寫訂閱 moderation metadata 契約；不含真實回應或錄音。"""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_chatgpt_events import sse, text_response
from vlt.session.chatgpt_events import response_text


def fixture():
    events = [json.loads(b[6:]) for b in text_response('translation').split(b'\n\n') if b]
    metadata = {'type': 'response.metadata', 'response_id': 'resp_1', 'metadata': {
        'openai_chatgpt_moderation_metadata': {
            'prompt': {'omnimod': {'protection_type': 'omnimod', 'outputs': [{
                'results': [{'category_flags': {'A1': False, 'REFUSED': False}}], 'is_blocked': False}]}},
            'generation': {}, 'tool_call': {}, 'tool_response': {}}}}
    events.insert(2, metadata)
    for sequence, event in enumerate(events):
        event['sequence_number'] = sequence
    events[-1]['response']['model'] = 'gpt-6-sol'
    return events


class MetadataTests(unittest.TestCase):
    def test_unblocked_moderation_metadata_preserves_completed_translation(self):
        self.assertEqual(response_text(sse(*fixture()), expected_model='gpt-6-sol'), 'translation')

    def test_generation_moderation_uses_same_strict_contract(self):
        events = fixture()
        metadata = events[2]['metadata']['openai_chatgpt_moderation_metadata']
        metadata['generation'] = copy.deepcopy(metadata['prompt'])
        self.assertEqual(response_text(sse(*events), expected_model='gpt-6-sol'), 'translation')

    def test_blocked_malformed_unknown_and_tool_metadata_are_rejected(self):
        def variants():
            for value in (True, None, 0, 'false'):
                e = fixture()
                e[2]['metadata']['openai_chatgpt_moderation_metadata']['prompt']['omnimod']['outputs'][0]['is_blocked'] = value
                yield e
            for key, value in (('type', 'unknown'), ('response_id', 'other'),
                               ('sequence_number', 1), ('metadata', []), ('tools', [{'type': 'function'}])):
                e = fixture()
                e[2][key] = value
                yield e
            for key, value in (('tool_call', {'called': True}), ('tool_response', {'text': 'answer'}),
                               ('generation', {'unknown': {}}), ('unknown', {})):
                e = fixture()
                e[2]['metadata']['openai_chatgpt_moderation_metadata'][key] = value
                yield e
            for key, value in (('outputs', []), ('protection_type', 'unknown')):
                e = fixture()
                e[2]['metadata']['openai_chatgpt_moderation_metadata']['prompt']['omnimod'][key] = value
                yield e
            for value in ([], {}, {'A1': 'false'}, {'A1': 0}):
                e = fixture()
                e[2]['metadata']['openai_chatgpt_moderation_metadata']['prompt']['omnimod']['outputs'][0]['results'][0]['category_flags'] = value
                yield e
            for key in ('response_id', 'sequence_number', 'metadata'):
                e = fixture()
                del e[2][key]
                yield e

        for events in variants():
            with self.subTest(event=events[2]), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                response_text(sse(*events), expected_model='gpt-6-sol')

    def test_metadata_never_substitutes_completion_or_correct_model(self):
        events = fixture()
        missing = events[:-1]
        wrong_model = copy.deepcopy(events)
        wrong_model[-1]['response']['model'] = 'other'
        late = events[:2] + events[3:] + [events[2]]
        wrong_label = sse(*events).replace(b'data: {"type": "response.metadata"',
            b'event: response.completed\ndata: {"type": "response.metadata"')
        for raw in (sse(*missing), sse(*wrong_model), sse(*late), wrong_label):
            with self.subTest(raw=raw[:80]), self.assertRaisesRegex(RuntimeError, '^event-invalid$'):
                response_text(raw, expected_model='gpt-6-sol')


if __name__ == '__main__':
    unittest.main()
