"""常駐麥克風代理的 GUI 行為；與翻譯供應商無關。"""
from __future__ import annotations
import os
import sys
import tkinter as tk
from pathlib import Path
from . import platform
from .i18n import t
from .config_io import (_fmt_scalar, _yaml_set_in_text, _yaml_set_or_create,
                        _yaml_scalar)
from .output import level_match

def _m(gui):
    return sys.modules[gui.__class__.__module__]

_TEST_ENTRIES = ("run_tests.py", "pytest", "pytest.exe", "py.test")

#: 设置窗里那块实时读数的刷新间隔。500ms 够看（电平是句级的慢变量），
#: 又不至于让「读引擎 status()」变成每帧开销。
_LEVEL_TICK_MS = 500

#: 界面上能拨的 dB 范围（比 `level_match.RANGES` 的**校验**范围更窄：校验要容得下
#: 手写 config.yaml 的人，界面只给「调了有意义」的那一段）。
_LEVEL_UI_FIXED_DB = (-24.0, 6.0)
_LEVEL_UI_OFFSET_DB = (-12.0, 12.0)


def _fmt_level(v) -> str:  # noqa: ANN001, ANN202
    """读数格式：没有测量值就是 `—`（绝不拿 0.0 冒充「测到了 0 dBFS」）。"""
    return "—" if v is None else f"{float(v):+.1f}"

def _is_test_process() -> bool:
    """当前进程是不是**测试/自检**进程 —— 这类进程绝不构造麦克风代理。

    代理会真开虚拟声卡输出流与麦克风（Windows 走 PortAudio；Linux 走 PipeWire 节点/子进程）。
    跑用例的纪律是「绝不碰真实音频设备」（见 tests/test_micproxy.py 顶部的打桩说明），
    所以在 GUI 构造阶段先问一句。
    判据只看**进程入口 / 已导入的测试框架 / 环境变量**，不靠「有没有显示器」这类
    间接信号 —— 间接信号在 CI 的 xvfb 下会误判成「真用户」。
    """
    if os.environ.get("PYTEST_CURRENT_TEST"): return True
    if "pytest" in sys.modules or "_pytest" in sys.modules: return True
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    entry = Path(argv0).name
    return (entry.startswith("test_") or entry in _TEST_ENTRIES
            or "unittest" in argv0 or "pytest" in argv0)


