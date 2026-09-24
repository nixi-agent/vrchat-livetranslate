#!/usr/bin/env python
"""wrap 渲染的回归测试：拉丁整词保护 + 中文避头尾。

用法：.venv/Scripts/python.exe tests/test_wrap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vlt.output.overlay import CLOSING_PUNCT, _tokens, render_panel, wrap_text  # noqa: E402

FONT = "C:/Windows/Fonts/msyh.ttc"
CASES = [
    "Hello! I'm Nixi. This sentence is being translated in real time — let's see how it looks on your wrist.",
    "你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。",
    "こんにちは、ニキシです。この文はリアルタイムで翻訳されています。手首に表示するとどう見えるでしょうか？",
    "VRChat 里的 instance 是英文单词，avatar 也是，混排时不应该被从中间劈开。",
]


def main() -> int:
    img = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 48)
    failures: list[str] = []

    for case in CASES:
        lines = wrap_text(d, case, font, 900)
        print(f"\n原文: {case[:60]}…")
        for i, ln in enumerate(lines):
            print(f"  {i+1}| {ln}")
        # ① 拉丁整词保护：每个拉丁词必须**完整出现在某一行内**
        # （不能用「上一行以字母结尾且下一行以字母开头」来判断——那分不清
        #   「词在行尾正常结束」和「词被劈开」，会误报）
        for tk in _tokens(case):
            if len(tk) > 1 and tk.isascii() and tk.isalnum():
                if not any(tk in ln for ln in lines):
                    failures.append(f"拉丁词被劈开: {tk!r} 未完整出现在任何一行")
        # ② 避头尾：行首不得是禁则标点
        for ln in lines[1:]:
            if ln and ln[0] in CLOSING_PUNCT:
                failures.append(f"行首出现禁则标点: {ln[:16]!r}")
        # ③ 行首不留空格
        for ln in lines:
            if ln != ln.lstrip():
                failures.append(f"行首有空格: {ln[:16]!r}")

    # ④ 渲染不崩、尺寸正确、有内容
    im = render_panel(CASES[1], CASES[0])
    assert im.size == (1024, 320), im.size
    assert im.getextrema()[3][1] > 0, "面板全透明，没有画出内容"

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ 失败 {len(failures)} 项：")
        for f in failures:
            print("   -", f)
        return 1
    print(f"✅ 全部通过（{len(CASES)} 个用例：拉丁整词保护 / 避头尾 / 行首空格 / 渲染）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
