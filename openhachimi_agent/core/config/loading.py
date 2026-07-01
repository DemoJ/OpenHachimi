"""config.yaml 加载与解析。

从 user/config.yaml(及 mcp-servers.json)读取原始 yaml,填充默认值、做单位转换、
解析相对路径,组装成 AppConfig。是配置系统的"读取侧",不负责写回文件。
"""

import json
from pathlib import Path
from typing import Any

import yaml

from openhachimi_agent.core.config._helpers import (
    _as_mapping,
    _config_bool,
    _config_int,
    _config_literal,
    _config_string,
    _config_string_list,
    _config_vision_support,
    _resolve_config_path,
    _string_mapping,
    logger,
)
from openhachimi_agent.core.config.models import (
    CONFIG_FILE_NAME,
    USER_DIR_NAME,
    ContextConfig,
    ContextSummaryConfig,
    DelegationConfig,
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
from openhachimi_agent.core.config.persistence import _ensure_http_api_token
from openhachimi_agent.content.prompt_registry import PROMPTS as _REGISTRY_PROMPTS


# 首次预置到 user/system_prompts/ 的提示词列表;引用 prompt_registry 的 name,避免两处重复定义。
# 仅当 override 根目录不存在时整体初始化一次,已存在则完全不碰(不补缺、不覆盖),防升级期覆盖改动。
_DEFAULT_PROMPT_OVERRIDES = tuple(spec.name for spec in _REGISTRY_PROMPTS)

# 迁移旧 vision.prompt 时写入的覆盖文件路径(相对 system_prompts/)。
_VISION_PROMPT_OVERRIDE_NAME = "vision/default_user"


def ensure_default_prompt_overrides(user_dir: Path) -> None:
    """首次运行时把内置首批提示词复制到 user/system_prompts/,供用户直接编辑。

    幂等且单向:仅当 override 根目录完全不存在时初始化,已存在则跳过(不补缺、不覆盖)。
    这意味着升级后新增的可覆盖提示词不会自动落进用户目录;用户按需手动从内置复制即可。
    """
    from importlib.resources import files

    from openhachimi_agent.content.prompts import SYSTEM_PROMPTS_PACKAGE, _prompt_path_parts

    override_root = user_dir / "system_prompts"
    if override_root.exists():
        return  # 用户已接管,绝不覆盖

    override_root.mkdir(parents=True, exist_ok=True)
    for name in _DEFAULT_PROMPT_OVERRIDES:
        parts = _prompt_path_parts(name)
        target = override_root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = files(SYSTEM_PROMPTS_PACKAGE).joinpath(*parts)
        content = source.read_text(encoding="utf-8")
        target.write_text(content, encoding="utf-8")
    # 随首批文件写入一份用法说明,指明覆盖/回退/模板变量语义。
    _write_prompts_override_readme(override_root)
    logger.info("已预置首批可编辑提示词到 %s", override_root)


def _write_prompts_override_readme(override_root: Path) -> None:
    readme = override_root / "README.md"
    readme.write_text(_PROMPTS_OVERRIDE_README, encoding="utf-8")


_PROMPTS_OVERRIDE_README = """\
# 自定义系统提示词

本目录是哈基米系统提示词的**用户覆盖目录**。要改某个提示词,直接编辑本目录下对应的
`.md` 文件即可——加载时**本目录优先,留空或删除文件则回退内置默认**。

## 工作机制

- 加载顺序:`user/system_prompts/<路径>.md` > 内置 `system_prompts/<路径>.md`。
- 路径与内置一一对应,如内置 `agents/router.md` → 本目录 `agents/router.md`。
- 文件**留空(空白)**会自动回退内置,不会让空提示词进模型。
- 删除本目录下某个文件 = 回退到该提示词的内置版本。
- 升级版本时内置 `.md` 会被更新;本目录下的副本**不会被覆盖**(已接管即保留)。

## 含模板变量的提示词

部分提示词含 `{{ 变量名 }}` 占位符。凡经"渲染"加载的提示词(如 executor/repair 等),
删除占位符会导致渲染报错。**编辑此类文件时保留 `{{ }}` 占位符**。
本目录预置的 `context/summary.md` 含 `{{ current_date }}`,但该提示词不经渲染加载
(日期通过对话消息提供),占位符仅作文本提示,删除也无碍——仍建议保留以便对照内置。

## 首批预置(可直接编辑)

| 文件 | 用途 |
|---|---|
| `base.md` | 主模型基础人格:角色、核心原则、工具使用规范。改后新会话生效 |
| `vision/default_user.md` | 图片识别的用户提示词:决定识别产出的详略与重点方向 |
| `context/summary.md` | 长对话上下文压缩摘要的系统提示词(含 `{{ current_date }}`) |

## 新会话生效

提示词在 agent 构建时读取,改动对**新会话**生效;运行中的旧会话仍用其构建时的提示词。
"""


def migrate_legacy_vision_prompt(
    user_dir: Path, config_path: Path, raw_config: dict[str, Any]
) -> None:
    """把旧 yaml 里的 vision.prompt 一次性迁移到 user/system_prompts/vision/default_user.md。

    单一事实来源改造:废弃 yaml 多行文本配置,改为 .md 覆盖。检测到 legacy 非空时写迁移文件
    + 清除 yaml 行(下次检测为空即跳过)。用户已接管(.md 已存在)则不覆盖,仅清 yaml 行。
    """
    from importlib.resources import files

    from openhachimi_agent.content.prompts import SYSTEM_PROMPTS_PACKAGE, _prompt_path_parts
    from openhachimi_agent.core.config.webui_io import replace_yaml_section_kv

    vision_config = _as_mapping(raw_config.get("vision"), "vision")
    legacy_prompt = _config_string(vision_config, "prompt")
    if not legacy_prompt:
        return  # 无 legacy,无需迁移

    override_file = user_dir / "system_prompts" / "vision" / "default_user.md"
    override_exists = override_file.is_file() and override_file.read_text(encoding="utf-8").strip()

    logger.warning(
        "vision.prompt(yaml)已废弃,改为 user/system_prompts/vision/default_user.md 覆盖。"
    )
    if not override_exists:
        # 用户尚未接管该提示词:把 legacy 内容落成 .md 覆盖文件,行为无感延续。
        override_file.parent.mkdir(parents=True, exist_ok=True)
        if not override_file.is_file():
            override_file.write_text(legacy_prompt, encoding="utf-8")
        logger.warning("已把旧 vision.prompt 迁移到 %s", override_file)
    else:
        # 用户已自行接管,legacy 值让位于 .md,直接丢弃即可。
        logger.warning("已存在用户自定义 %s,旧 vision.prompt 将被忽略并清除。", override_file)

    # 从 yaml 清除该 key:用内置默认占位写回(空串)会污染文件,改为整段删除更干净。
    # _replace_or_insert 系函数只插入/替换不删除,故在此按行精确移除 vision 段的 prompt: 行。
    _remove_yaml_vision_prompt_key(config_path, raw_config)
    # 同步内存 raw_config,使后续逻辑读到空 prompt 不再触发重复迁移。
    if isinstance(raw_config.get("vision"), dict):
        raw_config["vision"].pop("prompt", None)


def _remove_yaml_vision_prompt_key(config_path: Path, raw_config: dict[str, Any]) -> None:
    """从 config.yaml 的 vision 段精确删除 prompt: 行(保留其余键与注释/缩进)。"""
    try:
        text = config_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        vision_idx = next((i for i, l in enumerate(lines) if l.strip() == "vision:"), None)
        if vision_idx is None:
            return
        vision_indent = len(lines[vision_idx]) - len(lines[vision_idx].lstrip())
        # 找 vision 段内缩进 > vision_indent 的连续行范围
        end = len(lines)
        for i in range(vision_idx + 1, len(lines)):
            s = lines[i].strip()
            if s and not s.startswith("#"):
                if len(lines[i]) - len(lines[i].lstrip()) <= vision_indent:
                    end = i
                    break
        # 在段内删 prompt: 行(含其后可能的续行;单行形式直接删该行)
        target = None
        for i in range(vision_idx + 1, end):
            if lines[i].lstrip().startswith("prompt:"):
                target = i
                break
        if target is None:
            return
        del lines[target]
        config_path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        # 失败兜底:整体重写(丢注释但保证可用),与 webui_io/persistence 一致策略。
        if isinstance(raw_config.get("vision"), dict):
            raw_config["vision"].pop("prompt", None)
        from openhachimi_agent.core.config._helpers import _yaml_safe_dump_to

        config_path.write_text(_yaml_safe_dump_to(raw_config), encoding="utf-8")


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
            maintenance_interval_seconds=_config_int(scheduler_config, "maintenance_interval_seconds", 21600),
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
    )


