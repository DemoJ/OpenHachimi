import pytest

from openhachimi_agent.content.prompts import load_system_prompt, render_system_prompt


def test_load_system_prompt_supports_root_prompt():
    assert "OpenHachimi" in load_system_prompt("base")


def test_load_system_prompt_supports_nested_prompt():
    assert "Planner Agent" in load_system_prompt("agents/planner")


def test_render_system_prompt_replaces_variables():
    rendered = render_system_prompt(
        "runtime/time",
        {"current_time": "2026-01-02 03:04:05", "weekday": "星期五"},
    )

    assert rendered.startswith("[系统环境] 当前真实时间: 2026-01-02 03:04:05(星期五)")


def test_render_system_prompt_requires_all_variables():
    with pytest.raises(ValueError, match="current_time"):
        render_system_prompt("runtime/time")


def test_load_system_prompt_rejects_traversal():
    with pytest.raises(ValueError):
        load_system_prompt("../base")
