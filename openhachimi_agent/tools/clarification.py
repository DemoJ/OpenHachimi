"""``clarify_user`` —— 主动追问用户的 agent-level intercept 工具。

设计参考
========
学自 NousResearch/hermes-agent 的 ``tools/clarify_tool.py``:
当模型在执行中察觉"必须由用户提供的信息缺失",由模型主动调用此工具触发本轮中止
+ 自然语言追问,而不是 router 在一开始就预判(router 看不到环境变量、MCP 配置、
文件系统,无法准确判断"凭据是否已存在")。

OpenHachimi 不照搬 Hermes 的同步 callback(多渠道异步事件循环架构不兼容),而是
依托既有的 ``suspend_current_plan`` / continuation decision 链路:
1. 调用 ``clarify_user`` 时把澄清问题写到 ``session_state["_user_clarification"]``;
2. 若有活动计划,挂起;
3. ``factory._validate_execution_result`` 在该 flag 存在时直接放行,模型可输出
   一段自然语言把问题告知用户后结束本轮;
4. 用户下一轮回答时,``router._continuation_prompt`` 把这个 pending 状态注入
   continuation agent,让其倾向 ``resume_suspended_plan``;
5. ``mark_turn_finished`` 不主动清除该字段——清理时机在下一轮的
   ``should_route_message`` 内,或下次 ``clarify_user`` 被覆盖时。

不抛 ``ModelRetry``——一抛 validator 会把这次调用本身当 retry 触发又转回来。
"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)


def clarify_user(
    ctx: RunContext[AgentDeps],
    question: str,
    missing_inputs: list[str] | None = None,
) -> str:
    """当你执行中发现必须由用户提供的信息缺失(凭据、账号、目标确认、二选一决策等)
    且**无法通过工具自行获取**时,调用此工具一次性把所有缺失项问清楚。

    调用后系统会:
    1. 把当前未完成的活动计划挂起(下一轮用户回答后自然 resume);
    2. 让本轮 final-answer validator 放行;
    3. 你接下来应立即给用户一段简短自然语言,复述 question(可补充少量背景说明
       为什么需要这些信息)。

    禁止滥用:
    - 能用 read_file / list_files / run_command 自查的信息**不要**问用户。
    - **不要**把"我接下来要做 X"的进度汇报伪装成 question。
    - **不要**在已经能确定下一步动作时为求心安调用此工具。

    参数:
    - question: 给用户的自然语言追问,清楚说明你需要什么、为什么需要。
    - missing_inputs: 可选,简短罗列每一项缺失输入的名称(如 ["发件人邮箱",
      "SMTP 授权码"]),帮助用户一次性提供齐全。
    """
    cleaned_question = (question or "").strip()
    if not cleaned_question:
        return (
            "错误:question 不能为空。请提供一段自然语言追问,清楚说明你需要什么、"
            "为什么需要。"
        )

    cleaned_missing = [str(item).strip() for item in (missing_inputs or []) if str(item).strip()]

    # 延迟 import:tools 包初始化期间 service.agent_runtime.context 还没就绪。
    from openhachimi_agent.service.agent_runtime.context import (
        has_active_todos,
        suspend_current_plan,
    )

    session_state = ctx.deps.session_state

    # 本轮幂等防御:模型常把 clarify_user 当"草稿区"反复打磨问题措辞,连续 3-5 次
    # 调用语义几乎一样的 question。每次重复都会:浪费 LLM token + 重写 session 标志
    # + 再 suspend(已挂起的计划),且 UI 会渲染多个相同条目。
    # 第二次起直接返回错误字符串(不抛 ModelRetry 避免计入 retry 预算),让模型立刻
    # 转向输出给用户的自然语言。
    existing = session_state.get("_user_clarification")
    if isinstance(existing, dict) and existing.get("question"):
        prior = str(existing.get("question", ""))
        snippet = prior if len(prior) <= 80 else prior[:77] + "..."
        logger.info(
            "clarify_user duplicate call ignored session_id=%s prior_q=%r new_q=%r",
            ctx.deps.session_id,
            prior[:80],
            cleaned_question[:80],
        )
        return (
            f"[clarify_user 本轮已调用过] 已登记的待澄清问题:{snippet}\n"
            f"**不要**再调用 clarify_user 打磨措辞,也**不要**再调任何执行类工具。"
            f"立刻输出一段自然语言把上面这个问题告诉用户,本轮即可结束。"
        )

    raised_at_seq = int(session_state.get("current_turn_ledger_start_seq", 0) or 0)
    session_state["_user_clarification"] = {
        "question": cleaned_question,
        "missing_inputs": cleaned_missing,
        "raised_at_seq": raised_at_seq,
    }

    if has_active_todos(session_state):
        suspend_current_plan(
            session_state,
            reason="awaiting_user_clarification",
            detail={
                "question": cleaned_question,
                "missing_inputs": cleaned_missing,
            },
            deps=ctx.deps,
        )

    logger.info(
        "clarify_user invoked session_id=%s missing=%s question=%r",
        ctx.deps.session_id,
        cleaned_missing,
        cleaned_question[:120],
    )

    snippet = cleaned_question if len(cleaned_question) <= 80 else cleaned_question[:77] + "..."
    return (
        f"[已记录待澄清,本轮将结束] 请用一句自然语言把这个问题告知用户:{snippet}\n"
        f"系统已挂起当前活动计划(若有);用户下一轮回答即可继续。**不要**再调任何其他"
        f"执行类工具,**不要**再次调用 clarify_user 打磨措辞,直接输出给用户的提问文字即可。"
    )
