# pyrefly: ignore [missing-import]
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ToolCallPart,
    ToolReturnPart,
)

from openhachimi_agent.core.redaction import REDACTED
from openhachimi_agent.service.agent_runtime.streaming import (
    ToolTraceEntry,
    _record_tool_trace,
    format_tool_call,
    format_tool_trace,
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


# ── 工具执行痕迹收集与渲染(失败轮兜底落库用) ─────────────────────────────────


def test_record_tool_trace_collects_calls_and_outcomes():
    """流式事件 → trace:工具调用 append,结果返回补写 outcome。"""
    trace: list[ToolTraceEntry] = []

    _record_tool_trace(
        trace,
        FunctionToolCallEvent(part=ToolCallPart(tool_name="run_command", args={"command": "ls", "cwd": "."})),
    )
    _record_tool_trace(
        trace,
        FunctionToolCallEvent(part=ToolCallPart(tool_name="write_file", args={"path": "a.py", "content": "x"})),
    )
    # 第一个工具的结果返回
    _record_tool_trace(
        trace,
        FunctionToolResultEvent(
            part=ToolReturnPart(tool_name="run_command", content="done", outcome="success"),
        ),
    )

    assert len(trace) == 2
    assert trace[0].tool_name == "run_command"
    assert trace[0].outcome == "success"
    assert trace[1].tool_name == "write_file"
    assert trace[1].outcome is None  # 未返回结果


def test_record_tool_trace_redacts_sensitive_args():
    """trace 文本走 format_tool_call 脱敏,密钥不落库。"""
    trace: list[ToolTraceEntry] = []
    _record_tool_trace(
        trace,
        FunctionToolCallEvent(
            part=ToolCallPart(tool_name="run_command", args={"command": "curl -H 'Authorization: Bearer secretvalue'"})
        ),
    )
    assert len(trace) == 1
    assert "secretvalue" not in trace[0].text
    assert REDACTED in trace[0].text


def test_record_tool_trace_skips_clarify_user():
    """clarify_user 是 deferred 工具,不进入 trace(其输出直接作为回复给用户)。"""
    trace: list[ToolTraceEntry] = []
    _record_tool_trace(
        trace,
        FunctionToolCallEvent(part=ToolCallPart(tool_name="clarify_user", args={"question": "?"})),
    )
    assert trace == []


def test_format_tool_trace_renders_steps_and_results():
    """中断摘要渲染:含已执行步骤与结果,指引下一轮继续执行而非从头重跑。"""
    trace = [
        ToolTraceEntry(tool_name="run_command", text="执行命令：gh api ...", outcome="success"),
        ToolTraceEntry(tool_name="write_file", text="写入文件 a.py", outcome="failed"),
        ToolTraceEntry(tool_name="browser_navigate", text="打开网页：URL：https://example.com", outcome=None),
    ]
    body = format_tool_trace(trace)

    assert "[系统记录]" in body and "上一轮任务执行中断" in body
    assert "1. 执行命令：gh api ...（成功）" in body
    assert "2. 写入文件 a.py（失败）" in body
    assert "3. 打开网页：URL：https://example.com（未返回结果）" in body
    assert "不要重复已完成步骤" in body


def test_format_tool_trace_empty():
    """无痕迹时返回空串,调用方据此跳过落库。"""
    assert format_tool_trace([]) == ""


# ── watchdog 语义:tool 阶段只留长兜底(超时由工具层 wait_for 回灌 LLM) ─────────


def test_watchdog_tool_phase_is_long_fallback_only():
    """tool 阶段的 watchdog 超时必须是长兜底(>=600s),不再承担具体工具超时。

    具体工具超时(如 run_command wait_seconds=60)已由 execution.with_execution_ledger
    的 wait_for 在工具层处理并回灌 LLM;watchdog 只防 wait_for 失效的死锁。
    """
    from types import SimpleNamespace

    from openhachimi_agent.service.agent_runtime.context import OperationState
    from openhachimi_agent.service.agent_runtime.streaming import (
        _operation_timeout_seconds,
        _update_operation_from_event,
    )

    config = SimpleNamespace(stream_idle_timeout_seconds=60, agent_timeout_seconds=300)

    state = OperationState()
    _update_operation_from_event(
        state,
        FunctionToolCallEvent(part=ToolCallPart(tool_name="run_command", args={"command": "ls", "wait_seconds": 60})),
    )
    # tool 阶段 → 长兜底,不会 60s 误杀
    assert _operation_timeout_seconds(state, config) == 600

    # model 阶段(LLM 无响应)仍是 180s 兜底
    _update_operation_from_event(
        state,
        FunctionToolResultEvent(part=ToolReturnPart(tool_name="run_command", content="ok")),
    )
    assert state.kind == "model"
    assert _operation_timeout_seconds(state, config) == 300

