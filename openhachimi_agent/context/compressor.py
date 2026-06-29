"""默认上下文引擎:四阶段有损压缩(借鉴 Hermes,适配 pydantic-ai ModelMessage)。

算法:
  0. 触发判定 + 反抖动(``should_compress``)
  1. 廉价预剪枝:旧工具结果换一行摘要 + 去重(无 LLM)
  2. 头尾边界:保护开头 N 条 + 尾部 token 预算
  3. 结构化摘要:LLM 摘要中间窗口(无摘要器时用确定性兜底)
  4. 组装:头 + 摘要 + 尾;清理孤儿工具配对;剥离历史图片

摘要用普通 ``UserPromptPart`` 作载体(provider 无关),不依赖服务端 compaction。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from openhachimi_agent.context.context_view import assemble_runtime_context
from openhachimi_agent.context.engine import ContextEngine
from openhachimi_agent.context.pruning import prune_old_tool_results
from openhachimi_agent.context.token_estimate import estimate_messages_tokens, estimate_text_tokens

logger = logging.getLogger(__name__)

# 内置默认上下文窗口(单位 token)。当传入 context_length 为 0 时使用。
# 注意:ContextConfig.context_length 以 K 为单位,在 agent_service 边界换算成 token 后传入。
_DEFAULT_CONTEXT_LENGTH = 128_000
# 摘要失败冷却时间(秒),避免连续失败刷屏
_SUMMARY_FAILURE_COOLDOWN_SECONDS = 45.0
# _SUMMARY_END_MARKER / _COMPRESSION_NOTE 已下沉到 context_view.py,供运行时组装复用。


# 摘要器签名:(turns: list[ModelMessage], focus_topic: str | None, previous_summary: str | None) -> str | None
SummaryFn = Callable[[list[ModelMessage], str | None, str | None], str | None]
# 抢救钩子签名:(full_messages: list[ModelMessage], dropped_window: list[ModelMessage]) -> None
PreCompressFn = Callable[[list[ModelMessage], list[ModelMessage]], None]


@dataclass
class CompressionResult:
    """压缩产出:不返回新消息列表,而是返回边界 + 摘要,由调用方落库 + 组装视图。

    边界下标(``head_end_idx``/``tail_start_idx``)对应当前内存列表的下标(从 0 起)。
    落库后与 ``session_messages`` 的全局 ``turn_index`` 一一对应(因压缩发生在本轮新消息
    append-only 落库之后)。``dropped=False`` 表示本次未实际压缩(无中间窗口等),
    调用方据此决定是否记元数据。
    """

    head: list[ModelMessage]
    tail: list[ModelMessage]
    summary: str
    head_end_idx: int
    tail_start_idx: int
    dropped: bool



class ContextCompressor(ContextEngine):
    """默认四阶段压缩引擎。"""

    def __init__(
        self,
        *,
        threshold_percent: float = 0.75,
        hard_ceiling_percent: float = 0.90,
        protect_first_n: int = 3,
        protect_last_n: int = 20,
        tail_token_budget: int = 20000,
        anti_thrash: bool = True,
        min_savings_pct: int = 10,
        context_length: int = 0,
        abort_on_summary_failure: bool = False,
        summarizer: SummaryFn | None = None,
        pre_compress_callback: PreCompressFn | None = None,
    ) -> None:
        self.threshold_percent = threshold_percent
        self.hard_ceiling_percent = hard_ceiling_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.tail_token_budget = tail_token_budget
        self.anti_thrash = anti_thrash
        self.min_savings_pct = min_savings_pct
        self.abort_on_summary_failure = abort_on_summary_failure
        self._summarizer = summarizer
        self._pre_compress_callback = pre_compress_callback

        self.context_length = context_length or _DEFAULT_CONTEXT_LENGTH
        self.threshold_tokens = int(self.context_length * self.threshold_percent)
        self.hard_ceiling_tokens = int(self.context_length * self.hard_ceiling_percent)

        # 每会话状态
        self._previous_summary: str | None = None
        self._last_summary_error: str | None = None
        self._last_compress_aborted: bool = False
        self._last_summary_fallback_used: bool = False
        self._last_compression_savings_pct: float = 100.0
        self._ineffective_compression_count: int = 0
        self._consecutive_ceiling_breaches: int = 0
        self._summary_failure_cooldown_until: float = 0.0
        self.last_compression_rough_tokens: int = 0

    @property
    def name(self) -> str:
        return "compressor"

    # ── 会话生命周期 ────────────────────────────────────────────────────
    def on_session_reset(self) -> None:
        super().on_session_reset()
        self._previous_summary = None
        self._last_summary_error = None
        self._last_compress_aborted = False
        self._last_summary_fallback_used = False
        self._ineffective_compression_count = 0
        self._consecutive_ceiling_breaches = 0
        self._summary_failure_cooldown_until = 0.0
        self._last_compression_savings_pct = 100.0
        self.last_compression_rough_tokens = 0

    def update_model(self, model: str, context_length: int, **kwargs: Any) -> None:
        self.context_length = context_length or self.context_length
        self.threshold_tokens = int(self.context_length * self.threshold_percent)
        self.hard_ceiling_tokens = int(self.context_length * self.hard_ceiling_percent)

    # ── token 追踪 ──────────────────────────────────────────────────────
    def update_from_response(self, usage: Any) -> None:
        """从 pydantic-ai Usage 更新 token 状态。

        ``usage`` 应为 ``RunUsage`` 对象。但 ``AgentRunResult.usage`` /
        ``StreamedRunResult.usage`` 在 pydantic-ai 当前版本是**方法**而非属性
        (官方 ``TODO (v2): Make this a property``),调用方若误传 ``result.usage``
        (漏括号)会拿到 bound method,所有 token 字段将为 ``None``。此处对 callable
        做一次解包防御,避免这种误用静默地把 token 统计全部清零。
        """
        if callable(usage) and not hasattr(usage, "input_tokens"):
            usage = usage()
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        # input_tokens 在某些 provider 上含缓存读;优先用 details 里的非缓存输入
        details = getattr(usage, "details", None) or {}
        non_cached = 0
        if isinstance(details, dict):
            non_cached = int(details.get("input_tokens", 0) or 0)
        self.last_prompt_tokens = non_cached or input_tokens
        self.last_completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.last_total_tokens = self.last_prompt_tokens + self.last_completion_tokens

    # ── 触发判定 ────────────────────────────────────────────────────────
    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """轮后主压缩:真实用量达阈值且未触发反抖动。"""
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        if tokens < self.threshold_tokens:
            # 真实用量回落到阈值下,顺便清空 ceiling 连击计数(不再处于紧急区)
            self._consecutive_ceiling_breaches = 0
            return False

        # P2 自愈:真实用量已突破 hard_ceiling 即视为紧急。anti-thrash 在紧急区累积一次后
        # 强制解锁——说明此前判定的"低效压缩"其实没把局面救下来,情况已恶化到必须再试,
        # 此时继续拒绝压缩只会被 preflight 在轮内打断,体验更差。
        in_ceiling_breach = tokens >= self.hard_ceiling_tokens
        if in_ceiling_breach:
            self._consecutive_ceiling_breaches += 1
        else:
            self._consecutive_ceiling_breaches = 0

        if self.anti_thrash and self._ineffective_compression_count >= 2:
            if self._consecutive_ceiling_breaches >= 1:
                logger.warning(
                    "anti-thrash 自愈:真实用量 %d 已突破 hard_ceiling=%d,强制重置 ineffective 计数(原值 %d)并重试压缩",
                    tokens,
                    self.hard_ceiling_tokens,
                    self._ineffective_compression_count,
                )
                self._ineffective_compression_count = 0
                return True
            logger.warning(
                "跳过压缩:最近 %d 次压缩各节省不足 %d%%。建议 /new 开新会话或 /compress <topic> 聚焦压缩。",
                self._ineffective_compression_count,
                self.min_savings_pct,
            )
            return False
        return True

    def should_compress_preflight(self, messages: list[ModelMessage]) -> bool:
        """轮内预检:粗略估计达硬上限。优先信任最近真实用量。"""
        if self.last_prompt_tokens and self.last_prompt_tokens >= self.hard_ceiling_tokens:
            return True
        rough = estimate_messages_tokens(messages)
        return rough >= self.hard_ceiling_tokens

    def has_content_to_compress(self, messages: list[ModelMessage]) -> bool:
        min_needed = self.protect_first_n + 3 + 1
        return len(messages) > min_needed

    # ── 主压缩入口 ──────────────────────────────────────────────────────
    def compress(
        self,
        messages: list[ModelMessage],
        *,
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        allow_llm_summary: bool = True,
    ) -> CompressionResult:
        """执行四阶段压缩,返回结构化边界 + 摘要(不再返回新消息列表)。

        调用方据 ``result.dropped`` 决定是否记元数据:dropped=True 时用
        ``head_end_idx``/``tail_start_idx`` 边界 + ``summary`` 写入
        ``session_compressions``,运行时上下文由
        ``SessionStore.load_context`` → ``context_view.assemble_runtime_context`` 重建。

        Args:
            allow_llm_summary: False 时跳过 LLM 摘要器,用确定性兜底(轮内预检用,
                避免中途中断去调 LLM)。True 时(轮后主压缩)用 LLM 生成结构化摘要。
        """
        self._last_summary_error = None
        self._last_compress_aborted = False
        self._last_summary_fallback_used = False

        if force and self._summary_failure_cooldown_until > 0.0:
            self._summary_failure_cooldown_until = 0.0

        _no_drop = CompressionResult(
            head=messages, tail=[], summary="", head_end_idx=0,
            tail_start_idx=len(messages), dropped=False,
        )
        if not self.has_content_to_compress(messages):
            logger.warning("无法压缩:消息数 %d 不足(需 > %d)", len(messages), self.protect_first_n + 4)
            return _no_drop

        original_messages = messages
        display_tokens = current_tokens or self.last_prompt_tokens or estimate_messages_tokens(messages)

        # 阶段 1:廉价预剪枝
        messages, pruned_count = prune_old_tool_results(
            messages,
            protect_tail_count=self.protect_last_n,
            protect_tail_tokens=self.tail_token_budget,
        )
        if pruned_count:
            logger.info("压缩阶段1:剪枝 %d 个旧工具结果", pruned_count)

        # 阶段 2:头尾边界
        head_end = self._protect_head_size(messages)
        tail_start = self._find_tail_cut_by_tokens(messages, head_end)
        if head_end >= tail_start:
            # 整个记录都在尾部预算内,无可压缩窗口
            self._ineffective_compression_count += 1
            self._last_compression_savings_pct = 0.0
            logger.warning("压缩跳过:无中间窗口可压缩(head=%d tail=%d)", head_end, tail_start)
            return _no_drop

        window = messages[head_end:tail_start]
        head = messages[:head_end]
        tail = messages[tail_start:]

        # 记忆抢救钩子:丢弃窗口前抢救到记忆库
        self.on_pre_compress(messages, window)

        # 阶段 3:结构化摘要
        summary = self._generate_summary(window, focus_topic=focus_topic, allow_llm=allow_llm_summary)

        if not summary and self.abort_on_summary_failure:
            self._last_compress_aborted = True
            logger.warning("摘要失败,中止压缩(abort_on_summary_failure=true),%d 条消息保留不变", len(window))
            return CompressionResult(
                head=original_messages, tail=[], summary="",
                head_end_idx=0, tail_start_idx=len(original_messages), dropped=False,
            )

        if not summary:
            # preflight 路径(allow_llm_summary=False)主动跳过 LLM,是设计行为而非异常;
            # 真正的 LLM 摘要失败已在 _generate_summary 里以 WARNING 记录过了。
            if not allow_llm_summary:
                logger.info("preflight 跳过 LLM 摘要,使用确定性兜底(窗口 %d 条)", len(window))
            else:
                logger.warning("摘要失败,插入确定性兜底摘要")
            self._last_summary_fallback_used = True
            summary = self._build_static_fallback_summary(window)

        self._previous_summary = summary

        # 阶段 4:组装(供 saved/ineffective 统计 + preflight 就地写回用,不落库)
        compressed = assemble_runtime_context(head, tail, summary)
        compressed = self._sanitize_tool_pairs(compressed)
        compressed = self._strip_historical_media(compressed)
        # preflight 路径(executor.py)需要就地拿到组装好的列表写回 ctx.history
        # —— 把它挂在 result 上,但 dropped/边界仍是压缩前内存列表语义。
        result = CompressionResult(
            head=head, tail=tail, summary=summary,
            head_end_idx=head_end, tail_start_idx=tail_start, dropped=True,
        )
        result.runtime_view = compressed  # type: ignore[attr-defined]

        self.compression_count += 1
        new_estimate = estimate_messages_tokens(compressed)
        saved = display_tokens - new_estimate
        savings_pct = (saved / display_tokens * 100) if display_tokens > 0 else 0
        self._last_compression_savings_pct = savings_pct

        # ineffective 判定单独用 estimator-vs-estimator,避免 display_tokens(可能来自
        # provider 真实值或 current_tokens)与 new_estimate(始终走 estimator)的尺度不一致,
        # 否则 estimator 系统性偏差会把 _ineffective_compression_count 推到 anti-thrash
        # 锁死阈值,导致后续压缩被永久拒绝直至会话重置。对外 log 与 _last_compression_savings_pct
        # 仍沿用真实口径,保持展示一致。
        estimator_before = estimate_messages_tokens(original_messages)
        estimator_saved = estimator_before - new_estimate
        estimator_savings_pct = (estimator_saved / estimator_before * 100) if estimator_before > 0 else 0.0

        # P1 兜底:消息数明显减少(≥20%)说明压缩实质生效,即使 estimator 算出 savings 偏低
        # 也不累积 ineffective。覆盖 estimator 对剪枝后短工具结果摘要、图片占位等场景估算不准
        # 但实际节省可观的情况,防止误锁。
        original_count = len(original_messages)
        msg_reduction_pct = (1 - len(compressed) / original_count) * 100 if original_count > 0 else 0.0
        effective_by_msg_count = msg_reduction_pct >= 20.0

        if estimator_savings_pct < self.min_savings_pct and not effective_by_msg_count:
            self._ineffective_compression_count += 1
        else:
            self._ineffective_compression_count = 0

        logger.info(
            "压缩完成 #%d:%d→%d 条消息(约省 %d token,%.0f%%;estimator 视角 %.0f%%,消息数 -%.0f%%)",
            self.compression_count,
            len(messages),
            len(compressed),
            saved,
            savings_pct,
            estimator_savings_pct,
            msg_reduction_pct,
        )
        return result

    # ── 阶段 2:边界 ────────────────────────────────────────────────────
    def _protect_head_size(self, messages: list[ModelMessage]) -> int:
        """保护开头 protect_first_n 条消息。"""
        return min(self.protect_first_n, len(messages))

    def _find_tail_cut_by_tokens(self, messages: list[ModelMessage], head_end: int) -> int:
        """从末尾向前累计 token 到 tail_token_budget,返回尾部起始索引。

        不会越过 head_end;至少保护 protect_last_n 条。
        """
        n = len(messages)
        if n == 0:
            return 0
        min_protect = min(self.protect_last_n, max(0, n - head_end))
        accumulated = 0
        boundary = n
        for i in range(n - 1, head_end - 1, -1):
            msg_tokens = estimate_text_tokens(_message_text(messages[i])) + 4
            protected_so_far = n - i
            if accumulated + msg_tokens > self.tail_token_budget and protected_so_far >= min_protect:
                boundary = i
                break
            accumulated += msg_tokens
            boundary = i
        protected_count = n - boundary
        if protected_count < min_protect:
            protected_count = min_protect
            boundary = max(head_end, n - protected_count)
        return boundary

    # ── 阶段 3:摘要 ────────────────────────────────────────────────────
    def _generate_summary(self, window: list[ModelMessage], *, focus_topic: str | None, allow_llm: bool = True) -> str | None:
        """生成结构化摘要。有摘要器且允许 LLM 时用 LLM,否则返回 None(由调用方走兜底)。"""
        now = time.monotonic()
        if now < self._summary_failure_cooldown_until:
            logger.debug("摘要冷却中(剩余 %.0fs)", self._summary_failure_cooldown_until - now)
            return None
        if not allow_llm or self._summarizer is None:
            # 无 LLM 摘要器或预检模式:返回 None 触发确定性兜底
            return None
        try:
            summary = self._summarizer(window, focus_topic, self._previous_summary)
            if not summary:
                self._last_summary_error = "summarizer returned empty"
                return None
            return summary
        except Exception as exc:  # noqa: BLE001
            self._last_summary_error = f"{exc.__class__.__name__}: {exc}"
            self._summary_failure_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS
            logger.warning("摘要生成失败:%s(冷却 %.0fs)", self._last_summary_error, _SUMMARY_FAILURE_COOLDOWN_SECONDS)
            return None

    def _build_static_fallback_summary(self, window: list[ModelMessage]) -> str:
        """无 LLM 时的确定性兜底摘要:抽取工具调用与文本要点。"""
        lines = ["## 历史任务快照(自动压缩兜底)"]
        tool_calls: list[str] = []
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        for msg in window:
            if isinstance(msg, ModelRequest):
                for part in getattr(msg, "parts", None) or []:
                    if isinstance(part, UserPromptPart):
                        text = str(part.content).strip()
                        if text:
                            user_msgs.append(text[:200])
            elif isinstance(msg, ModelResponse):
                for part in getattr(msg, "parts", None) or []:
                    if isinstance(part, ToolCallPart):
                        args = part.args
                        args_text = args if isinstance(args, str) else _safe_str(args)
                        tool_calls.append(f"- [{part.tool_name}] {args_text[:120]}")
                    else:
                        text = getattr(part, "content", "")
                        if isinstance(text, str) and text.strip():
                            assistant_msgs.append(text.strip()[:200])

        if user_msgs:
            lines.append("### 用户请求")
            lines.extend(f"- {m}" for m in user_msgs[-5:])
        if tool_calls:
            lines.append("### 执行的工具调用(近 20 项)")
            lines.extend(tool_calls[-20:])
        if assistant_msgs:
            lines.append("### 助手回复要点")
            lines.extend(f"- {m}" for m in assistant_msgs[-5:])
        lines.append("### 当前状态")
        lines.append("以上为压缩前的执行记录摘要,详细内容已折叠。如需具体信息请重新调用工具获取。")
        return "\n".join(lines)

    # ── 阶段 4:组装 ────────────────────────────────────────────────────
    def _assemble(
        self,
        head: list[ModelMessage],
        summary: str,
        tail: list[ModelMessage],
    ) -> list[ModelMessage]:
        """组装:头部(首条加压缩说明)+ 摘要 + 尾部。

        逻辑下沉到 ``context_view.assemble_runtime_context``,本方法仅作向后兼容
        的薄委托 —— 旧调用方 / 测试可能仍按 ``_assemble(head, summary, tail)`` 形式引用。
        """
        return assemble_runtime_context(head, tail, summary)

    def _sanitize_tool_pairs(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """清理孤儿 ToolCallPart(无对应 return)与孤儿 ToolReturnPart(无对应 call)。

        防止 API 因 tool_call_id 不匹配报错。
        """
        # 收集所有 tool_call_id
        call_ids: set[str] = set()
        return_ids: set[str] = set()
        for msg in messages:
            for part in getattr(msg, "parts", None) or []:
                if isinstance(part, ToolCallPart) and part.tool_call_id:
                    call_ids.add(part.tool_call_id)
                elif isinstance(part, ToolReturnPart) and part.tool_call_id:
                    return_ids.add(part.tool_call_id)

        result: list[ModelMessage] = []
        for msg in messages:
            parts = getattr(msg, "parts", None)
            if parts is None:
                result.append(msg)
                continue
            new_parts: list[Any] = []
            changed = False
            for part in parts:
                if isinstance(part, ToolCallPart) and part.tool_call_id and part.tool_call_id not in return_ids:
                    # 孤儿 call:替换为文本说明而非删除(保留调用意图)
                    new_parts.append(_orphan_call_note(part))
                    changed = True
                    continue
                if isinstance(part, ToolReturnPart) and part.tool_call_id and part.tool_call_id not in call_ids:
                    # 孤儿 return:跳过
                    changed = True
                    continue
                new_parts.append(part)
            result.append(replace(msg, parts=new_parts) if changed else msg)
        return result

    def _strip_historical_media(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """把非最新图片轮次的图片 part 替换为文本占位,避免多 MB base64 永久占位。

        保留最后一条含图片的请求原样;更早的图片换占位。
        """
        last_image_idx = -1
        for i, msg in enumerate(messages):
            if not isinstance(msg, ModelRequest):
                continue
            if any(_is_image_part(p) for p in getattr(msg, "parts", None) or []):
                last_image_idx = i
        if last_image_idx <= 0:
            return messages

        result: list[ModelMessage] = []
        for i, msg in enumerate(messages):
            if i >= last_image_idx or not isinstance(msg, ModelRequest):
                result.append(msg)
                continue
            parts = getattr(msg, "parts", None)
            if parts is None or not any(_is_image_part(p) for p in parts):
                result.append(msg)
                continue
            new_parts: list[Any] = []
            changed = False
            for part in parts:
                if _is_image_part(part):
                    new_parts.append(UserPromptPart(content="[历史图片附件已折叠以节省上下文]"))
                    changed = True
                else:
                    new_parts.append(part)
            result.append(replace(msg, parts=new_parts) if changed else msg)
        return result

    # ── 抢救钩子 ────────────────────────────────────────────────────────
    def on_pre_compress(self, messages: list[ModelMessage], window: list[ModelMessage]) -> None:
        if self._pre_compress_callback is not None:
            try:
                self._pre_compress_callback(messages, window)
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_pre_compress 抢救钩子失败:%s", exc)


# ── 模块级辅助 ──────────────────────────────────────────────────────────
def _message_text(msg: ModelMessage) -> str:
    chunks: list[str] = []
    for part in getattr(msg, "parts", None) or []:
        content = getattr(part, "content", None)
        if isinstance(content, str):
            chunks.append(content)
        elif content is not None:
            chunks.append(_safe_str(content))
        args = getattr(part, "args", None)
        if args is not None:
            chunks.append(args if isinstance(args, str) else _safe_str(args))
    return "\n".join(chunks)


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        import json

        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _is_image_part(part: Any) -> bool:
    """判断 part 是否为图片部件。"""
    part_kind = getattr(part, "part_kind", "")
    if part_kind in {"image", "binary"}:
        return True
    # pydantic-ai 的 BinaryContent 等可能以其他形式出现
    content = getattr(part, "content", None)
    if hasattr(part, "media_type") and isinstance(content, (bytes, bytearray)):
        return True
    return False


def _orphan_call_note(part: ToolCallPart) -> Any:
    """把孤儿 ToolCallPart 换成文本说明(保留调用意图)。

    孤儿 call 只出现在 ModelResponse 里(assistant 消息),所以必须替换为同样合法的
    TextPart——若改成 UserPromptPart,会导致 pydantic-ai 在把消息映射给 OpenAI
    时(_map_model_response → assert_never)炸成 AssertionError。
    """
    args = part.args
    args_text = args if isinstance(args, str) else _safe_str(args)
    return TextPart(content=f"[已折叠的工具调用:{part.tool_name} {args_text[:120]}]")


__all__ = ["ContextCompressor", "SummaryFn", "PreCompressFn"]
