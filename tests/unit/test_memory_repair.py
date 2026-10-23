# pyrefly: ignore [missing-import]
"""旧数据修复:被反复减半的置信度、旧记忆类型名。"""

from datetime import datetime, timedelta, timezone

from openhachimi_agent.memory.migration import run_legacy_repair
from openhachimi_agent.memory.models import MemoryAtom, MemoryScope, MemoryStability
from openhachimi_agent.memory.store import MemoryStore


def _rows(store):
    with store.connect() as conn:
        return {row["content"]: (row["memory_type"], row["confidence"]) for row in conn.execute("SELECT content, memory_type, confidence FROM memory_atoms")}


def _seed(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope()
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    halved = MemoryAtom(memory_type="user_preference", content="被减半的偏好", scope=scope, confidence=0.05, stability=MemoryStability.STABLE)
    low_but_recent = MemoryAtom(memory_type="project_fact", content="本来就低的事实", scope=scope, confidence=0.3, stability=MemoryStability.STABLE)
    legacy = MemoryAtom(memory_type="fact", content="旧类型名的记忆", scope=scope, confidence=0.9)
    for atom in (halved, low_but_recent, legacy):
        store.add_atom(atom)
    with store.connect() as conn:
        conn.execute("UPDATE memory_atoms SET last_accessed_at = ? WHERE id = ?", (old, halved.id))
        conn.execute("UPDATE memory_atoms SET last_accessed_at = ? WHERE id = ?", (recent, low_but_recent.id))
    return store


def test_dry_run_reports_without_changing(tmp_path):
    store = _seed(tmp_path)
    before = _rows(store)

    counts = run_legacy_repair(store.db_path, dry_run=True)

    assert counts == {"halved_confidence": 1, "legacy_types": 1}
    assert _rows(store) == before


def test_apply_restores_only_matching_rows(tmp_path):
    store = _seed(tmp_path)

    run_legacy_repair(store.db_path, dry_run=False)

    rows = _rows(store)
    assert rows["被减半的偏好"] == ("user_preference", 0.6)
    assert rows["本来就低的事实"] == ("project_fact", 0.3)
    assert rows["旧类型名的记忆"] == ("user_trait", 0.9)
