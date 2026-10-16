"""译音音量匹配（vlt/output/level_match.py）纯逻辑测试：**全程离线，绝不碰音频设备**。

本模块只做数字信号上的算术（RMS / 峰值 / 增益 / 限幅 / 斜坡），所以每条用例都用
`struct` 合成的 PCM 直接喂进去，不需要虚拟声卡、不需要 sounddevice、不需要 pytest。
和 `tests/test_micproxy.py` 一样是**单文件脚本**：全绿打印 `ALL PASSED` 并退出 0。

覆盖 .qoder-brief.md §3 的五组，外加评审后补的四组回归：
  1. 块电平 `block_db`（已知幅度正弦 / 全零静音；engine.chunk_level_db 委托到这里）
  2. `apply_gain_db`（0 dB 逐字节相等 / −6 dB / 正增益的天花板限幅）
  3. `MicReference`（中位数、样本门槛、有效期、抗尖峰、静音不得出结论）
  4. 增益计算（四档麦克风参考电平、峰值限幅把 +4 压到 +1.2、限速收敛）
  5. 参考取不到（保住上一次增益 / 从头到尾没拿到 → 回落固定增益）
  6. off→on：进 off 清斜坡状态、切回来**立刻**重算到新目标（不被限速）；fixed 清过期 fallback
  7. 单一实现：「增益+限幅→字节」/ 块电平 / dB 读数格式各只有一份（两条路逐字节相同）
  8. 配置层一致性：`vlt/config.py` 的默认值/区间 == `level_match` 的 `DEFAULT_*`/`RANGES`
     （靠测试钉，而不是让 config.py 反向 import output 包），以及 `_float_in_range` / `_str_choice`
  9. 跨线程（泵/音频线程 vs Tk 线程）：`MicReference` 无异常不丢块、`LevelMatcher` 互斥成立
 10. 句首斜坡在**短块**（< 20ms/块）下必须接着走，不许每个块边界重铺一遍
"""

from __future__ import annotations

import math

import struct

import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402

from vlt.output import level_match as LM                    # noqa: E402

from vlt.output.level_match import (                        # noqa: E402
    LEVEL_FLOOR_DB,
    MODE_FIXED,
    MODE_FOLLOW_MIC,
    MODE_OFF,
    LevelConfig,
    LevelMatcher,
    MicReference,
    TranslatedLevel,
    apply_gain_db,
)

def _sine(amp: float, n: int, *, freq: float = 1000.0, rate: int = 16000,
          channels: int = 1) -> bytes:
    """幅度 `amp`（满刻度计数）的正弦，s16le；`channels` > 1 时各声道相同。"""
    t = np.arange(n) / rate
    s = np.rint(amp * np.sin(2.0 * math.pi * freq * t))
    s = np.clip(s, -32768, 32767)
    if channels > 1:
        s = np.repeat(s, channels)
    return s.astype("<i2").tobytes()

def _silence(n: int, *, channels: int = 1) -> bytes:
    return struct.pack(f"<{n * channels}h", *([0] * (n * channels)))

def _amp_for_rms_db(db: float) -> float:
    """要让正弦的 RMS 落在 `db` dBFS，需要的幅度（正弦 RMS = amp/√2）。"""
    return 32768.0 * (10.0 ** (db / 20.0)) * math.sqrt(2.0)

