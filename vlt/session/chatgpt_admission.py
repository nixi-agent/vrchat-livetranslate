"""Engine-owned revocation follows approved output into delayed consumers."""
import threading
from dataclasses import dataclass

from .chatgpt_output import allowed, current, scope


@dataclass(frozen=True, eq=False, slots=True)
class _Approved:
    gate: object
    segment: object
    _audio_bytes: int = 0

    def __call__(self, direction=None):
        return self.gate.valid() and allowed(self.segment, direction)


class Admission:
    def __init__(self, engine, session):
        self.engine, self.session = engine, session
        self.revoked = threading.Event()
        self.context = self._context()

    def revoke(self):
        # GUI threads revoke only an Event; asyncio cancellation belongs to its owner.
        self.revoked.set()

    def _context(self):
        engine = self.engine
        cfg, who = engine._cfg, engine._direction
        base, directions = cfg.session_base, cfg.directions
        d = directions[who]
        return (who, getattr(cfg, '_policy_revision', 0),
                getattr(base, '_revision', 0), getattr(base.get('glossary'), '_revision', 0),
                getattr(directions, '_revision', 0),
                id(d), getattr(d, '_policy_revision', 0), getattr(d.hotwords, '_revision', 0),
                base.get('provider'), d.source_lang, d.target_lang, d.output_audio,
                tuple(sorted(cfg.merged_hotwords(who).items())))

    def valid(self):
        if self.revoked.is_set():
            return False
        engine = self.engine
        try:
            cfg = self.session.cfg
            direction = engine._cfg.directions[engine._direction]
            if (not engine._stopping and engine._session is self.session
                    and engine._admission is self and self._context() == self.context
                    and cfg.provider == engine._cfg.session_base.get('provider')
                    and cfg.source_lang == direction.source_lang
                    and cfg.target_lang == direction.target_lang
                    and cfg.output_audio == bool(direction.output_audio
                        and engine._direction == 'mine' and engine._virtualmic is not None)
                    and getattr(cfg, '_direction_policy_revision', None) in
                        (None, getattr(direction, '_policy_revision', 0))
                    and cfg.hotwords == engine._cfg.merged_hotwords(engine._direction)):
                return True
        except Exception:
            pass
        self.revoke()
        return False

    def accepts(self, approval):
        return isinstance(approval, _Approved) and approval.gate is self and self.valid()

    def consume(self, pipeline, direction, on_text, on_audio):
        with self.engine._sink_lock:
            return pipeline.consume(direction, on_text, on_audio, self._can_output)

    def _can_output(self, mono_bytes):
        ready = getattr(self.engine._virtualmic, 'can_accept_complete', None)
        return ready is None or ready(mono_bytes * 4)

    def wrap(self, callback):
        def emit(value):
            segment = current()
            if segment is None or not self.valid():
                return
            size = len(value) if isinstance(value, bytes) else 0
            with scope(_Approved(self, segment, size)):
                callback(value)
        return emit
