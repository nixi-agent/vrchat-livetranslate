"""配置加载：YAML + 环境变量。API key 不落配置文件，只从环境变量或 bl CLI 的配置读。"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import endpoints
from .session.base import (SessionConfig, DEFAULT_FINAL_SILENCE_S,
                           DEFAULT_FAST_FINAL_SILENCE_S, DEFAULT_FAST_FINAL_USER_QUIET_S)

from .paths import APP_DIR, BUNDLE_DIR

ROOT = APP_DIR                                    # 保留别名，兼容既有引用
DEFAULT_CONFIG = APP_DIR / "config.yaml"           # 可写：用户配置
# 入库的是模板；config.yaml 是用户自己的配置（设备名/语言偏好），已被 gitignore。
EXAMPLE_CONFIG = BUNDLE_DIR / "config.example.yaml"  # 只读：随程序分发的模板


def _as_str_map(raw: Any, what: str) -> dict[str, str]:
    """把配置里的「映射表」（专有词库 / 方向级热词）规范成 `dict[str, str]`。

    配置是手写的，写错形状的概率不为零：写成列表、写成标量、或者哪条只有键没有值。
    这些都必须**留痕后丢掉**，绝不能原样下发给服务端 —— 服务端收到非映射的 phrases
    会整条会话被拒（`session.update` 是整体校验，坏一个字段就全军覆没），
    而用户看到的只是「翻译不工作」，根本联想不到是自己那行 YAML 写错了。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        print(f"[config] ⚠️ {what} 不是映射表（读到 {type(raw).__name__}），已忽略该段；"
              f"正确写法：\n{what}:\n  \"原文\": \"译名\"", flush=True)
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key, val = str(k or "").strip(), str(v or "").strip()
        if key and val:
            out[key] = val
        else:
            print(f"[config] ⚠️ {what} 里有一条空条目（键或值为空），已跳过：{k!r} → {v!r}",
                  flush=True)
    return out


def ensure_config(path: Path | None = None) -> Path:
    """确保配置文件存在：不存在就从 config.example.yaml 复制一份。

    这样新克隆的仓库（以及给朋友用的时候）开箱即用，而用户的实际配置
    不会进版本库——设备名这类东西因机器而异，跟着仓库走只会互相污染。
    """
    p = Path(path) if path else DEFAULT_CONFIG
    if p.exists() or not EXAMPLE_CONFIG.exists():
        return p
    try:
        p.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[config] 已从 {EXAMPLE_CONFIG.name} 生成 {p.name}")
    except OSError as exc:
        print(f"[config] 生成 {p.name} 失败（将使用内置默认值）：{exc}")
    return p


def load_api_key(explicit: str | None = None, slot: str = "qianwen") -> str:
    """API key 读取顺序：显式参数 → 界面保存的（按 slot 分槽）→ 环境变量 → ~/.bailian/config.json。

    界面保存的排第二（仅次于显式传参）：用户在界面上填了 key，就是最明确的意图，
    不该被环境变量或 CLI 配置盖掉。来源会打一行日志（**只打码、绝不打明文**），
    否则"为什么连的还是旧 key"根本查不出来。

    `slot` 是密钥槽名（= 线路 id，见 endpoints.key_slot）：千问云与百炼**各存一份**
    key，切线路不用重填 —— 故界面保存的这一步按 slot 取对应那份。
    ⚠️ 但 `DASHSCOPE_API_KEY` 环境变量与 `~/.bailian/config.json`（bl CLI）这两条兜底
    **两条线路共用**：阿里官方对国际站也用同一个环境变量名 / 同一份 CLI 配置，
    没有「按线路分」的说法，所以这里不按 slot 区分。
    """
    if explicit:
        return explicit.strip()
    try:
        from .credentials import load_saved_key, mask_key

        saved = load_saved_key(slot)
        if saved:
            print(f"[config] API key 来源：界面保存（线路={endpoints.provider_name(slot)}，"
                  f"{mask_key(saved)}）", flush=True)
            return saved
    except Exception as exc:  # noqa: BLE001
        print(f"[config] ⚠️ 读取界面保存的 key 失败，继续走其它来源："
              f"{type(exc).__name__}: {exc}", flush=True)
    # ↓ 环境变量 / bl CLI 两条兜底：两条线路共用（见上方 docstring）
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env:
        return env.strip()
    cfg = Path(os.path.expanduser("~/.bailian/config.json"))
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        for key in ("api_key", "apiKey", "DASHSCOPE_API_KEY"):
            if data.get(key):
                return str(data[key]).strip()
    raise SystemExit("找不到 API key：设 DASHSCOPE_API_KEY，或先跑 `bl auth login --api-key <key>`")


