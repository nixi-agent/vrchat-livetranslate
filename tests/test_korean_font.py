#!/usr/bin/env python
"""韩文字体覆盖 + 中韩混排渲染的回归测试（离线、不碰 VR 运行时）。

用法：.venv/Scripts/python.exe tests/test_korean_font.py

## 为什么必须有这个测试

用户 2026-10 报：**韩语译文在手腕屏 / 桌面字幕上显示成一排方框**。
实测根因与泰文同因：默认 CJK 字体 `C:/Windows/Fonts/msyh.ttc`（微软雅黑）**不含韩文字形** ——
用 PIL 渲染 `안녕하세요` 得到的位图与渲染「私有区缺字位 U+E000 × 5」的位图
**逐字节完全相同**（= 豆腐块，本文件把这条判据固化成断言；`out/render_ko_demo.py` 出的
`panel_ko.png` 就是用户看到的那个样子）。

而这类 bug 是**静默**的：没有异常、没有告警，界面上什么都对，只有贴图是方块。
所以必须用位图比对钉住，光靠「跑一遍不崩」根本发现不了。
（泰文走的是同一套「按书写系统切 run、各用各的字体」的机制，由 `tests/test_thai_font.py` 钉；
两个文件共用机制、不共用用例。）

## ⚠️ 平台差异决定了本文件的判据必须**条件化**

* **Windows**：CJK 字体 = msyh.ttc，**不含韩文** → 缺韩文字体就是一排方框（本次修复的现场）；
* **Linux**：`find_cjk_font()` 常见落点是 Noto Sans CJK（**pan-CJK 族，含韩文**），
  于是「解析出的韩文字体」可能**就是同一份文件** —— 此时「换了字体」这件事在本机
  **不可观测**，相关断言会**明确跳过并打印原因**（口径同 `tests/test_wayland_window.py`
  缺 sway 时），而不是判红。真正要钉死的是「韩文落在韩文格、别蹭主字体」这条接线。
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
    resolve_korean_font_path,
    wrap_text,
)

# 缺字位（Private Use Area，任何字体都不该有真字形）→ 渲染出来就是豆腐块。
TOFU = "\uE000"
KO_WORD = "안녕하세요"                  # 5 个韩文音节（正文里的那一段码位）
KO_JAMO = "ㄱㄴㄷ"                      # 3 个**兼容字母**（U+3130–318F）：只认音节块会漏掉它
KO_CONJOINING = "\u1100\u1161\u11a8"   # 3 个**字母 Jamo**（U+1100–11FF）：NFD 形态的韩文
KO_SENTENCE = "안녕하세요저는니키입니다오늘도좋은하루되세요"     # 无空格 → 纯韩文单 run
KO_WITH_LATIN = "안녕하세요 저는 Nixi입니다"                   # 混拉丁（真实译文里常有专名）
CJK_WORD = "你好，我是逆袭"
CJK_SENTENCE = "你好，我是逆袭。这句话正在被实时翻译。"
MIXED_LINE = "你好，안녕하세요，我是 Nixi —— 中韩混排不能出方块"
FONT_SIZE = 48
CANVAS = (1400, 160)                 # 够宽够高，避免裁切影响 bbox 判据
ORIGIN = (10, 20)
RIGHT_ANCHOR_X = CANVAS[0] - 100     # 右对齐用例的锚点（留出整行宽度，别蹭到画布左边）
INK_TOLERANCE_PX = 3                 # run-aware 绘制的最右墨迹与测量值的实测差在 ±1px 内


# ---------------------------------------------------------------- 小工具（判据独立于实现）


def _korean_only(font: ImageFont.FreeTypeFont) -> dict[str, ImageFont.FreeTypeFont]:
    """「只带韩文那一格」的专用字体字典（生产入口是 `load_script_fonts(cfg, size)`）。"""
    return {"hangul": font}


def _is_hangul_char(ch: str) -> bool:
    """韩文音节 + 字母（含兼容字母）。**刻意不复用** `overlay._script_of`。"""
    o = ord(ch)
    return (0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F
            or 0xAC00 <= o <= 0xD7A3)


def _is_hanzi(ch: str) -> bool:
    return 0x4E00 <= ord(ch) <= 0x9FFF


def _hangul_chars(text: str) -> list[str]:
    return [c for c in text if _is_hangul_char(c)]


def _blank() -> Image.Image:
    """灰度画布：豆腐块判定只看字形墨迹，颜色/透明度无关。"""
    return Image.new("L", CANVAS, 0)


def _render(draw_fn) -> Image.Image:
    img = _blank()
    draw_fn(ImageDraw.Draw(img))
    return img


def _is_tofu(font: ImageFont.FreeTypeFont, text: str) -> bool:
    """`text` 用 `font` 画出来是不是豆腐块 —— 与「同样个数的缺字位」逐字节比对。"""
    if not text:
        return False
    ref = _render(lambda d: d.text(ORIGIN, TOFU * len(text), font=font, fill=255))
    got = _render(lambda d: d.text(ORIGIN, text, font=font, fill=255))
    return ref.tobytes() == got.tobytes()


def _ink_bbox(img: Image.Image) -> tuple[int, int]:
    """相对 ORIGIN 的 (最左, 最右) 墨迹位置（px）。空图返回 (0, 0)。"""
    bb = img.getbbox()
    return (bb[0] - ORIGIN[0], bb[2] - ORIGIN[0]) if bb else (0, 0)


# 本机解析出来的三份字体：主字体（CJK）/ 韩文 / 泰文。
CJK_FONT_PATH: str | None = resolve_font_path("")
KO_FONT_PATH: str | None = resolve_korean_font_path("")
THAI_FONT_PATH: str | None = overlay.resolve_thai_font_path("")

# 韩文字体与主字体是**同一份文件**（Linux 上 Noto Sans CJK 既有中日韩、也有韩文）→
# 「韩文换了字体」在本机不可观测。相关断言跳过并打印原因，不判红。
KO_IS_CJK = bool(KO_FONT_PATH and CJK_FONT_PATH
                 and Path(KO_FONT_PATH) == Path(CJK_FONT_PATH))

_SKIPPED: list[str] = []
_FONT_WHY = ("本机没有含韩文字形的字体（装了韩文字体后会自动跑；"
             "Windows 自带 malgun.ttf，Linux 见 docs/GUIDE.linux.md 的韩语一节）")
_SAME_FONT_WHY = ("本机解析出的韩文字体与主字体是同一份文件"
                  "（Noto Sans CJK 这类 pan-CJK 字体自带韩文）→「换了字体」不可观测")


def _skip(name: str, why: str = "") -> None:
    _SKIPPED.append(name)
    print(f"  ⏭ 跳过 {name}：{why or _FONT_WHY}")


def _no_ko_font() -> bool:
    """True = 本机没有含韩文字形的字体，依赖它的用例应跳过。"""
    return not KO_FONT_PATH


def _fonts_at(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """(主字体, 韩文字体) @ 指定字号，都走平台门面解析 —— 不写死任何平台路径。"""
    assert CJK_FONT_PATH, "平台层没能解析出主字体，后续判据无从谈起"
    assert KO_FONT_PATH, (f"平台层没能解析出韩文字体"
                          f"（find_korean_font()={platform_mod.find_korean_font()!r}）"
                          " —— 韩语会渲染成豆腐块")
    return (ImageFont.truetype(CJK_FONT_PATH, size),
            ImageFont.truetype(KO_FONT_PATH, size))


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    return _fonts_at(FONT_SIZE)


@contextlib.contextmanager
def _stub_script_fonts(values: dict[str, str | None]):
    """让指定书写系统解析到固定值（None = 本机没有覆盖该书写系统的字体）。

    ⚠️ 打桩的是**通用入口** `overlay.resolve_script_font_path`（`resolve_korean_font_path`
    只是它的薄包装）—— 打桩包装函数在实现搬家时会静默失效；同时清缓存，
    否则上一次的真实结果会把打桩盖住。
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


