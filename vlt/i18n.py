"""界面多语言（i18n）：以**中文原文为 key** 的轻量实现。

为什么以中文原文为 key（而不是另造 msgid 常量）：
- 改动面最小：已有中文界面把字面量包一层 `t()` 即可，不用全文换名；
- 漏翻时回落成中文原文，界面不会崩、不会出现空白；
- 缺哪些翻译 grep 得到（`tests/test_i18n.py` 有机械守卫，漏翻直接红）。

加一种新语言 = 在 `vlt/locales/` 下加一个 `<code>.py`（内含 `STRINGS` 词表），
并在 `_LANGS` 里登记一行（code + 母语写法）。zh 是基准语言，不需要词表。
"""
from __future__ import annotations

import ctypes
import importlib

# (语言代码, 母语写法)。语言名永远用各自的母语写法，不随界面语言变化；
# 列表顺序即设置里下拉的显示顺序。
_LANGS: list[tuple[str, str]] = [
    ("zh", "简体中文"),
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("ru", "Русский"),
]

# Windows 主语言 ID → 界面语言。表里没有的（德语/法语等已知但未支持的语言）按 en 接待；
# 详见 detect_system_language 的注释。
_PRIMARY_LANG: dict[int, str] = {0x04: "zh", 0x09: "en", 0x11: "ja", 0x12: "ko", 0x19: "ru"}

# None = 尚未设置过（此时 t() 按基准语言 zh 处理）。启动时由界面解析后 set_language。
_current: str | None = None
_catalogs: dict[str, dict[str, str]] = {}


def current_language() -> str:
    """当前界面语言代码；未设置过时按基准语言 "zh"。"""
    return _current or "zh"


def available_languages() -> list[tuple[str, str]]:
    """可选语言列表 [(code, 母语名称)]，顺序即下拉里显示的顺序。"""
    return list(_LANGS)


def normalize_language(code: str | None) -> str:
    """容错归一：`en-US` / `EN` / `zh_CN` → `en` / `en` / `zh`；不认识的一律 "zh"。

    回落 zh 而不是抛错：配置被手改坏时界面照常起得来，最多就是回到中文。
    """
    if not code:
        return "zh"
    primary = str(code).strip().lower().replace("_", "-").split("-", 1)[0]
    known = {c for c, _ in _LANGS}
    return primary if primary in known else "zh"


def detect_system_language() -> str:
    """读 Windows 用户默认 UI 语言（`GetUserDefaultUILanguage`）。

    返回 LANGID（如 0x0804=zh-CN、0x0409=en-US），低 10 位是主语言 ID。
    三条分界（都写死在 `_PRIMARY_LANG` 与下面的注释里，别改口径）：

    - **支持的语言**：0x04→zh、0x09→en、0x11→ja、0x12→ko、0x19→ru；
    - **已知但不在支持列表里的语言**（0x07 德语、0x0c 法语、0x0a 西班牙语…）→ **"en"**：
      这些用户按英文接待远比按中文合理，也不至于看到方块或空白；
    - **取不到值 / 非 Windows / 任何异常** → **"zh"**：保持老用户（中文环境）行为不变。
    """
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        primary = int(langid) & 0x3FF
    except Exception:  # noqa: BLE001 — 非 Windows / 任何意外都按 zh
        return "zh"
    return _PRIMARY_LANG.get(primary, "en")


def set_language(code: str | None) -> None:
    """设置当前界面语言（经过 normalize 容错，不认识的 code 落到 zh）。"""
    global _current
    _current = normalize_language(code)


def _catalog(lang: str) -> dict[str, str]:
    """取 `vlt/locales/<lang>.py` 的 STRINGS（带缓存；加载失败回落空词表=中文原文）。"""
    if lang in _catalogs:
        return _catalogs[lang]
    try:
        mod = importlib.import_module(f".locales.{lang}", __package__)
        cat = dict(getattr(mod, "STRINGS", {}) or {})
    except Exception as exc:  # noqa: BLE001 — 词表坏了不该拖垮界面
        print(f"[i18n] ⚠️ 词表 {lang} 加载失败，界面回落中文："
              f"{type(exc).__name__}: {exc}", flush=True)
        cat = {}
    _catalogs[lang] = cat
    return cat


def t(zh: str, **kw) -> str:  # noqa: ANN003
    """把中文原文翻成当前语言；查不到词条 → 原样返回中文。

    `kw` 非空时对结果做 `str.format(**kw)`；占位符对不上时打印一行告警并返回
    **未格式化**的文本 —— 文案问题绝不能把界面搞崩。
    """
    text = zh
    lang = current_language()
    if lang != "zh":
        text = _catalog(lang).get(zh, zh)
    if kw:
        try:
            return text.format(**kw)
        except Exception as exc:  # noqa: BLE001
            print(f"[i18n] ⚠️ 文案占位符格式化失败（返回未格式化文本）："
                  f"{zh!r} {kw!r} → {type(exc).__name__}: {exc}", flush=True)
            return text
    return text
