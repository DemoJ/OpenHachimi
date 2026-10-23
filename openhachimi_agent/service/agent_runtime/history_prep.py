"""Agent 运行前对消息历史的预处理。"""

from __future__ import annotations

import dataclasses
from typing import Any

from pydantic_ai.messages import ModelRequest, SystemPromptPart, ToolReturnPart

from openhachimi_agent.tools.planning import TODO_REMINDER


def strip_frozen_system_prompt(history: list[Any]) -> list[Any]:
    """去掉历史第一条请求里残留的旧 system prompt,原地修改并返回同一个列表。

    系统指令改用 pydantic-ai 的 instructions 后每次请求都会按当前状态重新生成。
    改造前保存的会话历史第一条请求里仍带着当时生成的 system prompt(时间、记忆召回、
    TODO 状态都停在那一刻),不去掉的话会和新指令一起发给模型,内容互相矛盾。

    只处理第一条请求:pydantic-ai 只在那里放 system prompt;后面轮次里有意插入的
    系统提示(例如追问超时提醒)保持不动。
    """
    if not history or not isinstance(history[0], ModelRequest):
        return history
    first = history[0]
    kept = [part for part in first.parts if not isinstance(part, SystemPromptPart)]
    if len(kept) != len(first.parts) and kept:
        history[0] = dataclasses.replace(first, parts=kept)
    return history


def strip_stale_todo_reminders(history: list[Any]) -> list[Any]:
    """删掉之前轮次工具结果里追加的 TODO 提醒,原地修改并返回同一个列表。

    提醒只在追加它的那一轮有用;留在历史里会一直占上下文,还会让模型以为现在仍需要更新。
    """
    for index, message in enumerate(history):
        if not isinstance(message, ModelRequest):
            continue
        parts = [_without_reminder(part) for part in message.parts]
        if any(new is not old for new, old in zip(parts, message.parts)):
            history[index] = dataclasses.replace(message, parts=parts)
    return history


def _without_reminder(part: Any) -> Any:
    if not isinstance(part, ToolReturnPart):
        return part
    content = part.content
    suffix = "\n\n" + TODO_REMINDER
    if isinstance(content, str) and content.endswith(suffix):
        return dataclasses.replace(part, content=content[: -len(suffix)])
    if isinstance(content, dict):
        cleaned = _dict_without_reminder(content, suffix)
        if cleaned is not content:
            return dataclasses.replace(part, content=cleaned)
    return part


def _dict_without_reminder(content: dict, suffix: str) -> dict:
    """与 planning._inject_reminder_if_needed 的三种追加方式对应:output / message 字段或 _todo_reminder 键。"""
    if "_todo_reminder" in content:
        return {key: value for key, value in content.items() if key != "_todo_reminder"}
    for key in ("output", "message"):
        value = content.get(key)
        if isinstance(value, str) and value.endswith(suffix):
            return {**content, key: value[: -len(suffix)]}
    return content


def prepare_history_for_run(history: list[Any]) -> list[Any]:
    """主 agent 每次运行前对历史做的全部清理。"""
    strip_frozen_system_prompt(history)
    strip_stale_todo_reminders(history)
    return history


__all__ = ["prepare_history_for_run", "strip_frozen_system_prompt", "strip_stale_todo_reminders"]
