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


def _build_unfinished_reminder(signal: dict | None) -> str | None:
    """根据 final verification signal 里的 unfinished_todos 生成软提醒文案。

    闸门2 对 unfinished_todos 不再打回(对齐 Hermes:停止闸门不卡 TODO 是否全完成),
    而是在模型最终回复末尾追加这段提醒后放行。返回 None 表示无未完成项,无需追加。

    仅统计 pending/in-progress(blocked/done 视为合法终止态,不提醒)。
    若 signal 同时含 latest_execution_not_successful,返回 None——失败优先走打回
    路径,软提醒不重复出现。
    """
    if not signal:
        return None
    issues = signal.get("issues", []) if isinstance(signal, dict) else []
    has_latest_failure = any(
        isinstance(issue, dict) and issue.get("type") == "latest_execution_not_successful"
        for issue in issues
    )
    if has_latest_failure:
        return None
    unfinished_items = [
        item for issue in issues
        if isinstance(issue, dict) and issue.get("type") == "unfinished_todos"
        for item in issue.get("items", [])
        if isinstance(item, dict)
    ]
    pending = [it for it in unfinished_items if it.get("status") in {"pending", "in-progress"}]
    if not pending:
        return None
    lines = [
        f"  - [{it.get('status', '?')}] #{it.get('id', '?')} {it.get('description', '')}"
        for it in pending[:10]
    ]
    return (
        "\n\n---\n[System 提醒] 当前计划仍有 "
        f"{len(pending)} 项未完成（pending/in-progress）：\n"
        + "\n".join(lines)
        + "\n若这些项确实无法继续，请在回复里如实告知用户：已完成什么、卡在哪、需要什么；"
          "不要装作全部完成。"
    )


def _build_reasoning_model_settings(config: AppConfig) -> dict:
    """把 llm_reasoning_effort 配置翻译成 pydantic-ai 的 model_settings。

    值即 openai SDK 官方 reasoning_effort 枚举(none/minimal/low/medium/high/xhigh),
    原样透传给 OpenAIChatModel。none 也显式发送,行为与官方参数一致;对不支持
    reasoning 的模型由服务端自行处理。
    """
    return {"openai_reasoning_effort": config.llm_reasoning_effort}


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
        model_settings=_build_reasoning_model_settings(config),
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
            # 透传 session_store/session_id 让 nudge 能引用"上次验证"证据(若已持久化)。
            verify_nudge = build_verify_on_stop_nudge(
                session_state,
                session_store=getattr(ctx.deps, "session_store", None),
                session_id=ctx.deps.session_id,
            )
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

            # ── 闸门 2:final verification signal ──
            # signal 含两类 issue:
            #   - unfinished_todos:计划仍有 pending/in-progress 项。
            #   - latest_execution_not_successful:本轮最近一次工具调用 failed。
            #
            # 语义(对齐 Hermes:停止闸门只校验"别带着失败装完成",不卡 TODO 是否全完成):
            #   - unfinished_todos → 降级为软提醒:在 result 末尾追加提示后放行,不打回。
            #     旧实现强制 TODO 全 done/blocked 才能结束,逼模型撒谎标 done 或标 blocked
            #     逃避交互;现在模型说停就停,用户仍能看到"还有没做完的"提醒。
            #   - latest_execution_not_successful → 仍打回(别带着失败装完成),带硬熔断。
            signal = get_final_verification_signal(session_state)
            if not signal:
                return result

            issues = signal.get("issues", []) if isinstance(signal, dict) else []
            latest_failure_issues = [
                issue for issue in issues
                if isinstance(issue, dict) and issue.get("type") == "latest_execution_not_successful"
            ]

            # ── unfinished_todos 软提醒:追加后放行,不打回 ──
            reminder = _build_unfinished_reminder(signal)
            if reminder:
                pending_count = reminder.count("\n  - [")
                logger.info(
                    "validator soft-remind unfinished todos (session=%s, pending=%d)",
                    ctx.deps.session_id,
                    pending_count,
                )
                session_state["_final_validator_yielded"] = True
                session_state["_final_validator_last_signal"] = signal
                return result + reminder

            # ── latest_execution_not_successful 打回(带硬熔断) ──
            # 到这里要么只有 latest_failure,要么 latest_failure + unfinished 同时存在
            # (工具刚失败 + 还有未完成项)——失败优先打回,让模型先处理失败。
            counter = int(session_state.get(VALIDATOR_RETRY_KEY, 0) or 0)

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

            latest_failure_lines = [
                f"  - 上一次 `{issue.get('tool_name', '?')}` 以 `{issue.get('status', '?')}` 结束："
                f"{(issue.get('detail') or '')[:300]}"
                for issue in latest_failure_issues
            ]
            parts = [
                f"[系统拦截] 你不能现在就给用户最终回复——最近一次工具调用没有成功"
                f"（第 {counter + 1} 次拦截）。",
            ]
            if latest_failure_lines:
                parts.append("失败详情：\n" + "\n".join(latest_failure_lines))
            parts.append(
                "请按以下顺序操作，**不要给用户回复任何文字**：\n"
                "1. 根据上面的失败详情判断根因（参数错？路径不存在？权限？依赖缺失？）；\n"
                "2. 修正后重试该工具，或换用其它工具/方案；\n"
                "3. 若确实无法继续（外部条件缺失、需要用户决策），"
                "用 `update_todo(id, \"blocked\", notes=\"原因\")` 明确标记，"
                "然后在最终回复里如实告知用户卡在哪、需要什么——不要装作完成。"
            )
            # 第 2 次拦截仍未通过 = 同一卡点重试过仍失败,引导模型修订计划而非硬重试。
            if counter >= 1:
                parts.append(
                    "你已连续失败多次。如果原计划的这一步确实走不通,考虑用 "
                    "`create_todos(merge=True, tasks=[...])` 修订计划:把受阻任务标 "
                    "blocked、追加替代步骤,按新计划继续——不要反复以同样方式重试。"
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
