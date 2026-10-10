"""译音音量匹配：把「译音」这条腿的电平对齐到「麦克风」这条腿。

为什么必须在程序内修：原声直通与译音**共用同一条**虚拟声卡输出流，VRChat 里的麦克风
音量只能整体调 —— 两条腿的**相对**失配（实测译音偏响 1.7 ~ 14.7 dB）在 VRChat 那边
无处可修。

本模块**只做纯逻辑**（不碰设备、不 import engine —— engine 已经 import
output.virtualmic，反向 import 会成环），所以可以离线逐条测（见 tests/test_level_match.py）。
"""
from __future__ import annotations

import math
from collections import deque
from typing import Callable

import numpy as np

# 纯数字静音的回落电平（与 engine.chunk_level_db 同语义）：log10(0) 是 -inf，
# 不兜底就会把 -inf 一路带进增益计算。
LEVEL_FLOOR_DB = -120.0
# 低于这个电平的块**不算说话**：环境底噪 / 键盘声进来会把麦克风中位数拉成「很轻」，
# 于是译音被拼命提升 → 一旦对方真开口就炸耳。
VOICED_FLOOR_DB = -50.0
# 电平统计的块长（秒）。与抓取的 100ms 块对齐，也是「一块 = 一次观测」的粒度。
BLOCK_S = 0.1
# 增益变化时，新句**第一块的前 20ms** 做线性斜坡，避免爆音（48k → 960 帧）。
RAMP_FRAMES = int(0.02 * 48000)

MODE_OFF = "off"
MODE_FIXED = "fixed"
MODE_FOLLOW_MIC = "follow_mic"
_MODES = (MODE_OFF, MODE_FIXED, MODE_FOLLOW_MIC)

# ---- 默认值（config.example.yaml 里逐字对应，改一处要改两处）----
DEFAULT_MODE = MODE_OFF
DEFAULT_FIXED_GAIN_DB = 0.0
DEFAULT_OFFSET_DB = 0.0
DEFAULT_MAX_BOOST_DB = 6.0
DEFAULT_MAX_CUT_DB = 24.0
DEFAULT_CEILING_DBFS = -1.0
DEFAULT_MAX_STEP_DB = 3.0
DEFAULT_MIC_MIN_BLOCKS = 8
DEFAULT_MIC_WINDOW_S = 120.0

# 合法区间（与 vlt/config.py 的 output.audio.level 归一化保持一致）
RANGES = {
    "max_cut_db": (0.0, 36.0),
    "max_boost_db": (0.0, 24.0),
    "ceiling_dbfs": (-6.0, 0.0),     # 0 = 满刻度，不含（贴边必削波）
    "offset_db": (-24.0, 24.0),
    "fixed_gain_db": (-36.0, 12.0),
    "max_step_db": (0.5, 12.0),
    "mic_min_blocks": (1, 100),
    "mic_window_s": (5.0, 600.0),
}


def _rms_db(x: np.ndarray) -> float:
    """已归一化到 [-1, 1] 的 float 数组 → RMS dBFS（纯静音回落 LEVEL_FLOOR_DB）。"""
    if x.size == 0:
        return LEVEL_FLOOR_DB
    rms = float(np.sqrt(float(np.mean(x * x))))
    if rms <= 0.0:
        return LEVEL_FLOOR_DB
    return max(LEVEL_FLOOR_DB, 20.0 * math.log10(rms))


def _block_db(pcm_s16: bytes) -> float:
    """一块 s16le PCM 的 RMS 电平（dBFS，0 = 满刻度）；纯静音回落 LEVEL_FLOOR_DB。

    与 engine.chunk_level_db **同语义**（这里刻意私有一份：本模块不能 import engine，
    否则 engine → output.virtualmic → output.level_match → engine 成环）。
    """
    if not pcm_s16:
        return LEVEL_FLOOR_DB
    arr = np.frombuffer(pcm_s16, dtype="<i2")
    if arr.size == 0:
        return LEVEL_FLOOR_DB
    return _rms_db(arr.astype(np.float64) / 32768.0)


def _peak_db(pcm_s16: bytes) -> float:
    """一块 s16le PCM 的峰值电平（dBFS）；纯静音回落 LEVEL_FLOOR_DB。"""
    if not pcm_s16:
        return LEVEL_FLOOR_DB
    arr = np.frombuffer(pcm_s16, dtype="<i2")
    if arr.size == 0:
        return LEVEL_FLOOR_DB
    peak = float(np.max(np.abs(arr.astype(np.float64)))) / 32768.0
    if peak <= 0.0:
        return LEVEL_FLOOR_DB
    return max(LEVEL_FLOOR_DB, 20.0 * math.log10(peak))


