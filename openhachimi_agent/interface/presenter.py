"""Presentation helpers for streaming tool progress and answer text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.transport.api_models import ArtifactRef


PresenterActionType = Literal["tool", "text", "system", "artifact"]


@dataclass
class PresenterAction:
    type: PresenterActionType
    text: str
    final: bool = False
    artifact: ArtifactRef | None = None


class ToolProgressPresenter:
    def __init__(self, *, mode: Literal["cli", "conversation"]) -> None:
        self.mode = mode
        self._tool_lines: list[str] = []
        # 已展示过的工具行集合,用于在同一 segment 内对完全相同的工具调用做去重。
        # 同一个 tool 事件被 pydantic-ai 因 retry/replan 重发,或模型一轮里输出多次
        # 等价 tool call(常见于 schema 不严格的开源模型),都会重复 yield 一份带相同
        # text 的 StreamEventItem,累积渲染就会让用户看到「• ✅ 创建计划...」× N。
        self._seen_lines: set[str] = set()

    def handle_event(self, event: StreamEventItem) -> list[PresenterAction]:
        if event.type == "tool":
            line = event.text
            if self.mode == "cli":
                # CLI 流式逐行打印,重复行直接吞掉避免刷屏。
                if line in self._seen_lines:
                    return []
                self._seen_lines.add(line)
                return [PresenterAction(type="tool", text=line)]
            # conversation 模式:同一行不再 append 到累积列表,但仍触发一次刷新
            # 以便上层及时把"工具进行中"的状态同步到客户端(例如 placeholder)。
            if line not in self._seen_lines:
                self._seen_lines.add(line)
                self._tool_lines.append(line)
            return [PresenterAction(type="tool", text=self.tool_summary())]

        actions: list[PresenterAction] = []
        if event.type == "text":
            actions.append(PresenterAction(type="text", text=event.text))
        elif event.type == "system":
            actions.append(PresenterAction(type="system", text=event.text))
        elif event.type == "artifact":
            actions.append(PresenterAction(type="artifact", text=event.text, artifact=event.artifact))
        return actions

    def finalize(self) -> list[PresenterAction]:
        if self.mode != "conversation" or not self._tool_lines:
            return []
        summary = self.tool_summary()
        self._tool_lines = []
        self._seen_lines.clear()
        return [PresenterAction(type="tool", text=summary, final=True)]

    def reset_tools(self) -> None:
        self._tool_lines = []
        self._seen_lines.clear()

    def tool_summary(self) -> str:
        lines = self._tool_lines
        return "\n".join(f"• {line}" for line in lines)
