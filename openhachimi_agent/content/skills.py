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
    # 可选分组标签,只用于在 system_prompts/runtime/skills_index 索引里按类目展示。
    # 未声明时归到 "general" 桶,完全向后兼容旧 SKILL.md。
    category: Optional[str] = None


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


def _dedupe_skills_first_wins(parsed: list[Skill]) -> list[Skill]:
    """按 ``config.name`` 去重,保留先扫到的那条(first wins)。

    背景:``skills_dirs`` 通常是 ``[user/skills, external_skills_dir]`` 这样的
    多目录列表。如果两个目录里出现同名 SKILL.md,旧实现会把两条都返回,导致
    ``runtime/skills_index`` 索引重复出现同一行,且 ``get_skill_instructions``
    返回的是第一条 —— 行为已经"first wins",但索引和工具结果不一致。

    现在把去重前置到 ``find_skills`` 层,语义统一:
    - 先扫到的赢(``skills_dirs`` 的次序决定优先级 —— 项目内 ``user/skills``
      在外部目录之前,所以项目自带的 skill 永远不会被外部目录的同名 skill 遮蔽)。
    - 被遮蔽者打一条 ``info`` 日志,方便用户排查"为什么改了外部目录没生效"。
    """
    seen: dict[str, Skill] = {}
    for skill in parsed:
        name = skill.config.name
        if name in seen:
            kept_path = seen[name].path
            logger.info(
                "skill name conflict: %r at %s shadowed by earlier %s (first-wins)",
                name,
                skill.path,
                kept_path,
            )
            continue
        seen[name] = skill
    return list(seen.values())


def find_skills(skills_dirs: list[Path]) -> list[Skill]:
    """Scans provided directories for SKILL.md files and parses them with caching.

    Skills are returned **deduplicated by ``config.name``** with a first-wins
    policy across ``skills_dirs`` — earlier directories take precedence over
    later ones for collisions. See ``_dedupe_skills_first_wins`` for details.
    """
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

    parsed: list[Skill] = []
    for skill_path in skill_paths:
        skill = parse_skill(skill_path)
        if skill:
            parsed.append(skill)

    skills = _dedupe_skills_first_wins(parsed)

    with _SKILLS_CACHE_LOCK:
        _SKILLS_CACHE[cache_key] = (current_mtime, list(skills))
    return skills


_SKILL_FM_KEY = "disable-model-invocation"


def set_skill_disable_model_invocation(skill_path: Path, disabled: bool) -> None:
    """改写指定 SKILL.md 的 frontmatter 的 ``disable-model-invocation`` 字段,保留 body 不变。

    与 prompts 范式一致——单一事实来源写回文件本身,而不是在 config.yaml 维护
    禁用清单(后者会因 first-wins 去重后 name↔path 漂移而腐烂)。frontmatter 无
    注释,整体重写零信息损失。find_skills 按 mtime 缓存,写文件后自动失效重读。

    幂等:key 与当前值相同则不写。失败抛 ValueError(由上层转 400)。
    """
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读取技能文件失败: {exc}") from exc

    stripped = text.lstrip()
    if not stripped.startswith("---\n"):
        raise ValueError("技能文件缺少有效的 YAML frontmatter(必须以 --- 开头)")
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        raise ValueError("技能文件的 YAML frontmatter 未闭合")

    fm_text = parts[1]
    body_str = parts[2]
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        raise ValueError("技能文件的 frontmatter 必须是 YAML 映射")

    current = fm.get(_SKILL_FM_KEY)
    # YAML 把裸 true/false 解析成 Python bool,统一以 bool 比对。
    current_bool = bool(current)
    if current_bool == disabled:
        return  # 幂等,无需写

    if disabled:
        fm[_SKILL_FM_KEY] = True
    else:
        # 关闭时移除该键,回退到"未声明"语义(同样表示不禁用)。
        fm.pop(_SKILL_FM_KEY, None)

    new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    new_content = f"---\n{new_fm}---\n{body_str}"
    try:
        skill_path.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"写入技能文件失败: {exc}") from exc
