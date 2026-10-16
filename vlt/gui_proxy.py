"""常駐麥克風代理的 GUI 行為；與翻譯供應商無關。"""
from __future__ import annotations
import os
import sys
import tkinter as tk
from pathlib import Path
from . import gui_proxy_hint
from . import platform
from .i18n import t
from .config_io import _fmt_scalar, _write_config_text, _yaml_set_or_create_checked
from .output.micproxy import MODE_TRANSLATED
from .gui_level import (LevelMethods, _LEVEL_TICK_MS as _LEVEL_TICK_MS,
                        _LEVEL_UI_FIXED_DB as _LEVEL_UI_FIXED_DB,
                        _LEVEL_UI_OFFSET_DB as _LEVEL_UI_OFFSET_DB)

def _m(gui):
    return sys.modules[gui.__class__.__module__]

_TEST_ENTRIES = ("run_tests.py", "pytest", "pytest.exe", "py.test")

#: 设置窗里那块实时读数的刷新间隔。500ms 够看（电平是句级的慢变量），
#: 又不至于让「读引擎 status()」变成每帧开销。

#: 界面上能拨的 dB 范围（比 `level_match.RANGES` 的**校验**范围更窄：校验要容得下
#: 手写 config.yaml 的人，界面只给「调了有意义」的那一段）。


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


