"""Answer validation state shared by streaming, persistence and history display."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse


ANSWER_STATUS_KEY = "openhachimi_answer_status"
AnswerStatus = Literal["committed", "rejected", "internal", "interrupted"]


def is_answer_retry(message: ModelMessage) -> bool:
    if not isinstance(message, ModelRequest):
        return False
    return any(
        getattr(part, "part_kind", None) == "retry-feedback"
        or (
            getattr(part, "part_kind", None) == "retry-prompt"
            and getattr(part, "tool_name", None) is None
        )
        for part in message.parts
    )


def answer_status(message: ModelMessage) -> str | None:
    metadata = getattr(message, "metadata", None)
    return metadata.get(ANSWER_STATUS_KEY) if isinstance(metadata, dict) else None


def set_answer_status(message: ModelResponse, status: AnswerStatus) -> None:
    message.metadata = {**(message.metadata or {}), ANSWER_STATUS_KEY: status}


def mark_latest_answer(messages: Sequence[ModelMessage], status: AnswerStatus) -> None:
    if messages and isinstance(messages[-1], ModelResponse):
        set_answer_status(messages[-1], status)


def rejected_answer_indices(messages: Sequence[ModelMessage]) -> set[int]:
    """Infer legacy output retries without treating tool retries as rejected answers."""
    rejected: set[int] = set()
    previous_response: int | None = None
    for index, message in enumerate(messages):
        if isinstance(message, ModelResponse):
            previous_response = index
            if answer_status(message) == "rejected":
                rejected.add(index)
        elif isinstance(message, ModelRequest):
            if is_answer_retry(message) and previous_response is not None:
                rejected.add(previous_response)
            if any(getattr(part, "part_kind", None) == "user-prompt" for part in message.parts):
                previous_response = None
    return rejected


def stamp_answer_attempts(
    messages: Sequence[ModelMessage], *, start_index: int = 0, committed: bool = True
) -> None:
    rejected = rejected_answer_indices(messages)
    responses = [index for index in range(start_index, len(messages)) if isinstance(messages[index], ModelResponse)]
    for index in responses:
        message = messages[index]
        if index in rejected:
            set_answer_status(message, "rejected")  # type: ignore[arg-type]
        elif not committed or index != responses[-1]:
            set_answer_status(message, "internal")  # type: ignore[arg-type]
        else:
            set_answer_status(message, "committed")  # type: ignore[arg-type]
