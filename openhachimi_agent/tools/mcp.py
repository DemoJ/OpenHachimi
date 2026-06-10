"""MCP (Model Context Protocol) 工具集加载器。"""

import logging
from pydantic_ai.mcp import MCPServerStdio, MCPServerStreamableHTTP
from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)


def load_mcp_toolsets(config: AppConfig) -> list:
    """根据应用配置加载 MCP 服务器/工具集。
    
    返回的实例需要在上下文中运行连接才能正常工作，
    即使用 `async with server.run_connection():`。
    """
    servers = []
    
    for name, server_cfg in config.mcp.servers.items():
        try:
            if server_cfg.type == "stdio":
                if not server_cfg.command:
                    logger.warning("MCP server '%s' 配置为 stdio 模式，但未指定 command。", name)
                    continue
                args = server_cfg.args or []
                logger.info("Loading MCP server '%s' (stdio): %s %s", name, server_cfg.command, " ".join(args))
                server = MCPServerStdio(command=server_cfg.command, args=args, env=server_cfg.env)
                servers.append(server)
            elif server_cfg.type == "http":
                if not server_cfg.url:
                    logger.warning("MCP server '%s' 配置为 http/sse 模式，但未指定 url。", name)
                    continue
                logger.info("Loading MCP server '%s' (http): %s", name, server_cfg.url)
                server = MCPServerStreamableHTTP(server_cfg.url)
                servers.append(server)
            else:
                logger.warning("未知的 MCP server '%s' 类型: %s", name, server_cfg.type)
        except Exception as exc:
            logger.error("加载 MCP server '%s' 失败: %s", name, exc)

    return servers
