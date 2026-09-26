# VRChat 实时同传

[![CI](https://github.com/nixi-agent/vrchat-livetranslate/actions/workflows/ci.yml/badge.svg)](https://github.com/nixi-agent/vrchat-livetranslate/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nixi-agent/vrchat-livetranslate?label=release)](https://github.com/nixi-agent/vrchat-livetranslate/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)](#一前置条件)
[![Python](https://img.shields.io/badge/python-3.11-blue)](#一前置条件)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

在 VRChat 里做**实时同声传译**：采集麦克风 / 游戏音频 → 阿里云百炼实时同传模型 →
译文送到 **chatbox 气泡**、**VR 手腕屏**，可选把译音回灌进虚拟麦克风**让对方直接听见**。

![界面](assets/gui.png)

---

> 🧭 **这份文档有两种读者**
>
> - **直接下 exe 的**：不需要 Python、不需要命令行。凡是出现 `.venv\Scripts\python.exe`、
>   `run_*.bat`、`--xxx` 的地方都是**源码安装专用**，跳过即可——你要的功能界面上都有。
> - **从源码跑的**：下面全部适用。

## 它能做什么

| 功能 | 状态 | 说明 |
|---|---|---|
| ① 我说 → **chatbox 气泡** | ✅ 已实现，默认开启 | 麦克风 → 端到端语音翻译 → 首个增量立即发、之后每 2 秒刷一次快照、句末必发最终版 |
| ② 别人说 → **VR 手腕屏** | ✅ 已实现 | 采集 VRChat 播放输出（WASAPI loopback）→ 译成中文 → 渲染到 SteamVR overlay，**有更新就立刻上屏** |
| ③ 我说 → **译音进对方耳朵** | ✅ 已实现，**默认关闭** | 模型直出译音 → 重采样 48kHz → 写进虚拟声卡 → VRChat 麦克风拾取。需自备虚拟声卡（VoiceMeeter / VB-Cable 等）；**尚无真机出声验证** |

---

## 零、最快上手：下载现成的 exe

到 **[Releases](https://github.com/nixi-agent/vrchat-livetranslate/releases/latest)** 下载
`VRChatLiveTranslate.exe`（**单文件、免安装、无控制台窗口**），双击即用。

- 仍然要自备一个**阿里云百炼 API key**：界面里的「⚙ 设置」粘贴保存
- 配置与日志写在 `%APPDATA%\vrchat-livetranslate`（exe 放在只读目录也能跑）
- 想做成绿色版（配置跟着 exe 走）→ 在 exe 旁边放一个**空的 `portable.txt`**
- 旧版把 `config.yaml` / `logs/` 放在 exe 旁边的话，首次运行会**自动迁移**到新位置，原来的文件不删

想看源码 / 自己打包 / 改代码 → 从下面「一」开始。

---

## 一、前置条件

1. **Windows 10 / 11**（用到 WASAPI 与 SteamVR）
2. **Python 3.11**（3.12 未测；安装时务必勾选 *Add python.exe to PATH*）—— *只用 exe 的话这条不用管*
   <https://www.python.org/downloads/release/python-3119/>
3. **VRChat**：设置里打开 OSC（`OSC enabled: True`），chat bubble visibility 设为 **Everyone**
4. **SteamVR** —— 只有「别人说 → 手腕屏」这个功能需要
5. **阿里云百炼 API key**（个人实名认证即可）

## 二、安装（源码）

双击 **`setup.bat`**（自动建 `.venv` + 装依赖，约 1–2 分钟）。

手动等价命令：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 三、配置 API key

> 🔑 还没有百炼账号？**[点此开通「阿里云百炼大模型」▸](https://www.aliyun.com/minisite/goods?userCode=q8nma978)**

按下面的顺序找一个用，**前面找到就不看后面**：

| 优先级 | 来源 | 怎么设 |
|---|---|---|
| 1 | 界面里保存的 key | 图形界面「⚙ 设置 → API key」粘贴保存（存在 `%USERPROFILE%\.vrchat-livetranslate\api_key.txt`） |
| 2 | 环境变量 | `setx DASHSCOPE_API_KEY "sk-你的key"`（**重开一个命令行窗口才生效**） |
| 3 | 百炼 CLI 的配置 | `bl auth login --api-key "sk-你的key"`（需要 `npm install -g bailian-cli`），读 `%USERPROFILE%\.bailian\config.json` |

**key 只从上面这三处读，不写进项目文件、不写进 `config.yaml`。**

> 🔒 **防误提交**：仓库带凭据扫描。跑一次 `install_secret_guard.bat` 装上 pre-commit 钩子后，
> 任何含 `sk-...` / `gho_...` / `AKIA...` / `Bearer ...` / 硬编码密钥的提交会被直接拦下
> （脚本：`scripts/check_no_secrets.py`）。
> 手动全量检查：`python scripts/check_no_secrets.py --once`
> 为什么需要：key 一旦进了 git 历史，删掉文件也清不掉，只能 rewrite 历史 —— 宁可在提交前拦。

## 四、自检

**用 exe 的**：双击打开界面，确认三件事——

1. 「⚙ 设置」里三个设备下拉不是空的（`麦克风` / `VRChat 音频` / `译音输出`）
2. API key 状态位显示**已配置**（否则点它会跳到开通页）
3. 勾上 `chatbox`，点「开始翻译」，对着麦克风说一句话 → 气泡里出现译文

**源码安装的**：双击 **`run_selfcheck.bat`**。它按顺序做四件事，不需要麦克风、不需要 VRChat 也能跑完前 3 步：

1. 模块导入检查（engine / session / overlay / chatbox / merger）
2. 读一次 API key 并打码打印（确认顺序对）
3. 离线渲染一帧手腕屏贴图（不接管 SteamVR）
4. 用自带测试音频跑一遍完整链路（中文 → 英文，打进 chatbox）

VRChat 开着的话，气泡里应该出现：

```
Hello, I'm Nixi. Today, we're going to test out the real-time simultaneous interpretation feature in VRChat.
```

**先自检再报错。**

---

## 五、使用

### 图形界面（推荐）

**双击 exe**，或源码安装下双击 **`run_gui.bat`**——都是同一个界面（Tkinter，标准库，零额外依赖、秒开）。

| 区域 | 内容 |
|---|---|
| 第一行 | `开始翻译` / `停止翻译`、方向单选（`我说` / `别人说` / `双向同时`）、语言对下拉（源 → 目标）、右侧 `☕ 赞助` 与 `⚙ 设置` |
| 第二行 | `输出:` 勾选 `chatbox` / `手腕屏` / `译音输出`，以及 `微调 ▸` 按钮；最右侧是 API key 状态位（已配置时是纯文本 `API key 已配置`，**未配置时变成可点的「⚠ 未配置 API key · 点此开通百炼 ▸」**） |
| 聊天区 | 右侧蓝色气泡 = 我说的，左侧灰色气泡 = 别人说的；每格两行，**上行原文小字、下行译文大字**；可滚动回看（上限 500 条） |
| 状态栏 | 左侧彩色圆点 + 最新一条状态；右侧统计（`运行中` / `已翻译 N 条` / `首增量 Xms`），两者互不覆盖 |
| `☕ 赞助` 弹窗 | Ko-fi 跳转按钮 + 微信 / 支付宝收款码（等比缩放到 240px，可直接扫） |

- **流式上屏**：未 final 的增量**就地重画同一条气泡**，不会每来一个增量就新增一条
- **双向同时**：同一个窗口跑两个翻译方向（麦克风 → 右侧、游戏音频 → 左侧），第二个**错开 300ms 启动**避免抢音频设备；任一侧失败，另一侧照常工作
- **语言镜像**：一个语言对双向对应——选「中文 → 英语」，别人说方向自动变「英语 → 中文」。
  源语言可选 `自动检测` / 中文 / 英语 / 日语 / 韩语 / 法语 / 德语 / 西班牙语 / 俄语（目标语言是同一张表但没有「自动检测」）；
  源语言选 `自动检测` 时对方方向的目标回落中文并在状态栏说明。改动**立即生效**并写回配置，下次启动保留
- **设置弹窗**（`⚙ 设置`）：API key 输入 / 清除、三个音频设备下拉 + `刷新`、日志区（`导出日志压缩包…`）
- **微调面板**（`微调 ▸`）：手腕屏的锚点 + 12 个滑块，**拖动即热重载、不用重启**（详见「六、配置」）

#### 自动化验收（无头，不开窗口）

```bat
:: 源码安装
.venv\Scripts\python.exe -m vlt.gui --self-test
.venv\Scripts\python.exe -m vlt.gui --self-test-dual

:: exe（没有控制台窗口，结果写在日志里；打包脚本就是这么自检的）
VRChatLiveTranslate.exe --self-test
```

成功打印 `GUI_SELFTEST_OK` / `GUI_SELFTEST_DUAL_OK mine=N theirs=N`，失败打印 `..._FAIL: ...` 到 stderr 并以非 0 退出。需要真实 API key。

> 🪵 **崩溃日志**：界面每次启动都会在 `logs/` 下写一个 `gui_<时间戳>.log`，
> 里面含**版本信息**（git HEAD）、Python 环境、以及所有 stdout/stderr（**每行行首带 `HH:MM:SS.mmm` 时间戳**）。
> 已接入四类捕获：原生崩溃（`faulthandler`）、主线程异常、子线程异常、Tk 回调异常。
> 闪退时**把这个文件发给维护者就能定位**——没有它的话窗口一关什么都不剩。
> 单文件写满 2MB 自动切段，日志总量超过 5MB 自动删最旧的；「设置」里可以一键导出成一个**脱敏**压缩包。

### 命令行（仅源码安装）

exe 是无控制台窗口的单文件 GUI，除了上面的 `--self-test` 之外没有命令行用法；命令行只对源码安装有意义。

```bat
:: 我说 → chatbox（真发到 VRChat）
.venv\Scripts\python.exe -m vlt.app --direction mine --mic --sink chatbox

:: 别人说 → 手腕屏（需要 SteamVR 在跑）
.venv\Scripts\python.exe -m vlt.app --direction theirs --loopback --sink overlay

:: 不开 VRChat 也能验链路：用自带测试音频跑一遍
.venv\Scripts\python.exe -m vlt.app --direction mine --pcm testdata\zh_test_16k.pcm --dry-run
```

两个方向要同时翻译 → **开两个命令行窗口**（或用图形界面的「双向同时」，那是一个窗口跑两条会话）。
两个方向各自一条 WebSocket 会话，互不干扰（限流 RPM 10，够用）。

嫌命令长就用现成的 bat：**`run_chatbox.bat`**（我说 → 气泡，会先列一遍设备再启动）、
**`run_overlay.bat`**（别人说 → 手腕屏）。

#### 全部参数

| 参数 | 作用 |
|---|---|
| `--direction mine/theirs` | 用 `config.yaml` 里哪个方向 |
| `--mic` / `--loopback` / `--pcm <file>` | 音源：麦克风 / VRChat 播放输出 / 离线 PCM（三选一，优先级 `--pcm` > `--mic` > `--loopback`） |
| `--sink chatbox/overlay/both` | 输出去向（默认 `chatbox`） |
| `--list-devices` | 列出音频输入/输出设备后退出 |
| `--dry-run` | 不真发 OSC，只打印与落报文 |
| `--overlay-dry-run` | overlay 不接管 SteamVR，把每帧渲染成 PNG 存 `out/overlay_frames/` |
| `--audio-out` | 开启译音输出（覆盖 `config.yaml` 的 `output.audio.enabled`） |
| `--audio-device <名称...>` | 虚拟声卡设备名回退链（覆盖 `config.yaml` 里的默认值） |
| `--no-realtime` | 尽快灌入 PCM（不做实时节流；只对 `--pcm` 有效） |
| `--settle-s <秒>` | 音频推完后等待响应的秒数（默认 8） |
| `--config <path>` | 用别的配置文件 |

> 麦克风采集是**连续运行**的：跑到 `Ctrl+C` 为止（没有时长参数）。
>
> 📌 **选音频设备不用传参数**：在图形界面「⚙ 设置」里选（下拉里第一项「自动检测」= 走回退链），
> 或直接改 `config.yaml` 的 `capture.mic_device` / `capture.loopback_device`。
> 存的是**设备名字符串**而不是索引——索引会随插拔/会话切换整个变掉。

---

## 六、配置（`config.yaml`）

> 📄 **首次运行会自动从 `config.example.yaml` 生成 `config.yaml`**。
> `config.yaml` 是**你自己的配置**（设备选择、语言偏好等）——它已被 gitignore、**不进版本库**，
> 因为设备名这类东西因机器而异，跟着仓库走只会互相污染。想恢复默认：删掉再启动即可。
>
> 它在哪：
> - **用 exe**：`%APPDATA%\vrchat-livetranslate\config.yaml`（放了 `portable.txt` 的绿色版则在 exe 旁边）
> - **源码安装**：仓库根目录的 `config.yaml`
>
> 🛟 **配置写坏不会让你起不来**：YAML 解析失败时会自动备份成 `config.yaml.broken`、
> 从模板重新生成一份并打印清楚原因，然后继续启动。

界面上改的任何东西（方向、输出勾选、语言、设备、手腕屏微调）都会**就地写回**这个文件——
只改那一行，**注释、空行、键顺序全部保留**（写入前还会先验证一遍生成的 YAML 合法，不合法就拒绝写入）。

### 关键键

```yaml
session:
  model: qwen3.8-livetranslate-flash-realtime   # 也可换 qwen3.5-*（事件名按代次自动分派）
  base_url: wss://dashscope.aliyuncs.com/api-ws/v1/realtime
  voice: Tina                 # ⚠️ 必须显式指定，不填会报 Voice 'Chelsie' is not supported
  turn_detection: null        # 留空 = 服务端默认
  final_silence_s: 3.0        # ⚠️ 必须 > 服务端增量间隔（实测最大 2.3s），调小会让最终版在句子中间抢跑
  max_new_sessions_per_minute: 4   # RPM 10 预算：每次 WS 连接算一次请求
  reconnect_backoff: [2, 5, 10, 30]

capture:                      # 空字符串 = 自动检测
  mic_device: ""
  loopback_device: ""

directions:
  mine:   { source_lang: zh,   target_lang: en, output_audio: false }   # 我说 → 气泡
  theirs: { source_lang: null, target_lang: zh, output_audio: false }   # 别人说 → 手腕屏（null = 自动识别）

chatbox:
  interval_s: 2.0             # 增量刷新节奏（漏桶 5 条/5 秒 → 2 秒一条余量充足）
  max_chars: 144              # 官方上限（字符，不是字节）

merger:
  interval_s: 2.0             # 首 delta 立即发，之后每 2 秒一次快照，句末必刷最终版
  carry_over: false           # true = 把上一段最终译文作为前缀保留（连续字幕观感）

overlay:
  interval_s: 0               # 0 = 有更新立刻上屏（本地显示不受 chatbox 限流约束）
  anchor: right_hand          # left_hand | right_hand | tracker | hmd
  size_px: [1024, 320]
  font_size: 42               # 译文字号
  source_font_size: 30        # 原文小字号
  show_source: true           # 双行显示（3.8 默认就返回源文识别结果，零额外成本）

output:
  audio:
    enabled: false            # 译音总开关：与 directions.<X>.output_audio 是「与」关系
    device_name: ""           # 手选的虚拟声卡名（非空时优先于回退链）
```

### 手腕屏微调面板（12 个滑块，拖动即生效）

| 滑块 | 范围 / 步长 | 滑块 | 范围 / 步长 |
|---|---|---|---|
| 位置 X / Y / Z | −0.30 ~ 0.30 m，0.005 | 大小 | 0.05 ~ 0.80 m，0.01 |
| 俯仰 / 偏航 / 翻滚 | −90 ~ 90°，1 | 弯曲 | 0.0 ~ 0.50，0.01 |
| 透明度 | 0.10 ~ 1.00，0.05 | 译文字号 | 20 ~ 64，1 |
| 原文字号 | 14 ~ 48，1 | 面板高 | 240 ~ 560 px，10 |

外加**锚点**下拉（右手 / 左手 / 前臂 tracker / 头显前固定）与 **tracker 序号**（0–3）。
改动走 200ms 防抖落盘；其中**译文字号 / 原文字号 / 面板高**会触发贴图重渲一帧，其余只重应用变换。

> 实测参考：42/30 字号 + 320px 面板放得下 **2 轮**对话；36/24 + 420px 放得下 **3 轮共 6 行**。

---

## 七、排障

| 现象 | 原因 / 处理 |
|---|---|
| 气泡里没东西 | VRChat 没开 / OSC 没开 / chat bubble visibility 是 Off。`--dry-run` 能看到发送日志说明程序没问题 |
| `[loopback] ❌ 没找到任何 loopback 设备` | VRChat 没在放声音；或**在远程桌面会话里跑**（WASAPI 端点按会话隔离，必须在物理机当前会话） |
| 麦克风采不到声音 | 同上；先在「⚙ 设置」里确认设备下拉里选的是哪个（枚举为空时状态栏会提示「远程会话下枚举为空是正常的」） |
| `Voice 'Chelsie' is not supported` | `session.voice` 没填。保持 `Tina` |
| `Invalid translation parameter` | `session.update` 缺 `translation` 字段（代码里已保证，改代码时注意） |
| `1007 Requests rate limit exceeded` | 撞了 RPM 10。等 1 分钟；别频繁重启（**每次 WS 连接都算一次请求**） |
| 译文停在半句话上 | 静默兜底阈值被调小了。`session.final_silence_s` 必须 > 2.3s，默认 3.0 |
| `[overlay] ⚠️ SteamVR 未运行或不可用` | 正常降级：只有手腕屏不显示，chatbox 不受影响 |
| 手腕屏看不见 | 先确认 SteamVR 在跑；再调「微调」里的位置 / 大小；`--overlay-dry-run` 能出 PNG 说明渲染没问题 |
| 手腕屏过一阵子消失 | 已带两级自愈（连续 3 次失败重建 overlay → 再 3 次失败硬重启 openvr 连接 → 之后每 50 次重试一次）。日志每 30s 有一条 `[overlay][diag] 心跳：…` 可以看「最后成功上传多久前 / 重建次数」 |
| 译音输出没声音 | ①两个开关都要开（输出勾选 + 方向级）；②只对「我说的话」方向有效；③虚拟声卡装了吗、状态栏/日志会明说原因；④**其余功能不受影响** |
| 界面「开始翻译」点了没反应 | 先看终端输出——asyncio 回调里的异常只在控制台打一行 traceback，不进状态栏（用 exe 的话看 `logs/` 里最新的那个 `.log`） |
| 报错看不懂 | **「⚙ 设置 → 导出日志压缩包」**，已自动脱敏，把 zip 发给维护者即可 |

---

## 八、项目结构

```
vlt/
├── app.py                CLI 入口（音频源 → 会话 → 节流 → 输出）
├── engine.py             可编程引擎：会话 + 看门狗重连 + 节流 + chatbox/overlay/译音的启停
├── gui.py                Tkinter 图形界面（含 --self-test 无头验收）
├── config.py             配置加载；凭据解析顺序；写坏自愈
├── credentials.py        API key 的保存 / 清除 / 打码
├── devices.py            音频设备枚举与「按名字解析回索引」
├── paths.py              可写目录决策（源码 / exe / 绿色版）+ 旧文件迁移
├── crashlog.py           崩溃捕获 + 日志切段/清理 + 脱敏导出
├── session/
│   ├── base.py           TextDelta / SessionConfig / create_session（按模型代次分派）
│   └── qwen38.py         3.8 与 3.5 的事件分派 + 连接预算 + 静默兜底 + 事件埋点
└── output/
    ├── merger.py         首 delta 立即发 → 2s 快照 → 句末 flush
    ├── chatbox.py        OSC ,sTT + 令牌桶 + 最终版补发队列
    ├── overlay.py        SteamVR 手腕屏：渲染 + 锚点 + 热重载 + 两级自愈
    └── virtualmic.py     译音回灌：24k→48k 重采样 + 抖动缓冲（整句丢弃，绝不切句）

scripts/                  探针与调试工具（probe_* / osc_listen / verify_release）
tests/                    17 个文件、95 个测试函数（离线可跑，CI 逐文件执行）
docs/                     P0.5 / P1 / P2 三份实测结果（协议、延迟、手腕屏）
testdata/                 自带测试音频（中文 8.56s、英文 7.92s，16kHz 单声道 PCM）
assets/                   图标、界面截图与赞助收款码
```

---

## 九、开发

### 跑测试

**不用 pytest**（依赖里没有），每个测试文件都是可独立执行的脚本，与 CI 完全同款：

```bat
:: 全部（逐文件跑，与 CI 完全同款；在 cmd 里直接粘，写进 .bat 文件时把 %t 改成 %%t）
for %t in (tests\test_*.py) do @.venv\Scripts\python.exe %t

:: 单个
.venv\Scripts\python.exe tests\test_virtualmic.py
```

测试**全部离线可跑**（不需要麦克风 / VRChat / SteamVR / 网络），覆盖设备解析、路径决策、
断线重连、日志轮转与脱敏、赞助弹窗、手腕屏自愈、译音缓冲不变式、配置就地写入不破坏注释等。

唯一例外是 `tests/test_engine.py`：它要打一次真实会话，**需要本机已配置 API key**，
CI 里显式跳过（workflow 里有 `::notice::` 说明原因，不做静默跳过）。

### 打包

```bat
build_exe.bat                                              :: 打包 + 打完自检
.venv\Scripts\python.exe scripts\build_exe.py --no-verify  :: 只打包
```

产物 `dist\VRChatLiveTranslate.exe`：PyInstaller **单文件、无控制台窗口**，
内置 `config.example.yaml` / `testdata` / `assets`，约 37 MB。
打完后默认会真跑一次 `exe --self-test`，从日志里找 `GUI_SELFTEST_OK` 才算通过。

### CI / 发布

- **CI**（`.github/workflows/ci.yml`，push 到 main / PR / 手动）：
  语法检查 → 凭据扫描 → 逐文件跑全部离线测试 → 再单独验一次**打包链路**（产物存在且 ≥20MB）
- **Release**（`.github/workflows/release.yml`，推 `v*` tag 触发）：
  先对账 tag 与 `vlt/__init__.py` 的 `__version__`（不一致直接失败）→ 打包 →
  算 SHA256 写 `SHA256SUMS.txt` → 建 Release，附件是 **exe + SHA256SUMS.txt**
- **下载后想自己复核**：`scripts/verify_release.py` 会把 Release 附件拉下来对账
  （SHA256、真跑 `--self-test`、版本行、字节码里搜新功能字符串、图标像素比对）：

  ```bat
  .venv\Scripts\python.exe scripts\verify_release.py v0.0.3 "硬重启"
  ```

---

## 十、已知限制

- 译音回灌**尚无真机出声验证**；默认关闭
- 手腕屏需要 **SteamVR 作为活动合成器**；走厂商原生 OpenXR runtime 时 PC 侧第三方 overlay 不显示
- 输入端**只能拿到游戏混音后的一路立体声**：拿不到逐说话人通道，混音里多人叠话时说话人归属天然不可靠
- WebSocket 链路**无回声消除与降噪** → **戴耳机是硬要求**（外放会把别人的声音采进来，同一句识别两遍）
- 同时翻译两个方向 = 两条 WebSocket 会话，预算按两个方向叠算

---

## ☕ 赞助

**请给我报销 token** 🙏

- ☕ **Ko-fi**（海外 / 信用卡 / PayPal）：

  [![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kcmnixi)

- 国内：微信 / 支付宝扫码

![收款码](assets/sponsor-qrcodes.png)

- 🔑 还没开通百炼？**[点此开通「阿里云百炼大模型」▸](https://www.aliyun.com/minisite/goods?userCode=q8nma978)**

> 图形界面顶栏也有「☕ 赞助」按钮，点开就是上面的入口。

---

## 许可

[MIT License](LICENSE) © 2026 Nixi
