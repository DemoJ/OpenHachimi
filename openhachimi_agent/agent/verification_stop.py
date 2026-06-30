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

from openhachimi_agent.agent.verification_evidence import is_edit_tool, is_verify_tool


# 闸门在单 turn 内最多 nudge 的次数。超过即放行 —— 避免模型反复"编辑→被拦→说完成"
# 死循环把整轮报废。与 Hermes 的 max_attempts=2 对齐。
_MAX_NUDGE_ATTEMPTS = 2

# session_state 下的验证状态键。
_VERIFY_STATE_KEY = "_verification_state"


def _state(session_state: dict[str, Any]) -> dict[str, Any]:
    """取(或初始化)本轮验证状态子字典。"""
    state = session_state.get(_VERIFY_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        session_state[_VERIFY_STATE_KEY] = state
    return state


def mark_tool_succeeded(session_state: dict[str, Any], tool_name: str | None) -> None:
    """工具成功后按语义更新验证状态:编辑类置 stale,验证类清 stale。

    由 ``with_execution_ledger`` 在记完 succeeded 事件后统一调用,工具层无需感知。
    run_command 身兼 edit/verify 两职:它成功后既算编辑(置 stale)也算验证(立即清
    stale),净效果是"模型用 run_command 自查过,没有遗留缺口"。
    """
    if not tool_name:
        return
    state = _state(session_state)
    if is_edit_tool(tool_name):
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


def build_verify_on_stop_nudge(session_state: dict[str, Any]) -> str | None:
    """停止闸门:若工作区处于 stale 且未超 nudge 上限,返回 nudge 文本;否则 None。

    返回 None 表示放行(无需再验证 / 已达上限)。调用方应将返回的文本作为
    ModelRetry 的内容抛出,把模型推回工具循环继续验证。
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
    return (
        f"[系统:验证缺口] 本轮你用 `{last_edit}` 改动了工作区,但之后还没有运行任何验证"
        "来确认改动成立(如跑测试 / lint / 构建核对 / git diff / 读取产物核对)。\n"
        "请先调用相应验证工具(优先 run_command 跑测试或核对、或 read_file / git_diff "
        "复核改动),确认无误后再给用户最终回复。\n"
        "若客观上无法验证(纯文本/文档改动、无测试可跑),请在回复里如实说明这是未经验证的"
        "改动,不要声称\"已验证通过\"。"
    )


__all__ = [
    "mark_tool_succeeded",
    "reset_turn_verification",
    "build_verify_on_stop_nudge",
]
