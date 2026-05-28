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
    run_mode: str = "interactive"
    channel_context: dict[str, Any] = field(default_factory=dict)
    scheduler_context: dict[str, Any] = field(default_factory=dict)

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