def _limit(x: np.ndarray, ceiling_dbfs: float) -> np.ndarray:
    """就地硬限幅到 ceiling_dbfs，并落回 int16 的表示范围（防绕回）。"""
    limit = 32768.0 * (10.0 ** (float(ceiling_dbfs) / 20.0))
    np.clip(x, -limit, limit, out=x)
    np.rint(x, out=x)
    np.clip(x, -32768.0, 32767.0, out=x)
    return x


def apply_gain_db(pcm_s16: bytes, db: float, *, ceiling_dbfs: float = -1.0) -> bytes:
    """给 s16le PCM 施加固定增益，并在 ceiling_dbfs 处**硬限幅**（int16 安全）。

    `db == 0.0` 时**逐字节原样返回**：既是性能（译音这条腿每 100ms 一块），也是 P0
    要求 —— `mode=off` 必须与升级前行为逐字节一致。
    """
    if not pcm_s16 or db == 0.0:
        return pcm_s16
    arr = np.frombuffer(pcm_s16, dtype="<i2")
    if arr.size == 0:
        return pcm_s16
    x = arr.astype(np.float64) * (10.0 ** (float(db) / 20.0))
    return _limit(x, ceiling_dbfs).astype("<i2").tobytes()


def _norm_mode(value) -> str | None:
    """把配置里的 `mode` 归一成 off/fixed/follow_mic；认不出来返回 None（调用方留痕回落）。

    ⚠️ YAML 1.1（PyYAML）把**裸词** `off` / `on` / `no` / `yes` 解析成布尔值，而本段的
    默认模式在 config.example.yaml 里就写作 `mode: off` —— 不做这层还原，照模板写的
    **正确**配置反而会被判非法、刷一条告警。
    """
    if value is False:
        return MODE_OFF
    if isinstance(value, str) and value.strip() in _MODES:
        return value.strip()
    return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _median(values) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


class LevelConfig:
    """译音音量匹配的参数（从 `output.audio.level` 解析 + 校验 + 非法回落）。"""

    __slots__ = ("mode", "fixed_gain_db", "offset_db", "max_boost_db", "max_cut_db",
                 "ceiling_dbfs", "max_step_db", "mic_min_blocks", "mic_window_s")

    def __init__(self, *, mode: str = DEFAULT_MODE,
                 fixed_gain_db: float = DEFAULT_FIXED_GAIN_DB,
                 offset_db: float = DEFAULT_OFFSET_DB,
                 max_boost_db: float = DEFAULT_MAX_BOOST_DB,
                 max_cut_db: float = DEFAULT_MAX_CUT_DB,
                 ceiling_dbfs: float = DEFAULT_CEILING_DBFS,
                 max_step_db: float = DEFAULT_MAX_STEP_DB,
                 mic_min_blocks: int = DEFAULT_MIC_MIN_BLOCKS,
                 mic_window_s: float = DEFAULT_MIC_WINDOW_S) -> None:
        self.mode = mode
        self.fixed_gain_db = float(fixed_gain_db)
        self.offset_db = float(offset_db)
        self.max_boost_db = float(max_boost_db)
        self.max_cut_db = float(max_cut_db)
        self.ceiling_dbfs = float(ceiling_dbfs)
        self.max_step_db = float(max_step_db)
        self.mic_min_blocks = int(mic_min_blocks)
        self.mic_window_s = float(mic_window_s)

    def __repr__(self) -> str:
        return (f"LevelConfig(mode={self.mode!r}, fixed_gain_db={self.fixed_gain_db:g}, "
                f"offset_db={self.offset_db:g}, max_boost_db={self.max_boost_db:g}, "
                f"max_cut_db={self.max_cut_db:g}, ceiling_dbfs={self.ceiling_dbfs:g}, "
                f"max_step_db={self.max_step_db:g}, mic_min_blocks={self.mic_min_blocks}, "
                f"mic_window_s={self.mic_window_s:g})")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "LevelConfig":
        """从 dict 建配置；脏值**留痕并回落默认**（绝不静默、也绝不抛异常）。

        不抛异常的理由：这段在引擎启动路径上，一个写错的 yaml 值不该把整条翻译腿弄挂。
        """
        if not isinstance(raw, dict):
            return cls()
        kw: dict = {}
        base = cls()

        mode = raw.get("mode", DEFAULT_MODE)
        norm = _norm_mode(mode)
        if norm is not None:
            kw["mode"] = norm
        else:
            print(f"[level] ⚠️ mode={mode!r} 非法（应为 {MODE_OFF}/{MODE_FIXED}/"
                  f"{MODE_FOLLOW_MIC}）→ 回落默认 {DEFAULT_MODE}", flush=True)

        for key in ("fixed_gain_db", "offset_db", "max_boost_db", "max_cut_db",
                    "ceiling_dbfs", "max_step_db", "mic_window_s"):
            if key not in raw:
                continue
            lo, hi = RANGES[key]
            val = raw[key]
            try:
                fval = float(val)
            except (TypeError, ValueError):
                fval = float("nan")
            if not math.isfinite(fval):
                print(f"[level] ⚠️ {key}={val!r} 非法 → 回落默认 "
                      f"{getattr(base, key):g}", flush=True)
                continue
            ok = (lo <= fval < 0.0) if key == "ceiling_dbfs" else (lo <= fval <= hi)
            if not ok:
                print(f"[level] ⚠️ {key}={fval:g} 超出 [{lo:g}, {hi:g}"
                      f"{'）' if key == 'ceiling_dbfs' else ']'} → 回落默认 "
                      f"{getattr(base, key):g}", flush=True)
                continue
            kw[key] = fval

        if "mic_min_blocks" in raw:
            lo, hi = RANGES["mic_min_blocks"]
            val = raw["mic_min_blocks"]
            try:
                ival = int(val)
                ok = lo <= ival <= hi and float(val) == float(ival)
            except (TypeError, ValueError):
                ival, ok = DEFAULT_MIC_MIN_BLOCKS, False
            if ok:
                kw["mic_min_blocks"] = ival
            else:
                print(f"[level] ⚠️ mic_min_blocks={val!r} 非法（应为 [{lo}, {hi}] 整数）"
                      f"→ 回落默认 {DEFAULT_MIC_MIN_BLOCKS}", flush=True)

        return cls(**kw)


