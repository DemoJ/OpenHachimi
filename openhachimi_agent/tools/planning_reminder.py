"""工具调用后的 TODO 更新提醒。

从 ``planning.py`` 原样拆出,行为不变。``planning.py`` 仍然重新导出这里的名字。
"""

import inspect
from functools import wraps

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.planning_state import _get_state, _save_state, has_open_tasks


# 追加在工具结果后的提醒。只对当前轮有用,下一轮运行前由 history_prep 从历史里删掉。
TODO_REMINDER = "[System Reminder: 你有正在进行中的 TODO 任务，如果已完成某些步骤，请务必使用 update_todo 更新状态！不要忘记整体的规划进度！]"


def with_todo_reminder(func):
    """
    工具装饰器：如果开启了 TODO 规划，且超过 3 次调用未更新 TODO，
    则在工具返回结果中注入提醒。
    """
    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(ctx, *args, **kwargs):
            result = await func(ctx, *args, **kwargs)
            return _inject_reminder_if_needed(ctx, result)
        return async_wrapper
    else:
        @wraps(func)
        def sync_wrapper(ctx, *args, **kwargs):
            result = func(ctx, *args, **kwargs)
            return _inject_reminder_if_needed(ctx, result)
        return sync_wrapper

def _inject_reminder_if_needed(ctx: RunContext[AgentDeps], result: str | dict | list | None) -> str | dict | list | None:
    state = _get_state(ctx)
    if not state.is_active or not state.tasks:
        return result

    if not has_open_tasks(state.tasks):
        return result

    state.tool_calls_since_update += 1
    if state.tool_calls_since_update >= 3:
        reminder = "\n\n" + TODO_REMINDER
        state.tool_calls_since_update = 0
        _save_state(ctx, state)
        if isinstance(result, str):
            return result + reminder
        elif isinstance(result, dict):
            new_result = dict(result)
            if "output" in new_result and isinstance(new_result["output"], str):
                new_result["output"] += reminder
            elif "message" in new_result and isinstance(new_result["message"], str):
                new_result["message"] += reminder
            else:
                new_result["_todo_reminder"] = reminder.strip()
            return new_result

    return result
