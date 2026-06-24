"""Execution ledger and TaskFrame-aware action checks."""

from __future__ import annotations

import inspect
import json
import time
from functools import wraps
from typing import Any, Callable


_LEDGER_LIMIT = 200



def _summarize(value: object, max_chars: int = 800) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _get_session_state(ctx: object) -> dict[str, Any]:
    deps = getattr(ctx, "deps", None)
    state = getattr(deps, "session_state", None)
    if isinstance(state, dict):
        return state
    return {}


def _bind_tool_args(func: Callable, ctx: object, args: tuple, kwargs: dict) -> dict[str, object]:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(ctx, *args, **kwargs)
        return {key: value for key, value in bound.arguments.items() if key != "ctx"}
    except Exception:
        raw_args = list(args)
        if kwargs:
            raw_args.append(kwargs)
        return {"args": raw_args}


def _current_task_id(session_state: dict[str, Any]) -> int | None:
    todo_state = session_state.get("todo_state")
    tasks = getattr(todo_state, "tasks", None)
    if not isinstance(tasks, dict):
        return None
    in_progress = [task for task in tasks.values() if getattr(task, "status", None) == "in-progress"]
    if len(in_progress) == 1:
        return int(getattr(in_progress[0], "id"))
    return None


def _append_ledger_event(
    session_state: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    args: dict[str, object] | None = None,
    result: object = None,
    violation: str = "",
) -> None:
    ledger = session_state.setdefault("execution_ledger", [])
    if not isinstance(ledger, list):
        ledger = []
        session_state["execution_ledger"] = ledger

    event = {
        "seq": len(ledger) + 1,
        "ts": time.time(),
        "tool_name": tool_name,
        "status": status,
        "task_id": _current_task_id(session_state),
        "args": args or {},
        "result_preview": _summarize(result) if result is not None else "",
        "violation": violation,
    }
    ledger.append(event)
    if len(ledger) > _LEDGER_LIMIT:
        del ledger[: len(ledger) - _LEDGER_LIMIT]


def _exception_ledger_status(exc: Exception) -> str:
    # CallDeferred / ApprovalRequired 是 pydantic-ai 的"挂起本次 run"控制流
    # 信号(由 clarify_user 等 deferred 工具主动抛出),不是错误。ledger 里把它
    # 记成 ``deferred``,避免污染 ``get_replan_signal`` 的连续 blocked 计数,也
    # 让 ``get_final_verification_signal`` 不会错误地把它当成"最近一次失败"。
    from pydantic_ai.exceptions import ApprovalRequired, CallDeferred

    if isinstance(exc, (CallDeferred, ApprovalRequired)):
        return "deferred"
    return "blocked" if getattr(exc, "ledger_status", "") == "blocked" else "failed"


def _exception_violation(exc: Exception, status: str) -> str:
    return str(exc) if status == "blocked" else ""


def get_execution_ledger(ctx: object) -> list[dict[str, Any]]:
    """Return the in-memory ledger for tests, diagnostics and future tools."""

    ledger = _get_session_state(ctx).get("execution_ledger", [])
    return ledger if isinstance(ledger, list) else []


def get_ledger_length(session_state: dict[str, Any]) -> int:
    ledger = session_state.get("execution_ledger", [])
    return len(ledger) if isinstance(ledger, list) else 0


def get_replan_signal(session_state: dict[str, Any], since_seq: int = 0) -> dict[str, object] | None:
    """Return a compact replan signal when there are consecutive failures in new ledger events.
    
    只有在新事件中存在连续 >= 2 次 blocked/failed 且尾部没有被 succeeded 覆盖时才触发 replan。
    单次 blocked（通常是 ModelRetry 后已成功重试）不应触发昂贵的重规划。
    """

    ledger = session_state.get("execution_ledger", [])
    if not isinstance(ledger, list):
        return None
    new_events = [
        event for event in ledger
        if isinstance(event, dict) and int(event.get("seq", 0)) > since_seq
    ]
    if not new_events:
        return None

    # 检查尾部是否以 blocked/failed 结尾
    latest = new_events[-1]
    if latest.get("status") not in {"blocked", "failed"}:
        return None

    # 统计尾部连续的 blocked/failed 次数
    consecutive_failures = 0
    for event in reversed(new_events):
        if event.get("status") in {"blocked", "failed"}:
            consecutive_failures += 1
        else:
            break

    # 单次失败不触发 replan，通常是 ModelRetry 后已自动重试成功
    if consecutive_failures < 2:
        return None

    notable_events = [
        event for event in new_events
        if event.get("status") in {"blocked", "failed"}
    ][-5:]
    summary = []
    for event in notable_events:
        detail = event.get("violation") or event.get("result_preview") or ""
        summary.append(
            {
                "seq": event.get("seq"),
                "tool_name": event.get("tool_name"),
                "status": event.get("status"),
                "task_id": event.get("task_id"),
                "args": event.get("args", {}),
                "detail": detail,
            }
        )

    return {
        "reason": "consecutive execution failures require replan",
        "consecutive_failures": consecutive_failures,
        "latest_status": latest.get("status"),
        "events": summary,
    }