# 夹具自检：谁改了夹具、把拉丁字母或空格塞进「纯韩文」样本，这里立刻红，
# 而不是让「豆腐块判据」悄悄失去分辨力。
assert KO_SENTENCE and all(_is_hangul_char(c) for c in KO_SENTENCE), \
    f"夹具 KO_SENTENCE 必须是纯韩文码位：{KO_SENTENCE!r}"
assert all(_is_hangul_char(c) for c in KO_WORD), f"夹具 KO_WORD 必须是纯韩文：{KO_WORD!r}"
assert all(_is_hangul_char(c) for c in KO_JAMO), f"夹具 KO_JAMO 必须是韩文字母：{KO_JAMO!r}"
assert all(0x1100 <= ord(c) <= 0x11FF for c in KO_CONJOINING), \
    f"夹具 KO_CONJOINING 必须是 U+1100–11FF 的字母 Jamo：{KO_CONJOINING!r}"
assert _hangul_chars(KO_WITH_LATIN) and not all(_is_hangul_char(c) for c in KO_WITH_LATIN), \
    f"夹具 KO_WITH_LATIN 必须韩文+其它书写系统混排：{KO_WITH_LATIN!r}"


# ---------------------------------------------------------------- ① 判据自身有分辨力


def test_tofu_probe_has_teeth() -> None:
    """★ 元测试：本机解析出的 **主字体渲染韩文必须是豆腐块**（Windows/msyh 就是本 bug 的现场）。

    这条不成立的话，下面「不是豆腐块」的断言都是空转。
    主字体**自带**韩文字形（Linux 的 Noto Sans CJK）属于环境特例：不算失败，但必须显式说出来 ——
    那种机器上本来就没这个 bug，真正的守卫是「韩文落韩文格」那条接线断言。
    """
    assert CJK_FONT_PATH, "平台层没解析出主字体"
    cjk = ImageFont.truetype(CJK_FONT_PATH, FONT_SIZE)
    if not _is_tofu(cjk, KO_WORD):
        print(f"  ⚠ 本机主字体（{Path(CJK_FONT_PATH).name}）自带韩文字形，"
              "「豆腐块判据」在本机没有分辨力（环境特例，不判失败）")
        return
    assert _is_tofu(cjk, KO_SENTENCE), "换个更长的韩文句子判据就失效了？"
    if KO_FONT_PATH:
        ko = ImageFont.truetype(KO_FONT_PATH, FONT_SIZE)
        assert not _is_tofu(ko, KO_WORD), \
            f"韩文字体（{Path(KO_FONT_PATH).name}）渲染 {KO_WORD!r} 也是豆腐块 —— 判据认不出对照物"
    assert not _is_tofu(cjk, CJK_WORD), \
        f"主字体连 {CJK_WORD!r} 都渲染成豆腐块 —— 参照物选错了，判据不可用"
    print(f"  ✓ 判据有分辨力：主字体 {Path(CJK_FONT_PATH).name} 渲染 {KO_WORD!r} "
          f"== 渲染 {TOFU!r}×{len(KO_WORD)}（中文非豆腐块；韩文字体在场时结果相反）")


