# pyrefly: ignore [missing-import]
"""测试 clarify_user 切到 ``CallDeferred`` 之后:
- ``execute_task`` 看到 ``DeferredToolRequests`` 输出立刻短路;
- ``execute_task_resume`` 把用户回复以 ``DeferredToolResults`` 灌回 executor;
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
from openhachimi_agent.service.agent_runtime.executor import (
    _find_pending_clarify_tool_call,
    execute_task,
    execute_task_resume,
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
        config=SimpleNamespace(agent_timeout_seconds=300, model_name="m"),
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
async def test_execute_task_short_circuits_on_deferred_output(monkeypatch):
    """模型 emit 了 clarify_user 工具调用(抛 CallDeferred)→ result.output 是
    DeferredToolRequests。execute_task 应直接 return,不发起 verification / repair。"""
    deferred = DeferredToolRequests(
        calls=[ToolCallPart(tool_name="clarify_user", args={"question": "?"}, tool_call_id="call_x")],
    )
    executor = FakeAgent([deferred])

    # 把 _build_executor_agent stub 成返回 FakeAgent,避免触发真实 LLM provider 构造
    from openhachimi_agent.service.agent_runtime import executor as executor_mod

    monkeypatch.setattr(executor_mod, "_build_executor_agent", lambda *a, **kw: executor)
    # 跳过视觉预处理(真实流程会查 vision_model 配置)
    from openhachimi_agent.vision import preprocess as vision_preprocess

    async def fake_preprocess(*args, **kwargs):
        return vision_preprocess.VisionPreprocessResult(mode="none")

    monkeypatch.setattr(executor_mod, "preprocess_vision_attachments", fake_preprocess)

    ctx = _ctx(session_state={})
    outcome = await execute_task(ctx, lambda role, kind: executor)

    assert isinstance(outcome.result.output, DeferredToolRequests)
    assert outcome.final_verification_signal is None
    # 只跑一次 executor,没有 self_critique / repair / replan 的二次调用
    assert len(executor.runs) == 1


@pytest.mark.asyncio
async def test_execute_task_resume_feeds_user_reply_as_deferred_result(monkeypatch):
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

    executor = FakeAgent(["补齐后的最终回复"])
    from openhachimi_agent.service.agent_runtime import executor as executor_mod

    monkeypatch.setattr(executor_mod, "_build_executor_agent", lambda *a, **kw: executor)

    outcome = await execute_task_resume(ctx, lambda role, kind: executor)

    assert outcome is not None
    assert outcome.result.output == "补齐后的最终回复"
    # 灌回参数正确
    assert len(executor.runs) == 1
    run = executor.runs[0]
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
async def test_execute_task_resume_falls_back_when_tool_call_id_missing(monkeypatch):
    """状态损坏:_user_clarification 缺 tool_call_id 且 history 中也没有未消费的
    clarify_user → 返回 None,清理标志,让上层 fall back 到正常 execute_task。"""
    session_state = {"_user_clarification": {"question": "lost id"}}
    ctx = _ctx(session_state=session_state, history=[], message="user reply")

    outcome = await execute_task_resume(ctx, lambda role, kind: None)

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


def test_planner_toolset_does_not_register_clarify_user():
    """设计决策:planner 没有执行类工具,无法做出"我已用工具自查、确认信息只能由
    用户提供"的合法判断。clarify_user 只在 executor 注册;planner 应通过 TODO 化
    "探测/确认 X"让 executor 在真实证据上决定要不要 clarify_user。
    一并锁住 executor 仍注册 clarify_user,避免下次重构时一刀切。"""
    from openhachimi_agent.tools.registry import EXECUTOR_TOOLSET, PLANNER_TOOLSET

    # FunctionToolset.tools 是 dict[name, ToolFunction]
    assert "clarify_user" not in PLANNER_TOOLSET.tools
    assert "clarify_user" in EXECUTOR_TOOLSET.tools


def test_planner_uses_create_todos_as_output_tool():
    """create_todos 是 planner 的 *output tool*(通过 factory 的 ToolOutput 注册),
    模型一调它就视为 final answer,graph 立即终止 run —— 不会再发起第二步 LLM
    调用让模型 emit 一段重复的"执行步骤概览"文本。

    因此 ``PLANNER_TOOLSET`` 里不应再有 ``create_todos``(否则会和 output tool
    重复注册并引起 schema 冲突);get_todos 作为只读调研工具仍保留。
    """
    from openhachimi_agent.tools.registry import PLANNER_TOOLSET

    assert "create_todos" not in PLANNER_TOOLSET.tools
    assert "get_todos" in PLANNER_TOOLSET.tools


def test_clarify_user_function_tool_event_is_silenced():
    """clarify_user 是 deferred 工具,question 参数本身就是要发给用户看的完整
    自然语言追问;turn.run_agent 的 deferred outcome 分支会把它当作本轮 assistant
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


