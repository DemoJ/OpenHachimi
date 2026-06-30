"""Verification evidence — classify tools into "editors" vs "verifiers".

照搬 NousResearch/hermes-agent ``verification_stop.py`` 的策略层,但放弃它
重型的 SQLite evidence ledger(它给 Hermes 的 run_command 等命令做入库)。OpenHachimi
已有 ``execution_ledger``(session_state 内存)记录每个工具的成败,这里只补一层
**工具语义分类**:哪些工具算"改了工作区"(edit → 下次停止需要 fresh 证据)、
哪些算"产出了验证证据"(verify → 抵消 edit 的 stale)。

策略只读不改 ledger,不阻断工具、不自己跑检查 —— 纯谓词,供 ``verification_stop``
闸门和 ``_validate_execution_result`` 消费。
"""

from __future__ import annotations

from functools import lru_cache


# ── 改动工作区的工具 ──────────────────────────────────────────────────────
# 工具调用成功后,工作区状态相对"上次验证证据"变成 stale。停止闸门据此决定要不要
# nudge 模型先验证再结束。与 ``tools/registry.py:_MUTATION_FUNCS`` 保持同步:那里是
# "有副作用、要 ledger + reminder" 的全集,这里取其中的"改了代码/文件/命令产物"子集
# (remember / forget_memory 不改工作区代码,不算编辑证据缺口)。
_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        # 文件写入/替换/删除
        "write_file",
        "replace_in_file",
        "make_directory",
        "delete_path",
        "publish_artifact",
        # 命令执行(可能改文件、装依赖、跑构建)
        "run_command",
        "send_command_input",
        # 浏览器交互(可能改远端状态:提交表单、点击按钮)
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_new_tab",
        "browser_switch_tab",
        "browser_close_tab",
        # skill 安装会落盘文件
        "install_skill",
    }
)


# ── 产出验证证据的工具 ─────────────────────────────────────────────────────
# 这些工具成功执行后,视为"模型对工作区做了一次有效核验",可抵消最近一次 edit 的
# stale。run_command 一身二任:它既可能是编辑(跑构建/改文件)也可能是验证(跑测试/
# lint/git diff 核对)。这里宽松地把 run_command 同时算 edit 和 verify —— 闸门判定时
# 只看"最近一次编辑之后有没有再调过任意 verify 工具",所以一次 run_command 之后若没
# 有新的 write_file,本身就构成"编辑者已自查"的证据。
_VERIFY_TOOLS: frozenset[str] = frozenset(
    {
        "run_command",        # 跑测试/lint/构建核对/git diff
        "send_command_input",
        "git_status",
        "git_diff",
        "read_file",          # 读取自己刚写入的文件核对内容
        "list_files",
        "find_files",
        "search_text",
        "browser_get_state",  # 浏览器操作后抓取页面状态确认结果
        "browser_extract_content",
        "command_status",
    }
)


@lru_cache(maxsize=1)
def edit_tools() -> frozenset[str]:
    """返回所有"编辑类"工具名集合(语义稳定的快照)。"""
    return _EDIT_TOOLS


@lru_cache(maxsize=1)
def verify_tools() -> frozenset[str]:
    """返回所有"验证类"工具名集合(语义稳定的快照)。"""
    return _VERIFY_TOOLS


def is_edit_tool(tool_name: str | None) -> bool:
    """该工具的成功调用会让工作区相对"上次验证"变成 stale。"""
    return bool(tool_name) and tool_name in _EDIT_TOOLS


def is_verify_tool(tool_name: str | None) -> bool:
    """该工具的成功调用产出一次验证证据,可抵消最近一次 edit。"""
    return bool(tool_name) and tool_name in _VERIFY_TOOLS


__all__ = [
    "edit_tools",
    "verify_tools",
    "is_edit_tool",
    "is_verify_tool",
]
