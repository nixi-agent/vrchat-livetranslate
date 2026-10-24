"""完整候選 PCM 的本機封存；不代表服務端已完成生成。"""
import re


class Capture:
    def __init__(self):
        self._pcm = bytearray()
        self._seal_id = None
        self._sealed = False
        self.failure = None

    def fail(self, reason):
        self.failure = self.failure or reason
        self._pcm.clear()
        raise RuntimeError(self.failure)

    def append(self, pcm):
        if self.failure:
            raise RuntimeError(self.failure)
        if self._sealed:
            self.fail('capture-sealed')
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 48000:
            self.fail('capture-pcm')
        if len(self._pcm) + len(pcm) > 4_320_000:
            self.fail('capture-size')
        self._pcm.extend(pcm)

    def request_seal(self, seal_id):
        if self.failure:
            raise RuntimeError(self.failure)
        if self._seal_id is not None or not isinstance(seal_id, str) or not re.fullmatch('[0-9a-f]{16}', seal_id):
            self.fail('capture-ack')
        self._seal_id = seal_id

    def acknowledge(self, event):
        if self.failure:
            raise RuntimeError(self.failure)
        if (self._sealed or self._seal_id is None or not isinstance(event, dict)
                or set(event) != {'type', 'seal_id', 'samples'}
                or event['type'] != 'sealed' or event['seal_id'] != self._seal_id
                or type(event['samples']) is not int or event['samples'] <= 0
                or event['samples'] * 2 != len(self._pcm)):
            self.fail('capture-ack')
        self._sealed = True
        result = bytes(self._pcm)
        self._pcm.clear()
        return result