def merge_hotwords(glossary: dict[str, str] | None,
                   hotwords: dict[str, str] | None) -> dict[str, str]:
    """合并「全局专有词库」与「方向级热词」——**全项目唯一的口径**。

    为什么要有这个函数：词库要同时喂给两条腿（实时会话的 `translation.corpus.phrases`
    和打字翻译的 `translation_options.terms`）。若两处各写一遍合并逻辑，迟早会漂移
    （改了一处忘了另一处）——于是「说话时对、打字时不对」这种最难查的 bug 就来了。

    优先级：方向级覆盖全局（同名词条以 `directions.<X>.hotwords` 为准）。
    这样「全局一份常用词库 + 某个方向临时特例」不用把词库复制两遍。
    """
    return {**(glossary or {}), **(hotwords or {})}


@dataclass
class Direction:
    source_lang: str | None = None
    target_lang: str = "en"
    output_audio: bool = False
    hotwords: dict[str, str] = field(default_factory=dict)
    voice: str | None = None

    def to_session_config(self, base: dict[str, Any]) -> SessionConfig:
        return SessionConfig(
            model=base["model"],
            target_lang=self.target_lang,
            source_lang=self.source_lang,
            output_audio=self.output_audio,
            voice=self.voice or base.get("voice") or "Tina",
            # 全局专有词库 + 本方向的覆盖，合并口径只有 merge_hotwords 一处
            hotwords=merge_hotwords(base.get("glossary"), self.hotwords),
            turn_detection=base.get("turn_detection"),
            base_url=base["base_url"],
            # provider 只随会话带出去供日志/诊断；实际地址仍由 base_url 决定。
            provider=base.get("provider", endpoints.DEFAULT_PROVIDER),
            api_key=base["api_key"],
            reconnect_backoff=tuple(base.get("reconnect_backoff", (2, 5, 10, 30))),
            max_new_sessions_per_minute=int(base.get("max_new_sessions_per_minute", 4)),
            final_silence_s=float(base.get("final_silence_s", DEFAULT_FINAL_SILENCE_S)),  # 默认值必须 > 服务端增量间隔（实测最大 2.3s），改小会让最终版在句子中间抢跑
            # 快封句（上游也静了 → 说话的人确实说完了）：文字静默到这个值就封，省 ~1.8s；
            # None = 关掉快路径（退回纯 final_silence_s）
            fast_final_silence_s=_opt_float(
                base.get("fast_final_silence_s", DEFAULT_FAST_FINAL_SILENCE_S),
                DEFAULT_FAST_FINAL_SILENCE_S, key="session.fast_final_silence_s"),
            fast_final_user_quiet_s=_float_or_default(
                base.get("fast_final_user_quiet_s", DEFAULT_FAST_FINAL_USER_QUIET_S),
                DEFAULT_FAST_FINAL_USER_QUIET_S, key="session.fast_final_user_quiet_s"),
        )


@dataclass
class AppConfig:
    session_base: dict[str, Any]
    directions: dict[str, Direction]
    chatbox: dict[str, Any]
    merger: dict[str, Any]
    overlay: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)      # 界面上次的选择（方向/输出勾选），启动时恢复
    text_input: dict[str, Any] = field(default_factory=dict)   # 打字输入（替代说话）
    # 房间中继原始配置段。**故意不在这里做字段级校验**：校验统一走
    # `vlt/room/model.py::RoomConfig.from_dict`（脏值回落 + 留痕都做好了），
    # 这里只负责把原始 dict 带出来，避免出现两份口径。
    room: dict[str, Any] = field(default_factory=dict)
    # 桌面字幕（PC 桌面模式：贴 VRChat 窗口的叠加窗）。与 `overlay`（头显手腕屏）是两条腿，
    # 视觉参数从 `overlay` 段继承，见 vlt/output/desktop_overlay.py 的 from_dict。
    desktop_overlay: dict[str, Any] = field(default_factory=dict)

    def direction(self, name: str) -> Direction:
        if name not in self.directions:
            raise SystemExit(f"配置里没有方向的 key：{name}（现有：{list(self.directions)}）")
        return self.directions[name]

    def merged_hotwords(self, name: str) -> dict[str, str]:
        """某个方向**实际生效**的专有词库（全局 + 方向级覆盖）。

        引擎两条腿都从这里取值：实时会话走 `Direction.to_session_config`（内部调
        `merge_hotwords`），打字翻译走这里 —— 同一个口径，不会一边有一套。
        """
        d = self.directions.get(name)
        return merge_hotwords(self.session_base.get("glossary"), d.hotwords if d else None)


