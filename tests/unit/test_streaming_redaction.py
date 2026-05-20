# pyrefly: ignore [missing-import]
from openhachimi_agent.service.agent_runtime.streaming import (
    REDACTED,
    format_tool_call,
    redact_tool_args,
    summarize_tool_args,
)


def test_redact_tool_args_masks_sensitive_keys_recursively():
    args = {
        "api_key": "sk-secret123456789",
        "nested": {"password": "hunter2"},
        "items": [{"token": "ghp_secret123456789"}],
        "safe": "visible",
    }

    redacted = redact_tool_args(args)

    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["password"] == REDACTED
    assert redacted["items"][0]["token"] == REDACTED
    assert redacted["safe"] == "visible"


def test_summarize_tool_args_redacts_sensitive_string_patterns():
    summary = summarize_tool_args({"command": "curl -H 'Authorization: Bearer abcdefghijklmnop' https://example.com?api_key=secretvalue"})

    assert "abcdefghijklmnop" not in summary
    assert "secretvalue" not in summary
    assert REDACTED in summary


def test_format_tool_call_redacts_command_and_input_text():
    command_text = format_tool_call("run_command", {"command": "export TOKEN=secretvalue && deploy", "cwd": "."})
    input_text = format_tool_call("send_command_input", {"command_id": "cmd", "text": "password=hunter2"})

    assert "secretvalue" not in command_text
    assert "hunter2" not in input_text
    assert REDACTED in command_text
    assert REDACTED in input_text


def test_format_tool_call_redacts_browser_type_text():
    text = format_tool_call("browser_type", {"element_id": 1, "text": "api_key=secretvalue"})

    assert "secretvalue" not in text
    assert REDACTED in text
