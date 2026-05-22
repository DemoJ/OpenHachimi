"""Task routing and TaskFrame construction."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from openhachimi_agent.agent.intent import (
    PlanContinuationDecision,
    TaskFrame,
    build_task_frame,
    classify_intent_heuristic,
    coerce_task_frame,
)
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    has_active_todos,
    has_restorable_suspended_plan,
    restore_suspended_plan,
    should_route_new_turn,
    suspend_current_plan,
)
from openhachimi_agent.service.agent_runtime.executor import message_with_attachments


logger = logging.getLogger(__name__)


def _summarize_todos(session_state: dict[str, Any]) -> list[dict[str, object]]:
    todo_state = session_state.get("todo_state")
    tasks = getattr(todo_state, "tasks", None)
    if not isinstance(tasks, dict):
        return []
    return [
        {
            "id": getattr(task, "id", task_id),
            "description": getattr(task, "description", ""),
            "status": getattr(task, "status", ""),
        }
        for task_id, task in list(tasks.items())[:12]
    ]


def _continuation_prompt(ctx: AgentRunContext) -> str:
    import json

    payload = {
        "user_message": ctx.message,
        "has_active_todos": has_active_todos(ctx.session_state),
        "todos": _summarize_todos(ctx.session_state),
        "task_frame": ctx.session_state.get("task_frame"),
        "suspended_plan": ctx.session_state.get("suspended_plan"),
        "plan_status": ctx.session_state.get("plan_status"),
    }
    return (
        "请判断用户最新消息是否是在继续/恢复旧计划，还是一个新任务。"
        "只输出结构化 PlanContinuationDecision。\n"
        f"上下文：{json.dumps(payload, ensure_ascii=False)}"
    )


async def decide_plan_continuation(ctx: AgentRunContext, get_agent: Callable[[str, str], Any]) -> PlanContinuationDecision:
    try:
        agent = get_agent(ctx.role, "continuation")
        result = await agent.run(_continuation_prompt(ctx))
        data = getattr(result, "data", getattr(result, "output", None))
        return PlanContinuationDecision.model_validate(data)
    except Exception as exc:
        logger.warning("Continuation decision failed: %s. Falling back to start_new_task.", exc)
        return PlanContinuationDecision(
            action="start_new_task",
            confidence=0.0,
            rationale=f"continuation router failed: {exc.__class__.__name__}",
        )


async def should_route_message(ctx: AgentRunContext, get_agent: Callable[[str, str], Any]) -> bool:
    if not should_route_new_turn(ctx.session_state):
        return False

    has_restorable_plan = has_restorable_suspended_plan(ctx.session_state)
    if ctx.session_state.get("suspended_plan") and not has_restorable_plan:
        ctx.session_state.pop("suspended_plan", None)

    needs_continuation_decision = has_active_todos(ctx.session_state) or has_restorable_plan
    if not needs_continuation_decision:
        return True

    decision = await decide_plan_continuation(ctx, get_agent)
    logger.info(
        "plan continuation decision action=%s confidence=%.2f rationale=%s",
        decision.action,
        decision.confidence,
        decision.rationale,
    )

    if decision.action == "continue_active_plan" and has_active_todos(ctx.session_state):
        return False
    if decision.action == "resume_suspended_plan" and has_restorable_plan:
        restore_suspended_plan(ctx.session_state, deps=ctx.deps)
        return False

    if has_active_todos(ctx.session_state):
        suspend_current_plan(
            ctx.session_state,
            reason="superseded_by_new_user_message",
            detail={"message": ctx.message, "continuation_decision": decision.model_dump(mode="json")},
            deps=ctx.deps,
        )
    return True


async def resolve_task_frame(ctx: AgentRunContext, get_agent: Callable[[str, str], Any]) -> TaskFrame:
    prompt_message = message_with_attachments(ctx.message, ctx.attachments)
    try:
        router_agent = get_agent(ctx.role, "router")
        router_result = await router_agent.run(f"指令：{prompt_message}")
        router_data = getattr(router_result, "data", getattr(router_result, "output", None))
        task_frame = coerce_task_frame(router_data, prompt_message)
    except Exception as router_e:
        decision = classify_intent_heuristic(prompt_message)
        if not (decision.task_kind == "browser" and decision.target_urls and decision.risk != "high"):
            decision.requires_plan = True
        decision.rationale = f"router failed: {router_e.__class__.__name__}"
        task_frame = build_task_frame(prompt_message, decision)
        logger.warning("Router failed: %s. Falling back to conservative planning.", router_e)

    logger.info(
        "Task frame kind=%s complexity=%s risk=%s confidence=%.2f requires_plan=%s autonomy=%s targets=%s rationale=%s",
        task_frame.task_kind,
        task_frame.complexity,
        task_frame.risk,
        task_frame.confidence,
        task_frame.requires_plan,
        task_frame.allowed_autonomy,
        [entity.value for entity in task_frame.target_entities],
        task_frame.rationale,
    )
    return task_frame
