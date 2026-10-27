"""Shared runtime context for one agent turn."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from pydantic_ai.messages import ModelMessage

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.planning import has_open_tasks
from openhachimi_agent.transport.api_models import AttachmentRef


@dataclass
class OperationState:
    kind: str = "model"
    name: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    last_progress_at: float = field(default_factory=time.perf_counter)
    heartbeat_count: int = 0
    detail: str = ""

    def start(self, kind: str, name: str | None = None, detail: str = "") -> None:
        now = time.perf_counter()
        self.kind = kind
        self.name = name
        self.started_at = now
        self.last_progress_at = now
        self.heartbeat_count = 0
        self.detail = detail

    def progress(self) -> None:
        self.last_progress_at = time.perf_counter()
        self.heartbeat_count = 0

    def describe(self) -> str:
        if self.name:
            return f"{self.kind} {self.name}"
        return self.kind


@dataclass
class AgentRunContext:
    config: AppConfig
    role: str
    session_id: str
    message: str
    attachments: list[AttachmentRef]
    history: list[ModelMessage]
    deps: AgentDeps
    session_state: dict[str, Any]
    stream: bool
    stream_queue: asyncio.Queue[object] | None = None
    stream_event_handler: Callable[[object, object], Awaitable[None]] | None = None
    operation_state: OperationState = field(default_factory=OperationState)
    context_compressor: Any = None
    answer_stream: Any = None


def has_active_todos(session_state: dict[str, Any]) -> bool:
    """计划激活且还有 pending / in-progress 项(done / blocked 都算结束)。"""
    todo_state = session_state.get("todo_state")
    if not getattr(todo_state, "is_active", False):
        return False
    return has_open_tasks(getattr(todo_state, "tasks", None))


def has_restorable_suspended_plan(session_state: dict[str, Any]) -> bool:
    if not session_state.get("suspended_plan"):
        return False
    todo_state = session_state.get("todo_state")
    return has_open_tasks(getattr(todo_state, "tasks", None))


def _current_todo_state(session_state: dict[str, Any]) -> Any:
    return session_state.get("todo_state")


def suspend_current_plan(session_state: dict[str, Any], reason: str, detail: object | None = None, deps: AgentDeps | None = None) -> None:
    todo_state = _current_todo_state(session_state)
    if getattr(todo_state, "is_active", False):
        todo_state.is_active = False
    if todo_state is not None and deps is not None:
        try:
            from openhachimi_agent.tools.planning import _save_state

            _save_state(SimpleNamespace(deps=deps), todo_state)
        except Exception:
            pass
    session_state["suspended_plan"] = {
        "reason": reason,
        "detail": detail,
    }


def restore_suspended_plan(session_state: dict[str, Any], deps: AgentDeps | None = None) -> None:
    todo_state = _current_todo_state(session_state)
    if todo_state is not None and getattr(todo_state, "tasks", None):
        todo_state.is_active = True
        if deps is not None:
            try:
                from openhachimi_agent.tools.planning import _save_state

                _save_state(SimpleNamespace(deps=deps), todo_state)
            except Exception:
                pass
    session_state.pop("suspended_plan", None)
