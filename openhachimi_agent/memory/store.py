"""长期记忆 SQLite 存储门面。

具体职责拆分在同目录的 store_* 模块中，MemoryStore 保持原有公共 API。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from openhachimi_agent.memory._store import utils as store_utils
from openhachimi_agent.memory._store.atoms import AtomStoreMixin
from openhachimi_agent.memory._store.blocks import BlockStoreMixin
from openhachimi_agent.memory._store.jobs import JobQueueStoreMixin
from openhachimi_agent.memory._store.lifecycle import LifecycleStoreMixin
from openhachimi_agent.memory._store.profiles import ProfileStoreMixin
from openhachimi_agent.memory._store.schema import SCHEMA_VERSION as _SCHEMA_VERSION
from openhachimi_agent.memory._store.schema import SchemaStoreMixin
from openhachimi_agent.memory._store.search import SearchStoreMixin
from openhachimi_agent.memory._store.turns import TurnStoreMixin
from openhachimi_agent.memory._store.vectors import VectorStoreMixin
from openhachimi_agent.memory.vector_index import SQLiteVecIndex, SQLiteVectorShardIndex

SCHEMA_VERSION = _SCHEMA_VERSION
_load_json_array = store_utils._load_json_array


class MemoryStore(
    SchemaStoreMixin,
    TurnStoreMixin,
    AtomStoreMixin,
    BlockStoreMixin,
    ProfileStoreMixin,
    JobQueueStoreMixin,
    VectorStoreMixin,
    SearchStoreMixin,
    LifecycleStoreMixin,
):
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_index = SQLiteVectorShardIndex()
        self.sqlite_vec_index = SQLiteVecIndex()
        self._local = threading.local()
        self.initialize()

    @contextmanager
    def connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "MemoryStore",
    "SCHEMA_VERSION",
    "_load_json_array",
]
