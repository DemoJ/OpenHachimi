"""应用配置。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


USER_DIR_NAME = "user"
CONFIG_FILE_NAME = "config.yaml"


@dataclass(frozen=True)
class AppConfig:
    """集中管理应用运行时配置。"""

    base_dir: Path
    user_dir: Path
    config_path: Path
    roles_dir: Path
    memory_dir: Path
    model_name: str
    openai_base_url: str
    default_role_name: str
    openai_api_key: str | None
    log_dir: Path
    log_level: str
    log_console: bool
    skills_dirs: list[Path]
    browser_headless: bool
    browser_channel: str | None
    browser_user_agent: str | None
    browser_window_size: str | None
    browser_idle_timeout: int
    telegram_bot_token: str | None
    telegram_proxy_url: str | None  # HTTP/SOCKS5 代理地址，例如 socks5://127.0.0.1:1080
    agent_timeout_seconds: int
    stream_idle_timeout_seconds: int


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


def load_config() -> AppConfig:
    """从 user/config.yaml 和项目目录加载配置。"""
    base_dir = Path(__file__).resolve().parents[2]
    user_dir = base_dir / USER_DIR_NAME
    config_path = user_dir / CONFIG_FILE_NAME

    if not config_path.exists():
        raise FileNotFoundError(
            f"未找到配置文件：{config_path}。请复制 user/config.example.yaml 为 user/config.yaml 后填写配置。"
        )

    with config_path.open("r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}

    if not isinstance(raw_config, dict):
        raise ValueError("config.yaml 顶层必须是对象。")

    app_config = _as_mapping(raw_config.get("app"), "app")
    llm_config = _as_mapping(raw_config.get("llm"), "llm")
    paths_config = _as_mapping(raw_config.get("paths"), "paths")
    logging_config = _as_mapping(raw_config.get("logging"), "logging")

    skills_dirs = [user_dir / "skills"]
    external_skills_dir = _config_string(paths_config, "external_skills_dir")
    if external_skills_dir:
        skills_dirs.append(_resolve_config_path(base_dir, external_skills_dir, base_dir))

    return AppConfig(
        base_dir=base_dir,
        user_dir=user_dir,
        config_path=config_path,
        roles_dir=_resolve_config_path(
            base_dir,
            _config_string(paths_config, "roles_dir"),
            user_dir / "roles",
        ),
        memory_dir=_resolve_config_path(
            base_dir,
            _config_string(paths_config, "memory_dir"),
            base_dir / ".memory",
        ),
        model_name=_config_string(llm_config, "model", "gpt-5.2"),
        openai_base_url=_config_string(llm_config, "base_url"),
        default_role_name=_config_string(app_config, "default_role", "default"),
        openai_api_key=_config_string(llm_config, "api_key") or None,
        log_dir=_resolve_config_path(
            base_dir,
            _config_string(logging_config, "dir"),
            base_dir / ".logs",
        ),
        log_level=_config_string(logging_config, "level", "INFO").upper(),
        log_console=_config_bool(logging_config, "console", False),
        skills_dirs=skills_dirs,
        browser_headless=_config_bool(app_config, "browser_headless", True),
        browser_channel=_config_string(app_config, "browser_channel") or None,
        browser_user_agent=_config_string(app_config, "browser_user_agent") or None,
        browser_window_size=_config_string(app_config, "browser_window_size") or None,
        browser_idle_timeout=_config_int(app_config, "browser_idle_timeout", 300, minimum=0),
        telegram_bot_token=_config_string(app_config, "telegram_bot_token") or None,
        telegram_proxy_url=_config_string(app_config, "telegram_proxy_url") or None,
        agent_timeout_seconds=300,
        stream_idle_timeout_seconds=_config_int(app_config, "stream_idle_timeout_seconds", 60),
    )
