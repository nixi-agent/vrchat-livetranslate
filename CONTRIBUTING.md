# 参与贡献

谢谢你有兴趣帮这个项目做点事 🙏

这份文档专门给**第一次来的人**补上「怎么开始、怎么验、提 PR 要带什么」。仓库的
[README](README.md) 面向使用者，这里面向**改代码 / 改文档 / 改翻译**的人。

> 一句话版本：**能跑起来 → 本地测试全绿 → commit 说清做了什么 → PR 里贴出命令和原始输出。**
> 做不到全绿也欢迎先开 issue 或 draft PR 讨论。

README 里还有一份[许愿列表](README.md)：那些是维护者想做、但希望别人来认领的事
（教程视频、图文教程、母语校对……）。看到哪条觉得「这个我能做」，不用先问，直接动手就行。

---

## 一、先把项目跑起来

项目是 **Python 3.11 + Tkinter**。源码运行**两个平台各有独立指南，不要照抄对方**：

- **Windows** → [使用指南 docs/GUIDE.md](docs/GUIDE.md)：跑 `setup.bat` 建 `.venv` 并装依赖；
  API key 怎么填、界面怎么用、怎么排障都在那份里。
- **Linux** → [Linux 使用指南 docs/GUIDE.linux.md](docs/GUIDE.linux.md)：跑 `./setup.sh`；
  依赖清单、虚拟声卡、手腕屏、AppImage、排障都在那份里。

> 本文件**不复述**安装步骤 —— 抄一份就会有两份各自过期。安装出问题请对照上面两份指南。

想先看懂结构再动手 → **[docs/dev/](docs/dev/索引.md)**（开发者文档入口）：分层与两条音频链路、
每个模块一行职责、验收与门禁在哪几层、**按症状查的踩坑索引**。它只做「地图与索引」，
同样不复述上面那些内容。

解释器一律用**仓库自己的 venv**，不要用裸 `python`（本机与 CI 都**没有 `python3` 这个命令**）：

```bat
:: Windows（在仓库根目录）
.venv\Scripts\python.exe -m vlt.gui
```

```bash
# Linux
./run_gui.sh
```

---

## 二、跑测试

**这个项目不用 pytest。** 每个 `tests/test_*.py` 都是**能单独执行的脚本**：全绿时打印
`OK` / `ALL PASSED` 并以退出码 0 结束，失败时退出码非 0。跑法就是逐文件执行：

```bat
:: Windows —— 单个
.venv\Scripts\python.exe tests\test_virtualmic.py

:: Windows —— 全部（★ 与 CI 同一条命令：仓库运行器）
.venv\Scripts\python.exe scripts\run_tests.py
```

```bash
# Linux —— 单个（★ 必须挂 xvfb-run，理由见下面第 3 条）
xvfb-run -a ./.venv/bin/python tests/test_virtualmic.py

# Linux —— 全部（★ 与 CI 同一条命令；run_tests.py 会自动逐用例挂 xvfb-run）
./.venv/bin/python scripts/run_tests.py
```

> `scripts/run_tests.py` 是**本机与 CI 共用的唯一运行器**（CI 的两个 job 就是直接调它）：
> 逐文件跑 `tests/test_*.py`、默认跳过 `test_engine.py`（见下面第 2 条）、`--only <关键词>`
> 过滤、`--coverage` 出覆盖率摘要。它在 **Linux 上给每个用例自动挂 `xvfb-run -a`**（每用例
> 一个干净虚拟 X，虚拟屏显式钉成 `1920x1080`），Windows / macOS 自带桌面会话则不挂。
> 找不到 `xvfb-run` 时本机只打印提示（可 `--no-xvfb` 明确裸跑）；**CI 传
> `--require-tk --require-xvfb`，缺依赖直接判红**，不会静默少跑一批 GUI 用例。

几条**必须知道**的规矩：

- **测试必须离线**。除下面那个例外，任何用例都不许联网、要麦克风、要 VRChat 或 SteamVR。
  想自证离线，可以挂个死代理再跑一遍（例如 `HTTP_PROXY=http://127.0.0.1:1`）仍然全绿才算数。
- **唯一例外是 `tests/test_engine.py`**：它要打一次**真实**会话，需要本机已配好 API key，
  CI 里**显式跳过**（workflow 里有 `::notice::` 写明原因）。它是本机实测项，别为了「变绿」
  删掉或改松。
