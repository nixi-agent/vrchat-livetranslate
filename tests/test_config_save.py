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
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

# 这个用例用中文下拉标签操作控件（`gui._anchor_combo.set("外部 tracker")`）。
# 界面语言会跟随系统语言（CI 与外国机器是英文系统）→ 必须钉死，
# 否则同一份代码在不同机器上结果不同。产品代码不依赖这个补丁。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

# 钉死语言还不够：配置里的 ui.lang 优先于系统语言检测（gui.py）。开发者本机
# config.yaml 若是 lang: en，中文下拉标签就失效；且本用例断言「注释不被破坏」，
# 需要一份带注释的配置。统一走沙箱（内容 = config.example.yaml，与 CI 一致）。
from _cfgbox import sandbox_config  # noqa: E402

CONFIG = sandbox_config(reset=True)   # 沙箱配置路径（已同步改写 gui.DEFAULT_CONFIG）
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
    # 手腕屏微调 / 桌面字幕调整两页的控件随设置弹窗在**启动时**就建好了（弹窗只是隐藏着），
    # 所以这里不点开设置也能直接操作控件 —— 原来那行 `_toggle_tune_panel()` 已随面板进设置作废。

    # 模拟用户操作：改锚点 + 拖滑块 + tracker 序号 + 切译音开关 + 改语言
    # ⚠️ 顺序要跟真界面一致：先切锚点（会把该锚点那一份回填到滑块），再拖滑块。
    #    反过来的话，`_load_anchor_offset` 会把刚拖的值覆盖掉 —— 那不是 bug，是
    #    「换锚点当然显示新锚点的值」。
    gui._anchor_combo.set("外部 tracker")
    gui._on_anchor_change()          # 真界面里是下拉事件触发的：先载入该锚点那一份，再落盘
    start = {k: v for k, v in gui._tune_values.items()}      # 该锚点这一份的起点
    gui._tune_values["pos_x"] = -0.075
    gui._tune_values["width_m"] = 0.31
    gui._tune_values["curvature"] = 0.15
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
    # ⚠️ 位姿要落在**当前锚点那一份**（overlay.offsets.tracker），而不是把
    #    overlay.offset 改掉 —— 后者是别的锚点的兜底，改了等于换个锚点就丢一份位姿。
    data = yaml.safe_load(after)
    ov = data.get("overlay") or {}
    offsets = ov.get("offsets") or {}
    # 别的锚点必须原封不动（「各存各的」）：两边都摘掉本次动过的 tracker 再比对
    off_before = ((yaml.safe_load(before) or {}).get("overlay") or {}).get("offsets") or {}
    other_after = {k: v for k, v in offsets.items() if k != "tracker"}
    other_before = {k: v for k, v in off_before.items() if k != "tracker"}
    for name, got, want in [
        ("overlay.anchor", ov.get("anchor"), "tracker"),
        ("overlay.tracker_index", ov.get("tracker_index"), 1),
        ("overlay.offsets.tracker.pos", (offsets.get("tracker") or {}).get("pos"),
         [-0.075, start["pos_y"], start["pos_z"]]),
        ("overlay.offsets.tracker.rot", (offsets.get("tracker") or {}).get("rot"),
         [start["rot_x"], start["rot_y"], start["rot_z"]]),
        ("overlay.offset.width_m", (ov.get("offset") or {}).get("width_m"), 0.31),
        ("overlay.offset.curvature", (ov.get("offset") or {}).get("curvature"), 0.15),
        ("overlay.enabled", ov.get("enabled"), True),
        # 别的锚点那一份必须原封不动（这就是「各存各的」）
        ("overlay.offsets 里别的锚点", other_after, other_before),
        ("overlay.offset.pos（兜底不该被写）", (ov.get("offset") or {}).get("pos"),
         ((yaml.safe_load(before) or {}).get("overlay") or {}).get("offset", {}).get("pos")),
    ]:
        if got != want:
            fails.append(f"{name} 写错了：{got!r}（期望 {want!r}）")
        else:
            print(f"  ✓ {name} = {got!r}")
    for want in ("source_lang: ja", "target_lang: zh"):
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

    # 四个新用例：块序列替换 / 写入校验 / 坏配置恢复 / 深层注释不被吃掉 / 补建+复检
    try:
        test_block_sequence_value_is_replaced_intact()
        test_write_guard_refuses_invalid_yaml()
        test_broken_config_backed_up_and_regenerated()
        test_deep_indented_comments_survive_leaf_writes()
        test_set_or_create_checked_catches_silent_noop()
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


