"""Sentence gain matching and ramp ownership; pure DSP with no device I/O."""
from __future__ import annotations
import threading
from typing import Callable
import numpy as np


def _m():
    from . import level_match
    return level_match


class LevelMatcher:
    """引擎侧门面：`apply()` 累积本句电平并施加当前增益，句尾 `note_sentence_end()` 重算。

    增益**只在句首变**（句中变会造成音量台阶），且变化时对句首前 20ms 做线性斜坡。

    ⚠️ **跨线程：全部共享状态都在 `_lock` 里**。`apply()` 有两个调用者 ——
    `Engine._on_audio`（事件循环线程）与 `Engine._speak_stream`（`asyncio.to_thread`
    的工作线程，`_speak_lock` 只串行化「打字」那两条，管不到说话那条腿）。说话译音与
    打字 TTS 重叠时，`_tts._sumsq/_n/_peak`（`+=` 会丢更新）与
    `_ramping/_ramp_done_frames` 会被两线程交错读改写：句子电平互相污染、增益算歪，
    句首斜坡的推进也会丢帧。`set_config` / `status` / `gain_db` / `mic_db` 跑在 Tk 线程，
    同样进锁。
    """

    __slots__ = ("_cfg", "_mic_db", "_gain_db", "_tts", "_last_tts_db", "_last_peak_db",
                 "_fallback", "_ever_ref", "_ramping", "_ramp_from", "_ramp_to",
                 "_ramp_done_frames", "_lock")

    def __init__(self, cfg: _m().LevelConfig,
                 mic_db: Callable[[], float | None] | None = None) -> None:
        self._cfg = cfg
        self._mic_db = mic_db
        self._gain_db = 0.0 if cfg.mode == _m().MODE_OFF else cfg.fixed_gain_db
        self._tts = _m().TranslatedLevel()
        self._last_tts_db: float | None = None
        self._last_peak_db: float | None = None
        self._fallback = False
        self._ever_ref = False
        self._ramping = False
        self._ramp_from = 0.0
        self._ramp_to = 0.0
        self._ramp_done_frames = _m().RAMP_FRAMES
        #: 用 **RLock**：本类内部有嵌套获取（`status`/`note_sentence_end`/`set_config`
        #: 持锁时还会调 `mic_db()`），普通 Lock 会自锁。跨线程语义与 Lock 相同。
        self._lock = threading.RLock()

    # ---- 配置 / 状态 ----

    def set_config(self, cfg: _m().LevelConfig) -> None:
        """UI 改参数后热更新：**下一句**生效（句中不突变，避免爆音）。

        两个例外，都是因为 `off` 期间 `apply()` 会短路：

        * **进入 `off`**：把斜坡状态一次清干净。不清的话 `_ramping` 会一直挂着
          （短路发生在斜坡推进之前），切回来时第一句从**上一个会话的旧增益**斜到 0 dB ——
          实测：`收敛 -11.75 → 切 off → 切回：-11.75 → -11.14 → -8.83 → -5.89 → +0.00`。
        * **从 `off` 切回 `fixed`/`follow_mic`**：立刻用 `_last_tts_db` / 当前参考重算一次，
          首句就是正确档位（不重算就得整句 0 dB 播完、句尾才对齐）。这一步**不限速** ——
          限速是防「句与句之间音量一跳一跳」，而这里是从「不加增益」重新起步，没有连续性
          可保；爆音由句首那 20ms 斜坡兜（从当前增益滑到新目标）。

        另外：新模式不是 `follow_mic` 时立刻清掉 `fallback` —— `fixed` 根本不看麦克风参考，
        不清就会在「切过来」到「下一句句尾」之间一直挂着一条过期状态（界面读数据此显示回落）。
        """
        with self._lock:
            was_off = self._cfg.mode == _m().MODE_OFF
            self._cfg = cfg
            if cfg.mode == _m().MODE_OFF:
                self._gain_db = 0.0
                self._ramping = False
                self._ramp_from = 0.0
                self._ramp_to = 0.0
                self._ramp_done_frames = _m().RAMP_FRAMES
                self._fallback = False
                return
            if cfg.mode != _m().MODE_FOLLOW_MIC:
                self._fallback = False
            if was_off:
                self._set_gain(self._compute_gain(self._gain_db, self._last_tts_db,
                                                  self._last_peak_db, limit_step=False))

    @property
    def cfg(self) -> _m().LevelConfig:
        with self._lock:
            return self._cfg

    def gain_db(self) -> float:
        with self._lock:
            return self._gain_db

    def mic_db(self) -> float | None:
        with self._lock:
            if self._mic_db is None:
                return None
            try:
                return self._mic_db()
            except Exception:                                # noqa: BLE001
                return None

    def status(self) -> dict:
        """给 UI 读数用（`—` 由 UI 判 None 决定，这里只给原始值）。

        整体持锁：四个字段必须来自**同一瞬间**的快照，否则界面会显示
        「增益是新的、余量是旧的」这种自相矛盾的读数。
        """
        with self._lock:
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
        """一句译音说完：用**刚说完这句**的实测重算增益，返回新增益（dB）。"""
        with self._lock:
            prev = self._gain_db
            self._last_tts_db = self._tts.level_db()
            self._last_peak_db = self._tts.peak_db()
            self._tts.reset()
            self._set_gain(self._compute_gain(prev, self._last_tts_db, self._last_peak_db))
            return self._gain_db

    def _compute_gain(self, prev: float, tts_db: float | None,
                      peak_db: float | None, *, limit_step: bool = True) -> float:
        """目标增益的**唯一**计算处：模式 → 目标 → 限幅 → 峰值限幅 → 限速（顺序不可改）。

        两个调用点共用它，参数来源不同：句尾（`note_sentence_end`）用刚测完这句的实测；
        从 `off` 切回时（`set_config`）用上一次留下的实测，并且**不限速**（见 `set_config`）。
        """
        mode = self._cfg.mode

        if mode == _m().MODE_OFF:
            self._fallback = False
            return 0.0

        if mode == _m().MODE_FIXED:
            # 固定增益与麦克风参考无关：上一次 `follow_mic` 失参考留下的 fallback 必须在这里
            # 清掉，否则切到 fixed 之后 `status()["fallback"]` 会一直挂着一条过期状态。
            self._fallback = False
            g: float = self._cfg.fixed_gain_db
        elif mode == _m().MODE_FOLLOW_MIC:
            ref = self.mic_db()
            if ref is not None:
                self._ever_ref = True
            if ref is None:
                # 参考取不到：**保住上一次的增益**（不归零、不炸）。从头到尾一次都没拿到
                # 过 → 回落固定增益。两种都置 fallback，调用方据此留痕（去重）。
                self._fallback = True
                g = prev if self._ever_ref else self._cfg.fixed_gain_db
            elif tts_db is None:
                # 首句还没有 tts 实测：用 prev（初值 = fixed_gain_db）。**不为「先测后放」
                # 缓冲任何东西** —— 那会给译音凭空加一整句的延迟。
                g = prev
            else:
                self._fallback = False
                g = ref + self._cfg.offset_db - tts_db
                g = _m()._clamp(g, -self._cfg.max_cut_db, self._cfg.max_boost_db)
        else:
            g = prev

        if peak_db is not None:
            # 峰值限幅：**只降不升**。译音峰值实测已贴 −2.2 dBFS、天花板 −1 → 最多 +1.2 dB。
            g = min(g, self._cfg.ceiling_dbfs - peak_db)

        if not limit_step:
            return g
        # 限速：句间增益变化不超过 max_step_db，防止音量一跳一跳。
        return prev + _m()._clamp(g - prev, -self._cfg.max_step_db, self._cfg.max_step_db)

    def _set_gain(self, new: float) -> None:
        if abs(new - self._gain_db) < 1e-9:
            return
        # 新句第一块的前 _m().RAMP_FRAMES 帧做线性斜坡（旧增益 → 新增益），避免爆音。
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

        整段在 `_lock` 里（见类 docstring）：本方法有两个线程在调，句首斜坡的
        推进与 `_tts` 的累积都是读改写。
        """
        with self._lock:
            if not pcm_48k_stereo_s16:
                return pcm_48k_stereo_s16
            if self._cfg.mode == _m().MODE_OFF:
                # 防御：off 期间永远走不到下面的斜坡推进，挂着 `_ramping` 只会让切回来的
                # 第一句从旧增益斜到 0（`set_config` 进 off 时已经清过一次）。
                self._ramping = False
                self._ramp_done_frames = _m().RAMP_FRAMES
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
            x = arr[:frames * 2].astype(np.float64).reshape(frames, 2)
            flat = 10.0 ** (self._gain_db / 20.0)

            if self._ramping:
                left = max(0, _m().RAMP_FRAMES - self._ramp_done_frames)
                n = min(left, frames)
                gains = np.full(frames, flat, dtype=np.float64)
                if n > 0:
                    # ⚠️ 按**已走帧数**切片，不是每次都从 0 铺满整条斜坡：块长可以小于
                    # 20ms（Linux 的 `pw-cat` 泵实测 5ms/块），从 0 重铺就会让每个块边界
                    # 把整条斜坡重来一遍（实测每块首帧 −0.65 dB、末帧 −11.95 dB，第 5 块
                    # 才恒 −12）→ 听感是 ~200Hz 的泵动/锯齿。
                    # 首块 ≥ 20ms 时 t = 0..n−1 与原来的 `linspace(from, to, n,
                    # endpoint=False)` 逐步等价 —— 既有行为不变。
                    t = np.arange(self._ramp_done_frames, self._ramp_done_frames + n,
                                  dtype=np.float64)
                    ramp = (self._ramp_from
                            + (self._ramp_to - self._ramp_from) * (t / _m().RAMP_FRAMES))
                    gains[:n] = 10.0 ** (ramp / 20.0)
                self._ramp_done_frames += frames
                if self._ramp_done_frames >= _m().RAMP_FRAMES:
                    self._ramping = False
                    self._ramp_done_frames = _m().RAMP_FRAMES
            else:
                gains = np.full(frames, flat, dtype=np.float64)

            out = _m()._scale_and_limit(x, gains[:, None], self._cfg.ceiling_dbfs)
            return out + tail if tail else out