def _opt_float(value, default: float, *, key: str) -> float | None:
    """快封句的「文字静默」阈值：`None`/空串 = **用户明确关掉**（回 `None`）；非法值 → 留痕 + 回落默认值。

    本仓库的配置是手写的，写错一个字母不该让程序起不来（与 `normalize_provider` 同一取舍）；
    「关掉」必须能表达 —— 靠 `fast_final_silence_s: null` 退回纯 `final_silence_s`。
    ⚠️ 但非法值**不许静默**：一个 `1,1` 的笔误会把功能悄悄关掉、日志一个字都没有 ——
    按本仓库既有口径（`engine.silence_gate_settings` / `repeat_guard_settings`）打一行 `[config] ⚠️`。
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        print(f"[config] ⚠️ {key}={value!r} 不是数字 → 回落默认值 {default}"
              f"（要关掉快封句请写 null）", flush=True)
        return default


def _float_or_default(value, default: float, *, key: str) -> float:
    """普通浮点配置：非法值 → 留痕 + 回落默认值（绝不因一个手写笔误让程序起不来）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        print(f"[config] ⚠️ {key}={value!r} 不是数字 → 回落默认值 {default}", flush=True)
        return default


def _int_clamped(value, default: int, *, key: str, lo: int, hi: int) -> int:
    """带范围的整数配置：非法值 → 留痕 + 回落默认值；越界 → 留痕 + 夹到 [lo, hi]。

    与 `_float_or_default` 同一口径（手写笔误不许让程序起不来、也不许静默带病运行）。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        print(f"[config] ⚠️ {key}={value!r} 不是整数 → 回落默认值 {default}", flush=True)
        return default
    if n < lo or n > hi:
        clamped = max(lo, min(hi, n))
        print(f"[config] ⚠️ {key}={n} 超出合理范围 [{lo}, {hi}] → 夹到 {clamped}", flush=True)
        return clamped
    return n


def _float_in_range(value, default: float, *, key: str, lo: float, hi: float,
                    hi_exclusive: bool = False) -> float:
    """带范围的浮点配置：非法 / 越界 → **留痕 + 回落默认值**（不夹到边界）。

    与 `_int_clamped` 的差别是刻意的：那些整数是缓冲毫秒数，夹到边界仍是**可用**值；
    这里管的是 dB —— `max_cut_db: 900` 夹成 36 等于替用户编了一个他从没要的声学设置，
    还不如退回默认值并打一行 `[config] ⚠️`（同一口径：手写笔误不许让程序起不来，
    也绝不许静默带病运行）。`hi_exclusive` 给 `ceiling_dbfs` 用：0 dBFS = 满刻度，
    贴边必削波，所以是**开区间**。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        print(f"[config] ⚠️ {key}={value!r} 不是数字 → 回落默认值 {default:g}", flush=True)
        return default
    if not math.isfinite(f):
        print(f"[config] ⚠️ {key}={value!r} 不是有限数 → 回落默认值 {default:g}", flush=True)
        return default
    ok = (lo <= f < hi) if hi_exclusive else (lo <= f <= hi)
    if not ok:
        close = ")" if hi_exclusive else "]"
        print(f"[config] ⚠️ {key}={f:g} 超出合理范围 [{lo:g}, {hi:g}{close} → "
              f"回落默认值 {default:g}", flush=True)
        return default
    return f