class ProxyMethods(LevelMethods):
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
        ⚠️ 走 `_write_proxy_leaves`（**补建父级 + 写后复检**），不是 `_yaml_set_in_text`：
        老 config.yaml 里没有 `output.audio.proxy` 整段时旧写法是静默 no-op → 标记永远存不下去
        → 这条说明每次启动都弹（用户实测）。写不进去也**不留死角**：一行 `[proxy]` WARN
        （由 `_write_proxy_leaves` 打）＋ 一个同目录的兜底标记文件，见
        `gui_proxy_hint.write_fallback_marker`。
        """
        ok = self._write_proxy_leaves(
            [(["output", "audio", "proxy", "hint_shown"], _fmt_scalar(True))],
            what="代理首次启用说明的「已提示」标记")
        self._sync_proxy_cfg_mem(proxy={"hint_shown": True})   # 本次运行不再弹（内存真源）
        if not ok:
            gui_proxy_hint.write_fallback_marker(self)

    def _write_proxy_leaves(self, leaves: list[tuple[list[str], str]], *, what: str) -> bool:
        """把 ``output.audio...`` 里的若干叶子值就地写进 config.yaml：**缺失的父级补建 + 写后复检**。

        为什么一律不走 `_yaml_set_in_text`：老用户的 config.yaml 是更早的模板生成的，里面
        可能**整段** `output.audio.proxy`（甚至 `audio.level`）都不存在 —— 旧函数遇到缺父键
        **原样返回**（静默 no-op），于是「关掉代理下次启动又开了」「缓冲改了没记住」
        「首次启用说明每次都弹」全都是同一个根因。`_yaml_set_or_create` 会按缩进补建整条链。

        为什么还要复检（`_yaml_set_or_create_checked`）：补建函数在「找不到落点」时同样
        是不抛的原地返回，光看返回值分不出来 —— 写盘前解析回来确认值真的变了，不等就抛。

        失败 / 没生效一律留一行 ``[proxy]`` WARN（禁静默降级）并返回 False；
        **本函数绝不抛**（写配置失败不该打断界面，更不能把代理启动搞崩）。
        """
        p = _m(self).DEFAULT_CONFIG
        try:
            if not p.exists():
                raise FileNotFoundError(f"配置文件不存在（{p}）")
            text = p.read_text(encoding="utf-8")
            for key_path, value in leaves:
                text = _yaml_set_or_create_checked(text, key_path, value)
            _write_config_text(p, text)
        except Exception as exc:                       # noqa: BLE001 — 绝不因写配置打断界面
            names = " / ".join(".".join(k) for k, _ in leaves)
            print(f"[proxy] ⚠️ {what}没能写进 config.yaml（{names}）："
                  f"{type(exc).__name__}: {exc}（这次改动只在内存里生效）", flush=True)
            return False
        return True


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


    def _rebind_running_engines(self, sink) -> tuple[int, int]:  # noqa: ANN001
        """把**在跑**引擎的译音输出腿整条换到 *sink*（None = 换回引擎自建）。返回 (成功数, 失败数)。

        为什么在这里逐个调 `Engine.rebind_audio_sink`：翻译进行中开 / 关代理时，在跑的引擎手里
        仍是构造时那条 `audio_sink`（issue #90）—— 只把参考接线改活会让「引擎自建 VirtualMic」
        与「新起的代理」同时占同一条虚拟声卡端点。真正的修法是把输出腿整条换掉，由引擎侧那把
        `_sink_lock` 保证与音频泵 / 事件循环互斥（见 `Engine.rebind_audio_sink`）。

        只对**在跑**且**真有** `rebind_audio_sink` 的引擎动手：老实现 / 测试替身没有这个方法就
        跳过（既不计成功也不计失败）—— 调用方据 (0, 0) 走「没能重接线」的旧留痕。每个引擎的
        逐分支留痕（成功 / 跳过 / 失败）由 `Engine.rebind_audio_sink` 自己打；这里只兜住异常，
        绝不让重接线打断界面（禁静默降级：异常也留一行 WARN）。
        """
        ok = fail = 0
        for e in (self._engines or []):
            if not getattr(e, "running", False):
                continue
            rebind = getattr(e, "rebind_audio_sink", None)
            if not callable(rebind):
                continue
            try:
                if rebind(sink):
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:                   # noqa: BLE001 — 绝不因重接线打断界面
                fail += 1
                print(f"[proxy] ⚠️ 重接线引擎译音输出时出错：{type(exc).__name__}: {exc}",
                      flush=True)
        return ok, fail


    def _proxy_translated_wanted(self) -> bool:
        """中途开代理后要不要把档位切到「译音」—— 口径与 `gui_engine.start()` **完全一致**。

        那边是 `want_audio = vmic_var.get()`，再 `Direction.output_audio = want_audio and
        direction == "mine"`（译音输出只对「我说的话」那条腿有效）。两条都成立才切：没勾
        「译音输出」、或当前方向不含「我说」时切过去等于让对方听静音 —— 比原声更糟。
        """
        ctx = getattr(self, "_engine_ctx", None)
        var = getattr(ctx, "vmic_var", None)
        if var is None:
            var = getattr(self, "_vmic_var", None)
        try:
            want = bool(var.get()) if var is not None else False
        except Exception:                            # noqa: BLE001 — 占位控件不一定能 get
            want = False
        if not want:
            return False
        return any(direction == "mine" and getattr(eng, "running", False)
                   for eng, direction in zip(self._engines or [],
                                             self._engine_dirs or []))


    def _proxy_adopt_translation(self, proxy) -> None:  # noqa: ANN001
        """让**中途新建**的代理知道「翻译正在跑」，并按 start() 的口径决定档位（review #1）。

        为什么必须补这一手：`MicProxy._translation_active` 默认 False，而
        `set_translation_active()` 只在翻译启停时被调（`gui_engine.start/stop`）——
        中途开代理这条路径以前从头到尾没调它，于是 `set_mode(MODE_TRANSLATED)` 被代理
        直接拒（`micproxy.set_mode` 里那条「翻译未运行，无法切到译音档」）。症状是：
        重接线已把译音 PCM 灌进代理的 translated 环形缓冲，代理却停在原声档、没人 drain
        （溢出丢弃），用户点「原声/译音」还被告知「翻译未运行」→ 这一轮根本听不到译音。
        """
        try:
            proxy.set_translation_active(True)
        except Exception as exc:                     # noqa: BLE001 — 绝不让它打断界面
            print(f"[proxy] ⚠️ 通知代理「翻译在跑」失败：{type(exc).__name__}: {exc}"
                  "（档位停在原声；主界面「原声/译音」按钮仍可手动切）", flush=True)
            return
        if not self._proxy_translated_wanted():
            print("[proxy] 中途开代理：未勾「译音输出」或本方向不含「我说」→ 档位保持原声"
                  "（与「开始翻译」同一口径：切过去等于让对方听静音）", flush=True)
            return
        if proxy.set_mode(MODE_TRANSLATED):
            print("[proxy] 中途开代理 → 档位切到「译音」（与「开始翻译」的默认行为一致；"
                  "可在主界面「原声/译音」按钮切回）", flush=True)
        else:
            print("[proxy] ⚠️ 中途开代理后切「译音」失败（保持原档位；"
                  "主界面「原声/译音」按钮仍可手动切）", flush=True)


    def _restart_proxy(self) -> None:
        """设置页勾选框的落地点：想开就开、想关就关，按钮态与提示一起刷新。

        翻译**进行中**开 / 关代理时，顺带把在跑引擎的译音输出腿整条重接线（issue #90）：
        开 → 接到代理的 `translated_sink`；关 → 换回引擎自建 VirtualMic。每条腿都逐分支留痕。
        """
        if self._proxy_wanted():
            if self._start_proxy():
                self._set_status("info", t("麦克风代理已启用"))
                # 翻译进行中开的代理：把在跑引擎的译音输出接到代理那条常驻流上（否则引擎
                # 仍用构造时的 audio_sink=None 自建，follow_mic 读不到代理的参考电平）。
                proxy = self._proxy
                sink = getattr(proxy, "translated_sink", None) if proxy is not None else None
                if sink is not None and any(getattr(e, "running", False)
                                            for e in self._engines):
                    ok, fail = self._rebind_running_engines(sink)
                    if fail:
                        print(f"[proxy] ⚠️ 翻译进行中开了代理，但 {fail} 个在跑引擎没能接到"
                              "代理的译音输出：这些引擎本轮译音未接上，重新点「开始翻译」可恢复",
                              flush=True)
                        self._set_status(
                            "warn",
                            t("引擎译音输出重接线失败（本轮译音未接上代理，"
                              "重新点「开始翻译」可恢复）"))
                    elif ok:
                        # ★ review #1：新建的代理 `_translation_active` 是 False，不补这一手
                        #   它就以为翻译没在跑 → `set_mode(MODE_TRANSLATED)` 被拒 → 译音灌进
                        #   translated 缓冲却没人 drain，这一轮根本听不到译音。
                        self._proxy_adopt_translation(proxy)
                        # 如实报**当前档位**：以前这里恒写「原声/译音切换生效」，而代理其实
                        # 停在原声档 —— 与实际相反的留痕比没有留痕更难排查。
                        gear = ("译音" if getattr(proxy, "mode", None) == MODE_TRANSLATED
                                else "原声")
                        print(f"[proxy] 翻译进行中开了代理：已把 {ok} 个在跑引擎的译音输出接到"
                              f"代理 translated_sink（follow_mic 参考即时可用；"
                              f"当前档位 {gear}）", flush=True)
                    else:
                        # 在跑的引擎没有 rebind_audio_sink（旧实现 / 测试替身）→ 没能接上，
                        # 如实留一行痕（禁静默降级：状态栏保持「已启用」，那是真的）。
                        print("[proxy] ⚠️ 翻译进行中开了代理，但在跑引擎不支持重接线："
                              "这些引擎本轮译音仍走自建输出，重新点「开始翻译」后才会接到代理",
                              flush=True)
            return
        running = any(getattr(e, "running", False) for e in self._engines)
        self._close_proxy()
        self._refresh_voice_mode_btn()
        # 先落这条 info：没能重接线时（老引擎 / 替身）它就是**最后**一条状态（旧行为契约）。
        self._set_status("info", t("麦克风代理已关闭（回到旧行为）"))
        if running:
            ok, fail = self._rebind_running_engines(None)
            if fail:
                # 换回自建时虚拟声卡打不开 → 这些引擎本轮译音不出声（必须让用户知道）。
                print(f"[proxy] ⚠️ 翻译进行中关掉了代理，且 {fail} 个在跑引擎没能切回自建译音"
                      "输出（虚拟声卡打不开？）：这些引擎本轮译音不出声，重新点「开始翻译」可恢复",
                      flush=True)
                self._set_status(
                    "warn",
                    t("引擎切回自建译音输出失败（虚拟声卡打不开？本轮译音不出声，"
                      "重新点「开始翻译」可恢复）"))
            elif ok:
                print(f"[proxy] 翻译进行中关掉了代理：已把 {ok} 个在跑引擎的译音输出切回引擎自建"
                      "虚拟声卡（翻译不中断）", flush=True)
            else:
                # 在跑的引擎没有 rebind_audio_sink（旧实现 / 测试替身）→ 保持旧行为的明确提示。
                # 引擎手里那条 audio_sink 指向刚关掉的代理缓冲：本轮译音**没人接管**了。
                print("[proxy] ⚠️ 翻译进行中关掉了代理：本轮译音输出已失效，"
                      "重新点「开始翻译」后由引擎自建虚拟声卡输出（旧行为）", flush=True)


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
        # ⚠️ 不用 `_yaml_set_in_text`：老配置里没有 `output.audio.proxy` 整段时它是静默 no-op
        #    → 「关掉代理、下次启动又开了」（用户在界面上的改动没落盘，还看不到任何提示）。
        self._write_proxy_leaves(
            [(["output", "audio", "proxy", "enabled"], _fmt_scalar(self._audio_ctx.proxy_enabled))],
            what="代理开关")
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
        # ⚠️ 两个键都走补建+复检的那条路：老配置里**没有** `output.audio.proxy` 整段，
        #    以前 `_yaml_set_in_text` 对 `passthrough_buffer_ms` 是静默 no-op →
        #    用户「改了直通缓冲、下次启动又变回默认」，且界面上什么提示都没有。
        #    `buffer_ms` 在同一条链上一起走（它虽在老配置里也有，但 `audio:` 段整体缺失时
        #    同样存不下去）。
        self._write_proxy_leaves(
            [(["output", "audio", "proxy", "passthrough_buffer_ms"], _fmt_scalar(pt)),
             (["output", "audio", "buffer_ms"], _fmt_scalar(tr))],
            what="代理缓冲")
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


    def _settings_win_viewable(self) -> bool:
        """设置窗此刻是不是真看得见（withdrawn / 已销毁都算看不见）。"""
        win = getattr(self, "_settings_win", None)
        if win is None:
            return False
        try:
            return bool(win.winfo_viewable())
        except Exception:                            # noqa: BLE001
            return False
