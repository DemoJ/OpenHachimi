"""工作区命令执行工具。"""

from __future__ import annotations

import time
import logging
import asyncio

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.utils import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    assert_safe_command,
    get_command_shell,
    normalize_relative_path,
    resolve_workspace_path,
    inject_prompt_if_unread,
)

logger = logging.getLogger(__name__)
MAX_RUN_COMMAND_WAIT_SECONDS = 120


async def run_command(
    ctx: RunContext[AgentDeps],
    command: str,
    cwd: str = ".",
    wait_seconds: float = 5.0,
) -> dict[str, object]:
    """在工作区内启动并执行系统命令。
    
    命令会在后台异步运行。它会最多等待 wait_seconds 秒，如果在此期间命令执行完毕，
    则返回完整的执行结果；如果命令仍未结束（例如运行时间很长，或者需要交互式输入），
    则返回一个 command_id 和迄今为止的输出内容。
    wait_seconds=0 会立即返回后台任务；长测试、构建、安装可设置到 60~120 秒。
    后续可以使用 command_status 和 send_command_input 工具与之交互。
    """
    if not command.strip():
        raise ValueError("command 不能为空")

    logger.info("tool run_command cwd=%s wait_seconds=%.1f command=%s", cwd, wait_seconds, command)
    assert_safe_command(command)
    target_cwd = resolve_workspace_path(ctx.deps.base_dir, cwd)
    if not target_cwd.exists():
        raise FileNotFoundError(f"工作目录不存在：{cwd}")
    if not target_cwd.is_dir():
        raise NotADirectoryError(f"工作目录不是目录：{cwd}")

    shell_command, shell_name = get_command_shell()
    
    # 使用 ProcessManager 启动后台进程
    proc = ctx.deps.process_manager.start_process(
        [*shell_command, command],
        cwd=target_cwd,
        shell_name=shell_name,
        session_id=ctx.deps.session_id,
    )
    
    # 等待一小段时间
    wait_seconds = max(0.0, min(wait_seconds, MAX_RUN_COMMAND_WAIT_SECONDS))
    end_time = time.monotonic() + wait_seconds

    while time.monotonic() < end_time and proc.is_running():
        await asyncio.sleep(0.5)
        
    output, truncated = proc.get_output()
    is_running = proc.is_running()
    
    result = {
        "command_id": proc.id,
        "is_running": is_running,
        "cwd": normalize_relative_path(ctx.deps.base_dir, target_cwd) if target_cwd != ctx.deps.base_dir else ".",
        "shell": shell_name,
        "command_text": command,
        "output": output,
        "output_truncated": truncated,
        "message": "命令正在后台运行，可能在等待输入" if is_running else "命令执行完毕",
    }
    return result


def command_status(ctx: RunContext[AgentDeps], command_id: str) -> dict[str, object]:
    """检查后台运行中的命令状态，获取最新的输出日志。"""
    proc = ctx.deps.process_manager.get_process(command_id)
    if not proc:
        return {"error": f"找不到命令 ID: {command_id}"}

    output, truncated = proc.get_output()
    is_running = proc.is_running()

    result = {
        "command_id": command_id,
        "is_running": is_running,
        "output": output,
        "output_truncated": truncated,
        "message": "命令仍在运行" if is_running else "命令已结束",
    }
    return result


from typing import Literal

async def send_command_input(
    ctx: RunContext[AgentDeps], 
    command_id: str, 
    text: str = "",
    special_key: Literal["enter", "up", "down", "space", "esc", "ctrl-c", "none"] = "none",
    wait_seconds: float = 2.0
) -> dict[str, object]:
    """向后台运行且等待输入的交互式命令发送输入文本或特殊按键。
    
    如果需要发送普通文本，使用 `text` 参数。
    如果需要发送特殊按键（如回车、方向键），请使用 `special_key` 参数。
    不要在 text 中发送像 "\\n" 或 "\\r" 这样的转义字符，请直接使用 special_key="enter"。
    wait_seconds 表示最多等待多久以捕获新输出；有新输出或进程结束会提前返回。
    """
    proc = ctx.deps.process_manager.get_process(command_id)
    if not proc:
        return {"error": f"找不到命令 ID: {command_id}"}

    if not proc.is_running():
        return {"error": "命令已经结束，无法发送输入。"}

    before_output, _ = proc.get_output()
    before_len = len(before_output)

    try:
        if text:
            proc.send_input(text)
            
        if special_key != "none":
            key_map = {
                "enter": "\r",
                "up": "\x1b[A",
                "down": "\x1b[B",
                "space": " ",
                "esc": "\x1b",
                "ctrl-c": "\x03"
            }
            if special_key in key_map:
                proc.send_input(key_map[special_key])
                
    except Exception as e:
        return {"error": f"发送输入失败：{e}"}
        
    wait_seconds = max(0.0, min(wait_seconds, 15.0))
    deadline = time.monotonic() + wait_seconds
    output, truncated = proc.get_output()

    while time.monotonic() < deadline:
        if not proc.is_running() or len(output) != before_len:
            break
        await asyncio.sleep(0.1)
        output, truncated = proc.get_output()

    if proc.is_running() and len(output) == before_len:
        output, truncated = proc.get_output()

    result = {
        "command_id": command_id,
        "is_running": proc.is_running(),
        "output": output,
        "output_truncated": truncated,
    }
    return result
