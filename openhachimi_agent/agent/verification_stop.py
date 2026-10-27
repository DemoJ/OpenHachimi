"""Bounded turn-end verification gate with independent workspace/browser gaps.

Only actual successful effects (or explicitly reported partial effects on failure)
create a gap. Evidence clears its own domain only. The existing two-nudge cap and
non-code file exemption remain; this gate never performs checks itself.
"""

from __future__ import annotations

from typing import Any

from openhachimi_agent.agent.execution import ToolOutcome, get_tool_outcome
from openhachimi_agent.agent.verification_evidence import _PATH_ARG_NAMES, is_non_code_path


# 闸门在单 turn 内最多 nudge 的次数。超过即放行 —— 避免模型反复"编辑→被拦→说完成"
# 死循环把整轮报废。与 Hermes 的 max_attempts=2 对齐。
_MAX_NUDGE_ATTEMPTS = 2

# session_state 下的验证状态键。
_VERIFY_STATE_KEY = "_verification_state"

# 编辑类工具里,改的是文件路径的工具——对这些工具按路径后缀判断是否纯文本,
# 纯文本编辑不置 stale(照搬 Hermes 的文件类型过滤,避免文档改动误触发验证 nudge)。
_FILE_EDIT_TOOLS: frozenset[str] = frozenset({"write_file", "replace_in_file"})


def _state(session_state: dict[str, Any]) -> dict[str, Any]:
    """取(或初始化)本轮验证状态子字典。"""
    state = session_state.get(_VERIFY_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        session_state[_VERIFY_STATE_KEY] = state
    return state


def _extract_path_from_args(bound_args: dict[str, Any] | None) -> str | None:
    """从工具的 bound_args 里抽出文件路径参数值(若有)。

    不同写入工具参数名不同(write_file 用 file_path,有的用 path/target)。遍历已知
    路径参数名取第一个非空值。读不到返回 None——调用方据此保守置 stale。
    """
    if not isinstance(bound_args, dict):
        return None
    for name in _PATH_ARG_NAMES:
        val = bound_args.get(name)
        if isinstance(val, str) and val.strip():
            return val
    return None


def mark_tool_succeeded(
    session_state: dict[str, Any],
    tool_name: str | None,
    bound_args: dict[str, Any] | None = None,
) -> None:
    """Compatibility entry point for successful tools without explicit metadata."""
    mark_tool_outcome(session_state, tool_name, get_tool_outcome(None, tool_name), bound_args)


def mark_tool_outcome(
    session_state: dict[str, Any],
    tool_name: str | None,
    outcome: ToolOutcome | None,
    bound_args: dict[str, Any] | None = None,
) -> None:
    """Apply effects even on partial failure, but accept evidence only on success."""
    if not tool_name or outcome is None:
        return
    state = _state(session_state)
    for scope in outcome.effects:
        if scope == "workspace" and tool_name in _FILE_EDIT_TOOLS:
            path = _extract_path_from_args(bound_args)
            if path and is_non_code_path(path):
                continue
        state[f"{scope}_edited"] = True
        state[f"last_{scope}_edit_tool"] = tool_name
        if scope == "workspace":
            state["last_edit_tool"] = tool_name
    if outcome.status == "succeeded":
        for scope in outcome.evidence:
            state[f"{scope}_edited"] = False
            state[f"last_{scope}_verify_tool"] = tool_name
            if scope == "workspace":
                state["last_verify_tool"] = tool_name


def reset_turn_verification(session_state: dict[str, Any]) -> None:
    """每轮入口清零验证状态(类比 executor.py 清 _final_validator_* 旗标)。"""
    session_state[_VERIFY_STATE_KEY] = {
        "workspace_edited": False,
        "browser_edited": False,
        "nudge_attempts": 0,
    }


def build_verify_on_stop_nudge(
    session_state: dict[str, Any],
    *,
    session_store: Any = None,
    session_id: str | None = None,
) -> str | None:
    """停止闸门:若工作区处于 stale 且未超 nudge 上限,返回 nudge 文本;否则 None。

    返回 None 表示放行(无需再验证 / 已达上限)。调用方应将返回的文本作为
    ModelRetry 的内容抛出,把模型推回工具循环继续验证。

    若传入 ``session_store`` + ``session_id``,nudge 会附上"上次验证"引用(命令文本
    + 运行状态 + 输出末尾),让模型知道上次验证的具象内容。不传则退化为原文案
    (向后兼容,测试可只传 session_state)。
    """
    state = _state(session_state)
    workspace_edited = bool(state.get("workspace_edited"))
    browser_edited = bool(state.get("browser_edited"))
    if not workspace_edited and not browser_edited:
        return None
    attempts = int(state.get("nudge_attempts", 0) or 0)
    if attempts >= _MAX_NUDGE_ATTEMPTS:
        # 已 nudge 两次仍 stale:放行,把模型的话给用户,避免死循环。下游
        # _validate_execution_result 仍会基于 final_verification_signal 闸门再判一次。
        return None
    state["nudge_attempts"] = attempts + 1
    parts = []
    if workspace_edited:
        last_edit = state.get("last_workspace_edit_tool", state.get("last_edit_tool", "编辑工具"))
        parts.append(
            f"[系统:验证缺口] 本轮你用 `{last_edit}` 改动了工作区,但之后还没有运行任何验证"
            "来确认改动成立(如跑测试 / lint / 构建核对 / git diff / 读取产物核对)。\n"
            "请先调用相应验证工具(优先 run_command 跑测试或核对、或 read_file / git_diff "
            "复核改动),确认无误后再给用户最终回复。"
        )
        evidence_block = _format_recent_evidence(session_store, session_id)
        if evidence_block:
            parts.append(evidence_block)
    if browser_edited:
        last_edit = state.get("last_browser_edit_tool", "浏览器交互工具")
        parts.append(
            f"[系统:验证缺口] 本轮你用 `{last_edit}` 改动了浏览器/远端状态(也可能是失败操作的部分副作用),"
            "但之后还没有浏览器验证证据。请使用 browser_get_state / browser_extract_content "
            "观察实际结果,或 browser_wait_for(text=...) 确认预期文本。"
            "本地 git_status / git_diff / 文件读取不能验证远端操作。"
        )
    parts.append(
        "若客观上无法验证,请在回复里如实说明这是未经验证的改动,不要声称\"已验证通过\"。"
    )
    return "\n".join(parts)


def _format_recent_evidence(session_store: Any, session_id: str | None) -> str | None:
    """从 SessionStore 取最近一条验证证据,格式化成 nudge 附注。无则返回 None。"""
    if session_store is None or not session_id:
        return None
    try:
        recent = session_store.load_recent_verification_evidence(session_id, limit=1)
    except Exception:
        return None
    if not recent:
        return None
    ev = recent[0]
    command = str(ev.get("command") or "").strip()
    if not command:
        return None
    running = bool(ev.get("is_running"))
    summary = str(ev.get("output_summary") or "").strip()
    if len(summary) > 200:
        summary = "..." + summary[-200:]
    status_label = "仍在运行中" if running else "已结束"
    line = f"\n[上次验证参考] 最近一次验证命令:`{command}`({status_label})。"
    if summary:
        line += f" 输出末尾:{summary}"
    return line


__all__ = [
    "mark_tool_outcome",
    "mark_tool_succeeded",
    "reset_turn_verification",
    "build_verify_on_stop_nudge",
]
