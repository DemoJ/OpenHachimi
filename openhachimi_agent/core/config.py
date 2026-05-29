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
class MemorySchedulerConfig:
    enabled: bool = True
    poll_interval_seconds: int = 2
    batch_size: int = 10
    lock_seconds: int = 300


@dataclass(frozen=True)
class MemoryConsolidationConfig:
    enabled: bool = True
    atom_limit: int = 200
    block_limit: int = 50
    min_atom_confidence: float = 0.55
    min_block_atoms: int = 2


@dataclass(frozen=True)
class MemoryVectorConfig:
    backend: str = "shard"
    shard_top_dims: int = 4
    candidate_multiplier: int = 20
    min_bruteforce_rows: int = 200


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    db_path: Path | None = None
    embedding: MemoryEmbeddingConfig = field(default_factory=MemoryEmbeddingConfig)
    recall: MemoryRecallConfig = field(default_factory=MemoryRecallConfig)
    capture: MemoryCaptureConfig = field(default_factory=MemoryCaptureConfig)
    privacy: MemoryPrivacyConfig = field(default_factory=MemoryPrivacyConfig)
    scheduler: MemorySchedulerConfig = field(default_factory=MemorySchedulerConfig)
    consolidation: MemoryConsolidationConfig = field(default_factory=MemoryConsolidationConfig)
    vector: MemoryVectorConfig = field(default_factory=MemoryVectorConfig)


@dataclass(frozen=True)
class SchedulerDeliveryConfig:
    default_mode: str = "origin"
    fallback_to_inbox: bool = True
    home_targets: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SchedulerSecurityConfig:
    prompt_scan_enabled: bool = True
    allow_scheduler_mutation_in_scheduled_runs: bool = False
    allow_interactive_tools_in_scheduled_runs: bool = False


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = True
    db_path: Path | None = None
    poll_interval_seconds: int = 60
    max_concurrency: int = 2
    default_timeout_seconds: int = 300
    claim_lock_seconds: int = 600
    delivery: SchedulerDeliveryConfig = field(default_factory=SchedulerDeliveryConfig)
    security: SchedulerSecurityConfig = field(default_factory=SchedulerSecurityConfig)


@dataclass(frozen=True)
class ResearchConfig:
    enabled_backends: list[str] = field(default_factory=lambda: ["duckduckgo"])
    brave_api_key: str | None = None
    tavily_api_key: str | None = None
    search_timeout_seconds: int = 15
    max_backend_results: int = 10
    min_independent_sources: int = 3
    require_citations: bool = True
    browser_fallback_enabled: bool = True


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
    show_tool_calls: bool
    attachments_dir: Path
    max_attachment_size_bytes: int
    allowed_attachment_mime_types: list[str]
    agent_timeout_seconds: int
    stream_idle_timeout_seconds: int
    memory: MemoryConfig
    scheduler: SchedulerConfig
    research: ResearchConfig


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


def _load_memory_config(base_dir: Path, raw_config: dict[str, Any], llm_config: dict[str, Any]) -> MemoryConfig:
    memory_config = _as_mapping(raw_config.get("memory"), "memory")
    embedding_config = _as_mapping(memory_config.get("embedding"), "memory.embedding")
    recall_config = _as_mapping(memory_config.get("recall"), "memory.recall")
    capture_config = _as_mapping(memory_config.get("capture"), "memory.capture")
    privacy_config = _as_mapping(memory_config.get("privacy"), "memory.privacy")
    scheduler_config = _as_mapping(memory_config.get("scheduler"), "memory.scheduler")
    consolidation_config = _as_mapping(memory_config.get("consolidation"), "memory.consolidation")
    vector_config = _as_mapping(memory_config.get("vector"), "memory.vector")

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
        scheduler=MemorySchedulerConfig(
            enabled=_config_bool(scheduler_config, "enabled", True),
            poll_interval_seconds=_config_int(scheduler_config, "poll_interval_seconds", 2),
            batch_size=_config_int(scheduler_config, "batch_size", 10),
            lock_seconds=_config_int(scheduler_config, "lock_seconds", 300),
        ),
        consolidation=MemoryConsolidationConfig(
            enabled=_config_bool(consolidation_config, "enabled", True),
            atom_limit=_config_int(consolidation_config, "atom_limit", 200),
            block_limit=_config_int(consolidation_config, "block_limit", 50),
            min_atom_confidence=float(consolidation_config.get("min_atom_confidence", 0.55)),
            min_block_atoms=_config_int(consolidation_config, "min_block_atoms", 2),
        ),
        vector=MemoryVectorConfig(
            backend=_config_string(vector_config, "backend", "shard"),
            shard_top_dims=_config_int(vector_config, "shard_top_dims", 4),
            candidate_multiplier=_config_int(vector_config, "candidate_multiplier", 20),
            min_bruteforce_rows=_config_int(vector_config, "min_bruteforce_rows", 200),
        ),
    )


