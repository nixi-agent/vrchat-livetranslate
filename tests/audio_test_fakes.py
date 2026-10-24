"""Offline audio guard without importing or unloading real device drivers."""
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock


@contextmanager
def no_audio_device():
    missing = object()
    original = sys.modules.get('sounddevice', missing)
    driver = SimpleNamespace(RawOutputStream=Mock(side_effect=AssertionError('device opened')))
    sys.modules['sounddevice'] = driver
    try:
        yield driver
    finally:
        if original is missing:
            sys.modules.pop('sounddevice', None)
        else:
            sys.modules['sounddevice'] = original
