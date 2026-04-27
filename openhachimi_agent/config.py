"""应用配置。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    """集中管理应用运行时配置。"""

    base_dir: Path
    prompts_dir: Path
    roles_dir: Path
    memory_dir: Path
    model_name: str
    openai_base_url: str
    default_role_name: str
    openai_api_key: str | None


def load_config() -> AppConfig:
    """从环境变量和项目目录加载配置。"""
    base_dir = Path(__file__).resolve().parent.parent
    return AppConfig(
        base_dir=base_dir,
        prompts_dir=base_dir / "prompts",
        roles_dir=base_dir / "roles",
        memory_dir=base_dir / ".memory",
        model_name=os.getenv("OPENAI_MODEL", "gpt-5.2"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
        default_role_name=os.getenv("OPENHACHIMI_ROLE", "default"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
    )
