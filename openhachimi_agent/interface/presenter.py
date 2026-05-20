"""Presentation helpers for streaming tool progress and answer text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem


PresenterActionType = Literal["tool", "text", "system"]


@dataclass
class PresenterAction:
    type: PresenterActionType
    text: str
    final: bool = False


class ToolProgressPresenter:
    def __init__(self, *, mode: Literal["cli", "conversation"]) -> None:
        self.mode = mode
        self._tool_lines: list[str] = []

    def handle_event(self, event: StreamEventItem) -> list[PresenterAction]:
        if event.type == "tool":
            line = event.text
            if self.mode == "cli":
                return [PresenterAction(type="tool", text=line)]
            self._tool_lines.append(line)
            return [PresenterAction(type="tool", text=self.tool_summary())]

        actions: list[PresenterAction] = []
        if event.type == "text":
            actions.append(PresenterAction(type="text", text=event.text))
        elif event.type == "system":
            actions.append(PresenterAction(type="system", text=event.text))
        return actions

    def finalize(self) -> list[PresenterAction]:
        if self.mode != "conversation" or not self._tool_lines:
            return []
        summary = self.tool_summary()
        self._tool_lines = []
        return [PresenterAction(type="tool", text=summary, final=True)]

    def reset_tools(self) -> None:
        self._tool_lines = []

    def tool_summary(self) -> str:
        lines = self._tool_lines
        return "\n".join(f"• {line}" for line in lines)
