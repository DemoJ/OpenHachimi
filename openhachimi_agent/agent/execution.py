"""Execution ledger and TaskFrame-aware action checks."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from functools import wraps
from typing import Any, Callable


_LEDGER_LIMIT = 200

_logger = logging.getLogger(__name__)

# run_command 工具内部 wait_seconds 的上限(command.py MAX_RUN_COMMAND_WAIT_SECONDS)。
# 工具层超时据此给余量,保证 run_command 总能正常返回后台句柄而不是被掐死。
_RUN_COMMAND_WAIT_LIMIT_SECONDS = 120
_RUN_COMMAND_TIMEOUT_MARGIN_SECONDS = 30


def tool_timeout_seconds(tool_name: str, args: dict[str, object] | None, config: object | None) -> int:
    """单个工具调用的超时阈值(秒):超时时中断该次调用并把超时事实回灌给 LLM。

    阈值按工具类型分类,与历史上 streaming watchdog 的分类口径一致:
    - run_command 按模型给出的 wait_seconds 动态放宽(上限 120s + 30s 余量),
      避免"wait_seconds>60 必被误杀"的历史 bug 在工具层重演;
    - 浏览器/网络/文件检索类给固定阈值;
    - 兜底取 max(120, stream_idle_timeout_seconds * 3)。

    这是"工具超时只中断本次调用、由 LLM 决策下一步"语义的唯一权威来源;
    streaming watchdog 只保留 model/planner 阶段与死锁兜底。
    """
    idle = getattr(config, "stream_idle_timeout_seconds", 60) if config is not None else 60
    if tool_name == "run_command":
        wait = 0.0
        if isinstance(args, dict):
            try:
                wait = float(args.get("wait_seconds") or 0)
            except (TypeError, ValueError):
                wait = 0.0
        wait = max(0.0, min(wait, float(_RUN_COMMAND_WAIT_LIMIT_SECONDS)))
        return max(90, int(wait) + _RUN_COMMAND_TIMEOUT_MARGIN_SECONDS)
    if tool_name in {"send_command_input", "command_status"}:
        return 60
    if tool_name.startswith("browser_"):
        return 120
    if tool_name in {"web_fetch", "web_search", "discover_web_resources"}:
        return 90
    if tool_name in {"read_file", "list_files", "find_files", "search_text", "git_status", "git_diff"}:
        return 120
    return max(120, int(idle) * 3)


def _format_tool_timeout(tool_name: str, timeout_seconds: int) -> str:
    """工具超时的回灌文案:如实告知 LLM 发生了什么,并给出可操作的下一步选项。

    与 ``_format_tool_error`` 同一语义层级 —— 失败事实交给 LLM,由它决定
    换工具/调小 wait_seconds 改后台轮询/如实告知用户,而不是中断整轮 run。
    """
    lines = [
        f"[工具执行超时] 工具 {tool_name} 执行超过 {timeout_seconds} 秒未完成，本次调用已被中断。",
        "",
        "请根据情况判断下一步：",
        "- 若是长时间运行的命令：调小 wait_seconds 让它立即返回后台句柄，再用 command_status 轮询结果；",
        "- 若是网页/搜索类工具：可能是网络不通或目标无响应，可换用其它工具或数据源；",
        "- 若是浏览器工具：页面可能卡死，可尝试 browser_get_state 确认状态后换标签页重试；",
        "- 若确实无法自行恢复，如实告知用户原因。",
        "不要重复以完全相同的方式重试。",
    ]
    return "\n".join(lines)


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


def _should_reraise_tool_exception(exc: BaseException) -> bool:
    """判断工具异常是否必须原样向上抛,不能被吞成返回值。

    - ``CallDeferred`` / ``ApprovalRequired``:pydantic-ai deferred 控制流
      (``clarify_user`` 依赖它阻断本轮 run)。
    - ``ModelRetry``:工具主动要求 LLM 重试(路径越界、危险命令、文件不存在等),
      交给 pydantic-ai 的 retry 预算机制,不要在这里吞掉。
    - ``asyncio.CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit``:
      不可恢复中断,必须透传。
    其余 ``Exception`` 视为可恢复的运行时错误,回灌给 LLM 由其自行决策。
    """
    from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry

    if isinstance(exc, (CallDeferred, ApprovalRequired, ModelRetry)):
        return True
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return True
    return False


def _format_tool_error(exc: Exception) -> str:
    """把可恢复的工具异常格式化为回灌给 LLM 的错误字符串。

    复用 ``safe_error_detail`` 抹掉 api_key/token/cookie 等敏感信息,避免把
    凭据泄露进对话历史。LLM 拿到这段字符串后可自行判断重试/换方案/告知用户。
    """
    from openhachimi_agent.core.redaction import safe_error_detail

    return (
        f"[工具执行出错] {safe_error_detail(exc)}\n\n"
        "该工具调用未能完成。请根据上述错误信息判断下一步:可修正参数后重试、"
        "换用其它工具,或若无法自行恢复则如实告知用户原因。"
        "不要重复以完全相同的方式重试。"
    )


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


def _tool_timeout_for_call(ctx: object, tool_name: str, bound_args: dict[str, object]) -> int:
    """从 ctx.deps.config 取配置,计算本次工具调用的超时阈值。异常时给保守兜底。"""
    deps = getattr(ctx, "deps", None)
    config = getattr(deps, "config", None)
    try:
        return tool_timeout_seconds(tool_name, bound_args, config)
    except Exception:
        return 120


def with_execution_ledger(func: Callable) -> Callable:
    """Record tool execution in the in-memory ledger + feed verification state.

    ledger 记每个工具的 started/succeeded/failed(供 ``get_replan_signal``/
    ``get_final_verification_signal`` 消费);succeeded 后再调
    ``verification_stop.mark_tool_succeeded`` 按工具语义更新验证状态(编辑类
    置 stale、验证类清 stale),供停止闸门 ``build_verify_on_stop_nudge`` 判定。

    超时语义:async 工具调用被 ``asyncio.wait_for`` 包住,超过
    ``tool_timeout_seconds`` 阈值时中断本次调用并把超时事实回灌给 LLM(由 LLM
    决策下一步),而不是像旧 watchdog 那样 cancel 整个 agent run。外部取消
    (用户中断/整轮终止)产生的 CancelledError 照常透传,不会被吞成超时。
    """

    tool_name = getattr(func, "__name__", "unknown_tool")

    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(ctx, *args, **kwargs):
            session_state = _get_session_state(ctx)
            bound_args = _bind_tool_args(func, ctx, args, kwargs)
            _append_ledger_event(session_state, tool_name=tool_name, status="started", args=bound_args)
            timeout = _tool_timeout_for_call(ctx, tool_name, bound_args)
            try:
                result = await asyncio.wait_for(func(ctx, *args, **kwargs), timeout=timeout)
            except asyncio.TimeoutError:
                _append_ledger_event(
                    session_state,
                    tool_name=tool_name,
                    status="failed",
                    args=bound_args,
                    result=f"timeout after {timeout}s",
                    violation="",
                )
                _logger.warning("tool call timed out tool_name=%s timeout=%ds", tool_name, timeout)
                return _format_tool_timeout(tool_name, timeout)
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
                # 控制流 / 重试信号(CallDeferred/ApprovalRequired/ModelRetry)原样向上抛,
                # 交给 pydantic-ai 的 deferred / retry 预算机制;其余可恢复运行时错误
                # 吞成错误字符串回灌给 LLM,不中断整轮。
                if _should_reraise_tool_exception(exc):
                    raise
                return _format_tool_error(exc)
            _append_ledger_event(session_state, tool_name=tool_name, status="succeeded", args=bound_args, result=result)
            _mark_verify_state(session_state, tool_name, bound_args)
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
            if _should_reraise_tool_exception(exc):
                raise
            return _format_tool_error(exc)
        _append_ledger_event(session_state, tool_name=tool_name, status="succeeded", args=bound_args, result=result)
        _mark_verify_state(session_state, tool_name, bound_args)
        return result
    return sync_wrapper


def _mark_verify_state(session_state: dict[str, Any], tool_name: str, bound_args: dict[str, Any] | None = None) -> None:
    """工具成功后按语义更新验证状态(编辑置 stale / 验证清 stale)。

    延迟 import 防止 ``agent.execution`` ↔ ``agent.verification_stop`` 循环;
    验证模块失败不应阻断工具返回(ledger 已记完,闸门判定可缺位)。

    bound_args 透传给 mark_tool_succeeded,供文件类型过滤(write_file/replace_in_file
    改纯文本时不置 stale)。非文件编辑工具忽略该参数。
    """
    try:
        from openhachimi_agent.agent.verification_stop import mark_tool_succeeded

        mark_tool_succeeded(session_state, tool_name, bound_args)
    except Exception:
        pass
