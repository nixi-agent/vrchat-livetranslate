#!/usr/bin/env bash
# 把 VRChat 实时同传打成 **AppImage**（Linux：双击即用，不需要装 Python / 依赖）。
#
# 用法：
#     ./scripts/build_appimage.sh              # 构建 + 构建后自检 + 独立验收
#     ./scripts/build_appimage.sh --no-slim    # 跳过 pyopenxr 瘦身（产物大 ~100 MB 未压缩）
#     ./scripts/build_appimage.sh --no-verify  # 只构建、不做独立验收（产物未验证，别发布）
#
# 产物：dist/VRChatLiveTranslate-x86_64.AppImage
#
# ## 路线（2026-10 起）：PyInstaller onedir → AppDir → appimagetool
#
# 取代旧路线（把 uv 的独立 Python + 整个 site-packages 拷进 AppDir）。理由：
#   * 与 Windows 侧（scripts/build_exe.py）同一条打包工具链，排除清单/坑只有一套要懂；
#   * 依赖按 import 图收集，不再整份 site-packages 搬运；
#   * 产物体积略小（实测 58 MB → ~54 MB 压缩）。
#
# ## 打进去什么、不打包什么
#
# | 内容 | 来源 | 说明 |
# |---|---|---|
# | 解释器 + 依赖 | PyInstaller 按 import 图收集（`.venv`） | onedir：`_internal/` 明文目录 |
# | 程序源码 | `run_gui.py` + `vlt/` | 字节码进 PYZ（不再是明文 .py） |
# | 数据 | `assets/` + `config.example.yaml` + `testdata/` | `--add-data` |
# | Tk | **自编**：Xft + 关 CUPS | 见下 |
#
# ## Tk：为什么要自编，而且还要关掉 CUPS
#
# 两个都跟 uv / python-build-standalone 的 Tk 有关：
#   1. 它可能**不认 fontconfig**（`--enable-xft` 没吃到）→ Tk 只认 X 核心字体，
#      界面上一排**没有文字的控件**（不是「难看」而是「废掉」）。
#   2. 它的 Unix 构建默认 `--enable-libcups`（打印支持），于是 Tk 硬链 `libcups`，
#      顺着 `libcups → libgnutls → p11-kit/nettle/gmp/unistring → avahi/dbus/systemd`
#      拖进 **14 个库（11.8 MB 解压 / ~4.6 MB 压缩）** —— 我们根本不打印。
# 所以构建期用 `--enable-xft --disable-libcups` 编一份同版本 `libtcl9tk9.0.so`
# （缓存到 `build/tools/tk-xft-nocups-<ver>/`），用 `--add-binary` 顶掉 PyInstaller
# 收进来的那份；构建后断言：**必须链 Xft、必须不链 CUPS**。
# （解释器自带的 Tk 只有同时满足这两条才会被直接采用。）
#
# ⚠️ 注意与「打不打包字体」是**两件事**：
#   * Xft 版 Tk —— 让 Tk 看得见 fontconfig 里的字体（本脚本负责）；
#   * 打包字体   —— 保证机器上存在可用的中日韩字体（2026-10 起交给宿主机，见下）。
#
# ## 哪些系统库交给宿主机（反向裁剪）
#
# PyInstaller 默认把 ELF 依赖闭包全带进 `_internal/`（「自包含」）。但桌面基础栈
# 机器上必然有，带一份只是白胖；所以构建后**反向删掉**下面两组（见第 4 步）：
#   * **X11 客户端栈**：libX11 / libXext / libXrender / libXau / libXdmcp / libXss / libXft
#     —— 这套 Wayland 会话（XWayland）也必须有，没有的话 GUI 本来就跑不起来；
#   * **libpipewire** —— 程序对 PipeWire 只用 `pw-dump/pw-record/pw-cat` 命令行，
#     包里那份没有任何代码调用（构建期有「无引用者」断言兜底）。
#   * **保留**：fontconfig/freetype/png/expat/brotli（字体渲染）、libstdc++/libgcc
#     （numpy/OpenBLAS 的 ABI，不赌宿主版本）。
#
# ⚠️ **不再有 `libportaudio / libasound / libjack`**（2026-10）：Linux 上 PortAudio 已彻底出局 ——
# 麦克风走原生 `pw-record`（`vlt/platform/linux.py: LinuxMicSource`），TTS 试听走原生
# `pw-cat --playback`（`vlt/ui_text._play_pcm_local`）。于是 `sounddevice` 也被 `--exclude-module`
# 排除，PyInstaller 分析里根本不会出现 PortAudio 及其依赖 —— 既不用往包里收、也不用再要求构建机装它们。
# （`SoundDeviceMicSource` 已移到 `vlt/platform/win.py`；win.py 同样被 `--exclude-module` 剔除。）
#
# 裁剪后会做两道断言：① 宿主的 ldconfig 真的能提供这些 soname；② 包里其余 ELF 的
# `ldd` 没有 `not found`。⚠️ 运行机缺哪个，症状都是「那一层加载失败」，见 GUIDE.linux。
#
# ## 数据写在哪
#
# AppImage 里是只读 squashfs。`vlt/paths.py` 认 `APPIMAGE`/`APPDIR`，把 config.yaml / logs
# 写到 `$XDG_DATA_HOME/vrchat-livetranslate`（缺省 `~/.local/share/vrchat-livetranslate`）。
#
# ## 独立验收（第 6 步）
#
# 构建完**转调** `scripts/verify_appimage.py`（平台纯度 + 包内导入/反向排除/xr 瘦身 +
# 离线渲染 + Tk 字体）。包内探针做在产物里（`vlt/selfcheck.py` 的 `--verify-*`），
# 因为 PyInstaller 布局下包里没有独立解释器了；那个脚本也能单独对着任意 AppImage 跑
# （CI 就是直接调它，不必重新构建）。
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
APP_ID="vrchat-livetranslate"
APP_NAME="VRChatLiveTranslate"
OUT_DIR="${REPO}/dist"
WORK="${REPO}/build/pyi-appimage"
APPDIR="${WORK}/AppDir"
BUNDLE="${WORK}/pyi/${APP_NAME}"
TOOLS="${REPO}/build/tools"
VENV_PY="${REPO}/.venv/bin/python"
XFT_TK=""                       # ensure_xft_tk() 的产出：要注入包里的 Tk .so
die() { echo "[X] $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }
# ⚠️ 别写 `ldd … | grep -q …`：`set -o pipefail` 下 grep -q 提前退出会给左边的命令发
#    SIGPIPE（141），整条管道被当成失败（ldconfig -p 的输出够大时**必中**，实测踩过）。
_ldd_has() { local out; out="$(ldd "$1" 2>/dev/null || true)"; grep -qi -- "$2" <<<"$out"; }

VERIFY=1
SLIM=1
for _arg in "$@"; do
    case "$_arg" in
        --no-verify) VERIFY=0 ;;
        --no-slim)   SLIM=0 ;;
        *) die "未知参数：$_arg（可用：--no-verify / --no-slim）" ;;
    esac
