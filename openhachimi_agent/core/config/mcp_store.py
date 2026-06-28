"""MCP 服务器清单的持久化读写(user/mcp-servers.json)。

读复用 loading.load_mcp_config;写采用整体覆盖(json 无注释,无信息损失),
原子写临时文件 + os.replace,避免中途损坏。
"""

import json
import logging
import os
from pathlib import Path

from openhachimi_agent.core.config.loading import load_mcp_config
from openhachimi_agent.core.config.models import MCPConfig, MCPServerConfig

logger = logging.getLogger(__name__)


def get_mcp_config(user_dir: Path) -> MCPConfig:
    """读取当前 MCP 配置(文件不存在则返回空清单)。"""
    return load_mcp_config(user_dir)


def _server_to_dict(srv: MCPServerConfig) -> dict:
    """MCPServerConfig → mcp-servers.json 的 server 对象。

    type 字段不写回——加载时由 command/url presence 派生,与 example 一致。
    仅在 env/headers 非空时才写入对应键,保持文件简洁。
    """
    if srv.type == "stdio":
        d: dict = {"command": srv.command or "", "args": list(srv.args)}
        if srv.env:
            d["env"] = dict(srv.env)
        return d
    d = {"url": srv.url or ""}
    if srv.headers:
        d["headers"] = dict(srv.headers)
    return d


def write_mcp_config(user_dir: Path, servers: dict[str, MCPServerConfig]) -> None:
    """整体覆盖写 user/mcp-servers.json,原子替换。

    servers 保留插入顺序(dict 自 Python 3.7 起有序),前端提交顺序即文件顺序。
    """
    out = {"mcpServers": {name: _server_to_dict(srv) for name, srv in servers.items()}}
    target = user_dir / "mcp-servers.json"
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, target)
    logger.info("mcp-servers.json rewritten, servers=%s", list(servers))