# 真机验收脚本（`scripts/verify/`）

这里放**真机 / 真界面**的验收脚本 —— 它们证明的是「功能在真实环境里跑通了」，
而不是单元测试覆盖的纯逻辑。放在版本库里的目的是：**让验收可复现、让评审看得见**。

## 为什么不进 CI

这些脚本**全部不进 CI**，因为它们需要 CI 环境给不了的东西：

- **真实桌面会话**（能开 Tk 窗口、能 BitBlt 抓屏）—— CI runner 是无头虚拟屏；
- **Windows 的 Win32 能力**（色键透明、`WS_EX_TRANSPARENT` 鼠标穿透、`PrintWindow` 抓图）；
- 个别脚本还需要**真麦克风 / 真 VRChat 窗口**或**到线上房间服务的网络**。

CI 只跑 `tests/`（纯离线单元测试）与打包门禁；这里的手动跑法见下。

## 环境前提

- 解释器一律用仓库 venv：Windows 是 `./.venv/Scripts/python.exe`（本机**没有 `python3`**）；
- 所有命令都在**仓库根目录**下执行；
- 脚本须**只写 `out/` 或临时目录沙箱**，**绝不改真实 `config.yaml`**（本目录脚本都遵守）；
- 缺硬件 / 缺平台时脚本会**几秒内明确报错退出**，不 hang、不静默跳过。

## 脚本一览

| 脚本 | 一句话作用 | 需要的环境 | 实测状态 |
|---|---|---|---|
| `check_mic_pump_silence.py` | 用真实代码喂 PCM，证明「距上次上送音频的间隔」在真链路恒 ~0.1s、**不能**当「用户说完」的判据，必须用电平信号 | 无特殊硬件（离线，任意平台） | **本机已实测跑通** |
| `verify_gui_desktop.py` | 真起界面走「勾选桌面字幕 → 出图 → 透明度滑块 → 拖动落盘 → 销毁」，只写沙箱配置 | Windows + 真实桌面会话（GUI） | **本机已实测跑通** |
| `verify_desktop_overlay.py` | 起一个假 VRChat 窗，验字幕窗的贴窗 / 跟随 / 透明度 / 鼠标穿透，并截图自证 | Windows + 真实桌面会话（GUI） | **本机已实测跑通** |
| `verify_overlay_over_desktop.py` | 从**桌面 DC** 抓屏，证明色键区域真的透过去、面板画在上面（用户在 VRChat 里看到的样子） | Windows + **真实可见**桌面会话 | **本机已实测跑通** |
| `verify_room_button.py` | 验房间按钮三态、空房间码拦截并跳设置页、输入框已移入设置，并截主窗/设置页 | Windows + 真实桌面会话（GUI） | **本机已实测跑通** |
| `verify_room_button_live.py` | 真 Tk 窗 + 真 WebSocket：点「连接房间」真连线上服务、按钮自动变「断开连接」，再点真断开 | 真实桌面会话 + **联网到线上房间服务** | **本机已实测跑通**（联网成功走到 online） |
| `verify_room_i18n.py` | 五种语言各起一次窗，逐控件比需求宽 / 实际宽查裁切，并给 en/ru 截设置页 | Windows + 真实桌面会话（GUI） | **本机已实测跑通** |
| `verify_proxy_loopback.py` | 假麦克风喂已知正弦（1kHz）→ 麦克风代理直通 / 译音档 → 从虚拟声卡**录音端**抓回来判主频（原声应 1kHz、译音应 500Hz、切回又 1kHz），证明档位路由真的过线 | Windows + **已装虚拟声卡**（VB-CABLE / VoiceMeeter）；**不需要**麦克风 / 头显 | **本机已实测跑通** |
| `verify_proxy_loopback_linux.py` | 同一件事的 **Linux 版**：假麦克风喂 1kHz → 代理直通 / 译音档 → 从**运行时声明**的虚拟麦（`vlt_mic_source`）抓回来判主频；**顺带验**两条 Linux 硬约束：声明虚拟麦**不抢默认输出**、退出后**节点零残留** | Linux + **PipeWire**（`pw-dump`/`pw-record`/`pw-cat`）；**不需要**虚拟声卡 / 麦克风 / 头显 / 桌面会话 | **本机已实测跑通** |
| `verify_proxy_app_start.py` | **真起界面**（非 headless）证明「启动即起代理」：启动日志出现「虚拟声卡已打开 / 麦克风代理已启动 / 麦克风直通已启动」，并从录音端真抓到假麦克风的 1kHz | Windows + 真实桌面会话 + **已装虚拟声卡** | **本机已实测跑通** |
| `run_suite.sh` | 在指定 worktree（默认本仓库）跑全量**离线**测试并汇总（死代理 + 清 `DASHSCOPE_API_KEY`） | bash + 该 worktree 的 `.venv` | **本机已实测跑通** |

