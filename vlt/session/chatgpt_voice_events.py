"""窄 AVAS 候選事件契約；反射 PCM 只驗格式，絕不播放或拼補。"""
import asyncio
import base64
import math
import re

from .chatgpt_events import LiveEvents, RawEvent, bounded_json, _id, _text, _tool_free


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise RuntimeError('voice-event')


def _times(event):
    start, end = event['start_ms'], event['end_ms']
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 3600000 for v in (start, end)) or start > end:
        raise RuntimeError('voice-event')
    return start, end


def _usage(value, depth=0):
    if not isinstance(value, dict) or len(value) > 64 or depth > 4:
        raise RuntimeError('voice-event')
    for key, counter in value.items():
        if not re.fullmatch('[a-z_]{1,64}', key):
            raise RuntimeError('voice-event')
        if isinstance(counter, dict):
            _usage(counter, depth + 1)
        elif depth == 0 and key == 'backend_model_usage' and counter == []:
            pass  # 已觀察的空後端用量清單；非空或未核實內容仍拒絕。
        elif type(counter) is not int or not 0 <= counter <= 2**53:
            raise RuntimeError('voice-event')


class VoiceEvents:
    def __init__(self):
        self.changed = asyncio.Event()
        self.parsers = {name: LiveEvents() for name in ('primary', 'sideband')}
        self.started = set()
        self.done = {}
        self.closed = set()
        self.session_id = self.turn_id = None
        self.close_requested = False
        self.invalid = False
        self._count = 0
        self._delta_bytes = {name: 0 for name in self.parsers}
        self._item_bytes = {name: 0 for name in self.parsers}
        self._items = {}
        self._reflected_bytes = 0
        self._input_bytes = 0

    @property
    def complete(self):
        return (not self.invalid and len(self.done) == 2 and self.done['primary'] == self.done['sideband'])

    def request_close(self):
        if not self.complete or self.close_requested:
            self.invalid = True
            self.changed.set()
            raise RuntimeError('voice-event')
        self.close_requested = True

    def consume(self, raw, channel):
        try:
            if self.invalid or channel not in self.parsers:
                raise RuntimeError('voice-event')
            self._count += 1
            if self._count > 2000:
                raise RuntimeError('voice-event')
            event = bounded_json(raw.encode() if isinstance(raw, str) else raw)
            kind = event.get('type')
            _tool_free(event, allow_empty_tools=False)
            if channel in self.closed and not (channel == 'sideband' and kind == 'session.input_audio.append'
                    and self.close_requested and self.complete):
                raise RuntimeError('voice-event')
            if kind == 'session.started':
                _fields(event, ('type', 'session'))
                _fields(event['session'], ('id', 'status', 'expires_at'))
                session = event['session']
                if session['status'] != 'active' or type(session['expires_at']) is not int or not 0 < session['expires_at'] <= 2**53:
                    raise RuntimeError('voice-event')
                result = self.parsers[channel].consume(raw.encode() if isinstance(raw, str) else raw)
                if self.session_id not in (None, result.turn_id):
                    raise RuntimeError('voice-event')
                self.session_id = result.turn_id
                self.started.add(channel)
                return result
            if channel not in self.started:
                raise RuntimeError('voice-event')
            if kind in ('output_transcript.added', 'turn.created', 'turn.done'):
                return self._transcript(event, raw, channel)
            if kind == 'turn.delta':
                _fields(event, ('type', 'turn_id', 'delta', 'start_ms', 'end_ms'))
                turn_id = _id(event['turn_id'])
                if (turn_id != self.turn_id or ('assistant', turn_id) not in self.parsers[channel]._created
                        or channel in self.done):
                    raise RuntimeError('voice-event')
                text = _text(event['delta'])
                self._delta_bytes[channel] += len(text.encode())
                if self._delta_bytes[channel] > 8192:
                    raise RuntimeError('voice-event')
                return RawEvent('turn-delta', turn_id, 'assistant', text, *_times(event))
            if kind in ('session.input_audio.append', 'session.output_audio.delta'):
                if channel != 'sideband':
                    raise RuntimeError('voice-event')
                field = 'audio' if kind == 'session.input_audio.append' else 'delta'
                _fields(event, ('type', field) if field == 'audio' else ('type', field, 'start_ms', 'end_ms'))
                encoded = event[field]
                limit = 192000 if field == 'audio' else 48000
                if not isinstance(encoded, str) or not encoded or len(encoded) > limit * 4 // 3:
                    raise RuntimeError('voice-event')
                pcm = base64.b64decode(encoded, validate=True)
                if not pcm or len(pcm) % 2 or len(pcm) > limit:
                    raise RuntimeError('voice-event')
                if field == 'audio':
                    self._input_bytes += len(pcm)
                    if self._input_bytes > 5760000:  # 120 秒 24k mono；不進候選緩衝。
                        raise RuntimeError('voice-event')
                else:
                    self._reflected_bytes += len(pcm)
                    if self._reflected_bytes > 4320000:
                        raise RuntimeError('voice-event')
                    _times(event)
                return RawEvent('audio-observation', self.session_id)
            if kind == 'session.usage.updated':
                _fields(event, ('type', 'usage', 'usage_limit'))
                _fields(event['usage'], ('audio_duration_ms', 'backend_model_usage'))
                _fields(event['usage_limit'], ('status', 'reset_seconds'))
                if any(value is not None for value in event['usage_limit'].values()):
                    raise RuntimeError('voice-event')
                _usage(event['usage'])  # 已核實的用量 envelope；不是完成或放行依據。
                return RawEvent('usage', self.session_id)
            if kind == 'session.closed':
                _fields(event, ('type', 'reason', 'usage'))
                if not self.close_requested or not self.complete or event['reason'] != 'client_request':
                    raise RuntimeError('voice-event')
                _usage(event['usage'])
                self.closed.add(channel)
                return RawEvent('closed', self.session_id)
            raise RuntimeError('voice-event')
        except Exception:
            self.invalid = True
            raise RuntimeError('voice-event') from None
        finally:
            self.changed.set()

    def _transcript(self, event, raw, channel):
        kind = event['type']
        if kind == 'output_transcript.added':
            _fields(event, ('type', 'item', 'start_ms', 'end_ms'))
            _fields(event['item'], ('id', 'type', 'text'), ('role',))
            if event['item']['type'] != 'output_transcript':
                raise RuntimeError('voice-event')
            identifier = _id(event['item']['id'])
            if event['item'].get('role', 'assistant') != 'assistant' or channel in self.done:
                raise RuntimeError('voice-event')
            text = _text(event['item']['text'])
            self._item_bytes[channel] += len(text.encode())
            if self._item_bytes[channel] > 8192:
                raise RuntimeError('voice-event')
            # item ID 不等於 turn ID；片段只作受限觀察，不作完成或放行依據。
            result = RawEvent('transcript-observation', identifier, 'assistant', text, *_times(event))
            if identifier in self._items:
                previous, channels = self._items[identifier]
                if previous != result or channel in channels:
                    raise RuntimeError('voice-event')
                channels.add(channel)
            else:
                self._items[identifier] = result, {channel}
            return result
        else:
            _fields(event, ('type', 'turn'))
            _fields(event['turn'], ('id', 'role', 'start_ms', 'end_ms', 'transcript'))
            if event['turn']['role'] != 'assistant':
                raise RuntimeError('voice-event')
            identifier = _id(event['turn']['id'])
            _times(event['turn'])
        if self.turn_id not in (None, identifier):
            raise RuntimeError('voice-event')
        self.turn_id = identifier
        result = self.parsers[channel].consume(raw.encode() if isinstance(raw, str) else raw)
        if kind == 'turn.done':
            self.done[channel] = result
            if len(self.done) == 2 and not self.complete:
                raise RuntimeError('voice-event')
        return result
