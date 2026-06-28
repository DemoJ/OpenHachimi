"""MCP toolset 加载与签名跟踪。

`AgentService` 持有 `_mcp_stack` / `_mcp_toolsets` / `_mcp_config_signature` /
`_mcp_errors` 等字段(测试会直接读取),这里只提供纯函数:计算签名、加载新栈。
合并新结果与关闭旧栈仍在 `AgentService` 内完成,以保留原有日志和锁语义。
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.redaction import redact_exception


logger = logging.getLogger(__name__)


@dataclass
class McpReloadResult:
    """`load_new_mcp_stack` 的返回值,由调用方择机替换 service 字段并关闭旧栈。"""

    new_config: AppConfig
    new_stack: contextlib.AsyncExitStack
    new_toolsets: list
    new_signature: tuple[float, int] | None
    errors: list[str]


def get_mcp_config_signature(
    user_dir: Path,
    fallback: tuple[float, int] | None,
) -> tuple[float, int] | None:
    """读取 mcp-servers.json 的 (mtime, size) 作为缓存键。文件缺失返回 None,
    读取异常返回 fallback(沿用上次值,避免抖动)。
    """
    mcp_file = user_dir / "mcp-servers.json"
    try:
        if not mcp_file.exists() or not mcp_file.is_file():
            return None
        stat = mcp_file.stat()
        return (stat.st_mtime, stat.st_size)
    except Exception as exc:
        logger.debug("Failed to check mcp-servers.json signature: %s", exc)
        return fallback


async def load_new_mcp_stack(
    config: AppConfig,
    signature: tuple[float, int] | None,
) -> McpReloadResult:
    """加载新的 MCP toolset 栈。调用方负责替换 service 字段并关闭旧栈。

    成功的 toolset 进入 new_stack 与 new_toolsets,失败的记入 errors。
    若整体抛出,会先关闭 new_stack 再向上抛。
    """
    from openhachimi_agent.core.config import load_mcp_config
    from openhachimi_agent.tools.mcp import load_mcp_toolsets

    runtime_config = replace(config, mcp=load_mcp_config(config.user_dir))
    new_stack = contextlib.AsyncExitStack()
    connected: list = []
    errors: list[str] = []

    try:
        for name, ts in load_mcp_toolsets(runtime_config):
            try:
                await new_stack.enter_async_context(ts)
            except Exception as exc:
                errors.append(redact_exception(exc))
                logger.exception("Failed to start MCP toolset connection")
            else:
                connected.append((name, ts))
    except Exception:
        await new_stack.aclose()
        raise

    return McpReloadResult(
        new_config=runtime_config,
        new_stack=new_stack,
        new_toolsets=connected,
        new_signature=signature,
        errors=errors,
    )
