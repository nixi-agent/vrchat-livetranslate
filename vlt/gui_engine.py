"""引擎启动 / 停止 / 生命周期的纯逻辑。

从 ``vlt.gui.TranslationGUI`` 的 K 区（约 L1648-L2198）提取而来。
所有 Tk 控件与可变状态通过 :class:`EngineCtx` 传入，函数本身不持有 ``self`` 引用。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

import sys
import threading
import time

from .gui_engine_context import EngineCtx as EngineCtx
from .gui_sinks import (start_overlay, push_overlay as push_overlay, desktop_cfg as desktop_cfg,
    start_desktop, stop_desktop, push_desktop as push_desktop, toggle_desktop_drag as toggle_desktop_drag,
    schedule_desktop_save as schedule_desktop_save, save_desktop_cfg as save_desktop_cfg, _stop_overlay)
from . import config as _cfg_mod
from . import crashlog, gui_chat, gui_voice
from .config import Direction
from .engine import (Engine, EngineEvents, effective_loopback_mode,
                     monitor_capture_conflict)
from .i18n import t
from .output.micproxy import MODE_TRANSLATED
from .ui_theme import CLOSE_WAIT_STOP_S, STOP_WAIT_S

# ================================================================ 上下文

# ================================================================ API 密钥

def resolve_api_key_safe(ctx: EngineCtx) -> str:
    """安全获取 API 密钥（委托给 ``gui_voice``）。"""
    return gui_voice.resolve_api_key_safe(ctx.cfg)

# ================================================================ 语言推送

def push_lang_to_engines(ctx: EngineCtx) -> None:
    """把当前语言设置推送到所有正在运行的引擎。"""
    a = ctx.lang_pair.get("source", "")
    b = ctx.lang_pair.get("target", "") or "en"
    for name, (src, tgt) in (("mine", (a, b)), ("theirs", (b, a or "zh"))):
        d = ctx.cfg.directions.setdefault(name, Direction())
        d.source_lang = src
        d.target_lang = tgt
    for eng, direction in zip(ctx.engines, ctx.engine_dirs):
        if not eng.running:
            continue
        src, tgt = (a, b) if direction == "mine" else (b, a or "zh")
        eng.set_languages(src, tgt)

def push_chatbox_toggle(ctx: EngineCtx) -> None:
    """把 chatbox 勾选框的当前状态推到所有**正在运行**的引擎（界面点一下即时生效）。

    只有 direction=="mine" 的腿会接受（气泡只承载自己发言，见 ``Engine.set_chatbox_enabled``）。
    若本次方向根本不含「我说的话」、用户却勾了 chatbox —— 没有任何腿能上屏，
    必须像启动时那样明说（禁静默降级），否则用户对着"没反应的勾选框"排查。
    """
    want = bool(ctx.chatbox_var.get()) if ctx.chatbox_var else False
    applied = False
    for eng in ctx.engines:
        try:
            if eng.set_chatbox_enabled(want):
                applied = True
        except Exception as exc:              # noqa: BLE001
            print(f"[gui] ⚠️ 运行时切换 chatbox 失败（忽略，其余腿照常）："
                  f"{type(exc).__name__}: {exc}", flush=True)
    if want and ctx.engines and not applied and ctx.set_status_fn:
        _zh = ("chatbox 只发「我说的话」的译文（当前方向不含它）→ 本次 chatbox 不会输出；"
               "对方的译文看手腕屏／聊天区")
        ctx.set_status_fn("warn", t(_zh))

# ================================================================ 启动

def bind_gui_callbacks(ctx: EngineCtx, gui) -> None:
    """设置 EngineCtx 上的回调引用（从 gui 实例读取）。"""
    c = ctx
    c.set_status_fn = gui._set_status; c.on_engine_text_fn = gui._on_engine_text
    c.set_text_input_enabled_fn = gui._set_text_input_enabled; c.refresh_api_key_fn = gui._refresh_api_key_in_cfg
    c.start_room_fn = gui._start_room; c.stop_room_fn = gui._stop_room
    c.refresh_room_status_fn = gui._refresh_room_status_label
    c.sync_gate_probe_fn = gui._sync_gate_level_probe; c.stop_gate_probe_fn = gui._stop_gate_probe
    c.maybe_replace_on_exit_fn = gui._maybe_replace_on_exit
    c.destroy_root_fn = lambda: gui._root.destroy(); c.save_ui_state_fn = gui._save_ui_state
    c.cancel_poll_fn = lambda: gui_chat.cancel_poll(gui._chat_ctx, gui._root)
    c.proxy_fn = lambda: gui._proxy          # 现取：代理会被设置页重开/关闭，实例会变
    c.power_state_fn = gui._set_power_state  # 开始/停止单按钮的唯一刷新入口

def _proxy_of(ctx: EngineCtx):
    """取常驻麦克风代理；没有（Linux / 用户关掉 / 虚拟声卡没打开）则 None。"""
    if ctx.proxy_fn is None:
        return None
    try:
        return ctx.proxy_fn()
    except Exception as exc:              # noqa: BLE001
        print(f"[proxy] ⚠️ 读取麦克风代理失败（按「无代理」处理，译音输出回落到引擎自建）："
              f"{type(exc).__name__}: {exc}", flush=True)
        return None

def notify_proxy_translation(ctx: EngineCtx, active: bool) -> None:
    """翻译启停 → 通知代理（译音档只在翻译运行时允许；停止即强制回落并锁定原声档）。"""
    p = _proxy_of(ctx)
    if p is None:
        return
    try:
        p.set_translation_active(bool(active))
        print(f"[proxy] 翻译{'开始' if active else '停止'} → "
              f"{'允许切到译音档' if active else '强制回落原声档'}"
              f"（当前档位 {p.mode}）", flush=True)
    except Exception as exc:              # noqa: BLE001
        print(f"[proxy] ⚠️ 同步翻译状态失败（忽略，代理仍可用）："
              f"{type(exc).__name__}: {exc}", flush=True)

def set_power_state(ctx: EngineCtx, state: str) -> None:
    """刷主界面那个「开始/停止」单按钮。回调缺失（headless / 老 ctx）时静默跳过。"""
    if ctx.power_state_fn is None:
        return
    try:
        ctx.power_state_fn(state)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] ⚠️ 刷新开始/停止按钮失败（忽略）：{type(exc).__name__}: {exc}",
              flush=True)

def start(ctx: EngineCtx) -> bool:
    """启动翻译引擎（主入口，约 105 行）。

    检查 API key → 读方向 / 输出 → 建 specs → 配方向 → 起覆盖层 →
    起引擎 → 改按钮态。

    返回：因「没配 API key」而被第一段拦下时返回 ``True``（gui.py 薄壳据此
    自动弹设置窗引导填 key）；其余情况（已在跑 / 正常启动）返回 ``False``。
    """
    if any(e.running for e in ctx.engines):
        return

    # 防呆：key 可能在运行期被保存/清除/改环境，先重解再检查
    if ctx.refresh_api_key_fn:
        ctx.refresh_api_key_fn()
    if not (ctx.cfg.session_base.get("api_key") or "").strip():
        # 没 key 就别白连一次（会撞 401），直接把用户送到填 key 的地方
        if ctx.set_status_fn:
            ctx.set_status_fn("error", t("还没配置 API key —— 点右上角「API key ›」填一个再开始"))
        # 返回 True：光给状态栏红字不够，用户点了「开始」却什么都没发生会以为卡了。
        # gui.py 薄壳据此自动弹设置窗，把填 key 的入口送到眼前。
        return True

    d = ctx.direction_var.get()
    # 四个输出开关的实际布尔值：守卫拦人时一次性取齐。
    # 守卫以前是**静默**的（v0.12.2 用户发来的日志里那次点击一行都没有），
    # 出事只能靠猜他勾了什么 —— 现在任何一条拦截路径都必须留下这四个值和方向。
    cb_on = bool(ctx.chatbox_var.get()) if ctx.chatbox_var else False
    ov_on = bool(ctx.overlay_var.get()) if ctx.overlay_var else False
    dt_on = bool(ctx.desktop_var.get()) if ctx.desktop_var else False
    vm_on = bool(ctx.vmic_var.get()) if ctx.vmic_var else False
    flags = f"chatbox={cb_on} overlay={ov_on} desktop={dt_on} vmic={vm_on} 方向={d}"

    sinks: set[str] = set()
    if cb_on:
        sinks.add("chatbox")
    if ov_on:
        sinks.add("overlay")
    if dt_on:
        # "desktop" 只是界面层的标记（字幕窗由界面持有，引擎不认这个 sink），
        # 放在这里是为了让「只勾桌面字幕」也能通过下面的"至少选一个输出"检查。
        sinks.add("desktop")
    # 译音输出（虚拟声卡）**也是一种输出**：只勾它、方向含「我说的话」时必须能开始。
    # 旧写法不把它算进来 → 只想让对方听到译音、不要 chatbox / 字幕的用户被
    # 「请至少选择一个输出」挡住，只能被迫再勾一个自己不要的输出（用户实测 v0.12.2）。
    # 方向只翻别人时译音本来就不会出声（下面 audio_warn 分支会把 want_audio 置 False），
    # 所以只在方向含「我说的话」时才计为有效输出。
    has_mine_leg = d in ("mine", "dual")
    if vm_on and has_mine_leg:
        sinks.add("voice")
    if not sinks:
        if vm_on:
            # 只勾了译音输出、但方向不含「我说的话」：说清「为什么不会出声、该怎么办」，
            # 别丢一句含糊的「请至少选择一个输出」让人去猜（禁静默降级）。
            msg = t("译音输出只对「我说的话」方向有效（当前方向只翻别人）→ 换个输出，或把方向改成「我说」／「双向同时」")
        else:
            msg = t("请至少选择一个输出")
        print(f"[gui] ⚠️ 输出检查未通过，已拦下启动：{msg}（{flags}）", flush=True)
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", msg)
        return

    a = ctx.lang_pair.get("source", "")
    b = ctx.lang_pair.get("target", "") or "en"
    specs: list[tuple] = []
    if d in ("mine", "dual"):
        specs.append(("mine", "mine", "mic", a, b))
    if d in ("theirs", "dual"):
        specs.append(("theirs", "theirs", "loopback", b, a or "zh"))

    # chatbox 只承载「我说的话」的译文（气泡在别人眼里代表我发言）。
    # 若本次方向不含「我说」，用户勾了 chatbox 也一条都发不出去 —— 必须像
    # 译音输出那样明说，别让人对着"没反应的 chatbox"排查（禁静默降级）。
    chatbox_warn = ""
    if "chatbox" in sinks and not any(s[1] == "mine" for s in specs):
        _zh = ("chatbox 只发「我说的话」的译文（当前方向不含它）→ 本次 chatbox 不会输出；"
               "对方的译文看手腕屏／聊天区")
        chatbox_warn = t(_zh)
        print(f"[gui] ⚠️ {_zh}", flush=True)

    # 启动时把「方向 + 每条腿的来源/语言 + 输出面」写进日志。
    _label = {"mine": "我说的话", "theirs": "别人说", "dual": "双向同时"}.get(d, d)
    print(f"[gui] 启动：方向={_label}({d}) | 输出={','.join(sorted(sinks)) or '无'} | "
          f"语言对={a}→{b}", flush=True)
    for _w, _dir, _src, _sl, _tl in specs:
        print(f"[gui]   腿 {_dir}：来源={'麦克风' if _src == 'mic' else '游戏音频(loopback)'}"
              f" | {_sl}→{_tl}", flush=True)

    # 译音输出：勾选框（总开关）+ 方向级开关，两者是「与」关系，都开才出声。
    want_audio = vm_on
    audio_warn = ""
    if want_audio and not any(s[1] == "mine" for s in specs):
        _zh = "译音输出只对「我说的话」方向有效（当前方向不含它）→ 本次已忽略"
        audio_warn = t(_zh)
        print(f"[gui] ⚠️ {_zh}", flush=True)
        want_audio = False
    if isinstance(ctx.cfg.output, dict):
        ctx.cfg.output.setdefault("audio", {})["enabled"] = want_audio
        _dev = (ctx.cfg.output.get("audio") or {}).get("device_name") or "自动回退链"
    else:
        _dev = "自动回退链"
    # 代理在跑 → 译音不自己开流，而是灌进代理那条**常驻**虚拟声卡输出流。
    # 日志里必须区分两条路：出问题时「声音从哪来」是第一个要问的。
    _proxy = _proxy_of(ctx)
    _via = ""
    if _proxy is not None:
        _via = "，经麦克风代理"
        try:
            _dev = _proxy.translated_sink.device_name or _dev
        except Exception:                          # noqa: BLE001
            pass
    print(f"[gui]   译音输出={'开' if want_audio else '关'}（虚拟声卡：{_dev}{_via}）", flush=True)

    # 配置每条腿的方向
    for _who, direction, _src, src_lang, tgt_lang in specs:
        dd = ctx.cfg.directions.setdefault(direction, Direction())
        dd.source_lang = src_lang
        dd.target_lang = tgt_lang
        dd.output_audio = want_audio and direction == "mine"

    # 初始化运行态
    ctx.current = {}
    ctx.auto_scroll = True
    ctx.engines = []
    ctx.engine_dirs = []
    ctx.specs = specs
    ctx.sinks = sinks
    ctx.pending_starts = len(specs)

    # 手腕屏 / 桌面字幕由界面持有，内容镜像聊天区（两个方向都进同一块屏）
    start_overlay(ctx)
    start_desktop(ctx)
    # 连接意图开着就确保房间在跑（stop() 会连房间一起停；start_room 幂等，已在跑则无操作）
    if ctx.start_room_fn:
        ctx.start_room_fn()

    # 先告诉代理「翻译开始了」：译音档只在翻译运行时允许切，顺序反了会被拒
    notify_proxy_translation(ctx, True)
    # 开始翻译就默认走「译音」档（用户要求：勾了译音输出，对方就该听到译音）。
    # ⚠️ 只在**译音真的会有声音**时才切：勾选框没勾 / 方向不含「我说的话」时
    #    want_audio 是 False，切过去等于让对方听静音 —— 比原声更糟，所以不切。
    if want_audio:
        _p_mode = _proxy_of(ctx)
        if _p_mode is not None:
            if _p_mode.set_mode(MODE_TRANSLATED):
                print("[proxy] 开始翻译 → 默认档位切到「译音」"
                      "（可在主界面「原声/译音」按钮切回）", flush=True)
            else:
                print("[proxy] ⚠️ 开始翻译时默认切「译音」失败"
                      "（保持原档位；主界面按钮仍可手动切）", flush=True)
    # 启动第一个引擎（后续引擎由 start_engine 错开 300ms 调度）
    start_engine(ctx, 0)

    # 按钮态
    set_power_state(ctx, "running")
    if ctx.set_text_input_enabled_fn:
        ctx.set_text_input_enabled_fn(d in ("mine", "dual"))

    warns = [w for w in (audio_warn, chatbox_warn) if w]
    if warns:
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", "；".join(warns))
    elif d == "mine":
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("正在启动…（只翻译你说的话；要翻译对方/视频请选「双向同时」）"))
    else:
        if ctx.set_status_fn:
            ctx.set_status_fn("info",
                              t("正在启动（双向）…") if len(specs) == 2 else t("正在启动…"))

def apply_ear_monitor_policy(ctx: EngineCtx) -> None:
    """开翻前处置「耳返 × 采集端点」的回路风险（每次开始翻译跑一次）。

    两种结局，都不静默：
      * 能切 process → 把内存里的 `capture.loopback_mode` 改成 process（只影响本次运行），
        状态栏说明「已自动改为只抓 VRChat」；
      * 切不了（旧系统）→ 关掉耳返 + warn，绝不留下会自我回译的回路。
    配置**不写盘**：用户没主动改的设置不替他落盘，下次启动仍是原样。
    """
    cap = (ctx.cfg.output or {}).get("capture") or {}
    mon = ((ctx.cfg.output or {}).get("audio") or {}).get("monitor") or {}
    if not mon.get("enabled"):
        return
    if not monitor_capture_conflict(mon, cap):
        return
    if effective_loopback_mode(cap, mon) == "process":
        cap["loopback_mode"] = "process"
        on_engine_status(ctx.q, "info",
                         "耳返与 VRChat 音频是同一个设备 → 采集自动改为只抓 VRChat"
                         "（否则会把自己的译音再翻一遍）", "monitor")
        return
    proxy = _proxy_of(ctx)
    if proxy is not None:
        proxy.restart_monitor(enabled=False,
                             device_name=str(mon.get("device_name") or ""),
                             volume=float(mon.get("volume", 70) or 70))
    mon["enabled"] = False
    on_engine_status(ctx.q, "warn",
                     "耳返已关闭：本机不支持「只抓 VRChat」的采集方式，"
                     "系统输出采集会把耳返再翻一遍", "monitor")


def start_engine(ctx: EngineCtx, index: int) -> None:
    """启动第 *index* 个引擎实例（递归错开 300ms 启动多条腿）。"""
    specs = ctx.specs
    if index >= len(specs):
        return
    who, direction, source, src_lang, tgt_lang = specs[index]
    if index == 0:
        apply_ear_monitor_policy(ctx)
    # 引擎不碰手腕屏 / 桌面字幕 / 译音标记：前两个由界面持有（一块屏显示两个方向的
    # 对话，交给两个引擎会撞 `OverlayError_KeyInUse`），"voice" 只是界面层的输出标记
    # （引擎按 `Direction.output_audio` 自己决定出不出音，不认这个 sink）。
    own_sinks = {s for s in ctx.sinks if s not in ("overlay", "desktop", "voice")}
    # chatbox 只发「我说的话」的译文——theirs 腿不需要它（与 engine._chatbox_wanted 同义，双保险）。
    if direction == "theirs":
        own_sinks.discard("chatbox")
    events = EngineEvents(
        # 上行挂在界面这一层（不改 engine.py）：on_engine_text_fn 把文本塞进界面队列，
        # 再顺带把「我自己说的话」的源文发进房间。_srcid 绑定这条腿的真实来源
        # （mic/loopback）—— should_publish 靠它把 loopback 那条腿排除掉（防二次广播/回环）。
        on_text=(lambda src, txt, final, _who=who, _srcid=source:
                 ctx.on_engine_text_fn(_who, _srcid, src, txt, final))
                if ctx.on_engine_text_fn else lambda *a: None,
        on_status=lambda lvl, msg, _who=who: on_engine_status(ctx.q, lvl, msg, _who),
        on_stats=lambda s: ctx.q.put(("stats", s)),
    )
    # 麦克风代理在跑 → 译音灌进代理那条**常驻**输出流（引擎不自建、也不关它，
    # 见 engine._setup_virtualmic 的 _owns_virtualmic=False）；
    # 没代理 → None，引擎自建虚拟声卡输出（旧行为，随翻译启停）。
    proxy = _proxy_of(ctx)
    eng = Engine(
        cfg=ctx.cfg,
        direction=direction,
        source=source,
        sinks=own_sinks,
        events=events,
        config_path=_cfg_mod.DEFAULT_CONFIG,
        audio_sink=(proxy.translated_sink if proxy is not None else None),
    )
    ctx.engines.append(eng)
    ctx.engine_dirs.append(direction)
    ctx.pending_starts -= 1
    eng.start()
    # 引擎起来了 → 电平改由引擎那条腿提供，探针必须让位
    # （同一时刻只允许一路 loopback，否则两路抢同一个采集端点）
    if ctx.sync_gate_probe_fn:
        ctx.sync_gate_probe_fn()
    if ctx.pending_starts > 0:
        # 两个引擎错开 300ms 启动，避免同时抢占音频设备
        ctx.start_job = ctx.root.after(300, lambda: start_engine(ctx, index + 1))

def on_engine_status(q, lvl: str, msg: str, who: str) -> None:
    """引擎状态既要进状态栏，也要进日志（stdout）。

    状态栏文字不落盘 —— 少了这一行，「译音输出为什么没出声」「设备为什么没匹配上」
    这类提示在用户发来的日志里完全看不到，只能靠猜。
    """
    print(f"[{who}][{lvl}] {msg}", flush=True)
    q.put(("status", lvl, msg))

# ================================================================ 手腕屏（界面持有）

# ================================================================ 桌面字幕（界面持有）

# ================================================================ 停止

def stop(ctx: EngineCtx) -> None:
    """停止翻译：**绝不在界面线程等引擎收尾**（真机实测冻 20s = 窗口无响应）。

    先对 **所有** 引擎并发下发停止信号（采集立刻停），收尾交给后台线程，
    界面只留一行「正在停止…」，收尾完成由 ``_poll`` 从队列里收到通知再恢复。
    """
    ctx.pending_starts = 0
    ctx.specs = []
    if ctx.start_job is not None:
        try:
            ctx.root.after_cancel(ctx.start_job)
        except Exception:
            pass
        ctx.start_job = None

    engines = list(ctx.engines)
    for eng in engines:
        try:
            eng.request_stop()            # 只发信号：并发下发，谁都不等谁
        except Exception as exc:          # noqa: BLE001
            print(f"[gui] 下发停止信号失败（忽略）：{type(exc).__name__}: {exc}", flush=True)

    # 立刻让代理回落原声档（不等引擎收尾）：译音源没了，麦克风该马上重新直通
    notify_proxy_translation(ctx, False)

    # 关闭覆盖层
    _stop_overlay(ctx)
    stop_desktop(ctx)

    # 停房间
    if ctx.stop_room_fn:
        ctx.stop_room_fn()
    if ctx.refresh_room_status_fn:
        ctx.refresh_room_status_fn()

    ctx.engines = []                      # 立刻移走：收尾由后台线程负责
    ctx.engine_dirs = []

    if ctx.set_text_input_enabled_fn:
        ctx.set_text_input_enabled_fn(False)

    # 引擎没了 → 设置窗若还开着且勾了「启用」，电平交回独立探针
    if ctx.sync_gate_probe_fn:
        ctx.sync_gate_probe_fn()

    if not engines:
        # 没有引擎在手：但可能还有上一次的收尾在飞（只有 on_close 这条重复调用路径会走到）
        if ctx.stop_done_evt and ctx.stop_done_evt.is_set():
            set_power_state(ctx, "idle")
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("已停止"))
        return

    # 收尾期间禁掉「开始翻译」：旧引擎还在关麦克风/虚拟声卡，立刻重启会抢设备
    set_power_state(ctx, "stopping")
    if ctx.set_status_fn:
        ctx.set_status_fn("info", t("正在停止…"))

    if ctx.stop_done_evt:
        ctx.stop_done_evt.clear()
    threading.Thread(target=wait_stop_done, args=(ctx, engines), daemon=True,
                     name="vlt-stop-wait").start()

def wait_stop_done(ctx: EngineCtx, engines: list) -> None:
    """（**后台线程**）等引擎真正收尾完，再入队让 ``_poll`` 恢复界面。绝不碰 Tk。"""
    t0 = time.monotonic()
    stuck: list[int] = []
    for i, eng in enumerate(engines):
        try:
            if not eng.wait_stopped(STOP_WAIT_S):
                stuck.append(i)
        except Exception as exc:          # noqa: BLE001
            print(f"[gui] ⚠️ 等引擎收尾出错（忽略）：{type(exc).__name__}: {exc}", flush=True)
    elapsed = time.monotonic() - t0
    if stuck:
        print(f"[gui] ⚠️ 停止收尾超时（{STOP_WAIT_S:.0f}s）：第 {stuck} 个引擎还没退出"
              "（仍在关麦克风/虚拟声卡；界面照常恢复，状态栏会如实提示仍在收尾）",
              flush=True)
    ctx.q.put(("stop_done", elapsed, len(engines), bool(stuck)))
    if ctx.stop_done_evt:
        ctx.stop_done_evt.set()

# ================================================================ 窗口关闭

def on_close(ctx: EngineCtx) -> None:
    """关窗口：先停引擎（**有界** 等采集线程真正退出），再销毁窗口。

    顺序很重要——如果先销毁窗口再去等引擎，主线程会阻塞在一个已经失效的
    Tk 事件循环上，界面看起来就是"卡死后闪退"。
    """
    ctx.closing = True
    # 先取消 poll 循环，防止窗口销毁后 after 回调仍在事件队列里
    if ctx.cancel_poll_fn:
        try:
            ctx.cancel_poll_fn()
        except Exception:                       # noqa: BLE001
            pass
    # 先放掉电平探针占着的采集设备
    if ctx.stop_gate_probe_fn:
        ctx.stop_gate_probe_fn()
    try:
        stop(ctx)
    except Exception as exc:
        print(f"[gui] 停止引擎时出错（继续关闭）：{exc}", file=sys.stderr)
    try:
        if ctx.stop_done_evt and not ctx.stop_done_evt.wait(CLOSE_WAIT_STOP_S):
            print(f"[gui] ⚠️ 退出时等引擎收尾超过 {CLOSE_WAIT_STOP_S:.0f}s，"
                  f"直接关闭（进程退出会释放设备）", flush=True)
    except Exception:
        pass
    try:
        # 「稍后更新」的另一半：正常退出时替换（绝不自动拉起新版）。
        if ctx.maybe_replace_on_exit_fn:
            ctx.maybe_replace_on_exit_fn()
    except Exception as exc:
        print(f"[update] ⚠️ 退出时替换出现异常（继续关闭）：{exc}", file=sys.stderr)
    try:
        if ctx.destroy_root_fn:
            ctx.destroy_root_fn()
        else:
            ctx.root.destroy()
    except Exception:
        pass
    crashlog.close()
