"""Utility functions and constants for browser operations."""

HUMAN_VERIFICATION_REQUIRED = "HUMAN_VERIFICATION_REQUIRED"

def _human_verification_message(url: str, reason: str) -> str:
    return (
        f"{HUMAN_VERIFICATION_REQUIRED}: 检测到人机验证或访问挑战，当前页面：{url}。"
        f"触发原因：{reason}。\n"
        "【系统强制指令】：你必须在当前回合立即停止操作！绝对禁止在此次回复中调用任何其他工具（如切换搜索引擎、尝试其他网站等）。\n"
        "请直接向用户输出提示：“遇到人机验证。请在打开的浏览器窗口中手动完成验证，然后回复‘继续’。”，并立刻结束本回合，等待用户输入指令。"
    )
