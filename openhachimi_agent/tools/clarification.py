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
import time

from pydantic_ai import RunContext
from pydantic_ai.exceptions import CallDeferred

from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)

# 最多 4 个预设选项(对齐 Hermes clarify_tool.MAX_CHOICES)。UI 渲染成可点选行,
# 第 5 个"其它(自行输入)"由前端自动追加。超过 4 个截断保留前 4 个。
_MAX_CHOICES = 4


def _flatten_choice(c: object) -> str:
    """把单个 choice 强制归一为用户可见的展示字符串。

    schema 声明 choices 为纯字符串数组,但 LLM 有时输出 dict 形
    (如 ``[{"description": "..."}]``)。直接 ``str(c)`` 会把整个 dict 变成 Python
    repr(``{'description': '...'}``)——这会泄露到每个渲染面(CLI/消息渠道/UI)
    并且原样当作用户的答案回灌。在这里(唯一平台无关入口)统一 unwrap。

    dict 取键顺序:label → description → text → title(对齐 Hermes);name/value
    故意排除(它们常带枚举值或短标识,不是人读标签)。无任何已知键的 dict 丢弃
    (返回空——垃圾标签不如没标签)。
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def _normalize_choices(value: object) -> list[str]:
    """容错把模型给的 choices 归一为 ``list[str]``(最多 ``_MAX_CHOICES`` 个)。

    - None / [] → [](开放问答,无预设选项)
    - list → 逐项 _flatten_choice,丢空串,截断到 _MAX_CHOICES
    - str → 尝试 JSON 解(模型常给字符串化 list);失败则按分隔符切分
    """
    if value is None:
        return []
    if isinstance(value, list):
        flat = [s for s in (_flatten_choice(c) for c in value) if s]
        return flat[:_MAX_CHOICES]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                flat = [s for s in (_flatten_choice(c) for c in parsed) if s]
                return flat[:_MAX_CHOICES]
            if isinstance(parsed, str) and parsed.strip():
                return [parsed.strip()][:_MAX_CHOICES]
        except (json.JSONDecodeError, TypeError):
            pass
        for sep in (",", "，", "、", "\n", ";", "；"):
            if sep in text:
                flat = [s.strip() for s in text.split(sep) if s.strip()]
                return flat[:_MAX_CHOICES]
        return [text][:_MAX_CHOICES]
    return []


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
    choices: list[str] | str | None = None,
) -> str:
    """当你执行中发现需要用户介入才能继续时,调用此工具一次性问清。支持两种模式:

    1. **多选**:提供最多 4 个 ``choices``,用户选其一(前端会自动加第 5 个"其它
       (自行输入)")。适合需要用户在多个方案/选项间决策的场景。
    2. **开放问答**:省略 ``choices``,用户自由文本回复。适合需要用户提供凭据、
       账号、确认信息等开放输入的场景。

    何时该用(对齐 Hermes clarify 的 4 类场景):
    - 任务歧义,需要用户选一个方向(用 choices)
    - 需要凭据/账号/确认等只有用户知道的信息(开放问答)
    - 某步有多个可行方案、且 trade-off 需要用户权衡(用 choices)
    - 想在继续前确认一个关键决策(用 choices)

    何时**不要**用:
    - 能用 read_file / list_files / run_command 自查的信息——先自查。
    - 低风险决策——自己选一个合理默认值,别为求心安打断用户。
    - 已经能确定下一步动作时不要调用此工具。

    调用后系统会:
    1. 把当前未完成的活动计划挂起(下一轮用户回答后自然 resume);
    2. **立刻终止本轮 agent.run()**,无需再 emit 任何文字、无需再调任何工具;
    3. ``question`` 参数本身就是要发给用户的追问,系统会自动呈现给用户
       (若提供 ``choices``,前端渲染成可点选项);
    4. 用户下一轮的回复会作为本次工具调用的返回值无感知地灌回,模型继续工作。

    参数:
    - question: 给用户的自然语言追问,清楚说明你需要什么、为什么需要。
      **不要**把选项文本写进 question——选项放进 ``choices`` 数组,question 只放
      问题本身(如"部署到哪个环境?",choices=["staging","prod"])。
    - missing_inputs: 可选,简短罗列每一项缺失输入的名称(如 ["发件人邮箱",
      "SMTP 授权码"]),帮助用户一次性提供齐全。
    - choices: 可选,最多 4 个预设选项。提供时为多选模式;省略为开放问答模式。
      每个 choice 是一个字符串(也接受 dict 形如 {"description":"..."} 会自动取值)。
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
    cleaned_choices = _normalize_choices(choices)

    # 延迟 import:tools 包初始化期间 service.agent_runtime.context 还没就绪。
    from openhachimi_agent.service.agent_runtime.context import (
        has_active_todos,
        suspend_current_plan,
    )

    session_state = ctx.deps.session_state

    # 写 session_state:
    # - turn.py 在看到 DeferredToolRequests 输出时读 question 当本轮 assistant 回复;
    # - execute_task_resume 在下一轮读 tool_call_id 构造 DeferredToolResults;
    # - choices 供前端渲染可点选项(created_at 供超时兜底判断)。
    clarification_entry: dict[str, object] = {
        "question": cleaned_question,
        "missing_inputs": cleaned_missing,
        "tool_call_id": ctx.tool_call_id,
        "created_at": time.time(),
    }
    if cleaned_choices:
        clarification_entry["choices"] = cleaned_choices
    session_state["_user_clarification"] = clarification_entry

    if has_active_todos(session_state):
        suspend_current_plan(
            session_state,
            reason="awaiting_user_clarification",
            detail={
                "question": cleaned_question,
                "missing_inputs": cleaned_missing,
                "choices": cleaned_choices,
            },
            deps=ctx.deps,
        )

    logger.info(
        "clarify_user invoked session_id=%s tool_call_id=%s missing=%s choices=%d question=%r",
        ctx.deps.session_id,
        ctx.tool_call_id,
        cleaned_missing,
        len(cleaned_choices),
        cleaned_question[:120],
    )

    # 关键:抛 CallDeferred 让 pydantic-ai 的 agent_graph 把这次 tool call 标记为
    # "external"(等外部回灌结果)、把 DeferredToolRequests 作为 run 的 output 返回,
    # 当前 run 在此刻终止。模型不会被询问要不要 emit 别的文字、不会再调任何工具,
    # final-answer validator / self_critique 等下游裁判全都跑不到。
    metadata: dict[str, object] = {
        "kind": "clarify_user",
        "question": cleaned_question,
        "missing_inputs": cleaned_missing,
    }
    if cleaned_choices:
        metadata["choices"] = cleaned_choices
    raise CallDeferred(metadata=metadata)