# ---------------------------------------------------------------- ② 韩文字体真的有韩文字形


def test_korean_font_renders_real_glyphs() -> None:
    """★ 解析出来的韩文字体渲染韩文，位图**不等于**缺字位位图（整词 + 逐字两种口径）。"""
    if _no_ko_font():
        return _skip("test_korean_font_renders_real_glyphs")
    _, ko = _fonts()
    assert not _is_tofu(ko, KO_WORD), \
        f"韩文字体渲染 {KO_WORD!r} 仍是豆腐块（== {TOFU!r}×{len(KO_WORD)}）"
    bad = sorted({f"U+{ord(c):04X}" for c in _hangul_chars(KO_SENTENCE + KO_JAMO)
                  if _is_tofu(ko, c)})
    assert not bad, f"韩文字体缺这些韩文字形：{bad}"
    got = _render(lambda d: d.text(ORIGIN, KO_WORD, font=ko, fill=255))
    assert got.getbbox(), f"韩文字体渲染 {KO_WORD!r} 全空白（一个墨迹像素都没有）"
    print(f"  ✓ 韩文字体渲染真字形：整词 + {len(set(_hangul_chars(KO_SENTENCE + KO_JAMO)))} "
          f"个不同韩文码位（含兼容字母）均非豆腐块，{KO_WORD!r} 墨迹 {_ink_bbox(got)[1]}px")


def test_korean_font_resolved_through_platform_facade() -> None:
    """韩文字体必须经 `vlt.platform.find_korean_font()` 门面解析，且落在真实存在的文件上。

    顺带钉住降级留痕（禁静默）：门面返回 None 时 `resolve_korean_font_path` 也要返回 None
    并**打一行含「韩文」的告警**（只打一次），渲染不许崩。
    """
    path = resolve_korean_font_path("")
    if path:
        assert Path(path).exists(), f"韩文字体路径无效：{path!r}"
        assert path == platform_mod.find_korean_font(), \
            (f"解析结果与门面不一致（overlay 自己写了平台路径？）："
             f"{path!r} vs {platform_mod.find_korean_font()!r}")
        src = (ROOT / "vlt" / "output" / "overlay.py").read_text(encoding="utf-8")
        assert Path(path).name not in src, \
            f"共享模块 overlay.py 里出现了具体韩文字体文件名 {Path(path).name!r}（平台路径必须留在 vlt/platform/）"
    else:
        print("  ⏭ 本机没有韩文字体 → 跳过「路径有效 / 与门面一致 / 不在共享模块里」三条；"
              "降级留痕那半条照跑")

    saved_warned = set(overlay._script_font_warned)
    orig_facade = platform_mod.find_korean_font
    try:
        overlay._script_font_cache.clear()
        overlay._script_font_warned.discard("hangul")
        platform_mod.find_korean_font = lambda: None      # type: ignore[assignment]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = resolve_korean_font_path("")
        assert got is None, f"门面找不到字体时应返回 None（让调用方降级），实际 {got!r}"
        assert "韩文" in buf.getvalue(), \
            f"降级必须留痕（禁静默）且写明是韩文，实际输出：{buf.getvalue()!r}"
        # 告警只打一次，不许刷屏
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            resolve_korean_font_path("")
        assert "韩文" not in buf2.getvalue(), "同一条降级告警打了第二次（刷屏）"
        # 降级后渲染不许崩（回落主字体 → 该段是豆腐块，已留痕）
        cfg = OverlayConfig()
        img = render_panel(KO_SENTENCE, CJK_WORD, cfg)
        assert img.size == cfg.size_px
        assert img.getextrema()[3][1] > 0
    finally:
        platform_mod.find_korean_font = orig_facade       # type: ignore[assignment]
        overlay._script_font_cache.clear()
        overlay._script_font_warned.clear()
        overlay._script_font_warned.update(saved_warned)
    assert resolve_korean_font_path("") == path, "打桩复原后解析结果应回到真实字体"
    if path:
        print(f"  ✓ 韩文字体经平台门面解析（{Path(path).name}），overlay.py 不含该文件名；"
              "门面返回 None 时降级留痕（只一次、写明韩文）且渲染不崩")
    else:
        print("  ✓ 本机无韩文字体：门面返回 None 时降级留痕（只一次、写明韩文）且渲染不崩"
              "（解析路径那一半已跳过）")


# ---------------------------------------------------------------- ③ 接线：韩文必须落「韩文格」


