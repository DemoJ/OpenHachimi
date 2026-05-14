"""Execution ledger and TaskFrame-aware action checks."""

from __future__ import annotations

import inspect
import json
import time
from functools import wraps
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from pydantic_ai.exceptions import ModelRetry


_NAVIGATION_TOOLS = {"browser_navigate", "browser_new_tab", "web_fetch"}
_LEDGER_LIMIT = 200


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _summarize(value: object, max_chars: int = 800) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _get_session_state(ctx: object) -> dict[str, Any]:
    deps = getattr(ctx, "deps", None)
    state = getattr(deps, "session_state", None)
    if isinstance(state, dict):
        return state
    return {}


def _bind_tool_args(func: Callable, ctx: object, args: tuple, kwargs: dict) -> dict[str, object]:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(ctx, *args, **kwargs)
        return {key: value for key, value in bound.arguments.items() if key != "ctx"}
    except Exception:
        raw_args = list(args)
        if kwargs:
            raw_args.append(kwargs)
        return {"args": raw_args}


def _current_task_id(session_state: dict[str, Any]) -> int | None:
    todo_state = session_state.get("todo_state")
    tasks = getattr(todo_state, "tasks", None)
    if not isinstance(tasks, dict):
        return None
    in_progress = [task for task in tasks.values() if getattr(task, "status", None) == "in-progress"]
    if len(in_progress) == 1:
        return int(getattr(in_progress[0], "id"))
    return None


def _append_ledger_event(
    session_state: dict[str, Any],
    *,
    tool_name: str,
    status: str,
    args: dict[str, object] | None = None,
    result: object = None,
    violation: str = "",
) -> None:
    ledger = session_state.setdefault("execution_ledger", [])
    if not isinstance(ledger, list):
        ledger = []
        session_state["execution_ledger"] = ledger

    event = {
        "seq": len(ledger) + 1,
        "ts": time.time(),
        "tool_name": tool_name,
        "status": status,
        "task_id": _current_task_id(session_state),
        "args": args or {},
        "result_preview": _summarize(result) if result is not None else "",
        "violation": violation,
    }
    ledger.append(event)
    if len(ledger) > _LEDGER_LIMIT:
        del ledger[: len(ledger) - _LEDGER_LIMIT]


def _extract_url_arg(tool_name: str, bound_args: dict[str, object]) -> str | None:
    if tool_name == "browser_new_tab" and bound_args.get("url") is None:
        return None
    value = bound_args.get("url")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _target_urls_from_task_frame(session_state: dict[str, Any]) -> list[str]:
    task_frame = session_state.get("task_frame")
    if not isinstance(task_frame, dict):
        return []
    urls: list[str] = []
    for entity in task_frame.get("target_entities", []):
        if isinstance(entity, dict) and entity.get("type") == "url" and entity.get("value"):
            urls.append(str(entity["value"]))
    return urls


def _action_violation(tool_name: str, bound_args: dict[str, object], session_state: dict[str, Any]) -> str:
    task_frame = session_state.get("task_frame")
    if not isinstance(task_frame, dict):
        return ""

    if task_frame.get("allowed_autonomy") != "narrow" or tool_name not in _NAVIGATION_TOOLS:
        return ""

    target_urls = _target_urls_from_task_frame(session_state)
    if not target_urls:
        return ""

    url = _extract_url_arg(tool_name, bound_args)
    if not url:
        return ""

    observations = session_state.setdefault("task_frame_observations", {})
    observed = set(observations.get("target_urls_observed", [])) if isinstance(observations, dict) else set()
    canonical_targets = {_canonical_url(target_url) for target_url in target_urls}
    canonical_url = _canonical_url(url)

    if canonical_targets.intersection(observed):
        return ""
    if canonical_url in canonical_targets:
        return ""

    return (
        "Action would navigate/fetch a different URL before observing the user-provided primary target. "
        f"Requested target URLs: {', '.join(target_urls)}; attempted URL: {url}. "
        "Recalibrate to the TaskFrame instead of substituting a search result or guessed site."
    )


def _record_success_observation(tool_name: str, bound_args: dict[str, object], session_state: dict[str, Any]) -> None:
    if tool_name not in _NAVIGATION_TOOLS:
        return
    url = _extract_url_arg(tool_name, bound_args)
    if not url:
        return
    canonical_url = _canonical_url(url)
    target_urls = _target_urls_from_task_frame(session_state)
    canonical_targets = {_canonical_url(target_url) for target_url in target_urls}
    if canonical_url not in canonical_targets:
        return
    observations = session_state.setdefault("task_frame_observations", {})
    observed = observations.setdefault("target_urls_observed", [])
    if canonical_url not in observed:
        observed.append(canonical_url)


def get_execution_ledger(ctx: object) -> list[dict[str, Any]]:
    """Return the in-memory ledger for tests, diagnostics and future tools."""

    ledger = _get_session_state(ctx).get("execution_ledger", [])
    return ledger if isinstance(ledger, list) else []


