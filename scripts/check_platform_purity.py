"""断言打包产物里**没有混进另一个平台的实现**。

用法：
    python scripts/check_platform_purity.py dist/VRChatLiveTranslate.exe --platform windows
    python scripts/check_platform_purity.py dist/VRChatLiveTranslate.AppImage --platform linux

退出码：0 = 干净；1 = 发现违规（CI 里当红灯用）。

## 为什么要这个脚本

「Windows 版不含 pipewire / openxr」「Linux 版不含 WASAPI 那套」这两条要求，
如果只写在构建脚本的注释里，迟早有人加个 `--hidden-import` 就破了 ——
而且破了没有任何征兆，只是 exe 悄悄变胖、或者在别的机器上冒出莫名其妙的报错。

所以把它变成**可执行的断言**：构建完自动跑，红了就说明隔离被破坏。

## 判据为什么不是「搜 exe 原始字节」

单文件 exe 里的 PYZ 是 **zlib 压缩**的，直接对 exe 做字符串搜索永远搜不到
（这个坑 `scripts/verify_release.py` 的注释里已经记过一次）。正确做法是
用 PyInstaller 自己的读取器解出目录表与各条目字节，再搜解压后的内容。

* 模块级判据：`pkg_archive_contents()` 列出的名字（含 PYZ 内的模块名）
* 内容级判据：逐条目 `extract()` 出字节再搜（能抓到内嵌的 XML / conf 常量）

## AppImage 侧怎么读（两种产物两条路径）

AppImage 是 **squashfs**，没有 PyInstaller 那套读取器。这里用 AppImage 运行时自带的
`--appimage-extract` 就地解包（不要求宿主装 squashfs-tools），然后按**内层布局**分两路：

* **PyInstaller onedir（2026-10 起的官方布局）**：`usr/bin/<名>/<名>` 是内层可执行文件，
  代码与实现字样都在它的 PYZ 里 —— 直接复用 exe 那条读取路径（`pkg_archive_contents`
  列模块名、逐条目取内容）。⚠️ 不做这条分支的话，旧分支在 `usr/app/` 找不到任何 `.py`，
  两条判据**双双空过**（假绿）——这正是这个脚本必须跟着构建路线改的原因。
* 旧布局（`usr/app/` 明文源码 + `usr/python`）——保留兼容，但新构建不再产出；
  模块级判据 = `usr/app/**/*.py` 的包路径 + `usr/python/.../site-packages` 的顶层包名；
  内容级判据 = 只扫 `usr/app/**/*.py`（编译后取常量，与 exe 路径同语义）。

## 字样判据为什么排除 docstring

见 `_string_blobs`：共享模块的 docstring 里经常要写「Windows 用 X、Linux 用 Y」的对照
说明，那是文档而不是实现。只有实现代码（常量 + 名字）参与判据。
"""
from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
from typing import Iterator

# 每个平台**不允许**出现的东西。
#
#   modules —— 模块名（exe：PyInstaller 的 TOC/内嵌 PYZ；AppImage：内层 onedir
#              可执行文件的 PYZ），靠构建脚本的 --exclude-module（Linux 见
#              build_appimage.sh 的 EXCLUDES；Windows 见 build_exe.py 的 EXCLUDE_WIN）保证
#   strings —— **实现**里不允许出现的字样（函数名、API 名、平台工具名）
#
# ⚠️ 加新平台独占模块时，**同时**加进 scripts/build_exe.py 的 EXCLUDE_WIN
#    （或 AppImage 构建脚本的反向排除），否则这里会红。
#
# ⚠️ 字样判据**只扫实现，不扫 docstring**（见 `_string_blobs` 的说明）：
#    共享模块的模块/类/函数 docstring 里经常要写「Windows 用 X、Linux 用 Y」这种
#    对照说明，那是给人看的文档，不是被带进产物的实现。历史上有人把
#    "文档字符串也算" 写进注释，结果整条 Linux 侧判据从来没跑过（因为一跑就红）。
FORBIDDEN: dict[str, dict[str, list[str]]] = {
    "windows": {
        "modules": [
            "vlt.platform.linux",          # PipeWire 设备枚举/采集/虚拟声卡（pw-dump/record/cat）
            "vlt.platform.wayland",        # 原生 layer-shell 桌面叠加窗（libwayland-client）
            "vlt.platform.x11",            # 原生 ARGB 桌面叠加窗（libX11）
            "vlt.output.openxr_overlay",   # 自建 OpenXR 手腕屏（pyopenxr + EGL/Wayland）
            "vlt.output.micproxy_linux",   # Linux 麦克风代理（pw-cat 管道 + 运行时声明虚拟麦）
            "xr",                          # pyopenxr 本体
        ],
        "strings": [
            # Linux 侧的平台工具名/API 名：只该出现在上面那几个被排除的模块里。
            b"libpipewire-module-loopback",
            b"pw-dump",
            b"pw-loopback",
            b"libwayland-client",
            b"libX11.so.6",
            b"eglGetPlatformDisplay",
            b"GraphicsBindingEGLMNDX",
            b"XR_MNDX_egl_enable",
            b"XR_EXTX_overlay",
            b"pyopenxr",
        ],
    },
    "linux": {
        "modules": [
            "vlt.platform.win",            # WASAPI / pyaudiowpatch 那套
            "vlt.output.openvr_overlay",   # SteamVR 手腕屏后端（Windows 独占）
            "vlt.output.win_d3d11",        # 持久 D3D11 贴图（SetOverlayTexture 上传；上一条的依赖）
            "openvr",                      # SteamVR 接口本体（上一条的依赖）
            "pyaudiowpatch",
            "pycaw",
            "comtypes",
        ],
        "strings": [
            b"pyaudiowpatch",
            b"paWASAPI",
            b"VoiceMeeter",
            b"VB-Audio",
            b"msyh.ttc",                   # Windows 专有字体路径
            b"GetUserDefaultUILanguage",   # Win32 语言探测（Linux 侧读 LC_ALL/LANG）
            # SteamVR/openvr 后端的实现标记：模块整份删除后，这些字样也不该出现
            b"pyopenvr",
            b"IVROverlay",
            b"VRApplication_Background",
            b"HmdMatrix34_t",
            b"D3D11CreateDevice",          # 持久 D3D11 贴图后端（win_d3d11）的实现标记
        ],
    },
}


