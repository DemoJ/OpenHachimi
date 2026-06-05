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


def test_unparseable_router_output_does_not_force_plan_for_low_risk_task():
    decision = coerce_intent_decision(object(), "帮我生成一个简单示例文件")

    assert decision.confidence < 0.5
    assert decision.requires_plan is False
    assert decision.execution_mode == "direct"


def test_relevant_skill_sets_skill_direct_execution_mode():
    frame = coerce_task_frame(
        {
            "user_request": "用 demo skill 处理",
            "goal": "用 demo skill 处理",
            "task_kind": "file_ops",
            "complexity": "simple",
            "risk": "low",
            "confidence": 0.8,
            "requires_plan": False,
            "relevant_skills": ["demo"],
            "target_entities": [],
            "invariants": [],
        },
        "用 demo skill 处理",
    )

    assert frame.execution_mode == "skill_direct"


def test_build_task_frame_no_url_special_handling():
    """所有任务一视同仁，不对含 URL 的任务做特殊 autonomy 设置。"""
    frame = build_task_frame("请访问 https://example.com/a 看一下")

    # 不再强制 narrow，由 LLM 判断
    assert frame.allowed_autonomy in ("bounded", "broad")
    assert frame.target_urls == ["https://example.com/a"]


def test_coerce_task_frame_adds_install_skill_invariant_for_skill_update_url():
    url = "https://github.com/DemoJ/product-manager-suite"
    frame = coerce_task_frame(
        {
            "user_request": f"请更新我本地已安装的 product-manager-suite skill 到最新版本，仓库地址是：{url}",
            "goal": "更新 product-manager-suite skill",
            "task_kind": "unknown",
            "complexity": "simple",
            "risk": "low",
            "confidence": 0.8,
            "requires_plan": False,
            "target_entities": [],
            "invariants": [],
        },
        f"请更新我本地已安装的 product-manager-suite skill 到最新版本，仓库地址是：{url}",
    )

    invariant_text = "\n".join(frame.invariants)
    assert frame.target_urls == [url]
    assert "install_skill" in invariant_text
    assert url in invariant_text
    assert "command-based update flow" in invariant_text


def test_coerce_task_frame_does_not_add_skill_invariant_for_regular_npx_command():
    frame = coerce_task_frame(
        {
            "user_request": "用 npx vite build 打包项目",
            "goal": "打包项目",
            "task_kind": "shell",
            "complexity": "simple",
            "risk": "low",
            "confidence": 0.8,
            "requires_plan": False,
            "target_entities": [],
            "invariants": [],
        },
        "用 npx vite build 打包项目",
    )

    assert not any("install_skill" in invariant for invariant in frame.invariants)