done
unset _arg

# ---------------------------------------------------------------- 0. 前置检查
step "0/7 检查前置条件"

[ -x "$VENV_PY" ] || die "没有 .venv —— 先跑 ./setup.sh"

# PyInstaller：只在构建机需要（不进产物）；没装就按项目一贯的装法装进 .venv。
if ! "$VENV_PY" -m PyInstaller --version >/dev/null 2>&1; then
    echo "    未装 PyInstaller → 装进 .venv"
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$VENV_PY" "pyinstaller>=6.6" || die "uv 装 PyInstaller 失败"
    else
        "$VENV_PY" -m pip install "pyinstaller>=6.6" || die "pip 装 PyInstaller 失败（.venv 没带 pip？装个 uv 更省事）"
    fi
fi
echo "    PyInstaller : $("$VENV_PY" -m PyInstaller --version)"
echo "    解释器      : $("$VENV_PY" -c 'import sys; print(sys.base_prefix)')"

# appimagetool：本机没有就下官方 release 到 build/tools（**不动系统**）
APPIMAGETOOL=""
resolve_appimagetool() {
    if [ -n "${APPIMAGETOOL:-}" ] && [ -x "$APPIMAGETOOL" ]; then return 0; fi
    if command -v appimagetool >/dev/null 2>&1; then APPIMAGETOOL="$(command -v appimagetool)"; return 0; fi
    local extracted="${TOOLS}/squashfs-root/AppRun"
    if [ -x "$extracted" ]; then APPIMAGETOOL="$extracted"; return 0; fi
    echo "    本机没有 appimagetool → 下官方 release 到 ${TOOLS}（不装进系统）"
    mkdir -p "$TOOLS"
    local url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    curl -sSL --max-time 180 -o "${TOOLS}/appimagetool" "$url" || return 1
    chmod +x "${TOOLS}/appimagetool"
    # 不依赖 FUSE：就地解包，用里面的 AppRun
    ( cd "$TOOLS" && ./appimagetool --appimage-extract >/dev/null 2>&1 ) || return 1
    [ -x "$extracted" ] || return 1
    APPIMAGETOOL="$extracted"
}
resolve_appimagetool || die "拿不到 appimagetool（网络不通？也可以手动装 appimagetool 包后重跑）"
echo "    appimagetool: $APPIMAGETOOL"

# ---------------------------------------------------------------- 1. Xft 版 Tk
step "1/7 准备 Xft 版 Tk（换掉包里的 libtcl9tk9.0.so）"

