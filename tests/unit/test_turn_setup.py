from types import SimpleNamespace

from openhachimi_agent.service.agent_runtime.turn_setup import _build_turn_deps


def test_build_turn_deps_uses_resolved_session_id(mock_config):
    """微信渠道传 session_id=None 时,记忆作用域必须用 load_context 解析后的会话 ID,
    否则 memory_turns.session_id NOT NULL 约束会让每轮记忆捕获都失败。"""
    service = SimpleNamespace(
        config=mock_config,
        browser_manager=None,
        process_manager=None,
        session_store=None,
    )
    inputs = SimpleNamespace(
        role="default",
        session_id=None,
        channel_name="weixin",
        effective_message="你好",
        channel_context_data={},
        scheduler_context={},
    )

    deps, scope, _ = _build_turn_deps(
        service, inputs, {}, run_mode="interactive", actual_session_id="20260821-123906-3ec0d74a",
    )

    assert scope.session_id == "20260821-123906-3ec0d74a"
    assert deps.session_id == "20260821-123906-3ec0d74a"
