"""ChatGPT 專用 16 kHz mono s16le 分段；不建立音訊或服務連線。"""
from dataclasses import dataclass
from array import array
from collections import deque
import sys

_FRAME_SAMPLES = 160  # 10 ms: isolated peaks must not erase real speech pauses.


@dataclass(frozen=True)
class PCMSection:
    pcm: bytes
    reason: str = ''


class Segmenter:
    def __init__(self):
        self.reset()

    def feed(self, pcm, *, allow_pause=True):
        if not allow_pause:
            self._pause_cut = None
        if not isinstance(pcm, bytes) or len(pcm) % 2 or len(pcm) > 32000:
            self.reset()
            raise RuntimeError('pcm-invalid')
        samples = array('h')
        samples.frombytes(pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        samples = self._pending + samples
        complete = len(samples) // _FRAME_SAMPLES * _FRAME_SAMPLES
        self._pending = samples[complete:]
        results = []
        for start in range(0, complete, _FRAME_SAMPLES):
            frame = samples[start:start + _FRAME_SAMPLES]
            voiced = sum(value * value for value in frame) >= 220 ** 2 * _FRAME_SAMPLES
            self._onset_frames = self._onset_frames + 1 if voiced else 0
            if self._section is None:
                if self._onset_frames < 3:
                    self._preroll.extend(frame)
                    continue
                self._section = array('h', self._preroll)
                self._preroll.clear()
            if allow_pause and voiced and self._quiet >= 5600 and len(self._section) >= 64000:
                self._pause_cut = len(self._section)
            self._section.extend(frame)
            self._quiet = 0 if voiced else self._quiet + _FRAME_SAMPLES
            if self._quiet >= 8000:
                if sys.byteorder != 'little':
                    self._section.byteswap()
                results.append(PCMSection(self._section.tobytes()))
                self._reset_section()
            elif self._pause_cut is not None and self._onset_frames >= 3:
                # Confirm the next onset, then separate at the completed 350 ms pause.
                prefix, self._section = self._section[:self._pause_cut], self._section[self._pause_cut:]
                self._pause_cut = None
                if sys.byteorder != 'little':
                    prefix.byteswap()
                results.append(PCMSection(prefix.tobytes()))
            elif len(self._section) >= 480000:
                # Keep every sample; prefer a quiet valley near the ASR input limit.
                tail = self._section[-32000:]
                energies = [sum(v * v for v in tail[i:i + _FRAME_SAMPLES])
                            for i in range(0, len(tail), _FRAME_SAMPLES)]
                energy = best = sum(energies[:20])
                cut = len(self._section) - len(tail) + 1600
                for i in range(1, len(energies) - 19):
                    energy += energies[i + 19] - energies[i - 1]
                    if energy <= best:
                        best = energy
                        cut = len(self._section) - len(tail) + (i + 10) * _FRAME_SAMPLES
                if len(self._section) - cut <= self._quiet:
                    cut = len(self._section) - self._quiet - _FRAME_SAMPLES
                prefix, self._section = self._section[:cut], self._section[cut:]
                self._pause_cut = None
                if sys.byteorder != 'little':
                    prefix.byteswap()
                results.append(PCMSection(prefix.tobytes()))
        return results

    def finish(self):
        section = None
        if self._section is not None:
            samples = self._section + self._pending
            if len(samples) > 480000:
                section = PCMSection(b'', 'segment-size')
            else:
                if sys.byteorder != 'little':
                    samples.byteswap()
                section = PCMSection(samples.tobytes())
        self.reset()
        return section

    def reset(self):
        self._pending = array('h')
        self._reset_section()

    def _reset_section(self):
        self._preroll = deque(maxlen=16000)
        self._section = None
        self._quiet = 0
        self._onset_frames = 0
        self._pause_cut = None