ensure_xft_tk() {
    # 1) PyInstaller 会收哪份 Tk？—— 构建解释器的 base_prefix 下那份。
    local base tk_lib
    base="$("$VENV_PY" -c 'import sys; print(sys.base_prefix)')"
    tk_lib="$(find "$base/lib" -maxdepth 1 -name 'libtcl9tk9.0.so' -print -quit 2>/dev/null || true)"
    [ -n "$tk_lib" ] && [ -f "$tk_lib" ] \
        || die "找不到解释器自带的 libtcl9tk9.0.so（$base/lib）—— Tk 布局变了？"
    local ver
    ver="$(strings -a "$tk_lib" 2>/dev/null | grep -oE '^9\.[0-9]+\.[0-9]+' | head -1)"
    [ -n "$ver" ] || die "认不出解释器 Tk 的版本（$tk_lib）"

    # 情况 A：解释器自带的 Tk 同时满足「认 fontconfig」+「不链 CUPS」→ 直接注入。
    if _ldd_has "$tk_lib" xft && ! _ldd_has "$tk_lib" cups; then
        XFT_TK="$tk_lib"
        echo "    解释器 Tk ${ver} 已链 Xft 且不链 CUPS → 直接用，无需自编"
        return 0
    fi

    # 情况 B：缓存里有同版本、且「链了 Xft + 没链 CUPS」的自编 Tk（复用前双重校验）。
    # ⚠️ 缓存**必须按 Tcl/Tk 版本分**：uv 升级了 Tcl/Tk 却命中旧缓存 = ABI 不匹配，
    #    可能在用户机器上崩而 CI 全绿（历史上踩过）。
    # ⚠️ 目录名带 `nocups`：旧缓存是带 CUPS 的版本，绝不能命中（否则 14 个库又回来了）。
    local cache="${TOOLS}/tk-xft-nocups-${ver}"
    local cached_ok=0
    if [ -f "${cache}/libtcl9tk9.0.so" ]; then
        local cver
        cver="$(strings -a "${cache}/libtcl9tk9.0.so" 2>/dev/null \
                | grep -oE '^9\.[0-9]+\.[0-9]+' | head -1)"
        if [ "$cver" = "$ver" ] \
           && _ldd_has "${cache}/libtcl9tk9.0.so" Xft \
           && ! _ldd_has "${cache}/libtcl9tk9.0.so" cups; then
            cached_ok=1
        else
            echo "    ⚠️ 缓存里的 Tk 版本/特性不符（要 ${ver}、链 Xft、不链 CUPS）→ 重新编译"
        fi
    fi

    if [ "$cached_ok" -eq 0 ]; then
        echo "    需要自编 Tk ${ver}（Xft + 关 CUPS；首次较慢，之后缓存）"
        # 源码目录同样按版本分：老版本解出来的 tcl-*/tk-* 留着会让 find 抓到错的源。
        local tag="core-$(echo "$ver" | tr '.' '-')"      # 9.0.4 → core-9-0-4
        local src="${TOOLS}/tcltk-src-${ver}"
        mkdir -p "$src"
        for pkg in tcl tk; do
            [ -f "${src}/${pkg}.tar.gz" ] ||                 curl -sSL --max-time 300 -o "${src}/${pkg}.tar.gz" \
                     "https://github.com/tcltk/${pkg}/archive/refs/tags/${tag}.tar.gz" \
                || die "下载 ${pkg} ${ver} 源码失败"
            ( cd "$src" && tar xzf "${pkg}.tar.gz" ) || die "解包 ${pkg} 失败"
        done
        local tdir; tdir="$(find "$src" -maxdepth 1 -type d -name 'tcl-*' | head -1)"
        local kdir; kdir="$(find "$src" -maxdepth 1 -type d -name 'tk-*'  | head -1)"
        [ -n "$tdir" ] && [ -n "$kdir" ] || die "找不到解出来的源码目录"
        local pfx="${TOOLS}/tcltk-prefix"
        rm -rf "$pfx"; mkdir -p "$pfx"
        # Tcl 只是 Tk 的构建依赖 —— 编出来**不进包**，所以装到 build/tools 里
        # ⚠️ configure 后必须 `make clean`：源码目录是复用的，上一轮（可能带 CUPS）编出来的
        #    .o 还在，不清理会拿旧目标文件去链接 → `undefined reference to cups*`（实测踩过）。
        ( cd "${tdir}/unix" && ./configure --prefix="$pfx" --enable-shared --enable-threads >/dev/null 2>&1 \
          && make clean >/dev/null 2>&1 \
          && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1 ) \
          || die "编译 Tcl 失败（构建机缺 X11/freetype/fontconfig 开发头文件？）"
        # ★ 关键 1：--enable-xft；★ 关键 2：--disable-libcups（不打印，别拖 14 个库进来）
        ( cd "${kdir}/unix" && ./configure --prefix="$pfx" --enable-shared --enable-threads \
            --with-tcl="$pfx/lib" --enable-xft --disable-libcups >/dev/null 2>&1 \
          && make clean >/dev/null 2>&1 \
          && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1 ) \
          || die "编译 Tk 失败（构建机缺 X11/freetype/fontconfig 开发头文件？）"
        [ -f "${pfx}/lib/libtcl9tk9.0.so" ] || die "编完了却没产出 libtcl9tk9.0.so"
        _ldd_has "${pfx}/lib/libtcl9tk9.0.so" xft \
            || die "编出来的 Tk 没链 Xft —— configure 没吃到 --enable-xft"
        if _ldd_has "${pfx}/lib/libtcl9tk9.0.so" cups; then
            die "编出来的 Tk 还链着 CUPS —— configure 没吃到 --disable-libcups"
        fi
        mkdir -p "$cache"
        cp "${pfx}/lib/libtcl9tk9.0.so" "${cache}/"
    else
        echo "    用缓存里编好的 Tk（${cache}，Tcl/Tk ${ver}，Xft+无 CUPS）"
    fi
    XFT_TK="${cache}/libtcl9tk9.0.so"
}

