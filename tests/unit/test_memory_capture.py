from openhachimi_agent.memory.capture import capture_turn_memories
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.recall import get_memory_store, recall_memories


def test_capture_turn_writes_l0_and_extracts_explicit_preference(mock_config):
    scope = MemoryScope(role_name="default", session_id="s1")

    turn_id = capture_turn_memories(mock_config, scope, "remember: prefer concise Chinese answers", "ok")
    context = recall_memories(mock_config, scope, "Chinese answers")

    assert turn_id
    assert context.results
    assert any("Chinese" in item.content for item in context.results)


def _count_extract_jobs(mock_config) -> int:
    """统计 L1 抽取队列里 extract_atoms_from_turn job 条数。"""
    store = get_memory_store(mock_config)
    with store.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_jobs WHERE job_type = 'extract_atoms_from_turn'"
        ).fetchone()[0]


def test_capture_skips_l1_extraction_for_scheduled_source(mock_config):
    """定时任务执行的 turn 只留 L0,不进 L1 抽取。

    user_message 命中 _looks_memorable(含"记住/以后")但不命中
    _looks_like_scheduler_payload(无系统前缀),用以验证挡住它的是 source 判断
    而非 payload 形态检测。
    """
    scope = MemoryScope(role_name="default", session_id="s1")
    memorable_prompt = "请记住：以后回答一律使用中文。"

    turn_id = capture_turn_memories(
        mock_config, scope, memorable_prompt, "已收到", source="scheduled"
    )
    store = get_memory_store(mock_config)

    assert turn_id  # L0 turn 仍写入
    assert store.stats()["turns"] == 1
    assert _count_extract_jobs(mock_config) == 0  # 不进 L1 抽取队列
    assert store.stats()["atoms"] == 0  # 也不走规则抽取写 atom


def test_capture_skips_l1_extraction_for_system_source(mock_config):
    """system 下发的 turn 同样只留 L0,不进 L1。"""
    scope = MemoryScope(role_name="default", session_id="s1")
    memorable_prompt = "请记住：以后回答保持简洁。"

    turn_id = capture_turn_memories(
        mock_config, scope, memorable_prompt, "已收到", source="system"
    )
    store = get_memory_store(mock_config)

    assert turn_id
    assert store.stats()["turns"] == 1
    assert _count_extract_jobs(mock_config) == 0
    assert store.stats()["atoms"] == 0


def test_capture_enqueues_llm_extraction_for_plain_chitchat(mock_config):
    """普通对话(提问/命令/寒暄)也进 LLM 抽取队列,由 LLM 判断是否为可记忆内容。

    移除关键词过滤闸门后,所有用户消息都进 LLM 抽取。LLM 比规则更能理解语义,
    避免"记住不要停"这类任务指令被误判,也避免"帮我研究一下"这类隐性任务被漏掉。
    """
    scope = MemoryScope(role_name="default", session_id="s1")

    for user_msg in (
        "帮我看看这个报错是什么原因造成的呢？能帮我分析一下吗",       # 提问
        "今天天气不错我们出去走走散散步吧，你觉得怎么样",          # 寒暄
        "运行一下这个脚本看看输出的结果如何，有没有什么问题",        # 命令噪声
    ):
        capture_turn_memories(mock_config, scope, user_msg, "ok")

    store = get_memory_store(mock_config)
    assert store.stats()["turns"] == 3  # L0 仍写入
    assert _count_extract_jobs(mock_config) == 3  # 所有用户消息都进 LLM 抽取队列
    # 规则抽取(_looks_memorable)可能为部分消息写 atom,取决于是否命中关键词
    # 这里不强制断言 atoms 数量,因为规则抽取只是兜底,LLM 才是主抽取器