def _fail(msg: str) -> None:
    print(f"  ❌ {msg}")


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def load_names(artifact: Path) -> list[str]:
    """产物里收录的全部名字（含 PYZ 内部模块）。"""
    from PyInstaller.archive.readers import pkg_archive_contents
    return list(pkg_archive_contents(str(artifact)))


def is_appimage(artifact: Path) -> bool:
    """按扩展名认 AppImage（我们的产物就叫 `*.AppImage`）。"""
    return artifact.name.endswith(".AppImage")


def _appdir_module_name(rel: Path) -> str:
    """AppDir 里的相对路径 → 点分模块名（`vlt/output/openvr_overlay.py` →
    `vlt.output.openvr_overlay`；`vlt/output/__init__.py` → `vlt.output`）。"""
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_docstring(code, idx: int, const) -> bool:
    """第 idx 个常量是不是该 code 对象的 docstring。

    * 模块 / 函数 / lambda：docstring 在 `co_consts[0]`；
    * **类体**：`co_consts[0]` 存的是**类名**（`__qualname__` 常量），docstring 在 `[1]`
      —— 只看 `[0]` 会把类 docstring 误判成实现代码（这个坑真踩过）。

    参见本文件顶部「字样判据为什么排除 docstring」。
    """
    if not isinstance(const, str):
        return False
    consts = code.co_consts
    if idx == 0:
        return True
    return idx == 1 and isinstance(consts[0], str) and consts[0] == code.co_name


def _string_blobs(node) -> Iterator[bytes]:
    """从 code 对象里递归取出**实现用**的字符串/字节常量与标识符名（供子串搜索）。

    ⚠️ **docstring 不参与**（见 `_is_docstring`）：共享模块的 docstring 里要写
    「Windows 用 X、Linux 用 Y」的对照说明，那是文档不是实现；把它算进来只会逼着
    大家把说明写残（历史上这条 Linux 判据正因为此从未跑过）。

    为什么不直接 `marshal.dumps(code)`：那要求检查时用的解释器与打包时**完全同版本**，
    否则 marshal 反序列化会失败。直接遍历 `co_consts` / `co_names` 没有版本约束，
    而且更精确 —— 不会像原始字节搜索那样撞上无关的字节序列。
    """
    import types
    stack = [node]
    while stack:
        cur = stack.pop()
        for i, const in enumerate(getattr(cur, "co_consts", ()) or ()):
            if isinstance(const, types.CodeType):
                stack.append(const)          # 函数/类内部还有一层
            elif _is_docstring(cur, i, const):
                continue                     # 文档，不是实现
            elif isinstance(const, bytes):
                yield const
            elif isinstance(const, str):
                yield const.encode("utf-8", "replace")
        for name in getattr(cur, "co_names", ()) or ():
            if isinstance(name, str):
                yield name.encode("utf-8", "replace")