- **Linux 上必须挂 `xvfb-run -a` 跑**（`run_tests.py` 会自动做；CI 的 `linux-tests` 同口径）：
  好几个用例会**建真窗口**再回读几何，平铺窗口管理器（Hyprland / niri / sway…）会把窗口
  重排成满屏，于是它们全部**假红**（实测：不挂 xvfb 时 `test_desktop_overlay*.py`、
  `test_i18n.py` 报的是几何/窗口宽度不符，挂上就全绿）。`xvfb-run -a` 给每个用例一个
  无窗口管理器的虚拟 X，与 CI 完全一致。缺 `xvfb-run` 就装 `xorg-server-xvfb`。
- **CI 跑在英文系统上，虚拟屏钉成 1920x1080**（`run_tests.py` 里显式 `-screen 0 1920x1080x24`，
  不再依赖发行版默认）。凡是本机是中文 Windows、断言了中文界面文案，或者
  假设窗口很宽 —— 本地绿了 CI 照样红（历史上真踩过：本机中文系统全绿、GitHub CI 一片红）。
  涉及界面语言的用例要在**构造窗口之前**把语言钉死。
- **全量测试一次只跑一份**。用例会共用 `out/` 沙箱和仓库根的 `config.yaml`；并发跑、或上一轮
  被中途杀掉，会互相踩出**假红**（同一份代码能跑出好几种结果）。要采信就独占跑一遍再下结论。
- 跑全量时你会看到 **60+ 个用例**逐一打印结果，`test_engine.py` 之外全绿即可。

---

## 三、提交信息怎么写

仓库实际在用的是 **Conventional Commits 前缀 + 中文描述**。历史里出现过的类型：

| 前缀 | 用在哪 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 文档（README 各语言版本、GUIDE、平台约束记录等） |
| `chore` | 杂项（依赖、构建脚本、注释等） |
| `test` | 只动测试 |
| `ci` | 只动 `.github/workflows/` |
| `perf` | 性能 |
| `refactor` / `polish` / `release` | 也出现过，按语义用 |

写法示例（**描述用中文说清「做了什么、为什么」**，别只写 `fix bug`）：

```
fix(session): 快封句的判据换成电平信号 —— 修「距上次上送音频的间隔」在真链路恒 ~0.1s
docs(contributing): 补齐面向外部贡献者的入口
test(i18n): 新增词表 key 覆盖的守卫用例
```

一条 commit 只做一件事；不相关的东西请拆开提交。

---

## 四、提 PR 之前

**硬性要求：在 GitHub 上被审之前，你本地要先跑完全量测试并且全绿。** 请先 `rebase` 到最新的
`main`，别让 reviewer 替你发现红。

PR 描述请按仓库的 **[PR 模板](.github/pull_request_template.md)** 填，其中两栏**不能留空**：

- **验证证据**：你**实际跑过**的命令 + **原始输出**（贴日志，不要只写「测试通过」）。改了什么就验
  什么；没跑到的部分**如实写明「未验证」**，这比含糊其辞有用得多。
- **真机结论**：涉及硬件 / 平台的行为（音频设备、虚拟声卡、手腕屏、UI 布局……）必须写清
  **哪台机器、什么硬件、实测到什么**。本机测不了的（例如 Windows 上没有 PipeWire / 头显）就老实写
  「未在本机复现，依据 X」。

改了文档要顺带过一遍链接检查：

```bat
.venv\Scripts\python.exe scripts\check_doc_links.py
```

> ⚠️ 维护者侧：CI 只在 push `main` 或开 PR 时自动跑，**推自己的分支不会自动触发**。需要时可以
> `gh workflow run ci.yml --ref <你的分支>` 手动跑一次。

---

## 五、改词表 / 翻译：不需要碰代码

界面文案走 i18n：`vlt/i18n.py` 里 `t("中文原文")` 的 key 就是**中文原文**，各语言词表在
`vlt/locales/<code>.py` 的 `STRINGS`（`zh` 是基准语言，没有词表）。也就是说
**`vlt/locales/<语言>.py` 就是一张「中文原文 → 你的语言」的对照表** —— 想改翻译，直接改这张表里的
值、提 PR 即可，**不用动任何逻辑代码**。

