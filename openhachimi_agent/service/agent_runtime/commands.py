"""命令识别的兼容入口。

历史上本文件包含 `/stop` `/new` `/compress` 的字符串识别函数与
`SIGNAL_LABELS` 常量;现在所有命令统一在 `command_registry.py` 注册,
本文件仅保留必要的薄包装供旧 import 路径过渡。
"""

from __future__ import annotations

from openhachimi_agent.core.identifiers import validate_latest_scope
from openhachimi_agent.service.agent_runtime.command_registry import (
    CommandOutcome,
    CommandSpec,
    parse_command,
)
from openhachimi_agent.service.agent_runtime.streaming import SIGNAL_LABELS


__all__ = [
    "CommandOutcome",
    "CommandSpec",
    "SIGNAL_LABELS",
    "parse_command",
    "latest_scope_from_context",
]


def latest_scope_from_context(channel_context: dict[str, object] | None) -> str | None:
    """从 channel_context 中提取 session_scope_key 并做校验。"""
    if not channel_context:
        return None
    scope = channel_context.get("session_scope_key")
    if not scope:
        return None
    return validate_latest_scope(str(scope))