def iter_entry_bytes(artifact: Path):
    """产出 (名字, 字节) —— 逐条目解压，含 PYZ 内的每条模块。

    ⚠️ 判类型要用 PyInstaller 自己的常量（`PKG_ITEM_PYZ` 的值是 `'z'` 不是 `'PYZ'`）——
    写字符串字面量会静默不匹配，于是 PYZ 从不被递归、检查全部漏过（踩过）。
    ⚠️ `ZlibArchiveReader.extract()` 返回的是 **code 对象**而不是字节（也踩过）。
    """
    from PyInstaller.archive.readers import (CArchiveReader, PKG_ITEM_PYZ,
                                             PKG_ITEM_ZIPFILE)
    arch = CArchiveReader(str(artifact))
    for name, toc_entry in arch.toc.items():
        *_, typecode = toc_entry
        try:
            if typecode == PKG_ITEM_PYZ:
                pyz = arch.open_embedded_archive(name)
                for mod in pyz.toc:
                    try:
                        data = pyz.extract(mod)
                    except Exception:        # noqa: BLE001 — 个别条目解不出不致命
                        continue
                    if isinstance(data, (bytes, bytearray)):
                        yield mod, bytes(data)
                    else:
                        for blob in _string_blobs(data):
                            yield mod, blob
            elif typecode == PKG_ITEM_ZIPFILE:
                # base_library.zip：CPython 标准库的 .pyc。里面不会有本项目的字样，
                # 但顺手搜掉，免得将来有人把东西塞进 zip 就绕过了检查。
                import io
                import zipfile
                with zipfile.ZipFile(io.BytesIO(arch.extract(name))) as zf:
                    for member in zf.namelist():
                        try:
                            yield f"{name}!{member}", zf.read(member)
                        except Exception:    # noqa: BLE001
                            continue
            else:
                yield name, arch.extract(name)
        except Exception:                    # noqa: BLE001
            continue


@contextlib.contextmanager
def appdir(artifact: Path) -> Iterator[Path]:
    """解出 AppImage 的 AppDir 根目录（临时目录，退出即删）。

    用 AppImage 运行时自带的 `--appimage-extract`，**不要求宿主装 squashfs-tools**；
    这也是 `scripts/build_appimage.sh` 的冒烟步骤用的同一招。
    """
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix="vlt-purity-") as tmp:
        try:
            proc = subprocess.run([str(artifact.resolve()), "--appimage-extract"],
                                  cwd=tmp, capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"执行 --appimage-extract 失败：{exc}") from exc
        root = Path(tmp) / "squashfs-root"
        if not root.is_dir():
            raise RuntimeError("--appimage-extract 没产出 squashfs-root："
                               + proc.stderr.decode("utf-8", "replace")[:300])
        yield root


def appimage_names(root: Path) -> list[str]:
    """AppImage 的模块级判据输入。

    * `usr/app/**/*.py` → 我们的包路径（`vlt.output.openvr_overlay` 这种）；
    * `usr/python/lib/python*/site-packages/*` → 第三方依赖的**顶层**包/模块名
      （AppImage 把整份 site-packages 都打进去了，`pyaudiowpatch` 之类的名字要能抓到）。
    """
    names: list[str] = []
    app = root / "usr" / "app"
    if app.is_dir():
        names += [_appdir_module_name(p.relative_to(app)) for p in app.rglob("*.py")]
    for site in (root / "usr" / "python" / "lib").glob("python*/site-packages"):
        for child in sorted(site.iterdir()):
            names.append(child.name[:-3] if child.suffix == ".py" else child.name)
    return sorted(names)


def appimage_entry_bytes(root: Path) -> Iterator[tuple[str, bytes]]:
    """AppImage 的实现条目：`usr/app/**/*.py` 编译后取常量（与 exe 路径同语义）。

    只扫我们自己的源码，不扫 site-packages（第三方库不看字样）也不扫资源/模板
    （`config.example.yaml` 这类本来就是跨平台的东西，详见文件顶部说明）。
    """
    app = root / "usr" / "app"
    if not app.is_dir():
        return
    for p in sorted(app.rglob("*.py")):
        name = _appdir_module_name(p.relative_to(app))
        try:
            code = compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except (OSError, SyntaxError):
            continue
        for blob in _string_blobs(code):
            yield name, blob