def _load_delegation_config(raw_config: dict[str, Any]) -> DelegationConfig:
    """从 ``delegation:`` 段读取子 agent 委派约束;缺段时全默认。"""
    delegation_config = _as_mapping(raw_config.get("delegation"), "delegation")
    return DelegationConfig(
        max_concurrent_children=max(1, _config_int(delegation_config, "max_concurrent_children", 3)),
        # depth 1=扁平(leaf 不可再委派);允许 1~3,低于 1 强制 1。
        max_spawn_depth=min(3, max(1, _config_int(delegation_config, "max_spawn_depth", 1))),
        orchestrator_enabled=_config_bool(delegation_config, "orchestrator_enabled", True),
        max_iterations=max(1, _config_int(delegation_config, "max_iterations", 50)),
        child_timeout_seconds=max(0.0, float(_config_int(delegation_config, "child_timeout_seconds", 0) or 0)),
    )


def _load_vision_config(raw_config: dict[str, Any], llm_config: dict[str, Any]) -> VisionConfig:
    vision_config = _as_mapping(raw_config.get("vision"), "vision")
    # vision.prompt 已废弃;legacy 迁移由 migrate_legacy_vision_prompt 在 load_config 末段处理,
    # 运行时图片识别走 load_system_prompt("vision/default_user")(覆盖→回退内置)。
    return VisionConfig(
        enabled=_config_bool(vision_config, "enabled", True),
        fallback_enabled=_config_bool(vision_config, "fallback_enabled", True),
        model=_config_string(vision_config, "model"),
        base_url=_config_string(vision_config, "base_url") or _config_string(llm_config, "base_url"),
        api_key=_config_string(vision_config, "api_key") or _config_string(llm_config, "api_key") or None,
        detail=_config_literal(vision_config, "detail", {"auto", "low", "high"}, "auto"),  # type: ignore[arg-type]
        prompt="",  # 已废弃,运行时无意义
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
        context_length=_config_int(context_config, "context_length", 128, minimum=0),
        summary=ContextSummaryConfig(
            model=_config_string(summary_config, "model"),
            base_url=_config_string(summary_config, "base_url") or _config_string(llm_config, "base_url"),
            api_key=_config_string(summary_config, "api_key") or _config_string(llm_config, "api_key") or None,
            max_tokens=_config_int(summary_config, "max_tokens", 4096, minimum=256),
            abort_on_failure=_config_bool(summary_config, "abort_on_failure", False),
        ),
    )


def load_config() -> "AppConfig":  # noqa: F821 — AppConfig 经 __init__.py re-export,运行时可见
    """从 user/config.yaml 和项目目录加载配置。"""
    from openhachimi_agent.core.config.models import AppConfig

    base_dir = Path(__file__).resolve().parents[3]
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

    cfg = AppConfig(
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
        delegation=_load_delegation_config(raw_config),
    )

    # 提示词覆盖机制:启动期注入 user 目录,使 load_system_prompt 优先读 user/system_prompts/。
    # 必须在所有运行时使用点(agent factory / summary / memory / vision)之前,这里恰为最早。
    from openhachimi_agent.content.prompts import set_prompts_override_dir

    set_prompts_override_dir(user_dir)
    # 废弃 vision.prompt 的 legacy 迁移(写迁移文件 + 清 yaml 行),不晚于覆盖机制启用。
    migrate_legacy_vision_prompt(user_dir, config_path, raw_config)
    # 首次运行预置首批可编辑 .md 副本(目录已存在则跳过,绝不覆盖用户改动)。
    ensure_default_prompt_overrides(user_dir)

    return cfg