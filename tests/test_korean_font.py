#!/usr/bin/env python
"""韩文字体覆盖 + 中韩混排渲染的回归测试（离线、不碰 VR 运行时）。

用法：.venv/Scripts/python.exe tests/test_korean_font.py

## 为什么必须有这个测试

用户 2026-10 报：**韩语译文在手腕屏 / 桌面字幕上显示成一排方框**。
实测根因：默认主字体 `C:/Windows/Fonts/msyh.ttc`（微软雅黑）**不含韩文字形** ——
用 PIL 渲染 `안녕하세요` 得到的位图与渲染「私有区缺字位 U+E000 × 5」的位图**逐字节完全相同**
（= 豆腐块）。这类 bug 是**静默**的：没有异常、没有告警，界面上什么都对，只有贴图是方块。
（泰文同因，机制共用，用例分开：见 `tests/test_thai_font.py`。）

## ★ 决定「换不换字体」的是**字形覆盖**，不是「书写系统」（评审 2026-10-15 纠正）

* 主字体**画得出**这个书写系统 → **不换**。Linux 上主字体常见落点是 Noto Sans CJK 这类 pan-CJK 族
  （自带韩文）：按书写系统无脑换只会换成同族另一个字重 —— 实测纯白像素 +37%，
  等于在本来没毛病的机器上把韩文**变粗一档**（假阳性）。
* 主字体**画不出**（Windows/msyh 的现场）→ 才去找专用字体，且那份要**打开 + 覆盖**双校验。

所以本文件**不按「本机是哪种机器」跳过**，而是把两种决策都用打桩测出来
（`test_no_switch_when_main_font_covers` / `test_switch_when_main_font_lacks`），
外加一条按**本机真实事实**（`font_covers`，不是「字体路径是否相同」）分派的行为断言 ——
「路径相等」曾经被当成跳过判据，结果在 Noto CJK 的 Regular vs Medium 上判成「不是同一份字体」
而硬红（评审实测），这条路别再走。

## 判据为什么可信

`test_tofu_probe_has_teeth` 先证明**判据本身有分辨力**（主字体渲染韩文必须真的得到豆腐块），
`test_coverage_probe_matches_independent_criterion` 再把 `font_covers()` 与「独立复算的豆腐块口径」
双向对齐 —— 否则「不是豆腐块」那些断言可能全是空转。
本文件**不复用**被测模块的 `_script_of` / `_split_script_runs` 做判据（实现一坏判据跟着坏）。
"""
from __future__ import annotations

import contextlib
import io
import sys
import unicodedata
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
    font_covers,
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


# ---------------------------------------------------------------- 环境事实（**按覆盖判，不按路径判**）

CJK_FONT_PATH: str | None = resolve_font_path("")
KO_FONT_PATH: str | None = resolve_korean_font_path("")
THAI_FONT_PATH: str | None = overlay.resolve_thai_font_path("")

# 本机主字体到底能不能画韩文 —— 这是决定「该不该换字体」的**唯一**环境事实。
MAIN_COVERS_KO: bool = font_covers("hangul", CJK_FONT_PATH)

# 主字体与韩文字体是不是**同一份文件**（同文件 + 同字号 ⇒ 位图必然逐字节相同）。
# ⚠️ 只用来说明「拿它当对照物没有分辨力」，**不用来跳过整组用例**（评审踩过的坑：
#    同族不同字重的两份文件路径不同、量宽却可能完全相同 → 拿路径当判据会误红）。
SAME_FONT_FILE: bool = bool(KO_FONT_PATH and CJK_FONT_PATH
                           and Path(KO_FONT_PATH) == Path(CJK_FONT_PATH))

_SKIPPED: list[str] = []
_TYPE_WHY = ("本机没有含韩文字形的专用字体（装了韩文字体后会自动跑；"
             "Windows 自带 malgun.ttf，Linux 见 docs/GUIDE.linux.md 的韩语一节）")
_SAME_WHY = "主字体与韩文字体是同一份文件（它当对照物必然位图相同，这一半没有分辨力）"


def _skip(name: str, why: str = "") -> None:
    _SKIPPED.append(name)
    print(f"  ⏭ 跳过 {name}：{why or _TYPE_WHY}")


def _no_ko_font() -> bool:
    """True = 本机没有含韩文字形的**专用**字体，依赖它的用例应跳过。"""
    return not KO_FONT_PATH


