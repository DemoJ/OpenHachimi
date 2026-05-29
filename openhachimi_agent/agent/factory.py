"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.recall import build_memory_context_text
from openhachimi_agent.tools import PLANNER_TOOLSET, EXECUTOR_TOOLSET, SCHEDULED_EXECUTOR_TOOLSET
from openhachimi_agent.agent.intent import PlanContinuationDecision, TaskFrame


logger = logging.getLogger(__name__)


def _build_base_agent(config: AppConfig, role_name: str, agent_type: str, allowed_tools: set[str] | None = None) -> Agent:
    if not config.openai_api_key:
        raise ValueError("未配置 llm.api_key，请先在 user/config.yaml 中填写 API Key。")

    logger.info(
        "building %s agent role=%s model=%s base_url_configured=%s",
        agent_type,
        role_name,
        config.model_name,
        bool(config.openai_base_url),
    )
    import datetime

    system_prompt = load_system_prompt()
    role_content = load_role_content(config.roles_dir, role_name)

    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    
    from pydantic_ai import FunctionToolset
    from openhachimi_agent.tools.skills import build_skill_tool
    
    # 动态扫描带 arguments 的技能并注册为宏工具
    dynamic_skill_tools = []
    skills = find_skills(config.skills_dirs)
    for skill in skills:
        if skill.config.arguments and not skill.config.disable_model_invocation:
            dynamic_skill_tools.append(build_skill_tool(skill))
            
    dynamic_toolset = FunctionToolset(tools=dynamic_skill_tools)

    if agent_type == "planner":
        toolsets = [PLANNER_TOOLSET, dynamic_toolset]
        extra_prompt = (
            "\n\n[System Role] 你现在是 **Planner Agent (规划者)**。\n"
            "你的唯一职责是：理解用户目标，然后使用 `create_todos` 制定一个可执行的步骤计划。\n"
            "你自己不要去执行任何调研、搜索或网络请求，那是 Executor 的事。\n\n"
            "Executor 拥有以下工具能力：\n"
            "- 浏览器：browser_navigate（打开URL）、browser_get_state（读取页面）、browser_click、browser_type、browser_scroll、browser_new_tab 等\n"
            "- 网络：web_fetch（HTTP抓取）、web_search（搜索引擎）、discover_web_resources\n"
            "- 文件：read_file、write_file、replace_in_file、publish_artifact、list_files、find_files、search_text\n"
            "- 命令行：run_command、send_command_input\n"
            "- Git：git_status、git_diff\n\n"
            "请基于对以上 Executor 工具能力的理解来制定执行计划。\n"
        )
    else:
        base_executor_toolset = SCHEDULED_EXECUTOR_TOOLSET if agent_type == "scheduled_executor" else EXECUTOR_TOOLSET
        filtered_executor_toolset = base_executor_toolset
        if allowed_tools is not None:
            filtered_tools = [
                t for t in base_executor_toolset.tools
                if getattr(t, "__name__", "") in allowed_tools or getattr(t, "name", "") in allowed_tools
            ]
            filtered_executor_toolset = FunctionToolset(tools=filtered_tools)

        toolsets = [filtered_executor_toolset, dynamic_toolset]
        if agent_type == "scheduled_executor":
            extra_prompt = (
                "\n\n[System Role] 你现在是 **Scheduled Executor Agent (定时任务执行者)**。"
                "当前运行在定时任务无人值守执行模式。你可以完成本次任务本身，但禁止创建、修改、暂停、恢复、删除、立即触发或标记任何定时任务。"
                "不要尝试安排后续调度；如任务需要后续调度，请在最终结果中说明需要用户在交互模式下确认。"
                "你只能使用调度只读工具查询定时任务、运行记录、收件箱或投递预览。"
                "如果当前有活动 TODO，你的主要目标是严格按照当前的 TODO 列表，一步步执行具体操作，并在每一步完成后调用 `update_todo`。不要偏离原定计划！"
                "同一轮内，成功的 write_file、replace_in_file、make_directory 或 publish_artifact 返回值可作为对应路径已创建/已修改/已发布的证据；除非后续操作失败或用户要求核验，不要立刻读取或列目录只为确认它存在。"
                "\n当用户要求生成、导出、下载或发送文件时，先用 `write_file` 创建文件，再调用 `publish_artifact` 将该文件发布给用户。"
            )
        else:
            extra_prompt = (
                "\n\n[System Role] 你现在是 **Executor Agent (执行者)**。"
                "如果当前有活动 TODO，你的主要目标是严格按照当前的 TODO 列表，一步步执行具体操作（写代码、运行命令等），并在每一步完成后调用 `update_todo`。不要偏离原定计划！"
                "如果 TaskFrame.execution_mode 是 direct 或 skill_direct，优先直接完成用户目标，不要为了低风险任务主动创建 TODO、反复读取已知路径或进行宽泛探索。"
                "同一轮内，成功的 write_file、replace_in_file、make_directory 或 publish_artifact 返回值可作为对应路径已创建/已修改/已发布的证据；除非后续操作失败或用户要求核验，不要立刻读取或列目录只为确认它存在。"
                "如果 TaskFrame.execution_mode 是 skill_direct，已匹配的 skill 是当前任务的主流程；除非 skill 缺少必要输入、工具失败或用户目标与 skill 冲突，否则不要再进行宽泛仓库探索。"
                "用户要求稍后提醒、几分钟后回复、每天/每周/cron 定时执行时，必须使用 create_delayed_task 或 create_scheduled_task 创建真实定时任务；不要调用 run_command 执行 sleep、timeout、循环等待或后台脚本。"
                "\n当用户要求生成、导出、下载或发送文件时，先用 `write_file` 创建文件，再调用 `publish_artifact` 将该文件发布给用户。"
            )

    agent = Agent(
        OpenAIChatModel(config.model_name, provider=provider),
        system_prompt=system_prompt + extra_prompt,
        instructions=role_content,
        deps_type=AgentDeps,
        toolsets=toolsets,
        defer_model_check=True,
        retries=3,  # 允许工具调用失败后最多重试 3 次，避免因单次输出格式问题导致整体失败
    )

    if agent_type in {"executor", "scheduled_executor"}:
        @agent.output_validator
        def _validate_execution_result(ctx: RunContext[AgentDeps], result: str) -> str:
            from openhachimi_agent.agent.execution import get_final_verification_signal
            import json
            from pydantic_ai.exceptions import ModelRetry
            
            signal = get_final_verification_signal(ctx.deps.session_state)
            if signal:
                raise ModelRetry(
                    f"[系统拦截] 你不能现在就结束任务并回复最终结果！当前 TODO 列表中仍有未完成的任务，或最后一次执行失败。\n"
                    f"验证详情：{json.dumps(signal, ensure_ascii=False)}\n"
                    f"请务必先调用 `update_todo` 工具将所有完成的任务状态更新为 done，或者继续调用工具执行未完成的步骤。"
                )
            return result

    @agent.system_prompt
    def _inject_time() -> str:
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return f"[系统环境] 当前真实时间: {current_time}\n"

    @agent.system_prompt
    def _inject_memory_context(ctx: RunContext[AgentDeps]) -> str:
        if not ctx.deps.config.memory.enabled:
            return ""
        return build_memory_context_text(ctx.deps.config, ctx.deps.memory_context)

    @agent.system_prompt
    def _inject_matched_skills(ctx: RunContext[AgentDeps]) -> str:
        task_frame_dict = ctx.deps.session_state.get("task_frame")
        if not task_frame_dict:
            return ""
        
        relevant_skills = task_frame_dict.get("relevant_skills", [])
        if not relevant_skills:
            return ""

        skills = find_skills(ctx.deps.skills_dirs)
        skill_map = {s.config.name: s for s in skills}
        
        injected = []
        for name in relevant_skills:
            if name in skill_map:
                injected.append(f"<skill name=\"{name}\">\n{skill_map[name].body}\n</skill>")
                
        if injected:
            return "\n\n[System] 以下是基于当前任务意图自动匹配到的专家技能指令，请在执行时严格遵循：\n" + "\n\n".join(injected)
        return ""

    return agent


