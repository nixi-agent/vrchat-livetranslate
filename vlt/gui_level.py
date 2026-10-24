"""GUI level matching controls and readout lifecycle."""
from __future__ import annotations
import sys
import tkinter as tk
from .i18n import t
from .config_io import _fmt_scalar, _write_config_text, _yaml_set_or_create_checked, _yaml_scalar
from .output import level_match
from .output.level_match import format_db

_LEVEL_TICK_MS = 500
_LEVEL_UI_FIXED_DB = (-24.0, 6.0)
_LEVEL_UI_OFFSET_DB = (-12.0, 12.0)

def _m(gui):
    return sys.modules[gui.__class__.__module__]


class LevelMethods:
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

        ⚠️ **必须接住 `tk.TclError`**：用户在 Spinbox 里手打 ``abc``、或清空后按回车 /
        失焦时，`var.get()`（DoubleVar）抛的是 `_tkinter.TclError`（"expected floating-point
        number but got …"）而不是 ValueError。漏了它，异常就会逃出 Tk 回调 → crashlog 记
        一条「Tk 回调异常」、设置**静默不保存**、控件留着脏值 —— 与上面那句承诺正好相反。
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
        except (TypeError, ValueError, tk.TclError):
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
        try:
            path = _m(self).DEFAULT_CONFIG
            if not path.exists():
                raise FileNotFoundError(f"配置文件不存在（{path}）")
            text = path.read_text(encoding="utf-8")
            for key, val in changed.items():
                # ⚠️ 模式必须走 `_yaml_scalar`：裸词 `off` 是 YAML 1.1 的**布尔字面量**，
                #    不加引号写回去，下次启动读到的就是 Python 的 False 而不是 "off"。
                text = _yaml_set_or_create_checked(
                    text, ["output", "audio", "level", key],
                    _yaml_scalar(val) if isinstance(val, str) else _fmt_scalar(val))
            _write_config_text(path, text)
        except Exception as exc:                         # 保存失败不得冒称已生效，也必须能重试。
            print(f"[level] ⚠️ 保存译音音量失败：{type(exc).__name__}: {exc}", flush=True)
            self._set_status("warn", t("保存失败：{err}", err=str(exc)))
            self._sync_level_controls_state()
            return
        mem.update(changed)
        cfg = level_match.LevelConfig.from_dict(dict(mem))
        self._apply_level_config(cfg)
        print(f"[level] 译音音量已更新：模式 {mode} / 固定增益 {fixed:g} dB / "
              f"相对麦克风 {offset:g} dB（逐句重算生效，不给译音加缓冲）", flush=True)
        self._set_status("info", t("译音音量已更新（逐句生效）"))
        self._sync_level_controls_state()


    def _apply_level_config(self, cfg) -> None:  # noqa: ANN001
        """热更新：代理换参考电平的参数、在跑的引擎换增益参数；都没在跑就只留一行痕。"""
        p = self._proxy
        if p is not None:
            try:
                p.set_level_config(cfg)
            except Exception as exc:                 # noqa: BLE001 — 绝不因调参打断界面
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
                lbl.configure(text="—" if gain is None else f"{format_db(gain)} dB")
            except Exception:                        # noqa: BLE001
                pass
        line = getattr(self, "_level_readout_lbl", None)
        if line is None:
            return
        if mic is None and tts is None and gain is None:
            text = "—"
        else:
            text = t("麦克风 {mic} dBFS ｜ 译音 {tts} dBFS ｜ 增益 {gain} dB",
                     mic=format_db(mic), tts=format_db(tts), gain=format_db(gain))
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


    def _cancel_level_tick(self, _event=None) -> None:  # noqa: ANN001
        """取消挂起的 500ms 读数 tick（幂等）。设置窗 / 根窗被**销毁**时调用。

        为什么不走 `_refresh_level_readout()` 的「不可见就停」那条路：它得先问一句
        「窗口可见吗」才发现自己该停，而窗口已经销毁时连 `winfo_viewable()` 都要靠异常兜；
        更要紧的是**挂起的 `after` 还挂在 root 上**，Tk 收尾时会带着一个已经死掉的命令名
        到点，打一行 `invalid command name "..._refresh_level_readout"`（这是末影猫 L5 报的
        收尾噪音）。`<Destroy>` 事件在「关程序销毁根窗」时也会传到设置窗上，所以这一处绑定
        把两种收尾（销毁设置窗 / 销毁根窗）都盖到。

        绑在设置窗上还有一个好处：生产路径（`withdraw`）**不销毁**窗口，所以这条绑定在
        正常使用中一次都不会触发，纯兜底。
        """
        job = getattr(self, "_level_readout_job", None)
        self._level_readout_job = None
        root = getattr(self, "_root", None)
        if job is None or root is None:
            return
        try:
            root.after_cancel(job)
        except Exception:                            # noqa: BLE001 — 收尾路径绝不抛
            pass


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
