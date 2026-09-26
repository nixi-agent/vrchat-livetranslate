"""English 词表：key = 中文原文（与 `t("…")` 调用点逐字一致），value = 英文界面文案。

语气约定：深色主题桌面工具，简洁、口语化；按钮用祈使句；标题/按钮用 Title Case。
带 {占位符} 的条目，占位符名必须与调用点 `t("…", name=…)` 一致。
"""
from __future__ import annotations

STRINGS: dict[str, str] = {
    # ---- 主窗口 ----
    "VRChat 实时同传": "VRChat Live Translate",
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
    "微调 ▸": "Tune ▸",
    "微调 ▾": "Tune ▾",
    "译音输出": "Voice Output",
    "就绪": "Ready",
    "状态：{msg}": "Status: {msg}",

    # ---- 手腕屏微调面板 ----
    "锚点:": "Anchor:",
    "tracker 序号:": "Tracker #:",
    "（仅锚点=前臂 tracker 时有效）": "(only when Anchor = Forearm Tracker)",
    "右手": "Right Hand",
    "左手": "Left Hand",
    "前臂 tracker": "Forearm Tracker",
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
    "已选设备：{names}": "Selected devices: {names}",
    "设备：全部自动检测": "Devices: all Auto-Detect",

    # ---- 设置弹窗 ----
    "设置": "Settings",
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
    "日志": "Logs",
    "导出日志压缩包…": "Export Log Archive…",
    "导出日志压缩包": "Export Log Archive",
    "ZIP 压缩包": "ZIP Archive",
    "所有文件": "All Files",
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
    "⚠ 未配置 API key · 点此开通百炼 ▸": "⚠ No API Key · Set Up Bailian ▸",
    "❌ 没保存：{msg}": "❌ Not saved: {msg}",
    "API key 保存失败：{msg}": "Failed to save API key: {msg}",
    "API key 已保存（{shown}）": "API key saved ({shown})",
    "已清除保存的 API key": "Saved API key cleared",
    "本来就没有保存过 API key": "No saved API key to clear",
    "打不开浏览器，请手动复制访问：{url}":
        "Can't open the browser — please copy and visit: {url}",

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
        "A new version of VRChat Live Translate is available. "
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
        "VRChat Live Translate has been updated to the latest version — "
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
    "Release 附件不全：需要 {exe} 和 {sums}":
        "Release assets incomplete: {exe} and {sums} are required",
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
}
