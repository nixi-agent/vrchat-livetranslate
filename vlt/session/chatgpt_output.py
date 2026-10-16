"""Internal approval scope; legacy providers have no approval closure."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_current = ContextVar('chatgpt_output_approval', default=None)


def current():
    return _current.get()


def allowed(approval, direction=None):
    if approval is None:
        return True
    try:
        return (approval() if direction is None else approval(direction)) is True
    except Exception:
        return False


@dataclass(frozen=True, eq=False, slots=True)
class _Combined:
    rows: tuple

    def __call__(self):
        return all(allowed(approval, who) for approval, who in self.rows)


def same(first, second):
    if first is second:
        return True
    if not isinstance(first, _Combined) or not isinstance(second, _Combined):
        return False
    return len(first.rows) == len(second.rows) and all(
        a is b and who == other for (a, who), (b, other) in zip(first.rows, second.rows))


def combined(approvals):
    """A delayed mixed-direction frame retains each row's own authorization."""
    tagged = tuple(approvals)
    if not any(approval is not None for approval, _ in tagged):
        return None
    return _Combined(tagged)


def select_rows(entries, approval, latest=False):
    """Keep row identities aligned, including legacy rows, when selecting a view."""
    entries = tuple(entries)
    if isinstance(approval, _Combined):
        if len(entries) != len(approval.rows):
            return (), approval
        pairs = [(row, tag) for row, tag in zip(entries, approval.rows) if allowed(*tag)]
        if latest:
            pairs = pairs[-1:]
        return tuple(row for row, _ in pairs), combined(tag for _, tag in pairs)
    if not allowed(approval):
        return (), approval
    return entries[-1:] if latest else entries, approval


@contextmanager
def scope(approval):
    token = _current.set(approval)
    try:
        yield
    finally:
        _current.reset(token)
