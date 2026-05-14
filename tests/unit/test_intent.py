from openhachimi_agent.agent.intent import build_task_frame, classify_intent_heuristic, coerce_intent_decision, coerce_task_frame


def test_heuristic_code_change_requires_plan():
    decision = classify_intent_heuristic("帮我重构这个项目的执行模块并补测试")

    assert decision.task_kind == "code_change"
    assert decision.complexity == "complex"
    assert decision.requires_plan is True


def test_heuristic_high_risk_requires_confirmation_and_plan():
    decision = classify_intent_heuristic("删除旧目录并部署到线上")

    assert decision.risk == "high"
    assert decision.requires_plan is True
    assert decision.requires_user_confirmation is True


def test_simple_explicit_url_visit_does_not_require_plan():
    decision = classify_intent_heuristic("请访问 https://example.com/a 看一下")

    assert decision.task_kind == "browser"
    assert decision.complexity == "simple"
    assert decision.requires_plan is False
    assert decision.target_urls == ["https://example.com/a"]
    assert decision.must_preserve_targets is True


def test_router_complex_output_does_not_override_simple_targeted_url_visit():
    decision = coerce_intent_decision("COMPLEX_TASK", "请打开 https://example.com/a")

    assert decision.task_kind == "browser"
    assert decision.complexity == "simple"
    assert decision.requires_plan is False
    assert decision.target_urls == ["https://example.com/a"]


def test_task_frame_preserves_explicit_url_as_invariant():
    frame = build_task_frame("请访问 https://example.com/a 看一下")

    assert frame.task_kind == "browser"
    assert frame.requires_plan is False
    assert frame.allowed_autonomy == "narrow"
    assert frame.target_urls == ["https://example.com/a"]
    assert any("https://example.com/a" in invariant for invariant in frame.invariants)


def test_coerce_task_frame_repairs_router_that_overplans_simple_url():
    frame = coerce_task_frame(
        {
            "user_request": "请打开 https://example.com/a",
            "goal": "搜索 example 相关页面",
            "task_kind": "research",
            "complexity": "complex",
            "risk": "low",
            "confidence": 0.9,
            "requires_plan": True,
            "target_entities": [],
            "invariants": [],
        },
        "请打开 https://example.com/a",
    )

    assert frame.task_kind == "browser"
    assert frame.complexity == "simple"
    assert frame.requires_plan is False
    assert frame.target_urls == ["https://example.com/a"]


def test_legacy_router_result_is_coerced():
    decision = coerce_intent_decision("COMPLEX_TASK", "分析这个仓库")

    assert decision.complexity == "complex"
    assert decision.requires_plan is True
