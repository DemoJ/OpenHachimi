"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import DeferredToolRequests

from openhachimi_agent.content.prompts import load_system_prompt, render_system_prompt
from openhachimi_agent.content.role_filters import filter_mcp_toolsets_for_role, filter_skills_for_role, get_role_filters
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools import MAIN_TOOLSET


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


def _build_base_agent(config: AppConfig, role_name: str, agent_type: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None, run_mode: str = "interactive") -> Agent:
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
    # 角色级 skills/MCP 绑定:未配置的角色 get_role_filters 返回全 all(= 历史全局行为)。
    role_filters = get_role_filters(config, role_name)

    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )

    from pydantic_ai import FunctionToolset
    from openhachimi_agent.tools.skills import build_skill_tool, format_skill_prompt

    # 动态扫描带 arguments 的技能并注册为宏工具;先按角色绑定过滤可见 skill 集。
    # (与 runtime_context._skills_index_block 的过滤一致,两处共用 role_filters 逻辑。)
    dynamic_skill_tools = []
    skills = filter_skills_for_role(role_filters, find_skills(config.skills_dirs))
    for skill in skills:
        if skill.config.arguments and not skill.config.disable_model_invocation:
            dynamic_skill_tools.append(build_skill_tool(skill))

    dynamic_toolset = FunctionToolset(tools=dynamic_skill_tools)

    # mcp_toolsets 由 service 传入为带名列表 [(server_name, ts), ...];
    # 按角色绑定过滤后只取实例加入 toolsets。未选中的 server 仍保持连接,
    # 只是不对该角色暴露。
    mcp_toolsets = mcp_toolsets or []
    filtered_mcp = filter_mcp_toolsets_for_role(role_filters, mcp_toolsets)
    mcp_instances = [ts for _name, ts in filtered_mcp]

    # ── 主 agent / subagent 两分支 ──────────────────────────────────────
    # Hermes 式重构后只有两类 agent:
    # - main:单一主 agent,持全套工具(含 create_todos/clarify_user/delegate_task),
    #   output_type=[str, DeferredToolRequests](兼容 clarify_user 的 CallDeferred)。
    #   todo 是普通工具,主 agent 自主决定要不要建计划;停止时由 output_validator
    #   闸门(verification_stop + final_verification_signal)校验完整性。
    # - subagent:delegate_task 委派的子 agent,全新会话零记忆,str 输出,
    #   主工具集运行时由 resolve_child_toolsets 裁剪注入,构建时只挂骨架。
    #
    # scheduled 模式不再单独建 agent 实例:走 main agent,注入 scheduled_executor
    # prompt(见下方 main 分支);scheduler 写工具由 scheduler 工具自身的
    # ``ensure_scheduler_mutation_allowed`` 在 run_mode=scheduled 时拦截。
    if agent_type == "main":
        output_type: object = [str, DeferredToolRequests]
    else:  # subagent
        output_type = str

    if agent_type == "subagent":
        # 通用子 agent(对齐 hermes delegate_task 委派的 child)。
        # 构建时只挂骨架工具集(dynamic_toolset 角色技能 + mcp),主工具集(read/file/web/
        # browser/terminal 等)不在此固定——运行时由 delegate_task 经 resolve_child_toolsets
        # 按 toolsets 参数裁剪后,通过 child.run(..., toolsets=[resolved]) 注入。
        # 隔离哲学:子 agent 全新会话、零记忆、独立预算(见 agent/subagents.py)。
        # 不注册 output_validator(subagent 走 str fail-fast,不像主 agent 那样校验 TODO)。
        toolsets = [dynamic_toolset] + mcp_instances
        extra_prompt = load_system_prompt("agents/subagent")
    else:  # main
        base_main_toolset = MAIN_TOOLSET
        if allowed_tools is not None:
            filtered_tools = [
                t for t in base_main_toolset.tools
                if getattr(t, "__name__", "") in allowed_tools or getattr(t, "name", "") in allowed_tools
            ]
            base_main_toolset = FunctionToolset(tools=filtered_tools)

        toolsets = [base_main_toolset, dynamic_toolset] + mcp_instances
        if run_mode == "scheduled":
            # 定时任务无人值守:复用主 agent + 主 prompt,额外注入 scheduled_executor
            # 提示词(明确不暴露调度写入、不主动 clarify_user)。
            extra_prompt = load_system_prompt("agents/main_agent") + "\n\n" + load_system_prompt("agents/scheduled_executor")
        else:
            extra_prompt = load_system_prompt("agents/main_agent")

    # 最终 system prompt 顺序（从上到下）:
    #   1. base.md              — 系统人格、核心原则、安全边界
    #   2. role_content         — 角色设定（用户级 user/roles/<name>.md）
    #   3. extra_prompt         — main_agent.md / subagent.md 操作规范
    #   4. @agent.system_prompt — 运行时动态块（config/time/memory/技能索引）
    agent = Agent(
        OpenAIChatModel(config.model_name, provider=provider),
        system_prompt=system_prompt + "\n\n" + role_content + "\n\n" + extra_prompt,
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

    if agent_type == "main":
        # 同一轮内 validator 连续打回的次数,用于硬熔断。在 run_main_agent 入口处
        # 会清零(见 service.agent_runtime.main_agent.run_main_agent)。
        VALIDATOR_RETRY_KEY = "_final_validator_retries"
        VALIDATOR_HARD_LIMIT = 2  # 第 0、1 次打回,第 2 次强制放行避免 UnexpectedModelBehavior

        @agent.output_validator
        def _validate_execution_result(ctx: RunContext[AgentDeps], result: str) -> str:
            from openhachimi_agent.agent.execution import (
                _append_ledger_event,
                get_final_verification_signal,
            )
            from openhachimi_agent.agent.verification_stop import build_verify_on_stop_nudge
            import json
            from pydantic_ai.exceptions import ModelRetry

            session_state = ctx.deps.session_state

            # ── 闸门 1:验证缺口(workspace_edited 后无 fresh evidence) ──
            # 照搬 Hermes verification_stop:模型编辑了代码却想直接结束,先 nudge 它验证。
            # nudge 内部自带 max_attempts 上限,超限即返回 None 放行,不会死循环。
            verify_nudge = build_verify_on_stop_nudge(session_state)
            if verify_nudge:
                try:
                    _append_ledger_event(
                        session_state,
                        tool_name="<verification_stop>",
                        status="blocked",
                        args={"reason": "edited_without_fresh_evidence"},
                        result=verify_nudge,
                        violation=verify_nudge[:500],
                    )
                except Exception:
                    logger.debug("failed to append verification_stop event to ledger", exc_info=True)
                raise ModelRetry(verify_nudge)

            # ── 闸门 2:final verification signal(未完成 TODO + 最近工具失败) ──
            signal = get_final_verification_signal(session_state)
            if not signal:
                return result

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

            # 检查本轮是否已经查看过计划。如果已经查看过，validator 不再强制要求
            # get_todos（避免冗余循环），直接给出增量执行指令。
            plan_already_viewed = bool(ctx.deps.session_state.get("_plan_viewed_this_turn", False))
            if plan_already_viewed:
                parts.append(
                    "请按以下顺序操作，**不要给用户回复任何文字**：\n"
                    "1. 挑出第一个 status=pending 且依赖已 done 的任务，"
                    "用 `update_todo(id, \"in-progress\")` 标记（如果已有 in-progress 任务则跳过此步）；\n"
                    "2. 调用相应执行工具（write_file/run_command/web_fetch/web_search 等）真正完成它；\n"
                    "3. 完成后 `update_todo(id, \"done\", notes=...)`，再继续下一项；\n"
                    "4. 如果某项确实**无法**继续（外部条件缺失、需要用户决策），"
                    "用 `update_todo(id, \"blocked\", notes=\"原因\")` 明确标记，"
                    "然后在最终回复里清楚告知用户：已完成什么 / 卡在哪 / 需要什么——"
                    "不要装作完成。"
                )
            else:
                parts.append(
                    "请按以下顺序操作，**不要给用户回复任何文字**：\n"
                    "1. 调用 `get_todos` 查看完整列表；\n"
                    "2. 挑出第一个 status=pending 且依赖已 done 的任务，"
                    "用 `update_todo(id, \"in-progress\")` 标记；\n"
                    "3. 调用相应执行工具（write_file/run_command/web_fetch/web_search 等）真正完成它；\n"
                    "4. 完成后 `update_todo(id, \"done\", notes=...)`，再继续下一项；\n"
                    "5. 如果某项确实**无法**继续（外部条件缺失、需要用户决策），\n"
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

    # 主 agent 额外的按需块(TODO 接力 / direct-mode / 技能索引):
    # 把过去恒定写在 executor.md 里的几大段策略按 session 状态按需注入,并永远
    # 附上一份"技能索引"(name + 一句话用途,按 category 分组),让主模型自主
    # 决定要不要调 get_skill_instructions 拉某个 skill 的全文。subagent 各自的
    # 角色文档已经明确职责,这套块不在它身上注册。
    if agent_type == "main":
        @agent.system_prompt
        def _executor_extra_block(ctx: RunContext[AgentDeps]) -> str:
            try:
                from openhachimi_agent.content.runtime_context import build_executor_extra_dynamic_block

                return build_executor_extra_dynamic_block(ctx.deps)
            except Exception:  # noqa: BLE001
                logger.exception("executor extra dynamic block failed")
                return ""

    return agent


def build_main_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None, run_mode: str = "interactive") -> Agent:
    """创建单一主 Agent(Hermes 式:持全套工具 + todo 普通工具 + verification 闸门)。

    ``run_mode="scheduled"`` 时复用主 agent,但额外注入 scheduled_executor 提示词,
    scheduler 写工具由 scheduler 工具自身的 ``ensure_scheduler_mutation_allowed`` 拦截。
    """
    return _build_base_agent(config, role_name, "main", allowed_tools=allowed_tools, mcp_toolsets=mcp_toolsets, run_mode=run_mode)


def build_subagent_agent(config: AppConfig, role_name: str, mcp_toolsets: list | None = None) -> Agent:
    """创建通用子 Agent(对齐 hermes delegate_task 委派的 child)。

    供主 agent 通过 ``delegate_task`` 工具委派(pydantic-ai 多 agent 模式 +
    hermes 隔离哲学)。子 agent 全新会话、零长期记忆、独立预算;主工具集运行时
    由 :func:`openhachimi_agent.agent.subagents.resolve_child_toolsets` 裁剪注入,
    构建时不固定(只挂 dynamic_toolset + mcp 骨架)。
    """
    return _build_base_agent(config, role_name, "subagent", mcp_toolsets=mcp_toolsets)
