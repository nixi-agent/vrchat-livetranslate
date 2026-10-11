"""麦克风代理（Mic Proxy）配置层验收：解析/夹取 + 就地持久化往返 + GUI 控件态。

跑法：.venv/Scripts/python.exe tests/test_proxy_config.py

覆盖计划测试清单里「配置 + GUI」那几条：
  ① proxy 两个配置键（enabled / passthrough_buffer_ms）的解析、越界夹取、非法回落；
  ② 三个键（passthrough_buffer_ms / enabled / buffer_ms）经 config_io 就地写入的往返
     —— 值正确、注释一行不少、config.example.yaml 里那条**深层缩进注释**（下限 60ms）
     不被顺手删掉、文件仍是合法 YAML；
  ③ 设置页控件态：关代理 → 直通缓冲 Spinbox 置灰（译音缓冲始终可编辑）；
     主界面「原声/译音」按钮的置灰逻辑（翻译停 → 弹回原声档）；
     buffer_ms / passthrough 经 Spinbox 写回的持久化往返（含越界夹取）。

全程离线：配置解析用临时文件（require_key=False）；GUI 用**临时 config.yaml** 起真窗口，
且 `_is_test_process()` 守卫保证绝不真开麦克风代理（不碰用户音频图）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 小工具


def _n_comments(t: str) -> int:
    return sum(1 for ln in t.splitlines() if ln.strip().startswith("#"))


def _isolate_env(tmp: Path) -> dict:
    """把 HOME/USERPROFILE 指到临时目录并摘掉环境变量 key：隔绝本机可能存在的 key 来源。"""
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _load_capturing(text: str):
    """把一段 YAML 落成临时 config.yaml 并 load_config（require_key=False）。

    返回 (cfg, 捕获的 stdout)：配置解析里的**留痕**（夹取/非法回落）都打到 stdout，
    捕回来既保住测试输出干净，又能断言「不静默」。
    """
    tmp = Path(tempfile.mkdtemp(prefix="vlt-proxy-cfg-"))
    p = tmp / "config.yaml"
    p.write_text(text, encoding="utf-8")
    from vlt.config import load_config
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg = load_config(p, require_key=False)
    return cfg, buf.getvalue()


def _make_gui(config_path: Path):
    """用临时配置起真窗口；取消启动的自动更新检查调度（绝不真连 GitHub）。"""
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod

    cfg_mod.DEFAULT_CONFIG = config_path
    gui_mod.DEFAULT_CONFIG = config_path

    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    if gui._update_check_job is not None:
        gui._root.after_cancel(gui._update_check_job)
        gui._update_check_job = None
    gui._root.update()
    return gui


def _destroy(gui) -> None:
    if gui is None:
        return
    try:
        from vlt import gui_chat
        gui_chat.cancel_poll(gui._chat_ctx, gui._root)
    except Exception:  # noqa: BLE001
        pass
    try:
        gui._root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _temp_gui_config() -> Path:
    """带 output.audio.proxy 段的最小临时配置（写回时走**就地替换**，最贴近真实 config.yaml）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vlt-proxy-gui-"))
    p = tmp / "config.yaml"
    p.write_text(
        "ui:\n"
        "  lang: zh\n"
        "output:\n"
        "  audio:\n"
        "    enabled: false\n"
        "    buffer_ms: 300\n"
        "    proxy:\n"
        "      enabled: true\n"
        "      passthrough_buffer_ms: 150\n",
        encoding="utf-8")
    return p


class _FakeEngine:
    def __init__(self, running: bool) -> None:
        self.running = running


