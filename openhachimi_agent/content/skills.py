"""Claude Skills Parsing Module.

This module is responsible for finding and parsing Claude Skills,
which use a directory-based structure with SKILL.md containing
YAML frontmatter and a Markdown body.
"""

import os
import re
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)


class SkillConfig(BaseModel):
    """Represents the YAML frontmatter of a SKILL.md file."""
    name: str
    description: str
    disable_model_invocation: bool = Field(default=False, alias="disable-model-invocation")
    allowed_tools: Optional[list[str]] = Field(default=None, alias="allowed-tools")
    when_to_use: Optional[str] = None
    arguments: Optional[list[str]] = None
    context: Optional[str] = None


@dataclass
class Skill:
    """Represents a fully parsed Claude Skill."""
    path: Path
    config: SkillConfig
    body: str


def parse_skill(path: Path) -> Skill | None:
    """Parses a SKILL.md file into a Skill object."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to read skill file %s: %s", path, e)
        return None

    # Allow leading whitespace/newlines before the first '---'
    content_stripped = content.lstrip()
    if not content_stripped.startswith("---"):
        logger.warning("Skill file %s missing YAML frontmatter (must start with '---')", path)
        return None

    # Split into maximum 3 parts: ['', 'frontmatter', 'body']
    parts = content_stripped.split("---", 2)
    if len(parts) < 3:
        logger.warning("Skill file %s has unclosed YAML frontmatter", path)
        return None
        
    frontmatter_str = parts[1]
    body_str = parts[2]
    
    try:
        frontmatter_data = yaml.safe_load(frontmatter_str) or {}
        config = SkillConfig(**frontmatter_data)
        return Skill(path=path, config=config, body=body_str.strip())
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML in skill file %s: %s", path, e)
        return None
    except Exception as e:
        logger.warning("Invalid config in skill file %s: %s", path, e)
        return None


_SKILLS_CACHE: dict[str, tuple[float, list[Skill]]] = {}
_SKILLS_CACHE_LOCK = RLock()


def find_skills(skills_dirs: list[Path]) -> list[Skill]:
    """Scans provided directories for SKILL.md files and parses them with caching."""
    cache_key = ":".join(str(p.resolve()) for p in skills_dirs)

    current_mtime = 0.0
    skill_paths: list[Path] = []

    for directory in skills_dirs:
        if not directory.exists() or not directory.is_dir():
            continue

        for root, _, files in os.walk(directory):
            for file in files:
                if file.lower() == "skill.md":
                    p = Path(root) / file
                    skill_paths.append(p)
                    try:
                        current_mtime = max(current_mtime, p.stat().st_mtime)
                    except Exception:
                        pass

    with _SKILLS_CACHE_LOCK:
        cached = _SKILLS_CACHE.get(cache_key)
        if cached:
            cached_mtime, cached_skills = cached
            if current_mtime > 0 and cached_mtime >= current_mtime:
                return list(cached_skills)

    skills = []
    for skill_path in skill_paths:
        skill = parse_skill(skill_path)
        if skill:
            skills.append(skill)

    with _SKILLS_CACHE_LOCK:
        _SKILLS_CACHE[cache_key] = (current_mtime, list(skills))
    return skills
