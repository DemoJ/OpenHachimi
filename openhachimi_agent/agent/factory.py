"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import DeferredToolRequests

from openhachimi_agent.content.prompts import load_system_prompt, render_system_prompt
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools import PLANNER_TOOLSET, EXECUTOR_TOOLSET, SCHEDULED_EXECUTOR_TOOLSET
from openhachimi_agent.agent.intent import PlanContinuationDecision, TaskFrame


logger = logging.getLogger(__name__)


def should_pass_through_validation(signal: dict | None, session_state: dict) -> str | None:
    """检测 final-answer validator 是否应短路放行。返回放行原因(用于日志),否则 None。

    放行路径:所有未完成任务都显式标 ``blocked``,且最近无工具失败 —— 模型不是
    乱标 done 而是诚实声明卡点。在 ledger 写入之前判断,避免污染
    ``_replan_after_execution_signal`` 的连续 blocked 检测。

    历史上还有一条"_user_clarification 已写入则放行"的分支。该分支随 clarify_user
    切到 ``CallDeferred`` 机制后已无意义:抛 CallDeferred 后 run 在 graph 层立刻
    终止,output 是 ``DeferredToolRequests`` 而非 ``str``,validator 整段都不会
    被触发。
    """
    if not signal:
        return None

    issues = signal.get("issues", []) if isinstance(signal, dict) else []
    has_latest_failure = any(
        isinstance(issue, dict) and issue.get("type") == "latest_execution_not_successful"
        for issue in issues
    )
    unfinished_issues = [
        issue for issue in issues
        if isinstance(issue, dict) and issue.get("type") == "unfinished_todos"
    ]
    if unfinished_issues and not has_latest_failure:
        all_items = [
            item for issue in unfinished_issues
            for item in issue.get("items", [])
            if isinstance(item, dict)
        ]
        if all_items and all(item.get("status") == "blocked" for item in all_items):
            return f"all unfinished todos blocked (count={len(all_items)})"
    return None