ensure_xft_tk
echo "    注入的 Tk   : $XFT_TK"

# ---------------------------------------------------------------- 2. PyInstaller onedir
step "2/7 PyInstaller onedir 打包"

rm -rf "$WORK"
mkdir -p "$WORK/pyi" "$WORK/work" "$WORK/spec"

# 版本溯源：构建期把 git describe 烘焙成 buildinfo.json（冻结产物里没有 .git，
# 见 vlt/version.py；拿不到就写 null → 产物显示 +unknown，绝不假装正式版）。
BUILDINFO="${REPO}/build/buildinfo.json"
"$VENV_PY" "$REPO/scripts/gen_buildinfo.py" "$BUILDINFO" || die "生成 buildinfo.json 失败"

# 动态导入的模块（静态分析看不到）——磁盘上的 vlt 模块**全部**列在这里；
# 漏一个 = 用户拿到手 ImportError。第 3 步有一次「磁盘 vs 包内」对账断言兜底。
HIDDEN=(
    vlt vlt.app vlt.config vlt.config_io vlt.crashlog vlt.credentials vlt.devices
    vlt.engine vlt.gui vlt.i18n vlt.level_probe vlt.paths vlt.textin vlt.tts
    vlt.update_check vlt.version vlt.voices
    vlt.selfcheck                      # --verify-* 自检入口（验收脚本用；run_gui.py 按需导入）
    # 界面语言是 importlib 按语言码动态加载的（vlt/i18n.py）—— 不列就会静默回落中文
    vlt.locales vlt.locales.en vlt.locales.ja vlt.locales.ko vlt.locales.ru
    vlt.output vlt.output.chatbox vlt.output.merger vlt.output.overlay
    vlt.output.virtualmic
    vlt.output.openxr_overlay          # Linux 手腕屏后端（由 vlt/platform/linux.py 收）
    vlt.platform vlt.platform.audio vlt.platform.base vlt.platform.linux
    vlt.platform.wayland vlt.platform.x11 vlt.platform.overlay_pixels   # 桌面字幕原生窗（layer-shell / ARGB）+ 共用像素工具
    vlt.session vlt.session.base vlt.session.qwen38
    # 第三方：按需导入 / 运行时加载（音频第三方只在 Windows 侧，见文件头「反向裁剪」）
    miniaudio _miniaudio pythonosc websockets yaml
    PIL PIL.Image PIL.ImageDraw PIL.ImageTk numpy xr
    # ★ Wayland 下 PyOpenGL 会挑 `egl` 平台插件（OpenGL/platform/__init__.py 的
    #   plugin 匹配：XDG_SESSION_TYPE=wayland / WAYLAND_DISPLAY → "wayland"→EGLPlatform）。
    #   而 PyInstaller 官方的 hook-OpenGL 只收 `OpenGL.platform.glx` —— 少了这个模块，
    #   `import OpenGL.platform` 会走 `plugin.load() → None` 再 `None()` →
    #   `TypeError: 'NoneType' object is not callable`，把 `import xr` 整条链拖死
    #   （实测：用户 Wayland 会话下 OpenXR 手腕屏直接起不来）。
    OpenGL.platform.egl
    # ★ `PIL._tkinter_finder` 是被 `PIL/features.py` 用**字符串**引用的
    #   （importlib.import_module("PIL._tkinter_finder")），静态分析看不到 ——
    #   少了它，ImageTk.PhotoImage 一用就 `ModuleNotFoundError`，
    #   赞助弹窗的两张收款码会降级成「二维码图片缺失」（实测踩过）。
    PIL._tkinter_finder
)
# 带二进制/数据文件的库 → 连数据一起收
COLLECT_ALL=(xr pythonosc)
# Linux 产物里**不允许**出现 Windows 独占实现（与 build_exe.py 的 EXCLUDE_WIN 镜像）
EXCLUDES=(vlt.platform.win vlt.output.openvr_overlay vlt.output.win_d3d11 openvr pyaudiowpatch pycaw comtypes sounddevice)

