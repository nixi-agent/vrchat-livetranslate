"""分玩家模式的配置：默认值、范围校验、**唯一**一处口径。

⚠️ 放在 `vlt/players/` **外面**：界面（Windows 也编）要读写这份配置，而
`vlt.players` 是 Linux 独占、Windows 产物里被 exclude 掉的模块。这里只有纯数据
与校验，不带任何 /proc、raw socket 或平台依赖。


## 为什么单独一份

以前这些默认值散在 `gui_engine._start_players_leg` 与 `PlayerLeg.__init__` 里，
已经出现过一次不一致（`max_sessions` 非法时回落 3、而默认是 4）。设置页又要
逐项回填这些值 —— 三处各写一份必然继续漂移。

所以：**界面、启动腿、预热器都只认这个 dataclass**。非法值一律回落默认并留痕
（禁静默降级），范围与 `config.example.yaml` 的注释保持同一口径。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

MODE_OFF = "off"
MODE_DECRYPT = "decrypt"
DISTANCE_MODES = ("off", "order", "gate", "both")
#: 翻译名单模式：all = 全都翻；only = 只翻名单里的；except = 除了名单都翻
SELECT_MODES = ("all", "only", "except")

#: 只提示一次的废弃键（`parse_players` 会在启动、开设置页、开始翻译时各调一次，
#: 每次重复刷同一句没有意义）。
_WARNED_ONCE: set[str] = set()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_users(raw: object, warn: Callable[[str], None]) -> tuple[str, ...]:
    """解析 `players.select_users`：一个 user_id 字符串，或字符串列表。

    宽松处理（这份名单是用户手改 yaml 或界面勾选写出来的，不该因为一个空格报错）：
    - `None` / `[]` → 空元组（正常：等于「没勾任何人」）
    - 字符串按其内容整体当一个 id（**不**按逗号切 —— user_id 里没有逗号，
      而"顺手支持逗号"会让 `["usr_a,usr_b"]` 这种写法悄悄变成两个人）
    - 其它可迭代 → 逐项 `str()` + strip + 去空 + 去重（保序）
    - 不以 `usr_` 开头的也接受（VRChat 改过前缀，别因为格式猜错把人整没了），但留痕
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        items: list[object] = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        warn(f"players.select_users 应该是字符串或列表（{type(raw).__name__}）→ 忽略")
        return ()

    seen: set[str] = set()
    users: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, (dict, list, tuple, set)):
            warn(f"players.select_users 里有非标量项 {item!r} → 跳过")
            continue
        user = str(item).strip()
        if not user or user in seen:
            continue
        if not user.startswith("usr_"):
            warn(f"players.select_users 里 {user!r} 不像 user_id（应为 usr_ 开头）→ 仍然保留")
        seen.add(user)
        users.append(user)
    return tuple(users)