class MicReference:
    """麦克风「说话电平」的鲁棒估计：只收有声块，取滚动窗口内的**中位数**。

    中位数而不是均值：一次咳嗽 / 一下拍麦不该把整句译音的音量带跑（见测试里的
    +20 dB 尖峰用例）。
    """

    __slots__ = ("_cfg", "_blocks", "_last_ts", "_pending")

    def __init__(self, cfg: LevelConfig) -> None:
        self._cfg = cfg
        self._blocks: deque[float] = deque()
        self._last_ts: float | None = None
        self._pending: np.ndarray = np.empty(0, dtype=np.float64)

    def set_config(self, cfg: LevelConfig) -> None:
        """换参数但**不清历史**：UI 改一下滑块不该逼用户重新说 0.8 秒话。"""
        self._cfg = cfg
        self._trim()

    @property
    def cfg(self) -> LevelConfig:
        return self._cfg

    def _trim(self) -> None:
        cap = max(self._cfg.mic_min_blocks, int(self._cfg.mic_window_s / BLOCK_S))
        while len(self._blocks) > cap:
            self._blocks.popleft()

    def feed(self, pcm: bytes, rate: int, channels: int, *, now: float) -> None:
        """喂一块麦克风 PCM（任意采样率 / 声道数）；只有**有声块**进队列。"""
        if not pcm:
            return
        arr = np.frombuffer(pcm, dtype="<i2")
        if arr.size == 0:
            return
        ch = max(1, int(channels or 1))
        usable = arr.size - (arr.size % ch)
        if usable <= 0:
            return
        x = arr[:usable].astype(np.float64) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        sr = max(1, int(rate or 16000))
        per_block = max(1, int(round(sr * BLOCK_S)))
        if self._pending.size:
            x = np.concatenate((self._pending, x))
            self._pending = np.empty(0, dtype=np.float64)
        n_blocks = x.size // per_block
        if n_blocks <= 0:
            # 块比 100ms 还短：攒到够一块再算。不攒就会把碎片当成一整块（观测数虚高）。
            self._pending = x
            return
        head = x[:n_blocks * per_block].reshape(n_blocks, per_block)
        rest = x[n_blocks * per_block:]
        if rest.size:
            self._pending = rest
        ms = np.sqrt(np.mean(head * head, axis=1))
        got = False
        for rms in ms:
            r = float(rms)
            if r <= 0.0:
                continue
            db = max(LEVEL_FLOOR_DB, 20.0 * math.log10(r))
            if db < VOICED_FLOOR_DB:
                continue        # 底噪 / 静音：不进队列，也不刷新 last_ts
            self._blocks.append(db)
            got = True
        if got:
            self._last_ts = now
        self._trim()

    def db(self) -> float | None:
        """当前估计（dBFS）；样本不足 `mic_min_blocks` → None（**不猜**）。"""
        if len(self._blocks) < self._cfg.mic_min_blocks:
            return None
        return _median(self._blocks)

    def valid(self, now: float) -> bool:
        """样本够、且最近一次有声观测没过 `mic_window_s`。"""
        if len(self._blocks) < self._cfg.mic_min_blocks:
            return False
        if self._last_ts is None:
            return False
        return (now - self._last_ts) <= self._cfg.mic_window_s

    def count(self) -> int:
        return len(self._blocks)


