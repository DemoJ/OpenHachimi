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
