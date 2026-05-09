"""Skills tools.

This module provides tools for the agent to discover and read
Claude Skills defined in the workspace.
"""

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools.utils import check_prompt_read


def _ensure_skills_prompt_read(ctx: RunContext[AppConfig]) -> None:
    if not check_prompt_read(ctx, "system_prompts/skills.md"):
        raise ModelRetry(
            "🛑 拦截：在操作技能(Skills)前，必须首先调用 read_file 读取 openhachimi_agent/system_prompts/skills.md 了解技能安装与管理工作流。"
        )


def list_skills(ctx: RunContext[AppConfig]) -> str:
    """Lists available Claude Skills for the current project.
    
    Returns:
        A formatted string listing the name, description, when to use, 
        and the file path for each discovered skill.
    """
    _ensure_skills_prompt_read(ctx)
    skills = find_skills(ctx.deps.skills_dirs)
    if not skills:
        return "No skills found in the current project."
        
    result = ["Available skills for this project:"]
    for skill in skills:
        entry = f"- Name: {skill.config.name}\n  Path: {skill.path}\n  Description: {skill.config.description}"
        if skill.config.when_to_use:
            entry += f"\n  When to use: {skill.config.when_to_use}"
        result.append(entry)
        
    return "\n\n".join(result)


def get_skill_instructions(ctx: RunContext[AppConfig], skill_name: str) -> str:
    """Gets the specific markdown instructions for a named skill.
    
    Args:
        skill_name: The exact name of the skill to read (e.g. 'explain-code').
        
    Returns:
        The markdown body of the skill, or an error message if not found.
    """
    _ensure_skills_prompt_read(ctx)
    skills = find_skills(ctx.deps.skills_dirs)
    
    for skill in skills:
        if skill.config.name == skill_name:
            if skill.config.disable_model_invocation:
                return f"Skill '{skill_name}' is marked with disable_model_invocation=true. You should not run this skill directly."
            return skill.body
            
    return f"Skill '{skill_name}' not found. Please check available skills using list_skills."