def test_every_script_has_a_platform_finder() -> None:
    """★ 接线（**环境无关**）：`_SCRIPT_FINDERS` 里每个书写系统，平台两端都必须真有实现。

    缺了它 = 那门语言永远走降级分支（方框 + 一行告警），而「没接线」这件事在**装了字体的
    开发机**上完全看不见（解析成功、位图正确、用例全绿）—— 所以本条把注册表与平台层钉在一起：
    ⚠️ 它也是「只加了 `_SCRIPT_RANGES` 却忘了在 `vlt/platform/{win,linux}.py` 与门面里补
    `find_<script>_font`」这类漏接线的唯一守卫。
    """
    assert overlay._SCRIPT_FINDERS, "书写系统注册表是空的？"
    for script, finder in overlay._SCRIPT_FINDERS.items():
        fn = getattr(platform_mod, finder, None)
        assert callable(fn), \
            f"{script} 的门面函数 vlt.platform.{finder} 不存在（平台层漏接线 → 该语言永远方框）"
        backend = platform_mod.backend()
        assert callable(getattr(backend, finder, None)), \
            f"{type(backend).__name__} 里没有 {finder}（只有门面壳子，没接平台实现）"
    print(f"  ✓ 注册表 {sorted(overlay._SCRIPT_FINDERS)} 的探测函数在门面与 "
          f"{type(platform_mod.backend()).__name__} 里都真实存在")


def test_hangul_runs_go_to_the_hangul_slot() -> None:
    """★ 接线判据（**不依赖本机有没有韩文字体**）：韩文字符落 `hangul` 槽，别的字符不许蹭。

    做法：给 `hangul` 槽塞一个**字号明显不同**的字体 —— 宽度就能分辨走的是哪一格。
    （生产入口 `load_script_fonts()` 解析出来的韩文字体在 Linux 上可能与主字体同文件，
    那时位图口径不可观测，这条宽度口径仍有效。）
    """
    assert CJK_FONT_PATH, "平台层没解析出主字体"
    cjk = ImageFont.truetype(CJK_FONT_PATH, FONT_SIZE)
    big = ImageFont.truetype(CJK_FONT_PATH, FONT_SIZE * 2)      # 只换字号 → 宽度可辨
    d = ImageDraw.Draw(_blank())

    got = _measure_line(d, KO_WORD, cjk, {"hangul": big})
    want = d.textlength(KO_WORD, font=big)
    assert abs(got - want) < 0.01, \
        f"纯韩文行没走韩文格：量出 {got:.1f}px，按韩文格应 {want:.1f}px（主字体口径 {d.textlength(KO_WORD, font=cjk):.1f}px）"

    # 韩文有三档码位：音节 / 兼容字母 / 字母 Jamo —— 三档都得落韩文格
    for sample in (KO_JAMO, KO_CONJOINING):
        got = _measure_line(d, sample, cjk, {"hangul": big})
        want = d.textlength(sample, font=big)
        assert abs(got - want) < 0.01, \
            (f"{sample!r} 没走韩文格：量出 {got:.1f}px，按韩文格应 {want:.1f}px"
             "（兼容字母 U+3130–318F 与字母 Jamo U+1100–11FF 都要覆盖）")

    got = _measure_line(d, CJK_WORD, cjk, {"hangul": big})
    want = d.textlength(CJK_WORD, font=cjk)
    assert abs(got - want) < 0.01, \
        f"中文行被韩文格污染了：量出 {got:.1f}px，按主字体应 {want:.1f}px"

    # 混排 = 两段各按自己的格量（拉丁/标点算主字体，与改动前口径一致）
    got = _measure_line(d, MIXED_LINE, cjk, {"hangul": big})
    want = sum(d.textlength(chunk, font=(big if _is_hangul_char(chunk[0]) else cjk))
               for chunk in _runs_by_fixture(MIXED_LINE))
    assert abs(got - want) < 0.01, \
        f"混排行没按书写系统分段量：量出 {got:.1f}px，按「各用各的格」应 {want:.1f}px"

    # 没给的那一格（这里没给泰文）→ 回落主字体，不许借用别的格
    got = _measure_line(d, CJK_WORD + "x", cjk, {"hangul": big})
    assert abs(got - d.textlength(CJK_WORD + "x", font=cjk)) < 0.01, \
        "主字体那一段被其它书写系统的格抢走了"
    print(f"  ✓ 韩文落韩文格（{KO_WORD!r} → 该格字体；中文/拉丁仍走主字体；混排两段各按各的量）")