def _fonts_at(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """(主字体, 韩文专用字体) @ 指定字号，都走平台门面解析 —— 不写死任何平台路径。"""
    assert CJK_FONT_PATH, "平台层没能解析出主字体，后续判据无从谈起"
    assert KO_FONT_PATH, (f"平台层没能解析出韩文字体"
                          f"（find_korean_font()={platform_mod.find_korean_font()!r}）"
                          " —— 韩语会渲染成豆腐块")
    return (ImageFont.truetype(CJK_FONT_PATH, size),
            ImageFont.truetype(KO_FONT_PATH, size))


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    return _fonts_at(FONT_SIZE)


def _korean_only(font: ImageFont.FreeTypeFont) -> dict[str, ImageFont.FreeTypeFont]:
    """「只带韩文那一格」的专用字体字典（生产入口是 `load_script_fonts(cfg, size)`）。"""
    return {"hangul": font}


# ---------------------------------------------------------------- 小工具（判据独立于实现）


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


def _has_ink(font: ImageFont.FreeTypeFont, text: str) -> bool:
    return bool(_render(lambda d: d.text(ORIGIN, text, font=font, fill=255)).getbbox())


def _ink_bbox(img: Image.Image) -> tuple[int, int]:
    """相对 ORIGIN 的 (最左, 最右) 墨迹位置（px）。空图返回 (0, 0)。"""
    bb = img.getbbox()
    return (bb[0] - ORIGIN[0], bb[2] - ORIGIN[0]) if bb else (0, 0)


def _expected_coverage(font_path: str | None, samples: tuple[str, ...]) -> bool:
    """**独立复算**「这份字体覆盖不覆盖这些样本」（不复用 `overlay._probe_coverage`）。

    口径与实现同源但独立写：整串 != 缺字位整串；且非组合符号的单字必须有墨迹。
    """
    if not font_path or not Path(font_path).exists():
        return False
    try:
        font = ImageFont.truetype(font_path, 32)
    except Exception:  # noqa: BLE001
        return False
    for sample in samples:
        if _is_tofu(font, sample):
            return False
        for ch in sample:
            if unicodedata.category(ch).startswith("M"):
                continue
            if not _has_ink(font, ch):
                return False
    return True


# ---------------------------------------------------------------- 打桩工具


@contextlib.contextmanager
def _stub_script_fonts(values: dict[str, str | None]):
    """让指定书写系统解析到固定值（None = 本机没有覆盖该书写系统的字体）。

    ⚠️ 打桩的是**通用入口** `overlay.resolve_script_font_path`（`resolve_korean_font_path`
    只是它的薄包装）—— 打桩包装函数在实现搬家时会静默失效；同时清缓存，否则上一次的真实
    结果会把打桩盖住。
    """
    cache = dict(overlay._script_font_cache)
    orig = overlay.resolve_script_font_path
    overlay._script_font_cache.clear()

    def _fake(script: str, configured: str = "") -> str | None:
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


@contextlib.contextmanager
def _stub_coverage(values: dict[str, bool]):
    """打桩「**主字体**覆盖不覆盖某书写系统」（= 强制造出 pan-CJK 主机 / Windows 两种场景）。

    ⚠️ 只打桩 `main_font_covers`（决策点），**不要**打桩 `font_covers`：后者也负责
    「候选字体的覆盖校验」，一起打桩会让真能用的专用字体被误判成不合格 →
    「主字体画不出却不换字体」的假红（实测踩过）。
    """
    orig = overlay.main_font_covers

    def _fake(script: str, main_path: str | None = None) -> bool:
        if script in values:
            return values[script]
        return orig(script, main_path)

    overlay.main_font_covers = _fake                # type: ignore[assignment]
    try:
        yield
    finally:
        overlay.main_font_covers = orig             # type: ignore[assignment]


@contextlib.contextmanager
def _stub_main_font(path: str | None):
    """打桩「主字体解析结果」（不动真实缓存）。"""
    orig = overlay.resolve_font_path
    overlay.resolve_font_path = lambda _configured: path     # type: ignore[assignment]
    try:
        yield
    finally:
        overlay.resolve_font_path = orig                     # type: ignore[assignment]


def _clear_warned(script: str) -> set[str]:
    """清掉该书写系统的留痕标志（返回快照，调用方负责还原）。"""
    saved = set(overlay._script_font_warned)
    overlay._script_font_warned.clear()
    overlay._script_font_warned.update(k for k in saved if not k.startswith(f"{script}:"))
    return saved


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
    """★ 元测试：本机主字体渲染韩文 → 与 `font_covers` 的结论必须一致。

    Windows/msyh 上主字体就是画不出（本 bug 的现场）→ `font_covers` 必须是 False、
    渲染必须是豆腐块；pan-CJK 主机（Noto Sans CJK 等）上反过来。两边都是断言，没有「跳过」。
    """
    assert CJK_FONT_PATH, "平台层没解析出主字体"
    cjk = ImageFont.truetype(CJK_FONT_PATH, FONT_SIZE)
    tofu = _is_tofu(cjk, KO_WORD)
    assert tofu == (not MAIN_COVERS_KO), \
        (f"主字体渲染 {KO_WORD!r} 是不是豆腐块（{tofu}）与 font_covers 的结论"
         f"（覆盖={MAIN_COVERS_KO}）不一致 —— 覆盖判据与渲染判据打架了")
    if tofu:
        assert _is_tofu(cjk, KO_SENTENCE), "换个更长的韩文句子判据就失效了？"
        if KO_FONT_PATH:
            ko = ImageFont.truetype(KO_FONT_PATH, FONT_SIZE)
            assert not _is_tofu(ko, KO_WORD), \
                f"韩文字体（{Path(KO_FONT_PATH).name}）渲染 {KO_WORD!r} 也是豆腐块 —— 判据认不出对照物"
        assert not _is_tofu(cjk, CJK_WORD), \
            f"主字体连 {CJK_WORD!r} 都渲染成豆腐块 —— 参照物选错了，判据不可用"
        print(f"  ✓ 判据有分辨力：主字体 {Path(CJK_FONT_PATH).name} 渲染 {KO_WORD!r} "
              f"== 渲染 {TOFU!r}×{len(KO_WORD)}（= 缺字形，与 font_covers=False 一致）")
    else:
        print(f"  ✓ 本机主字体（{Path(CJK_FONT_PATH).name}）自带韩文字形 → 与 "
              f"font_covers=True 一致（这类机器本来就该**不换**字体，见 ④）")


def test_coverage_probe_matches_independent_criterion() -> None:
    """★ `font_covers()` 与**独立复算**的豆腐块口径双向对齐（不是自说自话）。

    对主字体、韩文专用字体（若有）各比一次：`font_covers(script, path)` 必须等于本条自己算的
    「整串非豆腐块 且 非组合符号单字都有墨迹」。判据不一致时先修判据，别改期望值。
    """
    checked = 0
    for label, path in (("主字体", CJK_FONT_PATH), ("韩文专用字体", KO_FONT_PATH)):
        if not path:
            continue
        want = _expected_coverage(path, overlay._SCRIPT_PRIMARY["hangul"])
        got = font_covers("hangul", path)
        assert got == want, \
            (f"{label}（{Path(path).name}）的覆盖结论与独立复算不一致："
             f"font_covers={got}，独立复算={want}")
        checked += 1
    assert checked, "一份字体都没有？后续判据无从谈起"
    # 反面：路径为空 / 不存在 → 一律 False（调用方据此回落）
    assert font_covers("hangul", None) is False, "字体路径为空时必须判「不覆盖」"
    print(f"  ✓ font_covers 与独立复算一致（比了 {checked} 份字体）；路径为空判 False")


# ---------------------------------------------------------------- ② 韩文专用字体真的有韩文字形


def test_korean_font_renders_real_glyphs() -> None:
    """★ 本机解析出来的韩文专用字体渲染韩文，位图**不等于**缺字位位图（整词 + 逐字两种口径）。"""
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
    """韩文专用字体必须经 `vlt.platform.find_korean_font()` 门面解析，且落在真实存在的文件上。

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
        print("  ⏭ 本机没有韩文专用字体 → 跳过「路径有效 / 与门面一致 / 不在共享模块里」三条；"
              "降级留痕那半条照跑")

    saved_warned = _clear_warned("hangul")
    orig_facade = platform_mod.find_korean_font
    try:
        overlay._script_font_cache.clear()
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
        print(f"  ✓ 韩文专用字体经平台门面解析（{Path(path).name}），overlay.py 不含该文件名；"
              "门面返回 None 时降级留痕（只一次、写明韩文）且渲染不崩")
    else:
        print("  ✓ 本机无韩文专用字体：门面返回 None 时降级留痕（只一次、写明韩文）且渲染不崩"
              "（解析路径那一半已跳过）")


# ---------------------------------------------------------------- ③ 注册表与接线（环境无关）


def test_script_registry_is_consistent() -> None:
    """★ 书写系统登记表**一处定义、多处派生**，且派生结果与平台层对得上（评审要求）。

    教训（评审实测）：三张表各自独立时，「只加 `_SCRIPT_RANGES` 不加 `_SCRIPT_FINDERS`」
    会让那一门永远静默回落主字体，而原来的守卫只遍历 `_SCRIPT_FINDERS` → 全绿。
    现在表是派生的，本条把「三张派生表两两相等」与「平台两端真有实现」一起钉住；
    另外要求每门的**覆盖探测样本落在这门登记的 Unicode 块里**（否则探的是别的书写系统）。
    """
    ids = set(overlay._SCRIPT_IDS)
    assert ids, "书写系统登记表是空的？"
    assert {r[2] for r in overlay._SCRIPT_RANGES} == ids, \
        "`_SCRIPT_RANGES` 里的书写系统与 `_SCRIPTS` 登记不一致（漏登记 = 该门永远不切 run）"
    assert set(overlay._SCRIPT_FINDERS) == ids, "`_SCRIPT_FINDERS` 与登记表不一致"
    assert set(overlay._SCRIPT_NAMES) == ids, "`_SCRIPT_NAMES` 与登记表不一致（留痕文案会退化成脚本 id）"
    assert set(overlay._SCRIPT_PRIMARY) == ids, "`_SCRIPT_PRIMARY` 与登记表不一致"
    assert set(overlay._SCRIPT_EXTRA) == ids, "`_SCRIPT_EXTRA` 与登记表不一致"
    for script in ids:
        ranges = [(lo, hi) for lo, hi, s in overlay._SCRIPT_RANGES if s == script]
        primary, extra = overlay._SCRIPT_PRIMARY[script], overlay._SCRIPT_EXTRA[script]
        assert ranges, f"{script} 没有登记 Unicode 块"
        assert primary, f"{script} 没有主导码位样本 —— 换不换字体就无从判断"
        assert not (set(primary) & set(extra)), f"{script} 的主导/次要样本重复了"
        for sample in primary + extra:
            for ch in sample:
                assert any(lo <= ord(ch) <= hi for lo, hi in ranges), \
                    (f"{script} 的样本 {ch!r}（U+{ord(ch):04X}）不在它登记的 Unicode 块里 —— "
                     "探测的是别的书写系统")
    backend_mod = platform_mod.backend()
    for script, finder in overlay._SCRIPT_FINDERS.items():
        assert callable(getattr(platform_mod, finder, None)), \
            f"{script} 的门面函数 vlt.platform.{finder} 不存在（平台层漏接线 → 该语言永远方框）"
        assert callable(getattr(backend_mod, finder, None)), \
            f"{type(backend_mod).__name__} 里没有 {finder}（只有门面壳子，没接平台实现）"
    print(f"  ✓ 登记表 {sorted(ids)}：三张派生表一致、样本落在登记块内、"
          f"探测函数在门面与 {type(backend_mod).__name__} 里都真实存在")


# ---------------------------------------------------------------- ④ 覆盖驱动：该不该换字体


def test_no_switch_when_main_font_covers() -> None:
    """★ **主字体画得出 → 一个字都不许换**（pan-CJK 主机；评审实测的假阳性就在这里）。

    做法：打桩「主字体覆盖韩文」，**解析入口保持真实实现** —— 若代码不看覆盖就去解析，
    本机真实的韩文字体（malgun 等）会被装进字典，本条立刻红。
    另一半：位图必须与「一个专用字体都没有」时**逐字节相同**（= 那种机器上的行为与改动前一致）。
    """
    cfg = OverlayConfig()
    with _stub_coverage({"hangul": True}):
        fonts = load_script_fonts(cfg, FONT_SIZE)
        assert "hangul" not in fonts, \
            ("主字体覆盖韩文却仍然换了字体（就是评审实测的「韩文被变粗一档」）："
             f"{Path(fonts['hangul'].path).name}")
        img_main = render_panel(KO_WORD, KO_SENTENCE, cfg)
    with _stub_script_fonts({s: None for s in overlay._SCRIPT_IDS}):
        img_base = render_panel(KO_WORD, KO_SENTENCE, cfg)
    assert img_main.tobytes() == img_base.tobytes(), \
        "覆盖为真时面板仍与「无专用字体」时不同 —— 说明有字体被偷偷用上了"
    d_probe = ImageDraw.Draw(_blank())
    cjk = ImageFont.truetype(CJK_FONT_PATH, FONT_SIZE)
    got_w = _measure_line(d_probe, KO_WORD, cjk, {})
    want_w = d_probe.textlength(KO_WORD, font=cjk)
    assert abs(got_w - want_w) < 0.01, "测量被换字体的逻辑带偏了"
    print(f"  ✓ 主字体覆盖韩文时：不产生韩文格（字典={sorted(fonts) or '空'}），"
          f"面板与「无专用字体」逐字节相同（{len(img_main.tobytes())} 字节），"
          f"测量 {want_w:.1f}px == 单字体口径")


def test_switch_when_main_font_lacks() -> None:
    """★ **主字体画不出 → 必须换**（Windows/msyh 的现场）：换出来的字真不是方框。

    做法：打桩「主字体不覆盖韩文」+ 真实解析 → 韩文格必须在字典里，且面板与
    「一个专用字体都没有」时不同（= 真的用上了）。
    """
    if _no_ko_font():
        return _skip("test_switch_when_main_font_lacks", _TYPE_WHY + "（这条要一份真能画韩文的字体）")
    cfg = OverlayConfig()
    with _stub_coverage({"hangul": False}):
        fonts = load_script_fonts(cfg, FONT_SIZE)
        assert "hangul" in fonts, \
            "主字体画不出韩文却没有换字体（= 用户看到的方框没被修）"
        img_switched = render_panel(KO_WORD, KO_SENTENCE, cfg)
    with _stub_script_fonts({s: None for s in overlay._SCRIPT_IDS}):
        img_base = render_panel(KO_WORD, KO_SENTENCE, cfg)
    if SAME_FONT_FILE:
        print(f"  ⏭ 位图那半条跳过：{_SAME_WHY}")
    else:
        assert img_switched.tobytes() != img_base.tobytes(), \
            "换了字体，但面板位图与「一个专用字体都没有」完全相同 —— 韩文格没被用上"
    ko = ImageFont.truetype(KO_FONT_PATH, FONT_SIZE)
    assert not _is_tofu(ko, KO_WORD), f"换上的字体渲染 {KO_WORD!r} 仍是方框"
    print(f"  ✓ 主字体画不出韩文时：韩文格 = {Path(fonts['hangul'].path).name}，"
          "面板与无专用字体时不同（真的换了）")


def test_resolve_accepts_primary_coverage_and_traces_partials() -> None:
    """★ 平台探测到的候选：**主导码位**覆盖 → 采用（缺次要码位只留一行痕）；主导都不覆盖 → 拒绝。

    为什么分两档：只认「全部样本都要有」会把「正文能画、只缺 NFD/半角那种罕见码位」的字体也拒掉
    —— 那是拿「宁可整段方框」换「不漏一个码位」（评审机器上的 UnPilgi 就是这种字体）。
    本条按**平台真实探测结果**分派断言，两种环境都真跑（不写死平台路径）。
    """
    cand = platform_mod.find_korean_font()
    if not cand:
        return _skip("test_resolve_accepts_primary_coverage_and_traces_partials",
                     "本机平台探测不到韩文候选字体（该分支无从构造）")
    saved_warned = _clear_warned("hangul")
    orig_facade = platform_mod.find_korean_font
    try:
        overlay._script_font_cache.clear()
        platform_mod.find_korean_font = lambda: cand       # type: ignore[assignment]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = resolve_korean_font_path("")
        out = buf.getvalue()
        if font_covers("hangul", cand):
            assert got == cand, f"主导码位能画的候选被拒了：{got!r} vs {cand!r}"
            missing = overlay.missing_extra_samples("hangul", cand)
            if missing:
                assert "部分次要码位" in out, \
                    f"缺次要码位必须留痕（禁静默降级），实际输出：{out!r}"
            else:
                assert "部分次要码位" not in out, f"全覆盖的候选不该报「缺次要码位」：{out!r}"
            print(f"  ✓ 候选 {Path(cand).name}：主导码位覆盖 → 采用"
                  f"（缺次要 {len(missing)} 组{'，已留痕' if missing else '，无'}）")
        else:
            assert got is None, f"主导码位画不出的候选被采用了：{got!r}"
            assert "并不真覆盖" in out, f"拒绝时必须留痕，实际输出：{out!r}"
            print(f"  ✓ 候选 {Path(cand).name}：主导码位画不出 → 拒绝并留痕")
    finally:
        platform_mod.find_korean_font = orig_facade       # type: ignore[assignment]
        overlay._script_font_cache.clear()
        overlay._script_font_warned.clear()
        overlay._script_font_warned.update(saved_warned)


def test_uncovered_candidate_is_rejected_with_trace() -> None:
    """★ 探测返回的字体**不真覆盖**该书写系统时：必须拒绝 + 留痕（禁静默降级）。

    为什么单列（评审 2026-10-15 的 P2-2）：平台层的候选表只能保证「能打开」，不保证「能画这门语言」
    （`fc-list` 的第一行只是扫描顺序）。上层若把不合格的候选直接用上，症状就是「一排豆腐块 +
    一行日志都没有」。本条用**本机确实不含韩文字形**的字体当不合格候选（临时挑，不写死平台路径），
    断言 `resolve_korean_font_path()` 返回 None 且输出里点明了原因、且只打一次。
    """
    bad = next((p for p in (THAI_FONT_PATH, CJK_FONT_PATH)
                if p and not font_covers("hangul", p)), None)
    if bad is None:
        return _skip("test_uncovered_candidate_is_rejected_with_trace",
                     "本机挑不出一份「存在但不含韩文字形」的字体（该分支无从构造）")
    saved_warned = _clear_warned("hangul")
    orig_facade = platform_mod.find_korean_font
    try:
        overlay._script_font_cache.clear()
        platform_mod.find_korean_font = lambda: bad       # type: ignore[assignment]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = resolve_korean_font_path("")
        assert got is None, \
            f"不合格的候选（{Path(bad).name} 不含韩文字形）被当成可用字体了：{got!r}"
        out = buf.getvalue()
        assert "并不真覆盖" in out and "韩文" in out, \
            f"拒绝不合格候选时必须留痕（禁静默降级），实际输出：{out!r}"
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            resolve_korean_font_path("")
        assert "并不真覆盖" not in buf2.getvalue(), "同一条告警打了第二次（刷屏）"
    finally:
        platform_mod.find_korean_font = orig_facade       # type: ignore[assignment]
        overlay._script_font_cache.clear()
        overlay._script_font_warned.clear()
        overlay._script_font_warned.update(saved_warned)
    print(f"  ✓ 不合格候选（{Path(bad).name}）被拒绝并留痕（只一次）→ 返回 None（回落主字体）")


def test_platform_skips_unopenable_candidates() -> None:
    """★ 平台层候选表要**逐份打开校验**：打不开的候选跳过，别把坏路径交给上层。

    评审（2026-10-15）实测过一条洞：`fc-list` 的首行只是扫描顺序，可能是 Pillow 打不开的位图
    字体；平台层若无脑返回它，上层只会「静默回落主字体 = 一排豆腐块」。
    本条用**临时文件**造场景（环境无关）：一个存在但不是字体的文件排前面 → 必须跳到真字体；
    只有坏候选 → 必须返回 None。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        junk = Path(td) / "not-a-font.bin"
        junk.write_bytes(b"this is definitely not a font file")
        assert junk.exists(), "临时文件没建出来"
        assert platform_mod.first_openable_font([str(junk)]) is None, \
            "只有打不开的候选时应当返回 None（让上层降级留痕）"
        assert platform_mod.first_openable_font([str(junk), str(Path(td) / "missing.ttf")]) is None, \
            "全是坏候选时应当返回 None"
        if CJK_FONT_PATH:
            got = platform_mod.first_openable_font([str(junk), CJK_FONT_PATH])
            assert got == CJK_FONT_PATH, \
                f"应当跳过打不开的候选取到真字体，实际 {got!r}"
        assert platform_mod.first_openable_font([]) is None, "空候选表应当返回 None"
        assert platform_mod.first_openable_font(["", "   "]) is None, "空路径候选应当被跳过"
    print("  ✓ 平台层逐份打开校验：坏候选 / 空候选被跳过，只有坏候选时返回 None")


def test_hangul_runs_go_to_the_hangul_slot() -> None:
    """★ 接线判据（**不依赖本机有没有韩文字体**）：韩文字符落 `hangul` 槽，别的字符不许蹭。

    做法：给 `hangul` 槽塞一个**字号明显不同**的字体 —— 宽度就能分辨走的是哪一格。
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
    print(f"  ✓ 韩文落韩文格（{KO_WORD!r} 与两档字母；中文/拉丁仍走主字体；混排两段各按各的量）")


def _runs_by_fixture(text: str) -> list[str]:
    """按「是不是韩文」切段（**夹具自己的实现**，不用被测模块的 `_split_script_runs`）。"""
    runs: list[str] = []
    for ch in text:
        if runs and _is_hangul_char(runs[-1][0]) == _is_hangul_char(ch):
            runs[-1] += ch
        else:
            runs.append(ch)
    return runs


# ---------------------------------------------------------------- ⑤ 主字体渲染行为不变


def test_cjk_font_not_swapped_for_korean() -> None:
    """★ 防回退：不能为了韩语把默认 CJK 字体换掉。

    判据：① `resolve_font_path("")` 仍等于 `find_cjk_font()`；
    ② 非韩文文本走 `_draw_line` 与改动前的写法 `d.text(..., font=cjk, anchor="la")`
       逐字节相同 —— 字体字典取**生产口径** `load_script_fonts(cfg, size)`。

    ⚠️ 样本里**不放韩文**：韩文在主字体覆盖不了的机器上本来就该换字体（那是修复）。
    """
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
    """★ 端到端：纯中日韩面板在「韩文格有字体」与「韩文格为空」两种情况下
    位图**逐字节一致** —— 韩语那条腿对不用韩语的用户是零影响的。"""
    if _no_ko_font():
        return _skip("test_pure_cjk_panel_bitmap_unchanged",
                     _TYPE_WHY + "（没有韩文字体时这条恒成立，但也就没有分辨力）")
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


# ---------------------------------------------------------------- ⑥ 中韩混排不出豆腐块


def test_mixed_line_uses_both_fonts() -> None:
    """★ 同一行里既有中文又有韩文：韩文那段必须用韩文字体画，且整行不出豆腐块。

    判据（三条互相独立）：
    ① run-aware 绘制的位图 **≠** 把韩文格换成主字体（= 修复前的方框效果）的位图；
    ② 混排行里每个韩文码位单独用韩文字体渲染都不是豆腐块；
    ③ 整行墨迹非空，且墨迹宽度与 `_measure_line` 的测量值吻合（测量跟着字体走）。
    """
    if _no_ko_font():
        return _skip("test_mixed_line_uses_both_fonts")
    cjk, ko = _fonts()
    assert _hangul_chars(MIXED_LINE) and any(_is_hanzi(c) for c in MIXED_LINE), \
        "用例本身必须同时含中文与韩文"

    fixed = _render(lambda d: _draw_line(d, ORIGIN, MIXED_LINE, cjk, _korean_only(ko), fill=255))
    if SAME_FONT_FILE:
        print(f"  ⏭ 跳过「位图≠方框版本」那半条：{_SAME_WHY}")
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
    note = (f"主字体单字体口径 {naive:.0f}px，差 {abs(naive - want):.0f}px"
            if abs(naive - want) > INK_TOLERANCE_PX
            else "（本机两份字体量宽相同，这条差异在本机不可观测）")
    print(f"  ✓ 中韩混排：韩文 {len(_hangul_chars(MIXED_LINE))} 码位全覆盖；"
          f"测量 {want:.0f}px / 实画 {got}px；{note}")


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
    """★ 混排长句折行：**每一行实画出来的墨迹都不许超出 max_w**（量错字体 = 溢出面板）。

    ⚠️ 这里**不断言**「旧口径（一律用主字体量）必然量错」—— 那取决于两份字体的量宽是否相同，
    在 Noto CJK 的 Regular/Medium 这类同族不同字重的机器上它**成立不了**（评审实测：差 0.0px，
    直接判红）。改为：断言「run-aware 测量 == 独立按 run 复算」这条**恒真且有意义**的不变量，
    旧口径的偏差只作汇报。
    """
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
            # 独立复算：按「是不是韩文」分段，各用各的字体量
            want = sum(d_probe.textlength(run, font=(ko if _is_hangul_char(run[0]) else cjk))
                       for run in _runs_by_fixture(ln))
            assert abs(m - want) <= 0.01, \
                f"max_w={max_w}：run-aware 测量 {m:.1f}px 与独立复算 {want:.1f}px 不符"
            assert m <= max_w + slack + 1, \
                f"max_w={max_w}：测量就超宽 {m:.0f}px（允许的悬挂 {slack:.0f}px）：{ln[:24]!r}"
            ink = _ink_bbox(_render(lambda dd, t=ln: _draw_line(dd, ORIGIN, t, cjk,
                                                               _korean_only(ko),
                                                               fill=255)))[1]
            assert ink <= max_w + slack + INK_TOLERANCE_PX, \
                f"max_w={max_w}：实画墨迹 {ink}px 超出面板（悬挂 {slack:.0f}px）：{ln[:24]!r}"
        report.append(f"{max_w}px→{len(lines)}行")

    # 汇报（不断言）：旧口径（一律用主字体量）在本机的偏差方向与量级
    naive_lines = wrap_text(d_probe, text, cjk, 400)
    errs = [abs(_measure_line(d_probe, ln, cjk, None) - _measure_line(d_probe, ln, cjk, _korean_only(ko)))
            for ln in naive_lines if _hangul_chars(ln)]
    worst = max(errs) if errs else 0.0
    note = (f"旧口径（一律主字体量）在 {len(errs)} 行混排上最大偏差 {worst:.0f}px"
            if worst > INK_TOLERANCE_PX
            else f"本机两份字体量宽相同（偏差 {worst:.1f}px），旧口径不会量错")
    print(f"  ✓ 混排折行：{'，'.join(report)}，每行测量 == 独立复算且实画不超宽"
          f"（含避头尾悬挂余量）；{note}")


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
        if not _hangul_chars(ln) or SAME_FONT_FILE:
            continue
        a = _render(lambda d, t=ln: _draw_line(d, ORIGIN, t, tf, _korean_only(tf_ko), fill=255))
        b = _render(lambda d, t=ln: _draw_line(d, ORIGIN, t, tf, _korean_only(tf), fill=255))
        assert a.tobytes() != b.tobytes(), f"面板里这一行的韩文仍是方框：{ln[:24]!r}"
        checked += 1

    conv = render_conversation([("mine", MIXED_LINE, MIXED_LINE),
                                ("peer:abc", KO_SENTENCE, "你好，很高兴认识你")], cfg)
    assert conv.size == cfg.size_px, conv.size
    assert conv.getextrema()[3][1] > 0, "对话视图全透明"
    if SAME_FONT_FILE:
        print(f"  ⏭ 跳过「面板里逐行非方框」那半条：{_SAME_WHY}（尺寸/非空两条照跑）")
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
    if not SAME_FONT_FILE:
        ref_cjk = _render(lambda d: d.text(ORIGIN, KO_SENTENCE, font=cjk, fill=255,
                                            anchor="la"))
        assert got.tobytes() != ref_cjk.tobytes(), "纯韩文行与主字体（方框）结果相同"
    print(f"  ✓ 纯韩文行走韩文字体（墨迹 {_ink_bbox(got)[1]}px"
          f"{'，与方框版本不同' if not SAME_FONT_FILE else '；本机与主字体同文件'}）")


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
        if not SAME_FONT_FILE:
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
        test_coverage_probe_matches_independent_criterion,
        test_korean_font_renders_real_glyphs,
        test_korean_font_resolved_through_platform_facade,
        test_script_registry_is_consistent,
        test_resolve_accepts_primary_coverage_and_traces_partials,
        test_uncovered_candidate_is_rejected_with_trace,
        test_platform_skips_unopenable_candidates,
        test_no_switch_when_main_font_covers,
        test_switch_when_main_font_lacks,
        test_hangul_runs_go_to_the_hangul_slot,
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
          f"（覆盖韩文={MAIN_COVERS_KO}） 韩文专用字体="
          f"{Path(KO_FONT_PATH or '（未找到）').name} "
          f"泰文={Path(THAI_FONT_PATH or '（未找到）').name}")
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
        print(f"ALL PASSED（{len(tests)} 个用例：豆腐块/覆盖判据×2 / 韩文专用字体×2 / 登记表 / 候选校验×3 / "
              "该换与不该换×2 / 接线 / 主字体行为不变×2 / 中韩混排×3 / 右对齐 / 纯韩文 / 字母两档）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
