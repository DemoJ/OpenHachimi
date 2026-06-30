"""单轮对话输出渲染纯函数。

提供决定流式轮次末尾是否补发 text 事件、把 outcome signal 渲染为自然语言
提示的纯函数。供 ``turn.run_turn`` 调用,无 service 状态依赖,可独立单测。
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def _resolve_terminal_stream_text(
    final_output_text: str,
    result_holder: dict[str, object],
    chunk_count: int,
) -> str:
    """决定流式轮次末尾还要不要补一次 text 事件,以及补什么内容。

    返回空串表示不需要补。

    两个补发场景:
    1. **deferred 路径**(``result_holder`` 含 ``clarification_question``):
       ``clarify_user`` 的 question 文本从来没流过——``streaming.py`` 故意吞掉了
       ``clarify_user`` 的 ``FunctionToolCallEvent``,期望 turn 末尾把 question 当成
       本轮 assistant 回复输出。**必须** 无条件补发,否则用户看到一行工具卡片就突然
       中断,不知道下一步该做什么(过去的 bug:用 ``chunk_count==0`` 兜底,只要模型
       在调 ``clarify_user`` 前流式吐过任何过渡文字,question 就会被静默丢弃)。
    2. **整轮零 chunk**:非 deferred 但 ``chunk_count == 0``,说明模型一段文字都没流过,
       靠 ``result.output`` 字段返回了最终答案——典型于结构化输出或极短回复。

    deferred 场景若前面已有过渡文字,补一个空行让追问从新段落起,避免和前面的解释挤在一起。
    """
    is_clarify_deferred = "clarification_question" in result_holder
    if not (is_clarify_deferred or chunk_count == 0):
        return ""
    if not final_output_text:
        return ""
    if is_clarify_deferred and chunk_count:
        return f"\n\n{final_output_text}"
    return final_output_text


def _format_signal_for_user(signal_key: str, signal_value: object) -> str:
    """把 outcome signal 渲染为给用户看的自然语言提示,而不是把原始 JSON 直接喷
    出来。signal 是给开发者看的内部诊断字段,模型并不需要、用户更看不懂。
    """
    if not isinstance(signal_value, dict):
        return ""
    issues = signal_value.get("issues", []) if isinstance(signal_value, dict) else []
    lines: list[str] = []

    if signal_key == "final_verification_signal":
        unfinished_items: list[dict] = []
        latest_failures: list[dict] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("type") == "unfinished_todos":
                for item in issue.get("items", []):
                    if isinstance(item, dict):
                        unfinished_items.append(item)
            elif issue.get("type") == "latest_execution_not_successful":
                latest_failures.append(issue)

        if unfinished_items:
            lines.append("[System] 有 TODO 尚未完成:")
            for item in unfinished_items[:5]:
                status = item.get("status", "?")
                desc = str(item.get("description", "") or "").strip()
                if len(desc) > 100:
                    desc = desc[:97] + "..."
                lines.append(f"  - [{status}] {desc}")
            if len(unfinished_items) > 5:
                lines.append(f"  …等 {len(unfinished_items)} 项")
        if latest_failures:
            lf = latest_failures[-1]
            tool_name = lf.get("tool_name", "?")
            detail = str(lf.get("detail", "") or "").strip()
            if len(detail) > 200:
                detail = detail[:197] + "..."
            lines.append(f"[System] 最近一次工具调用未成功:`{tool_name}` —— {detail}" if detail else f"[System] 最近一次工具调用未成功:`{tool_name}`")

    return "\n".join(lines)
