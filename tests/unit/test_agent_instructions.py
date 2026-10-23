# pyrefly: ignore [missing-import]
"""主 agent 的系统指令每轮都按当前状态重新生成,不会冻结在第一轮。

回归背景:之前用 @agent.system_prompt 注册动态块。pydantic-ai 只在 message_history 为空
时生成 system prompt,之后它作为第一条消息留在历史里原样复用,于是记忆召回、当前时间、
TODO 接力从第二轮起都停在第一轮的内容。
"""

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from openhachimi_agent.agent.factory import build_main_agent
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.service.agent_runtime.history_prep import (
    prepare_history_for_run,
    strip_frozen_system_prompt,
    strip_stale_todo_reminders,
)
from openhachimi_agent.tools.planning import TODO_REMINDER, TodoState, TodoTask


@pytest.fixture
def main_agent(mock_config):
    (mock_config.roles_dir / "default.md").write_text("# default\n\n测试用角色。\n", encoding="utf-8")
    return build_main_agent(mock_config, "default")


def _recording_model(seen: list[dict]):
    def model_fn(messages, info):
        seen.append({
            "instructions": info.instructions or "",
            "system_parts": [
                part.content for message in messages if isinstance(message, ModelRequest)
                for part in message.parts if isinstance(part, SystemPromptPart)
            ],
        })
        return ModelResponse(parts=[TextPart("ok")])
    return FunctionModel(model_fn)


@pytest.mark.asyncio
async def test_dynamic_blocks_refresh_on_second_turn(main_agent, mock_config):
    seen: list[dict] = []
    plan = TodoState(goal="g", is_active=True, tasks={1: TodoTask(id=1, description="第二轮才出现的任务", status="pending")})

    with main_agent.override(model=_recording_model(seen)):
        first = await main_agent.run("第一轮", deps=AgentDeps(config=mock_config, session_id="s1", session_state={}))
        await main_agent.run(
            "第二轮",
            deps=AgentDeps(config=mock_config, session_id="s1", session_state={"todo_state": plan}),
            message_history=first.all_messages(),
        )

    turn1, turn2 = seen
    assert "规划引导" in turn1["instructions"]
    # 第二轮按当前状态重新生成:出现 TODO 接力规则和计划快照,规划引导让位
    assert "执行接力规则" in turn2["instructions"]
    assert "第二轮才出现的任务" in turn2["instructions"]
    assert "规划引导" not in turn2["instructions"]
    # 不再有会被历史冻结的 system prompt
    assert turn1["system_parts"] == [] and turn2["system_parts"] == []


def test_strip_frozen_system_prompt_only_touches_first_request():
    history = [
        ModelRequest(parts=[SystemPromptPart(content="旧的冻结系统提示"), UserPromptPart(content="第一轮")]),
        ModelResponse(parts=[TextPart("ok")]),
        ModelRequest(parts=[SystemPromptPart(content="追问超时提醒"), UserPromptPart(content="第二轮")]),
    ]

    result = strip_frozen_system_prompt(history)

    assert result is history
    assert [type(part) for part in history[0].parts] == [UserPromptPart]
    assert isinstance(history[2].parts[0], SystemPromptPart)


def test_strip_stale_todo_reminders_handles_str_and_dict_results():
    suffix = "\n\n" + TODO_REMINDER
    history = [
        ModelRequest(parts=[UserPromptPart(content="做事")]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={}, tool_call_id="a")]),
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content="文件内容" + suffix, tool_call_id="a"),
            ToolReturnPart(tool_name="run_command", content={"output": "ok" + suffix}, tool_call_id="b"),
            ToolReturnPart(tool_name="web_fetch", content={"html": "x", "_todo_reminder": TODO_REMINDER}, tool_call_id="c"),
            ToolReturnPart(tool_name="git_diff", content="没有提醒", tool_call_id="d"),
        ]),
    ]

    strip_stale_todo_reminders(history)

    contents = [part.content for part in history[2].parts]
    assert contents == ["文件内容", {"output": "ok"}, {"html": "x"}, "没有提醒"]


def test_prepare_history_for_run_is_noop_for_clean_history():
    history = [ModelRequest(parts=[UserPromptPart(content="hi")]), ModelResponse(parts=[TextPart("ok")])]
    snapshot = list(history)

    assert prepare_history_for_run(history) == snapshot
