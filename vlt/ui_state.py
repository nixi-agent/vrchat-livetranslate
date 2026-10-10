"""界面状态推导的**纯逻辑**：线路/密钥槽取值、key 重解析、房间状态文案与落盘。

为什么放在这里（而不是留在 gui.py）：
  · 这些都是「输入是 cfg / 文本 / 房间对象，输出是字符串或写盘」的纯逻辑，
    不依赖任何 Tk 实例（控件、变量、窗口）—— gui.py 里只剩一行接线的薄方法；
  · 抽出来后可以离线单测（不 import tkinter，也不 import vlt.gui，防循环引用）。

gui.py 里对应的 `TranslationGUI._provider` / `_refresh_api_key_in_cfg` /
`_room_status_text` / `_save_room_cfg` 都改成调用本模块的薄包装 —— **对外语义不变**。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import endpoints
from .config_io import _fmt_scalar, _write_config_text, _yaml_set_in_text
from .i18n import t
from .room.model import ConnectionState, RoomConfig
from .ui_text import _ROOM_SECTION_TEMPLATE, _yaml_quote


def provider(cfg) -> str:
    """当前线路 id（脏值由 endpoints 归一化 + 留痕，调用方绝不自己判第二遍）。"""
    return endpoints.normalize_provider(cfg.session_base.get("provider"))


def provider_label(cfg) -> str:
    """当前线路的**界面显示名**（按界面语言取词）。给用户看的文案一律走这里，
    日志才用 `endpoints.provider_name()`（那份恒为中文）。"""
    from .ui_text import _provider_choices
    for name, pid in _provider_choices():
        if pid == provider(cfg):
            return name
    return t("千问云")          # 走不到：provider() 已归一化成合法 id


def current_key_slot(cfg) -> str:
    """当前线路的密钥槽名。千问云与千问云·海外版的 key 不通用 → 分槽各存一份，切线路不用重填。"""
    return endpoints.key_slot(provider(cfg))


def refresh_api_key_in_cfg(cfg) -> None:
    """按既有优先级链重新解析 API key 并写回 `cfg.session_base["api_key"]`。

    启动时 load_config() 解析出的 key 只是那一刻的快照；界面上保存/清除之后
    必须重解，否则「开始翻译」读的还是启动时那份（干净机器：保存了却报"还没配置"；
    有旧来源的机器：贴了新 key 却继续用旧的）。解析只走 config.load_api_key()，
    不自写第二套优先级；一个来源都没有时它会抛 SystemExit —— 这里置空串，
    绝不让异常冒到界面/主循环。留痕：来源 + 打码值，绝不打明文。
    """
    if provider(cfg) == endpoints.PROVIDER_CHATGPT:
        cfg.session_base["api_key"] = ""
        return
    from .config import load_api_key
    from .credentials import key_source, mask_key

    slot = current_key_slot(cfg)
    try:
        key = load_api_key(slot=slot)
    except SystemExit:
        key = ""
    cfg.session_base["api_key"] = key
    if key:
        source, _ = key_source(slot=slot)
        print(f"[gui] API key 已刷新：线路={endpoints.provider_name(provider(cfg))} "
              f"来源={source}（{mask_key(key)}）", flush=True)
    else:
        print(f"[gui] API key 已刷新：线路="
              f"{endpoints.provider_name(provider(cfg))} 没有任何来源（尚未配置）",
              flush=True)


def room_status_text(room) -> str:
    """房间行右侧的状态文案：连接态 + 在线人数，全部走 t()（界面禁技术词）。"""
    if room is None:
        return t("状态：{state} · {n} 人", state=t("未连接"), n=0)
    try:
        st = room.state()
    except Exception:                           # noqa: BLE001  取快照失败就退回「未连接」
        return t("状态：{state} · {n} 人", state=t("未连接"), n=0)
    state_zh = {
        ConnectionState.IDLE: "未连接",
        ConnectionState.CONNECTING: "连接中",
        ConnectionState.ONLINE: "已连接",
        ConnectionState.RECONNECTING: "重连中",
        ConnectionState.STOPPED: "已停止",
        ConnectionState.ERROR: "错误",
    }.get(st.conn, "未连接")
    return t("状态：{state} · {n} 人", state=t(state_zh), n=st.peer_count)


def room_cfg_from_text(text: str, fallback: RoomConfig) -> RoomConfig:
    """从配置**文本**里读 `room:` 段成 RoomConfig（补建段之后立刻回读用）。

    解析失败就沿用内存里的现有设置（宁可用旧设置，也别把用户刚填的选项清掉）。
    """
    try:
        raw = (yaml.safe_load(text) or {}).get("room")
    except Exception as exc:                    # noqa: BLE001
        print(f"[gui] 房间段回读失败（沿用内存里的设置）：{exc}", flush=True)
        return fallback
    if not isinstance(raw, dict):
        return fallback
    return RoomConfig.from_dict(raw)


def save_room_cfg(p: Path, room_cfg: RoomConfig) -> RoomConfig:
    """把房间三项（enabled/room_code/nickname）就地写回 config.yaml 的 room 段，
    返回写盘后（可能被补建段回读刷新过的）RoomConfig。

    ⚠️ room 段不存在时**补建**：老用户的 config.yaml（旧模板生成）没有这个段，
    而就地改文本「找不到路径就原样返回」→ 不补建就表现为静默不保存。
    """
    if not p.exists():
        return room_cfg
    try:
        text = p.read_text(encoding="utf-8")
        if not re.search(r"^room:", text, re.M):
            text = text.rstrip("\n") + "\n\n" + _ROOM_SECTION_TEMPLATE
        text = _yaml_set_in_text(text, ["room", "enabled"],
                                 _fmt_scalar(bool(room_cfg.enabled)))
        text = _yaml_set_in_text(text, ["room", "room_code"],
                                 _yaml_quote(room_cfg.room_code))
        text = _yaml_set_in_text(text, ["room", "nickname"],
                                 _yaml_quote(room_cfg.nickname))
        _write_config_text(p, text)
        # ⚠️ 上面补建的只是**文件**。内存里的 `room_cfg` 还是「配置里根本没有 room 段」
        # 时的默认值（`server_url` 为空）→ 紧接着勾选启用时，`RoomClient` 的启动校验会直接拒：
        #     [room] ❌ 房间链路没启动：没填 server_url（config.yaml 的 room.server_url）
        # 而用户打开 config.yaml 一看，明明有 —— 于是表现为「勾了房间没反应，
        # 重启一次才好」。所以写完把新段回读回来，让**本轮**勾选就能连上。
        room_cfg = room_cfg_from_text(text, room_cfg)
        print(f"[gui] 房间设置已保存：enabled={_fmt_scalar(bool(room_cfg.enabled))} "
              f"room_code={room_cfg.room_code!r} nickname={room_cfg.nickname!r}",
              flush=True)
    except Exception as exc:                    # noqa: BLE001  存盘失败只留痕，不影响使用
        print(f"[gui] 保存房间设置失败：{exc}", flush=True)
    return room_cfg