def test_production_font_dict_and_panel_path() -> None:
    """★ 生产入口：`load_script_fonts()` 必须把**已解析到的**书写系统真的装进字典，
    且 `render_panel()` 这条用户实际走的路必须用上它。

    为什么单列：前面那些用例大多**手搭** `{"hangul": ...}` 字典 —— 那证明得了「切 run + 绘制」，
    证明不了「render_panel → load_script_fonts 这条路拿到了韩文格」。
    （实测：把 `load_script_fonts` 的循环写成只遍历泰文，手搭字典的用例全绿。）
    """
    cfg = OverlayConfig()
    fonts = load_script_fonts(cfg, FONT_SIZE)
    for script in overlay._SCRIPT_FINDERS:
        path = overlay.resolve_script_font_path(script, cfg.font)
        if path:
            assert script in fonts, \
                f"{script} 已解析到 {Path(path).name}，但 load_script_fonts() 的字典里没有它"
        else:
            assert script not in fonts, f"{script} 没解析到字体却出现在字典里：{fonts[script]!r}"

    if _no_ko_font():
        print(f"  ⏭ 生产路径位图那半条跳过：{_FONT_WHY}（字典一致性那半条照跑）")
        return
    assert "hangul" in fonts, "生产字典里没有韩文格（韩文字体已解析到）"
    if KO_IS_CJK:
        print(f"  ⏭ 生产路径位图那半条跳过：{_SAME_FONT_WHY}")
        return
    ok = render_panel(KO_WORD, "", cfg)
    with _stub_script_fonts({"hangul": None}):
        degraded = render_panel(KO_WORD, "", cfg)
    assert ok.tobytes() != degraded.tobytes(), \
        "render_panel（生产路径）没用上韩文字体：面板与「韩文格为空」时一模一样"
    conv = render_conversation([("theirs", "", KO_WORD)], cfg)
    with _stub_script_fonts({"hangul": None}):
        conv_degraded = render_conversation([("theirs", "", KO_WORD)], cfg)
    assert conv.tobytes() != conv_degraded.tobytes(), \
        "render_conversation（生产路径）没用上韩文字体"
    print(f"  ✓ 生产入口：load_script_fonts() 的字典含 {sorted(fonts)}，"
          f"render_panel / render_conversation 的韩文位图与「韩文格为空」时不同")


def _runs_by_fixture(text: str) -> list[str]:
    """按「是不是韩文」切段（**夹具自己的实现**，不用被测模块的 `_split_script_runs`）。"""
    runs: list[str] = []
    for ch in text:
        if runs and _is_hangul_char(runs[-1][0]) == _is_hangul_char(ch):
            runs[-1] += ch
        else:
            runs.append(ch)
    return runs


# ---------------------------------------------------------------- ④ 主字体渲染行为不变


def test_cjk_font_not_swapped_for_korean() -> None:
    """★ 防回退：不能为了韩语把默认 CJK 字体换掉。

    判据：① `resolve_font_path("")` 仍等于 `find_cjk_font()`；
    ② 非韩文文本走 `_draw_line` 与改动前的写法 `d.text(..., font=cjk, anchor="la")`
       逐字节相同 —— 字体字典取**生产口径** `load_script_fonts(cfg, size)`。

    ⚠️ 样本里**不放韩文**：韩文在生产口径下本来就该换字体（那是修复）。
    """
    assert CJK_FONT_PATH and getattr(overlay, "resolve_font_path", None)
    assert CJK_FONT_PATH == platform_mod.find_cjk_font(), \
        (f"默认字体被换了：resolve_font_path('')={CJK_FONT_PATH!r} "
         f"find_cjk_font()={platform_mod.find_cjk_font()!r}")
    cjk = ImageFont.truetype(CJK_FONT_PATH, FONT_SIZE)
    cfg = OverlayConfig()
    fonts = load_script_fonts(cfg, FONT_SIZE)
    samples = [CJK_WORD, CJK_SENTENCE, "こんにちは、ニキシです", "Привет! Я из России.",
               "Hello, world — 123."]
    for text in samples:
        ref = _render(lambda d, t=text: d.text(ORIGIN, t, font=cjk, fill=255, anchor="la"))
        got = _render(lambda d, t=text: _draw_line(d, ORIGIN, t, cjk, fonts, fill=255))
        assert ref.tobytes() == got.tobytes(), \
            f"非韩文文本 {text[:12]!r} 的渲染位图变了（主字体行为被韩文改动波及）"
    assert not _is_tofu(cjk, CJK_WORD), \
        "前提不成立：主字体连中文都是豆腐块，上面的位图相同断言没有意义"
    print(f"  ✓ 默认字体仍是 find_cjk_font()；{len(samples)} 条中/日/俄/拉丁样本"
          f"在**生产口径**（{sorted(fonts) or '空字典'}）下单 run 渲染与改动前逐字节相同")


def test_pure_cjk_panel_bitmap_unchanged() -> None:
    """★ 端到端：纯中日韩面板在「韩文字体解析成功」与「解析失败(None)」两种情况下
    位图**逐字节一致** —— 韩语那条腿对不用韩语的用户是零影响的。"""
    cfg = OverlayConfig()
    text, source = CJK_SENTENCE, "こんにちは、ニキシです。"

    with_ko = render_panel(text, source, cfg)
    with _stub_script_fonts({"hangul": None}):
        without = render_panel(text, source, cfg)
    assert with_ko.tobytes() == without.tobytes(), \
        "纯 CJK 面板的位图因韩文字体逻辑而改变（主字体渲染行为必须不变）"
    assert with_ko.size == cfg.size_px, with_ko.size
    assert with_ko.getextrema()[3][1] > 0, "面板全透明，什么都没画出来"

    entries = [("mine", source, text), ("theirs", "Привет! Я из России.", "你好，我是逆袭")]
    conv_a = render_conversation(entries, cfg)
    with _stub_script_fonts({"hangul": None}):
        conv_b = render_conversation(entries, cfg)
    assert conv_a.tobytes() == conv_b.tobytes(), \
        "纯 CJK 对话视图的位图因韩文字体逻辑而改变"
    assert conv_a.size == cfg.size_px, conv_a.size
    assert conv_a.getextrema()[3][1] > 0, "对话视图全透明"
    print(f"  ✓ render_panel / render_conversation 的纯 CJK 位图"
          f"与「无韩文字体」时逐字节相同（{cfg.size_px}）")


