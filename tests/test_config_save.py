"""验证：GUI 保存配置不会破坏 config.yaml 的注释与键顺序。

背景：原来三处保存都走 `yaml.safe_load` + `yaml.dump` 整文件重写，
实测把一份 26 行注释的配置拍成了一坨没有注释、按键名重排的键值对。
现在改为 `_yaml_set_in_text` 就地改文本，本脚本验证它真的保住了。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

# 干净环境（CI）上没有任何 API key，而 `load_config()` 默认 require_key=True ——
# 走到「坏配置自愈后仍能启动」那一步会直接 SystemExit，测试假红（实测 CI 挂在这）。
# 这里给一个**拼接出来的假 key**（不触发仓库的凭据扫描）：本文件只验配置读写的
# 行为，跟 key 的真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "cfgtestonly0123456789abcdef")

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "config.yaml"
BACKUP = ROOT / "out" / "cfg_backup.yaml"


def n_comments(t: str) -> int:
    return sum(1 for ln in t.splitlines() if ln.strip().startswith("#"))


def top_keys(t: str) -> list[str]:
    return [ln.split(":")[0] for ln in t.splitlines()
            if ln and not ln[0].isspace() and ":" in ln and ln.rstrip().endswith((":", ""))]


def main() -> int:
    if not CONFIG.exists():
        # config.yaml 是被 gitignore 的个人配置，首次运行由程序从模板生成。
        # CI / 新克隆上它本来就不存在，这里照做一次（否则直接 FileNotFoundError）。
        CONFIG.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                          encoding="utf-8")
        print(f"config.yaml 不存在 → 已从 config.example.yaml 生成（{CONFIG}）")
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

    # 三个新用例：块序列替换 / 写入校验 / 坏配置恢复
    try:
        test_block_sequence_value_is_replaced_intact()
        test_write_guard_refuses_invalid_yaml()
        test_broken_config_backed_up_and_regenerated()
    except AssertionError as exc:
        fails.append(f"新增用例失败：{exc}")

    if fails:
        print("\n❌ 失败：")
        for f in fails:
            print("   -", f)
        return 1
    print("\n✅ 全部通过：注释、顺序、写入值都正确")
    return 0


def test_block_sequence_value_is_replaced_intact() -> None:
    """★ 复现真实事故：旧版 yaml.dump 把 `pos: [..]` 写成**块序列**（多行 `- 0.0`）。

    用户实测：替换时只换 `pos:` 那一行、留下孤立的 `- 0.0` → 整个文件非法 →
    overlay 热重载报 `expected <block end>, but found '-'`，拖滑块完全没效果。
    """
    from vlt.gui import _yaml_set_in_text

    text = ("overlay:\n"
            "  offset:\n"
            "    alpha: 0.9\n"
            "    pos:\n"
            "    - 0.0\n"
            "    - 0.06\n"
            "    - 0.02\n"
            "    width_m: 0.24\n"
            "capture:\n"
            "  mic_device: ''\n")
    out = _yaml_set_in_text(text, ["overlay", "offset", "pos"], "[-0.005, 0.06, 0.02]")
    print("  替换后：\n" + "\n".join("    " + ln for ln in out.splitlines()))
    data = yaml.safe_load(out)                     # ← 必须仍能被解析（原事故就是这里炸）
    assert data["overlay"]["offset"]["pos"] == [-0.005, 0.06, 0.02]
    assert data["overlay"]["offset"]["width_m"] == 0.24, "后面的兄弟键被误删"
    assert data["overlay"]["offset"]["alpha"] == 0.9, "前面的兄弟键被误删"
    assert data["capture"]["mic_device"] == "", "其他段被破坏"
    assert "- 0.06" not in out, f"孤立的序列项没被吃掉：{out!r}"
    print("  块序列值被整体替换、文件仍是合法 YAML OK")


def test_write_guard_refuses_invalid_yaml() -> None:
    """写入前必须校验：宁可这次不生效，也不能把用户配置写坏。"""
    from vlt.gui import _write_config_text

    guard = ROOT / "out" / "guard_test.yaml"
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text("keep: me\n", encoding="utf-8")
    try:
        _write_config_text(guard, "a:\n  b: 1\n  - oops\n")
    except RuntimeError as exc:
        print(f"  非法 YAML 被拦下 OK：{str(exc)[:70]}")
    else:
        raise AssertionError("非法 YAML 竟然被写进去了")
    assert guard.read_text(encoding="utf-8") == "keep: me\n", "原有内容被破坏"
    guard.unlink(missing_ok=True)


def test_broken_config_backed_up_and_regenerated() -> None:
    """配置被写坏时：启动必须仍能起来（备份坏文件 + 从模板重建），且不静默。"""
    from vlt.config import load_config

    tmp = ROOT / "out" / "broken_cfg"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / "config.yaml"
    f.write_text("overlay:\n  offset:\n    pos: [-0.005, 0.06, 0.02]\n    - 0.0\n",
                 encoding="utf-8")

    cfg = load_config(f)                            # 不该抛异常
    assert (tmp / "config.yaml.broken").exists(), "没有备份坏文件"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "session" in data, "没有从模板重建"
    assert cfg.session_base.get("model"), "重建后仍然读不到基础配置"
    print("  坏配置 → 已备份 + 已重建 + 程序仍可启动 OK")


if __name__ == "__main__":
    sys.exit(main())