def _speech_like(rms_db: float, peak_db: float, seconds: float, *,
                 rate: int = 48000, channels: int = 2) -> bytes:
    """RMS ≈ `rms_db`、峰值 ≈ `peak_db` 的**双声道 48k** 合成块（高波峰因数，像语音）。

    做法：一段目标 RMS 的正弦打底，再把稀疏几处样本抬到目标峰值 —— 抬的那几处对整体
    RMS 的贡献 < 0.01 dB，于是「RMS 与峰值可以分别钉住」，正好对上实测的
    「译音中位 −18 dBFS、峰值贴 −2.2 dBFS」。
    """
    n = int(rate * seconds)
    base = np.rint(_amp_for_rms_db(rms_db)
                   * np.sin(2.0 * math.pi * 233.0 * np.arange(n) / rate))
    peak = round(32768.0 * (10.0 ** (peak_db / 20.0)))
    for i in range(0, n, max(1, n // 8)):
        base[i] = peak
    base = np.clip(base, -32768, 32767)
    if channels > 1:
        base = np.repeat(base, channels)
    return base.astype("<i2").tobytes()

def _feed_voiced(ref: MicReference, *, rms_db: float, seconds: float = 3.0,
                 rate: int = 16000, channels: int = 1, t0: float = 0.0) -> float:
    """按 100ms 一块喂满 `seconds` 秒的有声语音，返回**最后一块的时间戳**。"""
    amp = _amp_for_rms_db(rms_db)
    per = int(rate * LM.BLOCK_S)
    now = t0
    last = t0
    for _ in range(int(round(seconds / LM.BLOCK_S))):
        ref.feed(_sine(amp, per, rate=rate, channels=channels), rate, channels, now=now)
        last = now
        now += LM.BLOCK_S
    return last

def _run_sentences(matcher: LevelMatcher, n: int, *, rms_db: float = -18.0,
                   peak_db: float = -2.2, seconds: float = 0.5) -> list[float]:
    """跑 n 句「合成译音」，返回每句句尾算出的增益。"""
    gains = []
    for _ in range(n):
        matcher.apply(_speech_like(rms_db, peak_db, seconds))
        gains.append(matcher.note_sentence_end())
    return gains

def _ramp_endpoints(src: bytes, out: bytes) -> tuple[float, float]:
    """句首 20ms 斜坡的**起点 / 终点**增益（dB）：只比有信号的帧，避开过零点。"""
    s = np.frombuffer(src, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    d = np.frombuffer(out, dtype="<i2").astype(np.float64).reshape(-1, 2)[:, 0]
    per_frame = np.full(s.size, np.nan)
    loud = np.abs(s) > 3000
    per_frame[loud] = 20.0 * np.log10(np.abs(d[loud]) / np.abs(s[loud]))
    win = 240
    assert int(np.count_nonzero(loud[:win])) > 10, "斜坡起点窗口内可比样本太少"
    assert int(np.count_nonzero(loud[LM.RAMP_FRAMES - win:LM.RAMP_FRAMES])) > 10, \
        "斜坡终点窗口内可比样本太少"
    head = float(np.nanmean(per_frame[:win]))
    tail = float(np.nanmean(per_frame[LM.RAMP_FRAMES - win:LM.RAMP_FRAMES]))
    return head, tail

_LEVEL_MIN_YAML = "ui:\n  lang: zh\noutput:\n  audio:\n    enabled: false\n"

def _load_level(extra: str = "") -> tuple[dict, str]:
    """把一段 YAML 落成临时 config.yaml 并 `load_config`，返回 (归一化后的 level 段, 留痕)。

    留痕捕回来既保住测试输出干净，又能断言「不静默」。全程离线：只读临时文件。

    ⚠️ **必须 hermetic：三步逐个封掉凭据来源**。`load_config` 内部会调 `load_api_key()`，
    而后者在「界面保存的 key」存在时会打一行 `[config] API key 来源：…`，被本函数当留痕捕回来
    → 与多处 `assert log == ""` 冲突。那会让用例**只在开发者本机红（存过 key）、CI 与无 key
    机器绿**：实测 Arch 本机 `rc=1`、空 HOME `rc=0`（同一提交）。故在此逐条封：① 界面保存
    （`credentials._storage_dir_override` → 空目录）② `DASHSCOPE_API_KEY` ③ `~/.bailian/config.json`
    （HOME/USERPROFILE 指向空目录，Windows 上 `expanduser("~")` 用后者）。用例结果与机器状态无关。
    """
    import contextlib
    import io
    import os
    import tempfile

    from vlt import credentials
    from vlt.config import load_config

    p = Path(tempfile.mkdtemp(prefix="vlt-level-cfg-")) / "config.yaml"
    p.write_text(_LEVEL_MIN_YAML + extra, encoding="utf-8")

    empty_home = Path(tempfile.mkdtemp(prefix="vlt-level-home-"))
    saved_override = credentials._storage_dir_override
    saved_env = {k: os.environ.pop(k) for k in ("DASHSCOPE_API_KEY",) if k in os.environ}
    saved_homes = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    credentials._storage_dir_override = lambda: empty_home / ".vrchat-livetranslate"
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(empty_home)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cfg = load_config(p, require_key=False)
    finally:
        credentials._storage_dir_override = saved_override
        os.environ.update(saved_env)
        for k, v in saved_homes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    audio = (cfg.output or {}).get("audio") or {}
    return (audio.get("level") or {}), buf.getvalue()

__all__ = ['LEVEL_FLOOR_DB', 'LM', 'LevelConfig', 'LevelMatcher', 'MODE_FIXED', 'MODE_FOLLOW_MIC', 'MODE_OFF', 'MicReference', 'Path', 'TranslatedLevel', '_amp_for_rms_db', '_feed_voiced', '_load_level', '_ramp_endpoints', '_run_sentences', '_silence', '_sine', '_speech_like', 'apply_gain_db', 'math', 'np']
