"""基于真实分词器的 token 计数,用于压缩预检与边界计算。

为什么不用 pydantic-ai 的 ``Model.count_tokens``:
  - pydantic-ai 在 ``Model`` 基类上定义了 ``count_tokens``,但那是**服务端 API
    调用**(让模型服务端帮忙算 token),只有 ``AnthropicModel`` 实现了它(走
    Anthropic ``/messages/count_tokens`` 接口)。本项目用 ``OpenAIChatModel``,
    其 ``count_tokens`` 会直接 ``NotImplementedError``。
  - 即便切到 Anthropic,服务端往返的延迟也无法满足压缩预检的高频同步调用:
    ``should_compress_preflight`` 与 ``_find_tail_cut_by_tokens`` 都在轮内
    同步路径上逐条消息计数,改成网络往返会引入数倍延迟,不可接受。

因此这里用本地分词器 ``tiktoken``(OpenAI 官方)做精确计数:纯本地、微秒级、
无网络依赖,计数结果与 OpenAI 服务端真实用量仅差每条消息固定结构开销
(此处用 ``per_message_overhead`` 近似 ChatML 的 ``<|im_start|>``/``<|im_end|>``)。

Encoding 选择按模型自适应:
  - 通过 ``tiktoken.encoding_for_model(model_name)`` 映射到对应 encoding
    (gpt-4o/4.1/o 系 => ``o200k_base``;gpt-4/3.5 系 => ``cl100k_base``)。
  - 第三方中转模型名不在 tiktoken 内置表里时回退到 ``o200k_base``(gpt-4o 系之后
    的通用默认),再不行用 ``cl100k_base`` 兜底。
  - 上层(如 agent_service 构造压缩引擎时)用 ``set_model_for_token_estimate``
    注入真实 ``model_name``,确保 encoding 与实际模型一致;未注入则用
    ``o200k_base`` 作为合理默认。

仅供预检与边界决策;对外展示与触发判定仍以 ``result.usage.input_tokens``
(provider 真实值)为权威,见 ``ContextCompressor.update_from_response``。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)

# 默认 encoding,当未注入具体 model_name 或 model 名无法识别时使用。
# 选 o200k_base 而非 cl100k_base:gpt-4o 系及之后的模型均采用它,是当前更合理
# 的默认(encoding 选错只会带来低个位数百分比偏差,不影响"是否逾越阈值"的判定)。
_DEFAULT_ENCODING_NAME = "o200k_base"
# 老 gpt-4/3.5 系 encoding,作为最终兜底(o200k_base 也加载失败时用)。
_FALLBACK_ENCODING_NAME = "cl100k_base"

# 当前会话生效的 model 名,由上层注入。默认 None 表示用 ``_DEFAULT_ENCODING_NAME``。
_active_model_name: str | None = None


class _EncodingLoadError(RuntimeError):
    """encoding 无法加载(通常用于显式区分"彻底无可用分词器"的情况)。"""


@lru_cache(maxsize=1)
def _get_encoding(name: str):
    """按 encoding 名加载并缓存 tiktoken Encoding(单例,首次后复用)。"""
    import tiktoken  # 延迟导入,避免给不需要 token 计数的代码路径强加依赖

    try:
        return tiktoken.get_encoding(name)
    except Exception as exc:  # noqa: BLE001  tiktoken 加载/下载失败有多种形态
        raise _EncodingLoadError(f"无法加载 tiktoken encoding {name!r}: {exc}") from exc


def _resolve_model_encoding(model_name: str) -> str:
    """把 model_name 映射到 tiktoken encoding 名;无法识别时回落到默认。"""
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model_name).name
    except KeyError:
        # 未知模型名(常见于第三方中转),用默认 encoding
        logger.debug("model %r 不在 tiktoken 已知表,回退到 %s", model_name, _DEFAULT_ENCODING_NAME)
        return _DEFAULT_ENCODING_NAME
    except Exception:  # noqa: BLE001  其他异常(如 tiktoken 版本差异)也回退
        logger.debug("model %r encoding 解析异常,回退到 %s", model_name, _DEFAULT_ENCODING_NAME, exc_info=True)
        return _DEFAULT_ENCODING_NAME


def _active_encoding_name() -> str:
    """返回当前生效的 encoding 名(依据注入的 model_name,未注入则用默认)。"""
    if _active_model_name:
        return _resolve_model_encoding(_active_model_name)
    return _DEFAULT_ENCODING_NAME


def _active_encoding():
    """返回当前生效的已加载 Encoding 实例;主用失败时回退到 fallback。"""
    primary = _active_encoding_name()
    try:
        return _get_encoding(primary)
    except _EncodingLoadError:
        if primary != _FALLBACK_ENCODING_NAME:
            logger.warning("主 encoding %s 加载失败,回退到 %s", primary, _FALLBACK_ENCODING_NAME)
            return _get_encoding(_FALLBACK_ENCODING_NAME)
        raise


def set_model_for_token_estimate(model_name: str | None) -> None:
    """注入当前会话生效的模型名,用于选择匹配的 tiktoken encoding。

    由上层(如 ``agent_service`` 构造压缩引擎时)调用一次。传 ``None`` 清回默认
    encoding。仅影响后续 ``estimate_*`` 调用,已缓存的 encoding 在变更后会按
    新 encoding 名重新加载。
    """
    global _active_model_name
    if model_name != _active_model_name:
        _active_model_name = model_name
        logger.debug("token 计数 encoding 按 model=%r 切换", model_name)


def estimate_text_tokens(text: str) -> int:
    """用真实分词器计算字符串的 token 数。"""
    if not text:
        return 0
    try:
        enc = _active_encoding()
    except _EncodingLoadError:
        # 极端情况:连 fallback encoding 都加载不了。退回字符级粗估,保证预检不崩。
        logger.error("所有 tiktoken encoding 加载失败,退回字符级粗估")
        return _char_fallback(text)
    return len(enc.encode(text))


# ── 字符级兜底:仅当 tiktoken 完全不可用时启用,保留原启发式 ──
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK 统一表意文字
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x3040, 0x30FF),    # 平假名 + 片假名
    (0xAC00, 0xD7AF),    # 韩文音节
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _char_fallback(text: str) -> int:
    """tiktoken 不可用时的字符级粗估(与改造前行为一致)。"""
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    # CJK ~1 token/字,其余 ~4 字符/token
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
    """估算消息列表的总 token 数(含每条消息的结构开销)。

    ``per_message_overhead`` 近似 ChatML 每条消息的 ``<|im_start|>``/``<|im_end|>``
    包装开销,与 OpenAI 服务端真实用量的偏差主要来自此处与工具调用 schema 注入,
    量级在低个位数百分比以内,足以支撑"是否逾越阈值"的判定而不影响结论。
    """
    total = 0
    for msg in messages:
        total += estimate_text_tokens(_message_text(msg)) + per_message_overhead
    return total