# ---------------------------------------------------------------- ⑤ 中韩混排不出豆腐块


def test_mixed_line_uses_both_fonts() -> None:
    """★ 同一行里既有中文又有韩文：韩文那段必须用韩文字体画，且整行不出豆腐块。

    判据（三条互相独立）：
    ① run-aware 绘制的位图 **≠** 把韩文字体换成主字体（= 修复前的方框效果）的位图；
    ② 混排行里每个韩文码位单独用韩文字体渲染都不是豆腐块；
    ③ 整行墨迹非空，且墨迹宽度与 `_measure_line` 的测量值吻合（测量跟着字体走）。

    ⚠️ 本机韩文字体与主字体同文件时，①③ 不可观测 → 明确跳过（见文件头平台差异一节）。
    """
    if _no_ko_font():
        return _skip("test_mixed_line_uses_both_fonts")
    cjk, ko = _fonts()
    assert _hangul_chars(MIXED_LINE) and any(_is_hanzi(c) for c in MIXED_LINE), \
        "用例本身必须同时含中文与韩文"

    fixed = _render(lambda d: _draw_line(d, ORIGIN, MIXED_LINE, cjk, _korean_only(ko), fill=255))
    if KO_IS_CJK:
        print(f"  ⏭ 跳过「位图≠方框版本」那半条：{_SAME_FONT_WHY}")
    else:
        before = _render(lambda d: _draw_line(d, ORIGIN, MIXED_LINE, cjk, _korean_only(cjk),
                                              fill=255))
        assert fixed.tobytes() != before.tobytes(), \
            "混排行渲染与「韩文用主字体画」完全相同 —— 韩文字体没被用上，韩文全是方框"

    bad = sorted({f"U+{ord(c):04X}" for c in _hangul_chars(MIXED_LINE) if _is_tofu(ko, c)})
    assert not bad, f"混排行里这些韩文码位会出豆腐块：{bad}"

    assert fixed.getbbox(), "混排行整行没有墨迹"
    d_probe = ImageDraw.Draw(_blank())
    want = _measure_line(d_probe, MIXED_LINE, cjk, _korean_only(ko))
    got = _ink_bbox(fixed)[1]
    assert abs(got - want) <= INK_TOLERANCE_PX, \
        f"测量与实际墨迹不符（换行会算错）：测量 {want:.1f}px，实画 {got}px"
    naive = d_probe.textlength(MIXED_LINE, font=cjk)
    if abs(naive - want) > INK_TOLERANCE_PX:
        extra = f"（主字体单字体口径 {naive:.0f}px，确有差别）"
    else:
        extra = "（本机韩文格与主字体量宽相同，这一条在本机没有分辨力）"
    print(f"  ✓ 中韩混排：韩文 {len(_hangul_chars(MIXED_LINE))} 码位全覆盖；"
          f"测量 {want:.0f}px / 实画 {got}px{extra}")


def _hang_allowance(d: ImageDraw.ImageDraw, ln: str,
                    cjk: ImageFont.FreeTypeFont,
                    ko: ImageFont.FreeTypeFont) -> float:
    """避头尾允许的溢出量：行尾**悬挂一个禁则标点**是 `wrap_text` 的既定设计，
    不是超宽 bug（口径同 `tests/test_thai_font.py`）。

    窄栏（如 220px）下这个悬挂必然出现，判据得给它留出正好一个标点的宽度。
    """
    if len(ln) > 1 and ln[-1] in CLOSING_PUNCT:
        return _measure_line(d, ln[-1], cjk, _korean_only(ko))
    return 0.0


def test_mixed_wrap_respects_max_width() -> None:
    """★ 混排长句折行：**每一行实画出来的墨迹都不许超出 max_w**（量错字体 = 溢出面板）。"""
    if _no_ko_font():
        return _skip("test_mixed_wrap_respects_max_width")
    cjk, ko = _fonts()
    d_probe = ImageDraw.Draw(_blank())
    text = ("这是一段中文和韩文混排的长句子，" + KO_WITH_LATIN
            + "，看看会不会溢出面板宽度。" * 2)
    assert _hangul_chars(text) and any(_is_hanzi(c) for c in text)
    report = []
    for max_w in (900, 400, 220):
        lines = wrap_text(d_probe, text, cjk, max_w, script_fonts=_korean_only(ko))
        assert len(lines) >= 2, f"max_w={max_w} 时没有折行（{len(lines)} 行）"
        for ln in lines:
            slack = _hang_allowance(d_probe, ln, cjk, ko)
            m = _measure_line(d_probe, ln, cjk, _korean_only(ko))
            assert m <= max_w + slack + 1, \
                f"max_w={max_w}：测量就超宽 {m:.0f}px（允许的悬挂 {slack:.0f}px）：{ln[:24]!r}"
            ink = _ink_bbox(_render(lambda dd, t=ln: _draw_line(dd, ORIGIN, t, cjk,
                                                               _korean_only(ko),
                                                               fill=255)))[1]
            assert ink <= max_w + slack + INK_TOLERANCE_PX, \
                f"max_w={max_w}：实画墨迹 {ink}px 超出面板（悬挂 {slack:.0f}px）：{ln[:24]!r}"
        report.append(f"{max_w}px→{len(lines)}行")

    # 对照：不传韩文格（= 修复前的测量口径）时，含韩文的行会被量错 ——
    # 量错的方向取决于主字体的缺字框比真韩文字形宽还是窄，所以只钉「量错」这个事实本身。
    if KO_IS_CJK:
        print("  ⏭ 跳过「旧口径量错」对照："
              + _SAME_FONT_WHY + "，两种口径必然一致")
    else:
        naive_lines = wrap_text(d_probe, text, cjk, 400)
        errs = []
        for ln in naive_lines:
            if not _hangul_chars(ln):
                continue
            naive_m = _measure_line(d_probe, ln, cjk, None)          # 旧口径：一律用主字体
            real = _measure_line(d_probe, ln, cjk, _korean_only(ko))  # 真实：按 run 各用各的
            errs.append((ln, naive_m - real))
        assert errs, "混排长句里一行含韩文的都没折出来？对照失效"
        worst_ln, worst = max(errs, key=lambda e: abs(e[1]))
        assert abs(worst) > INK_TOLERANCE_PX, \
            (f"旧口径与 run-aware 测量在混排行上没有差别（最大差 {worst:.1f}px）"
             " —— 「测量跟着字体走」这条改动没有可观测效果，判据无分辨力")
        direction = "高估→行变短留白" if worst > 0 else "低估→实画溢出面板"
        report.append(f"旧口径在 {len(errs)} 行混排上量错，最大偏差 {abs(worst):.0f}px"
                      f"（本机方向：{direction}；最差行 {worst_ln[:20]!r}）")
    print(f"  ✓ 混排折行：{'，'.join(report)}，每行测量与实画墨迹均不超宽（含避头尾悬挂余量）")


