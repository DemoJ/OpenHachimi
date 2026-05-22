import json

from openhachimi_agent.memory.migration import migrate_legacy_histories
from openhachimi_agent.memory.store import MemoryStore


def test_migrate_legacy_histories_imports_turns(tmp_path):
    memory_dir = tmp_path / ".memory"
    role_dir = memory_dir / "default"
    role_dir.mkdir(parents=True)
    history_path = role_dir / "s1.json"
    history_path.write_text(
        json.dumps(
            [
                {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "记住我喜欢中文"}]},
                {"kind": "response", "parts": [{"part_kind": "text", "content": "好的"}], "model_name": "test", "timestamp": "2026-01-01T00:00:00Z"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = MemoryStore(tmp_path / "memory.sqlite3")

    result = migrate_legacy_histories(store, memory_dir)

    assert result == {"scanned": 1, "imported": 1, "failed": 0}
    assert store.stats()["turns"] == 1


def test_migrate_legacy_histories_is_idempotent(tmp_path):
    memory_dir = tmp_path / ".memory"
    role_dir = memory_dir / "default"
    role_dir.mkdir(parents=True)
    (role_dir / "s1.json").write_text(
        json.dumps(
            [
                {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "hello"}]},
                {"kind": "response", "parts": [{"part_kind": "text", "content": "hi"}], "model_name": "test", "timestamp": "2026-01-01T00:00:00Z"},
            ]
        ),
        encoding="utf-8",
    )
    store = MemoryStore(tmp_path / "memory.sqlite3")

    first = migrate_legacy_histories(store, memory_dir)
    second = migrate_legacy_histories(store, memory_dir)

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert store.stats()["turns"] == 1
