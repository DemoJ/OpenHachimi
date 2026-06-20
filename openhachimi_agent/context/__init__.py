"""上下文管理:对话历史压缩引擎。

借鉴 Hermes Agent 的四阶段压缩算法与 Claude Code 的 focus 引导压缩,
结合 OpenHachimi 已有的强记忆召回系统(召回与 live context 解耦),
实现 provider 无关、多轮编排安全的对话历史压缩。

核心组件:
  - :class:`ContextEngine`: 可插拔引擎抽象基类
  - :class:`ContextCompressor`: 默认四阶段压缩实现
  - :func:`estimate_messages_tokens`: 粗略 token 估计(预检用)
  - :func:`prune_old_tool_results`: 廉价工具结果剪枝(无 LLM)
"""

from __future__ import annotations

from openhachimi_agent.context.engine import ContextEngine
from openhachimi_agent.context.compressor import ContextCompressor
from openhachimi_agent.context.token_estimate import estimate_messages_tokens, estimate_text_tokens
from openhachimi_agent.context.pruning import prune_old_tool_results

__all__ = [
    "ContextEngine",
    "ContextCompressor",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "prune_old_tool_results",
]