class TranslatedLevel:
    """一句译音的电平 / 峰值累积器（喂完一句再读，然后 reset）。"""

    __slots__ = ("_sumsq", "_n", "_peak")

    def __init__(self) -> None:
        self.reset()

    def feed(self, pcm_s16: bytes) -> None:
        if not pcm_s16:
            return
        arr = np.frombuffer(pcm_s16, dtype="<i2")
        if arr.size == 0:
            return
        x = arr.astype(np.float64) / 32768.0
        self._sumsq += float(np.sum(x * x))
        self._n += int(x.size)
        peak = float(np.max(np.abs(x)))
        if peak > self._peak:
            self._peak = peak

    def level_db(self) -> float | None:
        """本句 RMS（dBFS）；一个样本都没有 → None。"""
        if self._n <= 0 or self._sumsq <= 0.0:
            return None
        return max(LEVEL_FLOOR_DB, 20.0 * math.log10(math.sqrt(self._sumsq / self._n)))

    def peak_db(self) -> float | None:
        if self._n <= 0 or self._peak <= 0.0:
            return None
        return max(LEVEL_FLOOR_DB, 20.0 * math.log10(self._peak))

    def reset(self) -> None:
        self._sumsq = 0.0
        self._n = 0
        self._peak = 0.0


class LevelMatcher:
    """引擎侧门面：`apply()` 累积本句电平并施加当前增益，句尾 `note_sentence_end()` 重算。

    增益**只在句首变**（句中变会造成音量台阶），且变化时对句首前 20ms 做线性斜坡。
    """

    __slots__ = ("_cfg", "_mic_db", "_gain_db", "_tts", "_last_tts_db", "_last_peak_db",
                 "_fallback", "_ever_ref", "_ramping", "_ramp_from", "_ramp_to",
                 "_ramp_done_frames")

    def __init__(self, cfg: LevelConfig,
                 mic_db: Callable[[], float | None] | None = None) -> None:
        self._cfg = cfg
        self._mic_db = mic_db
        self._gain_db = 0.0 if cfg.mode == MODE_OFF else cfg.fixed_gain_db
        self._tts = TranslatedLevel()
        self._last_tts_db: float | None = None
        self._last_peak_db: float | None = None
        self._fallback = False
        self._ever_ref = False
        self._ramping = False
        self._ramp_from = 0.0
        self._ramp_to = 0.0
        self._ramp_done_frames = RAMP_FRAMES

    # ---- 配置 / 状态 ----

    def set_config(self, cfg: LevelConfig) -> None:
        """UI 改参数后热更新：**下一句**生效（句中不突变，避免爆音）。"""
        self._cfg = cfg
        if cfg.mode == MODE_OFF:
            self._set_gain(0.0)
            self._fallback = False

    @property
    def cfg(self) -> LevelConfig:
        return self._cfg

    def gain_db(self) -> float:
        return self._gain_db

    def mic_db(self) -> float | None:
        if self._mic_db is None:
            return None
        try:
            return self._mic_db()
        except Exception:                                    # noqa: BLE001
            return None

    def status(self) -> dict:
        """给 UI 读数用（`—` 由 UI 判 None 决定，这里只给原始值）。"""
        headroom = None
        if self._last_peak_db is not None:
            headroom = self._cfg.ceiling_dbfs - (self._last_peak_db + self._gain_db)
        return {
            "mode": self._cfg.mode,
            "mic_db": self.mic_db(),
            "tts_db": self._last_tts_db,
            "gain_db": self._gain_db,
            "headroom_db": headroom,
            "fallback": self._fallback,
        }

    # ---- 句尾重算 ----

    def note_sentence_end(self) -> float:
        """一句译音说完：用**刚说完这句**的实测重算增益，返回新增益（dB）。

        顺序不可改（见 .qoder-brief.md §2.1）：模式 → 目标 → 限幅 → 峰值限幅 → 限速。
        """
        prev = self._gain_db
        self._last_tts_db = self._tts.level_db()
        self._last_peak_db = self._tts.peak_db()
        self._tts.reset()
        mode = self._cfg.mode

        if mode == MODE_OFF:
            self._fallback = False
            self._set_gain(0.0)
            return self._gain_db

        if mode == MODE_FIXED:
            g: float = self._cfg.fixed_gain_db
        elif mode == MODE_FOLLOW_MIC:
            ref = self.mic_db()
            if ref is not None:
                self._ever_ref = True
            if ref is None:
                # 参考取不到：**保住上一次的增益**（不归零、不炸）。从头到尾一次都没拿到
                # 过 → 回落固定增益。两种都置 fallback，调用方据此留痕（去重）。
                self._fallback = True
                g = prev if self._ever_ref else self._cfg.fixed_gain_db
            elif self._last_tts_db is None:
                # 首句还没有 tts 实测：用 prev（初值 = fixed_gain_db）。**不为「先测后放」
                # 缓冲任何东西** —— 那会给译音凭空加一整句的延迟。
                g = prev
            else:
                self._fallback = False
                g = ref + self._cfg.offset_db - self._last_tts_db
                g = _clamp(g, -self._cfg.max_cut_db, self._cfg.max_boost_db)
        else:
            g = prev

        if self._last_peak_db is not None:
            # 峰值限幅：**只降不升**。译音峰值实测已贴 −2.2 dBFS、天花板 −1 → 最多 +1.2 dB。
            g = min(g, self._cfg.ceiling_dbfs - self._last_peak_db)

        # 限速：句间增益变化不超过 max_step_db，防止音量一跳一跳。
        g = prev + _clamp(g - prev, -self._cfg.max_step_db, self._cfg.max_step_db)
        self._set_gain(g)
        return self._gain_db

    def _set_gain(self, new: float) -> None:
        if abs(new - self._gain_db) < 1e-9:
            return
        # 新句第一块的前 RAMP_FRAMES 帧做线性斜坡（旧增益 → 新增益），避免爆音。
        self._ramping = True
        self._ramp_from = self._gain_db
        self._ramp_to = new
        self._ramp_done_frames = 0
        self._gain_db = new

    # ---- 施加 ----

    def apply(self, pcm_48k_stereo_s16: bytes) -> bytes:
        """译音 PCM 的唯一入口：先累积本句电平，再施加当前增益。

        **输出字节数与输入完全一致**（下游抖动缓冲要靠它算时长）。`mode=off` 走短路，
        逐字节原样返回 —— 与升级前行为一致，且不付任何测量开销。
        """
        if not pcm_48k_stereo_s16 or self._cfg.mode == MODE_OFF:
            return pcm_48k_stereo_s16
        self._tts.feed(pcm_48k_stereo_s16)
        if self._gain_db == 0.0 and not self._ramping:
            return pcm_48k_stereo_s16
        arr = np.frombuffer(pcm_48k_stereo_s16, dtype="<i2")
        if arr.size == 0:
            return pcm_48k_stereo_s16
        frames = arr.size // 2
        if frames <= 0:
            return pcm_48k_stereo_s16
        # 48k 立体声恒为偶数个样本；万一来一个落单样本，也别让 reshape 抛 —— 原样带回，
        # 保证「输出字节数 == 输入字节数」这条硬约束不破。
        tail = pcm_48k_stereo_s16[frames * 4:]
        x = arr[:frames * 2].astype(np.float64)
        flat = 10.0 ** (self._gain_db / 20.0)

        if self._ramping:
            left = max(0, RAMP_FRAMES - self._ramp_done_frames)
            n = min(left, frames)
            gains = np.full(frames, flat, dtype=np.float64)
            if n > 0:
                ramp = np.linspace(self._ramp_from, self._ramp_to, n, endpoint=False)
                gains[:n] = 10.0 ** (ramp / 20.0)
            self._ramp_done_frames += frames
            if self._ramp_done_frames >= RAMP_FRAMES:
                self._ramping = False
                self._ramp_done_frames = RAMP_FRAMES
        else:
            gains = np.full(frames, flat, dtype=np.float64)

        y = (x.reshape(frames, 2) * gains[:, None]).reshape(-1)
        out = _limit(y, self._cfg.ceiling_dbfs).astype("<i2").tobytes()
        return out + tail if tail else out
