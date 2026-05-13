import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass

from openhachimi_agent.tools.planning import (
    create_todos,
    get_todos,
    update_todo,
    _SESSION_TODO_STATES,
    _get_state,
    TodoState
)

@dataclass
class MockRunContext:
    deps: MagicMock

@pytest.fixture
def mock_ctx(mock_agent_deps):
    return MockRunContext(deps=mock_agent_deps)

@pytest.fixture(autouse=True)
def clean_todo_states():
    # Clean global state before each test
    _SESSION_TODO_STATES.clear()
    yield
    _SESSION_TODO_STATES.clear()

def test_create_and_get_todos(mock_ctx):
    tasks = ["Task 1", "Task 2"]
    res = create_todos(mock_ctx, tasks)
    assert "Task 1" in res
    assert "Task 2" in res
    assert "[ ] 1." in res
    assert "[ ] 2." in res

def test_update_todo(mock_ctx):
    create_todos(mock_ctx, ["Task 1"])
    res = update_todo(mock_ctx, 1, "in-progress", "working on it")
    assert "[-]" in res
    assert "working on it" in res
    
    res2 = update_todo(mock_ctx, 1, "done", "finished")
    assert "[x]" in res2
    assert "finished" in res2

def test_lru_cache_eviction(mock_config):
    # Fill the cache with 105 items
    for i in range(105):
        deps = MagicMock()
        deps.session_id = f"session_{i}"
        deps.config = mock_config
        ctx = MockRunContext(deps=deps)
        _get_state(ctx)
    
    # Check that cache size doesn't exceed 100
    assert len(_SESSION_TODO_STATES) <= 100
    # oldest sessions should be evicted, so session_0 should not be in dict
    assert "session_0" not in _SESSION_TODO_STATES
    assert "session_104" in _SESSION_TODO_STATES
