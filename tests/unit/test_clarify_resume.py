# pyrefly: ignore [missing-import]
"""测试 clarify_user 切到 ``CallDeferred`` 之后:
- ``run_main_agent`` 看到 ``DeferredToolRequests`` 输出立刻短路;
- ``resume_main_agent`` 把用户回复以 ``DeferredToolResults`` 灌回主 agent;
- 成功 resume 后 ``_user_clarification`` 被清。

不依赖真实 LLM,使用 FakeRunResult / FakeAgent stub 拦截 ``agent.run`` 调用,
观察 ``deferred_tool_results`` / ``message_history`` / ``user_prompt`` 这些参数。
"""

from types import SimpleNamespace

import pytest

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from openhachimi_agent.service.agent_runtime.context import AgentRunContext
from openhachimi_agent.service.agent_runtime.main_agent import (
    _find_pending_clarify_tool_call,
    resume_main_agent,
    run_main_agent,
)


class FakeRunResult:
    def __init__(self, output):
        self.output = output

    def all_messages(self):
        return []


class FakeAgent:
    """通用 stub:记录最近一次 run 的入参,按 outputs 队列顺序返回。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.runs: list[dict] = []

    async def run(self, message, **kwargs):
        self.runs.append({"message": message, "kwargs": kwargs})
        output = self.outputs.pop(0)
        return FakeRunResult(output)


def _ctx(*, session_state, history=None, message="hello", stream=False):
    deps = SimpleNamespace(
        run_mode="interactive",
        session_state=session_state,
        session_id="sess-1",
    )
    return AgentRunContext(
        config=SimpleNamespace(agent_timeout_seconds=300, model_name="m", base_dir=None),
        role="default",
        session_id="sess-1",
        message=message,
        attachments=[],
        history=list(history or []),
        deps=deps,
        session_state=session_state,
        stream=stream,
    )


@pytest.mark.asyncio
async def test_run_main_agent_short_circuits_on_deferred_output(monkeypatch):
    """模型 emit 了 clarify_user 工具调用(抛 CallDeferred)→ result.output 是
    DeferredToolRequests。run_main_agent 应直接 return,不发起 verification。"""
    deferred = DeferredToolRequests(
        calls=[ToolCallPart(tool_name="clarify_user", args={"question": "?"}, tool_call_id="call_x")],
    )
    main_agent = FakeAgent([deferred])

    # 把 _build_main_agent stub 成返回 FakeAgent,避免触发真实 LLM provider 构造
    from openhachimi_agent.service.agent_runtime import main_agent as main_mod

    monkeypatch.setattr(main_mod, "_build_main_agent", lambda role, run_mode, get_agent: main_agent)
    # 跳过视觉预处理(真实流程会查 vision_model 配置)
    from openhachimi_agent.vision import preprocess as vision_preprocess

    async def fake_preprocess(*args, **kwargs):
        return vision_preprocess.VisionPreprocessResult(mode="none")

    monkeypatch.setattr(main_mod, "preprocess_vision_attachments", fake_preprocess)

    ctx = _ctx(session_state={})
    outcome = await run_main_agent(ctx, lambda *a, **kw: main_agent)

    assert isinstance(outcome.result.output, DeferredToolRequests)
    assert outcome.final_verification_signal is None
    # 只跑一次主 agent,没有 replan / repair 的二次调用
    assert len(main_agent.runs) == 1


@pytest.mark.asyncio
async def test_resume_main_agent_feeds_user_reply_as_deferred_result(monkeypatch):
    """resume 路径:_user_clarification 存在 → 构造 DeferredToolResults 灌回模型。
    断言 FakeAgent 收到的 deferred_tool_results.calls 含正确 tool_call_id → 用户消息映射。"""
    user_reply = "邮箱是 a@b.com,授权码是 xyz"
    session_state = {
        "_user_clarification": {
            "question": "请提供邮箱和 SMTP 授权码",
            "missing_inputs": ["邮箱", "授权码"],
            "tool_call_id": "call_abc",
        },
    }
    history = [ModelRequest(parts=[UserPromptPart(content="原始任务")])]
    ctx = _ctx(session_state=session_state, history=history, message=user_reply)

    main_agent = FakeAgent(["补齐后的最终回复"])
    from openhachimi_agent.service.agent_runtime import main_agent as main_mod

    monkeypatch.setattr(main_mod, "_build_main_agent", lambda role, run_mode, get_agent: main_agent)
    # resume 路径也要跳过 preflight 压缩(无 compressor)
    monkeypatch.setattr(main_mod, "preflight_compress_history", lambda ctx: None)

    outcome = await resume_main_agent(ctx, lambda *a, **kw: main_agent)

    assert outcome is not None
    assert outcome.result.output == "补齐后的最终回复"
    # 灌回参数正确
    assert len(main_agent.runs) == 1
    run = main_agent.runs[0]
    # 第一位置参数是 None(user_prompt 不再独立传)
    assert run["message"] is None
    deferred_results = run["kwargs"]["deferred_tool_results"]
    assert isinstance(deferred_results, DeferredToolResults)
    assert deferred_results.calls == {"call_abc": user_reply}
    # message_history 必须带原 history,否则 graph 跑不到 CallToolsNode
    assert run["kwargs"]["message_history"] == history
    # 成功 resume 后标志被清
    assert "_user_clarification" not in ctx.session_state


@pytest.mark.asyncio
async def test_resume_main_agent_falls_back_when_tool_call_id_missing(monkeypatch):
    """状态损坏:_user_clarification 缺 tool_call_id 且 history 中也没有未消费的
    clarify_user → 返回 None,清理标志,让上层 fall back 到正常 run_main_agent。"""
    session_state = {"_user_clarification": {"question": "lost id"}}
    ctx = _ctx(session_state=session_state, history=[], message="user reply")

    outcome = await resume_main_agent(ctx, lambda *a, **kw: None)

    assert outcome is None
    assert "_user_clarification" not in ctx.session_state


def test_find_pending_clarify_tool_call_recovers_id_from_history():
    """兜底 helper:从 history 末尾抽取未消费的 clarify_user ToolCallPart。"""
    history = [
        ModelRequest(parts=[UserPromptPart(content="发邮件")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="clarify_user",
                    args={"question": "凭据?"},
                    tool_call_id="call_xyz",
                ),
            ],
        ),
    ]
    assert _find_pending_clarify_tool_call(history) == "call_xyz"


def test_find_pending_clarify_returns_none_when_no_pending():
    """最近一次 ModelResponse 没有 clarify_user 调用 → 不是 resume 场景。"""
    history = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[]),
    ]
    assert _find_pending_clarify_tool_call(history) is None


def test_main_toolset_registers_clarify_user_and_create_todos():
    """Hermes 式重构后单一主 agent:clarify_user 和 create_todos 都在 MAIN_TOOLSET
    (create_todos 不再是 planner 的 output tool,而是普通工具)。"""
    from openhachimi_agent.tools.registry import MAIN_TOOLSET

    assert "clarify_user" in MAIN_TOOLSET.tools
    assert "create_todos" in MAIN_TOOLSET.tools
    assert "get_todos" in MAIN_TOOLSET.tools


def test_clarify_user_function_tool_event_is_silenced():
    """clarify_user 是 deferred 工具,question 参数本身就是要发给用户看的完整
    自然语言追问;turn 的 deferred outcome 分支会把它当作本轮 assistant
    回复完整输出。所以 streaming layer **不应**再为它 emit 一条"工具卡片"事件
    (否则 UI 上会看到截断的 ``🔧 clarify_user：{"question": "...截断..."}`` 紧跟
    一段完整 question,既丑又重复)。
    """
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    from openhachimi_agent.service.agent_runtime.streaming import event_item_from_stream_event

    event = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="clarify_user",
            args={"question": "请提供 SMTP 凭据", "missing_inputs": ["邮箱"]},
            tool_call_id="call_abc",
        ),
    )
    assert event_item_from_stream_event(event) is None


def test_non_clarify_tool_call_still_emits_card():
    """clarify_user 的静默不应影响其它工具的卡片渲染。"""
    from pydantic_ai.messages import FunctionToolCallEvent, ToolCallPart

    from openhachimi_agent.service.agent_runtime.streaming import event_item_from_stream_event

    event = FunctionToolCallEvent(
        part=ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="c1"),
    )
    item = event_item_from_stream_event(event)
    assert item is not None
    assert item.type == "tool"
    assert "read_file" in item.tool_name or "读取" in item.text