def test_deep_indented_comments_survive_leaf_writes() -> None:
    """★ 回归（PR #4 审查）：比所属键缩进更深的说明注释，不能被就地写入顺手删掉。

    旧版 `_yaml_set_in_text` 的删除分支把「键之后更深缩进的块」整段删掉，而配置里
    新增的说明注释恰好写成这种深层续行 —— 拖一次滑块会删 5 行、选一次设备再多删
    1 行，丢的正是「换左手要镜像」和「tracker 按 role 寻址」这两处最该留住的。
    """
    from vlt.gui import _yaml_set_in_text, _yaml_set_or_create

    template = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    base = n_comments(template)

    cases = [
        (["overlay", "offset", "rot"], "[-47, 16, 0]", "换到左手要镜像"),
        # 位姿现在是按锚点分开存的（overlay.offsets.<锚点>.pos/rot）—— 这条路径比
        # `overlay.offset` 还深一层，同样不许把上面的说明注释吃掉。
        (["overlay", "offsets", "left_hand", "rot"], "[-47, 16, 0]", "左手不能照抄右手"),
        (["overlay", "tracker_index"], "1", "按 role 寻址"),
        (["capture", "loopback_device"], '"foo"', "VRChat 输出到的那个 sink"),
        (["overlay", "font"], '""', "Linux 上留空即可"),
    ]
    for path, value, marker in cases:
        # 与界面同一条调用路径：`offsets.*` 在老配置里可能整段不存在，要用 or_create
        setter = _yaml_set_or_create if "offsets" in path else _yaml_set_in_text
        out = setter(template, path, value)
        assert n_comments(out) == base, (
            f"{'.'.join(path)} 写入后注释被破坏：{base} → {n_comments(out)}")
        assert marker in out, f"{'.'.join(path)} 的说明注释丢了：{marker!r}"
        data = yaml.safe_load(out)                 # 删的时候别把 YAML 弄坏
        assert data is not None, f"{'.'.join(path)} 写入后不再是合法 YAML"
    print(f"  {len(cases)} 处深层缩进注释在就地写入后一字不少（各 {base} 行注释）OK")


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


def test_set_or_create_checked_catches_silent_noop() -> None:
    """缺父键时**补建**，而「补建也没写进去 / 值写歪」必须抛 —— 不许像 `_yaml_set_in_text` 那样静默返回原文。

    用户报的缺陷就是这种静默 no-op：老 config.yaml 里没有 `output.audio.proxy` 整段时，
    写 `proxy.hint_shown` 原样返回（不抛不报），`_write_config_text` 写回同一份文本，
    调用方以为成功了 → 「首次启用说明」每次启动都弹。
    """
    from vlt import config_io

    # 老用户的配置形态：`output.audio` 在，但**没有** proxy 子段
    legacy = ("ui:\n  lang: zh\noutput:\n  audio:\n"
              "    enabled: false           # 总开关\n    buffer_ms: 300\n")
    path = ["output", "audio", "proxy", "hint_shown"]

    # ① 旧函数：父键缺失 → **原样返回**（静默 no-op，把它的语义钉死）
    assert config_io._yaml_set_in_text(legacy, path, "true") == legacy, \
        "前提：旧函数对缺失父键应原样返回（这就是那条静默 no-op）"

    # ② 新函数：补建整条链，写后复检通过；原有键与注释一个字不少
    out = config_io._yaml_set_or_create_checked(legacy, path, "true")
    audio = ((yaml.safe_load(out) or {}).get("output") or {}).get("audio") or {}
    assert (audio.get("proxy") or {}).get("hint_shown") is True, f"没补建出标记：{audio!r}"
    assert audio.get("buffer_ms") == 300 and "# 总开关" in out, f"原有内容被动了：{out!r}"
    print("  ✓ 缺父键 → 补建整条链并复检通过（hint_shown 落进 output.audio.proxy，原键与注释都在）")

    # ③ 故障注入：让「补建」也退化成静默 no-op（返回原文）→ 复检必须**抛**，不许静默
    real = config_io._yaml_set_or_create
    config_io._yaml_set_or_create = lambda text, _p, _v: text          # type: ignore[assignment]
    try:
        try:
            config_io._yaml_set_or_create_checked(legacy, path, "true")
        except RuntimeError as exc:
            assert "没能写进配置" in str(exc), f"异常信息要说清是「没写进去」：{exc}"
            print(f"  ✓ 没生效时响亮报错 OK：{str(exc)[:60]}…")
        else:
            raise AssertionError("写不进去竟然没抛（静默降级）")
    finally:
        config_io._yaml_set_or_create = real                          # type: ignore[assignment]

    # ④ 故障注入：写进去了但**值不对**（写成 false）→ 同样要抛
    config_io._yaml_set_or_create = lambda text, p, _v: real(text, p, "false")
    try:
        try:
            config_io._yaml_set_or_create_checked(legacy, path, "true")
        except RuntimeError as exc:
            assert "不是期望的" in str(exc), f"异常信息要说清值不对：{exc}"
            print(f"  ✓ 值写歪时响亮报错 OK：{str(exc)[:60]}…")
        else:
            raise AssertionError("值不对竟然没抛（静默降级）")
    finally:
        config_io._yaml_set_or_create = real                          # type: ignore[assignment]

    # ⑤ 已存在的键走就地替换（不整文件重写）：注释一行不少
    template = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "hint_shown" in template, "前提：模板里已有这个键"
    out2 = config_io._yaml_set_or_create_checked(template, path, "true")
    assert n_comments(out2) == n_comments(template), "就地改写的取舍被破坏了（注释行数变了）"
    print("  ✓ 已存在的键：就地替换（注释行数不变）")


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
