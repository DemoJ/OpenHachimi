"""上下文压缩引擎单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from openhachimi_agent.context.compressor import ContextCompressor
from openhachimi_agent.context.pruning import prune_old_tool_results
from openhachimi_agent.context.token_estimate import estimate_messages_tokens


# ── 测试辅助 ──────────────────────────────────────────────────────────────
def _make_tool_turns(n: int, content: str = "X" * 4000) -> list:
    msgs: list = [ModelRequest(parts=[UserPromptPart(content="start")])]
    for i in range(n):
        msgs.append(ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": f"f{i}.py"}, tool_call_id=f"c{i}")]))
        msgs.append(ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content=content, tool_call_id=f"c{i}")]))
    msgs.append(ModelResponse(parts=[TextPart(content="done")]))
    return msgs


def _count_tool_returns(msgs: list) -> int:
    return sum(
        1
        for m in msgs
        for p in getattr(m, "parts", None) or []
        if isinstance(p, ToolReturnPart)
    )


# ── 阶段 1:剪枝与去重 ─────────────────────────────────────────────────────
def test_prune_replaces_old_large_tool_returns_with_summary():
    msgs = _make_tool_turns(5)
    pruned, count = prune_old_tool_results(msgs, protect_tail_count=2, protect_tail_tokens=300)
    assert count >= 1
    # 至少有一个工具结果被替换为摘要(以 [read_file] 开头)
    summaries = [
        str(p.content)
        for m in pruned
        for p in getattr(m, "parts", None) or []
        if isinstance(p, ToolReturnPart) and str(p.content).startswith("[read_file]")
    ]
    assert len(summaries) >= 1


def test_prune_deduplicates_identical_tool_returns():
    msgs = _make_tool_turns(5, content="SAME" * 1000)
    pruned, count = prune_old_tool_results(msgs, protect_tail_count=2, protect_tail_tokens=300)
    assert count >= 2
    backrefs = [
        1
        for m in pruned
        for p in getattr(m, "parts", None) or []
        if isinstance(p, ToolReturnPart) and "重复工具输出" in str(p.content)
    ]
    assert sum(backrefs) >= 1, "应至少有一个重复工具结果被回引"


def test_prune_preserves_tail_tool_returns_verbatim():
    big = "Z" * 5000
    msgs = _make_tool_turns(1, content=big)
    # 尾部保护足够大,工具结果应原样保留
    pruned, _ = prune_old_tool_results(msgs, protect_tail_count=5, protect_tail_tokens=100000)
    full_returns = [
        p
        for m in pruned
        for p in getattr(m, "parts", None) or []
        if isinstance(p, ToolReturnPart) and p.content == big
    ]
    assert len(full_returns) == 1


def test_prune_small_tool_returns_not_pruned():
    msgs = _make_tool_turns(3, content="tiny")
    _, count = prune_old_tool_results(msgs, protect_tail_count=2, protect_tail_tokens=300)
    # 小于 _MIN_PRUNE_CHARS 的工具结果不剪枝
    assert count == 0


# ── 阶段 0/2:触发、头尾保护 ────────────────────────────────────────────────
def test_should_compress_below_threshold_returns_false():
    comp = ContextCompressor(context_length=10000, threshold_percent=0.75)
    comp.update_from_response(SimpleNamespace(input_tokens=1000, output_tokens=10, details={}))
    assert comp.should_compress() is False


def test_should_compress_above_threshold_returns_true():
    comp = ContextCompressor(context_length=10000, threshold_percent=0.75)
    comp.update_from_response(SimpleNamespace(input_tokens=8000, output_tokens=10, details={}))
    assert comp.should_compress() is True


def test_should_compress_preflight_uses_rough_estimate():
    comp = ContextCompressor(context_length=2000, hard_ceiling_percent=0.90)
    msgs = _make_tool_turns(10)  # 大量内容,粗略估计必然超 1800
    assert comp.should_compress_preflight(msgs) is True


def test_compress_protects_head_and_tail():
    comp = ContextCompressor(
        context_length=8000,
        threshold_percent=0.75,
        protect_first_n=2,
        protect_last_n=4,
        tail_token_budget=600,
    )
    msgs = _make_tool_turns(15)
    before = len(msgs)
    result = comp.compress(msgs, current_tokens=estimate_messages_tokens(msgs))
    assert result.dropped
    compressed = result.runtime_view
    assert len(compressed) < before
    # 头部首条(用户 start)应保留
    assert any(isinstance(p, UserPromptPart) and "start" in str(p.content) for p in getattr(compressed[0], "parts", []))
    # 末条应是最后的 TextPart(done)或其所在消息
    last_texts = [str(p.content) for m in compressed for p in getattr(m, "parts", []) if isinstance(p, TextPart)]
    assert any("done" in t for t in last_texts)


# ── 阶段 4:配对清理 ────────────────────────────────────────────────────────
def test_sanitize_removes_orphan_tool_return():
    # 孤儿 ToolReturnPart(无对应 call)
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="x", content="orphan", tool_call_id="ghost")]),
        ModelResponse(parts=[TextPart(content="reply")]),
    ]
    comp = ContextCompressor(context_length=100000)
    # 直接测内部清理
    sanitized = comp._sanitize_tool_pairs(msgs)  # noqa: SLF001
    returns = [p for m in sanitized for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
    assert all(p.tool_call_id != "ghost" for p in returns)


def test_sanitize_replaces_orphan_tool_call():
    # 孤儿 ToolCallPart(无对应 return)
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[ToolCallPart(tool_name="x", args={}, tool_call_id="ghost")]),
        ModelResponse(parts=[TextPart(content="reply")]),
    ]
    comp = ContextCompressor(context_length=100000)
    sanitized = comp._sanitize_tool_pairs(msgs)  # noqa: SLF001
    calls = [p for m in sanitized for p in getattr(m, "parts", []) if isinstance(p, ToolCallPart)]
    assert all(p.tool_call_id != "ghost" for p in calls)
    # 关键回归:孤儿 call 位于 ModelResponse,替换为的 part 必须是 ModelResponse 合法的
    # TextPart——若改成 UserPromptPart,pydantic-ai 在 _map_model_response 会 assert_never。
    response_with_note = next(
        m for m in sanitized
        if isinstance(m, ModelResponse)
        and any(isinstance(p, TextPart) and "已折叠的工具调用" in str(p.content) for p in m.parts)
    )
    for part in response_with_note.parts:
        assert not isinstance(part, UserPromptPart), "ModelResponse 内不允许 UserPromptPart"


# ── 反抖动 ─────────────────────────────────────────────────────────────────
def test_anti_thrash_skips_after_two_ineffective_compressions():
    comp = ContextCompressor(
        context_length=100000,
        threshold_percent=0.75,
        hard_ceiling_percent=0.90,
        anti_thrash=True,
        min_savings_pct=10,
    )
    # 80000 高于阈值 75000、低于 ceiling 90000:在非紧急区,anti-thrash 应生效
    comp.update_from_response(SimpleNamespace(input_tokens=80000, output_tokens=10, details={}))
    # 模拟两次无效压缩(节省 < 10%)
    comp._ineffective_compression_count = 2  # noqa: SLF001
    assert comp.should_compress() is False


def test_anti_thrash_self_heals_when_real_usage_breaches_ceiling():
    """P2:真实用量突破 hard_ceiling 时,anti-thrash 锁定应被强制解除以避免轮内紧急压缩。"""
    comp = ContextCompressor(
        context_length=100000,
        threshold_percent=0.75,
        hard_ceiling_percent=0.90,
        anti_thrash=True,
        min_savings_pct=10,
    )
    # 95000 已突破 ceiling 90000:紧急区,即便 ineffective 计数 ≥ 2 也必须重试
    comp.update_from_response(SimpleNamespace(input_tokens=95000, output_tokens=10, details={}))
    comp._ineffective_compression_count = 2  # noqa: SLF001
    assert comp.should_compress() is True
    # 自愈后计数应清零,避免下一轮又锁住
    assert comp._ineffective_compression_count == 0  # noqa: SLF001


# ── 兜底摘要 ───────────────────────────────────────────────────────────────
def test_fallback_summary_used_when_no_summarizer():
    comp = ContextCompressor(context_length=8000, threshold_percent=0.75, protect_first_n=2, protect_last_n=3, tail_token_budget=500)
    msgs = _make_tool_turns(10)
    result = comp.compress(msgs, current_tokens=estimate_messages_tokens(msgs))
    assert comp._last_summary_fallback_used is True  # noqa: SLF001
    compressed = result.runtime_view
    # 摘要应出现在某条 user 消息中
    all_text = "\n".join(str(p.content) for m in compressed for p in getattr(m, "parts", []) if hasattr(p, "content"))
    assert "历史任务快照" in all_text


def test_abort_on_summary_failure_returns_unchanged():
    def failing_summarizer(turns, focus, prev):
        raise RuntimeError("boom")

    comp = ContextCompressor(
        context_length=8000,
        threshold_percent=0.75,
        protect_first_n=2,
        protect_last_n=3,
        tail_token_budget=500,
        abort_on_summary_failure=True,
        summarizer=failing_summarizer,
    )
    msgs = _make_tool_turns(10)
    result = comp.compress(msgs, current_tokens=estimate_messages_tokens(msgs))
    assert comp._last_compress_aborted is True  # noqa: SLF001
    assert result.dropped is False  # 中止时返回不压缩结果
    assert result.head is msgs  # head 指向原始列表


# ── 迭代摘要 / LLM 摘要器 ─────────────────────────────────────────────────
def test_summarizer_called_and_previous_summary_reused():
    calls: list = []

    def summarizer(turns, focus, prev):
        calls.append({"focus": focus, "prev": prev})
        return "## 历史任务快照\n第一次摘要内容"

    comp = ContextCompressor(
        context_length=8000,
        threshold_percent=0.75,
        protect_first_n=2,
        protect_last_n=3,
        tail_token_budget=500,
        summarizer=summarizer,
    )
    msgs = _make_tool_turns(10)
    comp.compress(msgs, current_tokens=estimate_messages_tokens(msgs))
    assert len(calls) == 1
    assert calls[0]["prev"] is None

    # 第二次压缩应传入上一次摘要作为 previous_summary
    msgs2 = _make_tool_turns(10)
    comp.compress(msgs2, current_tokens=estimate_messages_tokens(msgs2))
    assert len(calls) == 2
    assert calls[1]["prev"] == "## 历史任务快照\n第一次摘要内容"


def test_focus_topic_passed_to_summarizer():
    received_focus: list = []

    def summarizer(turns, focus, prev):
        received_focus.append(focus)
        return "## 历史任务快照\n焦点摘要"

    comp = ContextCompressor(
        context_length=8000,
        threshold_percent=0.75,
        protect_first_n=2,
        protect_last_n=3,
        tail_token_budget=500,
        summarizer=summarizer,
    )
    msgs = _make_tool_turns(10)
    comp.compress(msgs, current_tokens=estimate_messages_tokens(msgs), focus_topic="认证模块")
    assert received_focus == ["认证模块"]


def test_summarizer_failure_falls_back_and_cools_down():
    call_count = [0]

    def summarizer(turns, focus, prev):
        call_count[0] += 1
        raise RuntimeError("LLM down")

    comp = ContextCompressor(
        context_length=8000,
        threshold_percent=0.75,
        protect_first_n=2,
        protect_last_n=3,
        tail_token_budget=500,
        summarizer=summarizer,
    )
    msgs = _make_tool_turns(10)
    result = comp.compress(msgs, current_tokens=estimate_messages_tokens(msgs))
    assert comp._last_summary_fallback_used is True  # noqa: SLF001
    assert comp._last_summary_error is not None  # noqa: SLF001
    assert result.dropped
    compressed = result.runtime_view
    assert len(compressed) < len(msgs)  # 仍用兜底完成压缩


# ── allow_llm_summary 开关(预检模式)─────────────────────────────────────
def test_allow_llm_summary_false_skips_summarizer():
    calls = [0]

    def summarizer(turns, focus, prev):
        calls[0] += 1
        return "should not be called"

    comp = ContextCompressor(
        context_length=8000,
        threshold_percent=0.75,
        protect_first_n=2,
        protect_last_n=3,
        tail_token_budget=500,
        summarizer=summarizer,
    )
    msgs = _make_tool_turns(10)
    comp.compress(msgs, current_tokens=estimate_messages_tokens(msgs), allow_llm_summary=False)
    assert calls[0] == 0  # 预检模式不调 LLM
    assert comp._last_summary_fallback_used is True  # noqa: SLF001


# ── 会话重置 ───────────────────────────────────────────────────────────────
def test_on_session_reset_clears_state():
    comp = ContextCompressor(context_length=10000)
    comp.compression_count = 5
    comp._previous_summary = "old"  # noqa: SLF001
    comp.last_prompt_tokens = 9999
    comp.on_session_reset()
    assert comp.compression_count == 0
    assert comp._previous_summary is None  # noqa: SLF001
    assert comp.last_prompt_tokens == 0