def test_mixed_line_end_to_end_in_panel() -> None:
    """端到端：把中韩混排的句子塞进 `render_panel` / `render_conversation`，
    面板里韩文那段必须与「强制用主字体画」的结果不同（= 没出方框），且尺寸正确。"""
    if _no_ko_font():
        return _skip("test_mixed_line_end_to_end_in_panel")
    cfg = OverlayConfig()
    img = render_panel(MIXED_LINE, MIXED_LINE, cfg)
    assert img.size == cfg.size_px, img.size
    assert img.getextrema()[3][1] > 0, "面板全透明"

    inner_w = cfg.size_px[0] - 48
    d_probe = ImageDraw.Draw(_blank())
    tf, tf_ko = _fonts_at(cfg.font_size)
    checked = 0
    for ln in wrap_text(d_probe, MIXED_LINE, tf, inner_w, script_fonts=_korean_only(tf_ko)):
        if not _hangul_chars(ln) or KO_IS_CJK:
            continue
        a = _render(lambda d, t=ln: _draw_line(d, ORIGIN, t, tf, _korean_only(tf_ko), fill=255))
        b = _render(lambda d, t=ln: _draw_line(d, ORIGIN, t, tf, _korean_only(tf), fill=255))
        assert a.tobytes() != b.tobytes(), f"面板里这一行的韩文仍是方框：{ln[:24]!r}"
        checked += 1

    # 面板里画出来的像素必须与「逐行方案」一致：随便挑一行韩文，比对面板上的墨迹位置更麻烦，
    # 这里改为直接钉「面板非空 + 尺寸正确 + （可分辨时）逐行非方框」三条。
    conv = render_conversation([("mine", MIXED_LINE, MIXED_LINE),
                                ("peer:abc", KO_SENTENCE, "你好，很高兴认识你")], cfg)
    assert conv.size == cfg.size_px, conv.size
    assert conv.getextrema()[3][1] > 0, "对话视图全透明"
    if KO_IS_CJK:
        print(f"  ⏭ 跳过「面板里逐行非方框」那半条：{_SAME_FONT_WHY}（尺寸/非空两条照跑）")
    print(f"  ✓ 端到端：render_panel / render_conversation 尺寸与墨迹正确"
          f"（{checked} 行中韩混排逐行确认非方框；{cfg.size_px}，字号 {cfg.font_size}）")


def test_right_anchor_mixed_line_stays_inside() -> None:
    """`render_conversation` 的「我说」靠右对齐（anchor="ra"）：混排行右对齐后
    最左墨迹不许跑到 x<0（会被裁掉），最右墨迹要落在锚点上。"""
    if _no_ko_font():
        return _skip("test_right_anchor_mixed_line_stays_inside")
    cjk, ko = _fonts()
    want = _measure_line(ImageDraw.Draw(_blank()), MIXED_LINE, cjk, _korean_only(ko))
    assert RIGHT_ANCHOR_X - want >= 0, \
        f"用例本身有问题：锚点 {RIGHT_ANCHOR_X} 放不下 {want:.0f}px 的行，会被画布裁掉"
    img = _render(lambda d: _draw_line(d, (RIGHT_ANCHOR_X, ORIGIN[1]), MIXED_LINE, cjk,
                                       _korean_only(ko), fill=255, anchor="ra"))
    bb = img.getbbox()
    assert bb, "右对齐混排行没有墨迹"
    left, right = bb[0], bb[2]
    assert left >= 0, f"右对齐后左侧被裁掉：最左墨迹 x={left}"
    assert abs(right - RIGHT_ANCHOR_X) <= INK_TOLERANCE_PX, \
        f"右对齐没落在锚点上：最右墨迹 {right}，锚点 {RIGHT_ANCHOR_X}"
    assert abs((right - left) - want) <= INK_TOLERANCE_PX * 2, \
        f"右对齐混排行宽度异常：{right - left}px，测量 {want:.1f}px"
    left_img = _render(lambda d: _draw_line(d, ORIGIN, MIXED_LINE, cjk, _korean_only(ko),
                                            fill=255))
    lb = left_img.getbbox()
    assert abs((right - left) - (lb[2] - lb[0])) <= INK_TOLERANCE_PX, \
        f"右对齐与左对齐的行宽不一致：{right - left}px vs {lb[2] - lb[0]}px"
    print(f"  ✓ 右对齐混排行：墨迹 x∈[{left}, {right}]，锚点 {RIGHT_ANCHOR_X}，"
          f"测量 {want:.0f}px，与左对齐行宽一致")


