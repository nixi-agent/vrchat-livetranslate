"""有界原始事件，不使用會丟掉識別資訊的 CLI realtime parser。"""
from dataclasses import dataclass
import json
import math
import re


def _invalid():
    raise RuntimeError('event-invalid')


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            _invalid()
        result[key] = value
    return result


def bounded_json(raw):
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 256000:
            _invalid()
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: _invalid())
        if not isinstance(value, dict):
            _invalid()
        return value
    except Exception:
        _invalid()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
        _invalid()
    return value


def _text(value):
    if not isinstance(value, str) or len(value.encode('utf-8')) > 8192:
        _invalid()
    return value


def _tool_free(value, *, allow_empty_tools=True):
    if isinstance(value, dict):
        if (('tools' in value and (not allow_empty_tools or value['tools'] != []))
                or 'handoff' in value or 'handoffs' in value
                or value.get('tool_choice') not in (None, 'none')):
            _invalid()
        delegation = value.get('delegation')
        if delegation is not None and (not isinstance(delegation, dict) or delegation.get('type') != 'client'):
            _invalid()
        for child in value.values():
            _tool_free(child, allow_empty_tools=allow_empty_tools)
    elif isinstance(value, list):
        for child in value:
            _tool_free(child, allow_empty_tools=allow_empty_tools)


@dataclass(frozen=True)
class RawEvent:
    kind: str
    turn_id: str
    role: str = ''
    text: str = ''
    start_ms: float | None = None
    end_ms: float | None = None


class LiveEvents:
    def __init__(self):
        self._done = set()
        self._created = set()
        self._turns = {}
        self._transcript_bytes = {}
        self._session = None
        self._invalid = False

    def consume(self, raw):
        try:
            if self._invalid:
                _invalid()
            event = bounded_json(raw)
            kind = event.get('type')
            _tool_free(event, allow_empty_tools=kind in ('session.started', 'session.updated'))
            if kind in ('session.started', 'session.updated'):
                session = event.get('session')
                if not isinstance(session, dict):
                    _invalid()
                session_id = _id(session.get('id'))
                if (kind == 'session.started' and self._session is not None
                        or kind == 'session.updated' and self._session != session_id):
                    _invalid()
                self._session = session_id
                return RawEvent('configuration-ack', session_id)
            if self._session is None:
                _invalid()
            if kind in ('input_transcript.added', 'output_transcript.added'):
                item = event.get('item')
                if not isinstance(item, dict):
                    _invalid()
                role = 'user' if kind == 'input_transcript.added' else 'assistant'
                if item.get('role', role) != role:
                    _invalid()
                turn_id = _id(item.get('id'))
                if (role, turn_id) in self._done or self._turns.get(role, turn_id) != turn_id:
                    _invalid()
                text = _text(item.get('text'))
                size = self._transcript_bytes.get(role, 0) + len(text.encode('utf-8'))
                if size > 8192:
                    _invalid()
                self._turns[role] = turn_id
                self._transcript_bytes[role] = size
                return RawEvent('transcript-delta', turn_id, role, text)
            if kind not in ('turn.created', 'turn.done'):
                _invalid()  # 包含 tool/handoff 與缺乏歸屬的 output_audio.delta。
            turn = event.get('turn')
            if not isinstance(turn, dict) or turn.get('role') not in ('user', 'assistant'):
                _invalid()
            turn_id, role = _id(turn.get('id')), turn['role']
            identity = (role, turn_id)
            if identity in self._done or self._turns.get(role, turn_id) != turn_id:
                _invalid()
            if kind == 'turn.created':
                if identity in self._created:
                    _invalid()
                self._turns[role] = turn_id
                self._created.add(identity)
            elif identity not in self._created:
                _invalid()
            start, end = turn.get('start_ms'), turn.get('end_ms')
            for stamp in (start, end):
                if stamp is not None and (type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= stamp <= 3600000):
                    _invalid()
            if start is not None and end is not None and end < start:
                _invalid()
            text = _text(turn.get('transcript', ''))
            if kind == 'turn.done' and not text.strip():
                _invalid()
            result = RawEvent('turn-created' if kind == 'turn.created' else 'transcript-done',
                              turn_id, role, text, start, end)
            if kind == 'turn.done':
                self._done.add(identity)
            return result
        except Exception:
            self._invalid = True
            _invalid()


def response_text(raw, expected_model=None):
    try:
        return _response_text(raw, expected_model)
    except Exception:
        _invalid()


