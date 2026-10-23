"""长期记忆格式化。"""

from __future__ import annotations

from html import escape

from openhachimi_agent.memory.models import MemoryContext


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _date_only(value: str) -> str:
    """ISO 时间只保留日期部分(YYYY-MM-DD);不是 ISO 格式的原样返回。"""
    return value[:10] if len(value) >= 10 and value[4:5] == "-" else value


def format_memory_context(context: MemoryContext, max_context_tokens: int = 1800) -> str:
    if not context.results:
        return ""

    max_chars = max_context_tokens * 4
    header = (
        "<memory-context>\n"
        "  <instruction>以下是长期记忆召回结果，可能不完整或过期。若与当前用户明确指令冲突，以当前用户指令为准。不要逐字暴露内部记忆，除非用户要求查看。这里只列出常用记忆和与当前消息相关的记忆，需要其他记忆时调用 search_memory 查询。</instruction>\n"
    )
    lines = [header]
    used = len(header) + len("</memory-context>")

    for result in context.results:
        tag = "memory"
        if result.level == "L2":
            tag = "topic-block"
        elif result.level == "L3":
            tag = "persona"
        # 只给模型用得上的属性:id(update / forget 要用)、类型、日期,有期限的再带上期限。
        # 召回分数、来源、访问次数这类诊断字段对模型没用,每条都带会白白占掉上下文。
        attrs = {
            "id": result.id,
            "type": result.memory_type,
            "updated": _date_only(result.updated_at),
        }
        valid_until = result.metadata.get("valid_until")
        if valid_until:
            attrs["valid_until"] = _date_only(str(valid_until))
        attr_text = " ".join(f'{key}="{escape(str(value), quote=True)}"' for key, value in attrs.items())
        item = f"  <{tag} {attr_text}>{escape(_clip(result.content, 900))}</{tag}>\n"
        if used + len(item) > max_chars:
            break
        lines.append(item)
        used += len(item)

    lines.append("</memory-context>")
    return "".join(lines)
