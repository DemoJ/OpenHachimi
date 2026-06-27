"""config.yaml 解析辅助函数。

集中承载 _load_* / load_config / WebUI 读写引擎共用的底层解析逻辑，
使 loading / persistence / webui_io 三个上层模块只单向依赖本文件，避免相互循环。

本文件同时定义模块级 logger，供 loading / persistence / webui_io 复用——
历史上 logger 未定义即被引用，触发 NameError 而非降级 warning，于此一并修复。
"""

import logging
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)


def _as_mapping(value: object, section_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"config.yaml 中的 {section_name} 必须是对象。")
    return value


def _config_string(section: dict[str, Any], key: str, default: str = "") -> str:
    value = section.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _resolve_config_path(base_dir: Path, value: str, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return base_dir / path


def _config_bool(section: dict[str, Any], key: str, default: bool = False) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _config_int(section: dict[str, Any], key: str, default: int, minimum: int = 1) -> int:
    value = section.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config.yaml 中的 {key} 必须是整数。") from exc
    return max(minimum, parsed)


def _config_string_list(section: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = section.get(key, default)
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(f"config.yaml 中的 {key} 必须是字符串列表或逗号分隔字符串。")


def _string_mapping(value: object, section_name: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        logger.warning("%s 必须是对象，已忽略。", section_name)
        return None

    result = {}
    for key, item in value.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            logger.warning("%s 包含空键，已忽略。", section_name)
            continue
        result[normalized_key] = str(item)
    return result or None


def _config_vision_support(section: dict[str, Any], key: str, default: str = "auto") -> Literal["auto", "true", "false"]:
    value = section.get(key, default)
    if isinstance(value, bool):
        return "true" if value else "false"
    normalized = str(value or default).strip().lower()
    if normalized in {"auto", "true", "false"}:
        return normalized  # type: ignore[return-value]
    if normalized in {"1", "yes", "on"}:
        return "true"
    if normalized in {"0", "no", "off"}:
        return "false"
    raise ValueError(f"config.yaml 中的 {key} 必须是 auto、true 或 false。")


def _config_literal(section: dict[str, Any], key: str, allowed: set[str], default: str) -> str:
    value = _config_string(section, key, default).lower()
    if value not in allowed:
        allowed_text = "、".join(sorted(allowed))
        raise ValueError(f"config.yaml 中的 {key} 必须是以下值之一：{allowed_text}。")
    return value


def _quote_yaml_string(value: str) -> str:
    return f'"{value}"'


def _yaml_safe_dump_to(raw_config: dict[str, Any]) -> str:
    """统一 yaml 整体重写下文本:allow_unicode、保持插入顺序、不排序 key。"""
    return yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False)