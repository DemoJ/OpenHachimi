from openhachimi_agent.agent.intent import build_task_frame, classify_intent_heuristic, coerce_intent_decision, coerce_task_frame


def test_heuristic_is_conservative_fallback():
    """Heuristic 兜底不做精确分类，只做风险识别。"""
    decision = classify_intent_heuristic("帮我重构这个项目的执行模块并补测试")

    # 兜底模式不精确分类 task_kind
    assert decision.task_kind == "unknown"
    # 兜底默认 simple + 不需要规划，让 Executor 自主决策
    assert decision.requires_plan is False


def test_heuristic_high_risk_requires_confirmation():
    decision = classify_intent_heuristic("删除旧目录并部署到线上")

    assert decision.risk == "high"
    assert decision.requires_user_confirmation is True


def test_heuristic_extracts_urls():
    """Heuristic 仍然能提取 URL 供 TaskFrame 使用。"""
    decision = classify_intent_heuristic("请访问 https://example.com/a 看一下")

    assert decision.target_urls == ["https://example.com/a"]
    assert decision.must_preserve_targets is True


def test_coerce_task_frame_supplements_url_entities():
    """coerce_task_frame 只补充 target_entities，不覆盖 LLM 判断。"""
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

    # LLM 判断的字段不被覆盖
    assert frame.task_kind == "research"
    assert frame.complexity == "complex"
    assert frame.requires_plan is True
    # 但 URL 仍然被补充到 target_entities 中
    assert frame.target_urls == ["https://example.com/a"]


def test_coerce_task_frame_does_not_duplicate_existing_urls():
    """如果 LLM 已经提供了 URL entity，不重复添加。"""
    frame = coerce_task_frame(
        {
            "user_request": "请打开 https://example.com/a",
            "goal": "打开页面",
            "task_kind": "browser",
            "complexity": "simple",
            "risk": "low",
            "confidence": 0.9,
            "requires_plan": False,
            "target_entities": [
                {"type": "url", "value": "https://example.com/a", "role": "primary", "immutable": True}
            ],
            "invariants": [],
        },
        "请打开 https://example.com/a",
    )

    assert len(frame.target_entities) == 1
    assert frame.target_urls == ["https://example.com/a"]


def test_legacy_router_result_is_coerced():
    decision = coerce_intent_decision("COMPLEX_TASK", "分析这个仓库")

    assert decision.complexity == "complex"
    assert decision.requires_plan is True


def test_build_task_frame_no_url_special_handling():
    """所有任务一视同仁，不对含 URL 的任务做特殊 autonomy 设置。"""
    frame = build_task_frame("请访问 https://example.com/a 看一下")

    # 不再强制 narrow，由 LLM 判断
    assert frame.allowed_autonomy in ("bounded", "broad")
    assert frame.target_urls == ["https://example.com/a"]
