"""Offline playback regression: drain every PCM frame without opening devices."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from audio_test_fakes import no_audio_device

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlt.output.micproxy import MODE_TRANSLATED, MicProxy, _TranslatedBuffer  # noqa: E402
from vlt.output.micproxy_linux import LinuxMicProxy  # noqa: E402
from vlt.output.virtualmic import PRIME_TIMEOUT_S, PwCatVirtualMic, VirtualMic  # noqa: E402

FRAMES = 960
BLOCK = FRAMES * 4


def buffer(cls=VirtualMic):
    return cls(device_index=0, device_name="unused", buffer_ms=20)


def block(vm):
    if isinstance(vm, _TranslatedBuffer):
        return vm.drain_block(BLOCK) or bytes(BLOCK)
    out = bytearray(BLOCK)
    vm._audio_callback(out, FRAMES, None, None)
    return out


class PlaybackTailTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(no_audio_device())

    def test_virtualmic_drains_tail_once_and_pads(self):
        vm = buffer()
        pcm = b"\x01\x02\x03\x04" * (2 * FRAMES + 3)
        vm.push(pcm)
        vm.end_sentence()
        played = bytearray()
        for _ in range(3):
            out = bytearray(BLOCK)
            vm._audio_callback(out, FRAMES, None, None)
            played.extend(out)
        self.assertEqual(played, pcm + b"\0" * (3 * BLOCK - len(pcm)))
        self.assertEqual(vm._buf_bytes, 0)
        self.assertFalse(vm._head_started)
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, b"\0" * BLOCK)
        vm.push(b"\x05\x06\x07\x08" * FRAMES)
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, b"\x05\x06\x07\x08" * FRAMES)

    def test_short_segment_waits_for_prime_then_drains(self):
        vm = buffer()
        pcm = b"\x01\x02\x03\x04" * 3
        vm.push(pcm)
        out = bytearray(BLOCK)
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, b"\0" * BLOCK)
        self.assertEqual(vm._buf_bytes, len(pcm))
        vm._last_push_ts -= PRIME_TIMEOUT_S + 1
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, pcm + b"\0" * (BLOCK - len(pcm)))
        self.assertEqual(vm._buf_bytes, 0)

    def test_late_sentence_end_releases_playback_state(self):
        for cls in (VirtualMic, _TranslatedBuffer):
            with self.subTest(buffer=cls.__name__):
                vm = buffer(cls)
                vm.push(b"\x01\x02\x03\x04" * (FRAMES + 3))
                block(vm)
                vm._last_push_ts -= PRIME_TIMEOUT_S + 1
                block(vm)
                self.assertEqual(vm._buf_bytes, 0)
                vm.end_sentence()
                self.assertFalse(vm._head_started, "an already drained sentence has ended")

    def test_unsealed_short_fragment_waits_for_more_samples_or_idle(self):
        vm = buffer()
        first = b"\x01\x02\x03\x04" * FRAMES
        fragment = b"\x05\x06\x07\x08" * 3
        rest = b"\x09\x0a\x0b\x0c" * (FRAMES - 3)
        vm.push(first)
        out = bytearray(BLOCK)
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, first)
        vm.push(fragment)
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, bytes(BLOCK), "wait for a streaming block before inserting padding")
        self.assertEqual(vm._buf_bytes, len(fragment))
        vm.push(rest)
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, fragment + rest)
        vm.push(fragment)
        vm._last_push_ts -= PRIME_TIMEOUT_S + 1
        vm._audio_callback(out, FRAMES, None, None)
        self.assertEqual(out, fragment + bytes(BLOCK - len(fragment)))
        self.assertEqual(vm._buf_bytes, 0)

    def test_late_sentence_end_does_not_protect_next_unplayed_sentence(self):
        vm = buffer()
        vm._max_buffer_ms = 50
        vm.push(b"\x01\x02\x03\x04" * (FRAMES + 3))
        block(vm)
        vm._last_push_ts -= PRIME_TIMEOUT_S + 1
        block(vm)
        vm.end_sentence()
        old = b"\x05\x06\x07\x08" * (3 * FRAMES)
        new = b"\x09\x0a\x0b\x0c" * (3 * FRAMES)
        vm.push(old)
        vm.end_sentence()
        vm.push(new)
        with vm._lock:
            self.assertEqual(vm._drain(len(new)), new, "unplayed older sentence must be dropped whole")
        self.assertEqual(vm._buf_bytes, 0)

    def test_pipewire_writer_preserves_stream_fragment_then_plays_sealed_tail(self):
        vm = PwCatVirtualMic(target="unused", buffer_ms=20)
        first = b"\x01\x02\x03\x04" * FRAMES
        fragment = b"\x05\x06\x07\x08" * 3
        played = []

        def write(data):
            played.append(data)
            if len(played) == 2:
                self.assertEqual(vm._buf_bytes, len(fragment))
                vm.end_sentence()
            if len(played) == 3:
                vm._stop.set()

        vm._proc = SimpleNamespace(stdin=SimpleNamespace(write=write, flush=lambda: None))
        with patch("vlt.output.virtualmic.time.monotonic", return_value=100):
            vm.push(first + fragment)
            vm._writer_loop()
        self.assertEqual(played, [first, bytes(BLOCK), fragment + bytes(BLOCK - len(fragment))])
        self.assertEqual(vm._buf_bytes, 0)
        self.assertFalse(vm._head_started)

    def test_both_proxies_drain_same_tail_and_keep_passthrough_separate(self):
        for cls in (MicProxy, LinuxMicProxy):
            with self.subTest(platform=cls.__name__):
                proxy = cls(audio_cfg={})
                proxy._translated = buffer(_TranslatedBuffer)
                proxy._mode = MODE_TRANSLATED
                pcm = b"\x01\x02\x03\x04" * (FRAMES + 3)
                proxy._translated.push(pcm)
                proxy._translated.end_sentence()
                raw = b"\x05\x06\x07\x08" * FRAMES
                proxy._ring.push(raw)
                played = bytearray()
                for _ in range(2):
                    if isinstance(proxy, LinuxMicProxy):
                        out = proxy._mix_block(BLOCK)
                    else:
                        out = bytearray(BLOCK)
                        proxy._out_callback(out, FRAMES, None, None)
                    played.extend(out)
                self.assertEqual(played, pcm + b"\0" * (2 * BLOCK - len(pcm)))
                self.assertEqual(proxy._translated._buf_bytes, 0)
                self.assertIsNone(proxy._translated.drain_block(BLOCK))
                self.assertEqual(proxy._ring.drain(BLOCK)[0], raw)


if __name__ == "__main__":
    unittest.main()
