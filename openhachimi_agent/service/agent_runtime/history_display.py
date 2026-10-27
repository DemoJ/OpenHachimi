"""WebUI 历史消息展示:把落库的原始消息按「一轮对话」聚合成展示气泡。

一轮 = 一条用户消息 + 模型随后的全部回复(可能是多步工具调用)。流式界面里一轮只有
一个 assistant 气泡,这里同样合并,保证刷新前后看到的内容一致:

- 各步正文按顺序拼接;只调工具、没写正文的步骤不再被丢掉,工具调用汇总进 ``tool_calls``;
- ``clarify_user`` 追问作为正文展示(它是一次工具调用,没有 TextPart);
- 用户对追问的回复以 clarify_user 工具结果落库,这里还原成用户消息;
- token 用量按步累加,反映这一轮的真实消耗。

压缩标记由调用方按「压缩发生在哪条消息之后」传入,插在那一轮结束处,不隐藏、不改动任何消息。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from openhachimi_agent.agent.answer_attempts import answer_status, rejected_answer_indices

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)


logger = logging.getLogger(__name__)

CLARIFY_TOOL_NAME = "clarify_user"
# 追问超时后补写的工具结果以此开头,是给模型看的系统说明,不是用户回复。
_SYSTEM_NUDGE_PREFIX = "[System 提示]"
# 有进行中计划时,用户回复后面会拼上这段给模型的提示(见 main_agent._RESUME_PLAN_HINT)。
_RESUME_HINT_MARKER = "\n\n[系统提示] 以上是用户对你追问的回复"

USER_MESSAGE_METADATA_KEY = "openhachimi_user_message"
ARTIFACTS_METADATA_KEY = "openhachimi_artifacts"

UserRenderer = Callable[[ModelRequest], list[dict]]


def _tool_args(part: ToolCallPart) -> dict:
    args = part.args
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"args": args}
    return args if isinstance(args, dict) else {}


def collect_tool_calls(msg: ModelResponse, exclude: Iterable[str] = ()) -> list[str]:
    """把 ModelResponse 里的 ToolCallPart 渲染成脱敏展示文本(复用流式工具卡片的格式化)。"""
    from openhachimi_agent.service.agent_runtime.streaming import format_tool_call

    skipped = set(exclude)
    lines: list[str] = []
    for part in getattr(msg, "parts", ()):
        if not isinstance(part, ToolCallPart) or part.tool_name in skipped:
            continue
        try:
            lines.append(format_tool_call(part.tool_name, _tool_args(part)))
        except Exception:  # noqa: BLE001 — 展示层兜底,不让格式化失败炸掉历史读取
            logger.debug("tool call formatting failed tool=%s", part.tool_name, exc_info=True)
    return lines


def _clarify_question_text(part: ToolCallPart) -> str:
    """还原 clarify_user 追问在流式界面里的文本:问题 + 编号选项提示。"""
    from openhachimi_agent.tools.clarification import format_choices_hint

    args = _tool_args(part)
    question = str(args.get("question") or "").strip()
    choices = args.get("choices")
    if isinstance(choices, str):
        choices = [choices]
    if isinstance(choices, list) and choices:
        hint = format_choices_hint([str(c) for c in choices])
        return f"{question}\n\n{hint}".strip()
    return question


def _response_tokens(msg: ModelResponse) -> dict[str, int] | None:
    usage = getattr(msg, "usage", None)
    if usage is None:
        return None
    try:
        input_t = int(getattr(usage, "input_tokens", 0) or 0)
        output_t = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read_t = int(getattr(usage, "cache_read_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not (input_t or output_t):
        return None
    return {"input": input_t, "output": output_t, "total": input_t + output_t, "cache_read": cache_read_t}


def _response_artifacts(msg: ModelResponse) -> list[dict]:
    metadata = getattr(msg, "metadata", None)
    raw = metadata.get(ARTIFACTS_METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("artifacts metadata JSON parse failed")
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass
class _AssistantTurn:
    """一轮里模型的全部回复,合并成一个展示气泡。"""

    texts: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    tokens: dict[str, int] | None = None
    timestamp: str | None = None

    def add(self, msg: ModelResponse, *, rejected: bool = False) -> None:
        for part in getattr(msg, "parts", ()):
            if (
                isinstance(part, TextPart)
                and not rejected
                and answer_status(msg) != "internal"
                and str(part.content).strip()
            ):
                self.texts.append(str(part.content).strip())
            elif isinstance(part, ToolCallPart) and part.tool_name == CLARIFY_TOOL_NAME:
                question = _clarify_question_text(part)
                if question:
                    self.texts.append(question)
        self.tool_calls.extend(collect_tool_calls(msg, exclude=(CLARIFY_TOOL_NAME,)))
        self.artifacts.extend(_response_artifacts(msg))
        self._add_tokens(_response_tokens(msg))
        ts = getattr(msg, "timestamp", None)
        if ts is not None:
            self.timestamp = ts.isoformat()

    def _add_tokens(self, tokens: dict[str, int] | None) -> None:
        if tokens is None:
            return
        if self.tokens is None:
            self.tokens = dict(tokens)
            return
        for key, value in tokens.items():
            self.tokens[key] = self.tokens.get(key, 0) + value

    def to_item(self) -> dict | None:
        if not (self.texts or self.tool_calls or self.artifacts):
            return None
        return {
            "role": "assistant",
            "content": "\n\n".join(self.texts),
            "prefix": "",
            "timestamp": self.timestamp,
            "tokens": self.tokens,
            "artifacts": self.artifacts or None,
            "tool_calls": self.tool_calls or None,
        }


def _clarify_reply_item(msg: ModelRequest) -> dict | None:
    """用户对 clarify_user 追问的回复(以工具结果落库)还原成用户消息。"""
    for part in getattr(msg, "parts", ()):
        if not isinstance(part, ToolReturnPart) or part.tool_name != CLARIFY_TOOL_NAME:
            continue
        metadata = getattr(msg, "metadata", None)
        stamped = metadata.get(USER_MESSAGE_METADATA_KEY) if isinstance(metadata, dict) else None
        content = stamped if isinstance(stamped, str) and stamped else part.content
        if not isinstance(content, str) or content.startswith(_SYSTEM_NUDGE_PREFIX):
            return None
        content = content.split(_RESUME_HINT_MARKER, 1)[0].strip()
        if not content:
            return None
        ts = getattr(part, "timestamp", None)
        return {
            "role": "user", "content": content, "prefix": "",
            "timestamp": ts.isoformat() if ts is not None else None,
            "tokens": None, "attachments": None,
        }
    return None


def build_display_messages(
    rows: Iterable[tuple[int, ModelMessage]],
    render_user: UserRenderer,
    markers_after: dict[int, list[dict]] | None = None,
) -> list[dict]:
    """把 ``(turn_index, message)`` 行聚合成展示消息列表。

    ``render_user`` 把一条含用户输入的 ModelRequest 渲染成 user 展示项(含运行时上下文前缀、
    附件等,由 session_history 提供)。``markers_after`` 是 ``{turn_index: [标记项]}``,
    标记插在该行所在的那一轮结束处。
    """
    rows = list(rows)
    rejected = rejected_answer_indices([msg for _, msg in rows])
    markers_after = markers_after or {}
    items: list[dict] = []
    turn = _AssistantTurn()

    def flush() -> None:
        nonlocal turn
        item = turn.to_item()
        if item is not None:
            items.append(item)
        turn = _AssistantTurn()

    for index, (turn_idx, msg) in enumerate(rows):
        if isinstance(msg, ModelResponse):
            turn.add(msg, rejected=index in rejected)
        elif isinstance(msg, ModelRequest):
            user_items = render_user(msg)
            reply = None if user_items else _clarify_reply_item(msg)
            if user_items or reply:
                flush()
                items.extend(user_items or [reply])
        if turn_idx in markers_after:
            flush()
            items.extend(markers_after[turn_idx])
    flush()
    return items