class ProxyMethods:
    def _proxy_audio_cfg(self) -> dict:
        """取 ``output.audio`` 这一段（含 proxy 子段）的**副本**；配置畸形时回落空 dict。"""
        out = self._cfg.output if isinstance(self._cfg.output, dict) else {}
        a = out.get("audio")
        return dict(a) if isinstance(a, dict) else {}


    def _proxy_wanted(self) -> bool:
        """配置里是不是想要代理（缺省即启用，见 config.py 的 proxy 归一化）。"""
        p = self._proxy_audio_cfg().get("proxy") or {}
        return bool(p.get("enabled", True)) if isinstance(p, dict) else True


    def _sync_proxy_cfg_mem(self, *, audio: dict | None = None,
                            proxy: dict | None = None) -> None:
        """落盘之后同步**内存快照**：代理随时可能重开，它读的是这份而不是磁盘。"""
        if not isinstance(self._cfg.output, dict):
            return
        a = self._cfg.output.get("audio")
        if not isinstance(a, dict):
            a = {}; self._cfg.output["audio"] = a
        if audio:
            a.update(audio)
        if proxy:
            p = a.get("proxy")
            if not isinstance(p, dict):
                p = {}; a["proxy"] = p
            p.update(proxy)


    def _on_proxy_status(self, level: str, msg: str, **params) -> None:
        """代理的 on_status：**日志打中文原文、状态栏走 i18n**（与 ``gui_engine.on_engine_status`` 同口径）。

        `msg` 是**中文模板**（同时就是 i18n 词条 key），带值的部分走 `**params`：

          · 日志：`msg.format(**params)` —— 全仓日志统一中文，中英混排只会更难读；
          · 状态栏：`t(msg, **params)` —— 用户看的地方必须能翻译（英文界面下不许闪中文）。

        micproxy 内部每条消息自带 ``"[proxy] "`` 前缀，这里剥掉再补一个带级别的，
        否则日志里会出现 ``[proxy][warn] [proxy] …`` 这种双重前缀。
        """
        body = msg[len("[proxy] "):] if msg.startswith("[proxy] ") else msg
        print(f"[proxy][{level}] {body.format(**params) if params else body}", flush=True)
        self._q.put(("status", level, t(body, **params) if params else t(body)))


    def _start_proxy(self) -> bool:
        """构造并启动麦克风代理。返回是否**真的起来了**（False = 走旧行为）。

        ⚠️ 绝不抛异常、绝不阻塞启动：任何一步失败都只降级 + 留痕（禁静默降级）。
        """
        if self._proxy is not None:
            return True
        if not self._proxy_wanted():
            print("[proxy] 配置 output.audio.proxy.enabled=false：不启动代理"
                  "（译音输出回到旧行为：随翻译启停、由引擎自建）", flush=True)
            return False
        if _m(self)._is_test_process():
            # 测试纪律：绝不碰真实音频设备（虚拟声卡输出流与麦克风：Windows 是 PortAudio，
            # Linux 是 PipeWire 节点/子进程 —— 都会真的开流/起节点）
            print("[proxy] 测试/自检进程：不构造麦克风代理（避免打开真实音频设备）", flush=True)
            return False
        a = self._proxy_audio_cfg()
        out = self._cfg.output if isinstance(self._cfg.output, dict) else {}
        mic_name = (out.get("capture") or {}).get("mic_device") or None
        proxy = None
        try:
            # ★ 平台差异**只在门面里**：Windows → `vlt/output/micproxy.py`（PortAudio 输出流，
            #   虚拟声卡是用户自备的 VoiceMeeter/VB-Cable）；Linux → `vlt/output/micproxy_linux.py`
            #   （`pw-cat` 管道 + 运行时声明的 `VLT Mic` 节点）。档位语义两端一致。
            proxy = platform.create_mic_proxy(a, mic_name, self._on_proxy_status)
            ok = bool(proxy.start())
        except Exception as exc:                       # noqa: BLE001
            print(f"[proxy] 启动异常：{type(exc).__name__}: {exc}（其余功能不受影响）", flush=True)
            ok = False
        if not ok:
            if proxy is not None:
                try:
                    proxy.close()                      # 半开的流/线程必须放掉
                except Exception:                      # noqa: BLE001
                    pass
            self._proxy_degraded(
                t("麦克风代理不可用（虚拟声卡没打开？）；原声/译音切换已禁用"),
                "[proxy] 代理不可用 → 回到旧行为：译音输出由引擎自建（随翻译启停），"
                "主界面「原声/译音」切换已禁用")
            return False
        self._proxy = proxy
        self._refresh_voice_mode_btn()
        pt = (a.get("proxy") or {}).get("passthrough_buffer_ms")
        print(f"[proxy] 麦克风代理已启动（VRChat 的麦克风请**永久**指向虚拟声卡）："
              f"直通缓冲 {pt}ms / 译音缓冲 {a.get('buffer_ms')}ms / 当前档位 {proxy.mode}",
              flush=True)
        # 首次真正跑起来：说明一次它占用了虚拟声卡的输出流（只提示一次，见 gui_proxy_hint）
        self._maybe_show_proxy_hint(on_shown=self._mark_proxy_hint_shown)
        return True

    def _mark_proxy_hint_shown(self) -> None:
        """把「首次启用说明已弹过」落盘 —— 以后不再打扰（headless 走不到这里，见 gui_proxy_hint）。

        ⚠️ 落盘与配置路径都走 ``_m(self)``：那套打桩（`gui_mod._yaml_write` / `gui_mod.DEFAULT_CONFIG`）
        在动了这个类的模块命名空间上才生效，本仓库的用例正是这么钉的。
        """
        _m(self)._yaml_write(
            _m(self).DEFAULT_CONFIG, lambda t: _yaml_set_in_text(
                t, ["output", "audio", "proxy", "hint_shown"], _fmt_scalar(True)),
            err="保存代理首次启用提示标记")
        self._sync_proxy_cfg_mem(proxy={"hint_shown": True})


    def _proxy_degraded(self, status_text: str, log_line: str) -> None:
        """代理不可用 → 回到旧行为，并**各留一行痕**（状态栏一行 + stdout 一行）。"""
        self._proxy = None
        self._refresh_voice_mode_btn()                 # 无代理 → 按钮置灰
        print(log_line, flush=True)
        self._set_status("warn", status_text)


    def _close_proxy(self) -> None:
        """幂等关闭代理：先摘引用再 close，绝不让异常打断关窗流程。"""
        p = self._proxy
        if p is None:
            return
        self._proxy = None
        try:
            p.close()                                  # 幂等：停麦克风线程 + 关输出流
            print("[proxy] 麦克风代理已关闭（虚拟声卡输出流与麦克风直通线程已释放）", flush=True)
        except Exception as exc:                       # noqa: BLE001
            print(f"[proxy] ⚠️ 关闭代理时出错（忽略）：{type(exc).__name__}: {exc}", flush=True)


    def _restart_proxy(self) -> None:
        """设置页勾选框的落地点：想开就开、想关就关，按钮态与提示一起刷新。"""
        if self._proxy_wanted():
            if self._start_proxy():
                self._set_status("info", t("麦克风代理已启用"))
            return
        running = any(getattr(e, "running", False) for e in self._engines)
        self._close_proxy()
        self._refresh_voice_mode_btn()
        if running:
            # 引擎手里那条 audio_sink 指向刚关掉的代理缓冲：本轮译音**没人接管**了。
            # 必须说清楚，别让人以为还在出声（禁静默降级）。
            print("[proxy] ⚠️ 翻译进行中关掉了代理：本轮译音输出已失效，"
                  "重新点「开始翻译」后由引擎自建虚拟声卡输出（旧行为）", flush=True)
        self._set_status("info", t("麦克风代理已关闭（回到旧行为）"))


    def _apply_proxy_buffers(self, passthrough_ms: int, translated_ms: int) -> None:
        """缓冲改动落地：代理在跑就即时生效，不在跑就只落盘 + 如实说明下次何时生效。"""
        p = self._proxy
        if p is not None:
            try:
                p.reopen_with(passthrough_ms=passthrough_ms,
                              translated_buffer_ms=translated_ms)
                print(f"[proxy] 缓冲已更新：直通 {passthrough_ms}ms / 译音 {translated_ms}ms"
                      "（软件侧参数，输出流不重开、没有静音间隙）", flush=True)
                self._set_status("info", t("缓冲已更新（即时生效）"))
            except Exception as exc:                   # noqa: BLE001
                print(f"[proxy] ⚠️ 应用缓冲失败：{type(exc).__name__}: {exc}"
                      "（已落盘，下次启动生效）", flush=True)
                self._set_status("warn", t("缓冲已保存（译音缓冲下次开始翻译生效）"))
            return
        if self._proxy_wanted():
            print(f"[proxy] 代理没在跑（虚拟声卡没打开？）：缓冲已落盘 —— "
                  f"直通 {passthrough_ms}ms / 译音 {translated_ms}ms，下次启动代理时生效",
                  flush=True)
            self._set_status("warn", t("虚拟声卡未打开——检查「译音输出」设备；缓冲改动已存，下次生效"))
        else:
            print(f"[proxy] 代理已关闭：缓冲已落盘（直通 {passthrough_ms}ms / "
                  f"译音 {translated_ms}ms），译音缓冲下次开始翻译生效", flush=True)
            self._set_status("info", t("缓冲已保存（译音缓冲下次开始翻译生效）"))


    def _on_proxy_toggle(self) -> None:
        self._sync_audio_ctx(); self._audio_ctx.proxy_enabled = self._proxy_enabled_var.get()
        _m(self)._yaml_write(_m(self).DEFAULT_CONFIG, lambda t: _yaml_set_in_text(t, ["output", "audio", "proxy", "enabled"], _fmt_scalar(self._audio_ctx.proxy_enabled)), err="保存代理开关")
        self._sync_proxy_cfg_mem(proxy={"enabled": bool(self._audio_ctx.proxy_enabled)})
        self._restart_proxy()
        self._sync_proxy_controls_state()


    def _on_proxy_buffer_change(self, _event=None) -> None:
        if self._passthrough_spin is None: return
        try:
            pt = int(self._passthrough_var.get()); tr = int(self._translated_buf_var.get())
            pt_clamped = max(60, min(500, pt)); tr_clamped = max(50, min(2000, tr))
            if pt != pt_clamped: self._passthrough_var.set(pt_clamped)
            if tr != tr_clamped: self._translated_buf_var.set(tr_clamped)
            pt, tr = pt_clamped, tr_clamped
        except (ValueError, TypeError): return
        self._sync_audio_ctx()
        def _fn(t):
            t = _yaml_set_in_text(t, ["output", "audio", "proxy", "passthrough_buffer_ms"], _fmt_scalar(pt))
            return _yaml_set_in_text(t, ["output", "audio", "buffer_ms"], _fmt_scalar(tr))
        _m(self)._yaml_write(_m(self).DEFAULT_CONFIG, _fn, err="保存代理缓冲")
        self._sync_proxy_cfg_mem(audio={"buffer_ms": tr}, proxy={"passthrough_buffer_ms": pt})
        self._apply_proxy_buffers(pt, tr)


    def _sync_proxy_controls_state(self) -> None:
        if self._proxy_check is None: return
        on = bool(self._proxy_enabled_var.get())
        if self._passthrough_spin is not None:
            try: self._passthrough_spin.configure(state=tk.NORMAL if on else tk.DISABLED)
            except Exception: pass
        if self._translated_spin is not None:
            try: self._translated_spin.configure(state=tk.NORMAL)
            except Exception: pass
        if hasattr(self, '_proxy_hint') and self._proxy_hint is not None:
            self._proxy_hint.configure(text="" if on else t("已关闭：回到旧行为（译音输出随翻译启停，主界面切换开关置灰）"))


    def _current_mic_device(self) -> str:
        out = self._cfg.output if isinstance(self._cfg.output, dict) else {}
        return str((out.get("capture") or {}).get("mic_device") or "")


    def _apply_mic_change(self, device_name: str) -> None:
        """麦克风变更后让**直通腿**即时生效：只重启代理的采集线程，不动虚拟声卡输出流
        （引擎手里的 `translated_sink` 不受影响，翻译不断）。代理没在跑就只落盘，下次
        `start()` 生效。翻译输入那条腿在引擎启动时读定设备，运行中切换不影响本轮 ——
        状态栏如实说明，别让人以为翻译输入也换了（禁静默降级）。"""
        p = self._proxy
        if p is None:
            return
        try:
            p.reopen_mic(device_name or None)
        except Exception as exc:                        # noqa: BLE001 — 绝不因切麦打断界面
            print(f"[proxy] ⚠️ 切换麦克风失败：{type(exc).__name__}: {exc}"
                  "（已落盘，下次启动生效）", flush=True)
            self._set_status("warn", t("麦克风已保存（切换未即时生效，下次启动生效）"))
            return
        print(f"[proxy] 直通麦克风已切换：{device_name or '自动检测'}"
              "（仅重启采集线程；翻译输入下轮生效）", flush=True)
        self._set_status("info", t("麦克风已切换（直通即时生效；翻译输入下轮生效）"))

    # ── 译音音量（固定增益 / 跟随麦克风）：控件 → 落盘 → 热更新 ──
    #    与「麦克风代理」同一条腿：参数住在 output.audio.level，真正施加增益的是
    #    引擎侧的 LevelMatcher，麦克风电平参考由代理的直通线程顺带喂（见 level_match.py）。

    def _level_cfg_mem(self) -> dict:
        """``output.audio.level`` 的**内存真源**（缺这一段就现补一个空 dict 挂上去）。

        为什么必须写回 ``self._cfg``：设置窗是可以反复重建的，重建时读的是这份内存快照
        而不是磁盘 —— 只落盘不同步的话，关窗再开就看见旧值（缓冲那两个 spin 同理）。
        """
        out = self._cfg.output if isinstance(self._cfg.output, dict) else {}
        a = out.get("audio")
        if not isinstance(a, dict):
            return {}
        lv = a.get("level")
        if not isinstance(lv, dict):
            lv = {}; a["level"] = lv
        return lv

    def _level_mode_names(self) -> dict[str, str]:
        """模式代码 → 界面文案。每次都现取 ``t()``：界面语言可能在运行中改过。

        ⚠️ off 那一项用的 key 是「关闭音量匹配」而不是「关闭」—— 后者在 4 份词表里
        早就是按钮的 "Close"（``gui_update.py`` 的关闭按钮），复用会让英文界面的
        模式下拉显示成 "Close / Fixed gain / Follow mic"。
        """
        return {level_match.MODE_OFF: t("关闭音量匹配"),
                level_match.MODE_FIXED: t("固定增益"),
                level_match.MODE_FOLLOW_MIC: t("跟随麦克风")}

    def _level_mode_code(self) -> str:
        """下拉框当前选中项 → 模式代码；认不出来就退回配置里的值（控件还没建时走这条）。"""
        names = self._level_mode_names()
        var = getattr(self, "_level_mode_var", None)
        if var is not None:
            try:
                shown = str(var.get())
            except Exception:                        # noqa: BLE001 — 占位控件不一定能 get
                shown = ""
            for code, name in names.items():
                if name == shown:
                    return code
        cur = str(self._level_cfg_mem().get("mode") or level_match.MODE_OFF)
        return cur if cur in names else level_match.MODE_OFF

    def _level_read_db(self, var_name: str, lo: float, hi: float,
                       key: str) -> float:
        """读一个 dB 数值控件：脏值/越界一律夹回界面档位并对齐到 0.5 dB，再写回控件。

        夹取而不是拒收：手打 ``999`` 时如果什么都不做，界面显示 999、程序按 6 跑 ——
        这种「看到的和生效的不一样」比夹取更糟（禁静默降级）。
        """
        var = getattr(self, var_name, None)
        if var is None:
            mem = self._level_cfg_mem().get(key)
            try:
                return max(lo, min(hi, float(mem)))
            except (TypeError, ValueError):
                return 0.0
        try:
            val = float(var.get())
        except (TypeError, ValueError):
            val = 0.0
        val = round(max(lo, min(hi, val)) * 2.0) / 2.0
        try:
            var.set(val)
        except Exception:                            # noqa: BLE001
            pass
        return val

    def _on_level_change(self, _event=None) -> None:
        """译音音量三件控件的落地点：读控件 → 落盘 → 同步内存 → 热更新代理与在跑的引擎。"""
        if getattr(self, "_level_mode_combo", None) is None:
            return                                   # 设置页还没建（headless / 未打开）
        mode = self._level_mode_code()
        fixed = self._level_read_db("_level_fixed_var", *_LEVEL_UI_FIXED_DB,
                                    key="fixed_gain_db")
        offset = self._level_read_db("_level_offset_var", *_LEVEL_UI_OFFSET_DB,
                                     key="offset_db")
        mem = self._level_cfg_mem()
        changed = {k: v for k, v in (("mode", mode), ("fixed_gain_db", fixed),
                                     ("offset_db", offset)) if mem.get(k) != v}
        if not changed:
            self._sync_level_controls_state()
            return
        mem.update(changed)
        cfg = level_match.LevelConfig.from_dict(dict(mem))

        def _fn(text):
            for key, val in changed.items():
                # ⚠️ 模式必须走 `_yaml_scalar`：裸词 `off` 是 YAML 1.1 的**布尔字面量**，
                #    不加引号写回去，下次启动读到的就是 Python 的 False 而不是 "off"。
                text = _yaml_set_or_create(
                    text, ["output", "audio", "level", key],
                    _yaml_scalar(val) if isinstance(val, str) else _fmt_scalar(val))
            return text

        # `_yaml_set_or_create`（不是 `_in_text`）：老用户的 config.yaml 里可能整段
        # `level:` 都不存在，`_in_text` 遇到缺父键会静默 no-op —— 设置就永远存不下去。
        _m(self)._yaml_write(_m(self).DEFAULT_CONFIG, _fn, err="保存译音音量")
        self._apply_level_config(cfg)
        print(f"[level] 译音音量已更新：模式 {mode} / 固定增益 {fixed:g} dB / "
              f"相对麦克风 {offset:g} dB（逐句重算生效，不给译音加缓冲）", flush=True)
        self._set_status("info", t("译音音量已更新（逐句生效）"))
        self._sync_level_controls_state()

    def _apply_level_config(self, cfg) -> None:  # noqa: ANN001
        """热更新：代理换参考电平的参数、在跑的引擎换增益参数；都没在跑就只留一行痕。"""
        p = self._proxy
        if p is not None:
            if not hasattr(p, "set_level_config"):
                # Linux 那条代理（micproxy_linux.py，走 pw-cat 管道）本期没接音量匹配：
                # 参数照样落盘、引擎侧照样生效，只是**麦克风参考电平**取不到 →
                # `follow_mic` 会回落固定增益。如实说清楚，不甩一条 AttributeError。
                print("[level] 当前平台的麦克风代理不支持热更新电平参数："
                      "已落盘，麦克风电平参考取不到时 follow_mic 回落固定增益", flush=True)
            else:
                try:
                    p.set_level_config(cfg)
                except Exception as exc:             # noqa: BLE001 — 绝不因调参打断界面
                    print(f"[level] ⚠️ 代理热更新失败：{type(exc).__name__}: {exc}"
                          "（已落盘，下次启动生效）", flush=True)
        hit = 0
        for e in (self._engines or []):
            lm = getattr(e, "level_match", None)
            if lm is None:
                continue
            try:
                lm.set_config(cfg)                   # 逐句生效：下一句就用新参数
                hit += 1
            except Exception as exc:                 # noqa: BLE001
                print(f"[level] ⚠️ 引擎热更新失败：{type(exc).__name__}: {exc}"
                      "（已落盘，本轮仍用旧参数）", flush=True)
        if p is None and not hit:
            print("[level] 代理与引擎都没在跑：参数已落盘，下次启动生效", flush=True)

    def _sync_level_controls_state(self) -> None:
        """按模式置灰控件 + 刷新说明；`off` 时读数行显示 `—`（不参与计算）。"""
        if getattr(self, "_level_mode_combo", None) is None:
            return
        mode = self._level_mode_code()
        for name, on in (("_level_fixed_spin", mode == level_match.MODE_FIXED),
                         ("_level_offset_spin", mode == level_match.MODE_FOLLOW_MIC)):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state=tk.NORMAL if on else tk.DISABLED)
            except Exception:                        # noqa: BLE001
                pass
        hint = getattr(self, "_level_hint", None)
        if hint is not None:
            if mode == level_match.MODE_FIXED:
                msg = t("固定增益：所有译音统一加减这么多 dB（负数=更轻）")
            elif mode == level_match.MODE_FOLLOW_MIC:
                msg = t("跟随麦克风：把译音对齐到你说话时的电平（跨设备免调）")
            else:
                msg = t("已关闭：译音逐字节原样输出（与升级前行为一致）")
            try:
                hint.configure(text=msg)
            except Exception:                        # noqa: BLE001
                pass
        if mode == level_match.MODE_OFF:
            self._level_set_readout(None, None, None)

    def _level_set_readout(self, mic, tts, gain) -> None:  # noqa: ANN001
        """把三个读数写进控件；一个都没有就整行 `—`（绝不显示半真半假的数）。"""
        lbl = getattr(self, "_level_gain_lbl", None)
        if lbl is not None:
            try:
                lbl.configure(text="—" if gain is None else f"{float(gain):+.1f} dB")
            except Exception:                        # noqa: BLE001
                pass
        line = getattr(self, "_level_readout_lbl", None)
        if line is None:
            return
        if mic is None and tts is None and gain is None:
            text = "—"
        else:
            text = t("麦克风 {mic} dBFS ｜ 译音 {tts} dBFS ｜ 增益 {gain} dB",
                     mic=_fmt_level(mic), tts=_fmt_level(tts), gain=_fmt_level(gain))
        try:
            line.configure(text=text)
        except Exception:                            # noqa: BLE001
            pass

    def _level_live_values(self):  # noqa: ANN201
        """(麦克风, 译音, 增益)：有引擎就读引擎的 ``status()``，麦克风电平回落读代理。

        两个来源都是**已经在跑的东西**：引擎的 status() 是句尾测量的快照，代理的
        ``mic_reference_db`` 是直通线程顺带算的中位数 —— 读数绝不新开采集设备。
        """
        mic = tts = gain = None
        for e in (getattr(self, "_engines", None) or []):
            lm = getattr(e, "level_match", None)
            if lm is None:
                continue
            try:
                st = lm.status()
            except Exception:                        # noqa: BLE001
                continue
            if mic is None:
                mic = st.get("mic_db")
            if tts is None:
                tts = st.get("tts_db")
            if gain is None:
                gain = st.get("gain_db")
        if mic is None:
            p = getattr(self, "_proxy", None)
            if p is not None:
                try:
                    mic = p.mic_reference_db
                except Exception:                    # noqa: BLE001
                    mic = None
        return mic, tts, gain

    def _settings_win_viewable(self) -> bool:
        """设置窗此刻是不是真看得见（withdrawn / 已销毁都算看不见）。"""
        win = getattr(self, "_settings_win", None)
        if win is None:
            return False
        try:
            return bool(win.winfo_viewable())
        except Exception:                            # noqa: BLE001
            return False

    def _refresh_level_readout(self) -> None:
        """500ms tick：刷实时读数并**自己再排一次**；窗口一不可见就地停下。

        开关口径照 ``_sync_gate_level_probe()``：只在设置窗可见时跑。开/关设置窗各调
        一次本函数就够了 —— 可见时它续期，不可见时它取消挂起的 job 且不再续期。
        """
        root = getattr(self, "_root", None)
        job = getattr(self, "_level_readout_job", None)
        self._level_readout_job = None
        if job is not None and root is not None:
            try:
                root.after_cancel(job)
            except Exception:                        # noqa: BLE001
                pass
        if root is None or not self._settings_win_viewable():
            return
        if self._level_mode_code() == level_match.MODE_OFF:
            self._level_set_readout(None, None, None)
        else:
            self._level_set_readout(*self._level_live_values())
        try:
            self._level_readout_job = root.after(_LEVEL_TICK_MS,
                                                 self._refresh_level_readout)
        except Exception:                            # noqa: BLE001
            self._level_readout_job = None
