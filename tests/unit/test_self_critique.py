"""Tests for the self-critique (反思) feature.

Covers:
  - 正面场景: critique → revise → repair → pass
  - 正面场景: critique → revise → repair → still revise → return signal
  - 正面场景: critique passes directly (no repair needed)
  - 正面场景: 多任务计划中某个子任务结果不完整触发反思
  - 边界场景: self_critique agent 自身异常 → 降级为 pass
  - 边界场景: verification 未通过时不触发 critique（先走 repair）
  - 边界场景: 仅有一次 repair 机会（不会无限循环）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openhachimi_agent.agent.intent import SelfCritiqueDecision
from openhachimi_agent.service.agent_runtime.context import AgentRunContext
from openhachimi_agent.service.agent_runtime.executor import (
    _build_self_critique_message,
    _build_self_critique_repair_message,
    execute_task,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRunResult:
    def __init__(self, output):
        self.output = output

    def all_messages(self):
        return []


class FakeSequenceAgent:
    """Return pre-configured results in sequence."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self._call_count = 0
        self.messages: list[str] = []

    async def run(self, message, **kwargs):
        self.messages.append(str(message))
        idx = min(self._call_count, len(self._outputs) - 1)
        output = self._outputs[idx]
        self._call_count += 1
        if isinstance(output, Exception):
            raise output
        return FakeRunResult(output)


class FakeErrorAgent:
    """Always raises an exception."""

    def __init__(self, exc):
        self._exc = exc

    async def run(self, message, **kwargs):
        raise self._exc


def _make_ctx(config, message="测试任务", session_state=None):
    if session_state is None:
        session_state = {}
    deps = SimpleNamespace(run_mode="interactive", session_state=session_state)
    return AgentRunContext(
        config=config,
        role="default",
        session_id="test-session",
        message=message,
        attachments=[],
        history=[],
        deps=deps,
        session_state=session_state,
        stream=False,
    )


# ---------------------------------------------------------------------------
# 正面场景测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critique_passes_directly_no_repair(mock_config):
    """场景: critique 判定 pass，不需要修复。"""
    session_state = {}
    ctx = _make_ctx(mock_config, "生成报告", session_state)

    executor = FakeSequenceAgent(["完整报告已生成，包含所有章节。"])
    critic = FakeSequenceAgent([
        SelfCritiqueDecision(verdict="pass", confidence=0.95, rationale="报告完整"),
    ])

    def get_agent(_role, agent_type):
        if agent_type == "executor":
            return executor
        if agent_type == "self_critique":
            return critic
        raise AssertionError(f"unexpected agent_type: {agent_type}")

    outcome = await execute_task(ctx, get_agent)

    assert outcome.result.output == "完整报告已生成，包含所有章节。"
    assert outcome.self_critique_signal is None
    assert outcome.final_verification_signal is None
    # executor 只运行了一次，critic 也只运行了一次
    assert executor._call_count == 1
    assert critic._call_count == 1


@pytest.mark.asyncio
async def test_critique_revise_then_repair_passes(mock_config):
    """场景: critique 判定 revise → executor 修复 → 再次 critique pass。"""
    session_state = {}
    ctx = _make_ctx(mock_config, "搜索新闻", session_state)

    executor = FakeSequenceAgent([
        "搜索结果：找到了一些新闻",  # 候选答案
        "搜索结果：找到了 DeepSeek 74亿美元融资等5条重大新闻",  # 修复后
    ])
    critic = FakeSequenceAgent([
        SelfCritiqueDecision(
            verdict="revise",
            confidence=0.85,
            issues=["缺少具体新闻内容", "未附带来源引用"],
            repair_instructions="请列出具体新闻标题和来源",
            rationale="候选回复过于笼统",
        ),
        SelfCritiqueDecision(verdict="pass", confidence=0.9, rationale="修复后完整"),
    ])

    def get_agent(_role, agent_type):
        if agent_type == "executor":
            return executor
        if agent_type == "self_critique":
            return critic
        raise AssertionError(f"unexpected agent_type: {agent_type}")

    outcome = await execute_task(ctx, get_agent)

    assert "DeepSeek" in outcome.result.output
    assert outcome.self_critique_signal is None
    assert executor._call_count == 2
    assert critic._call_count == 2
    # 修复消息中应包含 critique 的 issues
    assert "缺少具体新闻内容" in executor.messages[1]


