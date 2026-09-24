"""验证：GUI 保存配置不会破坏 config.yaml 的注释与键顺序。

背景：原来三处保存都走 `yaml.safe_load` + `yaml.dump` 整文件重写，
实测把一份 26 行注释的配置拍成了一坨没有注释、按键名重排的键值对。
现在改为 `_yaml_set_in_text` 就地改文本，本脚本验证它真的保住了。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(r"D:/workspace/vrchat-livetranslate")
sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "config.yaml"
BACKUP = ROOT / "out" / "cfg_backup.yaml"


def n_comments(t: str) -> int:
    return sum(1 for ln in t.splitlines() if ln.strip().startswith("#"))


def top_keys(t: str) -> list[str]:
    return [ln.split(":")[0] for ln in t.splitlines()
            if ln and not ln[0].isspace() and ":" in ln and ln.rstrip().endswith((":", ""))]


def main() -> int:
    before = CONFIG.read_text(encoding="utf-8")
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(before, encoding="utf-8")
    print(f"备份 → {BACKUP}")
    print(f"原始：{len(before.splitlines())} 行，注释 {n_comments(before)} 行，"
          f"顶层键 {top_keys(before)}")

    from vlt import crashlog
    crashlog.install(ROOT / "out" / "crashtest", "cfgsave")
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    gui._toggle_tune_panel()

    # 模拟用户操作：拖滑块 + 改锚点 + tracker 序号 + 切译音开关 + 改语言
    gui._tune_values["pos_x"] = -0.075
    gui._tune_values["width_m"] = 0.31
    gui._tune_values["curvature"] = 0.15
    gui._anchor_combo.set("前臂 tracker")
    gui._tracker_var.set("1")
    gui._save_overlay_cfg()
    gui._vmic_var.set(True)
    gui._save_audio_flag()
    gui._lang_pair = {"source": "ja", "target": "zh"}
    gui._save_lang_config()

    after = CONFIG.read_text(encoding="utf-8")
    print(f"保存后：{len(after.splitlines())} 行，注释 {n_comments(after)} 行，"
          f"顶层键 {top_keys(after)}")

    fails = []
    if n_comments(after) != n_comments(before):
        fails.append(f"注释被破坏：{n_comments(before)} → {n_comments(after)}")
    if top_keys(after) != top_keys(before):
        fails.append("顶层键顺序被改变")
    for want in ("anchor: tracker", "tracker_index: 1", "pos: [-0.075, 0.06, 0.02]",
                 "width_m: 0.31", "curvature: 0.15", "enabled: true",
                 "source_lang: ja", "target_lang: zh"):
        if want not in after:
            fails.append(f"没写进去：{want}")
        else:
            print(f"  ✓ {want}")
    # 注释内容也要还在（不只是行数）
    if "相对锚点偏移（米）" not in after:
        fails.append("具体注释文本丢失（相对锚点偏移（米））")

    gui._root.destroy()
    CONFIG.write_text(before, encoding="utf-8")
    print(f"已还原 config.yaml（注释 {n_comments(CONFIG.read_text(encoding='utf-8'))} 行）")

    if fails:
        print("\n❌ 失败：")
        for f in fails:
            print("   -", f)
        return 1
    print("\n✅ 全部通过：注释、顺序、写入值都正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())
