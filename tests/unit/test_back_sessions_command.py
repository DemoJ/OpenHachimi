"""/back 与 /sessions 命令单元测试。

覆盖:正常跳回(含写指针)、跨渠道拒绝、会话不存在、无参、非法格式 sid、
非法渠道拒绝、列会话渠道过滤、空列表、当前 latest 标记。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openhachimi_agent.service.agent_runtime.command_registry import (
    CommandOutcome,
    parse_command,
)


# ── 桩 ──────────────────────────────────────────────────────────────────────

def _build_service(
    *,
    exists: bool = True,
    target_channel: str = "cli",
    latest_sid: str | None = "sess-current",
    sessions: list[dict] | None = None,
) -> MagicMock:
    """构造带 session_store 桩的 service。

    ``target_channel``:被 /back 目标会话的渠道(经 get_channel 返回)。
    ``latest_sid``:当前 scope 的 latest 指针(经 get_latest_session_id 返回)。
    ``sessions``:service.list_sessions 返回的 sessions 列表。
    """
    service = MagicMock()
    service._normalize_role.side_effect = lambda role: role or "default"
    service._validate_role_exists.return_value = None
    service._normalize_session_id.side_effect = lambda sid: sid  # 透传,格式校验留给真实现

    store = MagicMock()
    store.session_exists.return_value = exists
    store.get_channel.return_value = target_channel
    store.get_latest_session_id.return_value = latest_sid
    store.set_latest_session_id.return_value = None
    service.session_store = store

    service.list_sessions.return_value = {
        "role": "default",
        "sessions": sessions if sessions is not None else [],
        "total": len(sessions) if sessions else 0,
        "limit": 10,
        "offset": 0,
    }
    return service


def _ctx(channel_code: str, scope: str = "cli") -> dict[str, object]:
    """构造带 channel_code 与 session_scope_key 的 channel_context。"""
    return {"channel_code": channel_code, "session_scope_key": scope}


# ── /back ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_back_switches_and_writes_pointer_for_same_channel():
    """目标会话属于当前渠道 → 切回并写 latest 指针。"""
    service = _build_service(target_channel="cli", latest_sid="sess-old")
    spec, args = parse_command("/back sess-target")

    outcome = await spec.handler(service, args, "default", "sess-cur", _ctx("cli"), "cli")

    assert outcome.kind == "new_session"
    assert outcome.session_id == "sess-target"
    assert outcome.role == "default"
    service.session_store.set_latest_session_id.assert_called_once_with(
        "default", "sess-target", "cli",
    )


@pytest.mark.asyncio
async def test_back_rejects_cross_channel():
    """目标会话属于他渠道 → 拒绝,不写指针。"""
    service = _build_service(target_channel="webui")
    spec, args = parse_command("/back sess-target")

    outcome = await spec.handler(service, args, "default", None, _ctx("telegram"), "telegram")

    assert outcome.kind == "info"
    assert "不属于当前渠道" in outcome.message
    service.session_store.set_latest_session_id.assert_not_called()


@pytest.mark.asyncio
async def test_back_rejects_nonexistent_session():
    """会话不存在 → 提示不存在,不写指针(不误判成渠道不匹配)。"""
    service = _build_service(exists=False, target_channel="webui")
    spec, args = parse_command("/back sess-ghost")

    outcome = await spec.handler(service, args, "default", None, _ctx("cli"), "cli")

    assert outcome.kind == "info"
    assert "会话不存在" in outcome.message
    service.session_store.set_latest_session_id.assert_not_called()


@pytest.mark.asyncio
async def test_back_requires_argument():
    """无参 → 提示用法。"""
    service = _build_service()
    spec, args = parse_command("/back")

    outcome = await spec.handler(service, args, "default", None, _ctx("cli"), "cli")

    assert outcome.kind == "info"
    assert "/back" in outcome.message
    service.session_store.set_latest_session_id.assert_not_called()


@pytest.mark.asyncio
async def test_back_invalid_format_caught_as_info():
    """非法格式 sid → _normalize_session_id 抛错被捕获为 info(而非 500)。"""
    service = _build_service()
    service._normalize_session_id.side_effect = ValueError("会话 ID 格式不合法。")
    spec, args = parse_command("/back bad/sid")

    outcome = await spec.handler(service, args, "default", None, _ctx("cli"), "cli")

    assert outcome.kind == "info"
    assert "跳转失败" in outcome.message
    service.session_store.set_latest_session_id.assert_not_called()


@pytest.mark.asyncio
async def test_back_rejects_unknown_channel():
    """channel_context 无 channel_code → 拒绝跳转。"""
    service = _build_service()
    spec, args = parse_command("/back sess-target")

    outcome = await spec.handler(service, args, "default", None, None, "local")

    assert outcome.kind == "info"
    assert "无法识别" in outcome.message
    service.session_store.set_latest_session_id.assert_not_called()


# ── /sessions ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sessions_lists_current_channel_only_and_marks_latest():
    """只列当前渠道会话,并给 latest 会话加 ⭐。"""
    sessions = [
        {"session_id": "sess-b", "preview": "第二条对话", "message_count": 4, "channel": "cli"},
        {"session_id": "sess-a", "preview": "你好", "message_count": 2, "channel": "cli"},
    ]
    service = _build_service(latest_sid="sess-a", sessions=sessions)
    spec, args = parse_command("/sessions")

    outcome = await spec.handler(service, args, "default", None, _ctx("cli"), "cli")

    assert outcome.kind == "info"
    assert "sess-a" in outcome.message
    assert "sess-b" in outcome.message
    # 当前 latest 会话带 ⭐
    assert "⭐ sess-a" in outcome.message
    # 非当前会话不带 ⭐(用空格缩进)
    assert "   sess-b" in outcome.message
    # service.list_sessions 必须以当前 channel 过滤
    service.list_sessions.assert_called_once()
    call = service.list_sessions.call_args
    assert call.kwargs.get("channel") == "cli"


@pytest.mark.asyncio
async def test_sessions_empty_channel_returns_hint():
    """当前渠道无会话 → 友好提示。"""
    service = _build_service(sessions=[])
    spec, args = parse_command("/历史")

    outcome = await spec.handler(service, args, "default", None, _ctx("telegram"), "telegram")

    assert outcome.kind == "info"
    assert "暂无历史会话" in outcome.message


@pytest.mark.asyncio
async def test_sessions_rejects_unknown_channel():
    """channel_context 无 channel_code → 拒绝列出。"""
    service = _build_service()
    spec, args = parse_command("/sessions")

    outcome = await spec.handler(service, args, "default", None, {}, "local")

    assert outcome.kind == "info"
    assert "无法识别" in outcome.message
    service.list_sessions.assert_not_called()