def _load_scheduler_config(base_dir: Path, raw_config: dict[str, Any]) -> SchedulerConfig:
    scheduler_config = _as_mapping(raw_config.get("scheduler"), "scheduler")
    db_path_value = _config_string(scheduler_config, "db_path", ".scheduler/tasks.sqlite3")
    delivery_config = _as_mapping(scheduler_config.get("delivery"), "scheduler.delivery")
    security_config = _as_mapping(scheduler_config.get("security"), "scheduler.security")
    home_targets = delivery_config.get("home_targets", [])
    if not isinstance(home_targets, list):
        raise ValueError("config.yaml 中的 scheduler.delivery.home_targets 必须是列表。")
    return SchedulerConfig(
        enabled=_config_bool(scheduler_config, "enabled", True),
        db_path=_resolve_config_path(base_dir, db_path_value, base_dir / ".scheduler" / "tasks.sqlite3"),
        poll_interval_seconds=_config_int(scheduler_config, "poll_interval_seconds", 60),
        max_concurrency=_config_int(scheduler_config, "max_concurrency", 2),
        default_timeout_seconds=_config_int(scheduler_config, "default_timeout_seconds", 300),
        claim_lock_seconds=_config_int(scheduler_config, "claim_lock_seconds", 600),
        delivery=SchedulerDeliveryConfig(
            default_mode=_config_string(delivery_config, "default_mode", "origin"),
            fallback_to_inbox=_config_bool(delivery_config, "fallback_to_inbox", True),
            home_targets=[item for item in home_targets if isinstance(item, dict)],
        ),
        security=SchedulerSecurityConfig(
            prompt_scan_enabled=_config_bool(security_config, "prompt_scan_enabled", True),
            allow_scheduler_mutation_in_scheduled_runs=_config_bool(security_config, "allow_scheduler_mutation_in_scheduled_runs", False),
            allow_interactive_tools_in_scheduled_runs=_config_bool(security_config, "allow_interactive_tools_in_scheduled_runs", False),
        ),
    )


def _load_research_config(raw_config: dict[str, Any]) -> ResearchConfig:
    research_config = _as_mapping(raw_config.get("research"), "research")
    enabled_backends = _config_string_list(research_config, "enabled_backends", ["duckduckgo"])
    return ResearchConfig(
        enabled_backends=enabled_backends or ["duckduckgo"],
        brave_api_key=_config_string(research_config, "brave_api_key") or None,
        tavily_api_key=_config_string(research_config, "tavily_api_key") or None,
        search_timeout_seconds=min(60, _config_int(research_config, "search_timeout_seconds", 15)),
        max_backend_results=min(50, _config_int(research_config, "max_backend_results", 10)),
        min_independent_sources=min(20, _config_int(research_config, "min_independent_sources", 3)),
        require_citations=_config_bool(research_config, "require_citations", True),
        browser_fallback_enabled=_config_bool(research_config, "browser_fallback_enabled", True),
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
        show_tool_calls=_config_bool(app_config, "show_tool_calls", True),
        attachments_dir=_resolve_config_path(
            base_dir,
            _config_string(paths_config, "attachments_dir", ".tmp/attachments"),
            base_dir / ".tmp" / "attachments",
        ),
        max_attachment_size_bytes=_config_int(app_config, "max_attachment_size_mb", 50) * 1024 * 1024,
        allowed_attachment_mime_types=_config_string_list(
            app_config,
            "allowed_attachment_mime_types",
            [],
        ),
        agent_timeout_seconds=300,
        stream_idle_timeout_seconds=_config_int(app_config, "stream_idle_timeout_seconds", 60),
        memory=_load_memory_config(base_dir, raw_config, llm_config),
        scheduler=_load_scheduler_config(base_dir, raw_config),
        research=_load_research_config(raw_config),
    )
