"""应用配置。"""

import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from openhachimi_agent.content.prompts import load_system_prompt


USER_DIR_NAME = "user"
CONFIG_FILE_NAME = "config.yaml"
DEFAULT_VISION_PROMPT = load_system_prompt("vision/default_user")


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
class VisionConfig:
    enabled: bool = True
    fallback_enabled: bool = True
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    detail: Literal["auto", "low", "high"] = "auto"
    prompt: str = DEFAULT_VISION_PROMPT
    max_images_per_message: int = 4
    max_image_size_bytes: int = 10 * 1024 * 1024


@dataclass(frozen=True)
class MCPServerConfig:
    type: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None


@dataclass(frozen=True)
class MCPConfig:
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSummaryConfig:
    """摘要压缩用的辅助模型配置;留空时使用主模型。"""

    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    # 摘要输出的 token 上限。结构化摘要通常 1-3K token,4096 留足余量。
    max_tokens: int = 4096
    # 摘要失败时:False=插入确定性兜底摘要,True=中止压缩(冻结对话)
    abort_on_failure: bool = False


@dataclass(frozen=True)
class ContextConfig:
    """对话历史上下文压缩配置。

    阈值均为相对模型上下文窗口的比例:
      - threshold_percent: 轮后主压缩触发线(真实 input_tokens 用量)
      - hard_ceiling_percent: 轮内预检触发线(粗略估计,防单轮内爆窗口)
      - context_length: 模型上下文窗口大小,单位 K(128=128K tokens)
    """

    enabled: bool = True
    engine: str = "compressor"  # 预留可插拔引擎
    threshold_percent: float = 0.75
    hard_ceiling_percent: float = 0.90
    protect_first_n: int = 3
    protect_last_n: int = 20
    tail_token_budget: int = 20000
    anti_thrash: bool = True
    min_savings_pct: int = 10
    rescue_to_memory: bool = True  # on_pre_compress 抢救丢弃窗口到记忆库
    # 模型上下文窗口大小,单位 K(128 表示 128K tokens)。用于计算压缩触发阈值。
    # 0 表示用内置默认(128K)。非 128K 的模型需手动填写真实窗口。
    context_length: int = 128
    summary: ContextSummaryConfig = field(default_factory=ContextSummaryConfig)


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
    llm_supports_vision: Literal["auto", "true", "false"]
    log_dir: Path
    log_level: str
    log_console: bool
    skills_dirs: list[Path]
    browser_headless: bool
    browser_channel: str | None
    browser_user_agent: str | None
    browser_window_size: str | None
    browser_idle_timeout: int
    browser_cdp_wait_seconds: int
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
    vision: VisionConfig
    mcp: MCPConfig = field(default_factory=MCPConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    http_api_token: str | None = None
    server_host: str = "127.0.0.1"   # HTTP 服务监听地址；127.0.0.1=仅本机，0.0.0.0=开放局域网/公网访问
    server_port: int = 8765           # HTTP 服务监听端口


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


def _replace_or_insert_app_kv(
    config_path: Path, key: str, value: str, raw_config: dict[str, Any], *, quote: bool = True
) -> None:
    """在 config.yaml 的 app 段替换或插入一个 key: value 行，保留原有注释/缩进/行尾。

    用于将 deploy --host/--port 等命令行参数持久化回配置文件，使改配置后 restart 即生效。
    quote=False 时按裸值写入（适用于数字）。失败时回退到 yaml.safe_dump 重写整个文件。
    """
    formatted = _quote_yaml_string(value) if quote else str(value)
    try:
        text = config_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        app_index = next((idx for idx, line in enumerate(lines) if line.strip() == "app:"), None)
        if app_index is None:
            raise ValueError("config.yaml 缺少 app 配置段")

        app_indent = len(lines[app_index]) - len(lines[app_index].lstrip())
        insert_index = app_index + 1
        key_index = None
        for idx in range(app_index + 1, len(lines)):
            stripped = lines[idx].strip()
            if stripped and not stripped.startswith("#"):
                indent = len(lines[idx]) - len(lines[idx].lstrip())
                if indent <= app_indent:
                    break
            if stripped.startswith(f"{key}:"):
                key_index = idx
                break
            insert_index = idx + 1

        new_line = f"{' ' * (app_indent + 2)}{key}: {formatted}\n"
        if key_index is not None:
            line = lines[key_index]
            line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            comment = ""
            before_comment = line.rstrip("\r\n")
            if "#" in before_comment:
                comment = " " + before_comment[before_comment.index("#"):].strip()
            lines[key_index] = f"{' ' * (app_indent + 2)}{key}: {formatted}{comment}{line_ending}"
        else:
            lines.insert(insert_index, new_line)
        config_path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        raw_config.setdefault("app", {})[key] = value
        config_path.write_text(yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _replace_or_insert_http_api_token(config_path: Path, token: str, raw_config: dict[str, Any]) -> None:
    _replace_or_insert_app_kv(config_path, "http_api_token", token, raw_config)


def persist_server_endpoint(config_path: Path, raw_config: dict[str, Any], host: str, port: int) -> None:
    """将 host/port 写回配置文件 app.server_host / app.server_port。

    deploy --host/--port 透传至此，使命令行参数持久化，后续 restart 读配置即生效。
    """
    _replace_or_insert_app_kv(config_path, "server_host", host, raw_config)
    _replace_or_insert_app_kv(config_path, "server_port", str(port), raw_config, quote=False)
    raw_config.setdefault("app", {})["server_host"] = host
    raw_config.setdefault("app", {})["server_port"] = port


def _ensure_http_api_token(config_path: Path, raw_config: dict[str, Any]) -> str:
    app_config = _as_mapping(raw_config.get("app"), "app")
    token = _config_string(app_config, "http_api_token")
    if token:
        return token

    token = secrets.token_urlsafe(32)
    raw_config["app"] = app_config
    app_config["http_api_token"] = token
    try:
        _replace_or_insert_http_api_token(config_path, token, raw_config)
    except OSError as exc:
        raise OSError(
            f"无法写入 HTTP API Token 到配置文件：{config_path}。请修复文件权限或手动添加 app.http_api_token。"
        ) from exc
    return token


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


def _load_vision_config(raw_config: dict[str, Any], llm_config: dict[str, Any]) -> VisionConfig:
    vision_config = _as_mapping(raw_config.get("vision"), "vision")
    prompt = _config_string(vision_config, "prompt", VisionConfig.prompt)
    return VisionConfig(
        enabled=_config_bool(vision_config, "enabled", True),
        fallback_enabled=_config_bool(vision_config, "fallback_enabled", True),
        model=_config_string(vision_config, "model"),
        base_url=_config_string(vision_config, "base_url") or _config_string(llm_config, "base_url"),
        api_key=_config_string(vision_config, "api_key") or _config_string(llm_config, "api_key") or None,
        detail=_config_literal(vision_config, "detail", {"auto", "low", "high"}, "auto"),  # type: ignore[arg-type]
        prompt=prompt or VisionConfig.prompt,
        max_images_per_message=_config_int(vision_config, "max_images_per_message", 4),
        max_image_size_bytes=_config_int(vision_config, "max_image_size_mb", 10) * 1024 * 1024,
    )


def load_mcp_config(user_dir: Path) -> MCPConfig:
    mcp_file = user_dir / "mcp-servers.json"
    servers = {}
    if mcp_file.exists():
        try:
            with open(mcp_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            servers_config = data.get("mcpServers", {})
            for name, srv_conf in servers_config.items():
                if not isinstance(srv_conf, dict):
                    continue

                if "command" in srv_conf:
                    servers[name] = MCPServerConfig(
                        type="stdio",
                        command=srv_conf.get("command"),
                        args=srv_conf.get("args", []),
                        url=None,
                        env=_string_mapping(srv_conf.get("env"), f"mcpServers.{name}.env")
                    )
                elif "url" in srv_conf:
                    servers[name] = MCPServerConfig(
                        type="http",
                        command=None,
                        args=[],
                        url=srv_conf.get("url"),
                        env=None,
                        headers=_string_mapping(srv_conf.get("headers"), f"mcpServers.{name}.headers")
                    )
        except Exception as exc:
            logger.warning("Failed to parse mcp-servers.json: %s", exc)

    return MCPConfig(servers=servers)


def _load_mcp_config(user_dir: Path) -> MCPConfig:
    return load_mcp_config(user_dir)


def _load_context_config(raw_config: dict[str, Any], llm_config: dict[str, Any]) -> ContextConfig:
    context_config = _as_mapping(raw_config.get("context"), "context")
    summary_config = _as_mapping(context_config.get("summary"), "context.summary")

    def _config_float(section: dict[str, Any], key: str, default: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
        value = section.get(key, default)
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config.yaml 中的 {key} 必须是数值。") from exc
        return max(lo, min(hi, parsed))

    return ContextConfig(
        enabled=_config_bool(context_config, "enabled", True),
        engine=_config_string(context_config, "engine", "compressor") or "compressor",
        threshold_percent=_config_float(context_config, "threshold_percent", 0.75, lo=0.1, hi=0.95),
        hard_ceiling_percent=_config_float(context_config, "hard_ceiling_percent", 0.90, lo=0.2, hi=0.99),
        protect_first_n=_config_int(context_config, "protect_first_n", 3, minimum=0),
        protect_last_n=_config_int(context_config, "protect_last_n", 20, minimum=0),
        tail_token_budget=_config_int(context_config, "tail_token_budget", 20000, minimum=0),
        anti_thrash=_config_bool(context_config, "anti_thrash", True),
        min_savings_pct=_config_int(context_config, "min_savings_pct", 10, minimum=0),
        rescue_to_memory=_config_bool(context_config, "rescue_to_memory", True),
        context_length=_config_int(context_config, "context_length", 128, minimum=0),
        summary=ContextSummaryConfig(
            model=_config_string(summary_config, "model"),
            base_url=_config_string(summary_config, "base_url") or _config_string(llm_config, "base_url"),
            api_key=_config_string(summary_config, "api_key") or _config_string(llm_config, "api_key") or None,
            max_tokens=_config_int(summary_config, "max_tokens", 4096, minimum=256),
            abort_on_failure=_config_bool(summary_config, "abort_on_failure", False),
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

    http_api_token = _ensure_http_api_token(config_path, raw_config)

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
        llm_supports_vision=_config_vision_support(llm_config, "supports_vision", "auto"),
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
        browser_cdp_wait_seconds=_config_int(app_config, "browser_cdp_wait_seconds", 45, minimum=5),
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
        http_api_token=http_api_token,
        server_host=_config_string(app_config, "server_host", "127.0.0.1") or "127.0.0.1",
        server_port=_config_int(app_config, "server_port", 8765, minimum=1),
        agent_timeout_seconds=300,
        stream_idle_timeout_seconds=_config_int(app_config, "stream_idle_timeout_seconds", 60),
        memory=_load_memory_config(base_dir, raw_config, llm_config),
        scheduler=_load_scheduler_config(base_dir, raw_config),
        research=_load_research_config(raw_config),
        vision=_load_vision_config(raw_config, llm_config),
        mcp=_load_mcp_config(user_dir),
        context=_load_context_config(raw_config, llm_config),
    )


# ================================================================ WebUI 设置页配置读写
# 直接读写 config.yaml 原始内容(不走 AppConfig),保留注释/缩进/单位语义。
# 泛化写回函数支持任意 section 路径,与 _replace_or_insert_app_kv 同思路;
# 失败时回退到 yaml.safe_dump 重写整个文件(丢注释但保证可用)。

CONFIG_KIND_SECRET = "secret"
CONFIG_KIND_STRING = "string"
CONFIG_KIND_SELECT = "select"
CONFIG_KIND_BOOL = "bool"
CONFIG_KIND_INT = "int"
CONFIG_KIND_FLOAT = "float"  # 浮点(如压缩阈值比例),前端复用 number 输入
CONFIG_KIND_MULTI = "multi"  # 字符串列表(多选);value 为 list[str],items 须在 options 内

# AI 模型设置页字段定义。path 为 yaml 中的 dotted 路径;kind 决定控件类型与写回格式;
# group 用于前端分组渲染;label/description 为 UI 文案。后续新增设置分组时扩展此表即可。
AI_MODEL_FIELDS: list[dict[str, Any]] = [
    {"path": "llm.api_key", "kind": CONFIG_KIND_SECRET, "group": "llm",
     "label": "API Key", "description": "OpenAI 兼容服务的访问密钥"},
    {"path": "llm.model", "kind": CONFIG_KIND_STRING, "group": "llm",
     "label": "模型", "description": "主模型名称,如 gpt-5.2 / hachimi"},
    {"path": "llm.base_url", "kind": CONFIG_KIND_STRING, "group": "llm",
     "label": "Base URL", "description": "OpenAI 兼容服务地址,如 https://api.openai.com/v1"},
    {"path": "llm.supports_vision", "kind": CONFIG_KIND_SELECT, "group": "llm",
     "label": "图片支持", "options": ["auto", "true", "false"],
     "description": "auto 自动按模型名判断;true 强制直传图片;false 使用视觉辅助模型"},
    {"path": "vision.enabled", "kind": CONFIG_KIND_BOOL, "group": "vision",
     "label": "启用图片处理", "description": "是否处理图片附件"},
    {"path": "vision.fallback_enabled", "kind": CONFIG_KIND_BOOL, "group": "vision",
     "label": "回退视觉模型", "description": "主模型不支持图片时,调用辅助视觉模型识别后再交给主模型"},
    {"path": "vision.model", "kind": CONFIG_KIND_STRING, "group": "vision",
     "label": "视觉模型", "description": "辅助视觉模型名称;留空且主模型不支持图片时不识别"},
    {"path": "vision.base_url", "kind": CONFIG_KIND_STRING, "group": "vision",
     "label": "Base URL", "description": "留空则复用主模型 base_url"},
    {"path": "vision.api_key", "kind": CONFIG_KIND_SECRET, "group": "vision",
     "label": "API Key", "description": "留空则复用主模型 api_key"},
    {"path": "vision.detail", "kind": CONFIG_KIND_SELECT, "group": "vision",
     "label": "图片精度", "options": ["auto", "low", "high"],
     "description": "OpenAI image_url.detail"},
    {"path": "vision.max_images_per_message", "kind": CONFIG_KIND_INT, "group": "vision",
     "label": "单消息最多图片", "description": "单条消息附带图片数量上限"},
    {"path": "vision.max_image_size_mb", "kind": CONFIG_KIND_INT, "group": "vision",
     "label": "单图大小上限 (MB)", "description": "单张图片大小上限,单位 MB"},
    {"path": "context.summary.model", "kind": CONFIG_KIND_STRING, "group": "summary",
     "label": "摘要模型", "description": "上下文压缩用的辅助模型;留空则使用主模型"},
    {"path": "context.summary.base_url", "kind": CONFIG_KIND_STRING, "group": "summary",
     "label": "Base URL", "description": "留空则复用主模型 base_url"},
    {"path": "context.summary.api_key", "kind": CONFIG_KIND_SECRET, "group": "summary",
     "label": "API Key", "description": "留空则复用主模型 api_key"},
    {"path": "context.summary.max_tokens", "kind": CONFIG_KIND_INT, "group": "summary",
     "label": "摘要 token 上限", "description": "摘要输出 token 上限;结构化摘要通常 1-3K"},
    {"path": "context.summary.abort_on_failure", "kind": CONFIG_KIND_BOOL, "group": "summary",
     "label": "失败即中止", "description": "关闭=插入兜底摘要;开启=中止压缩并冻结对话"},
]

# 网络与服务接入设置页字段定义。
# 改 server_host / server_port / http_api_token / telegram_bot_token / telegram_proxy_url
# 需重启进程才生效(端口/监听地址在启动期绑定,bot 在启动期建连);消息行为三项即时生效。
# server_host 用 select 限定 127.0.0.1 / 0.0.0.0,防止误下拉成公网监听;
# 若需绑定特定网卡 IP(如 192.168.1.10),请直接编辑 config.yaml——下拉刻意不提供该入口。
NETWORK_FIELDS: list[dict[str, Any]] = [
    # ── HTTP 服务(改后需重启) ──
    {"path": "app.server_host", "kind": CONFIG_KIND_SELECT, "group": "http",
     "label": "监听地址", "options": ["127.0.0.1", "0.0.0.0"],
     "description": "127.0.0.1=仅本机访问(最安全);0.0.0.0=开放局域网/公网,务必配合 token 与防火墙。改后需重启。如需绑定特定网卡 IP 请直接编辑 config.yaml"},
    {"path": "app.server_port", "kind": CONFIG_KIND_INT, "group": "http",
     "label": "监听端口", "description": "HTTP 服务监听端口;改后需重启才生效"},
    {"path": "app.http_api_token", "kind": CONFIG_KIND_SECRET, "group": "http",
     "label": "HTTP API Token", "description": "除 /health 外所有接口的访问令牌;改后需重启,且前端需用新 token 重新登录"},
    # ── Telegram(改后需重启) ──
    {"path": "app.telegram_bot_token", "kind": CONFIG_KIND_SECRET, "group": "telegram",
     "label": "Bot Token", "description": "通过 @BotFather 申请;留空不启用 Telegram 渠道。改后需重启(bot 启动期建连)"},
    {"path": "app.telegram_proxy_url", "kind": CONFIG_KIND_STRING, "group": "telegram",
     "label": "代理地址", "description": "无法直连 Telegram 时配置;支持 HTTP/SOCKS5,如 socks5://127.0.0.1:1080。改后需重启"},
    # ── 消息行为(即时生效,无需重启) ──
    {"path": "app.show_tool_calls", "kind": CONFIG_KIND_BOOL, "group": "behavior",
     "label": "显示工具调用", "description": "在 CLI/HTTP/Telegram 渠道显示工具调用进度"},
    {"path": "app.stream_idle_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "behavior",
     "label": "流式空闲检查间隔(秒)", "description": "流式队列无新事件时的进展检查/心跳间隔秒数"},
    {"path": "app.max_attachment_size_mb", "kind": CONFIG_KIND_INT, "group": "behavior",
     "label": "附件大小上限 (MB)", "description": "Telegram/HTTP 附件大小上限,单位 MB"},
]

# 浏览器自动化设置页字段定义。
# 全部位于 app 段;除 idle_timeout 设 0=不自动关闭外,均基本热生效——
# 改动在"下次启动浏览器实例时"生效(浏览器按需懒启动,故无需重启进程,当前实例不重启浏览器)。
# browser_channel 用 select + editable:下拉给 chrome/chromium/msedge 预设,
# 又允许填绝对路径(如 C:\\Program Files\\...或 /usr/bin/google-chrome)。
# editable select 跳过后端白名单校验,由浏览器启动逻辑自行兜底。
BROWSER_FIELDS: list[dict[str, Any]] = [
    {"path": "app.browser_headless", "kind": CONFIG_KIND_BOOL, "group": "instance",
     "label": "无头模式", "description": "开启=不显示浏览器窗口(服务器/无桌面环境推荐);关闭=显示窗口。下次启动浏览器实例时生效"},
    {"path": "app.browser_channel", "kind": CONFIG_KIND_SELECT, "group": "instance",
     "label": "浏览器通道", "options": ["chrome", "chromium", "msedge"], "editable": True,
     "description": "留空=用内置浏览器;可选 chrome/chromium/msedge 预设,或直接填可执行文件绝对路径。下次启动浏览器实例时生效"},
    {"path": "app.browser_user_agent", "kind": CONFIG_KIND_STRING, "group": "instance",
     "label": "User-Agent", "description": "自定义 User-Agent;留空则自动隐藏 Headless 特征。下次启动浏览器实例时生效"},
    {"path": "app.browser_window_size", "kind": CONFIG_KIND_STRING, "group": "instance",
     "label": "窗口尺寸", "description": "形如 1920,1080;留空则随机生成常见尺寸。下次启动浏览器实例时生效"},
    {"path": "app.browser_idle_timeout", "kind": CONFIG_KIND_INT, "group": "instance",
     "label": "空闲自动关闭(秒)", "description": "浏览器空闲多少秒后自动关闭以释放内存;0=不自动关闭。下次启动浏览器实例时生效"},
    {"path": "app.browser_cdp_wait_seconds", "kind": CONFIG_KIND_INT, "group": "instance",
     "label": "CDP 就绪等待(秒)", "description": "等待 Chrome CDP 调试端口就绪的最大秒数;冷启动慢或端口冲突时可调高。下次启动浏览器实例时生效"},
]

# 记忆系统设置页字段定义(功能域最复杂,前端在「记忆」页内再分 5 个子卡片组)。
# 用 group 字段划分子组:memory-general / -embedding / -recall / -capture / -privacy。
# memory.embedding.api_key 为 secret(留空回退复用 llm.api_key,与 summary 辅助模型一致)。
# 切换 总开关 enabled / 改 db_path / 改 embedding.* 建议重启进程(DB/embedding client 在启动期初始化);
# recall / capture / privacy 多为检索与捕获策略,基本在下次检索/捕获时生效,记为热生效。
# recall 全组在前端默认折叠并标注「高级调参,非必要勿改」——由 card 元数据(collapsible/defaultCollapsed)驱动,
# 字段表本身只负责 data。
MEMORY_FIELDS: list[dict[str, Any]] = [
    # ── 总开关 ──
    {"path": "memory.enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-general",
     "label": "启用记忆系统", "description": "关闭则完全停用长期记忆写入与召回。改后建议重启进程才彻底停用后台捕获/调度"},
    {"path": "memory.db_path", "kind": CONFIG_KIND_STRING, "group": "memory-general",
     "label": "数据库路径", "description": "长期记忆 SQLite 路径(相对路径以 user 目录为根)。改后建议重启,且不会自动迁移已有数据"},
    # ── Embedding 向量化 ──
    {"path": "memory.embedding.enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-embedding",
     "label": "启用 Embedding", "description": "关闭则只用 BM25 关键词召回,不做向量检索。改后建议重启进程"},
    {"path": "memory.embedding.model", "kind": CONFIG_KIND_STRING, "group": "memory-embedding",
     "label": "Embedding 模型", "description": "向量化模型名,默认 text-embedding-3-large。改模型后已存向量不再兼容,需重建记忆库"},
    {"path": "memory.embedding.base_url", "kind": CONFIG_KIND_STRING, "group": "memory-embedding",
     "label": "Base URL", "description": "Embedding 服务地址;留空则复用 llm.base_url。改后建议重启进程"},
    {"path": "memory.embedding.api_key", "kind": CONFIG_KIND_SECRET, "group": "memory-embedding",
     "label": "API Key", "description": "Embedding 服务密钥;留空则复用 llm.api_key。改后建议重启进程"},
    {"path": "memory.embedding.dimensions", "kind": CONFIG_KIND_INT, "group": "memory-embedding",
     "label": "向量维度", "description": "需与模型输出维度一致(默认 3072)。改后须重建记忆库,旧向量不再兼容"},
    {"path": "memory.embedding.batch_size", "kind": CONFIG_KIND_INT, "group": "memory-embedding",
     "label": "批大小", "description": "单次向量化请求的文本条数;过大可能超时/被限流。改后下次批处理生效"},
    {"path": "memory.embedding.timeout_seconds", "kind": CONFIG_KIND_INT, "group": "memory-embedding",
     "label": "超时(秒)", "description": "单次 Embedding 请求超时秒数。改后下次请求生效"},
    # ── 召回检索(高级调参,前端默认折叠) ──
    {"path": "memory.recall.max_context_tokens", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "上下文 token 上限", "description": "召回内容注入对话的最大 token 预算。下次召回生效"},
    {"path": "memory.recall.bm25_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "BM25 top_k", "description": "关键词召回候选数;从 BM25 取前 K 条。下次召回生效"},
    {"path": "memory.recall.vector_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "向量 top_k", "description": "向量召回候选数;从向量索引取前 K 条。下次召回生效"},
    {"path": "memory.recall.rrf_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "RRF k", "description": "RRF 融合平滑常数(通常 60);K 越大排名越平滑。下次召回生效"},
    {"path": "memory.recall.rerank_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "Rerank top_k", "description": "RRF 融合后送入重排的候选数。下次召回生效"},
    {"path": "memory.recall.final_l1_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "L1 终筛 top_k", "description": "重排后一级终筛保留数(注入 L2 概要)。下次召回生效"},
    {"path": "memory.recall.final_l2_top_k", "kind": CONFIG_KIND_INT, "group": "memory-recall",
     "label": "L2 终筛 top_k", "description": "二级终筛保留的最终记忆条数。下次召回生效"},
    {"path": "memory.recall.include_l3_profile", "kind": CONFIG_KIND_BOOL, "group": "memory-recall",
     "label": "注入 L3 概览", "description": "是否在召回时附带 L3 用户/偏好概览。下次召回生效"},
    # ── 记忆捕获 ──
    {"path": "memory.capture.enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-capture",
     "label": "启用记忆捕获", "description": "关闭则不再从对话提取记忆。改后下次捕获生效"},
    {"path": "memory.capture.async_enabled", "kind": CONFIG_KIND_BOOL, "group": "memory-capture",
     "label": "异步捕获", "description": "开启则在后台异步提取记忆,不阻塞回复;关闭则同步提取。改后下次捕获生效"},
    {"path": "memory.capture.min_turn_chars", "kind": CONFIG_KIND_INT, "group": "memory-capture",
     "label": "最小捕获字符数", "description": "单轮短于此字符数不触发捕获;过滤无意义寒暄。改后下次捕获生效"},
    {"path": "memory.capture.extract_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "memory-capture",
     "label": "提取超时(秒)", "description": "单次记忆提取超时秒数;异步模式下超时即放弃该轮。改后下次捕获生效"},
    # ── 隐私 ──
    {"path": "memory.privacy.pii_redaction", "kind": CONFIG_KIND_BOOL, "group": "memory-privacy",
     "label": "PII 脱敏", "description": "写入记忆前对个人身份信息(邮箱/电话等)做脱敏。改后下次捕获生效"},
    {"path": "memory.privacy.allow_secret_memory", "kind": CONFIG_KIND_BOOL, "group": "memory-privacy",
     "label": "允许记忆机密", "description": "开启才允许记忆被标记为机密的内容;关闭则强制忽略机密标记。改后下次捕获生效"},
    {"path": "memory.privacy.raw_turn_retention_days", "kind": CONFIG_KIND_INT, "group": "memory-privacy",
     "label": "原始轮次保留(天)", "description": "原始对话轮次保留天数;超期清理,仅保留提取后的结构化记忆。改后下次清理周期生效"},
]

# 上下文压缩设置页字段定义(高级调参,前端整组默认折叠)。
# 阈值为相对模型上下文窗口的比例(float);context_length 单位 K(128=128K tokens)。
# 这一组"调好了就别动":误调可能导致对话爆窗口或被过度压缩,故前端默认折叠并加警示。
CONTEXT_FIELDS: list[dict[str, Any]] = [
    {"path": "context.enabled", "kind": CONFIG_KIND_BOOL, "group": "context-advanced",
     "label": "启用上下文压缩", "description": "关闭则长会话不压缩,可能爆上下文窗口。新会话生效"},
    {"path": "context.engine", "kind": CONFIG_KIND_STRING, "group": "context-advanced",
     "label": "压缩引擎", "description": "预留可插拔引擎名,默认 compressor。非必要勿改;改后新会话生效"},
    {"path": "context.threshold_percent", "kind": CONFIG_KIND_FLOAT, "group": "context-advanced",
     "label": "轮后触发阈值", "description": "真实 input_tokens 占窗口比例达此值触发主压缩(0.1-0.95)。调高更晚压缩、更易爆窗口;调低则更早压缩、可能过度。新会话生效"},
    {"path": "context.hard_ceiling_percent", "kind": CONFIG_KIND_FLOAT, "group": "context-advanced",
     "label": "轮内硬上限", "description": "轮内预检触发线(粗略估计,防单轮 replan/repair 撑爆窗口,0.2-0.99)。应高于轮后阈值。新会话生效"},
    {"path": "context.protect_first_n", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "保护开头 N 条", "description": "始终不压缩的开头消息数(首轮指令等)。调大可保更多上下文但减少可压缩量。新会话生效"},
    {"path": "context.protect_last_n", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "保护尾部 N 条", "description": "尾部最少保留的最近消息数。新会话生效"},
    {"path": "context.tail_token_budget", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "尾部 token 预算", "description": "从末尾向前累计保留的 token 预算。新会话生效"},
    {"path": "context.anti_thrash", "kind": CONFIG_KIND_BOOL, "group": "context-advanced",
     "label": "反抖动", "description": "开启则连续两次压缩节省不足 min_savings_pct% 时停止压缩,避免反复压缩无收益。新会话生效"},
    {"path": "context.min_savings_pct", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "最小节省百分比", "description": "单次压缩应达到的最低节省百分比(配合反抖动)。新会话生效"},
    {"path": "context.rescue_to_memory", "kind": CONFIG_KIND_BOOL, "group": "context-advanced",
     "label": "丢弃窗口抢救记忆", "description": "压缩时把将被丢弃的窗口抢救写入记忆库,后续可召回找回。新会话生效"},
    {"path": "context.context_length", "kind": CONFIG_KIND_INT, "group": "context-advanced",
     "label": "上下文窗口(K)", "description": "模型上下文窗口大小,单位 K(128=128K tokens)。非 128K 模型需按实际填写(如 32K 填 32);0 用内置默认。必须与模型真实窗口一致,否则压缩阈值计算偏差"},
]

# 任务调度设置页字段定义。
# scheduler 需重启进程才生效(scheduler 在启动期初始化 DB 与轮询循环);
# delivery / security 同样在启动期读取。
SCHEDULER_FIELDS: list[dict[str, Any]] = [
    {"path": "scheduler.enabled", "kind": CONFIG_KIND_BOOL, "group": "scheduler-main",
     "label": "启用任务调度", "description": "关闭则不轮询执行定时任务。改后需重启进程才彻底停用"},
    {"path": "scheduler.db_path", "kind": CONFIG_KIND_STRING, "group": "scheduler-main",
     "label": "数据库路径", "description": "定时任务 SQLite 路径(相对路径以项目根为根)。改后建议重启,且不会自动迁移已有任务"},
    {"path": "scheduler.poll_interval_seconds", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "轮询间隔(秒)", "description": "调度器轮询任务表的间隔秒数。改后需重启进程"},
    {"path": "scheduler.max_concurrency", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "最大并发", "description": "同时执行的任务数上限。改后需重启进程"},
    {"path": "scheduler.default_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "默认超时(秒)", "description": "单次任务默认超时秒数。改后需重启进程"},
    {"path": "scheduler.claim_lock_seconds", "kind": CONFIG_KIND_INT, "group": "scheduler-main",
     "label": "认领锁(秒)", "description": "多实例时任务认领锁定时长秒数。改后需重启进程"},
    {"path": "scheduler.delivery.default_mode", "kind": CONFIG_KIND_SELECT, "group": "scheduler-delivery",
     "label": "默认投递模式", "options": ["origin", "inbox", "explicit", "none"],
     "description": "新建任务的默认投递模式:origin=回发起会话;inbox=记入收件箱;explicit=按 targets 投递;none=不投递。改后需重启进程"},
    {"path": "scheduler.delivery.fallback_to_inbox", "kind": CONFIG_KIND_BOOL, "group": "scheduler-delivery",
     "label": "投递失败回落收件箱", "description": "任务投递失败时是否回落记入收件箱。改后需重启进程"},
    {"path": "scheduler.security.prompt_scan_enabled", "kind": CONFIG_KIND_BOOL, "group": "scheduler-security",
     "label": "提示词扫描", "description": "开启则校验定时任务 cron 的 prompt 是否符合安全策略。改后需重启进程"},
    {"path": "scheduler.security.allow_scheduler_mutation_in_scheduled_runs", "kind": CONFIG_KIND_BOOL, "group": "scheduler-security",
     "label": "允许任务内改调度", "description": "定时任务执行中是否允许调用调度相关工具改动任务表。关闭更安全。改后需重启进程"},
    {"path": "scheduler.security.allow_interactive_tools_in_scheduled_runs", "kind": CONFIG_KIND_BOOL, "group": "scheduler-security",
     "label": "允许任务内交互工具", "description": "定时任务执行中是否允许调用交互类工具。关闭更安全。改后需重启进程"},
]

# 联网研究设置页字段定义。
# enabled_backends 用 multi 多选(duckduckgo/brave/tavily);
# brave_api_key / tavily_api_key 为 secret——后端选择与对应 key 前端联动显示。
RESEARCH_FIELDS: list[dict[str, Any]] = [
    {"path": "research.enabled_backends", "kind": CONFIG_KIND_MULTI, "group": "research-main",
     "label": "启用后端", "options": ["duckduckgo", "brave", "tavily"],
     "description": "勾选启用的搜索后端;duckduckgo 无需 Key,brave/tavily 需对应 API Key"},
    {"path": "research.brave_api_key", "kind": CONFIG_KIND_SECRET, "group": "research-main",
     "label": "Brave API Key", "description": "启用 brave 后端时必填。留空则 brave 后端报错"},
    {"path": "research.tavily_api_key", "kind": CONFIG_KIND_SECRET, "group": "research-main",
     "label": "Tavily API Key", "description": "启用 tavily 后端时必填。留空则 tavily 后端报错"},
    {"path": "research.search_timeout_seconds", "kind": CONFIG_KIND_INT, "group": "research-main",
     "label": "搜索超时(秒)", "description": "单次搜索请求超时秒数(上限 60)"},
    {"path": "research.max_backend_results", "kind": CONFIG_KIND_INT, "group": "research-main",
     "label": "单后端结果数上限", "description": "每个后端返回的最大结果数(上限 50)"},
    {"path": "research.min_independent_sources", "kind": CONFIG_KIND_INT, "group": "research-main",
     "label": "最少独立来源数", "description": "研究报告要求的最少独立来源数(上限 20);提高可增强可信度"},
    {"path": "research.require_citations", "kind": CONFIG_KIND_BOOL, "group": "research-main",
     "label": "强制引用", "description": "开启则研究报告必须附来源引用"},
    {"path": "research.browser_fallback_enabled", "kind": CONFIG_KIND_BOOL, "group": "research-main",
     "label": "浏览器兜底", "description": "API 后端全部失败时回退到浏览器自动化抓取。依赖浏览器实例可用"},
]

# 路径与日志设置页字段定义(基础设施,低频)。
# 路径改错可能导致服务找不到资源(角色/记忆/技能/附件);日志级别影响输出粒度。
# paths.* 用 string(相对路径以项目根为根);logging.level 用 select 限定标准日志级别。
PATHS_LOGGING_FIELDS: list[dict[str, Any]] = [
    {"path": "paths.roles_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "角色目录", "description": "角色定义所在目录(相对项目根)。改后需重启进程才重新加载角色;改错将找不到角色"},
    {"path": "paths.memory_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "记忆目录", "description": "记忆数据库等所在的根目录(相对项目根)。改后需重启进程;改错将找不到记忆库"},
    {"path": "paths.external_skills_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "外部技能目录", "description": "额外扫描读取的外部技能目录(相对项目根,留空则不额外扫描)。改后需重启进程"},
    {"path": "paths.attachments_dir", "kind": CONFIG_KIND_STRING, "group": "paths",
     "label": "附件目录", "description": "上传附件暂存目录(相对项目根)。改后需重启进程"},
    {"path": "logging.level", "kind": CONFIG_KIND_SELECT, "group": "logging",
     "label": "日志级别", "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
     "description": "日志输出级别。改后需重启进程才彻底切换"},
    {"path": "logging.dir", "kind": CONFIG_KIND_STRING, "group": "logging",
     "label": "日志目录", "description": "日志文件目录(相对项目根)。改后需重启进程;改错将无法写日志"},
    {"path": "logging.console", "kind": CONFIG_KIND_BOOL, "group": "logging",
     "label": "控制台输出", "description": "是否同时向控制台输出日志。改后需重启进程"},
]

# 各设置页分组的字段定义注册表。新增分组在此追加 key → 字段列表即可。
SETTINGS_FIELD_GROUPS: dict[str, list[dict[str, Any]]] = {
    "ai-models": AI_MODEL_FIELDS,
    "network": NETWORK_FIELDS,
    "browser": BROWSER_FIELDS,
    "memory": MEMORY_FIELDS,
    "context": CONTEXT_FIELDS,
    "scheduler": SCHEDULER_FIELDS,
    "research": RESEARCH_FIELDS,
    "paths-logging": PATHS_LOGGING_FIELDS,
}


def load_raw_config(config_path: Path) -> dict[str, Any]:
    """读取 config.yaml 原始 dict(不做默认值填充/单位转换/路径解析)。

    WebUI 设置页直接读写原始 yaml,避免 AppConfig 的转换破坏文件语义
    (如 max_image_size_mb→bytes、相对路径→绝对、空值回退复用主模型)。
    """
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _get_by_path(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


def _set_by_path(data: dict[str, Any], path: str, value: Any) -> None:
    segs = path.split(".")
    cur: dict[str, Any] = data
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segs[-1]] = value


def mask_secret(value: str) -> str:
    """敏感字段掩码:保留前3+后4,中间用 •••• 占位。过短则全掩。空值返回空串。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}••••{value[-4:]}"


def serialize_config_value(kind: str, raw: Any) -> Any:
    """yaml 原始值 → 前端 JSON 值。secret 的脱敏由调用方(mask_secret)处理。

    SELECT 字段(如 supports_vision)在 yaml 里可能写成裸 true/false(被解析成
    Python bool),需归一为 "true"/"false" 字符串,以匹配 options 白名单。
    """
    if kind in (CONFIG_KIND_STRING, CONFIG_KIND_SECRET):
        return "" if raw is None else str(raw)
    if kind == CONFIG_KIND_SELECT:
        if isinstance(raw, bool):
            return "true" if raw else "false"
        return "" if raw is None else str(raw)
    if kind == CONFIG_KIND_BOOL:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    if kind == CONFIG_KIND_INT:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if kind == CONFIG_KIND_FLOAT:
        try:
            if raw is None or raw == "":
                return 0.0
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    if kind == CONFIG_KIND_MULTI:
        # yaml 列表归一为 list[str](仅保留 options 白名单内项);标量/逗号串也兼容。
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return []
    return raw


def serialize_config_group(fields: list[dict[str, Any]], raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """返回 (values, masked)。secret 字段非空时用掩码替换并记入 masked。"""
    values: dict[str, Any] = {}
    masked: list[str] = []
    for f in fields:
        raw_val = _get_by_path(raw, f["path"])
        val = serialize_config_value(f["kind"], raw_val)
        if f["kind"] == CONFIG_KIND_MULTI:
            # 只保留 options 白名单内项,按 options 顺序排序展示。
            opts = f.get("options", [])
            val = [v for v in opts if v in val]
        if f["kind"] == CONFIG_KIND_SECRET and val:
            masked.append(f["path"])
            val = mask_secret(val)
        values[f["path"]] = val
    return values, masked


def _section_body_range(lines: list[str], idx_sec: int, section_indent: int) -> tuple[int, int]:
    """section 内容范围 [start, end):从 idx_sec+1 起到遇到缩进 <= section_indent 的非空非注释行止。"""
    end = len(lines)
    for idx in range(idx_sec + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        if indent <= section_indent:
            end = idx
            break
    return idx_sec + 1, end


def _locate_section(lines: list[str], section_path: list[str]) -> tuple[int, int, int] | None:
    """逐级定位 section,返回 (body_start, body_end, section_indent)。任一级缺失返回 None。"""
    cur_start, cur_end, parent_indent = 0, len(lines), -1
    for name in section_path:
        idx_sec = None
        for idx in range(cur_start, cur_end):
            stripped = lines[idx].strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(lines[idx]) - len(lines[idx].lstrip())
            if indent <= parent_indent:
                break
            if stripped.startswith(f"{name}:"):
                idx_sec = idx
                break
        if idx_sec is None:
            return None
        parent_indent = len(lines[idx_sec]) - len(lines[idx_sec].lstrip())
        cur_start, cur_end = _section_body_range(lines, idx_sec, parent_indent)
    return cur_start, cur_end, parent_indent


def replace_yaml_section_kv(
    config_path: Path,
    section_path: list[str],
    key: str,
    value_str: str,
    raw_config: dict[str, Any],
    *,
    quote: bool = True,
) -> None:
    """在 yaml 指定 section 内替换或插入一个 key 行,保留注释/缩进/行尾。

    section_path 如 ["vision"] 或 ["context","summary"]。section 不存在或解析失败时,
    回退到 yaml.safe_dump 重写整个文件(raw_config 应已用 _set_by_path 同步为最新)。
    """
    formatted = _quote_yaml_string(value_str) if quote else str(value_str)
    try:
        text = config_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        loc = _locate_section(lines, section_path)
        if loc is None:
            raise ValueError(f"section {'.'.join(section_path)} 不存在")
        body_start, body_end, section_indent = loc
        key_indent = section_indent + 2

        key_idx = None
        for idx in range(body_start, body_end):
            if lines[idx].strip().startswith(f"{key}:"):
                key_idx = idx
                break

        if key_idx is not None:
            line = lines[key_idx]
            line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            comment = ""
            before_comment = line.rstrip("\r\n")
            if "#" in before_comment:
                comment = " " + before_comment[before_comment.index("#"):].strip()
            lines[key_idx] = f"{' ' * key_indent}{key}: {formatted}{comment}{line_ending}"
        else:
            lines.insert(body_end, f"{' ' * key_indent}{key}: {formatted}\n")
        config_path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        config_path.write_text(yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def replace_yaml_section_list(
    config_path: Path,
    section_path: list[str],
    key: str,
    items: list[str],
    raw_config: dict[str, Any],
) -> None:
    """在 yaml 指定 section 内写回一个字符串列表(多选),保留其余注释/缩进。

    列表在 yaml 里写作多行 `- item`。本函数把 `key:` 及其后所有 `- ...` 连续行
    视为该列表块整体替换;原为单行 `key: a, b` 或 inline 形式也按块替换。
    section 不存在或解析失败时回退到 yaml.safe_dump 整体重写。
    """
    try:
        text = config_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        loc = _locate_section(lines, section_path)
        if loc is None:
            raise ValueError(f"section {'.'.join(section_path)} 不存在")
        body_start, body_end, section_indent = loc
        key_indent = section_indent + 2
        item_indent = key_indent + 2

        # 定位 key 行
        key_idx = None
        for idx in range(body_start, body_end):
            if lines[idx].strip().startswith(f"{key}:"):
                key_idx = idx
                break

        # 计算新内容块
        if items:
            new_block = [f"{' ' * key_indent}{key}:\n"] + [
                f"{' ' * item_indent}- {item}\n" for item in items
            ]
        else:
            new_block = [f"{' ' * key_indent}{key}: []\n"]

        if key_idx is not None:
            # 找到该列表块结束:key 行之后连续的以 item_indent 开头的 `- ` 行(可跨空行/注释行)。
            block_end = key_idx + 1
            line_ending = "\r\n" if lines[key_idx].endswith("\r\n") else "\n" if lines[key_idx].endswith("\n") else ""
            # 行尾统一化新块(用 \n;末行无需尾随,重组时补)。
            while block_end < body_end:
                raw_line = lines[block_end]
                stripped = raw_line.strip()
                if (
                    stripped.startswith("- ")
                    and (len(raw_line) - len(raw_line.lstrip())) == item_indent
                ):
                    block_end += 1
                elif stripped == "" or stripped.startswith("#"):
                    # 空行/注释行只有在直到遇到下一个 key 前且紧跟列表项才并入;
                    # 简化:遇到不在缩进的空/注释且后面不是列表项则停止。
                    break
                else:
                    break
            # 用新块替换 [key_idx, block_end)
            new_block = [b.rstrip("\n") + line_ending for b in new_block[:-1]] + [new_block[-1].rstrip("\n") + line_ending]
            lines[key_idx:block_end] = new_block
        else:
            lines.insert(body_end, "".join(new_block))
        config_path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        config_path.write_text(yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def apply_config_updates(
    config_path: Path,
    raw_config: dict[str, Any],
    fields: list[dict[str, Any]],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """按 updates(路径→值)写回 yaml,逐字段同步 raw_config。

    - 白名单校验:updates 的 path 必须在 fields 内,否则跳过。
    - secret:incoming 等于当前掩码(未改动)则跳过;空串代表清除(回退复用主模型)。
    - select:值必须在 options 内。
    - 每写一个字段同步 _set_by_path 到 raw_config,供后续字段掩码比对与 fallback 使用。
    返回 {"written": [...], "skipped": [...]}。校验失败抛 ValueError。
    """
    field_map = {f["path"]: f for f in fields}
    written: list[str] = []
    skipped: list[str] = []
    for path, incoming in updates.items():
        f = field_map.get(path)
        if f is None:
            skipped.append(path)
            continue
        kind = f["kind"]

        if kind == CONFIG_KIND_SECRET:
            current = _get_by_path(raw_config, path)
            current_str = "" if current is None else str(current)
            if incoming == mask_secret(current_str):
                skipped.append(path)
                continue
            native = "" if incoming is None else str(incoming)
            value_str, quote = native, True
        elif kind == CONFIG_KIND_BOOL:
            native = incoming if isinstance(incoming, bool) else str(incoming).strip().lower() in {"1", "true", "yes", "on"}
            value_str, quote = ("true" if native else "false"), False
        elif kind == CONFIG_KIND_INT:
            try:
                native = int(incoming)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path} 必须是整数") from exc
            value_str, quote = str(native), False
        elif kind == CONFIG_KIND_FLOAT:
            try:
                native = float(incoming) if incoming not in (None, "") else 0.0
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path} 必须是数值") from exc
            value_str, quote = str(native), False
        elif kind == CONFIG_KIND_MULTI:
            # incoming 应为 list[str](或逗号串/单串);仅保留 options 白名单内项,按 options 顺序写回。
            opts = f.get("options", [])
            if isinstance(incoming, list):
                raw_items = [str(x).strip() for x in incoming if str(x).strip()]
            elif isinstance(incoming, str):
                raw_items = [s.strip() for s in incoming.split(",") if s.strip()] if incoming else []
            elif incoming is None:
                raw_items = []
            else:
                raise ValueError(f"{path} 必须是列表")
            allowed = set(opts)
            native = [x for x in opts if x in raw_items]  # 按 options 顺序、去重、仅白名单
            _set_by_path(raw_config, path, native)
            section_path = path.split(".")[:-1]
            key = path.split(".")[-1]
            replace_yaml_section_list(config_path, section_path, key, native, raw_config)
            written.append(path)
            continue
        else:  # string / select
            native = "" if incoming is None else str(incoming)
            if kind == CONFIG_KIND_SELECT and not f.get("editable") and native not in f.get("options", []):
                raise ValueError(f"{path} 必须是 {f.get('options')} 之一")
            value_str, quote = native, True

        _set_by_path(raw_config, path, native)
        section_path = path.split(".")[:-1]
        key = path.split(".")[-1]
        replace_yaml_section_kv(config_path, section_path, key, value_str, raw_config, quote=quote)
        written.append(path)
    return {"written": written, "skipped": skipped}