def build_planner_agent(config: AppConfig, role_name: str) -> Agent:
    """创建专职规划的 Agent。"""
    return _build_base_agent(config, role_name, "planner")


def build_executor_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None) -> Agent:
    """创建专职执行的 Agent（拥有所有权限）。"""
    return _build_base_agent(config, role_name, "executor", allowed_tools=allowed_tools)


def build_scheduled_executor_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None) -> Agent:
    """创建定时任务执行 Agent（不暴露调度写入工具）。"""
    return _build_base_agent(config, role_name, "scheduled_executor", allowed_tools=allowed_tools)


def _build_router_model(config: AppConfig) -> OpenAIChatModel:
    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    return OpenAIChatModel(config.model_name, provider=provider)


def build_router_agent(config: AppConfig) -> Agent:
    """创建用于路由决策的轻量级 Agent。"""
    model = _build_router_model(config)

    skills = find_skills(config.skills_dirs)
    skills_info = "当前可用技能列表（技能名: 描述）：\n"
    if skills:
        for skill in skills:
            skills_info += f"- **{skill.config.name}**: {skill.config.description} (触发时机: {skill.config.when_to_use})\n"
    else:
        skills_info += "无\n"

    system_prompt = (
        "你是一个专业的任务框架分析器。请只做任务理解，不要执行任务。\n"
        "你需要把用户请求整理成 TaskFrame：目标、目标实体、不可变约束、复杂度、风险、execution_mode 和是否需要先规划，并挑选可能匹配的技能。\n"
        "- task_kind 可选：qa, code_change, file_ops, shell, browser, research, unknown。\n"
        "- simple：1-2 步即可完成，且低风险。\n"
        "- complex：需要跨文件/多工具/多步骤调研、代码修改、复杂网页操作或系统性分析。\n"
        "- high risk：删除、覆盖、部署、发布、涉及密钥、登录态或不可逆操作。\n"
        "- execution_mode 可选：direct、skill_direct、planned。简单低风险任务用 direct；命中技能且技能流程足以指导执行时用 skill_direct；只有复杂或高风险任务用 planned。\n"
        "- 如果用户明确给出 URL、文件路径、函数名等目标实体，必须放入 target_entities，并在 invariants 中说明不能替换或扩大目标。\n"
        "- 简单的显式 URL 访问/打开/查看任务应为 browser + simple + requires_plan=false + allowed_autonomy=narrow。\n"
        "- relevant_skills: 如果用户的意图与下方列出的技能匹配，请把匹配的技能名（name）填入该列表。最多选3个。\n"
        "- 用户给出的明确路径、URL、函数名，以及上一轮或同一轮工具成功返回的文件路径，应视为可信目标，不要因确认焦虑而要求额外规划。\n"
        "- 用户要求稍后提醒、几分钟后回复、每天/每周/cron 定时执行时，task_kind 应为 qa 或 unknown，execution_mode=direct，不要归类为 shell；执行阶段会使用定时任务工具而不是 sleep 命令。\n"
        "不确定时降低 confidence，但优先保持 direct；只有任务明显需要跨文件、多工具、多阶段验证或存在高风险时，才将 requires_plan 设为 true。\n\n"
        f"{skills_info}"
    )
    
    return Agent(
        model,
        system_prompt=system_prompt,
        output_type=TaskFrame,
    )


def build_continuation_agent(config: AppConfig) -> Agent:
    """创建用于判断用户是否要继续旧计划的轻量级 Agent。"""
    system_prompt = (
        "你是一个对话连续性判断器。只判断用户最新消息是否是在要求继续/恢复当前未完成计划，"
        "还是提出了一个新的任务或问题。不要执行任务。\n"
        "可选 action：\n"
        "- continue_active_plan：用户明确要继续当前仍 active 的计划。\n"
        "- resume_suspended_plan：用户明确要恢复已挂起的计划。\n"
        "- start_new_task：用户提出新目标、新问题、切换话题，或意图不明确。\n"
        "判断依据要结合用户原话、当前 TODO 摘要、挂起原因和 TaskFrame；"
        "不确定时选择 start_new_task，避免旧计划绑架新对话。"
    )
    return Agent(
        _build_router_model(config),
        system_prompt=system_prompt,
        output_type=PlanContinuationDecision,
    )
