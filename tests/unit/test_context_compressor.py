"""上下文压缩引擎单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage, RunUsage

from openhachimi_agent.context import token_estimate
from openhachimi_agent.context.compressor import ContextCompressor
from openhachimi_agent.context.pruning import prune_old_tool_results
from openhachimi_agent.context.token_estimate import estimate_messages_tokens


@pytest.fixture(autouse=True)
def _offline_token_estimate(monkeypatch):
    """压缩行为测试使用本地字符兜底,不因 tiktoken 首次加载下载 encoding。"""
    monkeypatch.setattr(
        token_estimate,
        "_active_encoding",
        Mock(side_effect=token_estimate._EncodingLoadError("offline compression test")),
    )


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


# ── 单次请求用量:不可把 RunUsage 成本累计当成上下文占用 ──────────────────────
async def _save_turn_usage(comp, new_history, *, history, result):
    from openhachimi_agent.service.agent_runtime.turn_postprocess import _save_turn_messages

    save_messages = Mock()
    service = SimpleNamespace(session_store=SimpleNamespace(save_messages=save_messages))
    await _save_turn_messages(
        service,
        SimpleNamespace(context_compressor=comp),
        result,
        new_history,
        role="default",
        actual_session_id="test-context-usage",
        latest_scope="test-scope",
        resolved_channel_code="webui",
        history=history,
        update_pointer=False,
    )
    return save_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("callable_run_usage", [False, True])
async def test_save_turn_uses_latest_request_not_cumulative_13_response_usage(callable_run_usage):
    inputs = [17562, 19425, 19733, 44059, 44506, 61799, 63197,
              64392, 93121, 93868, 94877, 98796, 98948]
    outputs = [249, 135, 124, 294, 179, 253, 106, 220, 667, 77, 3765, 77, 2734]
    responses = [
        ModelResponse(
            parts=[TextPart(content=f"step {i}")],
            usage=RequestUsage(input_tokens=tokens, output_tokens=outputs[i]),
        )
        for i, tokens in enumerate(inputs)
    ]
    cumulative = RunUsage()
    for response in responses:
        cumulative.incr(response.usage)
    assert len(responses) == 13
    assert cumulative.input_tokens == 814283
    assert cumulative.output_tokens == 8880
    assert max(inputs) == inputs[-1] == 98948

    usage_accessor = Mock(return_value=cumulative)
    result = SimpleNamespace(usage=usage_accessor if callable_run_usage else cumulative)
    history = [
        ModelRequest(parts=[UserPromptPart(content="old turn")]),
        ModelResponse(parts=[TextPart(content="old answer")], usage=RequestUsage(input_tokens=950000)),
    ]
    new_history = [*history, ModelRequest(parts=[UserPromptPart(content="current turn")]), *responses]
    comp = ContextCompressor(context_length=1000000, threshold_percent=0.75)

    save_messages = await _save_turn_usage(comp, new_history, history=history, result=result)

    assert comp.last_prompt_tokens == 98948
    assert comp.last_completion_tokens == 2734
    assert comp.last_total_tokens == 101682
    assert comp.should_compress() is False
    # 累计成本及响应原始用量仍用于计费/展示,不被压缩器改写。
    assert cumulative.input_tokens == 814283
    assert cumulative.output_tokens == 8880
    assert sum(response.usage.input_tokens for response in responses) == 814283
    assert sum(response.usage.output_tokens for response in responses) == 8880
    usage_accessor.assert_not_called()
    save_messages.assert_called_once_with(
        "default", "test-context-usage", new_history[len(history):],
        scope="test-scope", channel="webui", scope_key="test-scope", append=True, update_pointer=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_tokens, latest_tokens", [(950000, 98948), (98948, 750000)])
@pytest.mark.parametrize("tool_response", [False, True])
async def test_save_turn_uses_latest_response_even_with_trailing_tool_result(previous_tokens, latest_tokens, tool_response):
    history = [ModelResponse(parts=[TextPart(content="old turn")], usage=RequestUsage(input_tokens=999999))]
    parts = [ToolCallPart(tool_name="read_file", args={}, tool_call_id="latest")] if tool_response else [TextPart(content="latest")]
    new_history = [
        *history,
        ModelResponse(parts=[TextPart(content="earlier step")], usage=RequestUsage(input_tokens=previous_tokens)),
        ModelResponse(parts=parts, usage=RequestUsage(input_tokens=latest_tokens, output_tokens=10)),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="ok", tool_call_id="latest")]),
    ]
    comp = ContextCompressor(context_length=1000000)
    result = SimpleNamespace(usage=Mock(return_value=RunUsage(input_tokens=2000000)))

    await _save_turn_usage(comp, new_history, history=history, result=result)

    assert comp.last_prompt_tokens == latest_tokens
    assert comp.last_completion_tokens == 10
    assert comp.should_compress() is (latest_tokens >= 750000)
    result.usage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_usage", ["no_messages", "no_response", "empty", "none", "missing_input", "invalid"])
async def test_save_turn_does_not_fall_back_to_old_or_cumulative_usage(missing_usage):
    comp = ContextCompressor(context_length=1000000)
    comp.update_from_response(RequestUsage(input_tokens=950000, output_tokens=100))
    history = [ModelResponse(parts=[TextPart(content="old turn")], usage=RequestUsage(input_tokens=950000))]
    if missing_usage == "no_messages":
        new_history = list(history)
    elif missing_usage == "no_response":
        new_history = [*history, ModelRequest(parts=[UserPromptPart(content="current turn")])]
    else:
        latest = ModelResponse(parts=[TextPart(content="latest has no valid input usage")])
        if missing_usage == "none":
            latest.usage = None
        elif missing_usage == "missing_input":
            latest.usage = SimpleNamespace(details={"input_tokens": 950000})
        elif missing_usage == "invalid":
            latest.usage = SimpleNamespace(input_tokens="invalid", output_tokens=100)
        new_history = [
            *history,
            ModelResponse(parts=[TextPart(content="earlier current-turn step")], usage=RequestUsage(input_tokens=950000)),
            latest,
        ]
    result = SimpleNamespace(usage=Mock(return_value=RunUsage(input_tokens=2000000)))

    await _save_turn_usage(comp, new_history, history=history, result=result)

    assert comp.last_prompt_tokens == 0
    assert comp.last_completion_tokens == 0
    assert comp.last_total_tokens == 0
    assert comp.should_compress() is False
    result.usage.assert_not_called()


@pytest.mark.parametrize("prompt_tokens", [749999, 750000, 750001, 899999, 900000, 900001])
def test_context_cutoffs_use_normalized_input_including_cache_once(prompt_tokens):
    comp = ContextCompressor(context_length=1000000, threshold_percent=0.75, hard_ceiling_percent=0.90)
    comp.update_from_response(RequestUsage(
        input_tokens=prompt_tokens,
        output_tokens=2734,
        cache_read_tokens=prompt_tokens - 101,
        cache_write_tokens=100,
        details={"input_tokens": 1},
    ))

    assert comp.last_prompt_tokens == prompt_tokens
    assert comp.last_completion_tokens == 2734
    assert comp.last_total_tokens == prompt_tokens + 2734
    assert comp.should_compress() is (prompt_tokens >= 750000)
    assert comp.should_compress_preflight([]) is (prompt_tokens >= 900000)


@pytest.mark.parametrize("normalized_input, detail_input", [(800000, 1), (100, 950000), (0, 950000)])
def test_update_from_response_never_overrides_normalized_input_with_details(normalized_input, detail_input):
    comp = ContextCompressor(context_length=1000000)
    comp.update_from_response(SimpleNamespace(
        input_tokens=normalized_input,
        output_tokens=10,
        cache_read_tokens=500000,
        cache_write_tokens=200000,
        details={"input_tokens": detail_input},
    ))

    assert comp.last_prompt_tokens == normalized_input
    assert comp.last_total_tokens == normalized_input + 10


@pytest.mark.parametrize("usage", [None, RequestUsage(), SimpleNamespace(details={"input_tokens": 950000})])
def test_update_from_response_missing_usage_clears_old_measurement(usage):
    comp = ContextCompressor(context_length=1000000)
    comp.update_from_response(RequestUsage(input_tokens=950000, output_tokens=100))

    comp.update_from_response(usage)

    assert comp.last_prompt_tokens == 0
    assert comp.last_completion_tokens == 0
    assert comp.last_total_tokens == 0


def test_update_from_response_keeps_callable_compatibility():
    comp = ContextCompressor()
    comp.update_from_response(lambda: RequestUsage(input_tokens=100, output_tokens=10, details={"input_tokens": 1}))
    assert comp.last_prompt_tokens == 100
    assert comp.last_completion_tokens == 10
    assert comp.last_total_tokens == 110


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


# ── 压缩后旧请求用量失效,无实际压缩则保留 ─────────────────────────────────
@pytest.mark.parametrize("allow_llm_summary", [False, True])
def test_compress_invalidates_usage_until_next_response(allow_llm_summary):
    comp = ContextCompressor(
        context_length=1000000,
        protect_first_n=2,
        protect_last_n=3,
        tail_token_budget=500,
        summarizer=lambda turns, focus, previous: "compressed history",
    )
    msgs = _make_tool_turns(10)
    usage = RequestUsage(input_tokens=950000, output_tokens=2734)
    comp.update_from_response(usage)
    assert comp.should_compress() is True
    assert comp.should_compress_preflight(msgs) is True

    result = comp.compress(msgs, allow_llm_summary=allow_llm_summary)

    assert result.dropped is True
    assert comp.last_prompt_tokens == 0
    assert comp.last_completion_tokens == 0
    assert comp.last_total_tokens == 0
    assert comp.should_compress() is False
    assert comp.should_compress_preflight(result.runtime_view) is False
    assert usage.input_tokens == 950000 and usage.output_tokens == 2734
    # 新响应到达后恢复真实用量判定,不永久禁用压缩。
    comp.update_from_response(RequestUsage(input_tokens=900000, output_tokens=10))
    assert comp.should_compress() is True
    assert comp.should_compress_preflight(result.runtime_view) is True


@pytest.mark.parametrize("reason", ["short_history", "no_window", "summary_aborted"])
def test_compress_without_dropped_content_keeps_usage(reason):
    comp = ContextCompressor(
        context_length=1000000,
        protect_first_n=2,
        protect_last_n=3,
        tail_token_budget=1000000 if reason == "no_window" else 500,
        abort_on_summary_failure=reason == "summary_aborted",
        summarizer=lambda turns, focus, previous: None,
    )
    msgs = _make_tool_turns(0 if reason == "short_history" else 10)
    comp.update_from_response(RequestUsage(input_tokens=950000, output_tokens=2734))

    result = comp.compress(msgs)

    assert result.dropped is False
    assert comp.last_prompt_tokens == 950000
    assert comp.last_completion_tokens == 2734
    assert comp.last_total_tokens == 952734
    assert comp.should_compress_preflight(msgs) is True


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


def test_compression_savings_compare_the_same_estimator_scale():
    comp = ContextCompressor(
        context_length=1000000, protect_first_n=2, protect_last_n=3, tail_token_budget=500,
    )
    messages = _make_tool_turns(10)
    before = estimate_messages_tokens(messages)
    result = comp.compress(messages, current_tokens=814283, allow_llm_summary=False)
    assert result.dropped
    after = estimate_messages_tokens(result.runtime_view)
    assert comp._last_compression_savings_pct == pytest.approx((before - after) / before * 100)


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
