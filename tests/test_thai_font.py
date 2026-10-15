#!/usr/bin/env python
"""泰文字体覆盖 + 中泰混排渲染的回归测试（离线、不碰 VR 运行时）。

用法：.venv/Scripts/python.exe tests/test_thai_font.py

## 为什么必须有这个测试

语言表加了 `th`、模型也翻得出泰文，但**画到手腕屏上会是一排豆腐块**：
实测 `msyh.ttc`（微软雅黑，本项目默认 CJK 字体）不含泰文字形 ——
用 PIL 渲染 `สวัสดี` 得到的位图与渲染「私有区缺字位 U+E000 × 6」的位图
**逐字节完全相同**（主控 2026-10-02 实测，本文件把这条判据固化成断言）。

而这类 bug 是**静默**的：没有异常、没有告警，界面上什么都对，只有贴图是方块。
所以必须用位图比对钉住，光靠「跑一遍不崩」根本发现不了。

## 判据为什么可信（不是自说自话）

`test_tofu_probe_has_teeth` 先证明**判据本身有分辨力**：拿本机解析出来的 CJK 字体去渲染
泰文，必须真的得到豆腐块（= U+E000 的位图）。若某天 CJK 字体自带了泰文字形，
这条会打印提示（说明本机不需要泰文字体兜底），后面的「不是豆腐块」断言也就失去意义 ——
所以两条要一起看，绝不单独绿。

测试**不复用**被测模块的 `_script_of` / `_split_script_runs`：判据必须独立于实现，
否则实现一坏、判据跟着一起坏（同 `tests/test_wrap.py` 的做法）。

⚠️ 本文件只钉**泰文**（外加「这套机制不波及 CJK」这条不变量）。韩文走的是同一套「按书写系统
切 run、各用各的字体」机制，但判据、字体候选、降级文案都不同，由 `tests/test_korean_font.py`
单独钉住 —— 两边共用的是机制，不是用例。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt import platform as platform_mod            # noqa: E402
from vlt.output import overlay                      # noqa: E402
from vlt.output.overlay import (                    # noqa: E402
    CLOSING_PUNCT,
    OverlayConfig,
    _draw_line,
    _measure_line,
    load_script_fonts,
    render_conversation,
    render_panel,
    resolve_font_path,
    resolve_thai_font_path,
    wrap_text,
)

# 缺字位（Private Use Area，任何字体都不该有真字形）→ 渲染出来就是豆腐块。
# 这是主控判定「雅黑没泰文」用的同一个参照物。
TOFU = "\uE000"
THAI_WORD = "สวัสดี"                 # 6 个码位（含两个组合符号）
# ⚠️ 必须**纯泰文码位**（不含拉丁字母、不含空格）：
#   - CJK 字体有拉丁字形，句子里夹了 "Nixi" 就整体不等于豆腐块，元测试的判据会失效；
#   - 空格/拉丁在 `_split_script_runs` 里是独立 run，「纯泰文=单 run」的断言也会失效。
THAI_SENTENCE = "สวัสดีครับฉันชื่อนิกิยินดีที่ได้รู้จัก"
# 真实同传里泰语译文常夹英文专名 —— 这条用来跑折行（多 run + 三种书写系统）
THAI_WITH_LATIN = "สวัสดีครับ ฉันชื่อ Nixi ยินดีที่ได้รู้จัก"
CJK_WORD = "你好，我是逆袭"
CJK_SENTENCE = "你好，我是逆袭。这句话正在被实时翻译。"
MIXED_LINE = "你好，สวัสดี，我是 Nixi —— 中泰混排不能出豆腐块"
FONT_SIZE = 48
CANVAS = (1400, 160)                 # 够宽够高，避免裁切影响 bbox 判据
ORIGIN = (10, 20)
RIGHT_ANCHOR_X = CANVAS[0] - 100     # 右对齐用例的锚点（留出整行宽度，别蹭到画布左边）
# 实测 run-aware 绘制的最右墨迹与 _measure_line 的差在 ±1px 内，留 3px 余量
INK_TOLERANCE_PX = 3


# ---------------------------------------------------------------- 小工具（判据独立于实现）


def _thai_only(font: ImageFont.FreeTypeFont) -> dict[str, ImageFont.FreeTypeFont]:
    """「只带泰文那一格」的专用字体字典。

    生产入口是 `load_script_fonts(cfg, size)`（泰文/韩文…各一格）；用例只关心泰文，
    所以显式只给泰文 —— 这样「泰文那一段换了字体」这件事就是唯一变量。
    """
    return {"thai": font}


def _is_thai_char(ch: str) -> bool:
    """泰文块 U+0E00–U+0E7F。**刻意不复用** `overlay._is_thai`。"""
    return 0x0E00 <= ord(ch) <= 0x0E7F


def _is_hanzi(ch: str) -> bool:
    return 0x4E00 <= ord(ch) <= 0x9FFF


def _thai_chars(text: str) -> list[str]:
    return [c for c in text if _is_thai_char(c)]


def _blank() -> Image.Image:
    """灰度画布：豆腐块判定只看字形墨迹，颜色/透明度无关（也避免 L 模式塞 RGBA fill）。"""
    return Image.new("L", CANVAS, 0)


def _render(draw_fn) -> Image.Image:
    img = _blank()
    draw_fn(ImageDraw.Draw(img))
    return img


def _is_tofu(font: ImageFont.FreeTypeFont, text: str) -> bool:
    """`text` 用 `font` 画出来是不是豆腐块 —— 与「同样个数的缺字位」逐字节比对。

    这正是主控用的判据：msyh.ttc 渲染 `สวัสดี` == 渲染 `U+E000 × 6`。
    """
    if not text:
        return False
    ref = _render(lambda d: d.text(ORIGIN, TOFU * len(text), font=font, fill=255))
    got = _render(lambda d: d.text(ORIGIN, text, font=font, fill=255))
    return ref.tobytes() == got.tobytes()


def _ink_bbox(img: Image.Image) -> tuple[int, int]:
    """相对 ORIGIN 的 (最左, 最右) 墨迹位置（px）。空图返回 (0, 0)。"""
    bb = img.getbbox()
    return (bb[0] - ORIGIN[0], bb[2] - ORIGIN[0]) if bb else (0, 0)


def _fonts_at(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """(CJK 字体, 泰文字体) @ 指定字号，都走平台门面解析 —— 不写死任何平台路径。

    路径缺失时在这里就报清楚，而不是把 None 递给 `ImageFont.truetype`
    （那只会抛 `'NoneType' object has no attribute 'read'`，看不出是字体没探到）。
    """
    cjk_path = resolve_font_path("")
    thai_path = resolve_thai_font_path("")
    assert cjk_path, "平台层没能解析出 CJK 字体，后续判据无从谈起"
    assert thai_path, (f"平台层没能解析出泰文字体"
                       f"（find_thai_font()={platform_mod.find_thai_font()!r}）"
                       " —— 泰语会渲染成豆腐块")
    return ImageFont.truetype(cjk_path, size), ImageFont.truetype(thai_path, size)


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    return _fonts_at(FONT_SIZE)


# 本机是否有含泰文字形的字体。没有时（裸 Linux 机器 / 没装泰文字体的 CI 很常见），
# 依赖泰文字体的用例**明确跳过并说明原因**，而不是判红 —— 与
# `tests/test_wayland_window.py` / `test_x11_window.py` 缺 sway / Xvfb 时的口径一致。
# ⚠️ 不许静默：跳过的用例都会打印一行，结尾统计里也会写清跑了几条、跳过几条。
THAI_FONT_PATH: str | None = resolve_thai_font_path("")
_SKIPPED: list[str] = []
_NO_FONT_WHY = ("本机没有含泰文字形的字体（装了泰文字体后会自动跑；"
                "Linux 见 docs/GUIDE.linux.md 的泰语一节）")


def _no_thai_font() -> bool:
    """True = 本机没有泰文字体，该用例应跳过（调用方负责 `return _skip(name)`）。"""
    return not THAI_FONT_PATH


def _skip(name: str) -> None:
    _SKIPPED.append(name)
    print(f"  ⏭ 跳过 {name}：{_NO_FONT_WHY}")


@contextlib.contextmanager
def _stub_script_fonts(values: dict[str, str | None]):
    """让指定书写系统解析到固定值（None = 本机没有覆盖该书写系统的字体）。

    ⚠️ 打桩的是**通用入口** `overlay.resolve_script_font_path`（`resolve_thai_font_path` /
    `resolve_korean_font_path` 都只是它的薄包装）—— 打桩包装函数在实现搬家时会静默失效。
    同时清一次解析缓存：缓存会把上一次的真实结果留住，让打桩看不见。
    """
    cache = dict(overlay._script_font_cache)
    orig = overlay.resolve_script_font_path
    overlay._script_font_cache.clear()

    def _fake(script: str, configured: str) -> str | None:
        if script in values:
            return values[script]
        return orig(script, configured)

    overlay.resolve_script_font_path = _fake       # type: ignore[assignment]
    try:
        yield
    finally:
        overlay.resolve_script_font_path = orig    # type: ignore[assignment]
        overlay._script_font_cache.clear()
        overlay._script_font_cache.update(cache)


# 夹具自检：THAI_SENTENCE 必须是纯泰文码位（理由见上面的注释）。
# 放在 import 期就跑 —— 谁改了夹具、把拉丁字母或空格塞回来，这里立刻红，
# 而不是让「豆腐块判据」悄悄失去分辨力。
assert THAI_SENTENCE and all(_is_thai_char(c) for c in THAI_SENTENCE), \
    f"夹具 THAI_SENTENCE 必须是纯泰文码位：{THAI_SENTENCE!r}"
assert _thai_chars(THAI_WITH_LATIN) and not all(_is_thai_char(c) for c in THAI_WITH_LATIN), \
    f"夹具 THAI_WITH_LATIN 必须泰文+其它书写系统混排：{THAI_WITH_LATIN!r}"


# ---------------------------------------------------------------- ① 判据自身有分辨力


def test_tofu_probe_has_teeth() -> None:
    """★ 元测试：本机解析出的 **CJK 字体渲染泰文必须是豆腐块**。

    这条不成立的话，下面所有「不是豆腐块」的断言都是空转（拿一个本来就覆盖泰文的字体
    去测，怎么写都绿）。CJK 字体自带泰文字形属于环境特例，不算失败，但必须显式说出来。

    ⚠️ 判据只用**整词口径**：早期版本还要求「整词 == 逐字」一致，实测在 Linux 的
    Noto Sans CJK 上不成立 —— 泰文的组合符号（如 `ั` `ี`）在缺字形的字体里会被画成
    **空白**而不是方框，于是「逐字比 U+E000 的方框」判 False、整词判 True，
    两者本来就不该相等。整词口径才是「这段文字有没有被这个字体画出来」的正确问法。
    """
    cjk_path = resolve_font_path("")
    assert cjk_path, "平台层没解析出 CJK 字体"
    cjk = ImageFont.truetype(cjk_path, FONT_SIZE)
    whole = _is_tofu(cjk, THAI_WORD)
    if not whole:
        print(f"  ⚠ 本机 CJK 字体（{cjk_path}）自带泰文字形，"
              "「豆腐块判据」在本机没有分辨力（环境特例，不判失败）")
        return
    assert _is_tofu(cjk, THAI_SENTENCE), "换个更长的泰文句子判据就失效了？"
    # 判据有分辨力：同一段文字换成泰文字体就不是豆腐块（本机没有泰文字体时无从对照，跳过该半条）
    if THAI_FONT_PATH:
        thai = ImageFont.truetype(THAI_FONT_PATH, FONT_SIZE)
        assert not _is_tofu(thai, THAI_WORD), \
            f"泰文字体（{Path(THAI_FONT_PATH).name}）渲染 {THAI_WORD!r} 也是豆腐块 —— 判据认不出对照物"
    assert not _is_tofu(cjk, CJK_WORD), \
        f"CJK 字体连 {CJK_WORD!r} 都渲染成豆腐块 —— 参照物选错了，判据不可用"
    print(f"  ✓ 判据有分辨力：CJK 字体 {Path(cjk_path).name} 渲染 {THAI_WORD!r} "
          f"== 渲染 {TOFU!r}×{len(THAI_WORD)}（中文非豆腐块；泰文字体在场时结果相反）")


# ---------------------------------------------------------------- ② 泰文字体真的有泰文字形


def test_thai_font_renders_real_glyphs() -> None:
    """★ 解析出来的泰文字体渲染 `สวัสดี`，位图**不等于**缺字位位图（整词 + 逐字两种口径）。"""
    if _no_thai_font():
        return _skip("test_thai_font_renders_real_glyphs")
    _, thai = _fonts()
    assert not _is_tofu(thai, THAI_WORD), \
        f"泰文字体渲染 {THAI_WORD!r} 仍是豆腐块（== {TOFU!r}×{len(THAI_WORD)}）"
    bad = sorted({f"U+{ord(c):04X}" for c in _thai_chars(THAI_SENTENCE) if _is_tofu(thai, c)})
    assert not bad, f"泰文字体缺这些泰文字形：{bad}"
    got = _render(lambda d: d.text(ORIGIN, THAI_WORD, font=thai, fill=255))
    assert got.getbbox(), f"泰文字体渲染 {THAI_WORD!r} 全空白（一个墨迹像素都没有）"
    print(f"  ✓ 泰文字体渲染真字形：整词 + {len(set(_thai_chars(THAI_SENTENCE)))} "
          f"个不同泰文码位均非豆腐块，{THAI_WORD!r} 墨迹 {_ink_bbox(got)[1]}px")


def test_thai_font_resolved_through_platform_facade() -> None:
    """泰文字体必须经 `vlt.platform.find_thai_font()` 门面解析，且结果落在真实存在的文件上。

    顺带钉住降级留痕（禁静默）：门面返回 None 时 `resolve_thai_font_path` 也要返回 None
    并**打一行告警**，渲染不许崩（回落 CJK 字体 → 豆腐块，但至少留了痕）。
    """
    path = resolve_thai_font_path("")
    if path:
        assert Path(path).exists(), f"泰文字体路径无效：{path!r}"
        assert path == platform_mod.find_thai_font(), \
            (f"解析结果与门面不一致（overlay 自己写了平台路径？）："
             f"{path!r} vs {platform_mod.find_thai_font()!r}")

        src = (ROOT / "vlt" / "output" / "overlay.py").read_text(encoding="utf-8")
        assert Path(path).name not in src, \
            f"共享模块 overlay.py 里出现了具体泰文字体文件名 {Path(path).name!r}（平台路径必须留在 vlt/platform/）"
    else:
        print("  ⏭ 本机没有泰文字体 → 跳过「路径有效 / 与门面一致 / 不在共享模块里」三条；"
              "降级留痕那半条照跑")

    saved_warned = set(overlay._script_font_warned)
    orig_facade = platform_mod.find_thai_font
    try:
        overlay._script_font_cache.clear()
        # 留痕标志按「书写系统:类别」分键（见 overlay._warn_script_font），清掉泰文那一族
        overlay._script_font_warned.difference_update(
            k for k in list(overlay._script_font_warned) if k.startswith("thai:"))
        platform_mod.find_thai_font = lambda: None       # type: ignore[assignment]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = resolve_thai_font_path("")
        assert got is None, f"门面找不到字体时应返回 None（让调用方降级），实际 {got!r}"
        assert "豆腐块" in buf.getvalue(), \
            f"降级必须留痕（禁静默），实际输出：{buf.getvalue()!r}"
        # 告警只打一次，不许刷屏
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            resolve_thai_font_path("")
        assert "豆腐块" not in buf2.getvalue(), "同一条降级告警打了第二次（刷屏）"
        # 降级后渲染不许崩
        cfg = OverlayConfig()
        img = render_panel(THAI_SENTENCE, CJK_WORD, cfg)
        assert img.size == cfg.size_px
        assert img.getextrema()[3][1] > 0
    finally:
        platform_mod.find_thai_font = orig_facade        # type: ignore[assignment]
        overlay._script_font_cache.clear()
        overlay._script_font_warned.clear()
        overlay._script_font_warned.update(saved_warned)
    assert resolve_thai_font_path("") == path, "打桩复原后解析结果应回到真实字体"
    if path:
        print(f"  ✓ 泰文字体经平台门面解析（{Path(path).name}），overlay.py 不含该文件名；"
              "门面返回 None 时降级留痕（只一次）且渲染不崩")
    else:
        print("  ✓ 本机无泰文字体：门面返回 None 时降级留痕（只一次）且渲染不崩"
              "（解析路径那一半已跳过）")


# ---------------------------------------------------------------- ③ CJK 渲染行为不变


def test_cjk_font_not_swapped_for_thai() -> None:
    """★ 防回退：不能为了泰语（或别的书写系统）把默认 CJK 字体换掉。

    两条判据：
    ① `resolve_font_path("")` 仍然等于 `find_cjk_font()`（默认字体没被动过）；
    ② 非泰语文本走 `_draw_line` 与**改动前的写法** `d.text(..., font=cjk, anchor="la")`
       逐字节相同 —— 两种字体字典都试：只带泰文那格、以及**生产口径**
       `load_script_fonts(cfg, size)`（泰文+韩文…全都带上）。

    ⚠️ 样本里**不放泰文/韩文**：它们在生产口径下本来就该换字体（那是修复，不是回归），
       各自的行为由本文件后面的用例与 `tests/test_korean_font.py` 钉。
    """
    if _no_thai_font():
        return _skip("test_cjk_font_not_swapped_for_thai")
    assert resolve_font_path("") == platform_mod.find_cjk_font(), \
        (f"默认字体被换了：resolve_font_path('')={resolve_font_path('')!r} "
         f"find_cjk_font()={platform_mod.find_cjk_font()!r}")

    cjk, thai = _fonts()
    cfg = OverlayConfig()
    dicts = {"只带泰文格": _thai_only(thai), "生产口径": load_script_fonts(cfg, FONT_SIZE)}
    assert dicts["生产口径"], "生产口径的专用字体字典是空的，这条判据失去意义"
    samples = [CJK_WORD, CJK_SENTENCE, "こんにちは、ニキシです", "Привет! Я из России.",
               "Hello, world — 123."]
    checked = 0
    for label, fonts in dicts.items():
        for text in samples:
            ref = _render(lambda d, t=text: d.text(ORIGIN, t, font=cjk, fill=255, anchor="la"))
            got = _render(lambda d, t=text, ff=fonts: _draw_line(d, ORIGIN, t, cjk, ff,
                                                                 fill=255))
            assert ref.tobytes() == got.tobytes(), \
                f"非泰语文本 {text[:12]!r} 的渲染位图变了（{label}：CJK 行为被波及）"
            checked += 1
    assert not _is_tofu(cjk, CJK_WORD), \
        "前提不成立：CJK 字体连中文都是豆腐块，上面的位图相同断言没有意义"
    print(f"  ✓ 默认字体仍是 find_cjk_font()；{len(samples)} 条中/日/俄/拉丁样本 × "
          f"{len(dicts)} 种字体字典（共 {checked} 条）单 run 渲染与改动前逐字节相同")


def test_pure_cjk_panel_bitmap_unchanged() -> None:
    """★ 端到端：纯中日韩面板在「泰文字体解析成功」与「解析失败(None)」两种情况下
    位图**逐字节一致** —— 泰语那条腿对不用泰语的用户是零影响的。"""
    cfg = OverlayConfig()
    text, source = CJK_SENTENCE, "こんにちは、ニキシです。"

    with_thai = render_panel(text, source, cfg)
    with _stub_script_fonts({"thai": None}):
        without = render_panel(text, source, cfg)
    assert with_thai.tobytes() == without.tobytes(), \
        "纯 CJK 面板的位图因泰文字体逻辑而改变（CJK 渲染行为必须不变）"
    assert with_thai.size == cfg.size_px, with_thai.size
    assert with_thai.getextrema()[3][1] > 0, "面板全透明，什么都没画出来"

    entries = [("mine", source, text), ("theirs", "Привет! Я из России.", "你好，我是逆袭")]
    conv_a = render_conversation(entries, cfg)
    with _stub_script_fonts({"thai": None}):
        conv_b = render_conversation(entries, cfg)
    assert conv_a.tobytes() == conv_b.tobytes(), \
        "纯 CJK 对话视图的位图因泰文字体逻辑而改变"
    assert conv_a.size == cfg.size_px, conv_a.size
    assert conv_a.getextrema()[3][1] > 0, "对话视图全透明"
    print(f"  ✓ render_panel / render_conversation 的纯 CJK 位图"
          f"与「无泰文字体」时逐字节相同（{cfg.size_px}）")


# ---------------------------------------------------------------- ④ 中泰混排不出豆腐块


def test_mixed_line_uses_both_fonts() -> None:
    """★ 同一行里既有中文又有泰文：泰文那段必须用泰文字体画，且整行不出豆腐块。

    判据（三条互相独立）：
    ① run-aware 绘制的位图 **≠** 把泰文字体强行换成 CJK 字体（= 修复前的豆腐块效果）
       的位图 —— 证明泰文那段真的换了字体；
    ② 混排行里每个泰文码位单独用泰文字体渲染都不是豆腐块；
    ③ 整行墨迹非空，且墨迹宽度与 `_measure_line` 的测量值吻合（测量跟着字体走）。
    """
    if _no_thai_font():
        return _skip("test_mixed_line_uses_both_fonts")
    cjk, thai = _fonts()
    assert _thai_chars(MIXED_LINE) and any(_is_hanzi(c) for c in MIXED_LINE), \
        "用例本身必须同时含中文与泰文"

    fixed = _render(lambda d: _draw_line(d, ORIGIN, MIXED_LINE, cjk, _thai_only(thai), fill=255))
    # 修复前的效果：泰文段也用 CJK 字体画 → 豆腐块
    before = _render(lambda d: _draw_line(d, ORIGIN, MIXED_LINE, cjk, _thai_only(cjk), fill=255))
    assert fixed.tobytes() != before.tobytes(), \
        "混排行渲染与「泰文用 CJK 字体画」完全相同 —— 泰文字体没被用上，泰文全是豆腐块"

    bad = sorted({f"U+{ord(c):04X}" for c in _thai_chars(MIXED_LINE) if _is_tofu(thai, c)})
    assert not bad, f"混排行里这些泰文码位会出豆腐块：{bad}"

    assert fixed.getbbox(), "混排行整行没有墨迹"
    d_probe = ImageDraw.Draw(_blank())
    want = _measure_line(d_probe, MIXED_LINE, cjk, _thai_only(thai))
    got = _ink_bbox(fixed)[1]
    assert abs(got - want) <= INK_TOLERANCE_PX, \
        f"测量与实际墨迹不符（换行会算错）：测量 {want:.1f}px，实画 {got}px"
    # 单字体测量口径必须与 run-aware 不同，否则「测量跟着字体走」这条是空话
    naive = d_probe.textlength(MIXED_LINE, font=cjk)
    assert abs(naive - want) > INK_TOLERANCE_PX, \
        f"run-aware 测量({want:.1f}) 与 CJK 单字体测量({naive:.1f}) 相同，判据无分辨力"
    print(f"  ✓ 中泰混排：位图≠豆腐块版本；泰文 {len(_thai_chars(MIXED_LINE))} 码位全覆盖；"
          f"测量 {want:.0f}px / 实画 {got}px（CJK 单字体口径 {naive:.0f}px，确有差别）")


def _hang_allowance(d: ImageDraw.ImageDraw, ln: str,
                    cjk: ImageFont.FreeTypeFont,
                    thai: ImageFont.FreeTypeFont) -> float:
    """避头尾允许的溢出量：行尾**悬挂一个禁则标点**是 `wrap_text` 的既定设计
    （「行尾溢出一点也比行首标点好读」，见 overlay.CLOSING_PUNCT 注释），不是超宽 bug。

    窄栏（如 220px）下这个悬挂必然出现，判据得给它留出正好一个标点的宽度 ——
    多留就会漏掉真的溢出。
    """
    if len(ln) > 1 and ln[-1] in CLOSING_PUNCT:
        return _measure_line(d, ln[-1], cjk, _thai_only(thai))
    return 0.0


def test_mixed_wrap_respects_max_width() -> None:
    """★ 混排长句折行：**每一行实画出来的墨迹都不许超出 max_w**（量错字体 = 溢出面板）。"""
    if _no_thai_font():
        return _skip("test_mixed_wrap_respects_max_width")
    cjk, thai = _fonts()
    d_probe = ImageDraw.Draw(_blank())
    text = ("这是一段中文和泰文混排的长句子，" + THAI_WITH_LATIN
            + "，看看会不会溢出面板宽度。" * 2)
    assert _thai_chars(text) and any(_is_hanzi(c) for c in text)
    report = []
    for max_w in (900, 400, 220):
        lines = wrap_text(d_probe, text, cjk, max_w, script_fonts=_thai_only(thai))
        assert len(lines) >= 2, f"max_w={max_w} 时没有折行（{len(lines)} 行）"
        for ln in lines:
            slack = _hang_allowance(d_probe, ln, cjk, thai)
            m = _measure_line(d_probe, ln, cjk, _thai_only(thai))
            assert m <= max_w + slack + 1, \
                f"max_w={max_w}：测量就超宽 {m:.0f}px（允许的悬挂 {slack:.0f}px）：{ln[:24]!r}"
            ink = _ink_bbox(_render(lambda dd, t=ln: _draw_line(dd, ORIGIN, t, cjk,
                                                               _thai_only(thai),
                                                               fill=255)))[1]
            assert ink <= max_w + slack + INK_TOLERANCE_PX, \
                f"max_w={max_w}：实画墨迹 {ink}px 超出面板（悬挂 {slack:.0f}px）：{ln[:24]!r}"
        report.append(f"{max_w}px→{len(lines)}行")
    # 对照：不传 thai_font（= 改动前的测量口径）时，混排行**量错**了。
    # ⚠️ 不能只数「有几行溢出」：错误方向取决于 CJK 字体的缺字框比真泰文字形宽还是窄。
    #    本机 msyh.ttc 的缺字框更宽 → 旧口径**高估**、行变短留白；换个缺字框更窄的字体
    #    就会**低估** → 真的溢出面板。所以钉「量错」这个事实本身，方向只做汇报。
    naive_lines = wrap_text(d_probe, text, cjk, 400)
    assert naive_lines, "对照用例没折出行"
    errs = []
    for ln in naive_lines:
        if not _thai_chars(ln):
            continue
        naive_m = _measure_line(d_probe, ln, cjk, None)          # 旧口径：一律用 CJK 字体量
        real = _measure_line(d_probe, ln, cjk, _thai_only(thai))  # 真实：按 run 各用各的字体
        errs.append((ln, naive_m - real))
    assert errs, "混排长句里一行含泰文的都没折出来？对照失效"
    worst_ln, worst = max(errs, key=lambda e: abs(e[1]))
    assert abs(worst) > INK_TOLERANCE_PX, \
        (f"旧口径与 run-aware 测量在混排行上没有差别（最大差 {worst:.1f}px）"
         " —— 「测量跟着字体走」这条改动没有可观测效果，判据无分辨力")
    direction = "高估→行变短留白" if worst > 0 else "低估→实画溢出面板"
    print(f"  ✓ 混排折行：{'，'.join(report)}，每行测量与实画墨迹均不超宽（含避头尾悬挂余量）"
          f"；旧口径在 {len(errs)} 行混排上量错，最大偏差 {abs(worst):.0f}px"
          f"（本机方向：{direction}；最差行 {worst_ln[:20]!r}）")


def test_mixed_line_end_to_end_in_panel() -> None:
    """端到端：把中泰混排的句子塞进 `render_panel` / `render_conversation`，
    面板里泰文那段必须与「强制用 CJK 字体画」的结果不同（= 没出豆腐块），且尺寸正确。"""
    if _no_thai_font():
        return _skip("test_mixed_line_end_to_end_in_panel")
    cfg = OverlayConfig()
    img = render_panel(MIXED_LINE, MIXED_LINE, cfg)
    assert img.size == cfg.size_px, img.size
    assert img.getextrema()[3][1] > 0, "面板全透明"

    # 面板里还有底板/边框/分隔线，整图比会被噪声淹没 → 按面板实际用的字号逐行比
    inner_w = cfg.size_px[0] - 48
    d_probe = ImageDraw.Draw(_blank())
    tf, tf_thai = _fonts_at(cfg.font_size)
    checked = 0
    for ln in wrap_text(d_probe, MIXED_LINE, tf, inner_w, script_fonts=_thai_only(tf_thai)):
        if not _thai_chars(ln):
            continue
        a = _render(lambda d, t=ln: _draw_line(d, ORIGIN, t, tf, _thai_only(tf_thai), fill=255))
        b = _render(lambda d, t=ln: _draw_line(d, ORIGIN, t, tf, _thai_only(tf), fill=255))
        assert a.tobytes() != b.tobytes(), f"面板里这一行的泰文仍是豆腐块：{ln[:24]!r}"
        checked += 1
    assert checked, f"混排句在面板字号下一行含泰文的都没折出来？{MIXED_LINE!r}"

    conv = render_conversation([("mine", MIXED_LINE, MIXED_LINE),
                                ("peer:abc", THAI_SENTENCE, "你好，很高兴认识你")], cfg)
    assert conv.size == cfg.size_px, conv.size
    assert conv.getextrema()[3][1] > 0, "对话视图全透明"
    print(f"  ✓ 端到端：render_panel / render_conversation 里 {checked} 行中泰混排均非豆腐块"
          f"（{cfg.size_px}，字号 {cfg.font_size}）")


def test_right_anchor_mixed_line_stays_inside() -> None:
    """`render_conversation` 的「我说」靠右对齐（anchor="ra"）：混排行右对齐后
    最左墨迹不许跑到 x<0（会被裁掉），最右墨迹要落在锚点上。

    ⚠️ 锚点必须离画布左边足够远：整行实测 ~1100px，锚点写小了行会从左边画出去，
    墨迹被画布裁掉，判据就成了「比较两张都被裁过的图」。
    """
    if _no_thai_font():
        return _skip("test_right_anchor_mixed_line_stays_inside")
    cjk, thai = _fonts()
    want = _measure_line(ImageDraw.Draw(_blank()), MIXED_LINE, cjk, _thai_only(thai))
    assert RIGHT_ANCHOR_X - want >= 0, \
        f"用例本身有问题：锚点 {RIGHT_ANCHOR_X} 放不下 {want:.0f}px 的行，会被画布裁掉"
    img = _render(lambda d: _draw_line(d, (RIGHT_ANCHOR_X, ORIGIN[1]), MIXED_LINE, cjk,
                                       _thai_only(thai), fill=255, anchor="ra"))
    bb = img.getbbox()
    assert bb, "右对齐混排行没有墨迹"
    left, right = bb[0], bb[2]
    assert left >= 0, f"右对齐后左侧被裁掉：最左墨迹 x={left}"
    assert abs(right - RIGHT_ANCHOR_X) <= INK_TOLERANCE_PX, \
        f"右对齐没落在锚点上：最右墨迹 {right}，锚点 {RIGHT_ANCHOR_X}"
    assert abs((right - left) - want) <= INK_TOLERANCE_PX * 2, \
        f"右对齐混排行宽度异常：{right - left}px，测量 {want:.1f}px"
    # 与左对齐版本比：同一行、同样的 run 切分，只是整体平移 → 墨迹宽度必须一致
    left_img = _render(lambda d: _draw_line(d, ORIGIN, MIXED_LINE, cjk,
                                            _thai_only(thai), fill=255))
    lb = left_img.getbbox()
    assert abs((right - left) - (lb[2] - lb[0])) <= INK_TOLERANCE_PX, \
        f"右对齐与左对齐的行宽不一致：{right - left}px vs {lb[2] - lb[0]}px"
    print(f"  ✓ 右对齐混排行：墨迹 x∈[{left}, {right}]，锚点 {RIGHT_ANCHOR_X}，"
          f"测量 {want:.0f}px，与左对齐行宽一致")


def test_thai_only_line_uses_thai_font() -> None:
    """纯泰文行（单 run）也必须走泰文字体 —— 不能因为「只有一个 run」就用了 CJK 字体。"""
    if _no_thai_font():
        return _skip("test_thai_only_line_uses_thai_font")
    cjk, thai = _fonts()
    got = _render(lambda d: _draw_line(d, ORIGIN, THAI_SENTENCE, cjk, _thai_only(thai),
                                       fill=255))
    ref_thai = _render(lambda d: d.text(ORIGIN, THAI_SENTENCE, font=thai, fill=255,
                                        anchor="la"))
    ref_tofu = _render(lambda d: d.text(ORIGIN, THAI_SENTENCE, font=cjk, fill=255,
                                        anchor="la"))
    assert got.tobytes() == ref_thai.tobytes(), "纯泰文行没有用泰文字体画"
    assert got.tobytes() != ref_tofu.tobytes(), "纯泰文行与 CJK 字体（豆腐块）结果相同"
    print(f"  ✓ 纯泰文行走泰文字体（墨迹 {_ink_bbox(got)[1]}px，与豆腐块版本不同）")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_tofu_probe_has_teeth,
        test_thai_font_renders_real_glyphs,
        test_thai_font_resolved_through_platform_facade,
        test_cjk_font_not_swapped_for_thai,
        test_pure_cjk_panel_bitmap_unchanged,
        test_mixed_line_uses_both_fonts,
        test_mixed_wrap_respects_max_width,
        test_mixed_line_end_to_end_in_panel,
        test_right_anchor_mixed_line_stays_inside,
        test_thai_only_line_uses_thai_font,
    ]
    print("test_thai_font:")
    print(f"  环境：CJK={Path(resolve_font_path('') or '?').name} "
          f"泰文={Path(resolve_thai_font_path('') or '（未找到）').name}")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed}/{len(tests)} 个用例失败")
        return 1
    ran = len(tests) - len(_SKIPPED)
    if _SKIPPED:
        print(f"ALL PASSED（跑 {ran}/{len(tests)} 个用例，跳过 {len(_SKIPPED)} 条："
              f"{'、'.join(_SKIPPED)}）—— 跳过原因：{_NO_FONT_WHY}")
    else:
        print(f"ALL PASSED（{len(tests)} 个用例：豆腐块判据 / 泰文字体×2 / CJK 行为不变×2 / "
              "中泰混排×3 / 右对齐 / 纯泰文）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
