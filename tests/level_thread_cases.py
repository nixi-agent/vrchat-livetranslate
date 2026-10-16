"""level match regression cases; shared offline fixtures."""
from __future__ import annotations

from level_match_fixtures import (
    LM,
    LevelConfig,
    LevelMatcher,
    MODE_FIXED,
    MicReference,
    Path,
    TranslatedLevel,
    _amp_for_rms_db,
    _load_level,
    _sine,
    _speech_like,
)

def test_mic_reference_is_thread_safe():
    """★ `feed` 跑在代理的麦克风泵线程，`db`/`valid`/`count` 跑在 Tk 线程（500ms 读数 tick）。

    以前 `db()` 直接在 Tk 线程 `sorted(deque)`，泵线程同时 `append/popleft` 就会偶发
    `RuntimeError: deque mutated during iteration` —— 而它被调用方的 `except Exception`
    吞掉，用户看到的只是读数无缘无故闪一下 `—`（最难查的那类故障）。
    """
    import threading

    ref = MicReference(LevelConfig())
    lock = getattr(ref, "_lock", None)
    assert lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release"), \
        "MicReference 必须自带一把锁（共享状态：_blocks / _last_ts / _pending / _cfg）"
    assert "_lock" in MicReference.__slots__, "__slots__ 里漏了 _lock"

    amp = _amp_for_rms_db(-24.0)
    block = _sine(amp, int(16000 * LM.BLOCK_S))
    errors: list[tuple[str, Exception]] = []
    stop = threading.Event()

    def feeder(idx: int) -> None:
        try:
            t0 = float(idx)
            for i in range(300):
                ref.feed(block, 16000, 1, now=t0 + i * LM.BLOCK_S)
        except Exception as exc:                            # noqa: BLE001
            errors.append((f"feed#{idx}", exc))

    def reader(idx: int) -> None:
        try:
            while not stop.is_set():
                ref.db()
                ref.count()
                ref.valid(1e6)
                _ = ref.cfg
        except Exception as exc:                            # noqa: BLE001
            errors.append((f"read#{idx}", exc))

    def twiddler() -> None:
        # 只翻 `mic_min_blocks`，**不动** `mic_window_s`：窗口决定 `_trim` 的容量
        # （max(min_blocks, window/BLOCK_S)），把它改小就会裁掉块，让「900 块全在」无从断言。
        try:
            flip = False
            while not stop.is_set():
                flip = not flip
                ref.set_config(LevelConfig(mic_min_blocks=4 if flip else 8))
        except Exception as exc:                            # noqa: BLE001
            errors.append(("set_config", exc))

    threads = ([threading.Thread(target=reader, args=(i,), daemon=True) for i in range(3)]
               + [threading.Thread(target=twiddler, daemon=True)]
               + [threading.Thread(target=feeder, args=(i,), daemon=True) for i in range(3)])
    for th in threads[:4]:
        th.start()
    for th in threads[4:]:
        th.start()
    for th in threads[4:]:
        th.join(timeout=30.0)
    stop.set()
    for th in threads[:4]:
        th.join(timeout=10.0)
    assert not errors, f"并发下抛异常了：{errors[:3]}"
    assert not any(th.is_alive() for th in threads), "有线程没退（锁没放？）"

    # 结果仍然自洽：3×300 块有声语音都进了队列，中位数还是 −24 dBFS
    n = ref.count()
    assert n == 900, f"900 块有声语音一块都不该丢，实际 {n}"
    got = ref.db()
    assert got is not None and abs(got - (-24.0)) < 1.0, f"并发写入后估计失真：{got!r}"
    before = ref.count()
    for _ in range(200):
        ref.db()
    assert ref.count() == before, "db() 只读，不许改队列长度"
    print(f"  线程安全：3 泵 + 3 读 + 1 改参数并发无异常；{n} 块全在、中位数 {got:.2f} dBFS、"
          "db() 不改队列 OK")