def test_capture_enqueues_llm_extraction_for_question_with_intent_word(mock_config):
    """提问句式即使含正向意图词("以后")也进 LLM 抽取——由 LLM 判断是否为记忆意图。"""
    scope = MemoryScope(role_name="default", session_id="s1")

    capture_turn_memories(mock_config, scope, "我以后到底应该怎么配置这个参数比较好？能不能给我一些建议和指导", "ok")

    store = get_memory_store(mock_config)
    assert store.stats()["turns"] == 1
    assert _count_extract_jobs(mock_config) == 1  # 进 LLM 抽取队列


def test_capture_enqueues_llm_extraction_for_explicit_preference(mock_config):
    """显式偏好陈述过闸,进入 LLM 抽取队列。"""
    scope = MemoryScope(role_name="default", session_id="s1")

    capture_turn_memories(mock_config, scope, "请你记住,以后所有回答一律使用中文并且保持简洁。", "ok")

    store = get_memory_store(mock_config)
    assert store.stats()["turns"] == 1
    assert _count_extract_jobs(mock_config) == 1  # 过闸,入队 LLM 抽取



def _atom_contents(mock_config) -> list[str]:
    store = get_memory_store(mock_config)
    with store.connect() as conn:
        return [row[0] for row in conn.execute("SELECT content FROM memory_atoms ORDER BY created_at")]


def test_capture_does_not_store_plain_task_request_as_atom(mock_config):
    """普通任务指令(含"项目 / 不要"等词)不在同步路径写 atom,只交给后台 LLM 抽取。"""
    scope = MemoryScope(role_name="default", session_id="s1")

    capture_turn_memories(mock_config, scope, "帮我看下这个项目的代码，先不要改动任何文件", "ok")

    assert _atom_contents(mock_config) == []
    assert _count_extract_jobs(mock_config) == 1


def test_capture_stores_explicit_request_as_clauses(mock_config):
    """明确要求记住时,按子句存,不存整句原话,也不存单独的"请你记住"。"""
    scope = MemoryScope(role_name="default", session_id="s1")

    capture_turn_memories(mock_config, scope, "好的，请你记住，以后回答一律使用中文，不要用表情符号", "ok")

    contents = _atom_contents(mock_config)
    assert "以后回答一律使用中文" in contents
    assert "不要用表情符号" in contents
    assert not any("记住" in content for content in contents)


def test_capture_skips_task_constraint_with_remember(mock_config):
    """"记住不要……"是本次任务的执行约束,不当作长期记忆同步写入。"""
    scope = MemoryScope(role_name="default", session_id="s1")

    capture_turn_memories(mock_config, scope, "深挖 lxh.io 这个网站，记住不要在信息没挖干净前主动停止", "ok")

    assert _atom_contents(mock_config) == []


def test_capture_passes_recent_turns_to_extraction_job(mock_config):
    """后台抽取任务带上同会话前几轮,让 LLM 能理解"就按这个来"这类回复。"""
    import json

    scope = MemoryScope(role_name="default", session_id="s1")
    capture_turn_memories(mock_config, scope, "我们的项目要不要换成 PostgreSQL 数据库呢", "建议换")
    capture_turn_memories(mock_config, scope, "好的，那就按你说的换成 PostgreSQL 吧", "好的")

    store = get_memory_store(mock_config)
    with store.connect() as conn:
        payloads = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM memory_jobs WHERE job_type = 'extract_atoms_from_turn' ORDER BY created_at"
        )]
    assert payloads[0]["recent_turns"] == []
    assert payloads[1]["recent_turns"][0]["user_message"].startswith("我们的项目要不要换成")


def test_capture_skips_explicit_write_when_model_already_remembered(mock_config):
    """模型本轮已调 remember 时,不再对同一句"记住……"同步写一条(真实对话里出现过两条近似重复)。"""
    scope = MemoryScope(role_name="default", session_id="s1")

    capture_turn_memories(mock_config, scope, "请记住：以后回答我都用中文，并且尽量简洁。", "好的", skip_explicit=True)

    assert _atom_contents(mock_config) == []
    # 后台 LLM 抽取照常排队,重复由写入时的 LLM 决策处理
    assert _count_extract_jobs(mock_config) == 1
