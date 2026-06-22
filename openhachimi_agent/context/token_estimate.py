"""粗略 token 估计,用于压缩预检与边界计算。

不走真实分词器(避免依赖与延迟),用混合启发式:
  - CJK 字符按 ~1 token/字 估计(中日韩一个字通常 1-2 token)
  - 其余字符按 ~4 字符/token 估计(英文/代码的常见比例)

仅供预检与边界决策,真实用量以 ``result.usage.input_tokens`` 为准。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

# CJK 统一表意文字范围(常用),覆盖中日韩
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK 统一表意文字
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x3040, 0x30FF),    # 平假名 + 片假名
    (0xAC00, 0xD7AF),    # 韩文音节
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_text_tokens(text: str) -> int:
    """估算字符串的 token 数。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    # CJK ~1 token/字,其余 ~4 字符/token;每条消息额外计入结构开销
    return cjk + (other + 3) // 4


def _part_text(part: Any) -> str:
    """从消息部件中提取可估计 token 的文本。"""
    # UserPromptPart / TextPart / SystemPromptPart
    content = getattr(part, "content", None)
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    # ToolReturnPart.content / ToolCallPart.args 可能是 dict/list/str
    if isinstance(content, (dict, list)):
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def _message_text(msg: ModelMessage) -> str:
    """把单条消息所有部件的文本拼起来用于 token 估计。"""
    parts = getattr(msg, "parts", None) or []
    # ModelResponse 的 ToolCallPart.args 也要计入
    chunks: list[str] = []
    for part in parts:
        text = _part_text(part)
        if text:
            chunks.append(text)
        # ToolCallPart 的 args 字段单独处理(content 在 ToolCallPart 上可能是 args)
        args = getattr(part, "args", None)
        if args is not None and part is not None:
            args_text = args if isinstance(args, str) else (
                json.dumps(args, ensure_ascii=False, default=str) if isinstance(args, (dict, list)) else str(args)
            )
            if args_text and args_text != text:
                chunks.append(args_text)
    return "\n".join(chunks)


def estimate_messages_tokens(messages: list[ModelMessage], *, per_message_overhead: int = 4) -> int:
    """估算消息列表的总 token 数(含每条消息的结构开销)。"""
    total = 0
    for msg in messages:
        total += estimate_text_tokens(_message_text(msg)) + per_message_overhead
    return total