def test_level_matcher_is_thread_safe():
    """★ `LevelMatcher` 的共享态也要互斥：`apply()` 有**两个**调用者。

    说话译音走 `Engine._on_audio`（事件循环线程），打字译音走 `Engine._speak_stream`
    （`asyncio.to_thread` 的工作线程）——而 `_speak_lock` 只串行化两条「打字」路径，管不到
    说话那条腿。两句重叠时 `_tts._sumsq/_n/_peak`（`+=` 会丢更新）与
    `_ramping/_ramp_done_frames` 会被交错读改写：句子电平互相污染、句首斜坡丢帧。

    这条用例**钉的是互斥本身**，不是「恰好没抛异常」：把一个记录并发进入次数的
    `TranslatedLevel` 换进 `_tts`，只要两线程能同时进 `apply()`，`feed` 就会被并发进入
    （修复前实测会 ≥2）——「全绿」区分不出空转的用例和有效的用例，所以判据必须是
    「互斥成立」这种**去掉锁就必然变红**的性质。
    """
    import threading
    import time

    m = LevelMatcher(LevelConfig(mode=MODE_FIXED, fixed_gain_db=-6.0))
    lock = getattr(m, "_lock", None)
    assert lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release"), \
        "LevelMatcher 必须自带一把锁（_cfg/_gain_db/_ramping/_ramp_*/_tts 都是共享态）"
    assert "_lock" in LevelMatcher.__slots__, "__slots__ 里漏了 _lock"

    class _OverlapWatch(TranslatedLevel):
        """只多一个「同一时刻有几个人在 feed 里」的计数器，其余是真实现。"""

        def __init__(self) -> None:
            super().__init__()
            self._inside = 0
            self.max_inside = 0
            self._guard = threading.Lock()

        def feed(self, pcm_s16: bytes) -> None:
            with self._guard:
                self._inside += 1
                self.max_inside = max(self.max_inside, self._inside)
            time.sleep(0.002)               # 放大窗口：2ms 足够让另一个线程挤进来
            try:
                super().feed(pcm_s16)
            finally:
                with self._guard:
                    self._inside -= 1

    watch = _OverlapWatch()
    m._tts = watch                          # 只替换累积器；apply 的其余逻辑走真实现
    block = _speech_like(-18.0, -2.2, 0.1)
    errors: list[Exception] = []
    stop = threading.Event()

    def _audio_thread() -> None:
        try:
            for _ in range(60):
                out = m.apply(block)
                assert len(out) == len(block), "输出字节数必须恒等于输入"
        except Exception as exc:                            # noqa: BLE001
            errors.append(exc)

    def _ui_thread() -> None:
        try:
            while not stop.is_set():
                m.status()
                m.gain_db()
                m.mic_db()
                m.cfg
                m.set_config(LevelConfig(mode=MODE_FIXED, fixed_gain_db=-6.0))
        except Exception as exc:                            # noqa: BLE001
            errors.append(exc)

    audio = [threading.Thread(target=_audio_thread, daemon=True) for _ in range(3)]
    ui = threading.Thread(target=_ui_thread, daemon=True)
    ui.start()
    for th in audio:
        th.start()
    for th in audio:
        th.join(timeout=30.0)
    stop.set()
    ui.join(timeout=10.0)

    assert not errors, f"并发下抛异常了：{errors[:3]}"
    assert not any(th.is_alive() for th in audio + [ui]), "有线程没退（锁没放？）"
    assert watch.max_inside == 1, (
        f"{watch.max_inside} 个线程同时进了 apply()（_tts.feed 被并发进入）—— "
        "斜坡推进与电平累积会丢更新，必须全部进 _lock")
    print(f"  线程安全（LevelMatcher）：3 音频线程 × 60 块 + 1 界面线程并发无异常；"
          f"_tts.feed 最大并发进入 = {watch.max_inside}（互斥成立）OK")

def test_module_has_no_device_or_engine_dependency():
    """本模块必须**离线**：不 import engine（会成环）、不 import 任何音频设备库。

    用 AST 看**真实的 import 语句**而不是子串匹配 —— level_match.py 自己的文档字符串里
    就写着「不 import engine」，子串匹配会自己绊自己。
    """
    import ast

    mods: set[str] = set()
    source = Path(LM.__file__)
    for path in (source, source.with_name("level_matcher.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    internal = node.level == 1 and (
                        node.module == "level_matcher"
                        or (node.module is None and all(a.name == "level_match" for a in node.names))
                    )
                    if not internal:
                        mods.add("vlt." + (node.module or ""))
                elif node.module:
                    mods.add(node.module.split(".")[0])
    banned = {"sounddevice", "pyaudio", "engine", "platform", "tkinter",
              "vlt.engine", "vlt.platform", "vlt.audio_dsp"}
    hit = sorted(mods & banned)
    assert not hit, f"level_match.py 必须离线可测，却 import 了 {hit}（全部：{sorted(mods)}）"
    assert not any(m.startswith("vlt") for m in mods), \
        f"只许依赖标准库 + numpy（否则 engine → virtualmic → level_match 会成环）：{sorted(mods)}"
    assert not hasattr(LM, "open_mic"), "纯逻辑模块不该有开设备的入口"
    print(f"  离线约束：level_match.py 只 import {sorted(mods)}（无 engine/设备库）OK")

def test_load_level_hermetic_against_saved_key() -> None:
    """★ `_load_level` 必须与「本机是否存过 API key」无关（复审实测的非 hermetic 回归）。

    机器上存过界面 key 时 `load_api_key()` 会打一行 `[config] API key 来源：…`；`_load_level`
    若不封来源，这行会被当留痕捕回来 → 同一条断言在**开发者本机红、CI / 无 key 机器绿**。
    这里造一个含假 key 的凭据目录（= 模拟「存过 key 的机器」），断言留痕仍为空。
    """
    import tempfile

    from vlt import credentials

    fake = Path(tempfile.mkdtemp(prefix="vlt-fake-cred-"))
    (fake / "api_key.txt").write_text("sk-" + "F4k3K3y" * 14, encoding="utf-8")
    saved = credentials._storage_dir_override
    credentials._storage_dir_override = lambda: fake
    try:
        lv, log = _load_level("    level:\n      mode: follow_mic\n")
        assert lv["mode"] == "follow_mic", lv
        assert log == "", f"本机存过 key 不得让合法配置留痕：{log!r}"
    finally:
        credentials._storage_dir_override = saved
    print("  `_load_level` hermetic：本机存过界面 key（假凭据目录）时仍零留痕 OK")