@pytest.mark.asyncio
async def test_critique_revise_then_still_revise_returns_signal(mock_config):
    """场景: 修复后 critique 仍然 revise → 返回 self_critique_signal。"""
    session_state = {}
    ctx = _make_ctx(mock_config, "分析代码", session_state)

    executor = FakeSequenceAgent([
        "代码没问题",  # 候选
        "代码有bug但未修复",  # 修复后仍然不够
    ])
    critic = FakeSequenceAgent([
        SelfCritiqueDecision(
            verdict="revise",
            confidence=0.8,
            issues=["未提供具体分析"],
            repair_instructions="请分析具体代码逻辑",
        ),
        SelfCritiqueDecision(
            verdict="revise",
            confidence=0.7,
            issues=["分析仍不充分"],
            repair_instructions="请深入分析",
        ),
    ])

    def get_agent(_role, agent_type):
        if agent_type == "executor":
            return executor
        if agent_type == "self_critique":
            return critic
        raise AssertionError(f"unexpected agent_type: {agent_type}")

    outcome = await execute_task(ctx, get_agent)

    assert outcome.result.output == "代码有bug但未修复"
    assert outcome.self_critique_signal is not None
    assert outcome.self_critique_signal["issues"][0]["type"] == "self_critique_revision_required"
    assert "分析仍不充分" in outcome.self_critique_signal["issues"][0]["items"]


@pytest.mark.asyncio
async def test_critique_with_active_todos_uses_execution_evidence(mock_config):
    """场景: 活跃 TODO 存在时，critique 的 evidence 应包含 TODO 状态。"""
    session_state = {}
    # 设置模拟的 todo_state
    mock_task = SimpleNamespace(id=1, description="搜索AI新闻", status="done", notes="已完成")
    mock_task2 = SimpleNamespace(id=2, description="汇总报告", status="done", notes="")
    todo_state = SimpleNamespace(
        is_active=True,
        tasks={1: mock_task, 2: mock_task2},
    )
    session_state["todo_state"] = todo_state

    ctx = _make_ctx(mock_config, "查找AI新闻", session_state)

    executor = FakeSequenceAgent(["AI新闻汇总报告"])
    critic_evidence_captured = {}

    class CapturingCritic:
        def __init__(self):
            self._call_count = 0

        async def run(self, prompt, **kwargs):
            self._call_count += 1
            critic_evidence_captured["prompt"] = str(prompt)
            return FakeRunResult(SelfCritiqueDecision(verdict="pass", confidence=0.9))

    critic = CapturingCritic()

    def get_agent(_role, agent_type):
        if agent_type == "executor":
            return executor
        if agent_type == "self_critique":
            return critic
        raise AssertionError(f"unexpected agent_type: {agent_type}")

    outcome = await execute_task(ctx, get_agent)

    assert outcome.self_critique_signal is None
    # 验证 critique prompt 包含了 TODO 状态信息
    assert "搜索AI新闻" in critic_evidence_captured["prompt"]
    assert "汇总报告" in critic_evidence_captured["prompt"]


# ---------------------------------------------------------------------------
# 边界场景测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critique_agent_exception_degrades_to_pass(mock_config):
    """场景: self_critique agent 抛出异常 → 降级为 pass，不阻塞返回。"""
    session_state = {}
    ctx = _make_ctx(mock_config, "简单任务", session_state)

    executor = FakeSequenceAgent(["任务完成"])

    critic = FakeErrorAgent(RuntimeError("LLM API timeout"))

    def get_agent(_role, agent_type):
        if agent_type == "executor":
            return executor
        if agent_type == "self_critique":
            return critic
        raise AssertionError(f"unexpected agent_type: {agent_type}")

    outcome = await execute_task(ctx, get_agent)

    # 异常应被 catch，降级为 pass
    assert outcome.result.output == "任务完成"
    assert outcome.self_critique_signal is None


