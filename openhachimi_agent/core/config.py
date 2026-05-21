"""应用配置。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


USER_DIR_NAME = "user"
CONFIG_FILE_NAME = "config.yaml"


@dataclass(frozen=True)
class MemoryEmbeddingConfig:
    enabled: bool = True
    model: str = "text-embedding-3-large"
    base_url: str = ""
    api_key: str | None = None
    dimensions: int = 3072
    batch_size: int = 32
    timeout_seconds: int = 30


@dataclass(frozen=True)
class MemoryRecallConfig:
    max_context_tokens: int = 1800
    bm25_top_k: int = 50
    vector_top_k: int = 50
    rrf_k: int = 60
    rerank_top_k: int = 24
    final_l1_top_k: int = 10
    final_l2_top_k: int = 4
    include_l3_profile: bool = True


@dataclass(frozen=True)
class MemoryCaptureConfig:
    enabled: bool = True
    async_enabled: bool = True
    min_turn_chars: int = 20
    extract_timeout_seconds: int = 60


@dataclass(frozen=True)
class MemoryPrivacyConfig:
    pii_redaction: bool = True
    allow_secret_memory: bool = False
    raw_turn_retention_days: int = 180


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    db_path: Path | None = None
    embedding: MemoryEmbeddingConfig = field(default_factory=MemoryEmbeddingConfig)
    recall: MemoryRecallConfig = field(default_factory=MemoryRecallConfig)
    capture: MemoryCaptureConfig = field(default_factory=MemoryCaptureConfig)
    privacy: MemoryPrivacyConfig = field(default_factory=MemoryPrivacyConfig)


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
    memory: MemoryConfig


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


def _load_memory_config(base_dir: Path, raw_config: dict[str, Any], llm_config: dict[str, Any]) -> MemoryConfig:
    memory_config = _as_mapping(raw_config.get("memory"), "memory")
    embedding_config = _as_mapping(memory_config.get("embedding"), "memory.embedding")
    recall_config = _as_mapping(memory_config.get("recall"), "memory.recall")
    capture_config = _as_mapping(memory_config.get("capture"), "memory.capture")
    privacy_config = _as_mapping(memory_config.get("privacy"), "memory.privacy")

    db_path_value = _config_string(memory_config, "db_path", ".memory/long_term_memory.sqlite3")
    db_path = _resolve_config_path(base_dir, db_path_value, base_dir / ".memory" / "long_term_memory.sqlite3")

    return MemoryConfig(
        enabled=_config_bool(memory_config, "enabled", True),
        db_path=db_path,
        embedding=MemoryEmbeddingConfig(
            enabled=_config_bool(embedding_config, "enabled", True),
            model=_config_string(embedding_config, "model", "text-embedding-3-large"),
            base_url=_config_string(embedding_config, "base_url") or _config_string(llm_config, "base_url"),
            api_key=_config_string(embedding_config, "api_key") or _config_string(llm_config, "api_key") or None,
            dimensions=_config_int(embedding_config, "dimensions", 3072),
            batch_size=_config_int(embedding_config, "batch_size", 32),
            timeout_seconds=_config_int(embedding_config, "timeout_seconds", 30),
        ),
        recall=MemoryRecallConfig(
            max_context_tokens=_config_int(recall_config, "max_context_tokens", 1800),
            bm25_top_k=_config_int(recall_config, "bm25_top_k", 50),
            vector_top_k=_config_int(recall_config, "vector_top_k", 50),
            rrf_k=_config_int(recall_config, "rrf_k", 60),
            rerank_top_k=_config_int(recall_config, "rerank_top_k", 24),
            final_l1_top_k=_config_int(recall_config, "final_l1_top_k", 10),
            final_l2_top_k=_config_int(recall_config, "final_l2_top_k", 4),
            include_l3_profile=_config_bool(recall_config, "include_l3_profile", True),
        ),
        capture=MemoryCaptureConfig(
            enabled=_config_bool(capture_config, "enabled", True),
            async_enabled=_config_bool(capture_config, "async_enabled", True),
            min_turn_chars=_config_int(capture_config, "min_turn_chars", 20, minimum=0),
            extract_timeout_seconds=_config_int(capture_config, "extract_timeout_seconds", 60),
        ),
        privacy=MemoryPrivacyConfig(
            pii_redaction=_config_bool(privacy_config, "pii_redaction", True),
            allow_secret_memory=_config_bool(privacy_config, "allow_secret_memory", False),
            raw_turn_retention_days=_config_int(privacy_config, "raw_turn_retention_days", 180),
        ),
    )


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
        memory=_load_memory_config(base_dir, raw_config, llm_config),
    )
