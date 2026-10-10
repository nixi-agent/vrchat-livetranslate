"""API 密钥的保存 / 读取 / 打码 / 来源判定。

安全约束：
- 密钥只存在用户目录 %USERPROFILE%/.vrchat-livetranslate/ 下，绝不进仓库。
- 完整密钥绝不出现在日志、状态栏、异常信息里；界面回显一律打码。

**为什么分槽**：千问云与阿里云百炼·国际版是两条互斥线路，两边的 key **不通用**
（账号体系不同）。若共用一个文件，切线路就得重填 key、还会互相覆盖。故按「线路 id」
分槽各存一份：`qianwen` → 老文件名 `api_key.txt`（**保持不动，老用户零迁移**），
其它槽 → `api_key_<slot>.txt`（如 `api_key_qwencloud.txt`）。切线路时按 slot 各取各的。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

from .i18n import t

# 默认槽 = 千问云线路（也是 endpoints.DEFAULT_PROVIDER）。老调用不传 slot 就落到这里，
# 文件名仍是 api_key.txt，故**升级不改变任何老用户的既有行为**。
SLOT_QIANWEN = "qianwen"

# 槽名白名单：槽名会被拼进文件名（api_key_<slot>.txt），必须挡掉路径分隔符 / `..`，
# 否则一个恶意槽名就能拼出目录穿越的文件名。只放行 [a-z0-9_]（线路 id 本就长这样）。
_SLOT_RE = re.compile(r"[a-z0-9_]+")


def _storage_dir() -> Path:
    """密钥存储目录：用户主目录下。可被 _storage_dir_override 替换（测试用）。"""
    return Path.home() / ".vrchat-livetranslate"


_storage_dir_override: Callable[[], Path] | None = None


def _get_storage_dir() -> Path:
    if _storage_dir_override is not None:
        return _storage_dir_override()
    return _storage_dir()


def _validate_slot(slot: str) -> str:
    """校验并返回规范槽名；非法（含路径穿越风险的字符）→ 抛 ValueError。"""
    s = str(slot or "").strip()
    if not _SLOT_RE.fullmatch(s):
        raise ValueError(f"非法的密钥槽名：{slot!r}（只允许小写字母 / 数字 / 下划线）")
    return s


def _key_file(slot: str = SLOT_QIANWEN) -> Path:
    """槽 → 密钥文件路径。qianwen 用老名字，其它槽加后缀（见模块 docstring）。"""
    s = _validate_slot(slot)
    name = "api_key.txt" if s == SLOT_QIANWEN else f"api_key_{s}.txt"
    return _get_storage_dir() / name


def _validate_key(key: str) -> str:
    """校验密钥格式。通过返回 strip 后的值，不通过抛 ValueError。"""
    stripped = key.strip()
    if not stripped:
        raise ValueError(t("密钥不能为空"))
    if any(c.isspace() for c in stripped):
        raise ValueError(t("密钥不能包含空白字符"))
    if len(stripped) < 16:
        raise ValueError(t("密钥长度不足（{n} < 16）", n=len(stripped)))
    return stripped


def save_api_key(key: str, slot: str = SLOT_QIANWEN) -> Path:
    """写入用户目录（按 slot 分槽），返回路径。校验失败抛 ValueError（不写文件）。"""
    validated = _validate_key(key)
    d = _get_storage_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = _key_file(slot)
    if os.name == 'posix':
        d.chmod(0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=d, delete=False) as file:
            temporary = Path(file.name)
            file.write(validated)
        if os.name == 'posix':
            temporary.chmod(0o600)
        os.replace(temporary, p)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return p


def load_saved_key(slot: str = SLOT_QIANWEN) -> str | None:
    """读出某槽保存的密钥；不存在 / 读失败 / 内容为空 → 返回 None。"""
    p = _key_file(slot)
    try:
        text = p.read_text(encoding="utf-8").strip()
        return text if text else None
    except (OSError, UnicodeDecodeError):
        return None


def clear_saved_key(slot: str = SLOT_QIANWEN) -> bool:
    """删除某槽保存的密钥文件。返回是否实际删除了文件（只清这一个槽，不动别的）。"""
    p = _key_file(slot)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def mask_key(key: str) -> str:
    """打码：sk-****3f7a（51 字符）。

    规则：保留前 3 字符 + **** + 末 4 字符 + 总长度。
    短密钥（< 10 字符）也只露前 2 + **** + 末 1，绝不整串露出。
    """
    n = len(key)
    if n <= 3:
        return t("{head}****（{n} 字符）", head=key[:1], n=n)
    if n <= 7:
        return t("{head}****{tail}（{n} 字符）", head=key[:2], tail=key[-1], n=n)
    return t("{head}****{tail}（{n} 字符）", head=key[:3], tail=key[-4:], n=n)


def key_source(slot: str = SLOT_QIANWEN) -> tuple[str, str | None]:
    """返回 (来源标签, 打码后的值)。

    来源优先级：界面设置（按 slot 分槽）→ 环境变量 DASHSCOPE_API_KEY → 百炼 CLI 配置 → 未配置。
    ⚠️ 后两条兜底**两条线路共用**（阿里官方对国际站也用同一个环境变量名 / 同一份 CLI 配置），
    故不按 slot 区分；只有「界面设置」这一条按 slot 取对应线路那份。
    """
    saved = load_saved_key(slot)
    if saved:
        return (t("界面设置"), mask_key(saved))

    env = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if env:
        return (t("环境变量 DASHSCOPE_API_KEY"), mask_key(env))

    cfg = Path(os.path.expanduser("~/.bailian/config.json"))
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            for k in ("api_key", "apiKey", "DASHSCOPE_API_KEY"):
                v = str(data.get(k) or "").strip()
                if v:
                    return (t("百炼 CLI 配置"), mask_key(v))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    return (t("未配置"), None)