@pytest.mark.asyncio
async def test_verification_fails_skips_critique(mock_config):
    """场景: final_verification 未通过时（有未完成 TODO），不走 critique 流程。"""
    session_state = {}
    # 设置有未完成任务的 todo_state
    mock_task = SimpleNamespace(id=1, description="步骤一", status="in-progress", notes="")
    todo_state = SimpleNamespace(
        is_active=True,
        tasks={1: mock_task},
    )
    session_state["todo_state"] = todo_state
    # 模拟最后一次执行也是失败的
    session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "run_command", "status": "failed", "violation": "command not found"},
    ]
    session_state["current_turn_ledger_start_seq"] = 0

    ctx = _make_ctx(mock_config, "执行命令", session_state)

    executor = FakeSequenceAgent(["部分结果"])
    # verification 失败 → executor 被要求修复，修复后仍然有未完成 TODO
    # 最终 verification_signal 非 None，critique 不会被调用
    critic = FakeSequenceAgent([
        SelfCritiqueDecision(verdict="pass", confidence=0.9),  # 不应该被调用
    ])

    def get_agent(_role, agent_type):
        if agent_type == "executor":
            return executor
        if agent_type == "self_critique":
            return critic
        raise AssertionError(f"unexpected agent_type: {agent_type}")

    outcome = await execute_task(ctx, get_agent)

    # verification signal 应存在（有未完成 TODO）
    assert outcome.final_verification_signal is not None
    # critic 不应被调用（verification 未通过就不会走 critique）
    assert critic._call_count == 0


@pytest.mark.asyncio
async def test_only_one_repair_attempt(mock_config):
    """场景: 最多只允许一次 critique repair 机会。"""
    session_state = {}
    ctx = _make_ctx(mock_config, "写代码", session_state)

    executor = FakeSequenceAgent(["v1", "v2"])
    critic = FakeSequenceAgent([
        SelfCritiqueDecision(
            verdict="revise",
            issues=["需要修改"],
            repair_instructions="请修正",
        ),
        SelfCritiqueDecision(
            verdict="revise",
            issues=["仍然不够"],
            repair_instructions="继续修正",
        ),
    ])

    def get_agent(_role, agent_type):
        if agent_type == "executor":
            return executor
        if agent_type == "self_critique":
            return critic
        raise AssertionError(f"unexpected agent_type: {agent_type}")

    outcome = await execute_task(ctx, get_agent)

    # 第二次 critique 仍然是 revise，但已经用完一次 repair 机会
    # 不会再触发第三次修复
    assert outcome.self_critique_signal is not None
    # executor 只运行了 2 次（初始 + 1次修复），不会无限循环
    assert executor._call_count == 2
    assert critic._call_count == 2


# ---------------------------------------------------------------------------
# 单元测试: prompt 构建
# ---------------------------------------------------------------------------

def test_critique_prompt_contains_task_frame_and_evidence():
    """验证 critique prompt 包含 TaskFrame、用户消息、执行证据和候选回复。"""
    task_frame = {"goal": "搜索AI新闻", "invariants": ["不编造内容"]}
    evidence = {
        "todos": [{"id": 1, "description": "搜索", "status": "done"}],
        "current_turn_events": [{"seq": 1, "tool_name": "web_search", "status": "succeeded"}],
    }

    prompt = _build_self_critique_message(
        task_frame, "搜索AI新闻", "找到5条新闻", evidence,
    )

    assert "搜索AI新闻" in prompt
    assert "找到5条新闻" in prompt
    assert "web_search" in prompt
    assert "不编造内容" in prompt


def test_repair_prompt_contains_critique_issues_and_instructions():
    """验证 repair prompt 包含 critique 的 issues 和 repair_instructions。"""
    critique = SelfCritiqueDecision(
        verdict="revise",
        issues=["缺少引用来源", "摘要不完整"],
        repair_instructions="请补充来源引用并完善摘要",
        confidence=0.8,
    )
    evidence = {"current_turn_events": []}

    prompt = _build_self_critique_repair_message(
        {"goal": "写摘要"},
        "写新闻摘要",
        "新闻摘要草稿",
        critique,
        evidence,
    )

    assert "缺少引用来源" in prompt
    assert "摘要不完整" in prompt
    assert "请补充来源引用并完善摘要" in prompt
    assert "新闻摘要草稿" in prompt


# ---------------------------------------------------------------------------
# 单元测试: SelfCritiqueDecision 模型
# ---------------------------------------------------------------------------

def test_self_critique_decision_default_pass():
    """默认 verdict 应为 pass。"""
    decision = SelfCritiqueDecision()
    assert decision.verdict == "pass"
    assert decision.issues == []
    assert decision.repair_instructions == ""


def test_self_critique_decision_revise_with_issues():
    """revise 时应包含 issues 和 repair_instructions。"""
    decision = SelfCritiqueDecision(
        verdict="revise",
        issues=["结果不完整"],
        repair_instructions="请补齐缺失部分",
        confidence=0.7,
        rationale="候选回复缺少关键信息",
    )
    assert decision.verdict == "revise"
    assert len(decision.issues) == 1
    assert decision.repair_instructions == "请补齐缺失部分"
    assert decision.confidence == 0.7
