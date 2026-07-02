"""Turn-end verification gate (照搬 Hermes 策略层,复用 execution_ledger)。

Hermes 的 ``verification_stop`` 在模型"编辑代码后想直接结束"时,注入一条 synthetic
user nudge,强制它先验证再收尾。它配一套独立的 SQLite ``verification_evidence``
ledger 记录每个 run_command 的归档证据。

OpenHachimi 已有 ``execution_ledger``(session_state 内存,记每个工具的 started/
succeeded/failed)。这里**不开新库**,只维护一个轻量状态机:

- `mark_workspace_edited(session_state, tool_name)` — 编辑类工具成功后调用,置 stale。
- `mark_workspace_verified(session_state, tool_name)` — 验证类工具成功后调用,清 stale。
- `build_verify_on_stop_nudge(session_state, attempts)` — 停止闸门调用:若 stale 且
  未超上限,返回一段 nudge 文本;否则返回 None 放行。

策略层(它只做判定、不自己跑检查)与 Hermes 同源:被动、有界、最多 nudge 两次后放行
避免死循环。非代码编辑(prose/markdown)不触发 nudge 的细化交给 ``verification_evidence``
的工具分类 —— 我们没有 Hermes 那种按文件后缀过滤的能力(ledger 不存路径),所以
保险地把所有 edit 类工具都算作"可能需要验证",靠 max_attempts 上限兜底防止过度拦截。
"""

from __future__ import annotations

from typing import Any

from openhachimi_agent.agent.verification_evidence import (
    _PATH_ARG_NAMES,
    is_edit_tool,
    is_non_code_path,
    is_verify_tool,
)


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
    """工具成功后按语义更新验证状态:编辑类置 stale,验证类清 stale。

    由 ``with_execution_ledger`` 在记完 succeeded 事件后统一调用,工具层无需感知。
    run_command 身兼 edit/verify 两职:它成功后既算编辑(置 stale)也算验证(立即清
    stale),净效果是"模型用 run_command 自查过,没有遗留缺口"。

    文件类型过滤(照搬 Hermes):write_file/replace_in_file 若改的是纯文本/文档
    (.md/.txt/LICENSE 等),不置 stale——文档没有可验证的运行时行为,不该触发
    "去跑测试"的 nudge。读不到路径参数时保守置 stale(不误放)。
    """
    if not tool_name:
        return
    state = _state(session_state)
    if is_edit_tool(tool_name):
        # 文件写入工具按路径后缀过滤:纯文本编辑不置 stale。
        if tool_name in _FILE_EDIT_TOOLS:
            path = _extract_path_from_args(bound_args)
            if path and is_non_code_path(path):
                # 纯文档改动,跳过置 stale。
                return
        state["workspace_edited"] = True
        state["last_edit_tool"] = tool_name
    if is_verify_tool(tool_name):
        # 一次验证证据抵消最近一次编辑缺口。
        state["workspace_edited"] = False
        state["last_verify_tool"] = tool_name


def reset_turn_verification(session_state: dict[str, Any]) -> None:
    """每轮入口清零验证状态(类比 executor.py 清 _final_validator_* 旗标)。"""
    session_state[_VERIFY_STATE_KEY] = {
        "workspace_edited": False,
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
    if not state.get("workspace_edited"):
        return None
    attempts = int(state.get("nudge_attempts", 0) or 0)
    if attempts >= _MAX_NUDGE_ATTEMPTS:
        # 已 nudge 两次仍 stale:放行,把模型的话给用户,避免死循环。下游
        # _validate_execution_result 仍会基于 final_verification_signal 闸门再判一次。
        return None
    state["nudge_attempts"] = attempts + 1
    last_edit = state.get("last_edit_tool", "编辑工具")
    parts = [
        f"[系统:验证缺口] 本轮你用 `{last_edit}` 改动了工作区,但之后还没有运行任何验证"
        "来确认改动成立(如跑测试 / lint / 构建核对 / git diff / 读取产物核对)。\n"
        "请先调用相应验证工具(优先 run_command 跑测试或核对、或 read_file / git_diff "
        "复核改动),确认无误后再给用户最终回复。\n"
        "若客观上无法验证(纯文本/文档改动、无测试可跑),请在回复里如实说明这是未经验证的"
        "改动,不要声称\"已验证通过\"。"
    ]
    # 附上最近一次验证证据(若有),让模型知道上次跑了什么、结果如何。
    evidence_block = _format_recent_evidence(session_store, session_id)
    if evidence_block:
        parts.append(evidence_block)
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
    "mark_tool_succeeded",
    "reset_turn_verification",
    "build_verify_on_stop_nudge",
]