def get_final_verification_signal(session_state: dict[str, Any]) -> dict[str, object] | None:
    """Check whether the run has enough evidence to claim completion.

    "未完成"只包含 ``pending`` / ``in-progress`` —— 这两种状态意味着任务还没动
    或正在进行,模型不应在此时声明完成。``blocked`` 与 ``done`` 都视为终止态:
    ``done`` 是成功完成,``blocked`` 是模型已诚实声明"这一步走不通(缺资源/缺
    凭据/外部条件不满足)"。把 blocked 也算成"未完成证据"会让用户在合法暂停态
    上看到"[最终验证未通过] 当前执行结果仍缺少完成证据" 之类的吓人提示,而那
    其实只是模型按要求把 task 标了 blocked 而已。
    """

    issues: list[dict[str, object]] = []

    todo_state = session_state.get("todo_state")
    tasks = getattr(todo_state, "tasks", None)
    if getattr(todo_state, "is_active", False) and isinstance(tasks, dict) and tasks:
        unfinished = [
            {
                "id": getattr(task, "id", task_id),
                "description": getattr(task, "description", ""),
                "status": getattr(task, "status", ""),
            }
            for task_id, task in tasks.items()
            if getattr(task, "status", None) not in {"done", "blocked"}
        ]
        if unfinished:
            issues.append({"type": "unfinished_todos", "items": unfinished})

    ledger = session_state.get("execution_ledger", [])
    if isinstance(ledger, list) and ledger:
        turn_start_seq = int(session_state.get("current_turn_ledger_start_seq", 0) or 0)
        current_turn_events = [
            event for event in ledger
            if isinstance(event, dict) and int(event.get("seq", 0)) > turn_start_seq
        ]
        latest = current_turn_events[-1] if current_turn_events else None
        if isinstance(latest, dict) and latest.get("status") == "failed":
            # 注意:这里只看 ``failed``,不再看 ``blocked``。``blocked`` 在
            # ledger 里有两种来源:
            # 1. 工具体内 raise ExecutionGuardViolation(planning.py) —— 这是
            #    "模型违反 TODO 守卫"的内部信号,跟用户层"任务被阻塞"无关;
            # 2. validator 打回 —— 已通过 final-answer validator 的 pass-through
            #    机制独立处理。
            # 把 ``blocked`` 当成"最近一次执行失败"会和 unfinished_todos 的过滤
            # (blocked 任务被视为合法终止)产生语义冲突。
            issues.append(
                {
                    "type": "latest_execution_not_successful",
                    "tool_name": latest.get("tool_name"),
                    "status": latest.get("status"),
                    "detail": latest.get("violation") or latest.get("result_preview") or "",
                }
            )

    if not issues:
        return None
    return {
        "reason": "final verification failed",
        "issues": issues,
    }


def with_execution_ledger(func: Callable) -> Callable:
    """Record tool execution and block actions that contradict the active TaskFrame."""

    tool_name = getattr(func, "__name__", "unknown_tool")

    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(ctx, *args, **kwargs):
            session_state = _get_session_state(ctx)
            bound_args = _bind_tool_args(func, ctx, args, kwargs)
            _append_ledger_event(session_state, tool_name=tool_name, status="started", args=bound_args)
            try:
                result = await func(ctx, *args, **kwargs)
            except Exception as exc:
                status = _exception_ledger_status(exc)
                _append_ledger_event(
                    session_state,
                    tool_name=tool_name,
                    status=status,
                    args=bound_args,
                    result=exc,
                    violation=_exception_violation(exc, status),
                )
                raise
            _append_ledger_event(session_state, tool_name=tool_name, status="succeeded", args=bound_args, result=result)
            return result
        return async_wrapper

    @wraps(func)
    def sync_wrapper(ctx, *args, **kwargs):
        session_state = _get_session_state(ctx)
        bound_args = _bind_tool_args(func, ctx, args, kwargs)
        _append_ledger_event(session_state, tool_name=tool_name, status="started", args=bound_args)
        try:
            result = func(ctx, *args, **kwargs)
        except Exception as exc:
            status = _exception_ledger_status(exc)
            _append_ledger_event(
                session_state,
                tool_name=tool_name,
                status=status,
                args=bound_args,
                result=exc,
                violation=_exception_violation(exc, status),
            )
            raise
        _append_ledger_event(session_state, tool_name=tool_name, status="succeeded", args=bound_args, result=result)
        return result
    return sync_wrapper
