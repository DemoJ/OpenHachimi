"""工具中间件机制。"""

from typing import Callable, Any
from functools import wraps
import inspect

def apply_middlewares(tools: list[Callable], middlewares: list[Callable[[Callable], Callable]]) -> list[Callable]:
    """将中间件应用到工具列表上。中间件按顺序应用。"""
    wrapped_tools = []
    for tool in tools:
        wrapped_tool = tool
        # 中间件列表反向遍历，使得先传入的中间件在最外层（最先执行）
        for middleware in reversed(middlewares):
            wrapped_tool = middleware(wrapped_tool)
        wrapped_tools.append(wrapped_tool)
    return wrapped_tools

def with_prompt_injection(prompt_name: str) -> Callable[[Callable], Callable]:
    """创建一个中间件，在工具返回结果时按需注入系统提示词。"""
    from openhachimi_agent.tools.utils import inject_prompt_if_unread

    def middleware(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(ctx, *args, **kwargs):
                result = await func(ctx, *args, **kwargs)
                return inject_prompt_if_unread(ctx, prompt_name, result)
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(ctx, *args, **kwargs):
                result = func(ctx, *args, **kwargs)
                return inject_prompt_if_unread(ctx, prompt_name, result)
            return sync_wrapper
    
    return middleware
