"""Claude Skills Parsing Module.

This module is responsible for finding and parsing Claude Skills,
which use a directory-based structure with SKILL.md containing
YAML frontmatter and a Markdown body.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from openhachimi_agent.core.config import AppConfig


class SkillConfig(BaseModel):
    """Represents the YAML frontmatter of a SKILL.md file."""
    name: str
    description: str
    disable_model_invocation: bool = Field(default=False, alias="disable-model-invocation")
    allowed_tools: Optional[str] = Field(default=None, alias="allowed-tools")
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
        return None

    # Parse YAML frontmatter
    # Matches --- at start, non-greedy match for frontmatter, then ---, then the rest
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    
    if not match:
        # Some skills might just be raw markdown without frontmatter, but according
        # to Claude docs, 'name' and 'description' in frontmatter are required.
        return None
        
    frontmatter_str = match.group(1)
    body_str = match.group(2)
    
    try:
        frontmatter_data = yaml.safe_load(frontmatter_str) or {}
        config = SkillConfig(**frontmatter_data)
        return Skill(path=path, config=config, body=body_str.strip())
    except Exception as e:
        # Ignore skills with invalid frontmatter or missing required fields
        return None


def find_skills(skills_dirs: list[Path]) -> list[Skill]:
    """Scans provided directories for SKILL.md files and parses them."""
    skills = []
    
    for directory in skills_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
            
        for root, _, files in os.walk(directory):
            for file in files:
                if file.lower() == "skill.md":
                    skill_path = Path(root) / file
                    skill = parse_skill(skill_path)
                    if skill:
                        skills.append(skill)
                        
    return skills