PYI_ARGS=(
    --noconfirm --clean --noupx --onedir
    --name "$APP_NAME"
    --distpath "$WORK/pyi" --workpath "$WORK/work" --specpath "$WORK/spec"
    --paths "$REPO"
    --add-data "${REPO}/assets:assets"
    --add-data "${REPO}/testdata:testdata"
    --add-data "${REPO}/config.example.yaml:."
    --add-data "${BUILDINFO}:."          # 版本溯源（git describe，见 vlt/version.py）
    --add-binary "${XFT_TK}:."          # 顶掉 PyInstaller 收到的解释器自带那份
)
for h in "${HIDDEN[@]}";      do PYI_ARGS+=(--hidden-import "$h"); done
for c in "${COLLECT_ALL[@]}"; do PYI_ARGS+=(--collect-all "$c"); done
for e in "${EXCLUDES[@]}";    do PYI_ARGS+=(--exclude-module "$e"); done

PYI_LOG="${WORK}/pyinstaller.log"
echo "    （日志：${PYI_LOG}）"
if ! "$VENV_PY" -m PyInstaller "${PYI_ARGS[@]}" "$REPO/run_gui.py" >"$PYI_LOG" 2>&1; then
    tail -60 "$PYI_LOG" >&2
    die "PyInstaller 打包失败（完整日志：$PYI_LOG）"
fi
tail -4 "$PYI_LOG"
[ -x "$BUNDLE/$APP_NAME" ] || die "PyInstaller 结束但没产出 $BUNDLE/$APP_NAME"

# ---------------------------------------------------------------- 3. 构建后自检
step "3/7 构建后自检（数据 / Tk / 模块对账）"
_INT="$BUNDLE/_internal"

for f in "assets/app.png" "config.example.yaml" "testdata/zh_test_16k.pcm" "buildinfo.json"; do
    [ -e "$_INT/$f" ] || die "包内缺数据文件：$_INT/$f"
done
echo "    ✅ 数据文件齐（assets / config.example.yaml / testdata / buildinfo.json）"

[ -f "$_INT/libtcl9tk9.0.so" ] || die "包里没有 libtcl9tk9.0.so（--add-binary 没落地？）"
_ldd_has "$_INT/libtcl9tk9.0.so" xft \
    || die "包里的 Tk 不链 Xft/fontconfig —— 字体问题会直接废掉界面"
if _ldd_has "$_INT/libtcl9tk9.0.so" cups; then
    die "包里的 Tk 还链着 CUPS —— 会把 GnuTLS 那串（14 个库）拖回来（--disable-libcups 没生效？）"
fi
echo "    ✅ 包里的 Tk：认 fontconfig（Xft）、不链 CUPS"

# 模块对账：① 禁列模块一个不许有；② 磁盘上的 vlt 模块一个不许少。
# ② 才是关键：漏收模块（动态导入）不会在构建时炸，只会在用户机器上 ImportError。
"$VENV_PY" - "$BUNDLE/$APP_NAME" "$REPO/vlt" <<'PY' || die "模块对账没通过"
import pathlib, sys
from PyInstaller.archive.readers import pkg_archive_contents

exe, vlt_dir = sys.argv[1], pathlib.Path(sys.argv[2])
names = set(pkg_archive_contents(exe))

# ⚠️ 这份集合与上面的 EXCLUDES 是**两份**清单，加新的平台独占模块时必须同时改；
#    tests/test_platform_purity.py 现在会断言两份的 vlt.* 集合一致（漏改即红）。
forbidden = {"vlt.platform.win", "vlt.output.openvr_overlay", "vlt.output.win_d3d11"}
bad = sorted(n for n in names if any(n == f or n.startswith(f + ".") for f in forbidden))
assert not bad, f"包里混进了 Windows 独占模块：{bad}"

disk: set[str] = set()
for p in vlt_dir.rglob("*.py"):
    rel = p.relative_to(vlt_dir.parent).with_suffix("")
    if rel.name == "__init__":
        rel = rel.parent
    name = ".".join(rel.parts)
    if name not in forbidden:
        disk.add(name)
missing = sorted(disk - names)
assert not missing, f"磁盘上有、包里没有的 vlt 模块（用户拿到手就是 ImportError）：{missing}"

# 第三方里「靠运行时环境动态挑」的模块，PyInstaller 的 hook 覆盖不全 —— 单独钉住。
# 加新项时写清「为什么静态分析看不到」。
must_have = {
    # Wayland 的 PyOpenGL 平台插件（hook-OpenGL 只收 glx；见 build 脚本 HIDDEN 注释）
    "OpenGL.platform.egl",
    # Pillow 的 features.py 用字符串引用（importlib.import_module）：
    # 少了它，ImageTk.PhotoImage 直接 ModuleNotFoundError（赞助弹窗收款码降级）
    "PIL._tkinter_finder",
}
missing_dynamic = sorted(must_have - names)
assert not missing_dynamic, f"第三方动态模块缺失（运行期才会炸）：{missing_dynamic}"

print(f"    ✅ 模块对账通过：{len(names)} 个条目，vlt 模块一个不少、Windows 独占一个不多")
PY

# ---------------------------------------------------------------- 4. 瘦身
step "4/7 瘦身（pyopenxr 平台目录 / xr 调试层 / 系统库交还宿主机）"

