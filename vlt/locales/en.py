"""English 词表：key = 中文原文（与 `t("…")` 调用点逐字一致），value = 英文界面文案。

语气约定：深色主题桌面工具，简洁、口语化；按钮用祈使句；标题/按钮用 Title Case。
带 {占位符} 的条目，占位符名必须与调用点 `t("…", name=…)` 一致。
"""
from __future__ import annotations

STRINGS: dict[str, str] = {
    # ---- 语言名（源/目标语言下拉里显示；表本身仍以中文名为 key）----
    "中文": "Chinese",
    "英语": "English",
    "日语": "Japanese",
    "韩语": "Korean",
    "法语": "French",
    "德语": "German",
    "西班牙语": "Spanish",
    "俄语": "Russian",
    "泰语": "Thai",
    "意大利语": "Italian",
    # ---- 主窗口 ----
    "VRChat 实时同传": "vrchat-livetranslate",
    "⚙ 设置": "⚙ Settings",
    "☕ 赞助": "☕ Sponsor",
    "开始翻译": "Start",
    "停止翻译": "Stop",
    "方向:": "Direction:",
    "我说": "Me",
    "别人说": "Them",
    "双向同时": "Both",
    "输出:": "Output:",
    "手腕屏": "Wrist Overlay",
    "译音输出": "Voice Output",
    "🌐 气泡: 译文": "🌐 Bubble: Translated",
    "📝 气泡: 原文": "📝 Bubble: Source",
    "气泡改为显示译文": "Bubble now shows the translation",
    "气泡改为显示原文": "Bubble now shows the source text",
    "就绪": "Ready",
    # ---- 空聊天区占位提示 ----
    "译文会显示在这里": "Translations will appear here",
    "点「开始翻译」后开始说话": "Hit Start, then just talk",
    "状态：{msg}": "Status: {msg}",

    # ---- 房间文本中继 ----
    "房间": "Room",
    "房间码:": "Room code:",
    "昵称:": "Nickname:",
    "状态：{state} · {n} 人": "Status: {state} · {n} online",
    "未连接": "Not connected",
    "连接中": "Connecting",
    "已连接": "Connected",
    "重连中": "Reconnecting",
    "错误": "Error",
    "房间出错（翻译不受影响）：{msg}": "Room error (translation unaffected): {msg}",
    "连接房间": "Join Room",
    "断开连接": "Disconnect",
    "先在「设置 → 房间」里填房间码": "Set a room code in Settings → Room first",
    "随机生成": "Generate",
    # ---- 输入框右键菜单 ----
    "剪切": "Cut",
    "复制": "Copy",
    "粘贴": "Paste",
    "全选": "Select All",
    "和填了同一个房间码的人互相看到对方说的话；只有你自己说的话会被发出去。": "Everyone who enters the same room code sees each other's speech. Only what you say is sent.",
    "改动会即时保存；已连接时按新设置重连。": "Changes are saved right away; if connected, the room reconnects with the new settings.",

    # ---- 手腕屏微调面板 ----
    "锚点:": "Anchor:",
    "tracker 序号:": "Tracker #:",
    "（仅锚点=外部 tracker 时有效）": "(only when Anchor = External Tracker)",
    "右手": "Right Hand",
    "左手": "Left Hand",
    "外部 tracker": "External Tracker",
    "头显前固定": "Fixed to HMD",
    "位置X": "Pos X",
    "位置Y": "Pos Y",
    "位置Z": "Pos Z",
    "俯仰X": "Pitch",
    "偏航Y": "Yaw",
    "翻滚Z": "Roll",
    "大小": "Size",
    "弯曲": "Curvature",
    "透明度": "Opacity",
    "译文字号": "Font Size",
    "原文字号": "Source Size",
    "面板高": "Panel H",
    "底板不透明度": "Plate Opacity",
    "原文不透明度": "Source Opacity",

    # ---- 打字输入行 ----
    "打字:": "Type:",
    "发送": "Send",
    "回车发送 · Esc 清空": "Enter to send · Esc to clear",
    "打字替代的是麦克风 —— 先点「开始翻译」，且方向要含「我说」":
        "Typing replaces the microphone — click \"Start\" first, "
        "and the direction must include \"Me\"",
    "打字已送出（{n} 字），翻译中…": "Sent {n} characters, translating…",
    "引擎还没就绪，稍后重试": "Engine not ready yet — try again shortly",

    # ---- 状态栏 / 启动停止 ----
    "运行中": "Running",
    "仅翻译你说的话": "Your speech only",
    "已翻译 {n} 条": "{n} translated",
    "首增量 {ms}ms": "First delta {ms} ms",
    "还没配置 API key —— 点右上角「API key ›」填一个再开始":
        "No API key configured — click the \"API key ›\" button "
        "(top right) to add one before starting",
    "请至少选择一个输出": "Select at least one output",
    "chatbox 只发「我说的话」的译文（当前方向不含它）→ 本次 chatbox 不会输出；对方的译文看手腕屏／聊天区":
        "chatbox only carries translations of your own speech (not in the current "
        "direction), so nothing goes to chatbox this time; their translations "
        "appear on the wrist overlay / in the chat area",
    "译音输出只对「我说的话」方向有效（当前方向不含它）→ 本次已忽略":
        "Voice output only works for your own speech (not in the current "
        "direction) — ignored this time",
    "正在启动…（只翻译你说的话；要翻译对方/视频请选「双向同时」）":
        "Starting… (translating your speech only; choose \"Both\" "
        "to translate the other side / videos)",
    "正在启动（双向）…": "Starting (both directions)…",
    "正在启动…": "Starting…",
    "已停止": "Stopped",
    "正在停止…": "Stopping…",
    "已停止（上一次会话仍在收尾）": "Stopped (previous session still wrapping up)",
    "已切换为{target} → 中文": "Switched to {target} → Chinese",

    # ---- 设备扫描 / 选择 ----
    "自动检测": "Auto-Detect",
    "设备扫描失败：{msg}": "Device scan failed: {msg}",
    "建议停止翻译后再刷新设备列表": "Stop translation before refreshing the device list",
    "正在扫描设备…": "Scanning devices…",
    "未扫描到设备（远程会话下枚举为空是正常的）":
        "No devices found (an empty list is normal in a remote session)",
    "已扫描到 {m} 个麦克风 / {l} 个 loopback / {o} 个输出":
        "Found {m} mic(s) / {l} loopback / {o} output(s)",
    "已扫描到 {m} 个麦克风（VRChat 音频与译音输出自动处理）":
        "Found {m} mic(s) (VRChat audio and voice output are automatic)",
    "已选设备：{names}": "Selected devices: {names}",
    "设备：全部自动检测": "Devices: all Auto-Detect",

    # ---- 设置弹窗 ----
    "设置": "Settings",
    # 分页标签（Notebook tab）
    "常规": "General",
    "音频": "Audio",
    "词库": "Glossary",
    "关于": "About",
    "界面语言": "Interface Language",
    "界面语言在重启程序后生效": "Takes effect after restarting the app",
    "已保存：重启程序后界面将切换为 {lang}":
        "Saved — the interface will switch to {lang} after restart",
    "界面语言已保存：{lang}（重启程序后生效）":
        "Language saved: {lang} (takes effect after restart)",
    "清除": "Clear",
    "保存": "Save",
    "音频设备": "Audio Devices",
    "刷新": "Refresh",
    "麦克风:": "Microphone:",
    "VRChat 音频:": "VRChat Audio:",
    "译音输出:": "Voice Output:",
    "设备选择自动保存到 config.yaml": "Device selection is saved to config.yaml automatically",
    "Linux：VRChat 音频与译音输出已自动处理":
        "Linux: VRChat audio and voice output are handled automatically",
    # ---- 设置弹窗：输入门限（只作用于 VRChat 输出 /「别人说话」）----
    "输入门限": "Input Gate",
    "启用 —— 低于门限的声音不翻译（滤掉远处说话小声的玩家）":
        "Enable — audio below the threshold is not translated "
        "(filters out players talking softly in the distance)",
    "当前电平:": "Level:",
    "门限:": "Threshold:",
    "只有响度超过门限的声音才会被翻译；改完立刻生效（勾选「启用」后这里显示实时电平）":
        "Only sounds louder than the threshold are translated; changes take effect "
        "immediately (the live level shows here while \"Enable\" is checked)",
    "输入门限已保存：{db} dB": "Input gate saved: {db} dB",
    # ---- 设置弹窗：音色 ----
    "音色": "Voice Timbre",
    "说话译音:": "Spoken voice:",
    "打字译音:": "Typed voice:",
    "说话译音跟随「译音输出」开关（改完下次开始翻译生效）；打字译音立刻生效":
        "The spoken voice follows the \"Voice Output\" toggle and takes effect "
        "when you start translating again; the typed voice applies immediately",
    "（正在翻译：下次开始翻译生效）": " (translating now — applies when you start again)",
    "（已同步方向级音色 directions.mine.voice）":
        " (direction-level directions.mine.voice synced)",
    "（下次开始翻译生效）": " (applies when you start translating)",
    "说话译音音色已保存：{v}": "Spoken voice saved: {v}",
    "打字译音音色已保存：{v}（下一条打字即生效）":
        "Typed voice saved: {v} (applies to the next typed message)",
    # ---- 设置弹窗：音色试听 ----
    "试听": "Preview",
    "试听中…": "Previewing…",
    "正在试听「{v}」…": "Previewing \"{v}\"…",
    "试听完成：{v}": "Preview done: {v}",
    "试听失败：{msg}": "Preview failed: {msg}",
    "此音色暂不支持试听（服务端拒收该音色 id）":
        "This voice can't be previewed (the server rejected this voice id)",
    "还没配置 API key，无法试听（见右上角「设置」）":
        "No API key configured — can't preview (see \"Settings\" at the top right)",
    "请先选择或填写音色": "Please pick or type a voice first",

    # ---- 设置弹窗：专有词库 ----
    "专有词库": "Glossary",
    "作用方向:": "Scope:",
    "全局": "Global",
    "每行一条，格式：原文=译名（社团名 / 人名 / 专有术语）；作用于两个方向 —— 两个方向都要同一个译名时才放这里":
        "One per line as source=target (club names, player names, jargon); applies to BOTH "
        "directions — only put entries here when both directions want the same name",
    "每行一条，格式：原文=译名（社团名 / 人名 / 专有术语）；只对「{dir}」这条腿生效，同名词条会覆盖全局":
        "One per line as source=target (club names, player names, jargon); only affects the "
        "\"{dir}\" leg, and entries here override the global table",
    "保存词库": "Save Glossary",
    "已保存 {n} 条词条到「{scope}」（正在翻译时会重建会话生效）":
        "Saved {n} entries to \"{scope}\" (takes effect after the session is rebuilt while translating)",
    "已保存 {n} 条词条到「{scope}」；{bad} 行看不懂已忽略（要写成 原文=译名）":
        "Saved {n} entries to \"{scope}\"; {bad} unreadable line(s) ignored (use source=target)",
    "保存失败：{err}": "Save failed: {err}",
    "日志": "Logs",
    "导出日志压缩包…": "Export Log Archive…",
    "导出日志压缩包": "Export Log Archive",
    "ZIP 压缩包": "ZIP Archive",
    "所有文件": "All Files",
    "开发者": "Developer",
    "由可爱的赛博巫师和他的朋友们 开发": "Made by the lovely Cyber Wizard and friends",
    "赞助者": "Sponsors",
    "软件更新": "Software Update",
    "检查更新": "Check for Updates",
    "当前版本 v{ver} · 启动时会自动检查一次":
        "Current version v{ver} · checked automatically once at startup",
    "检查中…": "Checking…",
    "正在检查更新…": "Checking for updates…",
    "v{ver} 已设为「不再提示这个版本」": "You won't be notified about v{ver} again",
    "发现新版本 v{new}（当前 v{cur}）": "New version v{new} available (current: v{cur})",
    "已是最新 v{ver} ✅": "Already up to date (v{ver}) ✅",
    "检查失败：{reason}。可以点「检查更新」重试。":
        "Check failed: {reason}. Click \"Check for Updates\" to retry.",

    # ---- API key 状态 / 操作 ----
    "⚠️ 读取 key 状态失败：{msg}": "⚠️ Failed to read key status: {msg}",
    "当前：{src} {masked}": "Current: {src} {masked}",
    "⚠️ 未配置 API key —— 在上面粘贴后点「保存」":
        "⚠️ No API key configured — paste it above and click \"Save\"",
    "API key 已配置": "API key configured",
    "⚠ 未配置 API key · 点此开通千问云 ▸": "⚠ No API Key · Set Up Qwen Cloud ▸",
    "❌ 没保存：{msg}": "❌ Not saved: {msg}",
    "API key 保存失败：{msg}": "Failed to save API key: {msg}",
    "API key 已保存（{shown}）": "API key saved ({shown})",
    "已清除保存的 API key": "Saved API key cleared",
    "本来就没有保存过 API key": "No saved API key to clear",
    "打不开浏览器，请手动复制访问：{url}":
        "Can't open the browser — please copy and visit: {url}",

    # ---- 服务线路（千问云 / 千问云·海外版，互斥）----
    "服务线路": "Service Line",
    "千问云": "Qwen Cloud",
    "千问云·海外版": "Qwen Cloud (International)",
    "国内用「千问云」；海外用「千问云·海外版」（qwencloud.com）。两版的 API key 不互通，各存各的，来回切线路不用重填":
        "Use \"Qwen Cloud\" in mainland China and \"Qwen Cloud (International)\" "
        "(qwencloud.com) overseas. The two lines use separate API keys: each is "
        "stored on its own, so switching back and forth needs no re-entering",
    "⚠ 未配置 API key · 点此开通海外版 ▸": "⚠ No API Key · Set Up International ▸",
    "保存线路设置": "Save Line Settings",
    "已切换到 {line}（重启翻译后生效）":
        "Switched to {line} — takes effect when you start translation again",
    "当前线路需要先停止翻译，改完再重新开始":
        "Stop translation first; the new line applies once you start it again",
    "当前（{line}）：{src} {masked}": "Current ({line}): {src} {masked}",


    # ---- 日志导出 ----
    "打不开保存对话框：{msg}": "Can't open the save dialog: {msg}",
    "导出日志失败：{msg}": "Failed to export logs: {msg}",
    "日志已导出：{path}（{n} 个文件，{size} KB）":
        "Logs exported: {path} ({n} file(s), {size} KB)",
    "｜已脱敏 {n} 个文件": " · {n} file(s) redacted",
    "导出完成": "Export Complete",
    "把这个压缩包发给维护者即可。": "Send this archive to the maintainer.",
    "共 {n} 个文件，{size} MB（超过 {cap} MB 自动删最旧的）\n{path}\n出问题时导出压缩包发给维护者即可（自动脱敏，不含密钥）":
        "{n} file(s), {size} MB in total (oldest auto-deleted beyond {cap} MB)\n"
        "{path}\n"
        "If something goes wrong, export the archive and send it to the "
        "maintainer (auto-redacted, no keys included)",

    # ---- 暂未被本批调用点引用、但测试/下一批需要的词条 ----
    "已下载 {n} MB": "Downloaded {n} MB",

    # ---- 赞助弹窗 ----
    "赞助": "Sponsor",
    "☕ 请我喝一杯": "☕ Buy Me a Coffee",
    "打开 Ko-fi 赞助页面": "Open Ko-fi Sponsor Page",
    "微信": "WeChat",
    "支付宝": "Alipay",
    "扫码支持 · 你给的钱会变成 API token，然后被我烧掉":
        "Scan to support · your money becomes API tokens, which I then burn",
    "关闭": "Close",
    "二维码图片缺失": "QR image missing",
    "赞助弹窗打不开：{msg}": "Can't open the sponsor window: {msg}",
    "打不开浏览器，请手动访问 {url}": "Can't open the browser — please visit: {url}",

    # ---- 更新弹窗 / 下载进度窗 / 完成态 / 一次性提示 ----
    "原因见日志": "see the log for details",
    "「不再提示」没存下来：{msg}": "Couldn't save the \"skip this version\" choice: {msg}",
    "发现新版本": "New Version Available",
    "VRChat Live Translate 有新版本了。现在更新只要一两分钟，不影响你正在进行的翻译。":
        "A new version of vrchat-livetranslate is available. "
        "Updating takes just a minute or two and won't interrupt your ongoing translation.",
    "看看这次更新了什么": "See What's New in This Release",
    "立即更新": "Update Now",
    "下次再说": "Later",
    "不再提示这个版本": "Skip This Version",
    "如何更新": "How to Update",
    "你现在运行的是源码版，不能自动更新。\n\n"
    "· 会用 git：在仓库目录跑 git pull 就是最新版；\n"
    "· 或者点「确定」打开新版本下载页，下载安装包。\n\n"
    "点「取消」先不更新。":
        "You're running from source, so it can't update itself.\n\n"
        "· If you use git: run git pull in the repo folder to get the latest version;\n"
        "· or click \"OK\" to open the download page and grab the installer.\n\n"
        "Click \"Cancel\" to skip for now.",
    "无法自动更新": "Can't Update Automatically",
    "程序所在的位置不允许写入（比如放在 Program Files）。\n\n"
    "点「确定」打开下载页，自己下载新版本；点「取消」先不更新。":
        "The program's location isn't writable (e.g. it's in Program Files).\n\n"
        "Click \"OK\" to open the download page and download the new version yourself; "
        "click \"Cancel\" to skip for now.",
    "正在下载新版本": "Downloading New Version",
    "已下载 {done} / 约 {total} MB，一般 1–3 分钟就好。下载期间可以正常翻译。":
        "Downloaded {done} / about {total} MB — usually done in 1–3 minutes. "
        "You can keep translating while it downloads.",
    "已下载 {done} MB，请稍等。": "Downloaded {done} MB, please wait…",
    "点右上角关闭会取消下载，下次可以再下。":
        "Closing this window cancels the download; you can download it again later.",
    "打开下载页自己下": "Open the Download Page and Get It Yourself",
    "下载完成": "Download Complete",
    "新版本已经准备好了。\n"
    "点「立即重启并更新」：关闭当前窗口、自动换上新版本并重新打开。\n"
    "点「稍后更新」：继续用现在的版本；等你关闭程序时会自动换好，下次打开就是新版。":
        "The new version is ready.\n"
        "\"Restart and update now\": closes this window, swaps in the new version and reopens it.\n"
        "\"Update later\": keep using the current version; it swaps in when you close the app, "
        "and the next launch is the new version.",
    "立即重启并更新": "Restart and Update Now",
    "稍后更新": "Update Later",
    "下载没有成功": "Download Failed",
    "下载没有成功（网络可能不太稳定）。\n\n"
    "点「重试」再下载一次；点「取消」暂时跳过。\n"
    "（之后也可以到「设置 → 软件更新」再检查）":
        "The download didn't succeed (the network may be unstable).\n\n"
        "Click \"Retry\" to download again; click \"Cancel\" to skip for now.\n"
        "(You can also check again later in \"Settings → Software Update\".)",
    "正在重启…": "Restarting…",
    "更新没有成功": "Update Failed",
    "更新没有成功，现在的版本不受影响，可以继续用。\n\n"
    "点「重试」再试一次；点「取消」先继续用现在的版本"
    "（窗口里也可以「打开下载页自己下」）。":
        "The update didn't succeed; your current version is unaffected and keeps working.\n\n"
        "Click \"Retry\" to try again; click \"Cancel\" to keep using the current version "
        "(you can also \"open the download page\" in the window).",
    "更新没有成功，现在的版本不受影响，下次打开还是它。\n\n"
    "点「确定」打开下载页自己下；点「取消」直接退出。":
        "The update didn't succeed; your current version is unaffected and will still be "
        "there next time.\n\n"
        "Click \"OK\" to open the download page and get it yourself; "
        "click \"Cancel\" to exit directly.",
    "已更新到最新版本": "Updated to the Latest Version",
    "VRChat Live Translate 已更新到最新版本，一切照常使用。":
        "vrchat-livetranslate has been updated to the latest version — "
        "everything works as usual.",
    "知道了": "Got It",

    # ---- 日志区 ----
    "打开日志文件夹": "Open Log Folder",
    "打不开日志文件夹：{msg}": "Can't open the log folder: {msg}",

    # ---- credentials.py：来源标签 / 校验错误 / 打码片段 ----
    "界面设置": "Saved in app settings",
    "环境变量 DASHSCOPE_API_KEY": "Environment variable DASHSCOPE_API_KEY",
    "百炼 CLI 配置": "Bailian CLI config",
    "未配置": "Not configured",
    "密钥不能为空": "The key must not be empty",
    "密钥不能包含空白字符": "The key must not contain whitespace",
    "密钥长度不足（{n} < 16）": "Key too short ({n} < 16)",
    "{head}****（{n} 字符）": "{head}**** ({n} chars)",
    "{head}****{tail}（{n} 字符）": "{head}****{tail} ({n} chars)",

    # ---- update_check.py：给用户看的异常消息（[update] 日志保持中文） ----
    "GitHub 限流（未认证 60 次/小时），稍后再试":
        "GitHub rate limit hit (60 requests/hour unauthenticated) — try again later",
    "网络不可达：{msg}": "Network unreachable: {msg}",
    "网络不可达：连接超时": "Network unreachable: connection timed out",
    "响应不是合法 JSON：{msg}": "Response is not valid JSON: {msg}",
    "最新 Release 的 tag 不是版本号：{tag}":
        "The latest release tag is not a version number: {tag}",
    "Release 附件不全：缺少 {name} 或校验值":
        "Release assets incomplete: {name} or its checksum is missing",
    "替换 AppImage 失败：{msg}": "Failed to replace the AppImage: {msg}",
    "不是合法版本号，无法写入忽略列表：{version}":
        "Not a valid version number; can't add it to the ignore list: {version}",
    "config.yaml 不是合法 YAML，已放弃写入：{msg}":
        "config.yaml is not valid YAML; write aborted: {msg}",
    "拒绝非 https 地址：{url}": "Refusing non-https URL: {url}",
    "拒绝白名单外的地址：{url}": "Refusing URL outside the allowlist: {url}",
    "{sums} 里 {exe} 的校验值格式不对":
        "The checksum for {exe} in {sums} has an invalid format",
    "{sums} 里没有 {exe} 的校验值": "No checksum for {exe} found in {sums}",
    "下载的文件与发布页的摘要对不上，已删除（请重试）":
        "The downloaded file doesn't match the checksum on the release page; "
        "it has been deleted (please retry)",
    "手腕屏已开启（位置 / 字号等见「设置 → 手腕屏」）":
        "Wrist overlay is on — position, font size and more are under "
        "\"Settings → Wrist Overlay\"",
    "手腕屏没启动起来，已自动取消勾选（先把 SteamVR 打开，再勾一次即可）":
        "The wrist overlay could not start, so the tick was reverted — "
        "start SteamVR first, then tick it again",

    # ---- 桌面字幕（PC 桌面模式叠加窗）----
    "桌面字幕": "Desktop Subtitles",
    "桌面字幕已开启（拖到想要的位置；尺寸/字号/透明度见「设置 → 桌面字幕」）":
        "Desktop subtitles are on — drag them where you want; size, font and opacity are "
        "under \"Settings → Desktop Subtitles\"",
    "桌面字幕没启动起来，已自动取消勾选":
        "Desktop subtitles could not start, so the tick was reverted",
    "桌面字幕还没开启，先勾上「桌面字幕」再解锁拖动":
        "Desktop subtitles aren't running — tick \"Desktop Subtitles\" first, "
        "then unlock dragging",
    "解锁拖动": "Unlock & Drag",
    "锁定位置": "Lock Position",
    "桌面字幕已解锁：拖动字幕窗到想要的位置，放好后点「锁定位置」":
        "Desktop subtitles unlocked — drag the window where you want it, "
        "then click \"Lock Position\"",
    "桌面字幕位置已记住": "Desktop subtitle position saved",
    "（字幕窗默认可穿透，先解锁再拖；改动即时生效）":
        "(the window ignores clicks by default — unlock first, then drag; changes apply instantly)",
    "端口:": "Port:",
    "保存端口": "Save Port",
    "VRChat 默认收 9000 端口；只有你在 VRChat 里改过 OSC 端口（或中间挂了转发工具）时才需要动这里":
        "VRChat listens on port 9000 by default — change this only if you changed "
        "VRChat's OSC port (or run a forwarding tool in between)",
    "端口必须是 1–65535 之间的整数": "Port must be an integer between 1 and 65535",
    "OSC 端口已保存：{port}": "OSC port saved: {port}",
    "OSC 端口已保存：{port}（正在翻译，重开翻译后生效）":
        "OSC port saved: {port} (restart translation to apply)",
    "面板宽度": "Panel Width",
    "面板高度": "Panel Height",
    "头显里那块手腕屏的锚点、位置 / 旋转 / 字号等参数。":
        "Anchor, position / rotation / font size and more for the in-headset wrist overlay.",
    "贴在 VRChat 窗口上的那块字幕窗：尺寸 / 字号 / 透明度。":
        "The subtitle window pinned to the VRChat window: size / font size / opacity.",
    # ---- 麦克风代理（原声/译音一键切换）----
    # 运行期状态（经 `gui._on_proxy_status`：日志打中文原文、状态栏查这里的词条）。
    # key = 代理发出的中文模板（与 vlt/output/micproxy*.py 逐字一致，含 {占位符}）。
    "检测到测试进程（{name}）→ 拒绝{action}（这条腿不启用）":
        "Test process detected ({name}) → refusing to {action} (this leg stays off)",
    "未找到输出设备 {dev}，回退到回退链":
        "Output device {dev} not found — falling back to the fallback chain",
    "枚举输出设备失败：{err}（其余功能不受影响）":
        "Failed to enumerate output devices: {err} (everything else still works)",
    "没找到匹配的虚拟声卡输出设备（虚拟声卡装好了吗？）→ 麦克风代理不可用，其余功能不受影响。":
        "No matching virtual sound card output found (is it installed?) — Mic Proxy unavailable; everything else still works.",
    "虚拟声卡已打开：#{idx} {name}": "Virtual sound card opened: #{idx} {name}",
    "虚拟声卡 #{idx} 打不开：{err}": "Cannot open virtual sound card #{idx}: {err}",
    "打开虚拟声卡失败：{err}（其余功能不受影响）":
        "Failed to open the virtual sound card: {err} (everything else still works)",
    "麦克风直通线程异常退出：{kind}: {err}":
        "Mic passthrough thread exited with an error: {kind}: {err}",
    "麦克风直通已启动（{rate}Hz {channels}ch → 48k 立体声）":
        "Mic passthrough started ({rate}Hz {channels}ch → 48k stereo)",
    "直通缓冲欠载 {n} 次/{secs}s（可能爆音）：可在 设置→音频 调大直通缓冲":
        "Passthrough buffer underran {n} times in {secs}s (may crackle) — raise it in Settings → Audio",
    "翻译未运行，无法切到译音档（保持原声）":
        "Translation isn't running — can't switch to Translated (staying on Original)",
    "已切到「译音」档": "Switched to Translated",
    "已切到「原声」档": "Switched to Original",
    "虚拟声卡声明失败 → 麦克风代理不可用（其余功能不受影响）":
        "Failed to declare the virtual sound card — Mic Proxy unavailable (everything else still works)",
    "麦克风代理输出已接到虚拟声卡节点：{target}":
        "Mic Proxy output connected to the virtual sound card node: {target}",
    "麦克风代理": "Mic Proxy",
    "启用 —— VRChat 麦克风固定选虚拟声卡，原声/译音在主界面一键切":
        "Enable — VRChat mic stays on the virtual cable; switch Original/Translated in the main window",
    "直通缓冲(ms):": "Passthrough buffer (ms):",
    "译音缓冲(ms):": "Translated buffer (ms):",
    "直通缓冲越小延迟越低（下限 60ms）；持续爆音请调大。改完即时生效。":
        "Smaller passthrough buffer = lower latency (min 60ms); increase it if the audio keeps crackling. Applies instantly.",
    "已关闭：回到旧行为（译音输出随翻译启停，主界面切换开关置灰）":
        "Disabled: back to the old behavior (translated output follows translation start/stop; the main-window toggle is greyed out)",
    "虚拟声卡未打开——检查「译音输出」设备；缓冲改动已存，下次生效":
        "Virtual cable not open — check the Translated Output device; buffer changes saved, applied next time",
    "麦克风代理不可用（虚拟声卡没打开？）；原声/译音切换已禁用":
        "Mic Proxy unavailable (virtual cable not open?); Original/Translated switching disabled",
    "麦克风代理已启用": "Mic Proxy enabled",
    "麦克风代理已关闭（回到旧行为）": "Mic Proxy disabled (back to the old behavior)",
    "程序会把真实麦克风直通到虚拟声卡（VRChat 里麦克风固定选它），并常驻占用它的输出流。\n\n如果你的「默认播放设备」也是这块虚拟声卡，会听到自己的声音（回声）。\n不需要的话：设置 → 麦克风代理 → 取消勾选（立即释放声卡）。":
        "The app passes your real microphone through to the virtual sound card (keep VRChat's "
        "microphone set to it) and keeps that card's output stream open while it runs.\n\n"
        "If your default playback device is the same virtual sound card, you will hear yourself (echo).\n"
        "If you don't need it: Settings → Mic Proxy → uncheck it (the sound card is released right away).",
    "直通麦克风已切换：{name}": "Passthrough microphone switched: {name}",
    "麦克风已切换（直通即时生效；翻译输入下轮生效）":
        "Microphone switched (passthrough applied now; translation input takes effect when translation restarts)",
    "麦克风已保存（切换未即时生效，下次启动生效）":
        "Microphone saved (not applied instantly; takes effect on next start)",
    "缓冲已更新（即时生效）": "Buffers updated (applied instantly)",
    "缓冲已保存（译音缓冲下次开始翻译生效）":
        "Buffers saved (translated buffer applies next time translation starts)",
    "未勾选「译音输出」，译音档会无声（已在输出行勾选后重试）":
        "Translated Output is unchecked, so Translated mode will be silent (tick it in the output row and try again)",
    "🎙 原声": "🎙 Original",
    "🗣 译音": "🗣 Translated",
    # ---- 译音音量（固定增益 / 跟随麦克风）----
    "译音音量": "Translated Audio Volume",
    "原声与译音共用一条虚拟声卡输出流，VRChat 只能整体调麦克风音量——译音比原声响/轻要在这里修": "Original voice and translated audio share one virtual sound card output stream, and VRChat can only adjust microphone volume as a whole - fix translated audio being louder or quieter than your voice here",
    "模式:": "Mode:",
    "固定增益(dB):": "Fixed gain (dB):",
    "相对麦克风(dB):": "Relative to mic (dB):",
    "当前增益:": "Current gain:",
    "改完立即生效（逐句重算，不给译音加缓冲）": "Applies immediately (recomputed per sentence; no extra buffering for translated audio)",
    "关闭音量匹配": "Off",
    "固定增益": "Fixed gain",
    "跟随麦克风": "Follow microphone",
    "固定增益：所有译音统一加减这么多 dB（负数=更轻）": "Fixed gain: shift all translated audio by this many dB (negative = quieter)",
    "跟随麦克风：把译音对齐到你说话时的电平（跨设备免调）": "Follow microphone: match translated audio to your own speaking level (no tuning needed across devices)",
    "已关闭：译音逐字节原样输出（与升级前行为一致）": "Off: translated audio is passed through byte for byte (identical to the behavior before this feature)",
    "麦克风 {mic} dBFS ｜ 译音 {tts} dBFS ｜ 增益 {gain} dB": "Mic {mic} dBFS | Translated {tts} dBFS | Gain {gain} dB",
    "译音音量已更新（逐句生效）": "Translated audio volume updated (applies per sentence)",
    "麦克风电平参考取不到 → 译音音量匹配回落固定增益 {db} dB": "Microphone level reference unavailable - translated volume matching falls back to fixed gain {db} dB",
}
