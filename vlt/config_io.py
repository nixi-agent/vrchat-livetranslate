"""config.yaml 就地改写工具：`vlt/gui.py` 与 `vlt/update_check.py` 共用的单一真相。

为什么不 import gui 来复用：gui 的 import 链会拉起 tkinter / devices / engine，
而 update_check 必须保持纯逻辑、离线可测。所以这三个函数住在这个**无依赖**
（只用到 re / yaml / pathlib）的小模块里，双方都从这儿拿。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


def _yaml_set_in_text(text: str, path: list[str], value: str) -> str:
    """在 YAML 文本里**就地**改一个叶子值，保留注释、空行与键的顺序。

    为什么不用 `yaml.safe_load` + `yaml.dump` 整文件重写：那会抹平所有注释和顺序
    （实测把一份带完整中文说明的 config.yaml 变成一坨没有注释的键值对，键还被按字母重排）。
    配置文件是给人读的，程序存个设置不该毁掉它的可读性。
    找不到路径就返回原文——宁可这次没生效，也不退化成整文件重写。
    """
    lines = text.split("\n")

    def _span(key: str, indent: int, lo: int, hi: int):
        head = re.compile(rf"^(\s*){re.escape(key)}:\s*$")
        for i in range(lo, hi):
            m = head.match(lines[i])
            if m is None or len(m.group(1)) != indent:
                continue
            sub_hi = hi
            for j in range(i + 1, hi):
                if lines[j].strip() and not lines[j].startswith(" " * (indent + 1)):
                    sub_hi = j
                    break
            return i, sub_hi
        return None

    lo, hi, indent = 0, len(lines), 0
    for key in path[:-1]:
        got = _span(key, indent, lo, hi)
        if got is None:
            return text
        lo, hi = got[0] + 1, got[1]
        indent += 2

    leaf = path[-1]
    pat = re.compile(rf"^(\s*){re.escape(leaf)}:(\s*)([^#\n]*)(\s*#.*)?$")
    for i in range(lo, hi):
        m = pat.match(lines[i])
        if m and len(m.group(1)) == indent:
            comment = (m.group(4) or "").strip()
            new_line = f"{m.group(1)}{leaf}: {value}" + (f"   {comment}" if comment else "")
            # ⚠️ 关键：如果这一项的旧值是**多行块**（块序列 / 嵌套映射），必须把子行一并删掉，
            # 否则会留下孤立的 `- 0.0` 之类 → 整个文件变成非法 YAML。
            # 用户实测踩过：旧版整文件 yaml.dump 会把 `pos: [0.0, 0.06, 0.02]` 写成
            #    pos:
            #    - 0.0
            #  而本函数当时只换了 `pos:` 那一行，热重载就报
            #  `expected <block end>, but found '-'`，界面上拖滑块完全没效果。
            #
            # ⚠️ 但**纯注释行绝不删**：说明注释常写成「比所属键缩进更深」的续行
            #   （如 `rot:` 下面那几行「换左手要镜像」），旧版本把注释当子块一并吃掉 ——
            #   实测拖一次滑块 / 选一次设备就少 5~6 行说明（PR #4 审查：70 → 65）。
            #   注释不影响 YAML 语义，保留它们、只删真正的块行即可（见下方 drop）。
            drop: list[int] = []
            j = i + 1
            while j < hi and lines[j].strip():
                stripped = lines[j].lstrip()
                ind_j = len(lines[j]) - len(stripped)
                if stripped.startswith("#"):
                    if ind_j > indent:      # 本键的深层注释：保留，但继续往下扫块行
                        j += 1
                        continue
                    break                   # 同级注释：保留，块到此为止
                # 更深的缩进 = 属于本键的块；同级但以 "- " 开头 = 块序列（PyYAML 默认就不缩进）
                if ind_j > indent or (ind_j == indent and stripped.startswith("- ")):
                    drop.append(j)
                    j += 1
                    continue
                break
            for k in reversed(drop):        # 从后往前删，前面的行号才不会漂移
                del lines[k]
            lines[i] = new_line
            return "\n".join(lines)
    lines.insert(hi, f"{' ' * indent}{leaf}: {value}")
    return "\n".join(lines)


def _yaml_set_or_create(text: str, path: list[str], value: str) -> str:
    """就地改一个（可嵌套的）叶子值；**父级键缺失时按缩进补建整条链**。

    与 `_yaml_set_in_text` 同一取舍（保住注释 / 空行 / 键顺序，只在写前校验合法 YAML），
    区别只在「老配置里整段不存在」：旧函数遇到父键找不到就返回原文（静默 no-op），
    本函数把缺失的层级补到**已存在的父块末尾**（顶层缺就补到文件末尾）。
    场景：GUI 新加的打字译音音色写 `text_input.tts.voice`，而用户的旧 `config.yaml`
    是从更早的模板生成的、根本没有 `text_input` 段 —— 不补建的话这个设置永远存不下去。
    已存在的路径（如 `session.voice`）走就地替换，行为与旧函数一致。
    """
    lines = text.split("\n")

    def _find(key: str, indent: int, lo: int, hi: int):
        """在 [lo,hi) 里找缩进为 indent 的 `key:` → (行号, 子区间 lo, 子区间 hi)；没有返回 None。"""
        head = re.compile(rf"^(\s*){re.escape(key)}:(\s*)([^#\n]*)(\s*#.*)?$")
        for i in range(lo, hi):
            s = lines[i].lstrip()
            if not s or s.startswith("#"):
                continue
            if len(lines[i]) - len(s) != indent:
                continue
            if head.match(lines[i]) is None:
                continue
            child_hi = hi
            for j in range(i + 1, hi):
                s2 = lines[j].lstrip()
                if not s2 or s2.startswith("#"):
                    continue
                if len(lines[j]) - len(s2) <= indent:
                    child_hi = j
                    break
            return i, i + 1, child_hi
        return None

    def _walk(key_path: list[str], indent: int, lo: int, hi: int) -> None:
        got = _find(key_path[0], indent, lo, hi)
        if got is None:                          # 父级缺失 → 在本区间末尾补建整条链
            at = hi
            # ⚠️ 不能直接插在 `hi`（= 本块后面的第一个真行）：块尾往往还挂着**空行**
            #    和**下一段的标题注释**，插在它们后面的话，那段注释就变成新键的注释了。
            #    实测：在 `overlay:` 末尾补建 `offsets:`，结果
            #        # 第三条腿：译音输出（模型译音 → 虚拟声卡 → …）
            #          offsets:
            #            …
            #        output:
            #    「译音输出」的说明被挂到了手腕屏的键上。所以往回退过空行、
            #    以及**比本块更浅**的注释（那是下一段的标题；本块内部的续行注释
            #    缩进更深，必须留在原地 —— 见 `_yaml_set_in_text` 里那条取舍）。
            while at > lo:
                s = lines[at - 1].lstrip()
                if not s:
                    at -= 1
                    continue
                if s.startswith("#") and (len(lines[at - 1]) - len(s)) < indent:
                    at -= 1
                    continue
                break
            if at == len(lines) and lines and lines[-1] == "":
                at = len(lines) - 1               # 保住文件末尾的那个换行
            block: list[str] = []
            for d, k in enumerate(key_path):
                pad = " " * (indent + 2 * d)
                block.append(f"{pad}{k}: {value}" if d == len(key_path) - 1 else f"{pad}{k}:")
            lines[at:at] = block
            return
        i, clo, chi = got
        if len(key_path) == 1:                    # 叶子：就地替换，连带删掉旧的块子行（同旧函数）
            m = re.compile(rf"^(\s*){re.escape(key_path[0])}:(\s*)([^#\n]*)(\s*#.*)?$").match(lines[i])
            comment = (m.group(4) or "").strip() if m else ""
            lines[i] = f"{' ' * indent}{key_path[0]}: {value}" + (f"   {comment}" if comment else "")
            # 与 `_yaml_set_in_text` 同一取舍：删块行、**不删纯注释行**（见那边的说明）。
            drop: list[int] = []
            j = i + 1
            while j < chi:
                s = lines[j].lstrip()
                if not s:                   # 空行保留、继续扫（本函数原本就跨空行找子块）
                    j += 1
                    continue
                ind_j = len(lines[j]) - len(s)
                if s.startswith("#"):
                    if ind_j > indent:
                        j += 1
                        continue
                    break
                if ind_j > indent or (ind_j == indent and s.startswith("- ")):
                    drop.append(j)
                    j += 1
                    continue
                break
            for k in reversed(drop):
                del lines[k]
            return
        _walk(key_path[1:], indent + 2, clo, chi)

    _walk(path, 0, 0, len(lines))
    return "\n".join(lines)


def _yaml_set_or_create_checked(text: str, path: list[str], value: str) -> str:
    """`_yaml_set_or_create` 的**写后复检**版：解析回来确认路径上的值真的等于 *value*。

    为什么要多这一步：两个就地改写函数在「父键找不到 / 落点找不到」时都是**原样返回**
    （不抛、不报错也不留痕）—— 于是 `_write_config_text` 写回的是同一份文本，调用方只看到
    「没异常」，磁盘上其实一个字没变。这正是本仓库最忌讳的静默降级，光看返回值分不出来。
    实测形态：老用户的 `config.yaml`（更早的模板生成，**没有** `output.audio.proxy` 整段）
    里写 `proxy.hint_shown`，旧路径是静默 no-op → 「首次启用说明每次都弹」。

    复检**在内存里、写盘之前**：不等就直接抛，磁盘上的文件一个字没动，调用方那句
    「配置未改动」才是真话（与 `ui_text._persist_provider` 同一口径）。
    """
    out = _yaml_set_or_create(text, path, value)
    node = yaml.safe_load(out)
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise RuntimeError(
                f"{'.'.join(path)} 没能写进配置（就地改写找不到落点，路径在 {key!r} 处断开）")
        node = node[key]
    want = yaml.safe_load(value)
    if node != want:
        raise RuntimeError(
            f"{'.'.join(path)} 写进去的值不是期望的 {value!r}（读到 {node!r}）")
    return out


def _yaml_quote(x: object) -> str:
    """YAML 双引号标量（复用 JSON 的转义规则 —— YAML 双引号风格是它的超集）。

    一律加引号，是为了社团名/术语里那些 `#`、`:`、前后空格、`-` 开头不让 YAML 变味
    （`VRChat: VRChat` 不加引号也合法，但 `Rob: a club` 就不是了）。
    """
    return json.dumps("" if x is None else str(x), ensure_ascii=False)


def _yaml_set_mapping(text: str, path: list[str], mapping: dict[str, str]) -> str:
    """就地写入一整段**块映射**（如顶层 `glossary:` 专有词库），保留注释与键顺序。

    为什么需要第三个函数：`_yaml_set_in_text` / `_yaml_set_or_create` 都只改**单个叶子
    标量**。词库是一整段映射 —— 用它们得为每个词条各走一遍，还得先把不存在的条目
    删干净（否则用户删掉一条、文件里还留着，下次启动又「复活」）。整段替换语义明确：
    **这段归程序管，以界面里的当前内容为准**。

    ⚠️ 取舍（写下来免得后人踩）：段**内部**的注释会随整段一起被替换掉；段外（上方）
    的说明注释不受影响。所以模板里的词库说明一律写在 `glossary:` 上方，不写在段内。

    实现取巧但可靠：先借 `_yaml_set_or_create` 把该键收敛成 `key: {}`（顺带搞定
    「老配置里整段不存在」的补建），再把这**一行**展开成块映射。这样父级补建、
    注释保留、块行清理三个难点都复用了已经过测试的代码路径。

    mapping 为空 → 保留 `glossary: {}`（键在，用户才知道有这个功能）。
    """
    text = _yaml_set_or_create(text, path, "{}")
    if not mapping:
        return text

    leaf = path[-1]
    lines = text.split("\n")

    # ⚠️ 必须**逐级定位父块**再在里面找那一行，绝不能全文件按缩进找：
    #    同一层里可能另有一张同名的表 —— `directions.mine.hotwords` 与
    #    `directions.theirs.hotwords` 缩进都是 4 空格，全文件找第一个匹配就会改**错表**
    #    （实测踩过：存「别人说」的词条，结果写进了「我说」那张，目标那张留成 `{}`）。
    #    顺带：`hotwords:` 这种「值是映射、自己独占一行」的写法也匹配得到，
    #    而它往往是兄弟块里的同名键，正是最容易踩的那一脚。
    def _child_span(key: str, indent: int, lo: int, hi: int):
        """在 [lo,hi) 里找缩进为 indent 的 `key:`；返回 (行号, 子块起, 子块止)。"""
        head = re.compile(rf"^(\s*){re.escape(key)}:(\s*)([^#\n]*)(\s*#.*)?$")
        for i in range(lo, hi):
            stripped = lines[i].lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(lines[i]) - len(stripped) != indent or head.match(lines[i]) is None:
                continue
            child_hi = hi
            for j in range(i + 1, hi):
                s2 = lines[j].lstrip()
                if not s2 or s2.startswith("#"):
                    continue
                if len(lines[j]) - len(s2) <= indent:
                    child_hi = j
                    break
            return i, i + 1, child_hi
        return None

    lo, hi, indent = 0, len(lines), 0
    for key in path[:-1]:
        found = _child_span(key, indent, lo, hi)
        if found is None:
            return text          # 父级找不到（上一步刚写过，理论不可达）→ 宁可没生效，也不乱改
        _, lo, hi = found
        indent += 2

    pad = " " * indent
    pat = re.compile(rf"^{re.escape(pad)}{re.escape(leaf)}:\s*(\{{\}})?\s*(#.*)?$")
    for i in range(lo, hi):
        m = pat.match(lines[i])
        if m is None:
            continue
        comment = (m.group(2) or "").strip()
        block = [f"{pad}{leaf}:" + (f"   {comment}" if comment else "")]
        for k, v in mapping.items():
            # json.dumps 产出的就是合法的 YAML 双引号标量（含转义），且不会把 / 转义掉；
            # 一律加引号是为了社团名里那些 `#`、`:`、前后空格不让 YAML 变味。
            block.append(f"{pad}  {_yaml_quote(k)}: {_yaml_quote(v)}")
        lines[i:i + 1] = block
        return "\n".join(lines)
    return text          # 理论上不可达（上一步刚写过这行）；宁可这次没生效，也不乱改


def _write_config_text(path: Path, text: str) -> None:
    """写回配置前先验证仍是合法 YAML。

    宁可这次改动不生效（调用方会 catch 并打印），也**绝不能把用户的配置写坏** ——
    配置坏了影响的是启动，比一个滑块没生效严重得多。
    """
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"生成的新配置不是合法 YAML，已放弃写入：{exc}") from exc
    path.write_text(text, encoding="utf-8")


def _fmt_scalar(x) -> str:  # noqa: ANN001, ANN202
    """None → null；float → 紧凑写法（0.24 而不是 0.24000000000000002）；
    list/tuple → 行内流式 `[a, b]`（更新忽略列表等列表值要用）。"""
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        return f"{x:g}"
    if isinstance(x, (list, tuple)):
        return "[" + ", ".join(_fmt_scalar(i) for i in x) + "]"
    return str(x)


def _yaml_scalar(value) -> str:  # noqa: ANN001
    """把一个**字符串**渲染成安全的 YAML 标量（该不该加引号交给 PyYAML 决定）。

    为什么需要它：就地写配置时，值是直接拼进那一行的（`key: <value>`），
    而值里有几个来源是**外部数据** —— 设备名、音色 id 之类可能含 `#`（会把整行
    截成注释）、`: `、`[`、`&`、`*`、`%` 等 YAML 有意义的字符。整份 `yaml.dump`
    时代这件事是自动的，改成就地写之后必须自己保证不写出坏 YAML。
    `yaml.safe_dump` 只序列化这一个标量，返回 `CABLE Input` / `'a#b'` / `''`
    这样的安全写法（allow_unicode 保住中文设备名，不转成 \\uXXXX）。
    """
    # ⚠️ `width` 必须给到无穷大：PyYAML 默认 `width=80` 会把**超宽标量折行**，
    #    而这里只取第一行 —— 长设备名（Realtek/VB-Audio 那类很容易过 80 字符）会被
    #    静默截断，而且截断后仍是**合法 YAML**：值照写、没有任何报错，只有下次启动
    #    时设备选择悄悄回落到「自动检测」。需要加引号的长值更糟：首行是未闭合引号，
    #    整份配置被 `_write_config_text` 拒写（保存静默失效）。
    #    实测：86 字符设备名曾被截成 81 字符（见 tests/test_device_save.py 的长名用例）。
    text = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True,
                          width=10 ** 9)
    first, _, rest = text.partition("\n")
    if rest.strip() not in ("", "..."):
        # 除文档结束标记外还有第二行 = 值里带换行这类 PyYAML 必须折行的写法：
        # 退回「始终单行」的双引号风格（YAML 双引号是 JSON 的超集）。
        return _yaml_quote(value)
    return first.strip()


def write_leaf(path: list[str], value: str, err_label: str,
               *, config_path: "Path", create: bool = False) -> None:
    """把一个叶子值就地写进 config.yaml；失败只留痕，绝不把配置写坏。

    *config_path* 由调用方给（通常是 ``DEFAULT_CONFIG``）；本函数不 import config 模块，
    保持 config_io 的零依赖纪律。
    """
    p = config_path
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
        setter = _yaml_set_or_create if create else _yaml_set_in_text
        text = setter(text, path, value)
        _write_config_text(p, text)
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] {err_label}失败：{exc}", flush=True)