def test_emit_output_tool_card_compensates_for_missing_event():
    """ToolOutput 路径副作用补偿:create_todos 作为 output tool 调用时,
    pydantic-ai graph 不发 FunctionToolCallEvent。planner 路径必须手动把它
    构造成一条标准工具卡片塞进 stream queue,否则 UI 看不到"✅ 创建计划:..."。
    """
    import asyncio
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from openhachimi_agent.service.agent_runtime.planner import _emit_output_tool_card
    from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem

    queue: asyncio.Queue = asyncio.Queue()

    class FakeResult:
        def all_messages(self):
            return [
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="create_todos",
                            args={
                                "goal": "调研 + 实现",
                                "tasks": [
                                    {"id": 1, "description": "查源码", "success_criteria": "看清架构"},
                                    {"id": 2, "description": "改代码"},
                                ],
                            },
                            tool_call_id="ot1",
                        ),
                    ],
                ),
            ]

    ctx = SimpleNamespace(stream=True, stream_queue=queue)
    _emit_output_tool_card(ctx, FakeResult(), tool_name="create_todos")

    assert not queue.empty(), "应该有一条卡片事件被塞进 queue"
    item = queue.get_nowait()
    assert isinstance(item, StreamEventItem)
    assert item.type == "tool"
    assert item.tool_name == "create_todos"
    # 标题独占一行 + 多行明细块(参见 _tool_detail("create_todos") 的实现)
    assert "创建计划" in item.text
    assert "目标：调研 + 实现" in item.text
    assert "1. 查源码" in item.text
    assert "2. 改代码" in item.text


# ---------------------------------------------------------------------------
# 流式末尾补发 clarification question 的回归测试
#
# 历史 bug:run_turn 末尾仅在 ``stream_stats.chunk_count == 0`` 时才把
# ``final_output_text`` 作为 text 事件 yield。而 ``streaming.py`` 故意吞掉
# clarify_user 的 FunctionToolCallEvent —— 模型一旦在调 clarify_user 之前
# 流式吐过任何过渡文字(很常见,如"邮件能力检查完成,准备…"),chunk_count > 0,
# 整段追问被静默丢弃。用户视角:看到一行 ✅ 工具卡片就突然中断,不知道下一步
# 要做什么。修复方案是把判定抽到 _resolve_terminal_stream_text,deferred 路径
# (result_holder 含 clarification_question)无条件补发。
# ---------------------------------------------------------------------------

def test_terminal_stream_text_yields_clarification_when_chunks_streamed():
    """模型在 clarify_user 之前流式过 26 字符过渡文字(chunk_count=8)。
    deferred 路径必须仍然把 question 文本补发,且前面加 \\n\\n 与过渡文字隔开。"""
    from openhachimi_agent.service.agent_runtime.turn import _resolve_terminal_stream_text

    question = "请提供 SMTP 服务器、端口、发件人邮箱、授权码"
    result_holder = {"clarification_question": question}
    out = _resolve_terminal_stream_text(question, result_holder, chunk_count=8)
    assert out == f"\n\n{question}"


def test_terminal_stream_text_yields_clarification_when_no_chunks_streamed():
    """deferred 路径但前面一个 chunk 都没有(模型直接调 clarify_user 不带任何前置
    解释):仍要补发,但不需要前置 \\n\\n。"""
    from openhachimi_agent.service.agent_runtime.turn import _resolve_terminal_stream_text

    question = "需要凭据"
    out = _resolve_terminal_stream_text(
        question, {"clarification_question": question}, chunk_count=0,
    )
    assert out == question


def test_terminal_stream_text_yields_final_output_when_no_chunks_streamed():
    """非 deferred 但 chunk_count == 0:模型靠 result.output 字段返回最终答案
    (结构化输出 / 极短回复)。仍需补一次 text 事件,否则用户拿不到任何输出。"""
    from openhachimi_agent.service.agent_runtime.turn import _resolve_terminal_stream_text

    out = _resolve_terminal_stream_text("最终答案", {}, chunk_count=0)
    assert out == "最终答案"


def test_terminal_stream_text_silent_when_chunks_streamed_and_not_deferred():
    """正常路径(非 deferred 且 chunk_count > 0):final answer 已经流过,不要重复
    yield,否则会双倍出现在 UI 上。"""
    from openhachimi_agent.service.agent_runtime.turn import _resolve_terminal_stream_text

    out = _resolve_terminal_stream_text("最终答案", {}, chunk_count=5)
    assert out == ""


def test_terminal_stream_text_silent_when_output_empty():
    """final_output_text 为空(模型彻底没产生任何输出):不要 yield 空 text 事件。"""
    from openhachimi_agent.service.agent_runtime.turn import _resolve_terminal_stream_text

    assert _resolve_terminal_stream_text("", {"clarification_question": ""}, chunk_count=0) == ""
    assert _resolve_terminal_stream_text("", {}, chunk_count=0) == ""



