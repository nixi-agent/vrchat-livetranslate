"""API 密钥的保存 / 读取 / 打码 / 来源判定。

安全约束：
- 密钥只存在用户目录 %USERPROFILE%/.vrchat-livetranslate/api_key.txt，绝不进仓库。
- 完整密钥绝不出现在日志、状态栏、异常信息里；界面回显一律打码。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from .i18n import t


def _storage_dir() -> Path:
    """密钥存储目录：用户主目录下。可被 _storage_dir_override 替换（测试用）。"""
    return Path.home() / ".vrchat-livetranslate"


_storage_dir_override: Callable[[], Path] | None = None


def _get_storage_dir() -> Path:
    if _storage_dir_override is not None:
        return _storage_dir_override()
    return _storage_dir()


def _key_file() -> Path:
    return _get_storage_dir() / "api_key.txt"


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


def save_api_key(key: str) -> Path:
    """写入用户目录，返回路径。校验失败抛 ValueError（不写文件）。"""
    validated = _validate_key(key)
    d = _get_storage_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _key_file()
    p.write_text(validated, encoding="utf-8")
    return p


def load_saved_key() -> str | None:
    """读出保存的密钥；不存在 / 读失败 / 内容为空 → 返回 None。"""
    p = _key_file()
    try:
        text = p.read_text(encoding="utf-8").strip()
        return text if text else None
    except (OSError, UnicodeDecodeError):
        return None


def clear_saved_key() -> bool:
    """删除保存的密钥文件。返回是否实际删除了文件。"""
    p = _key_file()
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


def key_source() -> tuple[str, str | None]:
    """返回 (来源标签, 打码后的值)。

    来源优先级：界面设置 → 环境变量 DASHSCOPE_API_KEY → 百炼 CLI 配置 → 未配置。
    """
    saved = load_saved_key()
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
