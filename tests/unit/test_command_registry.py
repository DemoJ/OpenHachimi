"""command_registry 单元测试:覆盖别名解析、channel 过滤、帮助文案生成、各 handler 行为。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openhachimi_agent.service.agent_runtime.command_registry import (
    CommandOutcome,
    build_help_text,
    iter_for_help,
    iter_for_tg_menu,
    parse_command,
)


# ── 别名解析 ────────────────────────────────────────────────────────────────

def test_parse_command_recognizes_english_alias():
    parsed = parse_command("/compress")
    assert parsed is not None
    spec, args = parsed
    assert spec.name == "compress"
    assert args == ""


def test_parse_command_recognizes_chinese_alias():
    parsed = parse_command("/压缩")
    assert parsed is not None
    spec, _ = parsed
    assert spec.name == "compress"


def test_parse_command_extracts_args():
    parsed = parse_command("/compress 用户认证模块")
    assert parsed is not None
    spec, args = parsed
    assert spec.name == "compress"
    assert args == "用户认证模块"


def test_parse_command_extracts_chinese_args():
    parsed = parse_command("/role default")
    assert parsed is not None
    spec, args = parsed
    assert spec.name == "role"
    assert args == "default"


def test_parse_command_handles_single_token_aliases():
    """`q` 等无 `/` 前缀的别名整串匹配。"""
    parsed = parse_command("q")
    assert parsed is not None
    assert parsed[0].name == "exit"


def test_parse_command_returns_none_for_plain_text():
    assert parse_command("你好") is None
    assert parse_command("") is None
    assert parse_command("   ") is None


def test_parse_command_no_partial_word_match():
    """`/compression` 不应误命中 `/compress`。"""
    assert parse_command("/compression") is None


# ── channel 过滤 ────────────────────────────────────────────────────────────

def test_iter_for_help_filters_by_channel():
    """exit 命令限定 channel={"cli"};非 cli 渠道看不到。"""
    cli_specs = {spec.name for spec in iter_for_help("cli")}
    tg_specs = {spec.name for spec in iter_for_help("telegram")}
    assert "exit" in cli_specs
    assert "exit" not in tg_specs


def test_iter_for_tg_menu_excludes_cli_only_and_hidden():
    menu_names = {spec.name for spec in iter_for_tg_menu()}
    # exit 限定 cli,不在 telegram 菜单
    assert "exit" not in menu_names
    # help / start 显式 show_in_tg_menu=False
    assert "help" not in menu_names
    assert "start" not in menu_names
    # roles / role / new / stop / compress 应出现
    assert {"roles", "role", "new", "stop", "compress"}.issubset(menu_names)


def test_build_help_text_contains_aliases_and_args_hint():
    text = build_help_text("cli")
    assert "/compress" in text
    assert "/压缩" in text  # 同条目里展示别名
    assert "[主题]" in text  # args_hint
    assert "<名称>" in text  # /role <名称>


def test_build_help_text_excludes_hidden_commands():
    text = build_help_text("cli")
    # start 是 show_in_help=False
    assert "/start" not in text


# ── handler 行为(用 mock service)──────────────────────────────────────────

@pytest.fixture
def mock_service():
    """构造一个仅暴露被 handler 调用方法的 AgentService 桩。"""
    service = MagicMock()
    service.list_roles.return_value = SimpleNamespace(roles=["default", "writer"], current_role="default")
    service.new_session.return_value = SimpleNamespace(
        message="新会话已就绪",
        role="default",
        session_id="sess-new",
    )
    service.switch_role.return_value = SimpleNamespace(
        message="已切换到角色:writer",
        role="writer",
        session_id="sess-writer",
    )
    service.stop_session = AsyncMock(return_value=SimpleNamespace(
        message="已成功中断当前任务。",
        role="default",
        session_id="sess-default",
    ))
    service.compress_session = AsyncMock(return_value=SimpleNamespace(
        output="已压缩上下文:10→6 条消息。",
        role="default",
        session_id="sess-default",
    ))
    service._normalize_role.side_effect = lambda role: role or "default"
    service._resolve_priority_session.return_value = ("default", "sess-default")
    service.latest_session.return_value = SimpleNamespace(role="default", session_id="sess-default")
    return service


@pytest.mark.asyncio
async def test_help_handler_returns_text_outcome(mock_service):
    parsed = parse_command("/help")
    assert parsed is not None
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, None, None, None, "cli")
    assert isinstance(outcome, CommandOutcome)
    assert outcome.kind == "help"
    assert "/compress" in outcome.message


@pytest.mark.asyncio
async def test_roles_handler_lists_roles_with_marker(mock_service):
    parsed = parse_command("/roles")
    assert parsed is not None
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, "default", "sess-1", None, "cli")
    assert outcome.kind == "info"
    assert "default" in outcome.message
    assert "writer" in outcome.message
    assert "(当前)" in outcome.message


@pytest.mark.asyncio
async def test_role_handler_requires_argument(mock_service):
    parsed = parse_command("/role")
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, "default", "sess-1", None, "cli")
    assert outcome.role is None  # 无参数时不切换
    mock_service.switch_role.assert_not_called()


@pytest.mark.asyncio
async def test_role_handler_switches(mock_service):
    parsed = parse_command("/role writer")
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, "default", "sess-1", None, "cli")
    assert outcome.kind == "switch_role"
    assert outcome.role == "writer"
    assert outcome.session_id == "sess-writer"
    mock_service.switch_role.assert_called_once_with("writer", latest_scope=None)
    # 切换前会尝试停掉旧会话
    mock_service.stop_session.assert_awaited()


@pytest.mark.asyncio
async def test_compress_handler_passes_focus_topic(mock_service):
    parsed = parse_command("/压缩 认证模块")
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, "default", "sess-1", None, "telegram")
    assert outcome.kind == "compress"
    mock_service.compress_session.assert_awaited_once()
    call_args = mock_service.compress_session.await_args
    # service.compress_session(role, session_id, focus, scope)
    assert call_args.args[2] == "认证模块"


@pytest.mark.asyncio
async def test_stop_handler_invokes_service(mock_service):
    parsed = parse_command("/stop")
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, "default", "sess-1", None, "cli")
    assert outcome.kind == "stop"
    mock_service.stop_session.assert_awaited_once_with("sess-default")


@pytest.mark.asyncio
async def test_new_handler_starts_new_session(mock_service):
    parsed = parse_command("/新对话")
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, "default", "sess-1", None, "telegram")
    assert outcome.kind == "new_session"
    assert outcome.session_id == "sess-new"
    mock_service.new_session.assert_called_once()


@pytest.mark.asyncio
async def test_exit_handler_returns_exit_kind(mock_service):
    parsed = parse_command("/exit")
    spec, args = parsed
    outcome = await spec.handler(mock_service, args, None, None, None, "cli")
    assert outcome.kind == "exit"
