"""可插拔上下文引擎抽象基类(借鉴 Hermes ContextEngine)。

一个上下文引擎控制对话上下文接近模型 token 上限时的管理策略。
默认实现 :class:`~openhachimi_agent.context.compressor.ContextCompressor`
做四阶段有损压缩。未来可插 DAG/LCM 等第三方引擎。

引擎职责:
  - 决定何时压缩(``should_compress`` / ``should_compress_preflight``)
  - 执行压缩(``compress``:摘要/构建 DAG 等)
  - 从 API 响应追踪 token 用量(``update_from_response``)

生命周期:
  1. 实例化并注册
  2. 每轮 LLM 调用后 ``update_from_response(usage)``
  3. 每回合后检查 ``should_compress``;True 则调 ``compress``
  4. 真实会话边界(CLI 退出/``/new``)调 ``on_session_reset``
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic_ai.messages import ModelMessage


class ContextEngine(ABC):
    """所有上下文引擎必须实现的接口。"""

    # ── token 状态(run_agent 读取用于展示/日志)─────────────────────────
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0

    # ── 身份 ────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def name(self) -> str:
        """引擎短标识(如 'compressor')。"""

    # ── 核心接口 ────────────────────────────────────────────────────────
    @abstractmethod
    def update_from_response(self, usage: Any) -> None:
        """从 API 响应的 usage 更新 token 追踪。

        ``usage`` 为 pydantic-ai ``Usage`` 对象,含 ``input_tokens``/
        ``output_tokens``/``cache_read_tokens`` 等。
        """

    @abstractmethod
    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """是否应在本次回合后触发压缩(基于真实用量)。"""

    @abstractmethod
    def compress(
        self,
        messages: list[ModelMessage],
        *,
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
    ) -> list[ModelMessage]:
        """压缩消息列表,返回(可能更短的)新列表。

        Args:
            focus_topic: 可选焦点主题(来自手动 ``/compress <focus>``),
                引擎应优先保留相关信息。
            force: True 时绕过摘要失败冷却(手动重试)。
        """

    # ── 可选:轮内预检 ───────────────────────────────────────────────────
    def should_compress_preflight(self, messages: list[ModelMessage]) -> bool:
        """API 调用前的快速粗略检查(尚无真实 token 计数)。

        默认返回 False(跳过预检)。子类可覆盖以做廉价估计。
        """
        return False

    def has_content_to_compress(self, messages: list[ModelMessage]) -> bool:
        """是否有可压缩内容(手动 /compress 预检用)。默认 True。"""
        return True

    # ── 可选:会话生命周期 ───────────────────────────────────────────────
    def on_session_reset(self) -> None:
        """``/new`` 或 ``/reset`` 时重置每会话状态。"""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    def on_pre_compress(self, messages: list[ModelMessage], window: list[ModelMessage]) -> None:
        """压缩丢弃中间窗口前的钩子(默认空实现)。

        子类可覆盖以把待丢弃的 ``window`` 抢救到记忆库,使其可召回找回。
        """

    # ── 可选:状态展示 ───────────────────────────────────────────────────
    def get_status(self) -> dict[str, Any]:
        return {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, self.last_prompt_tokens / self.context_length * 100)
                if self.context_length
                else 0
            ),
            "compression_count": self.compression_count,
        }

    # ── 可选:模型切换 ───────────────────────────────────────────────────
    def update_model(self, model: str, context_length: int, **kwargs: Any) -> None:
        """模型切换时更新上下文窗口与阈值。默认按 threshold_percent 重算。"""
        self.context_length = context_length


__all__ = ["ContextEngine"]
