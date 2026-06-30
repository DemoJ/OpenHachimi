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