def test_korean_only_line_uses_korean_font() -> None:
    """纯韩文行（单 run）也必须走韩文字体 —— 不能因为「只有一个 run」就用了主字体。"""
    if _no_ko_font():
        return _skip("test_korean_only_line_uses_korean_font")
    cjk, ko = _fonts()
    got = _render(lambda d: _draw_line(d, ORIGIN, KO_SENTENCE, cjk, _korean_only(ko),
                                       fill=255))
    ref_ko = _render(lambda d: d.text(ORIGIN, KO_SENTENCE, font=ko, fill=255, anchor="la"))
    assert got.tobytes() == ref_ko.tobytes(), "纯韩文行没有用韩文字体画"
    if not KO_IS_CJK:
        ref_tofu = _render(lambda d: d.text(ORIGIN, KO_SENTENCE, font=cjk, fill=255,
                                            anchor="la"))
        assert got.tobytes() != ref_tofu.tobytes(), "纯韩文行与主字体（方框）结果相同"
    print(f"  ✓ 纯韩文行走韩文字体（墨迹 {_ink_bbox(got)[1]}px"
          f"{'，与方框版本不同' if not KO_IS_CJK else '；本机与主字体同文件'}）")


def test_korean_jamo_line_uses_korean_font() -> None:
    """★ 韩文**字母**档单 run 也要走韩文字体：兼容字母（`ㄱㄴㄷ`，U+3130–318F）
    与字母 Jamo（U+1100–11FF，NFD 形态的韩文）。

    只把「韩文音节」当韩文的实现会漏掉这两档 —— 实测主字体（msyh）渲染它们同样是豆腐块
    （判据同 `test_tofu_probe_has_teeth`），所以它们必须被覆盖。
    """
    if _no_ko_font():
        return _skip("test_korean_jamo_line_uses_korean_font")
    cjk, ko = _fonts()
    checked = []
    for sample, label in ((KO_JAMO, "兼容字母"), (KO_CONJOINING, "字母 Jamo")):
        got = _render(lambda d, t=sample: _draw_line(d, ORIGIN, t, cjk, _korean_only(ko),
                                                     fill=255))
        ref_ko = _render(lambda d, t=sample: d.text(ORIGIN, t, font=ko, fill=255, anchor="la"))
        assert got.tobytes() == ref_ko.tobytes(), \
            f"{label}行没有用韩文字体画（{sample!r} —— 这一档被漏掉了？）"
        if not KO_IS_CJK:
            ref_cjk = _render(lambda d, t=sample: d.text(ORIGIN, t, font=cjk, fill=255,
                                                         anchor="la"))
            assert got.tobytes() != ref_cjk.tobytes(), \
                f"{label}行与主字体（方框）结果相同：{sample!r}"
        assert not _is_tofu(ko, sample), f"韩文字体渲染 {sample!r} 也是方框——先修字体解析"
        checked.append(f"{label} {_ink_bbox(got)[1]}px")
    print(f"  ✓ 韩文字母两档都走韩文字体、非方框：{'、'.join(checked)}")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_tofu_probe_has_teeth,
        test_korean_font_renders_real_glyphs,
        test_korean_font_resolved_through_platform_facade,
        test_every_script_has_a_platform_finder,
        test_hangul_runs_go_to_the_hangul_slot,
        test_production_font_dict_and_panel_path,
        test_cjk_font_not_swapped_for_korean,
        test_pure_cjk_panel_bitmap_unchanged,
        test_mixed_line_uses_both_fonts,
        test_mixed_wrap_respects_max_width,
        test_mixed_line_end_to_end_in_panel,
        test_right_anchor_mixed_line_stays_inside,
        test_korean_only_line_uses_korean_font,
        test_korean_jamo_line_uses_korean_font,
    ]
    print("test_korean_font:")
    print(f"  环境：主字体={Path(CJK_FONT_PATH or '?').name} "
          f"韩文={Path(KO_FONT_PATH or '（未找到）').name} "
          f"泰文={Path(THAI_FONT_PATH or '（未找到）').name}"
          f"{'【韩文==主字体】' if KO_IS_CJK else ''}")
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
              f"{'、'.join(_SKIPPED)}）")
    else:
        print(f"ALL PASSED（{len(tests)} 个用例：豆腐块判据 / 韩文字体×2 / "
              "接线×3 / 主字体行为不变×2 / 中韩混排×3 / 右对齐 / 纯韩文 / 字母两档）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
