"""应用配置包:数据模型、加载解析、持久化、WebUI 字段定义与读写引擎。

原为单文件 core/config.py(1322 行,职责膨胀),拆为子模块各承一职:
  - models:        @dataclass 配置类型与常量(纯数据)
  - _helpers:      解析辅助函数 + 模块级 logger(私有)
  - persistence:   配置文件写回(app 段 / http_api_token / deploy host:port)
  - loading:       YAML 加载解析 + _load_* + load_config / load_mcp_config
  - webui_fields:  WebUI 设置页字段定义表(声明式数据)
  - webui_io:      WebUI 设置页读写引擎(serialize/replace/apply)

本 __init__ 只做 re-export,保持旧的 `from openhachimi_agent.core.config import X`
入口不变,调用方无需改动。
"""

from openhachimi_agent.core.config._helpers import logger  # noqa: F401 — 暴露给上层共享
from openhachimi_agent.core.config.loading import load_config, load_mcp_config
from openhachimi_agent.core.config.mcp_store import get_mcp_config, write_mcp_config
from openhachimi_agent.core.config.roles_store import (
    ROLES_CONFIG_FILE_NAME,
    RoleBindingConfig,
    get_role_binding,
    load_roles_config,
    write_roles_config,
)
from openhachimi_agent.core.config.models import (
    CONFIG_FILE_NAME,
    USER_DIR_NAME,
    AppConfig,
    ContextConfig,
    ContextSummaryConfig,
    MCPConfig,
    MCPServerConfig,
    MemoryCaptureConfig,
    MemoryConfig,
    MemoryConsolidationConfig,
    MemoryEmbeddingConfig,
    MemoryPrivacyConfig,
    MemoryRecallConfig,
    MemorySchedulerConfig,
    MemoryVectorConfig,
    ResearchConfig,
    SchedulerConfig,
    SchedulerDeliveryConfig,
    SchedulerSecurityConfig,
    VisionConfig,
)
from openhachimi_agent.core.config.persistence import (
    _ensure_http_api_token,
    _replace_or_insert_app_kv,
    _replace_or_insert_http_api_token,
    persist_server_endpoint,
)
from openhachimi_agent.core.config.webui_fields import (
    AI_MODEL_FIELDS,
    BROWSER_FIELDS,
    CONFIG_KIND_BOOL,
    CONFIG_KIND_FLOAT,
    CONFIG_KIND_INT,
    CONFIG_KIND_MULTI,
    CONFIG_KIND_SECRET,
    CONFIG_KIND_SELECT,
    CONFIG_KIND_STRING,
    CONTEXT_FIELDS,
    MEMORY_FIELDS,
    NETWORK_FIELDS,
    PATHS_LOGGING_FIELDS,
    RESEARCH_FIELDS,
    SCHEDULER_FIELDS,
    SETTINGS_FIELD_GROUPS,
)
from openhachimi_agent.core.config.webui_io import (
    apply_config_updates,
    load_raw_config,
    mask_secret,
    serialize_config_group,
    serialize_config_value,
)

__all__ = [
    # 常量
    "USER_DIR_NAME",
    "CONFIG_FILE_NAME",
    "logger",
    # 数据模型
    "AppConfig",
    "MemoryConfig",
    "MemoryEmbeddingConfig",
    "MemoryRecallConfig",
    "MemoryCaptureConfig",
    "MemoryPrivacyConfig",
    "MemorySchedulerConfig",
    "MemoryConsolidationConfig",
    "MemoryVectorConfig",
    "SchedulerConfig",
    "SchedulerDeliveryConfig",
    "SchedulerSecurityConfig",
    "ResearchConfig",
    "VisionConfig",
    "MCPConfig",
    "MCPServerConfig",
    "ContextConfig",
    "ContextSummaryConfig",
    # 加载 / 持久化 / WebUI 读写
    "load_config",
    "load_mcp_config",
    "get_mcp_config",
    "write_mcp_config",
    "load_roles_config",
    "write_roles_config",
    "get_role_binding",
    "RoleBindingConfig",
    "ROLES_CONFIG_FILE_NAME",
    "load_raw_config",
    "persist_server_endpoint",
    "_ensure_http_api_token",
    "_replace_or_insert_app_kv",
    "_replace_or_insert_http_api_token",
    "apply_config_updates",
    "serialize_config_group",
    "serialize_config_value",
    "mask_secret",
    # WebUI 字段定义
    "SETTINGS_FIELD_GROUPS",
    "AI_MODEL_FIELDS",
    "NETWORK_FIELDS",
    "BROWSER_FIELDS",
    "MEMORY_FIELDS",
    "CONTEXT_FIELDS",
    "SCHEDULER_FIELDS",
    "RESEARCH_FIELDS",
    "PATHS_LOGGING_FIELDS",
    "CONFIG_KIND_SECRET",
    "CONFIG_KIND_STRING",
    "CONFIG_KIND_SELECT",
    "CONFIG_KIND_BOOL",
    "CONFIG_KIND_INT",
    "CONFIG_KIND_FLOAT",
    "CONFIG_KIND_MULTI",
]