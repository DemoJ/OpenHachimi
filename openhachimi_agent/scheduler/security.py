"""定时任务安全策略。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyResult:
    status: str
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status == "allowed"


_THREAT_PATTERNS = [
    (r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
    (r"system\s+prompt\s+override", "system_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (r"do\s+not\s+tell\s+the\s+user", "deception"),
    (r"(?:cat|type|get-content|head|tail|less|more|读取|查看|打开)\s*[^\n]*(\.env|credentials|\.netrc|id_rsa|private[_-]?key)", "read_secrets"),
    (r"(?:cat|type|get-content|head|tail|less|more|读取|查看|打开)\s*[^\n]*(\.ssh|\.netrc|\.aws|\.gnupg)", "read_secrets"),
    (r"(?:cat|type|get-content|read|head|tail|读取|查看)\s*[^\n]*(config\.ya?ml|config\.yml|sessions\.sqlite3)", "read_secrets"),
    (r"(?:token|api[_-]?key|appsecret|password|passwd|credential)[^\n]{0,80}(?:telegram|wechat|微信|http|url|webhook|email|mail|发送|发给|send|post|upload)", "secret_exfiltration"),
    (r"(?:telegram|wechat|微信|webhook)[^\n]{0,80}(?:token|api[_-]?key|appsecret|password|passwd|credential)", "secret_exfiltration"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"/etc/sudoers|visudo", "sudoers_modification"),
    (r"rm\s+-rf\s+/", "destructive_root_rm"),
    (r"drop\s+database|truncate\s+table", "destructive_database"),
    (r"curl\s+[^\n]*(\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?)", "secret_exfiltration"),
    (r"wget\s+[^\n]*(\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?)", "secret_exfiltration"),
    # 下载即执行 / 内联代码执行:无人值守下没有确认兜底
    (r"(?:curl|wget)[^\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", "download_execute"),
    (r"(?:curl|wget)[^\n]*\|\s*(?:sudo\s+)?(?:python3?|node)\b", "download_execute"),
    (r"\|\s*(?:sudo\s+)?iex\b", "download_execute"),
    (r"python3?\s+(?:-\S+\s+)*-c\b", "inline_code_execution"),
    (r"node\s+(?:-\S+\s+)*(?:-e\b|--eval\b)", "inline_code_execution"),
    (r"(?:powershell|pwsh)[^\n]*\s-(?:enc|encodedcommand)\b", "inline_code_execution"),
    # 持久化载体
    (r"\bschtasks\b|\bcrontab\b|\bbitsadmin\b|\bmshta\b", "persistence"),
]

_INVISIBLE_CHARS = {"​", "‌", "‍", "⁠", "﻿", "‪", "‫", "‬", "‭", "‮"}
_SCHEDULED_MUTATIONS = {"create", "update", "update_delivery", "pause", "resume", "remove", "delete", "run", "mark_read"}
_SCHEDULED_MUTATION_ERROR = "定时任务执行期间禁止创建、修改、触发、删除或标记定时任务。"


def scan_scheduled_prompt(prompt: str) -> SafetyResult:
    """扫描无人值守定时任务提示词，拒绝明显危险或隐藏意图。"""
    for char in _INVISIBLE_CHARS:
        if char in prompt:
            return SafetyResult("rejected", f"提示词包含不可见字符 U+{ord(char):04X}。")
    for pattern, name in _THREAT_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            return SafetyResult("rejected", f"提示词命中安全规则：{name}。")
    return SafetyResult("allowed")


def ensure_scheduler_mutation_allowed(run_mode: str) -> None:
    """定时任务无人值守执行中禁止递归修改调度系统。"""
    if run_mode == "scheduled":
        raise RuntimeError(_SCHEDULED_MUTATION_ERROR)


def ensure_scheduler_action_allowed(run_mode: str, action: str, *, mutates: bool | None = None) -> None:
    """兼容旧 action 工具的安全检查；新写工具应直接使用 ensure_scheduler_mutation_allowed。"""
    if mutates is True:
        ensure_scheduler_mutation_allowed(run_mode)
        return
    if run_mode == "scheduled" and action in _SCHEDULED_MUTATIONS:
        raise RuntimeError(_SCHEDULED_MUTATION_ERROR)
