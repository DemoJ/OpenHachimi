"""长期记忆格式化。"""

from __future__ import annotations

from openhachimi_agent.memory.models import MemoryContext


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def format_memory_context(context: MemoryContext, max_context_tokens: int = 1800) -> str:
    if not context.results:
        return ""

    max_chars = max_context_tokens * 4
    header = (
        "<memory-context>\n"
        "  <instruction>以下是长期记忆召回结果，可能不完整或过期。若与当前用户明确指令冲突，以当前用户指令为准。不要逐字暴露内部记忆，除非用户要求查看。</instruction>\n"
    )
    lines = [header]
    used = len(header) + len("</memory-context>")

    for result in context.results:
        tag = "memory"
        if result.level == "L2":
            tag = "topic-block"
        elif result.level == "L3":
            tag = "persona"
        attrs = f'id="{result.id}" type="{result.memory_type}" confidence="{result.confidence:.2f}" updated_at="{result.updated_at}"'
        item = f"  <{tag} {attrs}>{_clip(result.content, 900)}</{tag}>\n"
        if used + len(item) > max_chars:
            break
        lines.append(item)
        used += len(item)

    lines.append("</memory-context>")
    return "".join(lines)