@dataclass(frozen=True)
class PlayersSettings:
    """`players:` 段的全部配置（默认值即产品默认）。

    字段顺序即设置页里的显示顺序（基本 → 音量 → 距离 → 高级）。
    """

    mode: str = MODE_OFF                 # off | decrypt（开 decrypt 即自动预热，没有单独开关）
    max_sessions: int = 4                # 同时翻译几个人（1..8）
    gate_db: float = -55.0               # 每人流的静音门限（RMS dBFS，-70..-30）
    gate_hysteresis_db: float = 6.0      # 开闸后要再低这么多才关闸（0..12）
    distance_mode: str = "off"           # off | order | gate | both
    max_distance_m: float = 0.0          # 距离门限（米；0 = 不限）
    pre_seconds: float = 120.0           # 等加密流量的最长秒数（10..300）
    prewarm_buffer_mb: int = 16          # 预热期保留多少密文（4..64）
    select_mode: str = "all"             # all | only | except（谁要翻译）
    select_users: tuple[str, ...] = ()   # 名单（user_id，形如 usr_xxx）

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_players(raw: object, *, log: Callable[[str], None] | None = None) -> PlayersSettings:
    """把 `config.players`（原始 dict）解析成 `PlayersSettings`。

    非法值**回落默认并留痕**（`log` 默认 print）—— 面板上填错不该静默变成别的行为。
    """
    def warn(message: str) -> None:
        (log or print)(f"[players] ⚠️ {message}")

    defaults = PlayersSettings()
    if not isinstance(raw, dict):
        if raw not in (None, {}):
            warn(f"players 段不是字典（{type(raw).__name__}）→ 用默认值")
        return defaults

    mode = str(raw.get("mode") or MODE_OFF).lower().strip()
    if mode not in (MODE_OFF, MODE_DECRYPT):
        warn(f"players.mode={mode!r} 未知（off/decrypt）→ 按 off")
        mode = MODE_OFF

    if "prewarm" in raw and "prewarm" not in _WARNED_ONCE:
        # 预热已经是分玩家的前提（开 decrypt 就自动预热），旧键忽略即可。
        _WARNED_ONCE.add("prewarm")
        warn("players.prewarm 已废弃：分玩家必须预热 → 忽略该键")

    def number(key: str, default: float, low: float, high: float) -> float:
        value = raw.get(key, default)
        try:
            number_value = float(value)
        except (TypeError, ValueError):
            warn(f"players.{key}={value!r} 不是数字 → 用默认 {default:g}")
            return default
        if not (low <= number_value <= high):
            warn(f"players.{key}={number_value:g} 超出 [{low:g}, {high:g}] → "
                 f"夹到边界（原值 {value!r}）")
            return _clamp(number_value, low, high)
        return number_value

    def integer(key: str, default: int, low: int, high: int) -> int:
        value = raw.get(key, default)
        try:
            as_float = float(value)
        except (TypeError, ValueError):
            warn(f"players.{key}={value!r} 不是整数 → 用默认 {default}")
            return default
        if as_float != int(as_float):
            warn(f"players.{key}={value!r} 不是整数 → 用默认 {default}")
            return default
        as_int = int(as_float)
        if not (low <= as_int <= high):
            warn(f"players.{key}={as_int} 超出 [{low}, {high}] → 夹到边界（原值 {value!r}）")
            return max(low, min(high, as_int))
        return as_int

    distance_mode = str(raw.get("distance_mode") or "off").lower().strip()
    if distance_mode not in DISTANCE_MODES:
        warn(f"players.distance_mode={distance_mode!r} 未知（{'/'.join(DISTANCE_MODES)}）→ off")
        distance_mode = "off"

    max_distance_m = number("max_distance_m", 0.0, 0.0, 30.0)
    if "gate" in distance_mode and max_distance_m <= 0:
        warn("distance_mode 含 gate 但 max_distance_m<=0 → gate 不生效")
        distance_mode = distance_mode.replace("gate", "").strip() or "off"

    select_mode = str(raw.get("select_mode") or "all").lower().strip()
    if select_mode not in SELECT_MODES:
        warn(f"players.select_mode={select_mode!r} 未知（{'/'.join(SELECT_MODES)}）→ all")
        select_mode = "all"
    select_users = _parse_users(raw.get("select_users"), warn)
    if select_mode != "all" and not select_users:
        # 不是错误：`only` + 空名单 = 谁都不翻（右栏全不勾也走这条），`except` = 照旧全翻。
        pass
    elif select_mode == "all" and select_users:
        warn("players.select_mode=all 但有 select_users → 名单不生效（全翻）")

    return PlayersSettings(
        mode=mode,
        max_sessions=integer("max_sessions", defaults.max_sessions, 1, 8),
        gate_db=number("gate_db", defaults.gate_db, -70.0, -30.0),
        gate_hysteresis_db=number("gate_hysteresis_db", defaults.gate_hysteresis_db, 0.0, 12.0),
        distance_mode=distance_mode,
        max_distance_m=max_distance_m,
        pre_seconds=number("pre_seconds", defaults.pre_seconds, 10.0, 300.0),
        prewarm_buffer_mb=integer("prewarm_buffer_mb", defaults.prewarm_buffer_mb, 4, 64),
        select_mode=select_mode,
        select_users=select_users,
    )