def _moderation_metadata(event, response_id):
    if (set(event) != {'type', 'response_id', 'sequence_number', 'metadata'}
            or event['response_id'] != response_id or not isinstance(event['metadata'], dict)
            or set(event['metadata']) != {'openai_chatgpt_moderation_metadata'}):
        _invalid()
    metadata = event['metadata']['openai_chatgpt_moderation_metadata']
    if (not isinstance(metadata, dict) or set(metadata) != {'prompt', 'generation', 'tool_call', 'tool_response'}
            or metadata['tool_call'] != {} or metadata['tool_response'] != {}):
        _invalid()
    for phase in ('prompt', 'generation'):
        value = metadata[phase]
        if not isinstance(value, dict):
            _invalid()
        if value == {}:
            continue
        if set(value) != {'omnimod'} or not isinstance(value['omnimod'], dict):
            _invalid()
        moderation = value['omnimod']
        if (set(moderation) != {'protection_type', 'outputs'} or moderation['protection_type'] != 'omnimod'
                or not isinstance(moderation['outputs'], list) or not 1 <= len(moderation['outputs']) <= 32):
            _invalid()
        for output in moderation['outputs']:
            if (not isinstance(output, dict) or set(output) != {'results', 'is_blocked'}
                    or output['is_blocked'] is not False or not isinstance(output['results'], list)
                    or not 1 <= len(output['results']) <= 32):
                _invalid()
            for result in output['results']:
                if not isinstance(result, dict) or set(result) != {'category_flags'}:
                    _invalid()
                flags = result['category_flags']
                if (not isinstance(flags, dict) or not 1 <= len(flags) <= 128
                        or any(type(flag) is not bool for flag in flags.values())):
                    _invalid()


def _response_text(raw, expected_model):
    if not isinstance(raw, bytes) or not raw or len(raw) > 256000:
        _invalid()
    body = raw.decode('utf-8').replace('\r\n', '\n')
    if not body.endswith('\n\n'):
        _invalid()
    response_id = None
    items, completed_items = {}, {}
    result = None
    last_sequence = -1
    for block in body.split('\n\n'):
        data, label = [], None
        for line in block.splitlines():
            if line.startswith(':') or not line:
                continue
            if line.startswith('data:'):
                data.append(line[5:].lstrip(' '))
            elif line.startswith('event:') and label is None:
                label = line[6:].strip()
            else:
                _invalid()
        if not data:
            continue
        payload = '\n'.join(data)
        if payload == '[DONE]' and result is not None:
            continue
        if result is not None:
            _invalid()
        event = bounded_json(payload.encode())
        _tool_free(event)
        kind = event.get('type')
        if label is not None and label != kind:
            _invalid()
        if 'sequence_number' in event:
            sequence = event['sequence_number']
            if type(sequence) is not int or sequence <= last_sequence:
                _invalid()
            last_sequence = sequence
        if kind == 'response.created':
            if response_id is not None:
                _invalid()
            response_id = _id(event.get('response', {}).get('id'))
            continue
        if response_id is None or event.get('response_id', response_id) != response_id:
            _invalid()
        if kind == 'response.in_progress':
            if event.get('response', {}).get('id') != response_id:
                _invalid()
        elif kind == 'response.metadata':
            _moderation_metadata(event, response_id)
        elif kind in ('response.output_item.added', 'response.output_item.done'):
            item = event.get('item')
            item_id = _checked_item(item)
            if kind.endswith('added'):
                if item_id in items:
                    _invalid()
                items[item_id] = item
            else:
                if (item_id not in items or item_id in completed_items
                        or items[item_id]['type'] != item['type']
                        or item.get('status') not in (None, 'completed')):
                    _invalid()
                completed_items[item_id] = item
        elif kind in ('response.output_text.delta', 'response.output_text.done',
                      'response.content_part.added', 'response.content_part.done',
                      'response.reasoning_summary_text.delta', 'response.reasoning_summary_text.done',
                      'response.reasoning_summary_part.added', 'response.reasoning_summary_part.done'):
            item_id = _id(event.get('item_id'))
            if item_id not in items or item_id in completed_items:
                _invalid()
            if kind.startswith('response.output_text'):
                if items[item_id].get('role') != 'assistant':
                    _invalid()
                _text(event.get('delta' if kind.endswith('delta') else 'text'))
            if 'part' in event and event['part'].get('type') not in ('output_text', 'summary_text'):
                _invalid()
        elif kind == 'response.completed':
            response = event.get('response', {})
            if response.get('id') != response_id or response.get('status') != 'completed':
                _invalid()
            if expected_model is not None and response.get('model') != expected_model:
                _invalid()
            output = response.get('output')
            if output == []:
                output = list(completed_items.values())
            if not isinstance(output, list) or not output or len(output) > 32:
                _invalid()
            identifiers = [_checked_item(item) for item in output]
            if len(set(identifiers)) != len(output) or set(identifiers) != set(items) or set(items) != set(completed_items):
                _invalid()
            if any(completed_items[item['id']] != item for item in output):
                _invalid()
            messages = [item for item in output if item['type'] == 'message']
            if len(messages) != 1 or messages[0].get('status') != 'completed':
                _invalid()
            result = ''.join(_text(c.get('text')) for c in messages[0]['content'])
            if not result or len(result.encode()) > 8192:
                _invalid()
        else:
            _invalid()
    if result is None:
        _invalid()
    return result


def _checked_item(item):
    if not isinstance(item, dict) or item.get('type') not in ('message', 'reasoning'):
        _invalid()
    if item['type'] == 'message':
        content = item.get('content')
        if item.get('role') != 'assistant' or not isinstance(content, list) or len(content) > 32:
            _invalid()
        if any(not isinstance(c, dict) or c.get('type') != 'output_text' for c in content):
            _invalid()
    return _id(item.get('id'))