- 现在支持：`zh`（基准）/ `en` / `ja` / `ko` / `ru`。
- `tests/test_i18n.py` 会强制校验**每套词表**都覆盖全部 `t()` key、且条数一致 —— 漏翻一条直接红。
  所以你只管把值改对：**别删 key、别改 key**。
- 机械守卫只查「key 覆盖 + 书写系统」，**抓不住错字 / 断字 / 语感不对**。逐条读一遍你的语言，
  比让脚本变绿重要得多。

**加一门新语言**远不止加一个词表文件，至少还要动 `vlt/gui.py` 的 `SOURCE_LANGS` / `TARGET_LANGS`
（**两边都要加**）、`vlt/tts.py` 的 `LANG_NAMES`、其它 `locales/*.py` 里补上这门语言的译名，
以及**字体覆盖**（很多 CJK 字体不含某些语言的字形，缺字体就是一排豆腐块）。动手前先开个 issue
说一声，维护者会告诉你当前的口径。

---

## 六、不要做的事

- **不要把 API key 提交进版本库。** 仓库带了 pre-commit 钩子（`scripts/check_no_secrets.py`，
  装法见 `install_secret_guard.bat`），会扫暂存区拦下 `sk-…`、GitHub token、`Bearer …` 等形态。
  ⚠️ **别指望它**：它只是模式匹配，新形态的 key、被拼出来的 key 可能漏。正确做法是让 key 只走
  配置 / 凭据存储 / 环境变量，**任何情况下都不写进文件**。key 一旦进了 git 历史，下一个 commit
  删掉也没用 —— 得 rewrite 历史。
- **不要改 [docs/平台约束记录.md](docs/平台约束记录.md) 里记着的硬约束**（例如 Linux 虚拟声卡的
  `media.class`、手腕屏的图层 flag、构建期平台隔离……）。那些不是风格选择，每一条背后都有
  **实测事故**。要改可以，但必须带上**你自己的实测依据**（复现步骤 + 结果），让维护者能判断，
  而不是「看起来多此一举就去掉」。
- **不要在提交里夹带个人数据**：真实 API key、设备名、`%APPDATA%` 下的真实路径、日志里别人的
  昵称 / 聊天内容、`config.yaml`（它在 `.gitignore` 里，入库的只有 `config.example.yaml`）。
- **不要动没被要求动的文件**：一次 PR 只解决一件事，别顺手重排格式或改无关文件，那会让 reviewer
  分不清哪些是你真正改的。

---

## 七、发版检查清单（维护者用）

> 发布由维护者手工触发，**PR 作者不用管这一节**。
>
> ⚠️ [release.yml](.github/workflows/release.yml) 里 `generate_release_notes: false` 是**明确要求**：
> 自动附的「What's Changed / New Contributors / Full Changelog」都不要，每个版本的说明是**手写**的
> —— 所以下面每一步都不能省，也**不要**去建议启用自动生成。

打 tag（`v*`）会触发发布流程，按顺序过一遍：

- [ ] **版本号对账**：`vlt/__init__.py` 的 `__version__` 与要打的 tag **必须一致**（workflow 会硬校验，
      不一致直接失败）。先改代码、提交，再打 tag。
- [ ] **五语文档同步**：README 五语（`README.md` + `docs/README.{en,ja,ko,ru}.md`）与 GUIDE 五语
      （`docs/GUIDE.md` + `docs/GUIDE.{en,ja,ko,ru}.md`）以及 `docs/GUIDE.linux.md` 里与本次改动相关的
      段落，都要更新到同一版本口径。
- [ ] **`config.example.yaml` 同步**：新增 / 改名的配置项写进模板并带注释。
- [ ] **词表同步**：本次若新增了 `t()` key，四份 `locales/*.py` 都要补齐（`tests/test_i18n.py` 会拦）。
- [ ] **全量离线测试全绿**（本地 + 看 CI 结论；`tests/test_engine.py` 除外）。
- [ ] **手写 Release 说明**：在 release.yml 的 `body:` 里手写「这版有什么」，**不要启用自动生成**。
      写完确认没有 What's Changed / New Contributors 那两节。
- [ ] **产物对账**：Release 附件应为 **exe** + **`SHA256SUMS.txt`** + **`…-x86_64.AppImage`** 三样；
      缺 AppImage 会红灯（`needs: appimage` 是故意的 —— 宁可整体红，也不发一个「只有 exe」的版本）。