class _FakeProxy:
    """只喂 `_refresh_voice_mode_btn` 需要的那一个属性（mode）；不真开任何流。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode


class _StubWidget:
    """占位控件（headless 下没有真 Tk 控件）：只记 configure 的关键字。"""

    def __init__(self) -> None:
        self.kw: dict = {}

    def configure(self, **kw) -> None:
        self.kw.update(kw)

    def cget(self, k):                       # noqa: ANN001
        return self.kw.get(k)


class _StubVar:
    """占位 Tk 变量：只提供 get/set。"""

    def __init__(self, v) -> None:           # noqa: ANN001
        self._v = v

    def get(self):
        return self._v

    def set(self, v) -> None:                # noqa: ANN001
        self._v = v


#: **老用户形态**的 config.yaml（更早的模板生成）：`output.audio` 在，但整段 `proxy:` 没有。
#: 界面上的代理设置在这个形态下曾经是**静默不落盘**的（`_yaml_set_in_text` 缺父键 → 原样返回）。
LEGACY_GUI_CONFIG = (
    "ui:\n"
    "  lang: zh\n"
    "session:\n"
    "  api_key: test-key-not-real\n"
    "output:\n"
    "  audio:\n"
    "    enabled: false           # 总开关\n"
    "    buffer_ms: 300           # 起播前先攒这么多\n"
    "    max_buffer_ms: 2000\n"
)


# ---------------------------------------------------------------- ① 解析 / 夹取


def test_proxy_config_parsing_and_clamping() -> None:
    """proxy 两键的解析：默认、显式、越界夹取（留痕）、非法回落（留痕）、非 dict 兜底。"""

    def _proxy_of(text: str):
        cfg, log = _load_capturing(text)
        audio = (cfg.output or {}).get("audio") or {}
        return (audio.get("proxy") or {}), audio.get("buffer_ms"), log

    # ① 无 proxy 段 → 默认（启用 + 直通缓冲 150ms）；buffer_ms 默认 300
    proxy, buf, _ = _proxy_of("output:\n  audio:\n    enabled: true\n")
    assert proxy.get("enabled") is True, f"默认应启用代理：{proxy!r}"
    assert proxy.get("passthrough_buffer_ms") == 150, f"直通缓冲默认应 150：{proxy!r}"
    assert buf == 300, f"译音缓冲默认应 300：{buf!r}"

    # ② 显式有效值原样读出
    proxy, buf, _ = _proxy_of(
        "output:\n  audio:\n    buffer_ms: 450\n"
        "    proxy:\n      enabled: false\n      passthrough_buffer_ms: 200\n")
    assert proxy.get("enabled") is False, proxy
    assert proxy.get("passthrough_buffer_ms") == 200, proxy
    assert buf == 450, buf

    # ③ 越界夹取（下限 60 / 上限 500）且**留痕**（不静默带病运行）
    proxy, _, log = _proxy_of(
        "output:\n  audio:\n    proxy:\n      passthrough_buffer_ms: 10\n")
    assert proxy.get("passthrough_buffer_ms") == 60, f"低于下限应夹到 60：{proxy!r}"
    assert "超出合理范围" in log, "夹取必须留痕"
    proxy, _, log = _proxy_of(
        "output:\n  audio:\n    proxy:\n      passthrough_buffer_ms: 9999\n")
    assert proxy.get("passthrough_buffer_ms") == 500, f"高于上限应夹到 500：{proxy!r}"
    assert "超出合理范围" in log, "夹取必须留痕"

    # ④ 非法值（非整数）→ 留痕 + 回落默认 150
    proxy, _, log = _proxy_of(
        "output:\n  audio:\n    proxy:\n      passthrough_buffer_ms: abc\n")
    assert proxy.get("passthrough_buffer_ms") == 150, f"非法值应回落 150：{proxy!r}"
    assert "不是整数" in log, "非法值必须留痕"

    # ⑤ proxy 段被写成非 dict（如一行标量）→ 不炸，回落默认
    proxy, _, _ = _proxy_of("output:\n  audio:\n    proxy: yes\n")
    assert proxy.get("enabled") is True and proxy.get("passthrough_buffer_ms") == 150, \
        f"proxy 非 dict 时应回落默认：{proxy!r}"

    print("  ✓ proxy 配置解析：默认 / 显式 / 越界夹取(60~500,留痕) / "
          "非法回落(150,留痕) / 非 dict 兜底")


# ---------------------------------------------------------------- ② 就地持久化往返


def test_proxy_keys_inplace_persistence() -> None:
    """就地写入 proxy 三键：值往返正确、注释一行不少、深层缩进注释保住、仍是合法 YAML。

    ★ 守住 `config.example.yaml` 里 `passthrough_buffer_ms` 下方那条**深层缩进注释**
      （「⚠️ 下限 60ms…」，缩进比所属键深得多）—— 与 test_config_save 的
      `test_deep_indented_comments_survive_leaf_writes` 同款回归点：就地写入不许把它当
      子块顺手删掉。
    """
    from vlt.config_io import _yaml_set_in_text, _yaml_set_or_create

    template = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    base = _n_comments(template)
    assert "下限 60ms" in template, "前提：模板里应有那条深层缩进注释"

    # 与界面同一条调用路径：proxy 子键走 create=True，buffer_ms 走就地替换
    text = _yaml_set_or_create(
        template, ["output", "audio", "proxy", "passthrough_buffer_ms"], "240")
    text = _yaml_set_or_create(text, ["output", "audio", "proxy", "enabled"], "false")
    text = _yaml_set_in_text(text, ["output", "audio", "buffer_ms"], "600")

    assert _n_comments(text) == base, f"注释被破坏：{base} → {_n_comments(text)}"
    assert "下限 60ms" in text, "深层缩进注释（下限 60ms）被就地写入吃掉了"
    data = yaml.safe_load(text)                 # 删的时候别把 YAML 弄坏
    assert isinstance(data, dict), "写入后不再是合法 YAML"
    audio = (data.get("output") or {}).get("audio") or {}
    proxy = audio.get("proxy") or {}
    assert proxy.get("passthrough_buffer_ms") == 240, f"直通缓冲没写对：{proxy!r}"
    assert proxy.get("enabled") is False, f"proxy 开关没写对：{proxy!r}"
    assert audio.get("buffer_ms") == 600, f"译音缓冲没写对：{audio!r}"
    # 兄弟键原封不动（不该被顺手改/删）
    assert audio.get("max_buffer_ms") == 2000, f"兄弟键 max_buffer_ms 被动了：{audio!r}"
    assert audio.get("sample_rate") == 48000, f"兄弟键 sample_rate 被动了：{audio!r}"
    print(f"  ✓ proxy 三键就地持久化往返 OK（值正确 / 注释 {base} 行不少 / "
          f"深层注释保住 / 合法 YAML）")


# ---------------------------------------------------------------- ③ GUI 控件态 + 写回


def test_gui_proxy_controls_state_and_writeback() -> None:
    """设置页控件态 + 主界面切换按钮置灰逻辑 + 缓冲经 Spinbox 写回的持久化往返。"""
    import tkinter as tk

    from vlt import i18n
    from vlt.i18n import t
    from vlt.output.micproxy import MODE_PASSTHROUGH, MODE_TRANSLATED

    saved_env = _isolate_env(Path(tempfile.mkdtemp(prefix="vlt-proxy-env-")))
    cfg_path = _temp_gui_config()
    gui = None
    try:
        gui = _make_gui(cfg_path)

        # ⓪ 初始态：**建完就断言**，不许先调一次刷新函数（否则只验了函数、漏掉出厂态）。
        #    `_proxy` 在本仓库恒为 None（Linux 永远无代理；Windows 的麦克风代理尚未接线），
        #    说明 `refresh_voice_mode_btn` 的判定是「无 proxy → 禁用」——按钮出厂就该是灰的。
        #    少了这条，Linux 上会出现一颗看着能点、点了只 `return` 的死按钮（本机实测）。
        assert str(gui._voice_mode_btn.cget("state")) == "disabled", \
            f"初始态就该置灰（无 proxy），实际 {gui._voice_mode_btn.cget('state')!r}"

        if gui._passthrough_spin is None:
            print("  [skip] 非 Windows：麦克风代理设置控件未建，跳过后续 GUI 态用例"
                  "（按钮初始态已在上方断言）")
            return

        # ① 初始（proxy 默认启用）：直通/译音缓冲都可编辑；测试进程守卫生效 → 不真开代理
        assert gui._proxy is None, "测试进程里不该真开代理（_is_test_process 守卫）"
        assert str(gui._passthrough_spin.cget("state")) == "normal", "启用时直通缓冲应可编辑"
        assert str(gui._translated_spin.cget("state")) == "normal", "译音缓冲应始终可编辑"

        # ② 关闭 proxy 勾选 → 直通缓冲置灰；译音缓冲仍可编辑；提示切「已关闭」
        gui._proxy_enabled_var.set(False)
        gui._sync_proxy_controls_state()
        assert str(gui._passthrough_spin.cget("state")) == "disabled", "关代理后直通缓冲应置灰"
        assert str(gui._translated_spin.cget("state")) == "normal", "关代理后译音缓冲仍可编辑"
        assert gui._proxy_hint.cget("text") == t(
            "已关闭：回到旧行为（译音输出随翻译启停，主界面切换开关置灰）"), \
            f"关代理提示文案不对：{gui._proxy_hint.cget('text')!r}"

        # ③ 重新启用 → 直通缓冲恢复可编辑（但没虚拟声卡 → proxy 仍 None）
        gui._proxy_enabled_var.set(True)
        gui._sync_proxy_controls_state()
        assert str(gui._passthrough_spin.cget("state")) == "normal", "重新启用后直通缓冲应恢复"

        # ④ 主界面切换按钮：无 proxy + 无翻译 → 禁用 + 「🎙 原声」
        gui._refresh_voice_mode_btn()
        assert str(gui._voice_mode_btn.cget("state")) == "disabled", "无代理时切换按钮应禁用"
        assert gui._voice_mode_btn.cget("text") == t("🎙 原声"), "无代理时应显示原声档"

        # ⑤ 注入假 proxy + 假引擎（running）→ 解禁；译音模式 → 文案「🗣 译音」
        fake_eng = _FakeEngine(running=True)
        gui._proxy = _FakeProxy(mode=MODE_TRANSLATED)
        gui._engines = [fake_eng]
        gui._refresh_voice_mode_btn()
        assert str(gui._voice_mode_btn.cget("state")) == tk.NORMAL, "翻译在跑+代理可用时应解禁"
        assert gui._voice_mode_btn.cget("text") == t("🗣 译音"), "译音模式应显示译音档"

        # ⑥ 翻译停（running=False）+ 代理回落原声 → 置灰 + 弹回「🎙 原声」
        #    （真实路径里 set_translation_active(False) 会强制把 mode 回落 passthrough）
        fake_eng.running = False
        gui._proxy.mode = MODE_PASSTHROUGH
        gui._refresh_voice_mode_btn()
        assert str(gui._voice_mode_btn.cget("state")) == "disabled", "翻译停后应置灰"
        assert gui._voice_mode_btn.cget("text") == t("🎙 原声"), "翻译停后应弹回原声档"
        gui._proxy = None
        gui._engines = []

        # ⑦ buffer_ms / passthrough 经 Spinbox 写回：持久化往返（就地改，不破坏结构）
        gui._passthrough_var.set(240)
        gui._translated_buf_var.set(600)
        gui._on_proxy_buffer_change()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        audio = (data.get("output") or {}).get("audio") or {}
        assert (audio.get("proxy") or {}).get("passthrough_buffer_ms") == 240, \
            f"直通缓冲没写回：{audio!r}"
        assert audio.get("buffer_ms") == 600, f"译音缓冲没写回：{audio!r}"

        # ⑧ 越界：Spinbox 手输超范围 → _read_spin 夹回 [60,500]/[50,2000]，并写回夹取值
        gui._passthrough_var.set(9999)
        gui._translated_buf_var.set(1)
        gui._on_proxy_buffer_change()
        assert gui._passthrough_var.get() == 500, f"直通缓冲应夹到 500：{gui._passthrough_var.get()}"
        assert gui._translated_buf_var.get() == 50, f"译音缓冲应夹到 50：{gui._translated_buf_var.get()}"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        audio = (data.get("output") or {}).get("audio") or {}
        assert (audio.get("proxy") or {}).get("passthrough_buffer_ms") == 500, audio
        assert audio.get("buffer_ms") == 50, audio

        # ⑨ ★ 解锁时序：引擎是往 `ctx.engines` 追加的，界面按钮读的却是自己的
        #    `_engines` —— 只有 `_unsync_engine_ctx()`（ctx→界面）之后才一致。
        #    实测事故：刷新放在同步**之前**，按钮被判成「没有引擎在跑」而一直置灰，
        #    用户根本切不到译音档（现象是「只能听原声、听不到译音」）。
        gui._proxy = _FakeProxy(mode=MODE_PASSTHROUGH)
        gui._engines = []
        gui._voice_ctx.proxy = None
        gui._engine_ctx.engines = [_FakeEngine(running=True)]
        gui._engine_ctx.engine_dirs = ["mine"]

        # ⑨-1 未同步就刷 = 旧行为 → 仍置灰（复现事故）
        gui._refresh_voice_mode_btn()
        assert str(gui._voice_mode_btn.cget("state")) == "disabled", \
            "未同步就刷时应当还是置灰（否则这条用例守不住那个 bug）"

        # ⑨-2 先同步再刷 = `_start()` 里的正确顺序 → 解禁
        gui._unsync_engine_ctx()
        gui._refresh_voice_mode_btn()
        assert str(gui._voice_mode_btn.cget("state")) == "normal", \
            "同步之后再刷应当解禁（翻译在跑 + 有代理）"
        assert gui._voice_ctx.proxy is gui._proxy, "刷完 VoiceCtx 该拿到代理"
        gui._engine_ctx.engines = []
        gui._unsync_engine_ctx()
        gui._proxy = None

        print("  ✓ GUI 代理控件态（关代理置灰直通缓冲）+ 切换按钮置灰（翻译停弹回原声）"
              " + 缓冲写回持久化往返（含越界夹取）全对")
        print("  ✓ 切换按钮解锁时序：必须「先 ctx→界面同步、再刷新」（复现了旧 bug 并守住）")
    finally:
        _destroy(gui)
        _restore_env(saved_env)
        i18n.set_language("zh")


def test_legacy_config_proxy_keys_really_persist() -> None:
    """**老配置（没有 `output.audio.proxy` 整段）**：设置页的开关与两条缓冲也必须真落盘。

    与 `test_gui_proxy_controls_state_and_writeback` 是一对：那份用**带 proxy 段**的配置
    （就地替换路径），这份用老配置（**需要补建整段**的路径）。旧写法 `_yaml_set_in_text`
    在这种配置上是**静默 no-op**（缺父键 → 原样返回，不抛不报）——
    用户现象就是「代理关掉、下次启动又开了」「直通缓冲改了没记住」。
    顺带把「落盘成功不许出现 [proxy] WARN」钉住（WARN 只留给真的写不进去）。
    """
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod

    tmp = Path(tempfile.mkdtemp(prefix="vlt-proxy-legacy-"))
    home = tmp / "home"
    home.mkdir()
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(LEGACY_GUI_CONFIG, encoding="utf-8", newline="\n")

    saved_env = _isolate_env(home)
    saved = (cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod._is_test_process)
    cfg_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui_mod._is_test_process = lambda: False                 # type: ignore[assignment]
    gui = None
    try:
        from vlt.gui import TranslationGUI

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            gui = TranslationGUI(headless=True)              # 不开真窗口、更不开音频设备
        gui._voice_ctx.voice_mode_btn = _StubWidget()        # 主界面「原声/译音」按钮占位
        gui._proxy_check = _StubWidget()
        gui._proxy_hint = _StubWidget()
        gui._passthrough_spin = _StubWidget()                # 不能是 None：否则写回路径整条短路
        gui._translated_spin = _StubWidget()
        gui._proxy_enabled_var = _StubVar(False)
        gui._passthrough_var = _StubVar(240)
        gui._translated_buf_var = _StubVar(600)
        gui._cfg.session_base["api_key"] = "test-key-not-real"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._on_proxy_toggle()                           # 用户：关掉麦克风代理
            gui._on_proxy_buffer_change()                    # 用户：直通 240 / 译音 600
        out = buf.getvalue()

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        audio = (data.get("output") or {}).get("audio") or {}
        proxy = audio.get("proxy") or {}
        assert proxy.get("enabled") is False, f"代理开关没落盘（老配置缺整段）：{audio!r}"
        assert proxy.get("passthrough_buffer_ms") == 240, f"直通缓冲没落盘：{audio!r}"
        assert audio.get("buffer_ms") == 600, f"译音缓冲没落盘：{audio!r}"
        assert audio.get("enabled") is False and audio.get("max_buffer_ms") == 2000, \
            f"老配置里已有的兄弟键被动了：{audio!r}"
        assert "# 总开关" in cfg_path.read_text(encoding="utf-8"), "注释被吃掉了"
        assert "[proxy] ⚠️" not in out, f"落盘成功就不该有 WARN（禁静默指的是失败路径）：{out!r}"
        assert gui._proxy_wanted() is False, "内存快照没跟上落盘"
        print("  ✓ 老配置（无 proxy 整段）：代理开关 + 直通/译音缓冲三键都真的落盘"
              "（补建 proxy: 段，原键与注释不动）")
    finally:
        cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, \
            gui_mod._is_test_process = saved
        _restore_env(saved_env)
        from vlt import i18n
        i18n.set_language("zh")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_proxy_config_parsing_and_clamping,
        test_proxy_keys_inplace_persistence,
        test_gui_proxy_controls_state_and_writeback,
        test_legacy_config_proxy_keys_really_persist,
    ]
    print("test_proxy_config:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
