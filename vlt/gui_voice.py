"""音色选择 / 试听 / 词库管理的 UI 逻辑。

从 ``vlt.gui.TranslationGUI`` 的 I 区（音色）+ J 区（词库）提取而来。
所有 Tk 控件与可变状态通过 :class:`VoiceCtx` 传入，函数本身不持有 ``self`` 引用。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

from . import config as _cfg_mod
from . import endpoints, tts
from .config import _as_str_map, load_api_key
from .config_io import (
    _write_config_text,
    _yaml_set_mapping,
    write_leaf as _write_leaf_shared,
)
from .glossary_text import (
    _glossary_line_issues,
    _glossary_to_lines,
    _parse_glossary_lines,
)
from .i18n import t
from .output.micproxy import MODE_PASSTHROUGH, MODE_TRANSLATED
from .voices import DEFAULT_TTS_MODEL
from .ui_state import current_key_slot
from .ui_text import (
    VOICE_PREVIEW_MODEL,
    VOICE_PREVIEW_TEXT,
    _is_unsupported_voice_err,
    _play_pcm_local,
)


# ================================================================ 上下文

@dataclass
class VoiceCtx:
    """音色 + 词库功能所需的全部**控件引用**与**状态**。

    可变状态（``cfg`` / ``engines`` / ``engine_dirs``）由 gui.py 管理，
    本模块的函数通过参数接收，不直接持有。
    """
    # ── 代理 ──
    proxy: Any = None                       # MicProxy（原声/译音切换）

    # ── 音色控件 ──
    voice_mode_btn: Any = None              # ttk.Button（主界面原声/译音切换）
    speech_voice_var: Any = None            # tk.StringVar
    tts_voice_var: Any = None               # tk.StringVar
    speech_preview_btn: Any = None          # ttk.Button
    tts_preview_btn: Any = None             # ttk.Button

    # ── 词库控件 ──
    glossary_text: Any = None               # tk.Text
    glossary_hint: Any = None               # ttk.Label
    glossary_status: Any = None             # ttk.Label
    glossary_scope_var: Any = None          # tk.StringVar
    glossary_scope_names: dict = field(default_factory=dict)

    # ── 试听状态 ──
    preview_busy: bool = False


# ================================================================ 音色模式（代理切换）

def toggle_voice_mode(ctx: VoiceCtx, *,
                      vmic_get: Callable[[], bool],
                      set_status: Callable,
                      refresh_btn: Callable) -> None:
    """主界面「原声/译音」按钮：切换代理档位。"""
    p = ctx.proxy
    if p is None:
        return
    if p.mode == MODE_TRANSLATED:
        p.set_mode(MODE_PASSTHROUGH)
    else:
        if not vmic_get():
            set_status("warn", t("未勾选「译音输出」，译音档会无声（已在输出行勾选后重试）"))
        if not p.set_mode(MODE_TRANSLATED):
            refresh_btn()
            return
    refresh_btn()


def voice_mode_text(proxy: Any) -> str:
    """当前档位对应的文案 —— 主界面按钮与桌面字幕胶囊**共用这一处**（别各写一份）。

    两处各写一份的话，迟早出现「按钮写译音、字幕胶囊写原声」的自相矛盾。
    """
    if proxy is not None and proxy.mode == MODE_TRANSLATED:
        return t("🗣 译音")
    return t("🎙 原声")


def refresh_voice_mode_btn(ctx: VoiceCtx, engines: list) -> None:
    """刷新主界面切换按钮的文案与可用态。"""
    btn = ctx.voice_mode_btn
    if btn is None:
        return
    p = ctx.proxy
    running = any(e.running for e in engines)
    btn.configure(text=voice_mode_text(p))
    btn.configure(state=(tk.NORMAL if (p is not None and running) else tk.DISABLED))


# ================================================================ 音色选择

def effective_speech_voice(cfg: Any) -> str:
    """「说话译音」现在**真正生效**的音色 —— 与 `Direction.to_session_config` 同口径：
    `directions.mine.voice` > `session.voice` > Tina。
    """
    d = (cfg.directions or {}).get("mine")
    over = (getattr(d, "voice", "") or "") if d is not None else ""
    base = (cfg.session_base or {}).get("voice") or ""
    return str(over or base or "Tina")


def on_speech_voice_change(ctx: VoiceCtx, cfg: Any, engines: list, _event=None, *,
                           set_status: Callable) -> None:
    """「说话译音」音色：写 session.voice 并同步内存。"""
    voice = ctx.speech_voice_var.get().strip()
    if not voice:
        return
    set_voice_config(cfg, voice)
    if isinstance(cfg.session_base, dict):
        cfg.session_base["voice"] = voice
    synced = ""
    d = (cfg.directions or {}).get("mine")
    if d is not None and (getattr(d, "voice", "") or ""):
        write_leaf(["directions", "mine", "voice"], voice, "保存说话译音音色",
                    create=True)
        d.voice = voice
        synced = t("（已同步方向级音色 directions.mine.voice）")
        print(f"[gui] ⚠️ directions.mine.voice 优先于 session.voice，已同步改为 {voice!r}",
              flush=True)
    running = any(e.running for e in engines)
    hint = t("（正在翻译：下次开始翻译生效）") if running else t("（下次开始翻译生效）")
    set_status("info", t("说话译音音色已保存：{v}", v=voice) + synced + hint)
    print(f"[gui] 说话译音音色 → {voice!r}（已写入 session.voice）", flush=True)


def read_tts_model_from_disk() -> str | None:
    """从**磁盘**读 `text_input.tts.model`；文件缺失返回 `None`，解析失败留痕后返回 `None`。

    为什么必须读盘、不能只用内存：`load_config()` 已经把「复刻/设计模型 + 内置音色」
    在内存里回落成默认值，设置窗拿的是**同一份**内存 `cfg` —— 只看内存的话，
    「启动时就已漂移」的用户永远不会触发落盘修复（评审 ⚠️2 实测：A/A2 都不落盘）。
    磁盘那份才是需要被修的真相。读失败不阻断保存，退回内存快照即可。
    """
    try:
        raw = yaml.safe_load(_cfg_mod.DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return None
        section = ((raw.get("text_input") or {}).get("tts") or {})
        if not isinstance(section, dict):
            return None
        return str(section.get("model") or "")
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] 读磁盘打字译音模型失败，退回内存快照：{type(exc).__name__}: {exc}",
              flush=True)
        return None


def on_tts_voice_change(ctx: VoiceCtx, cfg: Any, _event=None, *,
                        set_status: Callable) -> None:
    """「打字译音」音色：写 text_input.tts.voice 并同步内存。"""
    voice = ctx.tts_voice_var.get().strip()
    if not voice:
        return
    set_tts_voice_config(cfg, voice)
    model_changed = ""
    if isinstance(cfg.text_input, dict):
        tts_cfg = cfg.text_input.setdefault("tts", {})
        tts_cfg["voice"] = voice
        # 主线的下拉只有内置音色；老配置若把复刻/设计目标模型留在 model 里，
        # 选内置音色必须把模型一起改回默认，否则合成接口必失败。
        # ⚠️ 判定要看**磁盘**上的 model：启动时已经漂移的用户，内存里早被
        #    `load_config()` 修干净了，只读内存会漏掉落盘（评审 ⚠️2）。
        disk_model = read_tts_model_from_disk()
        stale = next((m for m in (disk_model, str(tts_cfg.get("model") or ""))
                      if _cfg_mod.tts_model_mismatch(m, voice)), "")
        if stale:
            tts_cfg["model"] = DEFAULT_TTS_MODEL
            set_tts_model_config(DEFAULT_TTS_MODEL)
            model_changed = f"；模型 {stale!r} 与内置音色不匹配，已改回 {DEFAULT_TTS_MODEL!r}"
            print(f"[gui] ⚠️ 打字译音模型 {stale!r} 与内置音色 {voice!r} 不匹配 → "
                  f"已改回 {DEFAULT_TTS_MODEL!r}", flush=True)
    set_status("info", t("打字译音音色已保存：{v}（下一条打字即生效）", v=voice))
    print(f"[gui] 打字译音音色 → {voice!r}（已写入 text_input.tts.voice）{model_changed}",
          flush=True)


def set_voice_config(cfg: Any, voice: str) -> None:
    """把说话译音音色写回 config.yaml 的 `session.voice`。"""
    write_leaf(["session", "voice"], voice, "保存说话译音音色")


def set_tts_voice_config(cfg: Any, voice: str) -> None:
    """把打字译音音色写回 `text_input.tts.voice`。"""
    write_leaf(["text_input", "tts", "voice"], voice, "保存打字译音音色",
               create=True)


def set_tts_model_config(model: str) -> None:
    """把打字译音模型写回 `text_input.tts.model`（只有「模型随音色一起修」时才用）。"""
    write_leaf(["text_input", "tts", "model"], model, "保存打字译音模型",
               create=True)


# ================================================================ 配置写入

def write_leaf(path: list[str], value: str, err_label: str,
               *, create: bool = False) -> None:
    """把一个叶子值就地写进 config.yaml；失败只留痕，绝不把配置写坏。"""
    _write_leaf_shared(path, value, err_label, config_path=_cfg_mod.DEFAULT_CONFIG, create=create)


# ================================================================ 词库管理

def glossary_scope(ctx: VoiceCtx) -> str:
    """当前下拉选中的 scope key（`global` / `mine` / `theirs`）。"""
    names = ctx.glossary_scope_names or {}
    var = ctx.glossary_scope_var
    shown = var.get() if var is not None else ""
    for key, label in names.items():
        if label == shown:
            return key
    return "global"


def glossary_scope_label(ctx: VoiceCtx, scope: str) -> str:
    """给用户看的名字（「全局」/「我说」/「别人说」）。"""
    return (ctx.glossary_scope_names or {}).get(scope, scope)


def glossary_scope_path(scope: str) -> list[str]:
    """该 scope 在 config.yaml 里的键路径。"""
    return ["glossary"] if scope == "global" else ["directions", scope, "hotwords"]


def read_glossary_from_disk(cfg: Any, scope: str) -> dict[str, str] | None:
    """从**磁盘**读某个 scope 的词表；读不到 / 解析失败返回 None。"""
    try:
        raw = yaml.safe_load(_cfg_mod.DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return None
        if scope == "global":
            return _as_str_map(raw.get("glossary"), "glossary（专有词库）")
        section = (raw.get("directions") or {}).get(scope) or {}
        return _as_str_map(section.get("hotwords"),
                           f"directions.{scope}.hotwords（方向级热词）")
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] 读磁盘词库（{scope}）失败，退回内存快照："
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


def read_glossary_from_memory(cfg: Any, scope: str) -> dict[str, str]:
    """内存快照（磁盘读不到时的兜底）。"""
    if scope == "global":
        return dict((cfg.session_base or {}).get("glossary") or {})
    d = (cfg.directions or {}).get(scope)
    return dict(getattr(d, "hotwords", None) or {})


def glossary_hint_text(ctx: VoiceCtx, scope: str) -> str:
    """提示语随 scope 变。"""
    if scope == "global":
        return t("每行一条，格式：原文=译名（社团名 / 人名 / 专有术语）；"
                 "作用于两个方向 —— 两个方向都要同一个译名时才放这里")
    return t("每行一条，格式：原文=译名（社团名 / 人名 / 专有术语）；"
             "只对「{dir}」这条通路生效，同名词条会覆盖全局",
             dir=glossary_scope_label(ctx, scope))


def on_glossary_scope_change(ctx: VoiceCtx, cfg: Any, _event=None) -> None:
    """切换作用方向 = 换一张表编辑。"""
    refresh_glossary_box(ctx, cfg)


def on_save_glossary(ctx: VoiceCtx, cfg: Any, *,
                     set_glossary_status: Callable,
                     push_to_engines: Callable,
                     config_path: Any = None) -> None:
    """把文本框里的词库写回 config.yaml 的**当前作用方向**那张表。"""
    scope = glossary_scope(ctx)
    try:
        mapping = _parse_glossary_lines(ctx.glossary_text.get("1.0", tk.END))
    except Exception as exc:  # noqa: BLE001
        set_glossary_status(t("保存失败：{err}", err=f"{type(exc).__name__}: {exc}"),
                            warn=True)
        return

    saved = save_glossary_config(glossary_scope_path(scope), mapping,
                                config_path=config_path or _cfg_mod.DEFAULT_CONFIG)
    # 同步内存
    if scope == "global":
        if isinstance(cfg.session_base, dict):
            cfg.session_base["glossary"] = dict(mapping)
    else:
        d = (cfg.directions or {}).get(scope)
        if d is None:
            print(f"[gui] ⚠️ 配置里没有方向 {scope!r}：本次只落盘，未同步内存", flush=True)
        else:
            d.hotwords = dict(mapping)
    push_to_engines(scope, mapping)

    bad = _glossary_line_issues(ctx.glossary_text.get("1.0", tk.END))
    if saved:
        if bad:
            for _lineno, _raw in bad:
                print(f"[gui] ⚠️ 词库第 {_lineno} 行格式看不懂（要写成 原文=译名），"
                      f"已忽略：{_raw!r}", flush=True)
            set_glossary_status(
                t("已保存 {n} 条词条到「{scope}」；{bad} 行看不懂已忽略（要写成 原文=译名）",
                  n=len(mapping), bad=len(bad),
                  scope=glossary_scope_label(ctx, scope)),
                warn=True)
        else:
            set_glossary_status(
                t("已保存 {n} 条词条到「{scope}」（正在翻译时会重建会话生效）",
                  n=len(mapping), scope=glossary_scope_label(ctx, scope)))
        print(f"[gui] 专有词库已保存（{scope}）：{len(mapping)} 条"
              + (f"（另有 {len(bad)} 行格式看不懂已忽略）" if bad else ""), flush=True)
    else:
        set_glossary_status(t("保存失败：{err}", err="写入 config.yaml 失败，见日志"),
                            warn=True)


def save_glossary_config(path: list[str], mapping: dict[str, str], *,
                         config_path: Any = None) -> bool:
    """整段替换 config.yaml 里 `path` 指向的那张词表；成功返回 True。"""
    p = config_path if config_path is not None else _cfg_mod.DEFAULT_CONFIG
    if not p.exists():
        return False
    try:
        text = p.read_text(encoding="utf-8")
        _write_config_text(p, _yaml_set_mapping(text, path, mapping))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] 保存专有词库失败（{path}）：{exc}", flush=True)
        return False


def push_glossary_to_engines(engines: list, engine_dirs: list,
                             ctx: VoiceCtx, scope: str,
                             mapping: dict[str, str]) -> None:
    """通知**受影响的**引擎。"""
    label = glossary_scope_label(ctx, scope)
    for eng, direction in zip(engines, engine_dirs):
        if not eng.running:
            continue
        if scope == "global":
            eng.set_glossary(mapping)
        elif direction == scope:
            eng.set_direction_hotwords(scope, mapping, label=label)


def set_glossary_status(ctx: VoiceCtx, text: str, *, warn: bool = False) -> None:
    """设置词库状态标签。"""
    lbl = ctx.glossary_status
    if lbl is None:
        return
    try:
        lbl.configure(text=text, style="Warn.TLabel" if warn else "Muted.TLabel")
    except Exception:  # noqa: BLE001
        pass


def refresh_glossary_box(ctx: VoiceCtx, cfg: Any) -> None:
    """把文本框重填成**磁盘上当前 scope 的那张**词表。"""
    box = ctx.glossary_text
    if box is None:
        return
    scope = glossary_scope(ctx)
    mapping = read_glossary_from_disk(cfg, scope)
    if mapping is None:
        mapping = read_glossary_from_memory(cfg, scope)
    try:
        box.delete("1.0", tk.END)
        for line in _glossary_to_lines(mapping):
            box.insert(tk.END, line + "\n")
        hint = ctx.glossary_hint
        if hint is not None:
            hint.configure(text=glossary_hint_text(ctx, scope))
        set_glossary_status(ctx, "")
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] 刷新词库文本框失败：{exc}", flush=True)


# ================================================================ 音色试听

def on_preview_speech_voice(ctx: VoiceCtx, cfg: Any, *,
                            q: Any,
                            set_status: Callable,
                            provider_fn: Callable[[], str],
                            resolve_api_key: Callable[[], str],
                            play_fn: Callable = None) -> None:
    """试听说话译音音色。"""
    preview_voice(ctx, cfg, "speech",
                  q=q, set_status=set_status, provider_fn=provider_fn,
                  resolve_api_key=resolve_api_key, play_fn=play_fn)


def on_preview_tts_voice(ctx: VoiceCtx, cfg: Any, *,
                         q: Any,
                         set_status: Callable,
                         provider_fn: Callable[[], str],
                         resolve_api_key: Callable[[], str],
                         play_fn: Callable = None) -> None:
    """试听打字译音音色。"""
    preview_voice(ctx, cfg, "tts",
                  q=q, set_status=set_status, provider_fn=provider_fn,
                  resolve_api_key=resolve_api_key, play_fn=play_fn)


def preview_voice(ctx: VoiceCtx, cfg: Any, kind: str, *,
                  q: Any,
                  set_status: Callable,
                  provider_fn: Callable[[], str],
                  resolve_api_key: Callable[[], str],
                  play_fn: Callable = None) -> None:
    """合成一句固定样例并在本地扬声器播放。"""
    if ctx.preview_busy:
        return
    btn = ctx.speech_preview_btn if kind == "speech" else ctx.tts_preview_btn
    var = ctx.speech_voice_var if kind == "speech" else ctx.tts_voice_var
    voice = (var.get() or "").strip()
    if not voice:
        set_status("warn", t("请先选择或填写音色"))
        return
    api_key = resolve_api_key()
    if not api_key:
        set_status("warn", t("还没配置 API key，无法试听（见右上角「设置」）"))
        return
    base_url = str(cfg.session_base.get("base_url") or "")
    try:
        omni_endpoint = endpoints.chat_url(base_url)
        tts_endpoint = endpoints.tts_url(base_url)   # Qwen-Audio-TTS（不是老多模态路径）
    except ValueError as exc:
        set_status("error", t("试听失败：{msg}", msg=exc))
        print(f"[gui] ❌ 试听取消：无法从当前线路派生端点（{exc}）", flush=True)
        return
    ctx.preview_busy = True
    if btn is not None:
        btn.configure(state=tk.DISABLED, text=t("试听中…"))
    set_status("info", t("正在试听「{v}」…", v=voice))
    line = endpoints.describe(provider_fn(), base_url)
    print(f"[gui] 试听音色 → {voice!r}（{kind}，模型 {VOICE_PREVIEW_MODEL}）| {line}",
          flush=True)
    threading.Thread(target=preview_worker,
                     args=(q, kind, voice, api_key),
                     kwargs={"omni_endpoint": omni_endpoint,
                             "tts_endpoint": tts_endpoint,
                             "play_fn": play_fn or _play_pcm_local},
                     daemon=True, name="vlt-voice-preview").start()


def preview_worker(q: Any, kind: str, voice: str, api_key: str, *,
                   omni_endpoint: str | None = None,
                   tts_endpoint: str | None = None,
                   play_fn: Callable = None) -> None:
    """守护线程体：合成 + 播放，结果回主线程。"""
    _play = play_fn or _play_pcm_local
    err = ""
    try:
        if kind == "speech":
            pcm = tts.synthesize_omni(VOICE_PREVIEW_TEXT, voice=voice, api_key=api_key,
                                      endpoint=omni_endpoint)
        else:
            pcm = tts.synthesize(VOICE_PREVIEW_TEXT, voice=voice,
                                 model=VOICE_PREVIEW_MODEL, api_key=api_key,
                                 endpoint=tts_endpoint)
        _play(pcm)
    except tts.TtsError as exc:
        err = str(exc)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    q.put(("voice_preview", kind, voice, err))


def on_voice_preview_done(ctx: VoiceCtx, kind: str, voice: str, err: str, *,
                          set_status: Callable) -> None:
    """主线程：恢复按钮 + 报结果。"""
    ctx.preview_busy = False
    btn = ctx.speech_preview_btn if kind == "speech" else ctx.tts_preview_btn
    try:
        if btn is not None and btn.winfo_exists():
            btn.configure(state=tk.NORMAL, text=t("试听"))
    except Exception:  # noqa: BLE001
        pass
    if not err:
        set_status("info", t("试听完成：{v}", v=voice))
        return
    if _is_unsupported_voice_err(err):
        set_status("warn", t("此音色暂不支持试听（服务端拒收该音色 id）"))
        print(f"[gui] ⚠️ 音色 {voice!r}（{kind}）试听被服务端拒收：{err}", flush=True)
        return
    set_status("error", t("试听失败：{msg}", msg=err))
    print(f"[gui] 试听失败（{voice!r}）：{err}", flush=True)


# ================================================================ 辅助

def resolve_api_key_safe(cfg: Any) -> str:
    """按 config.load_api_key 的口径取 key，取不到返回空串。"""
    try:
        return load_api_key(slot=current_key_slot(cfg))
    except SystemExit:
        return ""
    except Exception:  # noqa: BLE001
        return ""