# ⚠️ 保留集**绝不能硬编码目录名**：pyopenxr 1.1.6302 把平台目录改了名
#    （win32→windows_x86_64、aarch64→linux_aarch64、x86_64→linux_x86_64、
#    android→android_arm_v8a）。硬编码的删法会静默变成空操作（v0.5.0 白胖 ~13MB）。
#    为什么探针跑在**构建解释器**上而不是包内：包里的 pyopenxr 代码在 PYZ 归档里、
#    只有平台目录是明文文件，没法用宿主解释器 import；venv 里的同名同版本包会给出
#    同样的答案（两边都来自同一次 collect-all）。判据仍是「让 pyopenxr 自己说」。
_XR="$_INT/xr"
if [ "$SLIM" -eq 1 ]; then
    [ -d "$_XR" ] || die "找不到 $_XR —— pyopenxr 的布局变了？瘦身规则要跟着改"

    _KEEP_OUT="$(PYTHONPATH= "$VENV_PY" -P - <<'PY'
import os
from pathlib import Path
import xr.api_layer, xr.library
from xr.api_layer.layer_path import py_layer_library_path

# api_layer：import 期要暴露的目录（若环境里已有 XR_API_LAYER_PATH 也应一并保留）
api = {Path(py_layer_library_path()).parent.name}
for _p in os.environ.get("XR_API_LAYER_PATH", "").split(os.pathsep):
    if _p.strip():
        api.add(Path(_p).name)
# library：运行期 dlopen 的 loader 所在目录
lib = Path(xr.library.openxr_loader_library._name).parent.name
for _n in sorted(api):
    print("API_KEEP=" + _n)
