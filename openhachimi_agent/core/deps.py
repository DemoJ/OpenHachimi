from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.models import MemoryContext, MemoryScope

@dataclass
class AgentDeps:
    config: AppConfig
    session_id: str
    browser_manager: Any = None
    process_manager: Any = None
    session_state: dict[str, Any] = field(default_factory=dict)
    memory_scope: MemoryScope | None = None
    memory_context: MemoryContext | None = None
    memory_service: Any = None
    # SessionStore;为避免 storage <-> core 循环 import 用 Any。tools(如 planning)
    # 通过 ctx.deps.session_store 访问会话级 SQLite 库。
    session_store: Any = None
    run_mode: str = "interactive"
    # 当前角色名:供 runtime_context 的技能索引块按角色过滤可见 skills。
    # 默认空串=不按角色过滤(全用),与历史行为一致。
    role_name: str = ""
    channel_context: dict[str, Any] = field(default_factory=dict)
    scheduler_context: dict[str, Any] = field(default_factory=dict)

    @property
    def role_or_default(self) -> str:
        return self.role_name or self.config.default_role_name

    @property
    def channel(self) -> str:
        return str(self.channel_context.get("type") or self.channel_context.get("platform") or "local")

    @property
    def delivery_target(self) -> dict[str, Any]:
        if self.channel_context.get("platform") in {"telegram", "cli", "inbox"}:
            target = dict(self.channel_context)
            target["type"] = str(target.get("platform") or target.get("type"))
            return target
        return {}

    @property
    def base_dir(self) -> Path:
        return self.config.base_dir

    @property
    def skills_dirs(self) -> list[Path]:
        return self.config.skills_dirs
