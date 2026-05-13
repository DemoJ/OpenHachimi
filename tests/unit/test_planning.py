# pyrefly: ignore [missing-import]
import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass

from openhachimi_agent.tools.planning import (
    create_todos,
    get_todos,
    update_todo,
    _get_state,
    TodoState
)

@dataclass
class MockRunContext:
    deps: MagicMock

@pytest.fixture
def mock_ctx(mock_agent_deps):
    return MockRunContext(deps=mock_agent_deps)



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