内部助手：`_shot_window.py`（Win32 `PrintWindow` 截图的共用小工具，**不是给人单独跑的入口**，
被 `verify_room_button.py` / `verify_room_i18n.py` 复用）；`_proxy_probe.py`（两份回环探针共用的
假正弦 / 假麦克风源 / FFT 主频判据 —— 判据抄两份必然漂移，而「看着绿、其实没测到」正是这类
探针最要命的失败模式）。

## 跑法

```bash
# 离线、无硬件（任何时候都能跑）
./.venv/Scripts/python.exe scripts/verify/check_mic_pump_silence.py

# 需要真实桌面会话（Windows）
./.venv/Scripts/python.exe scripts/verify/verify_gui_desktop.py
./.venv/Scripts/python.exe scripts/verify/verify_desktop_overlay.py
./.venv/Scripts/python.exe scripts/verify/verify_overlay_over_desktop.py
./.venv/Scripts/python.exe scripts/verify/verify_room_button.py
./.venv/Scripts/python.exe scripts/verify/verify_room_i18n.py

# 需要联网到线上房间服务
./.venv/Scripts/python.exe scripts/verify/verify_room_button_live.py

# 需要虚拟声卡（VB-CABLE / VoiceMeeter）——不需要麦克风、不需要头显，麦克风那路被换成假源
./.venv/Scripts/python.exe scripts/verify/verify_proxy_loopback.py
./.venv/Scripts/python.exe scripts/verify/verify_proxy_app_start.py    # 这个还要真实桌面会话（要开窗）
#   设备名不一样时用 --out-pattern / --in-pattern 指定关键词；--list 看本机设备

# Linux 侧同一件事：不需要自备虚拟声卡（程序运行时自己声明）、不需要麦克风 / 头显 / 桌面会话
./.venv/bin/python scripts/verify/verify_proxy_loopback_linux.py
#   --list 看本机 PipeWire 音频节点（排查「虚拟麦在不在、类对不对」）

# 全量离线测试（可指定别的 worktree：bash scripts/verify/run_suite.sh <路径> [标签]）
bash scripts/verify/run_suite.sh
```

## 是否会写盘

- 配置类脚本一律把配置指到 **`out/` 沙箱或临时目录**，跑完 `git status` 应为空
  （`out/` 已在 `.gitignore` 里）；**没有任何脚本会改真实 `config.yaml`**；
- 截图证据落在 `out/`（如 `overlay_over_desktop.png`、`room_btn_*.png`）；
- `run_suite.sh` 的汇总写到 `out/<标签>_results.txt`，失败用例完整输出写到
  `out/<标签>_fail_<用例>.log`。
- `verify_proxy_loopback.py` / `verify_proxy_app_start.py` **不写任何文件**（只读音频、打印判据），
  配置也只读临时沙箱那份；`verify_proxy_loopback_linux.py` 同样不写文件，但会**短暂**在
  PipeWire 里声明一对节点（就是产品运行时的行为），退出前一定回收并**自检零残留**。

## ChatGPT 提示词防护

防护机制、实测发现及验证边界见 [Findings](../../docs/ChatGPT-prompt-defense.md)。
耗用订阅额度的额外实机测试程式、长对话资料与录音只保留在本机忽略的暂存目录，不随 repo、套件或 CI 发布。
Repo 中的 ChatGPT 政策、字幕分片、GUI 和生命周期测试都是离线回归，不登录 ChatGPT 或建立语音服务连接。