print("LIB_KEEP=" + lib)
PY
)" || die "调 pyopenxr 探针拿平台目录失败（venv 里的 pyopenxr 能 import 吗？）"

    mapfile -t XR_API_KEEP < <(printf '%s\n' "$_KEEP_OUT" | sed -n 's/^API_KEEP=//p')
    mapfile -t XR_LIB_KEEP < <(printf '%s\n' "$_KEEP_OUT" | sed -n 's/^LIB_KEEP=//p')
    [ "${#XR_API_KEEP[@]}" -gt 0 ] && [ "${#XR_LIB_KEEP[@]}" -gt 0 ] \
        || die "pyopenxr 探针没给出平台目录：${_KEEP_OUT:-（无输出）}"
    echo "    pyopenxr 自报当前平台目录：api_layer=${XR_API_KEEP[*]}  library=${XR_LIB_KEEP[*]}"

    # 删掉两个目录下**除保留集以外**的所有子目录（保留 __pycache__ / 顶层文件）
    _purge_other_platforms() {
        local base="$1"; shift
        local d name keep ok
        for d in "$base"/*/; do
            [ -d "$d" ] || continue
            name="$(basename "$d")"
            [ "$name" = "__pycache__" ] && continue
            ok=0
            for keep in "$@"; do [ "$name" = "$keep" ] && ok=1; done
            [ "$ok" -eq 1 ] || { rm -rf "$d"; echo "       - 删平台目录 $name"; }
        done
    }
    _purge_other_platforms "$_XR/api_layer" "${XR_API_KEEP[@]}"
    _purge_other_platforms "$_XR/library"   "${XR_LIB_KEEP[@]}"
    rm -f "$_XR/library/openxr_loader.dll" 2>/dev/null || true  # 顶层散落的 win loader（若有）

    # ★ 再砍一刀：三个**调试用** API layer（api_dump / core_validation / best_practices）。
    #   它们只在显式 `xr.api_layer.activate_*_layer()` / `XR_ENABLE_API_LAYERS` 时才被加载，
    #   项目里零调用；但 .so 有 16.6MB 解压 / ~2.2MB 压缩。保留 python 层与 loader
    #   （`import xr.api_layer` 期就要 `py_layer_library_path()` 指向的文件在）。
    for _k in "${XR_API_KEEP[@]}"; do
        _dir="$_XR/api_layer/$_k"
        _before=$(du -sk "$_dir" | cut -f1)
        rm -f "$_dir"/libXrApiLayer_{api_dump,core_validation,best_practices_validation}.so \
              "$_dir"/XrApiLayer_{api_dump,core_validation,best_practices_validation}.json
        _after=$(du -sk "$_dir" | cut -f1)
        [ -f "$_dir/libXrApiLayer_python.so" ] \
            || die "调试层瘦身误删：$_k/libXrApiLayer_python.so 不见了"
        echo "       - 调试层瘦身 $_k：$(( _before / 1024 ))MB → $(( _after / 1024 ))MB"
    done

    # 断言保留集没被误删：错了就在这里炸，而不是发到用户手里才「启动即崩」
    for _k in "${XR_API_KEEP[@]}"; do
        [ -d "$_XR/api_layer/$_k" ] \
            || die "瘦身误删：api_layer/$_k 目录不见了（import xr 会直接失败）"
    done
    for _k in "${XR_LIB_KEEP[@]}"; do
        [ -f "$_XR/library/$_k/libopenxr_loader.so" ] \
            || die "瘦身误删：library/$_k/libopenxr_loader.so 不见了（import xr 会直接失败）"
    done

    # ★ 体积门禁：光靠「上面断言还在」挡不住「该删的没删」。留一条硬上限兜底。
    XR_SLIM_MAX_MB="${XR_SLIM_MAX_MB:-60}"   # 瘦身后实测 ~25MB；未瘦身 ~126MB
    _xr_mb=$(( $(du -sk "$_XR" | cut -f1) / 1024 ))
    [ "$_xr_mb" -le "$XR_SLIM_MAX_MB" ] || die \
        "xr 瘦身后仍为 ${_xr_mb}MB（上限 ${XR_SLIM_MAX_MB}MB）—— 瘦身没生效？大概率是 pyopenxr 又改了平台目录名"
    echo "    ✅ xr 已瘦身：只留 ${XR_API_KEEP[*]}（$(du -sh "$_XR" | cut -f1)）"
else
    echo "    （已按 --no-slim 跳过 xr 瘦身：产物会大 ~100MB 未压缩）"
fi

# ---- 系统库反向裁剪：桌面基础栈交给宿主机（理由见文件头「哪些系统库交给宿主机」）----
# PyInstaller 默认把依赖闭包全带进 `_internal/`；这两组机器上必然有，带一份只是白胖。
HOST_LIBS=(
    libX11 libXext libXrender libXau libXdmcp libXss libXft   # X11 客户端栈（XWayland 也在用）
    libpipewire                                               # 程序只用宿主 pw-* CLI，包里那份无人调用
)
for _n in "${HOST_LIBS[@]}"; do
    # ① 构建机必须真的能提供这个 soname —— 否则包在**任何**机器上都少一个库。
    #    （用 grep 不带 -q：不能提前退出把 ldconfig 打成 SIGPIPE，见 _ldd_has 注释）
    #    注意不能带尾部点号：PipeWire 的 soname 是 `libpipewire-0.3.so.0`，不是 `libpipewire.`
    if ! ldconfig -p 2>/dev/null | grep -F -- "$_n" >/dev/null; then
        die "构建机没有 $_n（ldconfig -p 查不到）—— 这台机器装不出「交宿主机」的包"
    fi
    shopt -s nullglob; _hit=( "$_INT/$_n"* ); shopt -u nullglob
    if [ "${#_hit[@]}" -gt 0 ]; then
        rm -f "${_hit[@]}"
        echo "       - 交宿主机 $_n（${#_hit[@]} 个文件）"
    else
        # 没收集到也 OK（目标状态就是「包里没有」）；但留一行，方便对照构建日志
        echo "       - 交宿主机 $_n（本来就没收集，跳过）"
    fi
done

# ---- 清「孤儿」：PyInstaller 是照**解释器自带的 Tk**（带 CUPS）分析依赖的，
#      我们 --add-binary 换成无 CUPS 的 Tk 后，libcups→GnuTLS→…→systemd 这 14 个库
#      没有任何东西再引用 —— 纯死重（11.8MB 解压 / ~4.6MB 压缩）。
#      PyInstaller 没有「不收集某个 .so」的选项，只能在构建后清；清完由下面的
#      `ldd 无 not found` 断言证明真的没人需要它们。
TK_ORPHANS=(
    libcups libgnutls libp11-kit libunistring libgmp libnettle libhogweed
    libidn2 libtasn1 libavahi-client libavahi-common libdbus-1 libleancrypto libsystemd
)
for _n in "${TK_ORPHANS[@]}"; do
    shopt -s nullglob; _hit=( "$_INT/$_n"* ); shopt -u nullglob
    if [ "${#_hit[@]}" -gt 0 ]; then
        rm -f "${_hit[@]}"
        echo "       - 清孤儿 $_n（${#_hit[@]} 个文件）"
    fi
done

# ② 删干净没有（软链也要清掉，否则加载器还能从包里摸到）
for _n in "${HOST_LIBS[@]}" "${TK_ORPHANS[@]}"; do
    shopt -s nullglob; _left=( "$_INT/$_n"* ); shopt -u nullglob
    [ "${#_left[@]}" -eq 0 ] || die "反向裁剪失败：$_n* 还在包里（${_left[*]}）"
done
echo "    ✅ 已交还宿主机：${HOST_LIBS[*]}"

# ④ 剩下所有 ELF 的 ldd 不许有 not found（构建机上判；宿主缺库不在这里暴露）
_missing=""
while IFS= read -r -d '' _f; do
    _bad="$(ldd "$_f" 2>/dev/null | grep 'not found' || true)"
    [ -n "$_bad" ] && _missing+="$_f -> $_bad"$'\n'
done < <(find "$_INT" "$BUNDLE" -type f \
         \( -name '*.so' -o -name '*.so.*' -o -name "$APP_NAME" \) -print0)
[ -z "$_missing" ] || die "反向裁剪后有 ELF 依赖缺失：
$_missing"
echo "    ✅ 包内 ELF 依赖完整（ldd 无 not found）"

# ---------------------------------------------------------------- 5. AppDir + AppImage
step "5/7 组装 AppDir 并生成 AppImage"

mkdir -p "$APPDIR/usr/bin" "$OUT_DIR"
cp -r "$BUNDLE" "$APPDIR/usr/bin/$APP_NAME"

cat > "$APPDIR/AppRun" <<'RUN'
#!/usr/bin/env bash
# AppImage 入口。AppImage 不沙盒，环境变量原样继承（WAYLAND_DISPLAY / XDG_RUNTIME_DIR /
# DASHSCOPE_API_KEY 等都能正常用）。
set -euo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONUTF8=1
# 字体：AppImage **不自带任何字体**，直接用宿主机的 fontconfig（系统里已装的字体照常可用）。
# 所以这里不注入 FONTCONFIG_FILE —— 运行机需要自带中日韩字体，详见脚本头部说明。
exec "$HERE/usr/bin/VRChatLiveTranslate/VRChatLiveTranslate" "$@"
RUN
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/${APP_ID}.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=VRChat LiveTranslate
Name[zh_CN]=VRChat 实时同传
Comment=Real-time speech translation for VRChat
Comment[zh_CN]=在 VRChat 里做实时同声传译
Exec=vlt-gui
Icon=${APP_ID}
Categories=AudioVideo;Audio;Utility;
Terminal=false
StartupWMClass=Tk
DESK
cp "$REPO/assets/app.png" "$APPDIR/${APP_ID}.png"
ln -sf "${APP_ID}.png" "$APPDIR/.DirIcon"

OUT_IMG="$OUT_DIR/${APP_NAME}-x86_64.AppImage"
rm -f "$OUT_IMG"
# 压缩参数：appimagetool 默认 zstd L15 + 128K 块；换 L19 + 1M 块实测 52.8MB → 49.2MB
# （内容一字不改，只是压得更狠；1M 是 squashfs 允许的最大块，AppImage runtime 支持）。
ARCH=x86_64 "$APPIMAGETOOL" --no-appstream \
    --comp zstd \
    --mksquashfs-opt -b --mksquashfs-opt 1M \
    --mksquashfs-opt -Xcompression-level --mksquashfs-opt 19 \
    "$APPDIR" "$OUT_IMG" 2>&1 | tail -5
[ -f "$OUT_IMG" ] || die "没产出 AppImage"
chmod +x "$OUT_IMG"
echo "    ✅ $OUT_IMG（$(du -h "$OUT_IMG" | cut -f1)）"

# ---------------------------------------------------------------- 6. 独立验收
#
# 验收逻辑**不写在这里**：它要能单独对着任意 AppImage 跑（CI 直接调它，不必重新构建），
# 见 scripts/verify_appimage.py。这一步只是构建流程里的自动转调。
if [ "$VERIFY" -eq 1 ]; then
    step "6/7 AppImage 独立验收（平台纯度 + 包内导入 + xr 瘦身 + 离线渲染 + 字体）"
    if [ "$SLIM" -eq 1 ]; then
        "$VENV_PY" "$REPO/scripts/verify_appimage.py" "$OUT_IMG"
    else
        # --no-slim 时别去要求「瘦身已生效」，否则两个开关自相矛盾
        "$VENV_PY" "$REPO/scripts/verify_appimage.py" "$OUT_IMG" --allow-fat
    fi || die "AppImage 验收未通过（上面有明细）"
else
    step "6/7 独立验收已按 --no-verify 跳过"
    echo "    ⚠️ 跳过 = **未验证**：产物已生成，但平台隔离/导入/渲染都没检查过。"
    echo "       补跑：$VENV_PY scripts/verify_appimage.py $OUT_IMG"
fi

# ---------------------------------------------------------------- 7. 完成
step "7/7 完成"
# ⚠️ 本段是**未加引号的 heredoc**：`$VAR` / `$(...)` / 反引号都会被 shell 展开。要输出字面量的
#    代码格式（反引号）必须写成 \`...\`，否则会被**当命令执行**（实测踩过：`pw-record` 真被跑了、
#    还打印了帮助，文案被吞成空）。新增含反引号的行时务必转义。
cat <<EOF
产物：$OUT_IMG（$(du -h "$OUT_IMG" | cut -f1)）

双击即可运行（图形界面）。数据写在：
    \${XDG_DATA_HOME:-~/.local/share}/vrchat-livetranslate/   （config.yaml / logs / out）

注意：
  * 桌面会话 Wayland / X11 都行（手腕屏：Wayland 走 EGL、X11 走 GLX）
  * 手腕屏还需要 **OpenXR 运行时已起 + 头显已连**（Monado / WiVRn）
  * 译音虚拟声卡由程序运行时自己声明，**不需要**事先装 VB-Cable 之类
  * 需要宿主自带一套中日韩字体
  * X11 基础库由宿主提供；音频**全部走宿主的 PipeWire**（\`pw-dump/pw-record/pw-cat\`）：
    麦克风也走 \`pw-record\`，**不需要** portaudio / JACK / PulseAudio 兼容层
  * 详见 GUIDE.linux.md
EOF
