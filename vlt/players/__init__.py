"""分玩家语音（**实验性**，Linux / Windows 双平台，默认关闭）。

## 这个包要解决什么

VRChat 把所有玩家的语音混成一路输出，所以现有的「别人说话」腿（`theirs`，采
VRChat 的应用流）拿到的是**混音**：既分不出是谁，也没法按人过滤。VRChat 本来
是按玩家分别发语音包的（Photon `Event 1` / USpeak），只是被混在一起播放 —— 这
个包就是在**协议层**把这些包按 actor 拆开，一人一条音轨。

## 安全边界（这份注释是承诺，不是描述）

- **不注入、不改游戏文件、不 `ptrace` attach**（attach 会让目标进程的
  `TracerPid` 变成非 0，那是 EAC 类模块明确会读的字段）。
- 游戏内存的读取**只发生在** `process_vm_readv`，且由一个**用完即退**的提权助手
  执行；主程序全程普通用户，**永远拿不到原始内存**，只拿到几十个 32 字节候选。
  Windows 上没有这条路径（EAC 在句柄创建时就剥掉 `PROCESS_VM_READ`，`SYSTEM` 令牌、
  镜像名伪装、句柄竞速/窃取全试过都不行）：改成**只读裸盘上 pagefile 的那一小段**
  （`pagefile.py` + `key_win.py`），只在开扫那一刻要一次管理员权限，key 是进程级的
  （换世界/换房间不换），所以一次 UAC 之后整个游戏生命周期都不再弹；同样不 attach、
  不注入、不写游戏进程。
- 只读。代码里不存在任何写游戏内存的路径（`tests/test_players_safety.py` 用
  AST 扫描钉住这一条）；Windows 侧也**只** `ReadFile` 物理盘，不写。
- 音频不落盘（`docs/分玩家语音.md` 写明唯一例外是人工确认用的一次性 dump）。

## 为什么默认关闭

这条路要读游戏进程内存，属于 VRChat ToS 的灰区（作者不保证账号安全），而且
一旦 EAC 切到 Linux v2（eBPF LSM + lockdown），读内存会被**直接拒绝**。所以：
默认 `off`，界面上必须先看到风险说明并显式勾选；`vlt/players/guard.py` 会在
运行前做静态闸门判定，v2 迹象命中就拒绝启动并说明原因。

## 目录

| 模块 | 职责 |
|---|---|
| `procfs.py` | `/proc` 只读元数据：进程发现、映射统计、`Dumpable`/`TracerPid`、**游戏 UDP 端点**（方向归属） |
| `guard.py` | EAC 姿态与 v2 闸门（纯函数，可离线测） |
| `capture_linux.py` | `AF_PACKET` + 内核 BPF（`udp port 27002`）+ 以太网/IP/UDP 解析 + pcap + **方向判定** |
| `key_linux.py` | `process_vm_readv` 扫 `byte[32]` 候选（**不读 `/proc/<pid>/mem`**） |
| `gcm.py` | AES-256-GCM 单包解密与**按 flow 分组**的候选验钥 |
| `photon.py` | Photon 命令帧、分片重组、事件参数、USpeak 帧切分（含保底路径）、事件 226 世界 ID |
| `names.py` | 玩家记录：`actor → usr_id → displayName`，含 `output_log` 兜底 |
| `world.py` | 从 `output_log` 读「世界加载完成/正在加载」，给取钥做闸门 |
| `pending.py` | 取钥期间的密文缓冲：密钥晚到也不丢名单和开头语音（回放） |
| `warmup.py` | 预热：没开始翻译也抓包、取钥、只解玩家名（翻译腿借用它） |
| `positions.py` | 位置（Event 12）→ 距离；支持 `order`/`gate` 两种用法 |
| `router.py` | 每玩家一条翻译会话（共享 RPM 预算；空闲优先回收，全忙时按距离让位） |
| `leg.py` | 分玩家腿：抓包→取钥→解码→路由器（GUI 用它） |
| `helper.py` / `priv_helper.py` | 提权助手的两半：客户端会话 / 助手本体（**Linux 独占**） |
| `payload.py` | 把助手拼成自包含脚本，经 stdin 交给 `pkexec python3 -`（AppImage 用） |
| `opus.py` | libopus ctypes：解码到 16 kHz 单声道、帧自校验、wav 落盘（Windows 用游戏自带的 `opus.dll`） |

### Windows 侧新增（与上面逐项对位，接口同形）

| 模块 | 职责 |
|---|---|
| `procfs_win.py` | Toolhelp32 + `GetExtendedUdpTable`：`find_vrchat`（**按 exe 名**，EAC 拒绝模块快照）/`pick_host`/`udp_ports`/`image_path` |
| `dgram.py` | npcap（`wpcap.dll` via ctypes）：抓包 + 以太网/IP/UDP 解析 + pcap + **加速器头自适应剥离**（UU 下行 10 / 上行 12 字节，靠 GCM 认证兜底） |
| `pagefile.py` | NTFS 只读：`\\.\PhysicalDrive0` 裸盘 + boot sector + MFT + `$ATTRIBUTE_LIST`，定位 `pagefile.sys` 的物理段 |
| `gcm_win.py` | CNG（`bcrypt.dll`）AES-256-GCM：`GcmAuth` 批量验钥 + `Aead`（`gcm._aesgcm` 的后端） |
| `key_win.py` | 驱逐脏页（`NtSetSystemInformation` flush + `EmptyWorkingSet`）→ 扫 pagefile 找 `byte[32]` → GCM 认证；`SessionCache` 按 `Identity(pid, created)` 缓存 |
| `win_capture.py` | 把上面几件拼成 `_HelperCapture` 那九个方法（`drain`/`parse`/`acquire_key`…），`leg.py` 直接换腿 |

⚠️ **Windows 必须关掉内存压缩**（`Disable-MMAgent -MemoryCompression` + 重启）：开着时
压缩存储会把脏页回写成 XPRESS 压缩块，明文 key 不在 pagefile 里，实测 9,109 个候选全败。

入口与工具：

| 文件 | 用途 |
|---|---|
| `scripts/probe_player_audio.py` | `env`（零风险）/ `run`（P0-b：取钥 + 解码） |
| `scripts/vlt_priv_helper.py` | 一次性提权助手（开包 socket + 扫候选，然后退出） |
| `requirements-players.txt` | 只给这个功能的额外依赖（`cryptography`，**Linux 用**；Windows 走系统 CNG，零依赖） |

安装（仓库统一用 uv，与 `setup.sh` 同口径）：

```
uv pip install --python .venv/bin/python -r requirements-players.txt
```

Windows 不需要额外依赖：`vlt/players/` 全链路只用 ctypes + stdlib（opus 用游戏自带
的 `opus.dll`，GCM 用系统 `bcrypt.dll`，抓包用已装的 npcap）。
"""


# 安装提示只写一处：`gcm.py` 与探针脚本都引用它，避免两处文案漂移。
INSTALL_HINT = "uv pip install --python .venv/bin/python -r requirements-players.txt"
