"""``clarify_user`` —— 通过 pydantic-ai ``CallDeferred`` 阻断当前 run 的追问工具。

设计参考
========
学自 NousResearch/hermes-agent 的 ``tools/clarify_tool.py``:模型在执行中察觉
"必须由用户提供的信息缺失"时主动调用,把当前 run 阻断,等用户下一轮回复作为
工具结果灌回再继续。Hermes 走的是同步线程 + ``threading.Event.wait()``。

OpenHachimi 是异步事件循环 + pydantic-ai 异步 Agent,搬不动同步阻塞,但
pydantic-ai 1.x 已经提供原生的"工具延迟执行"机制:工具体内
``raise CallDeferred(metadata=...)`` → agent_graph 立刻终止当前 run,把这条
未解析的 tool call 作为 ``DeferredToolRequests`` 输出返回;下一轮调用
``agent.run(None, message_history=..., deferred_tool_results=DeferredToolResults(
calls={tool_call_id: user_reply}))`` 时,graph 跳过 ``ModelRequestNode`` 直接
走 ``CallToolsNode`` 把结果灌回去——对模型来说就像"刚才那个工具返回得特别慢"。

这相当于 Hermes 阻塞方案在异步架构下的精确等价物:
- 当前 run 不会再调任何工具、不会走 final-answer validator;
- 模型物理上没机会"再 emit 第二次 clarify_user 打磨措辞";
- 下一轮 resume 完全无感知,既往的 ``suspend_current_plan`` / ``restore_suspended_plan``
  仍负责 TODO 计划的暂停与恢复(``execute_task_resume`` 接管 resume 调用)。
"""

from __future__ import annotations

import json
import logging

from pydantic_ai import RunContext
from pydantic_ai.exceptions import CallDeferred

from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)


def _normalize_missing_inputs(value: object) -> list[str]:
    """容错把模型给的 missing_inputs 归一为 ``list[str]``。

    问题背景:不少开源模型(GLM/Qwen 等)在生成 tool args 时把 ``list[str]`` 字段
    输出成 JSON 字符串(``"[\"a\", \"b\"]"``),pydantic-ai 的 schema 校验拒收,
    在同一 planner.run() 内连续 retry 多次,既浪费 token 又给用户造成"我看到
    模型反复在调 clarify_user"的错觉(实际是 retry 而非真正执行到工具体)。

    在 schema 层把类型放宽到 ``list[str] | str | None``,函数内做归一化:
    - None / "" → []
    - list[str] → 去空白 + 丢空串
    - str:尝试当 JSON 解;失败时按"逗号/中文逗号/换行"切分;再不行整段当 1 项
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        # 优先按 JSON 解析(模型给字符串化 list 是最常见情况)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            if isinstance(parsed, str) and parsed.strip():
                return [parsed.strip()]
        except (json.JSONDecodeError, TypeError):
            pass
        # 退到分隔符切分
        for sep in (",", "，", "、", "\n", ";", "；"):
            if sep in text:
                return [item.strip() for item in text.split(sep) if item.strip()]
        return [text]
    # 其他类型(int/dict 等)按 str 兜底
    coerced = str(value).strip()
    return [coerced] if coerced else []


def clarify_user(
    ctx: RunContext[AgentDeps],
    question: str,
    missing_inputs: list[str] | str | None = None,
) -> str:
    """当你执行中发现必须由用户提供的信息缺失(凭据、账号、目标确认、二选一决策等)
    且**无法通过工具自行获取**时,调用此工具一次性把所有缺失项问清楚。

    调用后系统会:
    1. 把当前未完成的活动计划挂起(下一轮用户回答后自然 resume);
    2. **立刻终止本轮 agent.run()**,无需再 emit 任何文字、无需再调任何工具;
    3. ``question`` 参数本身就是要发给用户的追问,系统会自动呈现给用户;
    4. 用户下一轮的回复会作为本次工具调用的返回值无感知地灌回,模型继续工作。

    禁止滥用:
    - 能用 read_file / list_files / run_command 自查的信息**不要**问用户;
    - **不要**在已经能确定下一步动作时为求心安调用此工具;
    - 调用之后**不要**再 emit 任何文字——``question`` 本身就是给用户看的。

    参数:
    - question: 给用户的自然语言追问,清楚说明你需要什么、为什么需要。
    - missing_inputs: 可选,简短罗列每一项缺失输入的名称(如 ["发件人邮箱",
      "SMTP 授权码"]),帮助用户一次性提供齐全。
    """
    cleaned_question = (question or "").strip()
    if not cleaned_question:
        # 抛 ValueError 让 pydantic-ai 的工具校验把它转 ModelRetry,模型有机会
        # 在同一 run 内补一次合法调用,而不是把 run 抛废。不抛 CallDeferred
        # 是因为空 question 没法呈现给用户。
        raise ValueError(
            "clarify_user 的 question 不能为空。请提供一段自然语言追问,清楚说明"
            "你需要什么、为什么需要。"
        )

    cleaned_missing = _normalize_missing_inputs(missing_inputs)

    # 延迟 import:tools 包初始化期间 service.agent_runtime.context 还没就绪。
    from openhachimi_agent.service.agent_runtime.context import (
        has_active_todos,
        suspend_current_plan,
    )

    session_state = ctx.deps.session_state

    # 写 session_state:
    # - turn.py 在看到 DeferredToolRequests 输出时读 question 当本轮 assistant 回复;
    # - execute_task_resume 在下一轮读 tool_call_id 构造 DeferredToolResults。
    session_state["_user_clarification"] = {
        "question": cleaned_question,
        "missing_inputs": cleaned_missing,
        "tool_call_id": ctx.tool_call_id,
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
        "clarify_user invoked session_id=%s tool_call_id=%s missing=%s question=%r",
        ctx.deps.session_id,
        ctx.tool_call_id,
        cleaned_missing,
        cleaned_question[:120],
    )

    # 关键:抛 CallDeferred 让 pydantic-ai 的 agent_graph 把这次 tool call 标记为
    # "external"(等外部回灌结果)、把 DeferredToolRequests 作为 run 的 output 返回,
    # 当前 run 在此刻终止。模型不会被询问要不要 emit 别的文字、不会再调任何工具,
    # final-answer validator / self_critique 等下游裁判全都跑不到。
    raise CallDeferred(
        metadata={
            "kind": "clarify_user",
            "question": cleaned_question,
            "missing_inputs": cleaned_missing,
        }
    )
