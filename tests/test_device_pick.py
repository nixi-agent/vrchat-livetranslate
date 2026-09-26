"""默认输出设备 → loopback 匹配验收（改动④，真 bug 修复）。

## bug 是什么

旧代码选「默认输出设备的 loopback」时写死了中文：

    if d.get("index") == default_out or str(d["name"]).startswith("默认"):

`startswith("默认")` 是中文 Windows 专属——英/日/俄 Windows 上永不命中，
于是退化成「拿枚举到的第一个 loopback 设备」，**译音回灌可能写到错声卡，对方听不到**。

## 现在的优先级（pick_default_loopback 纯函数）

1) index 精确命中；2) 名字与默认输出设备名一致（去 loopback 后缀、忽略大小写）；
3) 多语言「默认」前缀兜底（中/英/日/韩/俄）；4) 都不中 → None，调用方回落并留痕。

## 离线可跑

全部用假设备字典，不枚举真实设备。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.engine import _strip_loopback_suffix, pick_default_loopback   # noqa: E402


def _loop(index: int, name: str) -> dict:
    return {"index": index, "name": name, "defaultSampleRate": 48000, "maxInputChannels": 2}


LOOPS = [
    _loop(10, "Steam Streaming Speakers [Loopback]"),
    _loop(11, "Speakers (Realtek Audio) [Loopback]"),
    _loop(12, "VoiceMeeter Input [Loopback]"),
]


def test_index_match_wins() -> None:
    """① index 精确命中是最可靠的，优先于名字与前缀。"""
    d = pick_default_loopback(LOOPS, 11, "完全不同的名字")
    assert d is not None and d["index"] == 11, f"index 命中失败：{d}"
    # index 命中要压过「默认」前缀：另一个设备叫 Default 也不能抢
    loops = [_loop(5, "Default - Speakers [Loopback]"), _loop(7, "Speakers [Loopback]")]
    d = pick_default_loopback(loops, 7, None)
    assert d is not None and d["index"] == 7, f"index 优先级被前缀抢走：{d}"
    print("  index 精确命中且优先级最高 OK")


def test_name_match_with_default_output() -> None:
    """② 名字与默认输出设备名一致（去 [Loopback] 后缀、忽略大小写）。"""
    d = pick_default_loopback(LOOPS, -1, "Speakers (Realtek Audio)")
    assert d is not None and d["index"] == 11, f"设备名匹配失败：{d}"
    d = pick_default_loopback(LOOPS, -1, "speakers (realtek audio)")      # 忽略大小写
    assert d is not None and d["index"] == 11, f"大小写不敏感失败：{d}"
    d = pick_default_loopback(LOOPS, None, "Steam Streaming Speakers")    # index=None 也能走名字
    assert d is not None and d["index"] == 10, f"index=None 时名字匹配失败：{d}"
    # 名字匹配要压过「默认」前缀
    loops = [_loop(5, "Default - Headset [Loopback]"), _loop(7, "Speakers [Loopback]")]
    d = pick_default_loopback(loops, -1, "Speakers")
    assert d is not None and d["index"] == 7, f"名字优先级被前缀抢走：{d}"
    print("  默认输出设备名匹配（去后缀/忽略大小写）OK")


def test_multilingual_default_prefixes() -> None:
    """③ 中/英/日/韩/俄「默认」前缀兜底 —— 旧代码只认中文，其它语言系统永不命中。"""
    cases = [
        ("中文", "默认 - 扬声器 [Loopback]"),
        ("英语", "Default - Speakers [Loopback]"),
        ("日语", "デフォルト - スピーカー [Loopback]"),
        ("韩语", "기본 - 스피커 [Loopback]"),
        ("俄语", "По умолчанию - Динамики [Loopback]"),
        ("俄语(大小写)", "ПО УМОЛЧАНИЮ - Динамики [Loopback]"),
    ]
    for lang, name in cases:
        loops = [_loop(3, "Something Else [Loopback]"), _loop(9, name)]
        d = pick_default_loopback(loops, -1, "")
        assert d is not None and d["index"] == 9, f"{lang}「默认」前缀没命中：{name!r} → {d}"
    print("  中/英/日/韩/俄「默认」前缀全部命中 OK")


def test_no_match_returns_none() -> None:
    """④ 全不匹配 → None（调用方负责回落到第一个 loopback 并留痕，绝不悄悄选错）。"""
    assert pick_default_loopback(LOOPS, -1, "Nonexistent Device") is None
    assert pick_default_loopback(LOOPS, 99, None) is None
    assert pick_default_loopback([], 0, "x") is None
    assert pick_default_loopback(None, 0, "x") is None
    print("  全不匹配 → None（不悄悄选错设备）OK")


def test_strip_loopback_suffix() -> None:
    assert _strip_loopback_suffix("Speakers (Realtek) [Loopback]") == "Speakers (Realtek)"
    assert _strip_loopback_suffix("Speakers (Realtek) (loopback)") == "Speakers (Realtek)"
    assert _strip_loopback_suffix("  Speakers  ") == "Speakers"
    assert _strip_loopback_suffix("Speakers") == "Speakers"
    print("  loopback 后缀剥离 OK")


def test_fallback_path_logs_warning() -> None:
    """调用方回落时必须留痕：用假 PyAudio 走一遍 pick_loopback_device 的回落分支。"""
    import contextlib
    import io

    import vlt.engine as E

    class _FakePA:
        def __init__(self) -> None:
            self.terminated = False

        def get_loopback_device_info_generator(self):
            return iter([_loop(3, "Some Random Device [Loopback]"),
                         _loop(4, "Another Device [Loopback]")])

        def get_host_api_info_by_type(self, _api):
            return {"defaultOutputDevice": 7}

        def get_device_info_by_index(self, _idx):
            return {"name": "Missing Default Out"}

        def terminate(self) -> None:
            self.terminated = True

    import pyaudiowpatch as pyaudio
    original = pyaudio.PyAudio
    pyaudio.PyAudio = _FakePA
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            picked, p = E.pick_loopback_device(["不存在的关键词zzz"])
        # 回退链不中 + 默认设备也不中 → 回落到第一个 loopback，并且**留痕说明用了回退**
        assert picked is not None and picked[0] == 3, f"回落结果不对：{picked}"
        out = buf.getvalue()
        assert "回退到第一个 loopback" in out, f"用了回退却没留痕：{out!r}"
    finally:
        pyaudio.PyAudio = original
    print("  全不匹配 → 回落第一个 loopback + 留痕 OK")


if __name__ == "__main__":
    print("test_device_pick:")
    test_index_match_wins()
    test_name_match_with_default_output()
    test_multilingual_default_prefixes()
    test_no_match_returns_none()
    test_strip_loopback_suffix()
    test_fallback_path_logs_warning()
    print("ALL PASSED")
