"""Tkinter 图形界面：翻译列表 + 开关 + 语言配置。

用法：
  python -m vlt.gui                       # 启动界面
  python -m vlt.gui --self-test            # 自动化验收（不起窗口）
"""
from __future__ import annotations

import argparse
import queue
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import yaml

from .config import load_config, DEFAULT_CONFIG
from .engine import Engine, EngineEvents

ROOT = Path(__file__).resolve().parent.parent

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


class TranslationGUI:
    """主界面。headless=True 时不创建 Tk 窗口（给 --self-test 用）。"""

    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._q: queue.Queue = queue.Queue()
        self._engine: Engine | None = None
        self._current_iid: str | None = None
        self._auto_scroll = True
        self._row_count = 0
        self._stats: dict = {}
        self._lang_memories: dict[str, dict] = {}

        self._cfg = load_config()
        for name, d in self._cfg.directions.items():
            self._lang_memories[name] = {
                "source": d.source_lang,
                "target": d.target_lang,
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
        self._build_tree()
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

    def _build_tree(self) -> None:
        tree_frame = ttk.Frame(self._root)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))

        cols = ("time", "source", "target")
        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        self._tree.heading("time", text="时间")
        self._tree.heading("source", text="原文")
        self._tree.heading("target", text="译文")
        self._tree.column("time", width=78, minwidth=60, stretch=False)
        self._tree.column("source", width=340, minwidth=100, stretch=False)
        self._tree.column("target", width=340, minwidth=100, stretch=False)
        # 列宽跟着窗口走：否则窗口拉大后译文列仍按固定宽度截断（长句看不全）
        self._tree.bind("<Configure>", self._fit_columns)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=self._on_tree_scroll)

        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _fit_columns(self, event=None) -> None:
        """按当前窗口宽度重新分配列宽：时间列固定，原文/译文平分剩余空间。"""
        total = self._tree.winfo_width()
        if total <= 1:                      # 还没完成布局
            return
        time_w = 78
        rest = max(total - time_w - 20, 200)
        src_w = int(rest * 0.44)
        self._tree.column("time", width=time_w)
        self._tree.column("source", width=src_w)
        self._tree.column("target", width=rest - src_w)

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

    def _on_tree_scroll(self, first, last) -> None:
        self._tree.yview_moveto(first)
        self._auto_scroll = float(last) >= 0.99

    def _on_direction_change(self) -> None:
        self._update_direction_langs()

    def _update_direction_langs(self) -> None:
        d = self._direction_var.get()
        mem = self._lang_memories.get(d, {})
        src = mem.get("source")
        tgt = mem.get("target")
        src_name = next((k for k, v in SOURCE_LANGS.items() if v == src), "自动检测")
        tgt_name = next((k for k, v in TARGET_LANGS.items() if v == tgt), "英语")
        self._source_combo.set(src_name)
        self._target_combo.set(tgt_name)

    def _on_lang_change(self, _event=None) -> None:
        if self._engine is not None and self._engine.running:
            src = SOURCE_LANGS.get(self._source_combo.get())
            tgt = TARGET_LANGS.get(self._target_combo.get())
            if tgt:
                self._engine.set_languages(src, tgt)
        self._save_lang_config()

    def _save_lang_config(self) -> None:
        d = self._direction_var.get()
        src = SOURCE_LANGS.get(self._source_combo.get())
        tgt = TARGET_LANGS.get(self._target_combo.get())
        self._lang_memories.setdefault(d, {})["source"] = src
        self._lang_memories[d]["target"] = tgt

        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            dirs = raw.get("directions") or {}
            if d in dirs:
                dirs[d]["source_lang"] = src
                dirs[d]["target_lang"] = tgt or dirs[d].get("target_lang", "en")
                raw["directions"] = dirs
                with p.open("w", encoding="utf-8") as f:
                    yaml.dump(raw, f, allow_unicode=True, default_flow_style=False)
        except Exception as exc:
            print(f"[gui] 保存配置失败：{exc}")

    # ================================================================ 引擎控制

    def _start(self) -> None:
        if self._engine is not None and self._engine.running:
            return
        d = self._direction_var.get()
        src_name = self._source_combo.get()
        tgt_name = self._target_combo.get()
        src_lang = SOURCE_LANGS.get(src_name)
        tgt_lang = TARGET_LANGS.get(tgt_name, "en")

        self._cfg.directions[d].source_lang = src_lang
        self._cfg.directions[d].target_lang = tgt_lang

        sinks: set[str] = set()
        if self._chatbox_var.get():
            sinks.add("chatbox")
        if self._overlay_var.get():
            sinks.add("overlay")
        if not sinks:
            self._set_status("warn", "请至少选择一个输出")
            return

        self._current_iid = None
        self._auto_scroll = True

        events = EngineEvents(
            on_text=lambda src, txt, final: self._q.put(("text", src, txt, final)),
            on_status=lambda lvl, msg: self._q.put(("status", lvl, msg)),
            on_stats=lambda s: self._q.put(("stats", s)),
        )

        self._engine = Engine(
            cfg=self._cfg,
            direction=d,
            source="mic",
            sinks=sinks,
            events=events,
            config_path=DEFAULT_CONFIG,
        )
        self._engine.start()
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        self._set_status("info", "正在启动…")

    def _stop(self) -> None:
        if self._engine is not None:
            self._engine.stop()
            self._engine = None
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
                    self._add_text(item[1], item[2], item[3])
                elif kind == "status":
                    self._set_status(item[1], item[2])
                elif kind == "stats":
                    self._stats.update(item[1])
                    self._refresh_status()
        except queue.Empty:
            pass
        if self._engine is not None and not self._engine.running:
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            self._set_status("info", "已停止")
            self._engine = None
        self._root.after(50, self._poll)

    # ================================================================ 文本列表

    def _add_text(self, source: str, text: str, is_final: bool) -> None:
        if not hasattr(self, "_tree"):
            return
        now_str = datetime.now().strftime("%H:%M:%S")
        # 有正在刷新的行 → 就地更新（终版走同一条路，只是多一个"封行"动作）。
        # 注意：终版**不能新插一行**，否则每句话都会变成两行（实测每句都会重复）。
        if self._current_iid is not None:
            iid = self._current_iid
            self._tree.set(iid, "source", source)
            self._tree.set(iid, "target", text)
            if is_final:
                self._tree.item(iid, tags=("final",))
                self._current_iid = None
            if self._auto_scroll:
                self._tree.see(iid)
            return
        # 没有当前行 → 新建（一句话的第一条就是终版时走这里）
        iid = str(self._row_count)
        self._row_count += 1
        self._tree.insert("", tk.END, iid=iid, values=(now_str, source, text))
        if is_final:
            self._tree.item(iid, tags=("final",))
        else:
            self._current_iid = iid
        if self._auto_scroll:
            self._tree.see(iid)

    # ================================================================ 状态栏

    def _set_status(self, level: str, msg: str) -> None:
        if not hasattr(self, "_status_label"):
            return
        colors = {"info": "#4a90d9", "warn": "#d9904a", "error": "#d94a4a"}
        self._status_label.configure(text=f"状态：{msg}", foreground=colors.get(level, "gray"))

    def _refresh_status(self) -> None:
        if not hasattr(self, "_stats_label"):
            return
        parts: list[str] = []
        if self._engine is not None and self._engine.running:
            parts.append("运行中")
        cb = self._engine.chatbox if self._engine else None
        if cb is not None:
            parts.append(f"已翻译 {cb.sent_ok} 条")
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
        done = threading.Event()

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


# ================================================================ 入口

import threading  # noqa: E402 (needed for run_self_test)


def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 - 图形界面")
    ap.add_argument("--self-test", action="store_true", help="自动化验收模式")
    args = ap.parse_args()

    if args.self_test:
        gui = TranslationGUI(headless=True)
        return gui.run_self_test()

    gui = TranslationGUI()
    gui._root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
