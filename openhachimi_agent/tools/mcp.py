"""MCP (Model Context Protocol) 工具集加载器。"""

import logging
from typing import Any

from openhachimi_agent.core.config import AppConfig

try:
    from pydantic_ai.mcp import MCPToolset, StdioTransport, StreamableHttpTransport
except ImportError as exc:  # pragma: no cover - 仅在缺依赖时触发
    raise ImportError(
        "请安装 MCP 依赖后再启用 MCP 服务器：pip install \"pydantic-ai-slim[mcp]\""
    ) from exc

logger = logging.getLogger(__name__)


def load_mcp_toolsets(config: AppConfig) -> list[tuple[str, Any]]:
    """根据应用配置加载 MCP 工具集。

    返回 ``[(server_name, toolset), ...]``——带名字映射,让下游
    (factory / role_filters)能按角色绑定配置按 server 名过滤。

    返回的 toolset 需要在上下文中运行连接才能正常工作,
    即使用 `async with toolset:`(由 mcp_manager 经 AsyncExitStack 管理)。
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
                transport = StdioTransport(command=server_cfg.command, args=args, env=server_cfg.env)
                servers.append((name, MCPToolset(client=transport)))
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
                transport = StreamableHttpTransport(server_cfg.url, headers=server_cfg.headers)
                servers.append((name, MCPToolset(client=transport)))
            else:
                logger.warning("未知的 MCP server '%s' 类型: %s", name, server_cfg.type)
        except Exception as exc:
            logger.error("加载 MCP server '%s' 失败: %s", name, exc)

    return servers