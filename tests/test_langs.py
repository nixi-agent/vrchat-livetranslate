"""语言表一致性：源/目标两侧必须对称，且同一个语言码两侧叫法一致。

## 为什么要有这个测试

语言表在 `vlt/gui.py` 里是**两份独立 dict**（源语言那份多一个「自动检测」），而
「语言镜像」逻辑是**按语言码在两侧互查**的：选「中文→俄语」时，「别人说」方向要
自动变成「俄语→中文」。任何一边漏加，界面都不会报错 —— 它会**静默回落**
（`_target_name` 找不到就返回「英语」，`_source_name` 返回「自动检测」），
用户看到的现象是「我明明选了俄语，但它没生效」。这种静默回落必须被钉住。

实测背景（2026-09-25）：用户要求加俄语。模型（qwen3.8-livetranslate-flash-realtime）
两个方向都支持，实测：
  en→ru 译文 `Привет. Я посещаю ваш мир из Канады. …`
  ru→zh 译文 `你好。我来拜访你们的世界，来自加拿大。…`
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_lang_tables_are_symmetric() -> None:
    """源语言表（去掉「自动检测」）与目标语言表必须有一模一样的语言码集合。"""
    from vlt.gui import SOURCE_LANGS, TARGET_LANGS

    src = {v for v in SOURCE_LANGS.values() if v}
    tgt = set(TARGET_LANGS.values())
    assert src == tgt, (f"源/目标语言表不对称：只在源有 {sorted(src - tgt)}，"
                        f"只在目标有 {sorted(tgt - src)}")

    # 同一个码两侧叫法要一致（否则下拉框里同一个码会出现两种名字）
    src_names = {v: k for k, v in SOURCE_LANGS.items() if v}
    tgt_names = {v: k for k, v in TARGET_LANGS.items()}
    for code, name in tgt_names.items():
        assert src_names[code] == name, \
            f"语言码 {code} 两侧叫法不一致：源={src_names[code]!r} 目标={name!r}"
    print(f"  语言表对称 OK（{len(tgt)} 种：{sorted(tgt)}）")


def test_russian_supported() -> None:
    """用户要求：俄语（ru）。模型本身支持，界面两个方向都必须能选到。"""
    from vlt.gui import SOURCE_LANGS, TARGET_LANGS

    assert TARGET_LANGS.get("俄语") == "ru", f"目标语言表里没有俄语：{TARGET_LANGS}"
    assert SOURCE_LANGS.get("俄语") == "ru", f"源语言表里没有俄语：{SOURCE_LANGS}"
    print("  俄语在源/目标两侧都可选 OK")


def test_name_lookup_does_not_silently_fall_back() -> None:
    """取名函数必须能认出俄语，不能回落到「英语 / 自动检测」。"""
    from vlt.gui import _source_name, _target_name

    assert _target_name("ru") == "俄语", f"目标取名回落了：{_target_name('ru')!r}"
    assert _source_name("ru") == "俄语", f"源取名回落了：{_source_name('ru')!r}"
    print("  俄语取名不回落 OK")


if __name__ == "__main__":
    print("test_langs:")
    test_lang_tables_are_symmetric()
    test_russian_supported()
    test_name_lookup_does_not_silently_fall_back()
    print("ALL PASSED")