def get_ledger_length(session_state: dict[str, Any]) -> int:
    ledger = session_state.get("execution_ledger", [])
    return len(ledger) if isinstance(ledger, list) else 0


def get_replan_signal(session_state: dict[str, Any], since_seq: int = 0) -> dict[str, object] | None:
    """Return a compact replan signal when the latest new ledger event is blocked/failed."""

    ledger = session_state.get("execution_ledger", [])
    if not isinstance(ledger, list):
        return None
    new_events = [
        event for event in ledger
        if isinstance(event, dict) and int(event.get("seq", 0)) > since_seq
    ]
    if not new_events:
        return None

    latest = new_events[-1]
    if latest.get("status") not in {"blocked", "failed"}:
        return None

    notable_events = [
        event for event in new_events
        if event.get("status") in {"blocked", "failed"}
    ][-5:]
    summary = []
    for event in notable_events:
        detail = event.get("violation") or event.get("result_preview") or ""
        summary.append(
            {
                "seq": event.get("seq"),
                "tool_name": event.get("tool_name"),
                "status": event.get("status"),
                "task_id": event.get("task_id"),
                "args": event.get("args", {}),
                "detail": detail,
            }
        )

    return {
        "reason": "latest execution ledger event requires replan",
        "latest_status": latest.get("status"),
        "events": summary,
    }


def get_final_verification_signal(session_state: dict[str, Any]) -> dict[str, object] | None:
    """Check whether the run has enough evidence to claim completion."""

    issues: list[dict[str, object]] = []

    todo_state = session_state.get("todo_state")
    tasks = getattr(todo_state, "tasks", None)
    if isinstance(tasks, dict) and tasks:
        unfinished = [
            {
                "id": getattr(task, "id", task_id),
                "description": getattr(task, "description", ""),
                "status": getattr(task, "status", ""),
            }
            for task_id, task in tasks.items()
            if getattr(task, "status", None) != "done"
        ]
        if unfinished:
            issues.append({"type": "unfinished_todos", "items": unfinished})

    task_frame = session_state.get("task_frame")
    if isinstance(task_frame, dict):
        target_urls = _target_urls_from_task_frame(session_state)
        if target_urls and task_frame.get("allowed_autonomy") == "narrow":
            observations = session_state.get("task_frame_observations", {})
            observed = set(observations.get("target_urls_observed", [])) if isinstance(observations, dict) else set()
            missing = [
                target_url for target_url in target_urls
                if _canonical_url(target_url) not in observed
            ]
            if missing:
                issues.append({"type": "target_urls_not_observed", "items": missing})

    ledger = session_state.get("execution_ledger", [])
    if isinstance(ledger, list) and ledger:
        latest = ledger[-1]
        if isinstance(latest, dict) and latest.get("status") in {"blocked", "failed"}:
            issues.append(
                {
                    "type": "latest_execution_not_successful",
                    "tool_name": latest.get("tool_name"),
                    "status": latest.get("status"),
                    "detail": latest.get("violation") or latest.get("result_preview") or "",
                }
            )

    if not issues:
        return None
    return {
        "reason": "final verification failed",
        "issues": issues,
    }


def with_execution_ledger(func: Callable) -> Callable:
    """Record tool execution and block actions that contradict the active TaskFrame."""

    tool_name = getattr(func, "__name__", "unknown_tool")

    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(ctx, *args, **kwargs):
            session_state = _get_session_state(ctx)
            bound_args = _bind_tool_args(func, ctx, args, kwargs)
            violation = _action_violation(tool_name, bound_args, session_state)
            if violation:
                _append_ledger_event(session_state, tool_name=tool_name, status="blocked", args=bound_args, violation=violation)
                raise ModelRetry(violation)
            _append_ledger_event(session_state, tool_name=tool_name, status="started", args=bound_args)
            try:
                result = await func(ctx, *args, **kwargs)
            except Exception as exc:
                _append_ledger_event(session_state, tool_name=tool_name, status="failed", args=bound_args, result=exc)
                raise
            _record_success_observation(tool_name, bound_args, session_state)
            _append_ledger_event(session_state, tool_name=tool_name, status="succeeded", args=bound_args, result=result)
            return result
        return async_wrapper

    @wraps(func)
    def sync_wrapper(ctx, *args, **kwargs):
        session_state = _get_session_state(ctx)
        bound_args = _bind_tool_args(func, ctx, args, kwargs)
        violation = _action_violation(tool_name, bound_args, session_state)
        if violation:
            _append_ledger_event(session_state, tool_name=tool_name, status="blocked", args=bound_args, violation=violation)
            raise ModelRetry(violation)
        _append_ledger_event(session_state, tool_name=tool_name, status="started", args=bound_args)
        try:
            result = func(ctx, *args, **kwargs)
        except Exception as exc:
            _append_ledger_event(session_state, tool_name=tool_name, status="failed", args=bound_args, result=exc)
            raise
        _record_success_observation(tool_name, bound_args, session_state)
        _append_ledger_event(session_state, tool_name=tool_name, status="succeeded", args=bound_args, result=result)
        return result
    return sync_wrapper