def _build_base_agent(config: AppConfig, role_name: str, agent_type: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None) -> Agent:
    if not config.openai_api_key:
        raise ValueError("未配置 llm.api_key，请先在 user/config.yaml 中填写 API Key。")

    logger.info(
        "building %s agent role=%s model=%s base_url_configured=%s",
        agent_type,
        role_name,
        config.model_name,
        bool(config.openai_base_url),
    )

    system_prompt = load_system_prompt()
    role_content = load_role_content(config.roles_dir, role_name)

    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    
    from pydantic_ai import FunctionToolset
    from openhachimi_agent.tools.skills import build_skill_tool, format_skill_prompt
    
    # 动态扫描带 arguments 的技能并注册为宏工具
    dynamic_skill_tools = []
    skills = find_skills(config.skills_dirs)
    for skill in skills:
        if skill.config.arguments and not skill.config.disable_model_invocation:
            dynamic_skill_tools.append(build_skill_tool(skill))
            
    dynamic_toolset = FunctionToolset(tools=dynamic_skill_tools)

    mcp_toolsets = mcp_toolsets or []

    # output_type 按 agent 类型分:
    # - executor:注册了 clarify_user(可能抛 CallDeferred),所以输出域是
    #   ``[str, DeferredToolRequests]``。
    # - planner:把 ``create_todos`` 标成 output tool —— 模型调它即视为本次 run 的
    #   final answer,graph 在工具执行后立刻终止,**不会再发起第 2 步 LLM 调用
    #   让模型 emit 一段重复的"执行步骤概览"自然语言**。read_file / list_files
    #   等只读调研工具仍可被普通调用,不影响 planner 先调研后规划的工作流。
    #   (改造前用 ``output_type=str``,模型必须额外 emit 一段 final text 才能
    #    结束 run,与刚刚调过的 create_todos 工具卡片内容完全重复。)
    # - scheduled_executor:无人值守路径,直接 ``str`` fail-fast。
    if agent_type == "executor":
        output_type: object = [str, DeferredToolRequests]
    elif agent_type == "planner":
        from pydantic_ai.output import ToolOutput
        from openhachimi_agent.tools.planning import create_todos

        # name="create_todos" 让该 output tool 复用现成函数签名 / docstring。
        # max_retries 默认沿用 agent.retries,无需再单独指定。
        output_type = ToolOutput(create_todos, name="create_todos")
    else:
        output_type = str

    if agent_type == "planner":
        toolsets = [PLANNER_TOOLSET, dynamic_toolset] + mcp_toolsets
        extra_prompt = load_system_prompt("agents/planner")
    else:
        base_executor_toolset = SCHEDULED_EXECUTOR_TOOLSET if agent_type == "scheduled_executor" else EXECUTOR_TOOLSET
        filtered_executor_toolset = base_executor_toolset
        if allowed_tools is not None:
            filtered_tools = [
                t for t in base_executor_toolset.tools
                if getattr(t, "__name__", "") in allowed_tools or getattr(t, "name", "") in allowed_tools
            ]
            filtered_executor_toolset = FunctionToolset(tools=filtered_tools)

        toolsets = [filtered_executor_toolset, dynamic_toolset] + mcp_toolsets
        if agent_type == "scheduled_executor":
            extra_prompt = load_system_prompt("agents/scheduled_executor")
        else:
            extra_prompt = load_system_prompt("agents/executor")

    agent = Agent(
        OpenAIChatModel(config.model_name, provider=provider),
        system_prompt=system_prompt + "\n\n" + extra_prompt,
        instructions=role_content,
        deps_type=AgentDeps,
        toolsets=toolsets,
        output_type=output_type,
        defer_model_check=True,
        # retries 既覆盖工具调用错误,也覆盖 output_validator 抛出的 ModelRetry。
        # 一次工具偶发失败(1) + 一次 final-answer validator 打回(1) + 一次 schema
        # 修正(1) 已耗光 retries=3 → UnexpectedModelBehavior。这里给到 5 是为
        # validator 重试链留出预算,真正的死循环熔断由 validator 内部计数器和
        # executor.py 的 replan 兜底负责,不能只靠抬上限。
        retries=5,
    )

    if agent_type in {"executor", "scheduled_executor"}:
        # 同一轮内 validator 连续打回的次数,用于硬熔断。在 execute_task 入口处
        # 会清零(见 service.agent_runtime.executor.execute_task)。
        VALIDATOR_RETRY_KEY = "_final_validator_retries"
        VALIDATOR_HARD_LIMIT = 2  # 第 0、1 次打回,第 2 次强制放行避免 UnexpectedModelBehavior

        @agent.output_validator
        def _validate_execution_result(ctx: RunContext[AgentDeps], result: str) -> str:
            from openhachimi_agent.agent.execution import (
                _append_ledger_event,
                get_final_verification_signal,
            )
            import json
            from pydantic_ai.exceptions import ModelRetry

            signal = get_final_verification_signal(ctx.deps.session_state)
            if not signal:
                return result

            session_state = ctx.deps.session_state

            # 短路放行:见 should_pass_through_validation 文档(在 ledger 写入之前判断,
            # 避免污染连续 blocked 检测)。
            passthrough_reason = should_pass_through_validation(signal, session_state)
            if passthrough_reason:
                logger.info(
                    "validator pass-through: %s (session=%s)",
                    passthrough_reason,
                    ctx.deps.session_id,
                )
                session_state["_final_validator_yielded"] = True
                session_state["_final_validator_last_signal"] = signal
                return result

            counter = int(session_state.get(VALIDATOR_RETRY_KEY, 0) or 0)

            # 同时把"validator 打回"记入 execution_ledger,让上层 executor.py 的
            # _replan_after_execution_signal 能感知到这条卡死的链条,在合适时机
            # 触发 replan(get_replan_signal 会看到连续 blocked 事件)。
            try:
                _append_ledger_event(
                    session_state,
                    tool_name="<final_answer_validator>",
                    status="blocked",
                    args={"attempt": counter + 1},
                    result=signal,
                    violation=(
                        json.dumps(signal, ensure_ascii=False)[:500]
                        if signal
                        else ""
                    ),
                )
            except Exception:
                logger.debug("failed to append validator event to ledger", exc_info=True)

            # 硬熔断:已经被打回 >=VALIDATOR_HARD_LIMIT 次,放行,把模型的话给用户。
            # 兜底"无解任务"场景(工具/权限缺失、用户输入不完整、模型坚持自己已完成等),
            # 避免 UnexpectedModelBehavior 把对话整轮报废。模型本来想说的话会和
            # 系统追加的"[System] 任务未完成"提示一起返回,由 executor.py 决定追加方式。
            if counter >= VALIDATOR_HARD_LIMIT:
                logger.warning(
                    "final answer validator yielding after %d retries to avoid loop "
                    "(session=%s); raw signal=%s",
                    counter,
                    ctx.deps.session_id,
                    json.dumps(signal, ensure_ascii=False)[:200],
                )
                session_state["_final_validator_yielded"] = True
                session_state["_final_validator_last_signal"] = signal
                return result

            session_state[VALIDATOR_RETRY_KEY] = counter + 1

            # 把未完成的 TODO 详情直接列在 ModelRetry 里。原版只说"请调用 update_todo
            # 将所有完成的任务状态更新为 done"——但任务从未真正完成,模型按指示
            # 标 done 等同于撒谎;不按又陷入死循环。新版给出具体可操作的下一步,
            # 让模型从"该不该标 done"转向"先 get_todos 看清状态、再 in-progress、
            # 再调真实工具",打破认知误区。
            issues = signal.get("issues", []) if isinstance(signal, dict) else []
            unfinished_lines: list[str] = []
            latest_failure_lines: list[str] = []
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                if issue.get("type") == "unfinished_todos":
                    for item in issue.get("items", [])[:10]:
                        if isinstance(item, dict):
                            unfinished_lines.append(
                                f"  - [{item.get('status', '?')}] #{item.get('id', '?')} {item.get('description', '')}"
                            )
                elif issue.get("type") == "latest_execution_not_successful":
                    latest_failure_lines.append(
                        f"  - 上一次 `{issue.get('tool_name', '?')}` 以 `{issue.get('status', '?')}` 结束："
                        f"{(issue.get('detail') or '')[:300]}"
                    )

            parts = [
                f"[系统拦截] 你不能现在就给用户最终回复——当前轮的任务尚未真正完成（第 {counter + 1} 次拦截）。",
            ]
            if unfinished_lines:
                parts.append("未完成的 TODO：\n" + "\n".join(unfinished_lines))
            if latest_failure_lines:
                parts.append("最近一次工具调用没有成功：\n" + "\n".join(latest_failure_lines))
            parts.append(
                "请按以下顺序操作，**不要给用户回复任何文字**：\n"
                "1. 调用 `get_todos` 查看完整列表；\n"
                "2. 挑出第一个 status=pending 且依赖已 done 的任务，"
                "用 `update_todo(id, \"in-progress\")` 标记；\n"
                "3. 调用相应执行工具（write_file/run_command/web_fetch/research_sources 等）真正完成它；\n"
                "4. 完成后 `update_todo(id, \"done\", notes=...)`，再继续下一项；\n"
                "5. 如果某项确实**无法**继续（外部条件缺失、需要用户决策），"
                "用 `update_todo(id, \"blocked\", notes=\"原因\")` 明确标记，"
                "然后在最终回复里清楚告知用户：已完成什么 / 卡在哪 / 需要什么——"
                "不要装作完成。"
            )
            parts.append(
                "禁止在文字里模仿工具返回格式（如 \"✅ 任务 X → done\"）；"
                "系统能区分真假，假陈述会再次被打回。"
            )

            raise ModelRetry("\n\n".join(parts))

    @agent.system_prompt
    def _config_prompt(ctx: RunContext[AgentDeps]) -> str:
        return render_system_prompt("runtime/config", {"user_dir": str(config.user_dir).replace("\\", "/")}) + "\n"

    # 每轮易变运行时上下文（当前时间 / TaskFrame 摘要 / 长期记忆召回 / 命中技能定义）
    # 通过 pydantic-ai 的 @agent.system_prompt 动态钩子注入 system prompt 末尾。
    #
    # 之所以放 system prompt 而不是 user-prompt：
    # - 这些内容在语义上属于"系统给模型的上下文"，不是用户说的话。如果塞到
    #   user-prompt 前缀，capture_turn_memories 会把"<memory-context>...<SKILL>..."
    #   也作为"用户输入"抽进长期记忆，雪球越滚越大。
    # - system prompt 是按 token 前缀渐进式命中 KV cache 的，前面稳定主体仍能
    #   完整命中；只损失末尾几十~几百 token，远低于把万字 SKILL 塞进 user-prompt
    #   的代价。
    # - 动态钩子每次 run 都重新计算，跨天/跨长会话仍能拿到当下时间和当下记忆，
    #   不会出现"几天后模型还以为是几天前"的问题。
    @agent.system_prompt
    def _runtime_dynamic_block(ctx: RunContext[AgentDeps]) -> str:
        try:
            from openhachimi_agent.content.runtime_context import build_system_dynamic_block

            return build_system_dynamic_block(ctx.deps)
        except Exception:  # noqa: BLE001  动态注入失败不应阻断 agent run
            logger.exception("runtime dynamic block failed")
            return ""

    # executor agent 额外的按需块(TODO 接力 / direct-mode / 技能索引):
    # 把过去恒定写在 executor.md 里的几大段策略按 session 状态按需注入,并永远
    # 附上一份"技能索引"(name + 一句话用途,按 category 分组),让主模型自主
    # 决定要不要调 get_skill_instructions 拉某个 skill 的全文。planner /
    # scheduled_executor 各自的角色文档已经明确职责,这套块不在它们身上注册。
    if agent_type == "executor":
        @agent.system_prompt
        def _executor_extra_block(ctx: RunContext[AgentDeps]) -> str:
            try:
                from openhachimi_agent.content.runtime_context import build_executor_extra_dynamic_block

                return build_executor_extra_dynamic_block(ctx.deps)
            except Exception:  # noqa: BLE001
                logger.exception("executor extra dynamic block failed")
                return ""

    return agent