- [ ] **R2 发布与复核（CI 自动，但要看结论）**：`publish-r2` job 把四个对象**覆盖式**传到
      `https://dl.kcm-nixi.cn`（`latest.json` / exe / AppImage / `SHA256SUMS.txt`，**全部带
      `Cache-Control: no-cache`** —— 默认的 `max-age=14400` 会让同键覆盖后 CDN 继续回旧内容，实测过），
      并**走公开域名把两份产物整份下回来逐字节复核**。它是 `release` 的前置：这一步红就不建 Release
      （客户端 v0.12.0 起主要从 R2 收更新，「发了版但用户收不到」必须整体红）。
      凭据是仓库 secrets `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`；本机补发 / 事后体检：
      `python scripts/publish_r2.py publish --dist dist --tag vX.Y.Z --env-file <KEY=VALUE 文件>`
      （只核现状用 `... verify --tag vX.Y.Z`）。
      ⚠️ 固定键 ⇒ 主源上**没有历史版本可回滚**：要退版只能去 GitHub tag 页拿附件。
- [ ] **R2 的信任边界（评审 #103 §1，已决策：暂不做清单签名）**：`dl.kcm-nixi.cn` 是更新链路的
      **信任锚**，清单自带的 sha256 只能证明「字节没坏」，证明不了「内容是我们发的」——
      域名 / DNS / CF 账号 / 桶写权限任一失守 = 可下发任意程序（HTTPS 挡不住，攻击者持有有效证书）。
      当前靠运维收敛：域名自动续费 + registrar lock + CF 2FA + CAA + **桶级最小权限 token**。
      待办（下次大版本前评估）：① 把仓库 secret 换成只读 `vrclt` 的 R2 token（需在 CF 后台建：
      R2 → Manage R2 API Tokens → Object Read & Write + 指定桶）；② 清单签名（Ed25519，公钥编译进
      客户端）—— 做了之后 CDN 才退化成哑管道。应急开关：`VLT_UPDATE_SOURCE=github` 强制跳过主源。
- [ ] **下载复核**：用 `scripts/verify_release.py <tag> "<本次新增的关键字符串>"` 把附件拉下来对账
      （SHA256、真跑 `--self-test`、版本行、字节码里搜新功能字符串、图标像素比对），
      并**顺带对账 R2**：清单版本 == 本次 tag、清单里每个产物的 sha256 == GitHub 的 digest、
      公开域上那个对象的字节数 == 附件 size（Range 探针，不整下）。
- [ ] **更新链路体检（客户端视角）**：`scripts/verify/verify_update_source.py --local-version <上一版>`
      —— 走真实入口（检查 → 主源清单 → 下载 → sha256 → pending 落盘）跑一遍，顺带报告兜底源是否可用。

---

## English (short)

This project is Chinese-first; the detailed docs are in Chinese. If you'd like to contribute but
don't read Chinese, this is the short version:

1. **Run it** — see [docs/GUIDE.md](docs/GUIDE.md) (Windows) or
   [docs/GUIDE.linux.md](docs/GUIDE.linux.md) (Linux). Always use the repo's `.venv`
   (`./.venv/Scripts/python.exe` on Windows), never a bare `python`.
2. **Test** — there is no pytest. Run each `tests/test_*.py` file directly with that interpreter;
   every test must run offline. The only exception is `tests/test_engine.py` (it needs a real API key
   and is skipped in CI). CI runs on an English-locale system with a 1920x1080 virtual screen, so don't rely on
   a Chinese UI or a wide window in your assertions.
3. **Commits** — Conventional Commit prefix + a Chinese description, e.g.
   `feat(scope): …`, `fix(scope): …`.
4. **PRs** — your full local test suite must be green *before* review. Fill in the PR template, and
   always paste the **command + raw output** you used to verify, plus which machine / hardware you
   tested on. Say so honestly when something was *not* verified.
5. **Translations** — edit `vlt/locales/<code>.py`, a plain "Chinese source → your language" table.
   No code changes needed; `tests/test_i18n.py` enforces key coverage.
6. **Never** commit API keys or personal data, and never change the hard constraints recorded in
   [docs/平台约束记录.md](docs/平台约束记录.md) without real-world evidence.
