# pyrefly: ignore [missing-import]
from openhachimi_agent.core.redaction import REDACTED
from openhachimi_agent.service.agent_runtime.streaming import (
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


def test_format_tool_call_create_todos_shows_task_summary():
    """create_todos 的工具调用展示应包含 goal + 任务列表,让用户在 telegram /
    WebUI 上能直接看到本次计划干什么。新版渲染为多行(标题独占一行 + 每项任务
    一行 + 有验收时缩进一行),不再用全角分号把整段压成一行。"""
    text = format_tool_call(
        "create_todos",
        {
            "goal": "调查浏览器访问网页无响应的根因，定位问题并提供解决方案",
            "tasks": [
                {"description": "环境检查", "success_criteria": "明确列出所有可用技能"},
                {"description": "浏览器技能详情分析", "success_criteria": "获取完整说明文档"},
                {"description": "日志与历史记录检查", "success_criteria": "找到失败日志"},
            ],
        },
    )

    # 标题独占一行,后面是多行明细块
    assert text.startswith("✅ 创建计划：\n")
    assert "\n目标：调查浏览器访问网页无响应的根因" in text
    assert "\n计划（共 3 项）：" in text
    # 每个 task description 应出现在明细块里
    assert "环境检查" in text
    assert "浏览器技能详情分析" in text
    assert "日志与历史记录检查" in text
    # 验收行应缩进展示
    assert "     验收：明确列出所有可用技能" in text
    # 整段必须含换行(多行渲染),而不是单行全角分号串
    assert text.count("\n") >= 5


def test_format_tool_call_create_todos_truncates_long_plan():
    """超过 6 项的 plan 概要应只显示前 6 项 + "…等 N 项"后缀,避免刷屏。"""
    tasks = [{"description": f"步骤 {i}"} for i in range(1, 10)]
    text = format_tool_call("create_todos", {"goal": "test goal", "tasks": tasks})

    assert "步骤 1" in text
    assert "步骤 6" in text
    assert "步骤 7" not in text
    assert "…等 3 项" in text
    # 总项数提示仍出现在标题里
    assert "计划（共 9 项）：" in text

