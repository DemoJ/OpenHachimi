"""应用配置数据模型。

集中存放所有 @dataclass 配置类型与模块级常量,是纯数据声明,
被 loading / persistence / webui_io 及外部调用方引用。不应包含加载或读写逻辑。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


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
    # 不依赖新对话的独立定时清理周期(expire_due_atoms/archive_decayed_atoms)。
    # maintenance job handler 也可被外部手动入队,但核心定时逻辑在 MemoryScheduler
    # _run_loop 内内联(不走 job queue,避免队列竞争)。默认 6 小时。
    maintenance_interval_seconds: int = 21600


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


@dataclass(frozen=True)
class DelegationConfig:
    """子 agent 委派(delegate_task)的运行时约束。对齐 hermes delegation 配置语义。

    所有字段都有默认值:旧 config.yaml 无 ``delegation:`` 段时不报错,沿用默认。
    """

    max_concurrent_children: int = 3       # 单次 delegate_task 并发子 agent 上限
    max_spawn_depth: int = 1               # 委派树深度上限(1=扁平,leaf 不可再委派)
    orchestrator_enabled: bool = True      # 全局开关;False 时 role=orchestrator 强制降为 leaf
    max_iterations: int = 50               # 单个子 agent 的工具调用轮次上限
    child_timeout_seconds: float = 0.0     # 单个子 agent 挂钟超时(0=不超时)


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool = True
    fallback_enabled: bool = True
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    detail: Literal["auto", "low", "high"] = "auto"
    # 已废弃:图片识别提示词改由 user/system_prompts/vision/default_user.md 覆盖。
    # 保留字段仅为兼容旧 yaml 读取(loading 层做一次性迁移),运行时不再使用其值。
    prompt: str = ""
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
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    http_api_token: str | None = None
    server_host: str = "127.0.0.1"   # HTTP 服务监听地址；127.0.0.1=仅本机，0.0.0.0=开放局域网/公网访问
    server_port: int = 8765           # HTTP 服务监听端口