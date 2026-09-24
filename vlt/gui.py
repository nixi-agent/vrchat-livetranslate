"""Tkinter 图形界面：聊天气泡视图 + 开关 + 语言镜像 + 双向同时。

用法：
  python -m vlt.gui                    # 启动界面
  python -m vlt.gui --self-test        # 自动化验收（单方向，不起窗口）
  python -m vlt.gui --self-test-dual   # 双向同时验收（两个 PCM 驱动两个引擎）
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import yaml

from .config import Direction, DEFAULT_CONFIG, load_config
from .engine import Engine, EngineEvents

ROOT = Path(__file__).resolve().parent.parent

FONT = ("Microsoft YaHei UI", 11)          # 译文（主）
FONT_SMALL = ("Microsoft YaHei UI", 9)     # 原文（辅，小一号）
FONT_META = ("Microsoft YaHei UI", 8)
MAX_BUBBLES = 500
BG = "#14161c"
COLOR_MINE = "#2f6fd0"
COLOR_THEIRS = "#33363f"
COLOR_TEXT = "#ffffff"
COLOR_META = "#6f7480"
# 原文小字的颜色：比译文暗一档但仍清晰可读（按气泡底色分别取，保证对比度）
COLOR_SRC_MINE = "#c3d4ee"
COLOR_SRC_THEIRS = "#9aa1ad"

SOURCE_LANGS = {
    "自动检测": None,
    "中文": "zh",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
}

TARGET_LANGS = {
    "中文": "zh",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
}


def _source_name(code: str | None) -> str:
    return next((k for k, v in SOURCE_LANGS.items() if v == code), "自动检测")


def _target_name(code: str) -> str:
    return next((k for k, v in TARGET_LANGS.items() if v == code), "英语")


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """圆角矩形：polygon + smooth=True 才有圆角。"""
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
           x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


@dataclass
class _Bubble:
    """一条聊天气泡。items = 这条气泡占用的 Canvas 图元（就地重画时整体删掉）。"""
    who: str
    source: str
    text: str
    ts: str
    final: bool = False
    y: int = 0
    h: int = 0
    items: list = field(default_factory=list)


class TranslationGUI:
    """主界面。headless=True 时不创建 Tk 窗口（给 --self-test / --self-test-dual 用）。"""

    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._q: queue.Queue = queue.Queue()
        self._engines: list[Engine] = []
        self._engine_dirs: list[str] = []      # 与 _engines 一一对应
        self._specs: list[tuple] = []
        self._sinks: set[str] = set()
        self._pending_starts = 0
        self._start_job: str | None = None
        self._bubbles: list[_Bubble] = []
        self._current: dict[str, _Bubble] = {}  # 每个方向一条正在流式刷新的气泡
        self._auto_scroll = True
        self._stats: dict = {}
        self._canvas_w = 600
        self._relayout_job: str | None = None
        self._last_status_level = ""

        self._cfg = load_config()
        mine = self._cfg.directions.get("mine")
        # 语言对：我的语言 A ↔ 对方语言 B。别人说方向自动镜像（B → A）。
        self._lang_pair = {
            "source": mine.source_lang if mine else "zh",
            "target": (mine.target_lang if mine else "en") or "en",
        }

        if not headless:
            self._build_ui()

    # ================================================================ UI 构建

    def _build_ui(self) -> None:
        self._root = tk.Tk()
        self._root.title("VRChat 实时同传")
        self._root.geometry("920x620")
        self._root.minsize(700, 400)

        self._build_controls()
        self._build_chat()
        self._build_status()

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_direction_langs()
        self._check_api_key()
        self._poll()

    def _build_controls(self) -> None:
        ctrl = ttk.Frame(self._root, padding=6)
        ctrl.pack(fill=tk.X)

        self._start_btn = ttk.Button(ctrl, text="开始翻译", command=self._start)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._stop_btn = ttk.Button(ctrl, text="停止翻译", command=self._stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(ctrl, text="方向:").pack(side=tk.LEFT)
        dir_frame = ttk.Frame(ctrl)
        dir_frame.pack(side=tk.LEFT, padx=(0, 12))
        self._direction_var = tk.StringVar(value="mine")
        ttk.Radiobutton(dir_frame, text="我说", variable=self._direction_var,
                         value="mine", command=self._on_direction_change).pack(side=tk.LEFT)
        ttk.Radiobutton(dir_frame, text="别人说", variable=self._direction_var,
                         value="theirs", command=self._on_direction_change).pack(side=tk.LEFT)
        ttk.Radiobutton(dir_frame, text="双向同时", variable=self._direction_var,
                         value="dual", command=self._on_direction_change).pack(side=tk.LEFT)

        ttk.Label(ctrl, text="源:").pack(side=tk.LEFT)
        self._source_combo = ttk.Combobox(ctrl, values=list(SOURCE_LANGS.keys()),
                                           state="readonly", width=10)
        self._source_combo.pack(side=tk.LEFT, padx=(0, 8))
        self._source_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

        ttk.Label(ctrl, text="目标:").pack(side=tk.LEFT)
        self._target_combo = ttk.Combobox(ctrl, values=list(TARGET_LANGS.keys()),
                                           state="readonly", width=10)
        self._target_combo.pack(side=tk.LEFT, padx=(0, 12))
        self._target_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

        out_frame = ttk.Frame(ctrl)
        out_frame.pack(side=tk.LEFT)
        ttk.Label(out_frame, text="输出:").pack(side=tk.LEFT)
        self._chatbox_var = tk.BooleanVar(value=True)
        self._overlay_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(out_frame, text="chatbox", variable=self._chatbox_var).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(out_frame, text="手腕屏", variable=self._overlay_var).pack(side=tk.LEFT, padx=4)

    def _build_chat(self) -> None:
        chat_frame = ttk.Frame(self._root)
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))

        # Treeview 不支持多行/换行，聊天气泡用 Canvas 手绘圆角矩形
        self._canvas = tk.Canvas(chat_frame, bg=BG, highlightthickness=0)
        self._vsb = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_canvas_scroll)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)

    def _build_status(self) -> None:
        bar = ttk.Frame(self._root, padding=(6, 3))
        bar.pack(fill=tk.X)
        # 左侧放"最新一条状态消息"，右侧放统计汇总——两者分开，
        # 否则"等待收尾"这类瞬时消息会把"已翻译 N 条 / 首增量 Xms"覆盖掉。
        self._status_label = ttk.Label(bar, text="就绪", foreground="gray")
        self._status_label.pack(side=tk.LEFT)
        self._stats_label = ttk.Label(bar, text="", foreground="#666")
        self._stats_label.pack(side=tk.RIGHT)

    # ================================================================ 事件处理

    def _check_api_key(self) -> None:
        try:
            from .config import load_api_key
            load_api_key()
        except SystemExit as e:
            self._set_status("error", str(e))

    def _on_canvas_scroll(self, first: str, last: str) -> None:
        self._vsb.set(first, last)
        # 滚到底部才恢复自动跟随；用户手动往上翻时不抢
        self._auto_scroll = float(last) >= 0.99

    def _on_mousewheel(self, event) -> None:
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_canvas_configure(self, event=None) -> None:
        w = event.width if event is not None else self._canvas.winfo_width()
        if w <= 1 or abs(w - self._canvas_w) <= 2:
            return
        self._canvas_w = w
        # 拖拽窗口会连发 Configure：合并到 120ms 后一次性重排
        if self._relayout_job is not None:
            try:
                self._root.after_cancel(self._relayout_job)
            except Exception:
                pass
        self._relayout_job = self._root.after(120, self._redraw_all)

    def _on_direction_change(self) -> None:
        self._update_direction_langs()

    def _update_direction_langs(self) -> None:
        d = self._direction_var.get()
        a, b = self._lang_pair["source"], self._lang_pair["target"]
        if d == "theirs":
            # 别人说：源=对方语言（=我的目标），目标=我的语言（=我的源）
            src_code, tgt_code = b, a or "zh"
            self._source_combo.configure(values=list(TARGET_LANGS.keys()))
            self._target_combo.configure(values=list(TARGET_LANGS.keys()))
        else:
            src_code, tgt_code = a, b
            self._source_combo.configure(values=list(SOURCE_LANGS.keys()))
            self._target_combo.configure(values=list(TARGET_LANGS.keys()))
        self._source_combo.set(_source_name(src_code))
        self._target_combo.set(_target_name(tgt_code))

    def _on_lang_change(self, _event=None) -> None:
        d = self._direction_var.get()
        if d == "theirs":
            self._lang_pair["target"] = TARGET_LANGS.get(self._source_combo.get(),
                                                         self._lang_pair["target"])
            self._lang_pair["source"] = TARGET_LANGS.get(self._target_combo.get())
        else:
            self._lang_pair["source"] = SOURCE_LANGS.get(self._source_combo.get())
            self._lang_pair["target"] = TARGET_LANGS.get(self._target_combo.get(),
                                                         self._lang_pair["target"])
        if self._lang_pair["source"] is None:
            # 自动检测没有对应目标：别人说方向的目标回落到中文
            self._set_status("info", f"已切换为{_target_name(self._lang_pair['target'])} → 中文")
        self._save_lang_config()
        self._push_lang_to_engines()
        self._update_direction_langs()

    def _save_lang_config(self) -> None:
        """把互为镜像的两组方向写回 config.yaml（mine: A→B / theirs: B→A）。"""
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        pairs = {
            "mine": {"source_lang": a, "target_lang": b},
            "theirs": {"source_lang": b, "target_lang": a or "zh"},
        }
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            dirs = dict(raw.get("directions") or {})
            for name, langs in pairs.items():
                d = dict(dirs.get(name) or {})
                d["source_lang"] = langs["source_lang"]
                d["target_lang"] = langs["target_lang"]
                dirs[name] = d
            raw["directions"] = dirs
            with p.open("w", encoding="utf-8") as f:
                yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)
        except Exception as exc:
            print(f"[gui] 保存配置失败：{exc}")

    def _push_lang_to_engines(self) -> None:
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        for name, (src, tgt) in (("mine", (a, b)), ("theirs", (b, a or "zh"))):
            d = self._cfg.directions.setdefault(name, Direction())
            d.source_lang = src
            d.target_lang = tgt
        for eng, direction in zip(self._engines, self._engine_dirs):
            if not eng.running:
                continue
            src, tgt = (a, b) if direction == "mine" else (b, a or "zh")
            eng.set_languages(src, tgt)

    # ================================================================ 引擎控制

    def _start(self) -> None:
        if any(e.running for e in self._engines):
            return
        d = self._direction_var.get()

        sinks: set[str] = set()
        if self._chatbox_var.get():
            sinks.add("chatbox")
        if self._overlay_var.get():
            sinks.add("overlay")
        if not sinks:
            self._set_status("warn", "请至少选择一个输出")
            return

        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        specs: list[tuple] = []
        if d in ("mine", "dual"):
            specs.append(("mine", "mine", "mic", a, b))
        if d in ("theirs", "dual"):
            specs.append(("theirs", "theirs", "loopback", b, a or "zh"))

        for _who, direction, _src, src_lang, tgt_lang in specs:
            dd = self._cfg.directions.setdefault(direction, Direction())
            dd.source_lang = src_lang
            dd.target_lang = tgt_lang

        self._current = {}
        self._auto_scroll = True
        self._engines = []
        self._engine_dirs = []
        self._specs = specs
        self._sinks = sinks
        self._pending_starts = len(specs)
        self._start_engine(0)
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        self._set_status("info", "正在启动（双向）…" if len(specs) == 2 else "正在启动…")

    def _start_engine(self, index: int) -> None:
        specs = self._specs
        if index >= len(specs):
            return
        who, direction, source, src_lang, tgt_lang = specs[index]
        events = EngineEvents(
            on_text=lambda src, txt, final, who=who: self._q.put(("text", who, src, txt, final)),
            on_status=lambda lvl, msg: self._q.put(("status", lvl, msg)),
            on_stats=lambda s: self._q.put(("stats", s)),
        )
        eng = Engine(
            cfg=self._cfg,
            direction=direction,
            source=source,
            sinks=self._sinks,
            events=events,
            config_path=DEFAULT_CONFIG,
        )
        self._engines.append(eng)
        self._engine_dirs.append(direction)
        self._pending_starts -= 1
        eng.start()
        if self._pending_starts > 0:
            # 两个引擎错开 300ms 启动，避免同时抢占音频设备
            self._start_job = self._root.after(300, self._start_engine, index + 1)

    def _stop(self) -> None:
        self._pending_starts = 0
        self._specs = []
        if self._start_job is not None:
            try:
                self._root.after_cancel(self._start_job)
            except Exception:
                pass
            self._start_job = None
        for eng in self._engines:
            eng.stop()
        self._engines = []
        self._engine_dirs = []
        self._start_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)
        self._set_status("info", "已停止")

    def _on_close(self) -> None:
        self._stop()
        self._root.destroy()

    # ================================================================ 队列轮询

    def _poll(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "text":
                    self._add_text(item[2], item[3], item[4], who=item[1])
                elif kind == "status":
                    self._set_status(item[1], item[2])
                elif kind == "stats":
                    self._stats.update(item[1])
                    self._refresh_status()
        except queue.Empty:
            pass
        if (self._pending_starts == 0 and self._engines
                and all(not e.running for e in self._engines)):
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            # 引擎失败时状态栏已有 error 消息，别用"已停止"盖掉
            if self._last_status_level != "error":
                self._set_status("info", "已停止")
            self._engines = []
            self._engine_dirs = []
        self._root.after(50, self._poll)

    # ================================================================ 聊天气泡

    def _add_text(self, source: str, text: str, is_final: bool, who: str = "mine") -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        cur = self._current.get(who)
        if cur is not None:
            # 流式增量：就地重画同一条气泡（终版只做"封口"，绝不新插第二条）
            cur.source = source
            cur.text = text
            if is_final:
                cur.final = True
                self._current.pop(who, None)
            if hasattr(self, "_canvas"):
                self._redraw_current(cur)
            if self._auto_scroll and hasattr(self, "_canvas"):
                self._canvas.yview_moveto(1.0)
            return
        # 没有正在刷新的气泡 → 新建（一句话的第一条就是终版时走这里）
        b = _Bubble(who=who, source=source, text=text, ts=now_str, final=is_final)
        if not is_final:
            self._current[who] = b
        b.y = (self._bubbles[-1].y + self._bubbles[-1].h + 8) if self._bubbles else 8
        self._bubbles.append(b)
        if hasattr(self, "_canvas"):
            self._draw_bubble(b)
            self._trim()
            self._update_scrollregion()
            if self._auto_scroll:
                self._canvas.yview_moveto(1.0)

    def _draw_bubble(self, b: _Bubble) -> int:
        """按已验证配方画一条气泡，返回下一条气泡的起始 y。

        每条气泡两行：**原文小字（上） + 译文大字（下）**——主次分明，
        你要读的永远是下面那行大字。服务端没返回原文（或原文与译文相同）时
        只画译文，不留空行。
        """
        cv = self._canvas
        w = self._canvas_w if self._canvas_w > 1 else 600
        maxw = int(w * 0.62)                       # width 参数自带自动换行

        # 1) 先量尺寸：两张文字都先画在 (0,0)，量完再挪进气泡
        tid_big = cv.create_text(0, 0, text=b.text, width=maxw,
                                 anchor="nw", font=FONT, fill=COLOR_TEXT)
        bx1, by1, bx2, by2 = cv.bbox(tid_big)
        big_w, big_h = bx2 - bx1, by2 - by1

        tid_small = None
        small_w = small_h = 0
        src = (b.source or "").strip()
        if src and src != (b.text or "").strip():   # 原文为空或与译文相同时不画小字行
            tid_small = cv.create_text(0, 0, text=b.source, width=maxw, anchor="nw",
                                       font=FONT_SMALL,
                                       fill=COLOR_SRC_MINE if b.who == "mine" else COLOR_SRC_THEIRS)
            sx1, sy1, sx2, sy2 = cv.bbox(tid_small)
            small_w, small_h = sx2 - sx1, sy2 - sy1

        gap = 6 if tid_small is not None else 0
        pad_x, pad_y = 12, 9
        bw = max(big_w, small_w) + pad_x * 2       # 气泡贴合内容自适应
        bh = small_h + gap + big_h + pad_y * 2
        y = b.y
        items = [tid_big]
        if tid_small is not None:
            items.append(tid_small)
        if b.ts:
            items.append(cv.create_text(
                w - 18 if b.who == "mine" else 18, y, text=b.ts,
                anchor="ne" if b.who == "mine" else "nw",
                font=FONT_META, fill=COLOR_META))
            y += 14
        bx = w - 18 - bw if b.who == "mine" else 18  # 右 / 左
        fill = COLOR_MINE if b.who == "mine" else COLOR_THEIRS
        rid = round_rect(cv, bx, y, bx + bw, y + bh, 12, fill=fill, outline="")
        # ⚠️ 必须压到最底层（tag_lower 不带第二参数）。若写成
        # `for t in items: cv.tag_lower(rid, t)`，第二次迭代会把矩形挪到
        # 大号文字之上，译文会被气泡背景整条遮住——原型阶段实测踩过这个坑。
        cv.tag_lower(rid)

        ty = y + pad_y
        if tid_small is not None:
            cv.coords(tid_small, bx + pad_x, ty)
            ty += small_h + gap
        cv.coords(tid_big, bx + pad_x, ty)         # 译文大字在下方
        items.append(rid)
        b.items = items
        b.h = y + bh - b.y
        return y + bh + 8

    def _redraw_current(self, b: _Bubble) -> None:
        """流式增量：删掉旧图元，在同一个起始 y 位置重画同一条气泡。"""
        for iid in b.items:
            self._canvas.delete(iid)
        old_h = b.h
        self._draw_bubble(b)
        delta = b.h - old_h
        if delta:
            # 气泡长高时把下面的气泡整体下移，避免重叠（双向同时时会用到）
            idx = self._bubbles.index(b)
            for other in self._bubbles[idx + 1:]:
                other.y += delta
                if other.items:
                    self._canvas.move(*other.items, 0, delta)
            self._update_scrollregion()

    def _trim(self) -> None:
        """气泡超过上限时删最旧的若干条，剩余图元整体上移。"""
        removed = 0
        while len(self._bubbles) > MAX_BUBBLES:
            old = self._bubbles.pop(0)
            if self._current.get(old.who) is old:
                self._current.pop(old.who, None)
            removed += old.h + 8
            for iid in old.items:
                self._canvas.delete(iid)
        if removed:
            self._canvas.move("all", 0, -removed)
            for b in self._bubbles:
                b.y -= removed

    def _update_scrollregion(self) -> None:
        if not hasattr(self, "_canvas"):
            return
        content = 8
        if self._bubbles:
            last = self._bubbles[-1]
            content = last.y + last.h + 8
        h = self._canvas.winfo_height()
        self._canvas.configure(scrollregion=(0, 0, max(self._canvas_w, 1), max(content, h)))

    def _redraw_all(self) -> None:
        """窗口 resize 后按新宽度全部重画（消息多时也比逐条挪快）。"""
        self._relayout_job = None
        if not hasattr(self, "_canvas"):
            return
        self._canvas.delete("all")
        y = 8
        for b in self._bubbles:
            b.y = y
            y = self._draw_bubble(b)
        self._update_scrollregion()
        if self._auto_scroll:
            self._canvas.yview_moveto(1.0)

    # ================================================================ 状态栏

    def _set_status(self, level: str, msg: str) -> None:
        self._last_status_level = level
        if not hasattr(self, "_status_label"):
            return
        colors = {"info": "#4a90d9", "warn": "#d9904a", "error": "#d94a4a"}
        self._status_label.configure(text=f"状态：{msg}", foreground=colors.get(level, "gray"))

    def _refresh_status(self) -> None:
        if not hasattr(self, "_stats_label"):
            return
        parts: list[str] = []
        if any(e.running for e in self._engines):
            parts.append("运行中")
        if any(e.chatbox is not None for e in self._engines):
            sent = sum(e.chatbox.sent_ok for e in self._engines if e.chatbox is not None)
            parts.append(f"已翻译 {sent} 条")
        ms = self._stats.get("first_delta_ms") or self._stats.get("connect_ms")
        if ms is not None:
            parts.append(f"首增量 {ms:.0f}ms")
        self._stats_label.configure(text=" · ".join(parts))

    # ================================================================ 自检

    def run_self_test(self) -> int:
        pcm_path = ROOT / "testdata" / "zh_test_16k.pcm"
        if not pcm_path.exists():
            print(f"GUI_SELFTEST_FAIL: 测试音频不存在 {pcm_path}", file=sys.stderr)
            return 1

        results: list[tuple] = []

        def on_text(src, txt, final):
            results.append((src, txt, final))

        def on_status(level, msg):
            print(f"[selftest][{level}] {msg}")

        cfg = load_config()
        cfg.directions["mine"].source_lang = "zh"
        cfg.directions["mine"].target_lang = "en"

        events = EngineEvents(on_text=on_text, on_status=on_status)
        engine = Engine(
            cfg=cfg, direction="mine", source=f"pcm:{pcm_path}",
            sinks={"chatbox"}, events=events, dry_run=True,
        )
        engine.start()
        engine.join(timeout=60)
        engine.stop(timeout=5)

        has_source = any(r[0].strip() for r in results)
        has_target = any(r[1].strip() for r in results)
        if has_source and has_target:
            print("GUI_SELFTEST_OK")
            return 0
        print(f"GUI_SELFTEST_FAIL: source={has_source} target={has_target} rows={len(results)}",
              file=sys.stderr)
        return 1

    def run_self_test_dual(self) -> int:
        """双向同时验收：两个测试 PCM 同时驱动两个引擎，左右两侧都必须出气泡。"""
        zh = ROOT / "testdata" / "zh_test_16k.pcm"
        en = ROOT / "testdata" / "en_test_16k.pcm"
        for p in (zh, en):
            if not p.exists():
                print(f"GUI_SELFTEST_DUAL_FAIL: 测试音频不存在 {p}", file=sys.stderr)
                return 1

        cfg = load_config()
        cfg.directions.setdefault("mine", Direction())
        cfg.directions.setdefault("theirs", Direction())
        cfg.directions["mine"].source_lang = "zh"
        cfg.directions["mine"].target_lang = "en"
        cfg.directions["theirs"].source_lang = "en"
        cfg.directions["theirs"].target_lang = "zh"

        engines: list[Engine] = []
        for who, direction, pcm_path in (("mine", "mine", zh), ("theirs", "theirs", en)):
            def on_text(src, txt, final, who=who):
                self._add_text(src, txt, final, who=who)

            events = EngineEvents(
                on_text=on_text,
                on_status=lambda lvl, msg, who=who: print(f"[dualtest][{who}][{lvl}] {msg}"),
            )
            eng = Engine(cfg=cfg, direction=direction, source=f"pcm:{pcm_path}",
                         sinks={"chatbox"}, events=events, dry_run=True)
            engines.append(eng)
            eng.start()
            time.sleep(0.3)      # 与 GUI 一致：错开 300ms，避免同时抢占音频设备
        for eng in engines:
            eng.join(timeout=90)
        for eng in engines:
            eng.stop(timeout=5)

        mine = [b for b in self._bubbles if b.who == "mine"]
        theirs = [b for b in self._bubbles if b.who == "theirs"]
        if mine and theirs:
            print(f"GUI_SELFTEST_DUAL_OK mine={len(mine)} theirs={len(theirs)}")
            return 0
        print(f"GUI_SELFTEST_DUAL_FAIL: mine={len(mine)} theirs={len(theirs)}",
              file=sys.stderr)
        return 1


# ================================================================ 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 - 图形界面")
    ap.add_argument("--self-test", action="store_true", help="自动化验收模式（单方向）")
    ap.add_argument("--self-test-dual", action="store_true",
                    help="双向同时验收：两个测试 PCM 同时驱动两个引擎")
    args = ap.parse_args()

    if args.self_test_dual:
        gui = TranslationGUI(headless=True)
        return gui.run_self_test_dual()
    if args.self_test:
        gui = TranslationGUI(headless=True)
        return gui.run_self_test()

    gui = TranslationGUI()
    gui._root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
