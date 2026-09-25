"""可写目录解析的验收：源码 / 打包 / 绿色版 三种形态，以及老配置迁移。

## 为什么值得单独测

打包后 `__file__` 指向解包临时目录，写进去的东西进程一退就没了（用户"什么都没留下"）。
所以规则必须钉死，而且**三种形态**容易互相踩：

| 形态                     | APP_DIR                          | BUNDLE_DIR |
| ------------------------ | -------------------------------- | ---------- |
| 源码运行                 | 仓库根                            | 仓库根     |
| exe（正常）              | `%APPDATA%\\vrchat-livetranslate` | `_MEIPASS` |
| exe + `portable.txt`     | exe 所在目录（绿色版）             | `_MEIPASS` |

另外：老版本把 config.yaml 放在 exe 旁边，升级后必须**自动搬**到新位置，
否则用户会看到"配置突然变回默认"，很难自行判断。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _reload(executable: Path, meipass: Path, appdata: Path, frozen: bool = True):
    """把 sys.frozen / sys.executable / sys._MEIPASS / APPDATA 摆成目标形态后重载模块。"""
    import vlt.paths as p

    saved = {k: os.environ.get("APPDATA") for k in ("APPDATA",)}
    os.environ["APPDATA"] = str(appdata)
    old = (getattr(sys, "frozen", None), sys.executable, getattr(sys, "_MEIPASS", None))
    if frozen:
        sys.frozen = True          # type: ignore[attr-defined]
    else:
        sys.__dict__.pop("frozen", None)
    sys.executable = str(executable)
    sys._MEIPASS = str(meipass)    # type: ignore[attr-defined]
    mod = importlib.reload(p)
    return mod, saved, old


def _restore(saved, old) -> None:
    import vlt.paths as p

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    frozen, exe, meipass = old
    if frozen is None:
        sys.__dict__.pop("frozen", None)
    else:
        sys.frozen = frozen        # type: ignore[attr-defined]
    sys.executable = exe
    if meipass is None:
        sys.__dict__.pop("_MEIPASS", None)
    else:
        sys._MEIPASS = meipass     # type: ignore[attr-defined]
    importlib.reload(p)


def test_source_mode_uses_repo_root() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vlt-src-"))
    mod, saved, old = _reload(tmp / "app.exe", tmp, tmp, frozen=False)
    try:
        assert mod.app_dir() == ROOT, f"源码模式 APP_DIR 应为仓库根，实际 {mod.app_dir()}"
        assert mod.bundle_dir() == ROOT, "源码模式 BUNDLE_DIR 也应是仓库根"
        assert mod.APP_DIR == ROOT, "模块级常量也要一致（应用读的是常量）"
        print(f"  源码模式 OK（APP_DIR = {mod.APP_DIR}）")
    finally:
        _restore(saved, old)


def test_frozen_uses_appdata() -> None:
    exe_dir = Path(tempfile.mkdtemp(prefix="vlt-exe-"))
    meipass = Path(tempfile.mkdtemp(prefix="vlt-meipass-"))
    appdata = Path(tempfile.mkdtemp(prefix="vlt-appdata-"))
    mod, saved, old = _reload(exe_dir / "app.exe", meipass, appdata)
    try:
        want = appdata / "vrchat-livetranslate"
        assert mod.app_dir() == want, f"打包后 APP_DIR 应为 {want}，实际 {mod.app_dir()}"
        assert mod.bundle_dir() == meipass, f"打包后 BUNDLE_DIR 应为 _MEIPASS，实际 {mod.bundle_dir()}"
        got = mod.ensure_app_dir()
        assert got.exists() and got == want, "ensure_app_dir 应创建 %APPDATA% 下的目录"
        print(f"  打包模式 OK（APP_DIR = {mod.app_dir()}，BUNDLE_DIR = _MEIPASS）")
    finally:
        _restore(saved, old)


def test_portable_marker_overrides() -> None:
    exe_dir = Path(tempfile.mkdtemp(prefix="vlt-portable-"))
    (exe_dir / "portable.txt").write_text("绿色版：配置与日志放 exe 同目录", encoding="utf-8")
    meipass = Path(tempfile.mkdtemp(prefix="vlt-meipass2-"))
    appdata = Path(tempfile.mkdtemp(prefix="vlt-appdata2-"))
    mod, saved, old = _reload(exe_dir / "app.exe", meipass, appdata)
    try:
        assert mod.is_portable(), "有 portable.txt 应判定为绿色版"
        # 用 samefile 比「是不是同一个目录」：Windows 上 mkdtemp 的路径与
        # resolve() 出来的大小写/短名形式可能不同（CI runner 上实测就是这样），
        # 直接比字符串会假红。
        assert os.path.samefile(mod.app_dir(), exe_dir), \
            f"绿色版 APP_DIR 应为 exe 目录，实际 {mod.app_dir()}"
        print("  绿色版（portable.txt）OK —— APP_DIR = exe 所在目录")
    finally:
        _restore(saved, old)


def test_legacy_config_migrated_once() -> None:
    """老版本放在 exe 旁边的 config.yaml 必须搬到 %APPDATA%，且不覆盖新位置已有文件。"""
    exe_dir = Path(tempfile.mkdtemp(prefix="vlt-legacy-"))
    (exe_dir / "app.exe").write_bytes(b"")
    (exe_dir / "config.yaml").write_text("# 老配置（里面是用户设过的设备名）\n", encoding="utf-8")
    (exe_dir / "logs").mkdir()
    (exe_dir / "logs" / "gui_20260101_000000.log").write_text("旧日志", encoding="utf-8")
    meipass = Path(tempfile.mkdtemp(prefix="vlt-meipass3-"))
    appdata = Path(tempfile.mkdtemp(prefix="vlt-appdata3-"))
    mod, saved, old = _reload(exe_dir / "app.exe", meipass, appdata)
    try:
        moved = mod.migrate_legacy_files()
        new_cfg = mod.app_dir() / "config.yaml"
        assert new_cfg.exists(), "旧 config.yaml 没有被搬到新位置"
        assert "老配置" in new_cfg.read_text(encoding="utf-8"), "搬过去的内容不对"
        assert (mod.app_dir() / "logs" / "gui_20260101_000000.log").exists(), "旧日志没搬"
        assert moved, "应返回搬迁记录（用于日志说明）"
        print(f"  老配置迁移 OK（{len(moved)} 项）")

        # 已存在则不覆盖：新位置改成一个标记，再跑一次迁移应保持不动
        new_cfg.write_text("# 新位置的配置\n", encoding="utf-8")
        mod.migrate_legacy_files()
        assert "新位置的配置" in new_cfg.read_text(encoding="utf-8"), "迁移不该覆盖新位置已有文件"
        print("  迁移不覆盖已有文件 OK")
    finally:
        _restore(saved, old)


if __name__ == "__main__":
    print("test_paths:")
    test_source_mode_uses_repo_root()
    test_frozen_uses_appdata()
    test_portable_marker_overrides()
    test_legacy_config_migrated_once()
    print("ALL PASSED")