def _str_choice(value, default: str, *, key: str, choices: tuple[str, ...]) -> str:
    """枚举型字符串配置：不在白名单 → 留痕 + 回落默认值（大小写/空格宽容）。

    ⚠️ 先过一道 YAML 1.1 的坑：PyYAML 把**裸词** `off` / `on` / `no` / `yes` 解析成
    布尔值，而 `output.audio.level.mode` 的默认值在 config.example.yaml 里就写作
    `mode: off` —— 不还原成词，用户照模板写的**正确**配置反被判非法、还刷一条告警。
    """
    if isinstance(value, bool):
        value = "on" if value else "off"
    if isinstance(value, str) and value.strip() in choices:
        return value.strip()
    print(f"[config] ⚠️ {key}={value!r} 不是 {'/'.join(choices)} 之一 → "
          f"回落默认值 {default!r}", flush=True)
    return default


def _resolve_api_key(api_key: str | None, require_key: bool, slot: str = "qianwen") -> str:
    """取 key；`require_key=False` 时"还没有 key"不抛错，而是返回空串。

    界面用得上：启动时**不能**因为没填 key 就起不来 —— 那样用户连"去哪填 key"
    的入口都看不到（首次使用、或换台电脑给朋友用，就是死局）。
    调用方（`_start`）会检查空 key 并给出明确指引。

    `slot` 透传给 `load_api_key`（按线路分槽取界面保存的那份）；日志里带上线路名，
    免得两条线路各存一份 key 时"哪条线路没配"看不出来。
    """
    try:
        return load_api_key(api_key, slot=slot)
    except SystemExit:
        if require_key:
            raise
        print(f"[config] ⚠️ 尚未配置 API key（线路={endpoints.provider_name(slot)}）—— 界面照常启动；"
              "开始翻译前请点主界面右上角的 API key 入口填一个", file=sys.stderr, flush=True)
        return ""


def _warn_provider_host_mismatch(provider: str, base_url: str) -> None:
    """provider 与 base_url 的 host 对不上时打一行 WARN（**不报错**，地址仍以 base_url 为准）。

    为什么会不一致：切换线路本该由界面同步改写 base_url，但用户可能只手改了
    `provider:` 忘了改 base_url（或反过来）。这时**绝不**自作主张改地址 —— base_url 是
    唯一真相源；只留一行痕，告诉用户「以 base_url 为准，去哪切」。host 解析失败也只留痕。
    """
    try:
        host = endpoints.host_of(base_url).lower()
    except ValueError as exc:
        print(f"[config] ⚠️ 无法从 session.base_url 解析 host（{exc}）；地址仍以 base_url 为准",
              flush=True)
        return
    if provider == endpoints.PROVIDER_QWENCLOUD and host.endswith("qianwenaiapi.com"):
        print("[config] ⚠️ 线路=千问云·海外版，但 session.base_url 还是国内千问云的域名 "
              "—— 地址以 base_url 为准；要切线路请在界面「设置 → 常规」里切换", flush=True)
    elif provider == endpoints.PROVIDER_QIANWEN and host.endswith("qwencloudapi.com"):
        print("[config] ⚠️ 线路=千问云，但 session.base_url 指向千问云·海外版的域名 "
              "—— 地址以 base_url 为准；要切线路请在界面「设置 → 常规」里切换", flush=True)