def build_planner_agent(config: AppConfig, role_name: str, mcp_toolsets: list | None = None) -> Agent:
    """创建专职规划的 Agent。"""
    return _build_base_agent(config, role_name, "planner", mcp_toolsets=mcp_toolsets)


def build_executor_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None) -> Agent:
    """创建专职执行的 Agent（拥有所有权限）。"""
    return _build_base_agent(config, role_name, "executor", allowed_tools=allowed_tools, mcp_toolsets=mcp_toolsets)


def build_scheduled_executor_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None) -> Agent:
    """创建定时任务执行 Agent（不暴露调度写入工具）。"""
    return _build_base_agent(config, role_name, "scheduled_executor", allowed_tools=allowed_tools, mcp_toolsets=mcp_toolsets)


def _build_router_model(config: AppConfig) -> OpenAIChatModel:
    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    return OpenAIChatModel(config.model_name, provider=provider)


def build_router_agent(config: AppConfig) -> Agent:
    """创建用于路由决策的轻量级 Agent。

    渐进披露改造后 router 不再做 skill 召回 —— skill 选择已下放给主模型在 executor
    阶段通过 ``get_skill_instructions`` 自助决定。router 只负责产出 task_kind /
    complexity / risk / requires_plan / execution_mode 等 TaskFrame 元字段。
    """
    model = _build_router_model(config)
    system_prompt = load_system_prompt("agents/router")
    return Agent(
        model,
        system_prompt=system_prompt,
        output_type=TaskFrame,
    )


def build_continuation_agent(config: AppConfig) -> Agent:
    """创建用于判断用户是否要继续旧计划的轻量级 Agent。"""
    system_prompt = load_system_prompt("agents/continuation")
    return Agent(
        _build_router_model(config),
        system_prompt=system_prompt,
        output_type=PlanContinuationDecision,
    )
