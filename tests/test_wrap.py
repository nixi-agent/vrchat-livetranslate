#!/usr/bin/env python
"""wrap 渲染的回归测试：整词保护（拉丁/西里尔/希腊/韩文）+ 中文避头尾 + CJK 逐字断行。

用法：.venv/Scripts/python.exe tests/test_wrap.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vlt.output.overlay import CLOSING_PUNCT, render_panel, wrap_text  # noqa: E402

FONT = "C:/Windows/Fonts/msyh.ttc"
MAX_W = 900
# 以字母开头的连续词（Unicode：西里尔/希腊/拉丁都算），CJK 词会被 _is_word_char 过滤掉
_WORD_RE = re.compile(r"[^\W\d_][\w'\-_/]*", re.UNICODE)


def _is_cjk_char(ch: str) -> bool:
    """测试**自带**的 CJK 判断（刻意不复用被测模块的 `_is_cjk`）。

    测试的每个判据都要独立于被测实现：拿实现自己的辅助函数去抽词，
    实现一坏、抽出来的词也跟着消失，测试就永远发现不了。
    """
    o = ord(ch)
    return (0x3000 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or o >= 0x20000)


def _words_of(text: str) -> list[str]:
    """从**原文**里独立抽出「应当整词保留」的词（CJK 除外）。

    ⚠️ 不用被测的 `_tokens()`：旧实现把所有非 ASCII 文字逐字切，
    拿它抽词会跟着实现一起退化 —— 测试就永远发现不了这个 bug
    （第一版就是这么写的，撤回修复后测试依然全绿，等于没测）。
    """
    out: list[str] = []
    for w in _WORD_RE.findall(text):
        if len(w) > 1 and not any(_is_cjk_char(c) for c in w):
            out.append(w)
    return out


CASES = [
    # 拉丁：整词保护的老用例
    "Hello! I'm Nixi. This sentence is being translated in real time — let's see how it looks on your wrist.",
    "你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。",
    "こんにちは、ニキシです。この文はリアルタイムで翻訳されています。手首に表示するとどう見えるでしょうか？",
    "VRChat 里的 instance 是英文单词，avatar 也是，混排时不应该被从中间劈开。",
    # ★ 俄语（2026-09-25 用户要求加俄语支持时实测发现：非 ASCII 文字被逐字切，
    #    `строк` 被劈成 `стро` + `к`）
    "Привет! Я из России. Это очень длинная русская фраза для проверки переноса строк на панели.",
    "Нажмите кнопку «Начать перевод», чтобы включить синхронный перевод в VRChat.",
    # 韩语同样用空格分词
    "안녕하세요! 저는 한국에서 왔습니다. 이 문장은 실시간으로 번역되고 있습니다.",
    # 希腊语（同为非 ASCII 拼音文字）
    "Γεια σας! Αυτή η πρόταση μεταφράζεται σε πραγματικό χρόνο για να ελέγξουμε το κείμενο.",
    # 混排：中文 + 俄语
    "这是一个 mixed 句子：俄语 Привет 和英语 hello 都不该被劈开。",
]
# 比整行还宽、只能按字符硬切的超长单词
LONG_WORD = "Достопримечательность" * 4


def main() -> int:
    img = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 48)
    failures: list[str] = []

    for case in CASES:
        lines = wrap_text(d, case, font, MAX_W)
        print(f"\n原文: {case[:60]}…")
        for i, ln in enumerate(lines):
            print(f"  {i+1}| {ln}")
        # ① 整词保护：整词（任意字母文字）必须**完整出现在某一行内**。
        #    不能用「上一行以字母结尾且下一行以字母开头」来判断——那分不清
        #    「词在行尾正常结束」和「词被劈开」，会误报。
        for tk in _words_of(case):
            if len(tk) <= 1:
                continue
            if d.textlength(tk, font=font) > MAX_W:
                continue                      # 比整行还宽的词允许硬切
            if not any(tk in ln for ln in lines):
                failures.append(f"单词被劈开: {tk!r} 未完整出现在任何一行")
        # ② 避头尾：行首不得是禁则标点
        for ln in lines[1:]:
            if ln and ln[0] in CLOSING_PUNCT:
                failures.append(f"行首出现禁则标点: {ln[:16]!r}")
        # ③ 行首不留空格
        for ln in lines:
            if ln != ln.lstrip():
                failures.append(f"行首有空格: {ln[:16]!r}")

    # ④ CJK 仍然逐字断行（别因为整词保护的改动把中文变成「整句一行」冲出版面）
    zh = "这是一句没有任何空格的中文长句，它必须能够按照面板宽度自动折行显示在手腕上。"
    zh_lines = wrap_text(d, zh, font, MAX_W)
    assert len(zh_lines) >= 2, f"中文没有折行（{len(zh_lines)} 行）——CJK 逐字切被改坏了"
    for ln in zh_lines:
        assert d.textlength(ln, font=font) <= MAX_W, f"中文行超宽：{ln[:20]!r}"

    # ⑤ 超长单词必须硬切而不是溢出面板
    lw_lines = wrap_text(d, LONG_WORD, font, MAX_W)
    assert len(lw_lines) >= 2, "超长单词没有硬切（会溢出面板）"
    for ln in lw_lines:
        assert d.textlength(ln, font=font) <= MAX_W + 1, \
            f"硬切后仍超宽（{d.textlength(ln, font=font):.0f}px > {MAX_W}）：{ln[:20]!r}"

    # ⑥ 渲染不崩、尺寸正确、有内容
    im = render_panel(CASES[1], CASES[0])
    assert im.size == (1024, 320), im.size
    assert im.getextrema()[3][1] > 0, "面板全透明，没有画出内容"

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ 失败 {len(failures)} 项：")
        for f in failures:
            print("   -", f)
        return 1
    print(f"✅ 全部通过（{len(CASES)} 个用例：整词保护含俄/希/韩 / 避头尾 / 行首空格 / "
          f"CJK 逐字断行 / 超长词硬切 / 渲染）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