def _string_scope_ok(name: str, prefixes: tuple[str, ...]) -> bool:
    """某条 PYZ/档案条目的名字是否在「字样判据」的扫描范围内（见 `_judge` 注释）。"""
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _judge(platform: str, rules: dict, names: list[str],
           entries: Iterator[tuple[str, bytes]],
           string_scope: tuple[str, ...] = ("vlt", "run_gui")) -> bool:
    """两条判据都在这里：模块名（硬）+ 实现字样。

    `string_scope`：字样判据**只扫我们自己的实现**（`vlt.*` 与入口 `run_gui`）。
    第三方库的代码本来就是跨平台实现（sounddevice 里就有 PortAudio 的 `paWASAPI`
    常量），拿「某平台禁列」去扫第三方是**误报**；而「第三方有没有混进来」由
    判据 1（模块名）负责 —— 想让某个第三方被拦，就把它加进 `FORBIDDEN[...]["modules"]`
    （例：Linux 侧的 `openvr`），别指望字样碰运气。
    """
    print(f"   收录条目：{len(names)}")
    ok = True

    # ---- 判据 1：模块名（最硬的一条，直接反映 --exclude-module 有没有生效）
    name_set = set(names)
    for mod in rules["modules"]:
        hits = sorted(n for n in name_set
                      if n == mod or n.startswith(mod + "."))
        if hits:
            _fail(f"混进了 {platform} 不该有的模块：{hits[:5]}"
                  f"{' …' if len(hits) > 5 else ''}")
            ok = False
    if ok:
        _ok(f"模块级干净（{len(rules['modules'])} 条禁列一条都没混进来）")

    # ---- 判据 2：内嵌内容里的字样（实现代码 / 资源常量 / 平台工具名）
    needles = rules["strings"]
    found: dict[bytes, list[str]] = {}
    for name, data in entries:
        if not _string_scope_ok(name, string_scope):
            continue
        for needle in needles:
            if needle in data:
                found.setdefault(needle, []).append(name)
    for needle in needles:
        if needle in found:
            where = found[needle][:3]
            _fail(f"内嵌内容里出现 {needle.decode()}（来自 {where}）")
            ok = False
    if not found:
        _ok(f"内容级干净（{len(needles)} 条禁列字样一处都没出现；扫描域：{string_scope}）")

    print(("== 结论：干净 ==" if ok else "== 结论：**隔离被破坏** =="))
    if not ok:
        print("   修法：把对应模块加进构建排除清单（Windows 见 scripts/build_exe.py 的 "
              "EXCLUDE_WIN；Linux 见 scripts/build_appimage.sh 的 EXCLUDES），"
              "并确认它没有出现在共享代码的顶层 import 里。")
    return ok


def _frozen_exe(root: Path) -> Path | None:
    """PyInstaller onedir 布局里的内层可执行文件（`usr/bin/<名>/<名>`，同级有 `_internal/`）。"""
    for internal in sorted(root.glob("usr/bin/*/_internal")):
        exe = internal.parent / internal.parent.name
        if exe.is_file():
            return exe
    return None


def check(artifact: Path, platform: str) -> bool:
    """按产物类型选读取路径，再交给 `_judge` 判两条。"""
    rules = FORBIDDEN[platform]
    print(f"== 检查 {platform} 产物：{artifact.name} ==")
    print(f"   大小：{artifact.stat().st_size / 1024 / 1024:.1f} MB")

    if is_appimage(artifact):
        try:
            with appdir(artifact) as root:
                frozen = _frozen_exe(root)
                if frozen is not None:
                    # PyInstaller onedir：复用 exe 的读取路径（见文件头说明）
                    return _judge(platform, rules, load_names(frozen),
                                  iter_entry_bytes(frozen))
                return _judge(platform, rules, appimage_names(root),
                              appimage_entry_bytes(root))
        except Exception as exc:             # noqa: BLE001
            _fail(f"解包失败：{type(exc).__name__}: {exc}")
            return False

    try:
        names = load_names(artifact)
    except ImportError:
        print("  ⚠️ 装不上 PyInstaller（需要它的读取器解包）→ **跳过 = 未验证**")
        print("     装法：pip install pyinstaller")
        return False
    except Exception as exc:                 # noqa: BLE001
        _fail(f"解包失败：{type(exc).__name__}: {exc}")
        return False
    return _judge(platform, rules, names, iter_entry_bytes(artifact))


def main() -> int:
    ap = argparse.ArgumentParser(description="断言打包产物没有混进另一个平台的实现")
    ap.add_argument("artifact", type=Path, help="打包产物（单文件 exe / 目录 / AppImage）")
    ap.add_argument("--platform", choices=sorted(FORBIDDEN), required=True)
    args = ap.parse_args()

    if not args.artifact.exists():
        print(f"找不到产物：{args.artifact}")
        return 2
    return 0 if check(args.artifact, args.platform) else 1


if __name__ == "__main__":
    raise SystemExit(main())