def load_config(path: str | Path | None = None, api_key: str | None = None,
                require_key: bool = True) -> AppConfig:
    p = ensure_config(Path(path) if path else None)   # 缺文件时从 config.example.yaml 生成
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            # 配置文件被写坏了（实测：残留的孤立序列项会让整个文件非法，
            # 比如 `pos: [...]` 下面还留着旧版的 `- 0.0`）→ 备份 + 从模板重建，
            # 否则程序下次连启动都起不来。**绝不静默**：打印清楚，并保留坏文件供排查。
            broken = p.with_name(p.name + ".broken")
            try:
                shutil.copyfile(p, broken)
            except OSError as exc2:  # noqa: BLE001
                broken = None
                print(f"[config] 备份损坏的配置也失败了：{exc2}", file=sys.stderr)
            print(f"[config] ❌ 配置文件解析失败：{exc}\n"
                  f"[config]    （这是「配置被写坏」的表现，不是你的错）\n"
                  f"[config]    已备份到 {broken}，并从 {EXAMPLE_CONFIG.name} 重新生成默认配置 ——"
                  f"设备/语言等选择需要重设一次",
                  file=sys.stderr)
            if EXAMPLE_CONFIG.exists():
                shutil.copyfile(EXAMPLE_CONFIG, p)
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    s = raw.get("session", {}) or {}
    # 线路：区分「键缺失」与「填了但填错」两种情况 ——
    #   · 键缺失（老配置）→ 用 get 的默认值兜进去，normalize 收到的是合法默认，**不留痕**：
    #     老 config.yaml 全都没有这键（且被 gitignore、升级时不会自动补），若把 None 直接
    #     喂给 normalize，会让每个老用户**每次启动**都看到一行「未识别的服务线路 None」的
    #     噪声 —— 那既不是他填的值、也违背「老配置行为零变化」（设计口径 §1.5）。
    #   · 填了非法值（如 provider: nope）→ 原样送 normalize，照旧留痕 + 回落（§1.4：不静默）。
    #   · 填了**已下线**的 bailian_intl → normalize 留痕后改按 qwencloud 处理（见下）。
    provider = endpoints.normalize_provider(s.get("provider", endpoints.DEFAULT_PROVIDER))
    # base_url 默认值也走 endpoints 派生（值不变，只是不再写第二份域名字面量 = 单一真相源）。
    base_url = s.get("base_url") or endpoints.default_base_url(endpoints.PROVIDER_QIANWEN)
    # 老配置迁移：阿里云百炼·国际版那条线路（要业务空间 ID + 地域、海外注册还要过风控）已下线，
    # 统一改走千问云·海外版。**响亮留痕**：地址换了、key 槽也换了，用户必须知道要重填 key。
    if endpoints.is_retired_base_url(base_url):
        base_url = endpoints.default_base_url(endpoints.PROVIDER_QWENCLOUD)
        provider = endpoints.PROVIDER_QWENCLOUD
        # ⚠️ 不回显旧地址：百炼的 host 首段就是**业务空间 ID**（账号标识），
        #    打进日志等于把它写进用户磁盘上的日志文件。
        print("[config] ⚠️ session.base_url 指向已下线的阿里云百炼·国际版线路，"
              "已改用千问云·海外版（host=maas.qwencloudapi.com）\n"
              "[config]    该线路与国内版 API key **不互通**，请在「设置 → 常规」里"
              "用海外版账号（https://www.qwencloud.com/）重新填一次 key", flush=True)
    # provider 与 base_url 的 host 对不上 → 打一行 WARN（不报错，地址仍以 base_url 为准）。
    _warn_provider_host_mismatch(provider, base_url)
    session_base = {
        "model": s.get("model", "qwen3.8-livetranslate-flash-realtime"),
        "base_url": base_url,
        "provider": provider,
        "voice": s.get("voice", "Tina"),
        "turn_detection": s.get("turn_detection"),
        "reconnect_backoff": s.get("reconnect_backoff", [2, 5, 10, 30]),
        "max_new_sessions_per_minute": s.get("max_new_sessions_per_minute", 4),
        "final_silence_s": float(s.get("final_silence_s", DEFAULT_FINAL_SILENCE_S)),  # 默认值必须 > 服务端增量间隔（实测最大 2.3s），改小会让最终版在句子中间抢跑
        # 快封句：上游也静了 → 文字静默 fast_final_silence_s 就封（省 ~1.8s）；None = 关掉
        "fast_final_silence_s": _opt_float(
            s.get("fast_final_silence_s", DEFAULT_FAST_FINAL_SILENCE_S),
            DEFAULT_FAST_FINAL_SILENCE_S, key="session.fast_final_silence_s"),
        "fast_final_user_quiet_s": _float_or_default(
            s.get("fast_final_user_quiet_s", DEFAULT_FAST_FINAL_USER_QUIET_S),
            DEFAULT_FAST_FINAL_USER_QUIET_S, key="session.fast_final_user_quiet_s"),
        # 长静音闸门 + 本地 repeat 抑制：原样透传，取值校验在 engine 里做
        # （非法值会**留痕**并回落默认值，见 engine.silence_gate_settings / repeat_guard_settings）
        "silence_gate_enabled": s.get("silence_gate_enabled", True),
        "silence_gate_after_s": s.get("silence_gate_after_s", 30),
        "silence_gate_preroll_s": s.get("silence_gate_preroll_s", 1.0),
        "repeat_guard_enabled": s.get("repeat_guard_enabled", True),
        "repeat_guard_hits": s.get("repeat_guard_hits", 3),
        "repeat_guard_ratio": s.get("repeat_guard_ratio", 0.9),
        # 全局专有词库放在这个公共底座里：`to_session_config` 只有这一个入参，
        # 而词库对两条腿（实时会话 / 打字翻译）是同一份 —— 放在这里两处都拿得到。
        "glossary": _as_str_map(raw.get("glossary"), "glossary（专有词库）"),
        # 密钥按线路分槽：千问云与百炼各存一份（切线路不用重填）。slot = 线路 id。
        "api_key": _resolve_api_key(api_key, require_key, slot=endpoints.key_slot(provider)),
    }
    directions = {}
    for name, d in (raw.get("directions") or {}).items():
        directions[name] = Direction(
            source_lang=(d or {}).get("source_lang"),
            target_lang=(d or {}).get("target_lang", "en"),
            output_audio=bool((d or {}).get("output_audio", False)),
            hotwords=_as_str_map((d or {}).get("hotwords"), f"directions.{name}.hotwords（方向级热词）"),
            voice=(d or {}).get("voice"),
        )
    if not directions:
        directions = {"mine": Direction(source_lang="zh", target_lang="en")}
    raw_output = raw.get("output") or {}
    raw_audio = raw_output.get("audio") or {}
    raw_proxy = raw_audio.get("proxy") or {}
    if not isinstance(raw_proxy, dict):
        raw_proxy = {}
    raw_level = raw_audio.get("level") or {}
    if not isinstance(raw_level, dict):
        raw_level = {}
    raw_capture = raw.get("capture") or {}
    raw_textin = raw.get("text_input") or {}
    # room 段原样带出（脏值交给 RoomConfig.from_dict 回落 + 留痕）；
    # 用户把 `room:` 写成一行字符串之类时这里只保证类型是 dict，不做字段级校验。
    raw_room = raw.get("room") or {}
    if not isinstance(raw_room, dict):
        raw_room = {}
    output = {
        "audio": {
            "enabled": bool(raw_audio.get("enabled", False)),
            "device": raw_audio.get("device") or ["voicemeeter input", "voicemeeter aux input", "cable input", "vb-audio"],
            "device_name": str(raw_audio.get("device_name") or ""),
            "sample_rate": int(raw_audio.get("sample_rate", 48000)),
            "buffer_ms": int(raw_audio.get("buffer_ms", 300)),
            "max_buffer_ms": int(raw_audio.get("max_buffer_ms", 2000)),
            # 麦克风代理（两端都支持）：常驻把麦克风直通虚拟声卡，界面一键切「原声/译音」。
            # ⚠️ passthrough_buffer_ms 的下限不能太小：麦克风输入块约 100ms，缓冲帽小于一个
            #    输入块会把每块削掉大半 → 严重断续（见 vlt/output/micproxy.py 模块头说明）。
            "proxy": {
                "enabled": bool(raw_proxy.get("enabled", True)),
                "passthrough_buffer_ms": _int_clamped(
                    raw_proxy.get("passthrough_buffer_ms", 150), 150,
                    key="output.audio.proxy.passthrough_buffer_ms", lo=60, hi=500),
                # 「首次启用说明已弹过」标记：程序自己维护（见 gui_proxy_hint），用户不用管。
                # 放在配置里而不是内存/状态文件：用户换机拷配置时不该再被弹一次。
                "hint_shown": bool(raw_proxy.get("hint_shown", False)),
            },
            # 译音音量（对齐麦克风）：治「译音比原声响/轻」。原声直通与译音**共用同一条**
            # 虚拟声卡输出流，VRChat 的麦克风音量只能整体调 → 两条腿的相对失配只能在程序内修。
            # ⚠️ 默认值与合法区间必须与 `vlt/output/level_match.py` 的 `DEFAULT_*` / `RANGES`
            #    一致（这里**不 import 它**：config.py 在导入图里位于 output 包之下，与
            #    「不 import engine」是同一条纪律，见上面 capture.gate_* 的注释）。
            "level": {
                "mode": _str_choice(
                    raw_level.get("mode", "off"), "off",
                    key="output.audio.level.mode",
                    choices=("off", "fixed", "follow_mic")),
                "fixed_gain_db": _float_in_range(
                    raw_level.get("fixed_gain_db", 0.0), 0.0,
                    key="output.audio.level.fixed_gain_db", lo=-36.0, hi=12.0),
                "offset_db": _float_in_range(
                    raw_level.get("offset_db", 0.0), 0.0,
                    key="output.audio.level.offset_db", lo=-24.0, hi=24.0),
                "max_boost_db": _float_in_range(
                    raw_level.get("max_boost_db", 6.0), 6.0,
                    key="output.audio.level.max_boost_db", lo=0.0, hi=24.0),
                "max_cut_db": _float_in_range(
                    raw_level.get("max_cut_db", 24.0), 24.0,
                    key="output.audio.level.max_cut_db", lo=0.0, hi=36.0),
                # 峰值天花板：0 dBFS = 满刻度，贴边必削波 → 上界是**开区间**。
                "ceiling_dbfs": _float_in_range(
                    raw_level.get("ceiling_dbfs", -1.0), -1.0,
                    key="output.audio.level.ceiling_dbfs", lo=-6.0, hi=0.0,
                    hi_exclusive=True),
                "max_step_db": _float_in_range(
                    raw_level.get("max_step_db", 3.0), 3.0,
                    key="output.audio.level.max_step_db", lo=0.5, hi=12.0),
                "mic_min_blocks": _int_clamped(
                    raw_level.get("mic_min_blocks", 8), 8,
                    key="output.audio.level.mic_min_blocks", lo=1, hi=100),
                "mic_window_s": _float_in_range(
                    raw_level.get("mic_window_s", 120.0), 120.0,
                    key="output.audio.level.mic_window_s", lo=5.0, hi=600.0),
            },
        },
        "capture": {
            "mic_device": str(raw_capture.get("mic_device") or ""),
            "loopback_device": str(raw_capture.get("loopback_device") or ""),
            # 输入门限（只作用于 loopback = VRChat 输出「别人说话」那条腿）：
            # 原样透传，取值校验在 engine.input_gate_settings 里做（非法值留痕 + 回落默认值）。
            # ⚠️ 默认值必须与 engine 的 INPUT_GATE_DEFAULT_* 一致（这里不能 import engine：
            #    engine 反向 import 本模块，会成环）。
            "gate_enabled": raw_capture.get("gate_enabled", True),
            "gate_db": raw_capture.get("gate_db", -45.0),
            "gate_hold_ms": raw_capture.get("gate_hold_ms", 500),
            "gate_preroll_ms": raw_capture.get("gate_preroll_ms", 250),
        },
    }
    return AppConfig(
        session_base=session_base,
        directions=directions,
        chatbox=raw.get("chatbox") or {},
        merger=raw.get("merger") or {},
        overlay=raw.get("overlay") or {},
        output=output,
        ui=raw.get("ui") or {},
        room=raw_room,
        desktop_overlay=raw.get("desktop_overlay") or {},
        text_input={
            "enabled": bool(raw_textin.get("enabled", True)),
            # 默认 qwen-mt-flash：实测 qwen3-livetranslate-flash 的**文本**接口会原样回吐
            # （中文进中文出，换个句子又正常），不能依赖；mt 系列稳定且能自动识别源语言。
            "model": str(raw_textin.get("model") or "qwen-mt-flash"),
            "timeout_s": float(raw_textin.get("timeout_s", 20.0)),
            # 打字也要出声：文本翻译不回音频，这一步单独用 TTS 合成后喂虚拟声卡
            "tts": {
                "enabled": bool((raw_textin.get("tts") or {}).get("enabled", True)),
                "model": str((raw_textin.get("tts") or {}).get("model") or "qwen3-tts-flash"),
                "voice": str((raw_textin.get("tts") or {}).get("voice") or "Cherry"),
                # 流式合成（SSE）：首段音频 0.36~0.42s 就能起播（整段合成要等 1.6~1.9s 才开口）
                "stream": bool((raw_textin.get("tts") or {}).get("stream", True)),
                "timeout_s": float((raw_textin.get("tts") or {}).get("timeout_s", 30.0)),
                "reuse_conn": bool((raw_textin.get("tts") or {}).get("reuse_conn", True)),
            },
        },
    )
