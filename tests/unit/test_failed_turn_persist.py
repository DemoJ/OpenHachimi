# pyrefly: ignore [missing-import]
"""``_persist_failed_turn_user_message`` 失败兜底落库单元测试。

覆盖 agent 调用失败(CDP 超时 / 模型错误等)时,本轮用户输入的兜底落库契约:

- 核心目标:失败兜底落库后,下一轮 ``load_context`` 能读到本轮用户消息,
  agent 不再"忘了刚才聊什么"。
- clarify_user deferred 路径跳过:用户回复以 deferred tool result 灌回模型,
  不应再作为独立 user 消息落库。
- 幂等:落库走 append-only 续编;且下一轮成功路径 ``_persist_turn`` 的
  ``new_history[len(history):]`` 会自然跳过已落库部分,不重复。

不依赖真实 LLM,使用真实 ``SessionStore``(schema 自举) + 最小 ``AgentRunContext``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from openhachimi_agent.service.agent_runtime.context import AgentRunContext
from openhachimi_agent.service.agent_runtime.context_snapshot import (
    _USER_MESSAGE_METADATA_KEY,
)
from openhachimi_agent.service.agent_runtime.turn_postprocess import (
    _persist_failed_turn_user_message,
)
from openhachimi_agent.storage.session_store import SessionStore


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.sqlite3")


def _new_sid() -> str:
    return SessionStore.new_session_id()


def _make_ctx(
    *,
    session_state: dict | None = None,
    history: list | None = None,
    message: str = "帮我总结一下",
) -> AgentRunContext:
    """最小 AgentRunContext —— 兜底函数只读 session_state,其余字段给占位即可。"""
    return AgentRunContext(
        config=SimpleNamespace(),
        role="default",
        session_id="sess-1",
        message=message,
        attachments=[],
        history=history or [],
        deps=MagicMock(),
        session_state=session_state or {},
        stream=False,
    )


def _stub_service(store: SessionStore) -> SimpleNamespace:
    return SimpleNamespace(session_store=store)


# ── 核心目标:失败后下一轮能读到用户消息 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_turn_user_message_visible_next_round(store: SessionStore):
    """agent 失败 → 兜底落库用户消息 → 下一轮 load_context 能读到它。

    这是修复的核心目标:失败前 history 为 [u0, r0],本轮用户说"帮我总结一下"但
    agent 崩了;兜底落库后,下一轮 load_context 应返回 [u0, r0, u1],u1 即本轮输入。
    """
    sid = _new_sid()
    # 失败前已有历史:一轮完整对话
    prior = [
        ModelRequest(parts=[UserPromptPart(content="第一条")]),
        ModelResponse(parts=[TextPart(content="回复一")]),
    ]
    store.save_messages("default", sid, prior, channel="webui")
    _, history = store.load_context("default", sid)
    assert len(history) == 2

    ctx = _make_ctx(history=history, message="帮我总结一下")
    service = _stub_service(store)

    await _persist_failed_turn_user_message(
        service, ctx,
        role="default", actual_session_id=sid,
        latest_scope=None, resolved_channel_code="webui",
        user_message="帮我总结一下",
    )

    # 下一轮 load_context:应能看到兜底落库的用户消息
    _, reloaded = store.load_context("default", sid)
    assert len(reloaded) == 3
    fallback = reloaded[-1]
    assert isinstance(fallback, ModelRequest)
    contents = [getattr(p, "content", None) for p in fallback.parts]
    assert "帮我总结一下" in contents
    # metadata 打了用户原话,展示侧可还原
    assert fallback.metadata.get(_USER_MESSAGE_METADATA_KEY) == "帮我总结一下"


# ── clarify_user deferred 路径跳过 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_when_clarify_user_pending(store: SessionStore):
    """_user_clarification 为真(等用户回复 deferred tool call)时不落库。

    用户回复以 deferred tool result 灌回模型,若再落独立 user 消息,下一轮会出现
    "用户消息 + 未消费 tool return"双入口,语义错乱。
    """
    sid = _new_sid()
    prior = [ModelRequest(parts=[UserPromptPart(content="原任务")])]
    store.save_messages("default", sid, prior)
    _, history = store.load_context("default", sid)

    ctx = _make_ctx(
        session_state={"_user_clarification": {"tool_call_id": "call-1", "question": "?"}},
        history=history,
        message="用户的澄清回复",
    )
    service = _stub_service(store)

    await _persist_failed_turn_user_message(
        service, ctx,
        role="default", actual_session_id=sid,
        latest_scope=None, resolved_channel_code=None,
        user_message="用户的澄清回复",
    )

    _, reloaded = store.load_context("default", sid)
    assert len(reloaded) == 1  # 没有追加任何消息


# ── 空消息不落库 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_empty_message(store: SessionStore):
    """空用户消息(纯空白)不落库,避免无意义空行污染历史。"""
    sid = _new_sid()
    ctx = _make_ctx(message="   ")
    service = _stub_service(store)

    await _persist_failed_turn_user_message(
        service, ctx,
        role="default", actual_session_id=sid,
        latest_scope=None, resolved_channel_code=None,
        user_message="   ",
    )

    _, reloaded = store.load_context("default", sid)
    assert reloaded == []


# ── 幂等:落库失败不抛异常,不掩盖原始 agent 错误 ──────────────────────────────


@pytest.mark.asyncio
async def test_persist_failure_swallowed(store: SessionStore):
    """session_store.save_messages 抛异常时,兜底函数只 warn 不抛。

    设计约束:兜底落库绝不能掩盖原始 agent 错误。本测试构造一个 save_messages
    必然失败的 service(传非法 role),验证 _persist_failed_turn_user_message 不抛。
    """
    ctx = _make_ctx(message="hi")
    service = _stub_service(store)
    # 让 save_messages 因 role 校验失败抛 ValueError(validate_role_name 拒绝空串)
    service.session_store = MagicMock()
    service.session_store.save_messages.side_effect = ValueError("bad role")

    # 不应抛异常
    await _persist_failed_turn_user_message(
        service, ctx,
        role="", actual_session_id="sess-1",
        latest_scope=None, resolved_channel_code=None,
        user_message="hi",
    )


# ── 幂等:append-only 续编,不覆盖已有历史 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_append_only_does_not_overwrite(store: SessionStore):
    """兜底落库走 append-only:已有 2 条 → 兜底 1 条 → 共 3 条,原 2 条不变。

    与正常 ``save_messages`` 一致:turn_index 从 MAX+1 续编,INSERT OR IGNORE
    按主键去重,保证可重入。
    """
    sid = _new_sid()
    prior = [
        ModelRequest(parts=[UserPromptPart(content="u0")]),
        ModelResponse(parts=[TextPart(content="r0")]),
    ]
    store.save_messages("default", sid, prior)
    _, history = store.load_context("default", sid)

    ctx = _make_ctx(history=history, message="失败轮用户输入")
    service = _stub_service(store)

    await _persist_failed_turn_user_message(
        service, ctx,
        role="default", actual_session_id=sid,
        latest_scope=None, resolved_channel_code=None,
        user_message="失败轮用户输入",
    )

    _, reloaded = store.load_context("default", sid)
    assert len(reloaded) == 3
    # 前 2 条原样保留
    assert isinstance(reloaded[0], ModelRequest)
    assert "u0" in [getattr(p, "content", None) for p in reloaded[0].parts]
    assert isinstance(reloaded[1], ModelResponse)
    # 第 3 条是兜底落库的
    assert isinstance(reloaded[2], ModelRequest)
    assert "失败轮用户输入" in [getattr(p, "content", None) for p in reloaded[2].parts]
