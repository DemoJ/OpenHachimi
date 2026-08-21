"""``service.agent_runtime.session_history.get_session_messages`` 单元测试。

核心不变量:压缩只影响喂给模型的运行时上下文,WebUI 消息流永远返回
全部原始消息;压缩标记条仅插在折叠区间起点作分隔提示,不隐藏任何消息。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

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
    """被压缩区间的原始消息必须原样出现在展示消息流中,标记条只作提示。"""
    sid = SessionStore.new_session_id()
    store.save_messages("default", sid, _build_history(10))  # 行 0..19(u0,r0,u1,r1,…)
    # 折叠行 4..11(8 条) = u2,r2,u3,r3,u4,r4,u5,r5
    store.record_compression("default", sid, 3, 12, "首段摘要", total_len=20)

    result = get_session_messages(_StubService(store), "default", sid)
    messages = result["messages"]

    # 全部 20 条真实消息都在:u0..u9 与 r0..r9 一个不少
    contents = [m.content for m in messages]
    for i in range(10):
        assert f"u{i}" in contents
        assert f"r{i}" in contents

    # 恰好一条压缩标记,位于区间起点(u2 之前),不替换任何消息
    folds = [m for m in messages if m.fold is not None]
    assert len(folds) == 1
    assert folds[0].fold["compressed_count"] == 8
    assert folds[0].fold["summary_excerpt"].startswith("首段摘要")
    assert contents.index("u1") < messages.index(folds[0]) < contents.index("u2")


def test_no_compression_returns_plain_messages(store: SessionStore):
    """无压缩记录时不插入任何标记条。"""
    sid = SessionStore.new_session_id()
    store.save_messages("default", sid, _build_history(3))

    result = get_session_messages(_StubService(store), "default", sid)
    assert all(m.fold is None for m in result["messages"])
    assert len(result["messages"]) == 6
