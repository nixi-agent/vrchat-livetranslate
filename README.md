# VRChat 实时同传

在 VRChat 里做实时同声传译。当前实现两条腿：

| 腿 | 状态 | 说明 |
|---|---|---|
| ① 我说 → **chatbox 气泡** | ✅ 可用 | 麦克风 → 端到端语音翻译 → 每 2 秒刷新译文，句末刷最终版 |
| ② 别人说 → **VR 手腕屏** | ✅ 可用（待真机目视） | 采集 VRChat 播放输出 → 中文渲染到 SteamVR overlay，**有更新就立刻上屏** |
| ③ 我说 → **译音进对方耳朵** | ⬜ 未实现 | 需装 VoiceMeeter（方案已定：模型直出译音 → 虚拟声卡） |

---

## 零、下载现成的 exe（不想折腾环境就用这个）

到 **[Releases](https://github.com/nixi-agent/vrchat-livetranslate/releases/latest)** 下载
`VRChatLiveTranslate.exe`（单文件、免安装、无控制台窗口），**双击即用**。

- 仍然要自备一个**阿里云百炼 API key**：界面「⚙ 设置」里粘贴保存，或设环境变量 `DASHSCOPE_API_KEY`
- 配置与日志写在 `%APPDATA%\vrchat-livetranslate`（exe 放在只读目录也能跑）；
  想做成绿色版（配置跟着 exe 走）→ 在 exe 旁边放一个空的 `portable.txt`
- 想看源码 / 自己打包 / 改代码 → 从下面的「一」开始

---

## 一、前置条件

1. **Windows 10/11**（用到 WASAPI 与 SteamVR）
2. **Python 3.11**（3.12 未测；务必勾选 Add to PATH）
   https://www.python.org/downloads/release/python-3119/
3. **VRChat**：设置里打开 OSC（`OSC enabled: True`），chat bubble visibility 设为 Everyone
4. **SteamVR**（只有第②条腿需要）
5. **阿里云百炼 API key**（必须，个人实名认证即可）

## 二、安装

双击 **`setup.bat`**（会自动建 venv + 装依赖，约 1–2 分钟）。

手动等价命令：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 三、配置 API key（二选一）

```bat
:: 方式A：环境变量（推荐，重开一个命令行窗口生效）
setx DASHSCOPE_API_KEY "sk-你的key"

:: 方式B：用百炼 CLI 存起来（需要 npm install -g bailian-cli）
bl auth login --api-key "sk-你的key"
```

app 读取顺序：`DASHSCOPE_API_KEY` 环境变量 → `%USERPROFILE%\.bailian\config.json`。
**key 只从这两处读，不写进项目文件。**

> 🔒 **防误提交**：仓库带凭据扫描。跑一次 `install_secret_guard.bat` 装上 pre-commit 钩子后，
> 任何含 `sk-...` / `gho_...` / 硬编码密钥的提交会被直接拦下（脚本：`scripts/check_no_secrets.py`）。
> 手动全量检查：`python scripts/check_no_secrets.py --once`
> 为什么需要：key 一旦进了 git 历史，删掉文件也清不掉，必须 rewrite 历史 —— 宁可在提交前拦。

## 四、自检（推荐先跑这个）

双击 **`run_selfcheck.bat`**。它会依次检查：模块齐全 → key 能读到 → 能渲染手腕屏贴图 →
用自带测试音频跑一遍完整链路（中文→英文，打进 chatbox）。

VRChat 开着的话，气泡里应该出现：
`Hello, I'm Nixi. Today, we're going to test out the real-time simultaneous interpretation feature in VRChat.`

## 五、正式使用

### ① 我说 → chatbox
双击 **`run_chatbox.bat`**（会先列音频设备，确认麦克风没问题再开始）。
或手动：

```bat
.venv\Scripts\python.exe -m vlt.app --direction mine --mic --sink chatbox
```

### ② 别人说 → 手腕屏（需要 SteamVR 在跑）
双击 **`run_overlay.bat`**。或手动：

```bat
.venv\Scripts\python.exe -m vlt.app --direction theirs --loopback --sink overlay
```

### 两条腿同时用
**开两个命令行窗口**，一个跑 chatbox、一个跑 overlay（各自一条 WS 会话，互不干扰）。
> 单进程双会话模式还没做；两条腿一起跑时各占一次 WS 连接（限流 RPM 10，够用）。

### 常用参数

| 参数 | 作用 |
|---|---|
| `--direction mine/theirs` | 用 config.yaml 里哪个方向 |
| `--mic` / `--loopback` / `--pcm <file>` | 音源：麦克风 / VRChat 播放输出 / 离线 PCM |
| `--sink chatbox/overlay/both` | 输出去向 |
| `--list-devices` | 列出音频设备（选麦克风时用） |
| `--mic-device realtek` | 按名称子串选麦克风 |
| `--dry-run` | 不真发 OSC，只打印 |
| `--overlay-dry-run` | 不接管 SteamVR，每帧渲染成 PNG 存 `out/overlay_frames/` |
| `--log-events` | 把服务端事件序列写进埋点（排查用） |
| `--seconds 30` | 只跑 30 秒（默认 0 = 一直跑到 Ctrl+C） |

## 六、图形界面

双击 **`run_gui.bat`** 启动图形界面（Tkinter，零额外依赖，秒开）。

整体为**深色主题（深灰 + 蓝）**：聊天区、顶栏、状态栏、下拉框、滚动条与 Windows 标题栏统一暗色，主按钮蓝色强调，状态栏用彩色圆点表示连接状态。

界面功能：
- **聊天气泡视图**：右侧蓝色气泡 = 我说，左侧灰色气泡 = 别人说；长句自动换行，可滚动回看（上限 500 条）
- **每格两行**：上行为**原文（9pt 小字灰）**、下行为**译文（11pt 大字亮色）**——你要读的永远是下面那行大字。
  服务端没返回原文、或原文与译文相同时只显示译文，不留空行
- **一键开关**：开始翻译 / 停止翻译
- **方向切换**：「我说」/「别人说」/「双向同时」（同一窗口跑两个引擎：麦克风→右侧 + VRChat 播放输出→左侧，错开 300ms 启动避免抢音频设备；任一侧失败另一侧照常工作）
- **语言镜像**：一个语言对双向对应——选「中文→英语」，别人说方向自动变「英语→中文」；源语言（自动检测 / 中 / 英 / 日 / 韩 / 法 / 德 / 西 / 俄）+ 目标语言
- **输出选择**：chatbox / 手腕屏，可勾选
- **☕ 赞助**：顶栏按钮，弹出 Ko-fi 跳转按钮 + 微信 / 支付宝收款码（等比展示，可直接扫码）
- **状态栏**：左侧显示最新一条状态消息，右侧显示统计（运行中 / 已翻译 N 条 / 首增量 Xms），两者互不覆盖

流式翻译时**就地重画同一条气泡**（未 final 时只刷新那一格），不会每来一个增量就新增一条。

语言改动立即生效并写回 `config.yaml`（两个方向互为镜像），下次启动保留。

> 🪵 **崩溃日志**：界面每次启动都会在 `logs/` 下写一个 `gui_<时间戳>.log`，
> 里面含**版本信息**（git commit）、Python 环境、以及所有输出。
> 万一闪退，**把这个文件发给开发者就能定位**——没有它的话窗口一关什么都不剩。
> 已接入三类崩溃的捕获：原生崩溃（段错误，`faulthandler`）、未捕获异常（主线程/子线程）、
> Tk 回调异常（Tk 默认只打印不退出，容易静默带病运行）。

自动化验收：

```bat
.venv\Scripts\python.exe -m vlt.gui --self-test
.venv\Scripts\python.exe -m vlt.gui --self-test-dual
```

## 七、改配置（`config.yaml`，改完**不用重启**）

> 📄 **首次运行会自动从 `config.example.yaml` 生成 `config.yaml`**。
> `config.yaml` 是**你自己的配置**（设备选择、语言偏好等）——它已被 gitignore、**不进版本库**，
> 因为设备名这类东西因机器而异，跟着仓库走只会互相污染。
> 想恢复默认：删掉 `config.yaml` 再启动即可（会重新生成一份）。

```yaml
session:
  model: qwen3.8-livetranslate-flash-realtime   # 可换 qwen3.5-*（字段/事件会自动切换）
  voice: Tina                                   # ⚠️ 必须显式指定，不填会报错
  final_silence_s: 3.0                          # 服务端不发 done 时的兜底终结阈值

directions:
  mine:   { source_lang: zh, target_lang: en, output_audio: false }   # 我说→气泡
  theirs: { source_lang: null, target_lang: zh, output_audio: false } # 别人说→手腕屏

chatbox:
  interval_s: 2.0        # 气泡刷新节奏（漏桶 5 条/5 秒，2 秒余量充足）

overlay:
  interval_s: 0          # 0 = 有更新立刻上屏（手腕屏不受 chatbox 限流约束）
  anchor: right_hand     # left_hand | right_hand | tracker | hmd
  offset:
    pos: [0.0, 0.06, 0.02]   # 位置（米）；改完存盘即生效
    width_m: 0.24            # 面板宽
  source_alpha: 205      # 原文亮度（150 会显灰像另一个颜色）
```

## 八、排障

| 现象 | 原因 / 处理 |
|---|---|
| 气泡里没东西 | VRChat 没开 / OSC 没开 / chat bubble visibility 是 Off。`--dry-run` 能看到发送日志说明程序没问题 |
| `[loopback] 没找到任何 loopback 设备` | VRChat 没在放声音；或在**远程桌面会话**里跑（WASAPI 端点按会话隔离，必须在物理机当前会话） |
| 麦克风采不到声音 | 同上；`--list-devices` 确认设备；`--mic-device` 指定 |
| `Voice 'Chelsie' is not supported` | `session.voice` 没填。保持 `Tina` |
| `Invalid translation parameter` | `session.update` 缺 `translation` 字段（代码里已保证，改代码时注意） |
| `1007 Requests rate limit exceeded` | 撞了 RPM 10。等 1 分钟；别频繁重启（**每次 WS 连接都算一次请求**） |
| `[overlay] SteamVR 未运行或不可用` | 正常降级：只有手腕屏不显示，chatbox 不受影响 |
| 手腕屏看不见 | 先确认 SteamVR 在跑；再调 `offset.pos` / `width_m`；`--overlay-dry-run` 能出 PNG 说明渲染没问题 |

## 九、项目结构

```
vlt/
├── app.py                主程序：CLI 入口，委托给 Engine
├── engine.py             可编程引擎：会话 + 节流 + chatbox + overlay 的启停
├── gui.py                Tkinter 图形界面
├── config.py             配置加载；API key 只从环境变量或 bl CLI 配置读
├── session/
│   ├── base.py           TextDelta / SessionConfig / LiveTranslateSession / create_session
│   └── qwen38.py         3.8/3.5 事件按代次分派 + 连接预算 + 静默兜底
└── output/
    ├── merger.py         首 delta 立即发 → 2s 快照 → 句末 flush（只管 chatbox）
    ├── chatbox.py        OSC ,sTT + 令牌桶 + 最终版补发队列
    └── overlay.py        手腕屏渲染 + SteamVR overlay + 配置热重载
scripts/                  探针与调试工具（probe_*、osc_listen）
tests/                    渲染回归测试 + 引擎无头测试
docs/                     P0.5 / P1 / P2 实测结果
testdata/                 自带测试音频（中文 8.56s、英文 7.92s，16kHz PCM）
```

## 十、设计要点（踩过的坑，别踩第二遍）

1. **事件名按模型代次分派**：3.8 纯文本用 `response.text.delta`，文本+音频用 `response.audio_transcript.delta`；
   3.5 用 `response.text.text`（含 `stash` 预测文本）。接错代 → 文本路静默为空。
2. **`session.update` 必须整体下发且必带 `translation`**，服务端不做字段合并。
3. **服务端在持续音频下不发 `done`** → 靠静默兜底（阈值必须大于增量间隔，实测最大 2.15s，取 3.0s）。
4. **RPM 10：每次 WS 连接算一次请求**，重连要退避。
5. **chatbox 是漏桶 5 条/5 秒**，且**最终版不可丢**（被拒要入队补发）；手腕屏不受此限制。
6. **WASAPI 端点按 Windows 会话隔离**，采集必须在物理控制台会话。
7. **Steam Link 场景下游戏音频走 `Steam Streaming Speakers`**，不是扬声器；设备选择用名称回退链，别写死 ID。

## ☕ 赞助

**请给我报销 token** 🙏

- ☕ **Ko-fi**（海外 / 信用卡 / PayPal）：

  [![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S7Y527MI45)
- 国内：微信 / 支付宝扫码

![收款码](assets/sponsor-qrcodes.png)



> 图形界面顶栏也有「☕ 赞助」按钮，点开就是上面的入口。
