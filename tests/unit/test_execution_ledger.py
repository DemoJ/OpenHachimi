from dataclasses import dataclass

import pytest
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.agent.execution import (
    get_execution_ledger,
    get_final_verification_signal,
    get_ledger_length,
    get_replan_signal,
    with_execution_ledger,
)
from openhachimi_agent.tools.planning import create_todos, update_todo


@dataclass
class MockRunContext:
    deps: object


def test_execution_ledger_records_success(mock_agent_deps):
    def read_file(ctx, path: str):
        return {"path": path, "content": "hello"}

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(read_file)

    assert guarded(ctx, "README.md") == {"path": "README.md", "content": "hello"}
    ledger = get_execution_ledger(ctx)

    assert [event["status"] for event in ledger] == ["started", "succeeded"]
    assert ledger[0]["tool_name"] == "read_file"
    assert ledger[0]["args"]["path"] == "README.md"
    assert get_ledger_length(mock_agent_deps.session_state) == 2


def test_execution_ledger_records_tool_failure(mock_agent_deps):
    """可恢复的工具异常被吞成错误字符串回灌给 LLM,ledger 仍记 started + failed。

    (旧版断言 ``RuntimeError`` 直接冒泡;改为"错误回灌"后,普通运行时错误不再
    中断整轮,而是返回带 ``[工具执行出错]`` 前缀的脱敏字符串,ledger 照常记失败。)
    """
    def write_file(ctx):
        raise RuntimeError("disk full")

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(write_file)

    result = guarded(ctx)

    assert "[工具执行出错]" in result
    assert "RuntimeError" in result
    assert "disk full" in result
    ledger = get_execution_ledger(ctx)
    assert [event["status"] for event in ledger] == ["started", "failed"]
    assert ledger[-1]["tool_name"] == "write_file"


def test_tool_failure_redacts_sensitive_detail(mock_agent_deps):
    """回灌给 LLM 的错误字符串必须抹掉 api_key/token 等敏感信息。"""
    def web_fetch(ctx):
        raise RuntimeError("请求失败 api_key=sk-secret1234567890 token=abc")

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(web_fetch)

    result = guarded(ctx)

    assert "[工具执行出错]" in result
    assert "[REDACTED]" in result
    assert "sk-secret1234567890" not in result


def test_model_retry_still_propagates(mock_agent_deps):
    """``ModelRetry`` 是工具主动要求 LLM 重试的信号,必须原样向上抛,不能被吞。"""
    def read_file(ctx):
        raise ModelRetry("文件不存在,请检查路径后重试")

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(read_file)

    with pytest.raises(ModelRetry):
        guarded(ctx)


def test_call_deferred_still_propagates(mock_agent_deps):
    """``CallDeferred`` 是 pydantic-ai deferred 控制流(clarify_user 依赖),必须透传。"""
    from pydantic_ai.exceptions import CallDeferred

    def clarify_user(ctx):
        raise CallDeferred(metadata={"kind": "clarify_user"})

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(clarify_user)

    with pytest.raises(CallDeferred):
        guarded(ctx)


def test_async_tool_failure_returned_as_string(mock_agent_deps):
    """异步工具的可恢复异常同样被吞成错误字符串(覆盖 async_wrapper 分支)。"""
    async def browser_navigate(ctx):
        raise RuntimeError("等待浏览器 CDP 端口 53797 就绪超时(已等待 45s)。")

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(browser_navigate)

    import asyncio

    result = asyncio.run(guarded(ctx))

    assert "[工具执行出错]" in result
    assert "CDP" in result
    ledger = get_execution_ledger(ctx)
    assert [event["status"] for event in ledger] == ["started", "failed"]





def test_replan_signal_requires_consecutive_failures(mock_agent_deps):
    """单次 blocked/failed 不触发 replan，连续 >=2 次才触发。"""
    # 单次 failed 后不触发 replan
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "browser_navigate", "status": "started"},
        {"seq": 2, "tool_name": "browser_navigate", "status": "failed", "result_preview": "timeout"},
    ]
    assert get_replan_signal(mock_agent_deps.session_state, since_seq=0) is None

    # 单次 failed 后跟 succeeded，不触发 replan
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "browser_navigate", "status": "blocked", "violation": "old"},
        {"seq": 2, "tool_name": "browser_navigate", "status": "started"},
        {"seq": 3, "tool_name": "browser_navigate", "status": "succeeded"},
    ]
    assert get_replan_signal(mock_agent_deps.session_state, since_seq=0) is None

    # 连续 2 次 failed，触发 replan
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "browser_navigate", "status": "started"},
        {"seq": 2, "tool_name": "browser_navigate", "status": "failed", "result_preview": "timeout"},
        {"seq": 3, "tool_name": "run_command", "status": "failed", "result_preview": "boom"},
    ]
    signal = get_replan_signal(mock_agent_deps.session_state, since_seq=0)
    assert signal is not None
    assert signal["consecutive_failures"] == 2
    assert signal["latest_status"] == "failed"


def test_final_verifier_detects_unfinished_todos(mock_agent_deps):
    ctx = MockRunContext(deps=mock_agent_deps)
    create_todos(ctx, ["Open the page"])

    signal = get_final_verification_signal(mock_agent_deps.session_state)

    assert signal is not None
    assert signal["issues"][0]["type"] == "unfinished_todos"

    update_todo(ctx, 1, "done", "opened")
    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_treats_blocked_as_terminal(mock_agent_deps):
    """blocked 是模型诚实声明的合法终止态(缺资源/缺凭据/外部条件不满足),
    不应被当成"未完成证据"触发"[最终验证未通过]"提示。两个任务一个 done
    一个 blocked → 应返回 None。"""
    ctx = MockRunContext(deps=mock_agent_deps)
    create_todos(ctx, ["fetch", "send mail"])
    update_todo(ctx, 1, "done", "fetched")
    update_todo(ctx, 2, "blocked", "no smtp credentials available")

    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_ignores_blocked_ledger_event(mock_agent_deps):
    """ledger 里 status=blocked 的事件来自 ExecutionGuardViolation(内部信号),
    不应被当成"最近一次工具失败"上报给用户。"""
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "write_file", "status": "blocked", "violation": "guard"}
    ]
    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_detects_latest_failed_event(mock_agent_deps):
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "run_command", "status": "failed", "result_preview": "boom"}
    ]

    signal = get_final_verification_signal(mock_agent_deps.session_state)

    assert signal is not None
    assert signal["issues"][0]["type"] == "latest_execution_not_successful"


def test_final_verifier_ignores_suspended_todos(mock_agent_deps):
    ctx = MockRunContext(deps=mock_agent_deps)
    create_todos(ctx, ["Open the page"])
    mock_agent_deps.session_state["todo_state"].is_active = False

    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_ignores_previous_turn_failed_event(mock_agent_deps):
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "run_command", "status": "failed", "result_preview": "old boom"},
        {"seq": 2, "tool_name": "read_file", "status": "succeeded", "result_preview": "ok"},
    ]
    mock_agent_deps.session_state["current_turn_ledger_start_seq"] = 1

    assert get_final_verification_signal(mock_agent_deps.session_state) is None
