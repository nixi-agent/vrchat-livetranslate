"""常駐麥克風代理的 GUI 行為；與翻譯供應商無關。"""
from __future__ import annotations
import os
import sys
import tkinter as tk
from pathlib import Path
from . import platform
from .i18n import t
from .config_io import _fmt_scalar, _yaml_set_in_text

def _m(gui):
    return sys.modules[gui.__class__.__module__]

_TEST_ENTRIES = ("run_tests.py", "pytest", "pytest.exe", "py.test")

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
