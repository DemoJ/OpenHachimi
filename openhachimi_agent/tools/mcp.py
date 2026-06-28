"""MCP (Model Context Protocol) 工具集加载器。"""

import logging
from typing import Any

from openhachimi_agent.core.config import AppConfig

MCPServerStdio: Any = None
MCPServerStreamableHTTP: Any = None

logger = logging.getLogger(__name__)


def _load_mcp_stdio_class() -> Any:
    global MCPServerStdio
    if MCPServerStdio is None:
        try:
            from pydantic_ai.mcp import MCPServerStdio as stdio_cls
        except ImportError as exc:
            raise ImportError(
                "请安装 MCP 依赖后再启用 stdio MCP 服务器：pip install \"pydantic-ai-slim[mcp]\""
            ) from exc
        MCPServerStdio = stdio_cls
    return MCPServerStdio


def _load_mcp_http_class() -> Any:
    global MCPServerStreamableHTTP
    if MCPServerStreamableHTTP is None:
        try:
            from pydantic_ai.mcp import MCPServerStreamableHTTP as http_cls
        except ImportError as exc:
            raise ImportError(
                "请安装 MCP 依赖后再启用 HTTP MCP 服务器：pip install \"pydantic-ai-slim[mcp]\""
            ) from exc
        MCPServerStreamableHTTP = http_cls
    return MCPServerStreamableHTTP


def load_mcp_toolsets(config: AppConfig) -> list[tuple[str, Any]]:
    """根据应用配置加载 MCP 服务器/工具集。

    返回 ``[(server_name, server_instance), ...]``——带名字映射,让下游
    (factory / role_filters)能按角色绑定配置按 server 名过滤。

    返回的实例需要在上下文中运行连接才能正常工作，
    即使用 `async with server.run_connection():`。
    """
    servers: list[tuple[str, Any]] = []

    for name, server_cfg in config.mcp.servers.items():
        try:
            if server_cfg.type == "stdio":
                if not server_cfg.command:
                    logger.warning("MCP server '%s' 配置为 stdio 模式，但未指定 command。", name)
                    continue
                args = server_cfg.args or []
                logger.info("Loading MCP server '%s' (stdio): %s %s", name, server_cfg.command, " ".join(args))
                stdio_cls = _load_mcp_stdio_class()
                server = stdio_cls(command=server_cfg.command, args=args, env=server_cfg.env)
                servers.append((name, server))
            elif server_cfg.type == "http":
                if not server_cfg.url:
                    logger.warning("MCP server '%s' 配置为 http/sse 模式，但未指定 url。", name)
                    continue
                logger.info(
                    "Loading MCP server '%s' (http): %s headers_configured=%s",
                    name,
                    server_cfg.url,
                    bool(server_cfg.headers),
                )
                http_cls = _load_mcp_http_class()
                server = http_cls(server_cfg.url, headers=server_cfg.headers)
                servers.append((name, server))
            else:
                logger.warning("未知的 MCP server '%s' 类型: %s", name, server_cfg.type)
        except Exception as exc:
            logger.error("加载 MCP server '%s' 失败: %s", name, exc)

    return servers
