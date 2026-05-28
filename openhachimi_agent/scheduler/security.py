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
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|id_rsa|private[_-]?key)", "read_secrets"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"/etc/sudoers|visudo", "sudoers_modification"),
    (r"rm\s+-rf\s+/", "destructive_root_rm"),
    (r"drop\s+database|truncate\s+table", "destructive_database"),
    (r"curl\s+[^\n]*(\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?)", "secret_exfiltration"),
    (r"wget\s+[^\n]*(\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?)", "secret_exfiltration"),
]

_INVISIBLE_CHARS = {"​", "‌", "‍", "⁠", "﻿", "‪", "‫", "‬", "‭", "‮"}
_SCHEDULED_MUTATIONS = {"create", "update", "update_delivery", "pause", "resume", "remove", "run", "mark_read"}


def scan_scheduled_prompt(prompt: str) -> SafetyResult:
    """扫描无人值守定时任务提示词，拒绝明显危险或隐藏意图。"""
    for char in _INVISIBLE_CHARS:
        if char in prompt:
            return SafetyResult("rejected", f"提示词包含不可见字符 U+{ord(char):04X}。")
    for pattern, name in _THREAT_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            return SafetyResult("rejected", f"提示词命中安全规则：{name}。")
    return SafetyResult("allowed")


def ensure_scheduler_action_allowed(run_mode: str, action: str) -> None:
    """定时任务无人值守执行中禁止递归修改调度系统。"""
    if run_mode == "scheduled" and action in _SCHEDULED_MUTATIONS:
        raise RuntimeError("定时任务执行期间禁止创建、修改、触发或删除定时任务。")
