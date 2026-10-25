"""``service.agent_runtime.session_history.get_session_messages`` 单元测试。

核心不变量:压缩只影响喂给模型的运行时上下文,WebUI 消息流永远返回
全部原始消息;压缩标记放在压缩发生的那一轮之后,不隐藏任何消息。
一轮内模型的多步回复合并成一个气泡,与流式界面一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RequestUsage,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from openhachimi_agent.service.agent_runtime.session_history import get_session_messages
from openhachimi_agent.storage.session_store import SessionStore


class _StubService:
    """仅暴露 get_session_messages 所需接口的 AgentService 桩。"""

    def __init__(self, store: SessionStore):
        self.session_store = store

    def _normalize_role(self, role_name):
        return role_name or "default"

    def _validate_role_exists(self, role):
        pass

    def _normalize_session_id(self, session_id):
        return session_id


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.sqlite3")


def _build_history(n: int) -> list:
    msgs = []
    for i in range(n):
        msgs.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
        msgs.append(ModelResponse(parts=[TextPart(content=f"r{i}")]))
    return msgs


def test_compressed_messages_still_fully_returned(store: SessionStore):
    """被压缩区间的原始消息必须原样出现;标记放在压缩发生时的最后一条消息之后。"""
    sid = SessionStore.new_session_id()
    store.save_messages("default", sid, _build_history(10))  # 行 0..19(u0,r0,u1,r1,…)
    # 折叠行 4..11(8 条) = u2,r2,u3,r3,u4,r4,u5,r5
    store.record_compression("default", sid, 3, 12, "首段摘要\n\n细节", total_len=20)
    store.save_messages("default", sid, _build_history(1))  # 压缩之后的新一轮

    result = get_session_messages(_StubService(store), "default", sid)
    messages = result["messages"]

    contents = [m.content for m in messages]
    for i in range(10):
        assert f"u{i}" in contents
        assert f"r{i}" in contents

    folds = [m for m in messages if m.fold is not None]
    assert len(folds) == 1
    fold = folds[0].fold
    assert fold["compressed_count"] == 8
    assert fold["summary_excerpt"] == "首段摘要"
    assert fold["summary"] == "首段摘要\n\n细节"
    # 压缩发生在 r9 落库之后、下一轮(u0 第二次出现)之前
    fold_pos = messages.index(folds[0])
    assert contents.index("r9") < fold_pos
    assert contents[fold_pos + 1] == "u0"


def test_no_compression_returns_plain_messages(store: SessionStore):
    """无压缩记录时不插入任何标记条。"""
    sid = SessionStore.new_session_id()
    store.save_messages("default", sid, _build_history(3))

    result = get_session_messages(_StubService(store), "default", sid)
    assert all(m.fold is None for m in result["messages"])
    assert len(result["messages"]) == 6


def test_multi_step_turn_merged_into_one_bubble(store: SessionStore):
    """只调工具不写正文的步骤也要体现;一轮合并成一个 assistant 气泡,token 累加。"""
    sid = SessionStore.new_session_id()
    usage = RequestUsage(input_tokens=100, output_tokens=10)
    store.save_messages("default", sid, [
        ModelRequest(parts=[UserPromptPart(content="帮我装 skill")]),
        ModelResponse(parts=[
            TextPart(content="我先看一下"),
            ToolCallPart(tool_name="run_command", args={"command": "ls"}, tool_call_id="c1"),
        ], usage=usage),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_command", content="ok", tool_call_id="c1")]),
        ModelResponse(parts=[
            ToolCallPart(tool_name="run_command", args={"command": "pwd"}, tool_call_id="c2"),
        ], usage=usage),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_command", content="ok", tool_call_id="c2")]),
        ModelResponse(parts=[TextPart(content="装好了")], usage=usage),
    ])

    messages = get_session_messages(_StubService(store), "default", sid)["messages"]
    assert [m.role for m in messages] == ["user", "assistant"]
    reply = messages[1]
    assert reply.content == "我先看一下\n\n装好了"
    assert len(reply.tool_calls) == 2
    assert reply.tokens["input"] == 300 and reply.tokens["output"] == 30


def test_clarify_question_and_reply_are_shown(store: SessionStore):
    """clarify_user 追问作为正文显示;用户对追问的回复还原为用户消息。"""
    sid = SessionStore.new_session_id()
    store.save_messages("default", sid, [
        ModelRequest(parts=[UserPromptPart(content="装飞书 skill")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="clarify_user",
            args={"question": "装哪个?", "choices": ["官方", "社区"]},
            tool_call_id="q1",
        )]),
        ModelRequest(
            parts=[ToolReturnPart(
                tool_name="clarify_user",
                content="官方\n\n[系统提示] 以上是用户对你追问的回复。你还有一个进行中的计划",
                tool_call_id="q1",
            )],
        ),
        ModelResponse(parts=[TextPart(content="好的,装官方版")]),
    ])

    messages = get_session_messages(_StubService(store), "default", sid)["messages"]
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1].content.startswith("装哪个?")
    assert "1）官方" in messages[1].content
    assert not messages[1].tool_calls  # 追问本身不再算作工具调用
    assert messages[2].content == "官方"
    assert messages[3].content == "好的,装官方版"


def test_clarify_timeout_nudge_not_shown_as_user(store: SessionStore):
    """追问超时后补写的系统说明不是用户消息,不能出现在聊天记录里。"""
    sid = SessionStore.new_session_id()
    store.save_messages("default", sid, [
        ModelRequest(parts=[UserPromptPart(content="q")]),
        ModelResponse(parts=[ToolCallPart(tool_name="clarify_user", args={"question": "?"}, tool_call_id="q1")]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="clarify_user", content="[System 提示] 用户长时间未回复", tool_call_id="q1",
        )]),
        ModelRequest(parts=[UserPromptPart(content="换个话题")]),
    ])

    messages = get_session_messages(_StubService(store), "default", sid)["messages"]
    assert [m.content for m in messages] == ["q", "?", "换个话题"]
