from dataclasses import dataclass
from openhachimi_agent.core.config import AppConfig

@dataclass
class AgentDeps:
    config: AppConfig
    session_id: str
