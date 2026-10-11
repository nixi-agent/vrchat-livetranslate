"""首次启用麦克风代理的说明弹窗：**只弹一次** + headless 绝不污染用户配置。

跑法：.venv/Scripts/python.exe tests/test_proxy_hint.py

为什么单独一份：这条链路横跨「配置标记 + GUI 接线 + 弹窗」三处，任何一处断掉的表现都很安静 ——
  · 标记没落盘 → 每次启动都弹（用户被烦到）；
  · headless 也落盘 → `--self-test` / 打包自检把用户的标记改成「已提示」，用户永远看不到说明（**踩过的形态**）；
  · 早退判断丢掉 → 代理每次启动都弹。
所以三条各自一条用例，另加「关掉代理时不该弹」与「关窗幂等」。

★ 用户报的缺陷（本文件 ⑥⑦ 两条专门守它）：「我期望只有第一次开的时候会弹一次，以后永远都不会弹，
  现在我发现我每次启动程序都会弹这一下」。根因：**老用户的 config.yaml 里没有 `output.audio.proxy`
  整段**，而旧的落盘写法 `_yaml_set_in_text` 遇到缺父键是**静默 no-op**（原样返回）→ 标记永远写不进
  磁盘 → 每次启动都重新弹。既有用例的 harness 里**预先写了** proxy 段，所以它测不出这个缺陷（假绿）。

打桩纪律（与 test_proxy_wiring 同一条）：不碰任何真实音频设备 ——
`platform.create_mic_proxy` 换成假代理，GUI 用 headless，弹窗用 opener 替身（CI 上不开 Tk 窗）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

CONFIG_TEXT = (
    "ui:\n"
    "  lang: zh\n"
    "session:\n"
    "  api_key: test-key-not-real\n"
    "output:\n"
    "  capture:\n"
    "    mic_device: ''\n"
    "  audio:\n"
    "    enabled: true\n"
    "    device: ['FakeCard Output']\n"
    "    sample_rate: 48000\n"
    "    buffer_ms: 300\n"
    "    max_buffer_ms: 2000\n"
    "    proxy:\n"
    "      enabled: {enabled}\n"
    "      passthrough_buffer_ms: 150\n"
)

#: 老用户的 config.yaml（更早的模板生成）：`output.audio` 在，但**整段 proxy 都没有**。
#: 这就是用户那台机器上的形态 —— 缺陷只在它上面现形（见文件头的 ⑥）。
LEGACY_CONFIG_TEXT = (
    "# 老用户从更早的模板生成的配置：没有 output.audio.proxy 整段\n"
    "ui:\n"
    "  lang: zh\n"
    "session:\n"
    "  api_key: test-key-not-real\n"
    "output:\n"
    "  capture:\n"
    "    mic_device: ''\n"
    "  audio:\n"
    "    enabled: false           # 总开关\n"
    "    device: ['FakeCard Output']\n"
    "    sample_rate: 48000\n"
    "    buffer_ms: 300           # 起播前先攒这么多\n"
    "    max_buffer_ms: 2000\n"
)

#: 兜底标记文件名（与 config.yaml 同目录）；见 vlt/gui_proxy_hint.py。
FALLBACK_NAME = ".proxy-hint-shown"


class _Widget:
    def __init__(self) -> None:
        self.kw: dict = {}

    def configure(self, **kw) -> None:
        self.kw.update(kw)

    def cget(self, k):
        return self.kw.get(k)


class _FakeProxy:
    """假代理：只满足 `_start_proxy` / `_refresh_voice_mode_btn` 用到的那几个面。"""

    def __init__(self) -> None:
        self.mode = "passthrough"
        self.closed = False
        self.started = False

    def start(self) -> bool:
        self.started = True
        return True

    def close(self) -> None:
        self.closed = True


def _read_proxy_cfg(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ((doc.get("output") or {}).get("audio") or {}).get("proxy") or {}


@contextlib.contextmanager
def _harness(*, enabled: bool = True, headless: bool = True, hint_shown: bool | None = None,
             legacy: bool = False, cfg_dir: Path | None = None, write_config: bool = True):
    """沙箱配置 + headless GUI + 假代理 + 假 opener。yield (gui, cfg_path, opened)。

    - ``legacy=True``：写一份**老用户形态**的 config.yaml（没有 `output.audio.proxy` 整段）；
    - ``cfg_dir``/``write_config=False``：**复用**上一次留下的配置目录、不再改写它 ——
      用来逐次模拟「重启程序」（每次都是新 GUI、配置从磁盘重读），
      这样才能测出「标记到底有没有落盘」而不是只测内存快照。
    """
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod
    import vlt.gui_proxy as gui_proxy_mod
    from vlt import gui_proxy_hint

    tmp = Path(cfg_dir) if cfg_dir is not None else Path(tempfile.mkdtemp(prefix="vlt-proxy-hint-"))
    cfg_path = tmp / "config.yaml"
    if write_config:
        if legacy:
            cfg_path.write_text(LEGACY_CONFIG_TEXT, encoding="utf-8", newline="\n")
        else:
            text = CONFIG_TEXT.format(enabled="true" if enabled else "false")
            if hint_shown is not None:
                text = text.replace("      passthrough_buffer_ms: 150\n",
                                    f"      passthrough_buffer_ms: 150\n      hint_shown: {str(hint_shown).lower()}\n")
            cfg_path.write_text(text, encoding="utf-8", newline="\n")

    saved_env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    os.environ.pop("DASHSCOPE_API_KEY", None)

    saved = (cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod._is_test_process,
             gui_proxy_mod.platform.create_mic_proxy, gui_proxy_hint._open_window)
    cfg_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui_mod._is_test_process = lambda: False                 # 只在本上下文里放开（假代理，不开流）
    # ⚠️ 代理实现已搬到 `vlt.gui_proxy`（#84 的拆分）：`vlt.gui` 上**没有** platform 属性，
    #    打桩点必须落在真正用到它的那个模块上 —— 打在 gui 上会 AttributeError（实测）。
    gui_proxy_mod.platform.create_mic_proxy = lambda *a, **k: _FakeProxy()

    opened: list = []
    gui_proxy_hint._open_window = lambda g: opened.append(g)

    from vlt.gui import TranslationGUI

    gui = None
    try:
        # ⚠️ 构造一律用 headless=True（CI 上不开真 Tk 窗、也不让构造函数自己去起代理），
        #    然后**只翻 `_headless` 标志**来模拟「正式运行时」这条路 —— 那条路的判据就在标志上。
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            gui = TranslationGUI(headless=True)
        gui._headless = headless
        gui._voice_ctx.voice_mode_btn = _Widget()
        gui._proxy_check = _Widget()
        gui._passthrough_spin = _Widget()
        gui._translated_spin = _Widget()
        gui._proxy_hint = _Widget()
        gui._cfg.session_base["api_key"] = "test-key-not-real"
        yield gui, cfg_path, opened
    finally:
        cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod._is_test_process, \
            gui_proxy_mod.platform.create_mic_proxy, gui_proxy_hint._open_window = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from vlt import i18n
        i18n.set_language("zh")


def _start(gui) -> tuple[bool, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = gui._start_proxy()
    return ok, buf.getvalue()


def test_headless_never_shows_and_never_writes_flag() -> None:
    """① headless（跑用例 / --self-test / 打包自检）：不弹、**也不落盘标记**。"""
    with _harness(headless=True) as (gui, cfg_path, opened):
        ok, out = _start(gui)
        assert ok is True, f"假代理应能起来：{out!r}"
        assert opened == [], "headless 下不许弹窗"
        cfg = _read_proxy_cfg(cfg_path)
        assert "hint_shown" not in cfg, \
            f"headless 不许写「已提示」标记（会把用户配置写坏、用户再也看不到说明）：{cfg!r}"
    print("  ✓ ① headless：不弹窗、不写标记")


def test_first_start_shows_once_then_persists() -> None:
    """② 首次真正启用 → 弹一次 + 落盘；再次启动代理不再弹。"""
    with _harness(headless=False) as (gui, cfg_path, opened):
        ok, out = _start(gui)
        assert ok is True, out
        assert len(opened) == 1, f"首次启用应弹一次，实际 {len(opened)} 次：{out!r}"
        assert "[proxy] 首次启用说明已弹出" in out, f"必须留痕（禁静默）：{out!r}"
        cfg = _read_proxy_cfg(cfg_path)
        assert cfg.get("hint_shown") is True, f"标记必须落盘：{cfg!r}"

        # 关掉代理再开一次（同一进程里模拟「下次再启用」）
        with contextlib.redirect_stdout(io.StringIO()):
            gui._close_proxy()
        ok2, out2 = _start(gui)
        assert ok2 is True, out2
        assert len(opened) == 1, f"已提示过就不该再弹：{len(opened)} 次"
        assert "[proxy] 首次启用说明已弹出" not in out2, "已提示过不该再留这行痕"
    print("  ✓ ② 首次启用弹一次 + 落盘；之后不再弹")


def test_second_process_reads_flag_and_stays_quiet() -> None:
    """③ 配置里已有标记（= 上次弹过）→ 新进程启动代理时不弹。"""
    with _harness(headless=False, hint_shown=True) as (gui, _cfg_path, opened):
        ok, out = _start(gui)
        assert ok is True, out
        assert opened == [], f"配置里已标记提示过，不许再弹：{out!r}"
    print("  ✓ ③ 配置已有标记：不弹")


def test_disabled_proxy_does_not_hint() -> None:
    """④ 代理在配置里关着 → 既不弹也不写标记（提示只属于「真的启用了」这件事）。"""
    with _harness(headless=False, enabled=False) as (gui, cfg_path, opened):
        ok, _out = _start(gui)
        assert ok is False, "关掉时代理不该起来"
        assert opened == [], "没启用就不该弹说明"
        assert "hint_shown" not in _read_proxy_cfg(cfg_path), "没启用就不该写标记"
    print("  ✓ ④ 代理关着：不弹、不写标记")


def test_close_is_idempotent_and_wired_into_exit() -> None:
    """⑤ 弹窗关闭幂等，且 `_on_close` 真的会把它收掉。"""
    from vlt import gui_proxy_hint

    with _harness(headless=False) as (gui, _cfg_path, _opened):
        class _Win:
            def __init__(self) -> None:
                self.destroyed = 0

            def destroy(self) -> None:
                self.destroyed += 1

        win = _Win()
        gui._proxy_hint_win = win
        gui._close_proxy_hint()                      # 走 delegate → gui_proxy_hint.close
        assert win.destroyed == 1 and gui._proxy_hint_win is None, "关窗应销毁并摘引用"
        gui_proxy_hint.close(gui)                    # 再关一次：幂等，不许抛
        assert win.destroyed == 1, "第二次 close 应是无操作"
    print("  ✓ ⑤ 关窗幂等 + 退出时收窗")


def _n_hashes(text: str) -> int:
    """含 `#` 的行数（本文件用老配置形态做回归，顺便盯住注释不被吃）。"""
    return sum(1 for ln in text.splitlines() if "#" in ln)


def test_legacy_config_without_proxy_section_marks_once() -> None:
    """⑥ **用户报的那个缺陷**：老 config.yaml（整段 `output.audio.proxy` 都没有）→ 标记要真的落盘。

    判据三条：① 首次启用弹一次；② 磁盘上出现 `output.audio.proxy.hint_shown: true`
    （不是只在内存里）；③ 老配置原有的键与注释一个字不少。
    然后**逐次模拟重启**（新 GUI、配置从磁盘重读）两次 —— 一次都不许再弹。
    """
    tmp = None
    with _harness(headless=False, legacy=True) as (gui, cfg_path, opened):
        before = cfg_path.read_text(encoding="utf-8")
        ok, out = _start(gui)
        assert ok is True, out
        assert len(opened) == 1, f"首次启用应弹一次，实际 {len(opened)} 次：{out!r}"
        assert "[proxy] 首次启用说明已弹出" in out, f"必须留痕（禁静默）：{out!r}"

        text = cfg_path.read_text(encoding="utf-8")
        assert _read_proxy_cfg(cfg_path).get("hint_shown") is True, (
            "标记必须真的落盘 —— 老配置缺整段时旧写法是静默 no-op，于是每次启动都弹："
            f"{text!r}")
        assert "hint_shown" not in before, "前提：老配置里本来没有这个标记"
        assert _n_hashes(text) == _n_hashes(before), "就地写入把注释吃掉了"
        doc = yaml.safe_load(text)
        audio = (doc.get("output") or {}).get("audio") or {}
        assert audio.get("buffer_ms") == 300 and audio.get("max_buffer_ms") == 2000, \
            f"老配置里已有的键被动了：{audio!r}"
        assert "[proxy] ⚠️" not in out, f"落盘成功就不该有 WARN：{out!r}"
        tmp = cfg_path.parent

    assert tmp is not None
    for nth in (2, 3):
        with _harness(headless=False, cfg_dir=tmp, write_config=False) as (gui2, _c2, opened2):
            ok2, out2 = _start(gui2)
            assert ok2 is True, out2
            assert opened2 == [], f"第 {nth} 次启动不该再弹：{out2!r}"
    print("  ✓ ⑥ 老配置（无 proxy 整段）：标记真的落盘 + 之后两次启动都不弹（用户报的缺陷）")


def test_unwritable_config_falls_back_loudly_and_never_repeats() -> None:
    """⑦ 故障注入：config.yaml 写不进去（只读 / 被占用 / 损坏）→ 响亮留痕 + 兜底标记，且不再无限弹。

    口径（本仓库「禁静默降级」）：写不进去**必须**有一行 `[proxy]` WARN 说清「没写进 config.yaml」；
    同时「已提示过」这件事不能一点痕迹都留不下 —— 否则用户每次启动都被同一条说明再打扰一次，
    而它按设计只该弹一次（用户原话「以后永远都不会弹」）。所以落一个同目录的兜底标记文件，
    下次启动读到它就安静，并且每次都会如实说明标记来自哪里。
    """
    tmp = None
    with _harness(headless=False, legacy=True) as (gui, cfg_path, opened):
        import vlt.gui_proxy as gui_proxy_mod

        def _boom(*_a, **_k):
            raise PermissionError("模拟：config.yaml 只读 / 被占用")

        real = gui_proxy_mod._write_config_text
        gui_proxy_mod._write_config_text = _boom          # type: ignore[assignment]
        try:
            ok, out = _start(gui)
        finally:
            gui_proxy_mod._write_config_text = real       # type: ignore[assignment]

        assert ok is True, out
        assert len(opened) == 1, f"写不进标记也不许影响这次弹窗：{out!r}"
        assert "[proxy] ⚠️" in out and "没能写进 config.yaml" in out, \
            f"写不进去必须响亮留痕（禁静默降级）：{out!r}"
        assert "hint_shown" not in cfg_path.read_text(encoding="utf-8"), "写不进去就别动文件"
        assert (cfg_path.parent / FALLBACK_NAME).exists(), \
            f"必须有兜底标记，否则下次启动还会再弹：{list(cfg_path.parent.iterdir())!r}"
        assert "已改用兜底标记" in out, f"用了兜底标记要留痕：{out!r}"
        tmp = cfg_path.parent

    assert tmp is not None
    with _harness(headless=False, cfg_dir=tmp, write_config=False) as (gui2, _c2, opened2):
        ok2, out2 = _start(gui2)
        assert ok2 is True, out2
        assert opened2 == [], f"兜底标记在 → 不该再弹：{out2!r}"
        assert "标记来自兜底文件" in out2, f"用了兜底标记就每次都要留痕：{out2!r}"
    print("  ✓ ⑦ config.yaml 写不进去：响亮 WARN + 兜底标记 → 下次启动不再弹（不静默、不无限弹）")


if __name__ == "__main__":
    test_headless_never_shows_and_never_writes_flag()
    test_first_start_shows_once_then_persists()
    test_second_process_reads_flag_and_stays_quiet()
    test_disabled_proxy_does_not_hint()
    test_close_is_idempotent_and_wired_into_exit()
    test_legacy_config_without_proxy_section_marks_once()
    test_unwritable_config_falls_back_loudly_and_never_repeats()
    print("\nALL PASSED")
