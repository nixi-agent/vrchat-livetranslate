"""译音音量匹配：把「译音」这条腿的电平对齐到「麦克风」这条腿。

为什么必须在程序内修：原声直通与译音**共用同一条**虚拟声卡输出流，VRChat 里的麦克风
音量只能整体调 —— 两条腿的**相对**失配（实测译音偏响 1.7 ~ 14.7 dB）在 VRChat 那边
无处可修。

本模块**只做纯逻辑**（不碰设备、不 import engine —— engine 已经 import
output.virtualmic，反向 import 会成环），所以可以离线逐条测（见 tests/test_level_match.py）。
"""
from __future__ import annotations

import math
import threading
from collections import deque

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


def block_db(pcm_s16: bytes) -> float:
    """一块 s16le PCM 的 RMS 电平（dBFS，满刻度 = 0）；纯静音回落 LEVEL_FLOOR_DB。

    这是「块电平」的**唯一实现**：`vlt/engine.py` 的 `chunk_level_db`（输入门限那条腿）
    委托到这里，不再是两处各写一份。可安全委托的原因：engine **本来就** import 本模块
    （engine → output.level_match 单向），所以反向委托不成环 —— 本模块仍然不 import engine。
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


def _scale_and_limit(x: np.ndarray, gains, ceiling_dbfs: float) -> bytes:  # noqa: ANN001
    """「增益 + 天花板限幅」的**唯一实现**：float64 样本 × 可广播的增益 → s16le 字节。

    `apply_gain_db`（整块一个标量增益）与 `LevelMatcher.apply`（每帧一个增益，句首斜坡）
    共用它 —— 以前两处各写一份限幅，改一处忘另一处就会「固定增益削波、跟随麦克风不削波」。
    """
    return _limit(x * gains, ceiling_dbfs).astype("<i2").tobytes()


def format_db(v: float | None) -> str:
    """电平/增益读数的**唯一**格式：没有测量值就是 `—`（绝不拿 0.0 冒充「测到了 0 dBFS」）。

    日志（`engine._note_level_sentence_end`）与界面读数（`gui_proxy._level_set_readout`）
    以前各留了一份逐字相同的实现，收敛到这里。
    """
    return "—" if v is None else f"{float(v):+.1f}"


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
    x = arr.astype(np.float64)
    return _scale_and_limit(x, 10.0 ** (float(db) / 20.0), ceiling_dbfs)


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

    ⚠️ **跨线程**：`feed` 跑在代理的麦克风泵线程，`db` / `valid` / `count` 跑在 Tk 线程
    （设置窗那条 500ms 读数 tick）。所有共享状态（`_blocks` / `_last_ts` / `_pending` /
    `_cfg`）都在 `_lock` 里访问 —— 以前 `db()` 直接在 Tk 线程 `sorted(deque)`，泵线程同时
    `append/popleft` 就会偶发 `RuntimeError: deque mutated during iteration`，而它被调用方的
    `except Exception` 吞掉，用户看到的只是读数无缘无故闪一下 `—`。
    """

    __slots__ = ("_cfg", "_blocks", "_last_ts", "_pending", "_lock")

    def __init__(self, cfg: LevelConfig) -> None:
        self._cfg = cfg
        self._blocks: deque[float] = deque()
        self._last_ts: float | None = None
        self._pending: np.ndarray = np.empty(0, dtype=np.float64)
        self._lock = threading.Lock()

    def set_config(self, cfg: LevelConfig) -> None:
        """换参数但**不清历史**：UI 改一下滑块不该逼用户重新说 0.8 秒话。"""
        with self._lock:
            self._cfg = cfg
            self._trim()

    @property
    def cfg(self) -> LevelConfig:
        with self._lock:
            return self._cfg

    def _trim(self) -> None:
        """按窗口裁掉最旧的块。**调用方必须已持有 `_lock`**（feed / set_config）。"""
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
        # 与共享状态无关的转换留在锁外：泵线程每 100ms 一块，别把 numpy 也串行化。
        x = arr[:usable].astype(np.float64) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        sr = max(1, int(rate or 16000))
        per_block = max(1, int(round(sr * BLOCK_S)))
        with self._lock:
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
        with self._lock:
            if len(self._blocks) < self._cfg.mic_min_blocks:
                return None
            snapshot = list(self._blocks)   # 先快照：排序绝不在遍历活 deque 时做
        return _median(snapshot)

    def valid(self, now: float) -> bool:
        """样本够、且最近一次有声观测没过 `mic_window_s`。"""
        with self._lock:
            if len(self._blocks) < self._cfg.mic_min_blocks:
                return False
            if self._last_ts is None:
                return False
            return (now - self._last_ts) <= self._cfg.mic_window_s

    def count(self) -> int:
        with self._lock:
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


from .level_matcher import LevelMatcher as LevelMatcher
