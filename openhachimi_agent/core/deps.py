from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openhachimi_agent.core.config import AppConfig

@dataclass
class AgentDeps:
    config: AppConfig
    session_id: str
    browser_manager: Any = None
    process_manager: Any = None
    session_state: dict[str, Any] = field(default_factory=dict)

    @property
    def base_dir(self) -> Path:
        return self.config.base_dir

    @property
    def skills_dirs(self) -> list[Path]:
        return self.config.skills_dirs
