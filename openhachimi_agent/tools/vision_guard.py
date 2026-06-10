"""Guards against re-reading images already handled by auxiliary vision."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.core.deps import AgentDeps


def normalize_vision_guard_path(path: Path) -> str:
    normalized = str(path.resolve())
    if os.name == "nt":
        return normalized.casefold()
    return normalized


def find_processed_vision_attachment(session_state: dict[str, Any], target_path: Path) -> dict[str, Any] | None:
    path_key = normalize_vision_guard_path(target_path)
    path_index = session_state.get("vision_attachment_paths")
    attachments = session_state.get("vision_attachments")
    if not isinstance(path_index, dict) or not isinstance(attachments, dict):
        return None

    attachment_ids = path_index.get(path_key)
    if attachment_ids is None:
        return None
    if isinstance(attachment_ids, str):
        candidate_ids = [attachment_ids]
    elif isinstance(attachment_ids, list):
        candidate_ids = [item for item in attachment_ids if isinstance(item, str)]
    else:
        return None

    for attachment_id in candidate_ids:
        entry = attachments.get(attachment_id)
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "fallback" or entry.get("status") != "succeeded":
            continue
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        size_bytes = entry.get("size_bytes")
        if isinstance(size_bytes, int) and target_path.exists():
            try:
                if target_path.stat().st_size != size_bytes:
                    continue
            except OSError:
                continue
        return entry
    return None


def raise_if_processed_vision_attachment(ctx: RunContext[AgentDeps], target_path: Path, *, tool_name: str) -> None:
    session_state = getattr(ctx.deps, "session_state", {})
    if not isinstance(session_state, dict):
        return

    processed = find_processed_vision_attachment(session_state, target_path)
    if processed is None:
        return

    path_key = normalize_vision_guard_path(target_path)
    session_state.setdefault("vision_tool_blocks", []).append(
        {
            "tool": tool_name,
            "attachment_id": processed.get("attachment_id"),
            "path": path_key,
            "reason": "already_processed_by_auxiliary_vision",
        }
    )
    summary = str(processed.get("summary") or "").strip()
    raise ModelRetry(
        render_system_prompt(
            "vision/processed_attachment_guard",
            {
                "attachment_id": processed.get("attachment_id"),
                "summary": summary,
            },
        )
    )
