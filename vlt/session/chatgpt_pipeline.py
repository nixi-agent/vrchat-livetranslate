"""逐段校驗 coordinator；服務門檻通過前不註冊到正式 session factory。"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

from .base import TextDelta
from .chatgpt_segments import Segmenter
from .chatgpt_output import scope
from ..ui_text import SOURCE_LANGS, TARGET_LANGS

_STAGE_TIMEOUT = 35
_VOICE_TIMEOUT = 120
_VERIFIER_VERSION = 'subscription-segment-v1'


class _Rejected(RuntimeError):
    pass


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _string(value):
    return isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= 8192


def _verdict(value, language):
    fields = {'approved', 'language', 'meaning', 'numbers', 'negation', 'complete', 'extra_content'}
    return (isinstance(value, dict) and set(value) == fields and value['language'] == language
            and all(value[k] is True for k in ('approved', 'meaning', 'numbers', 'negation', 'complete'))
            and value['extra_content'] is False)


def _policy(cfg):
    if cfg.provider != 'chatgpt':
        raise _Rejected('provider-invalid')
    if cfg.source_lang not in SOURCE_LANGS.values() or cfg.target_lang not in TARGET_LANGS.values():
        raise _Rejected('language-invalid')
    vocabulary = json.dumps(cfg.hotwords, ensure_ascii=False, sort_keys=True)
    if (len(vocabulary.encode()) > 8192 or not isinstance(cfg.hotwords, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in cfg.hotwords.items())):
        raise _Rejected('vocabulary-invalid')
    return cfg.source_lang, cfg.target_lang, bool(cfg.output_audio), vocabulary


@dataclass(frozen=True)
class _Section:
    direction: str
    segment_id: str
    epoch: int
    account_generation: int
    policy: tuple
    pcm: bytes
    source_hash: str


@dataclass(frozen=True)
class _Approval:
    section: _Section
    candidate_id: str
    source: str
    translation: str
    pcm: bytes
    audio_hash: str
    text_hash: str
    verifier_version: str = _VERIFIER_VERSION


class Coordinator:
    def feed(self, direction, pcm):
        if self._closed or direction not in self._configs:
            return 0
        try:
            self._sync_context()
            sections = self._segmenters[direction].feed(
                pcm, allow_pause=direction not in self._active and not self._ready[direction])
        except Exception:
            self._reason(direction, uuid.uuid4().hex, 'input-rejected')
            return 0
        accepted = 0
        for section in sections:
            if section.reason:
                self._reason(direction, uuid.uuid4().hex, section.reason)
            else:
                accepted += int(self.submit(direction, section.pcm))
        return accepted

    def finish(self, direction):
        if self._closed or direction not in self._configs:
            return 0
        try:
            self._sync_context()
            section = self._segmenters[direction].finish()
        except Exception:
            self._reason(direction, uuid.uuid4().hex, 'input-rejected')
            return 0
        if section is None:
            return 0
        if section.reason:
            self._reason(direction, uuid.uuid4().hex, section.reason)
            return 0
        return int(self.submit(direction, section.pcm))

    def __init__(self, configs, services, on_reason, account_generation, context_valid=None):
        if not configs or any(k not in ('mine', 'theirs') for k in configs):
            raise ValueError('direction-invalid')
        if any(cfg.provider != 'chatgpt' for cfg in configs.values()):
            raise ValueError('provider-invalid')
        self._configs = configs
        self._services = services
        self._on_reason = on_reason
        self._account_generation = account_generation
        self._context_valid = context_valid
        self._epoch = 0
        self._closed = False
        self._pending = {k: deque() for k in configs}
        self._ready = {k: deque() for k in configs}
        self._active = {}
        self._owned = set()
        self._segmenters = {k: Segmenter() for k in configs}
        self._input_context = self._context()

    def _context(self):
        return self._account_generation(), {k: (_policy(cfg), getattr(cfg, '_policy_revision', 0),
            getattr(cfg.hotwords, '_revision', 0)) for k, cfg in self._configs.items()}

    def _sync_context(self):
        context = self._context()
        if context != self._input_context:
            self.invalidate()
            self._input_context = context

    def submit(self, direction, pcm):
        if self._closed or direction not in self._configs:
            return False
        segment_id = uuid.uuid4().hex
        try:
            self._sync_context()
            if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 960000:
                raise _Rejected('pcm-invalid')
            policy = _policy(self._configs[direction])
            if direction == 'theirs' and policy[2]:
                raise _Rejected('direction-audio-disabled')
            section = _Section(direction, segment_id, self._epoch, self._account_generation(),
                               policy, pcm, _hash(pcm))
            if direction in self._active:
                if self._active[direction].cancelling():
                    raise _Rejected('direction-resetting')
                if len(self._pending[direction]) >= 2:
                    raise _Rejected('queue-overload')
                self._pending[direction].append(section)
            else:
                task = asyncio.create_task(self._worker(section))
                self._active[direction] = task
                self._owned.add(task)
                task.add_done_callback(lambda done: self._finished(direction, done))
            return True
        except _Rejected as exc:
            self._reason(direction, segment_id, str(exc))
        except Exception:
            self._reason(direction, segment_id, 'configuration-invalid')
        return False

    def _finished(self, direction, task):
        self._owned.discard(task)
        if self._active.get(direction) is task:
            self._active.pop(direction, None)

    def _valid(self, section):
        try:
            return (not self._closed and section.epoch == self._epoch
                    and (self._context_valid is None or self._context_valid(section.direction))
                    and self._context() == self._input_context
                    and section.account_generation == self._account_generation()
                    and section.policy == _policy(self._configs[section.direction])
                    and _hash(section.pcm) == section.source_hash)
        except Exception:
            return False

    async def _stage(self, section, name, operation, *args):
        if not self._valid(section):
            raise _Rejected('stale-result')
        timeout = _VOICE_TIMEOUT if name == 'voice' else _STAGE_TIMEOUT
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            async with asyncio.timeout(timeout) as timer:
                result = await operation(*args)
        except asyncio.TimeoutError:
            raise _Rejected(name + '-timeout') from None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if name == 'audio-asr' and error.args == ('asr-uncertain',):
                raise _Rejected('audio-asr-uncertain') from None
            if name in ('source-asr', 'audio-asr') and error.args == ('asr-empty',):
                raise _Rejected(name + '-empty') from None
            raise _Rejected(name + '-failed') from None
        if timer.expired() or asyncio.get_running_loop().time() >= deadline:
            raise _Rejected(name + '-timeout')
        if not self._valid(section):
            raise _Rejected('stale-result')
        return result

    async def _process(self, section):
        source_lang, target, voice, vocabulary = section.policy
        source = await self._stage(section, 'source-asr', self._services.source_asr, section.pcm, source_lang)
        if not _string(source):
            raise _Rejected('source-invalid')
        terms = json.loads(vocabulary)
        use_terms = bool(terms)
        if source_lang == target and terms:
            term_source = source.casefold()
            term_keys = [term.casefold() for term in terms]
            if target == 'zh':
                from opencc import OpenCC
                convert = OpenCC('t2s').convert
                term_source = convert(term_source)
                term_keys = [convert(term) for term in term_keys]
            use_terms = any(term in term_source for term in term_keys)
        if source_lang == target and not use_terms:
            translation = {'translation': source, 'language': target}
        else:
            translation = await self._stage(section, 'translation', self._services.translate,
                                            source, target, terms)
        if (not isinstance(translation, dict) or set(translation) != {'translation', 'language'}
                or translation['language'] != target or not _string(translation['translation'])):
            raise _Rejected('translation-invalid')
        text = translation['translation']
        checked = await self._stage(section, 'text-check', self._services.validate_text, source, text, target)
        if not _verdict(checked, target):
            raise _Rejected('text-rejected')
        pcm, candidate_id = b'', ''
        if voice:
            # synthesize 的契約須由 adapter 證明遠端完成及完整 local seal；此處不能用靜默猜測。
            candidate = await self._stage(section, 'voice', self._services.synthesize, text, target)
            if (not isinstance(candidate, tuple) or len(candidate) != 2
                    or not isinstance(candidate[0], str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', candidate[0])
                    or not isinstance(candidate[1], bytes) or not candidate[1] or len(candidate[1]) % 2
                    or len(candidate[1]) > 4_320_000):
                raise _Rejected('candidate-invalid')
            candidate_id, pcm = candidate
            transcript = await self._stage(section, 'audio-asr', self._services.audio_asr, pcm, target)
            if not _string(transcript):
                raise _Rejected('audio-transcript-invalid')
            checked = await self._stage(section, 'audio-check', self._services.validate_audio, text, transcript, target)
            if not _verdict(checked, target):
                raise _Rejected('audio-rejected')
        return _Approval(section, candidate_id, source, text, pcm, _hash(pcm), _hash(text.encode()))

    async def _worker(self, section):
        direction = section.direction
        try:
            while section is not None:
                try:
                    approval = await self._process(section)
                    if not self._valid(section):
                        raise _Rejected('stale-result')
                    if len(self._ready[direction]) >= 2:
                        raise _Rejected('output-overload')
                    self._ready[direction].append(approval)
                except _Rejected as exc:
                    self._reason(direction, section.segment_id, str(exc))
                except asyncio.CancelledError:
                    return
                except Exception:
                    self._reason(direction, section.segment_id, 'segment-failed')
                if self._active.get(direction) is not asyncio.current_task():
                    return
                section = self._pending[direction].popleft() if self._pending[direction] else None
        finally:
            if self._active.get(direction) is asyncio.current_task():
                self._active.pop(direction, None)

    def consume(self, direction, on_text, on_audio):
        if direction not in self._ready or not self._ready[direction]:
            return False
        approval = self._ready[direction].popleft()
        section = approval.section
        if (not self._valid(section) or approval.verifier_version != _VERIFIER_VERSION
                or _hash(approval.pcm) != approval.audio_hash
                or _hash(approval.translation.encode()) != approval.text_hash):
            self._reason(direction, section.segment_id, 'stale-result')
            return False
        source, text = approval.source, approval.translation
        if section.policy[0] == 'zh' or section.policy[1] == 'zh':
            from opencc import OpenCC
            convert = OpenCC('s2t' if section.policy[0] == section.policy[1] else 's2twp').convert
            source = convert(source) if section.policy[0] == 'zh' else source
            text = convert(text) if section.policy[1] == 'zh' else text
        if not self._valid(section):
            return False
        revoked = False
        def still_approved(expected_direction=None):
            nonlocal revoked
            if expected_direction is not None and expected_direction != section.direction:
                revoked = True
                self._reason(direction, section.segment_id, 'direction-mismatch')
                return False
            if not revoked and self._valid(approval.section) and approval.verifier_version == _VERIFIER_VERSION:
                return True
            revoked = True
            self._reason(direction, section.segment_id, 'stale-result')
            return False

        with scope(still_approved):
            on_text(TextDelta(confirmed=text, source=source, is_final=True))
            if approval.pcm and self._valid(section):
                on_audio(approval.pcm)
        return True

    def _reason(self, direction, segment_id, reason):
        try:
            self._on_reason(direction, segment_id, reason)
        except Exception:
            pass

    def invalidate(self):
        self._epoch += 1
        for segmenter in self._segmenters.values():
            segmenter.reset()
        for task in self._active.values():
            task.cancel()
        for queues in (self._pending, self._ready):
            for queue in queues.values():
                queue.clear()

    async def close(self):
        if not self._closed:
            self._closed = True
            self.invalidate()
            self._close_wait = asyncio.gather(*tuple(self._owned), return_exceptions=True)
        cancelled = False
        while not self._close_wait.done():
            try:
                await asyncio.shield(self._close_wait)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError()
        self._close_wait.result()
