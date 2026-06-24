"""会话相关状态的 SQLite 持久化层。

替代 v3 之前的 JSON 文件方案(``storage/memory.py`` + ``storage/session_meta.py`` +
``.memory/{role}/{session_id}.json`` / ``.meta.json`` / ``latest`` / ``latest_by_scope``
+ ``.memory/todos/{session_id}.json``)。所有这些状态现在塞进单个 SQLite 库
``{memory_dir}/sessions.sqlite3``,WAL 模式,与 ``memory/store.py`` 长期记忆库
和 ``scheduler/store.py`` 调度任务库并排。

设计要点
========
- **消息按条存**(``session_messages`` 一行一条 ModelMessage,``turn_index``
  单调递增),不再"每轮全量重写 1.7MB 整段历史"。``save_messages`` 内部还是
  DELETE+INSERT 全段重写,但磁盘 IO 由 SQLite WAL 管,不再走文件 fsync。
- **渠道元数据**(原 sidecar)直接入 ``sessions`` 表的 ``channel`` /
  ``scope_key`` / ``channel_raw`` 列,首写定终身语义靠 ``INSERT OR IGNORE``
  保证。
- **最新指针**(原 ``latest`` / ``latest_by_scope/{sha}`` 纯文本文件)迁到
  ``session_pointers`` 表,scope 列用 ``""`` 代表 unscoped 全局指针。
- **TODO 状态**(原 ``{memory_dir}/todos/{sid}.json``)迁到 ``session_todos``
  表的 JSON 列。表主键只用 ``session_id`` —— ``planning.py`` 工具上下文里没有
  role 字段,而 session_id 自带 timestamp+hex8 全局唯一,不需要 role 区分。

向调用方暴露的常量
==================
``CHANNEL_CODES`` / ``DEFAULT_CHANNEL`` / ``is_known_channel`` 从旧
``storage/session_meta.py`` 平移过来,仍由 ``interface/http.py`` 与
``service/agent_runtime/commands.py`` 直接 import 使用。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage

from openhachimi_agent.core.identifiers import (
    validate_latest_scope,
    validate_role_name,
    validate_session_id,
)

if TYPE_CHECKING:
    from openhachimi_agent.tools.planning import TodoState


logger = logging.getLogger(__name__)


# ── 渠道枚举(与旧 storage/session_meta.py 保持一致) ───────────────────────
# 新增渠道时记得同步更新:
# - ChatRequest.channel(transport/api_models.py)
# - 前端 store/Chat.vue 的下拉选项
CHANNEL_CODES: tuple[str, ...] = ("webui", "cli", "telegram", "weixin")
DEFAULT_CHANNEL: str = "webui"


def is_known_channel(channel: str | None) -> bool:
    return channel in CHANNEL_CODES


SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_to_epoch(value: str | None) -> float:
    """``sessions.updated_at`` -> POSIX timestamp(供 list_sessions 兼容旧 mtime)。"""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _normalize_scope(scope: str | None) -> str:
    """对应 schema 中 ``session_pointers.scope NOT NULL``:None / "" 都映射到 ""。"""
    if scope is None or scope == "":
        return ""
    return validate_latest_scope(scope) or ""


def _new_session_id() -> str:
    """生成 ``YYYYMMDD-HHMMSS-{hex8}`` 形式的 session_id。

    与旧 ``storage.memory.create_session_id`` 行为完全一致 —— 前缀 15 字符是
    ``agent_service.list_sessions`` 解析 ``created_at`` 的依据,不能改。
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid4().hex[:8]}"


class SessionStore:
    """会话 / 消息 / 指针 / TODO 的 SQLite 持久化。

    单进程 + per-session asyncio.Lock 保证应用层无并发;``BEGIN IMMEDIATE``
    给 SQLite 兜底,避免 ``compress_session`` 这类非 lock 路径偶发写碰撞。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 连接 / schema ────────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    role         TEXT NOT NULL,
                    session_id   TEXT NOT NULL,
                    channel      TEXT NOT NULL DEFAULT 'webui',
                    scope_key    TEXT,
                    channel_raw  TEXT,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    PRIMARY KEY (role, session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_role_updated
                    ON sessions(role, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_role_channel
                    ON sessions(role, channel, updated_at DESC);

                CREATE TABLE IF NOT EXISTS session_messages (
                    role         TEXT NOT NULL,
                    session_id   TEXT NOT NULL,
                    turn_index   INTEGER NOT NULL,
                    kind         TEXT NOT NULL,
                    message_json TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    PRIMARY KEY (role, session_id, turn_index),
                    FOREIGN KEY (role, session_id)
                        REFERENCES sessions(role, session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_pointers (
                    role         TEXT NOT NULL,
                    scope        TEXT NOT NULL,
                    session_id   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    PRIMARY KEY (role, scope)
                );

                CREATE TABLE IF NOT EXISTS session_todos (
                    session_id   TEXT PRIMARY KEY,
                    state_json   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                );
                """
            )

    # ── session id 工具 ──────────────────────────────────────────────────

    @staticmethod
    def new_session_id() -> str:
        return _new_session_id()

    # ── 指针 ─────────────────────────────────────────────────────────────

    def get_latest_session_id(self, role: str, scope: str | None = None) -> str | None:
        """读取最新会话指针。无指针返回 ``None``。

        与旧实现的 "legacy" 兜底逻辑(``memory.py:64-68`` 探查 ``{role}.json``)
        不再保留 —— 迁移已经把 JSON 全删了,从这个 store 视角"legacy" 不存在。
        """
        safe_role = validate_role_name(role)
        safe_scope = _normalize_scope(scope)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM session_pointers WHERE role=? AND scope=?",
                (safe_role, safe_scope),
            ).fetchone()
        if row is None:
            return None
        candidate = row["session_id"]
        try:
            return validate_session_id(candidate, allow_legacy=False)
        except ValueError as exc:
            logger.warning(
                "ignored invalid latest session id role=%s scope=%s error=%s",
                safe_role, safe_scope, exc,
            )
            return None

    def set_latest_session_id(
        self, role: str, session_id: str, scope: str | None = None
    ) -> None:
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        safe_scope = _normalize_scope(scope)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_pointers (role, scope, session_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(role, scope) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (safe_role, safe_scope, safe_sid, _now_iso()),
            )

    # ── 生命周期 ─────────────────────────────────────────────────────────

    def start_new_session(
        self,
        role: str,
        scope: str | None = None,
        *,
        channel: str | None = None,
        scope_key: str | None = None,
    ) -> str:
        """生成新 session_id,写入指针,可选地预登记 sessions 行(渠道绑定)。

        ``channel`` 不为空时会同步写 ``sessions`` 行 —— 这覆盖了旧
        ``new_session_for_channel`` 里 ``start_new_session`` + ``save_meta`` 的
        合并语义。``INSERT OR IGNORE`` 保证渠道首写定终身(后续 ``save_messages``
        碰到既有行不会覆盖 channel)。
        """
        safe_role = validate_role_name(role)
        safe_scope = _normalize_scope(scope)
        sid = _new_session_id()
        now = _now_iso()
        ch_effective, ch_raw = self._resolve_channel(channel)
        safe_scope_key = validate_latest_scope(scope_key) if scope_key else None

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if channel is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sessions
                        (role, session_id, channel, scope_key, channel_raw, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (safe_role, sid, ch_effective, safe_scope_key, ch_raw, now, now),
                )
            conn.execute(
                """
                INSERT INTO session_pointers (role, scope, session_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(role, scope) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (safe_role, safe_scope, sid, now),
            )
        logger.info(
            "new memory session created role=%s session_id=%s scope=%s channel=%s",
            safe_role, sid, safe_scope or None, channel,
        )
        return sid

    def session_exists(self, role: str, session_id: str) -> bool:
        """sessions 表里是否已有此行 —— 取代旧 ``get_memory_path(...).exists()`` 检查。"""
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE role=? AND session_id=?",
                (safe_role, safe_sid),
            ).fetchone()
        return row is not None

    def get_channel(self, role: str, session_id: str) -> str:
        """返回会话渠道;缺失时按 ``DEFAULT_CHANNEL`` 兜底(对应旧 ``infer_channel``)。"""
        safe_role = validate_role_name(role)
        try:
            safe_sid = validate_session_id(session_id, allow_legacy=False)
        except ValueError:
            return DEFAULT_CHANNEL
        with self._connect() as conn:
            row = conn.execute(
                "SELECT channel FROM sessions WHERE role=? AND session_id=?",
                (safe_role, safe_sid),
            ).fetchone()
        if row is None:
            return DEFAULT_CHANNEL
        ch = row["channel"]
        return ch if is_known_channel(ch) else DEFAULT_CHANNEL

    # ── 消息 I/O ─────────────────────────────────────────────────────────

    def load_messages(
        self,
        role: str,
        session_id: str | None = None,
        scope: str | None = None,
    ) -> tuple[str, list[ModelMessage]]:
        """加载某会话的全部消息(按 turn_index 升序)。

        ``session_id=None`` 时走最新指针;指针也为空时新建一个 session_id 返回空
        列表 —— 复刻旧 ``load_message_history`` 的行为(保证 ``run_turn`` 总能拿
        到合法 sid)。
        """
        safe_role = validate_role_name(role)
        if session_id:
            resolved_sid = validate_session_id(session_id, allow_legacy=False)
        else:
            resolved_sid = self.get_latest_session_id(safe_role, scope) or _new_session_id()

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT message_json FROM session_messages "
                "WHERE role=? AND session_id=? ORDER BY turn_index",
                (safe_role, resolved_sid),
            ).fetchall()

        if not rows:
            logger.info(
                "message history empty role=%s session_id=%s", safe_role, resolved_sid
            )
            return resolved_sid, []

        # 把每行的单条 message JSON 拼成 array,一次 validate 出 list[ModelMessage]。
        # 用字节拼接而不是 json.loads 列表再 dump_json,避免一次额外的 round-trip。
        arr_json = b"[" + b",".join(
            row["message_json"].encode("utf-8") for row in rows
        ) + b"]"
        messages = list(ModelMessagesTypeAdapter.validate_json(arr_json))
        logger.info(
            "message history loaded role=%s session_id=%s messages=%d",
            safe_role, resolved_sid, len(messages),
        )
        return resolved_sid, messages

    def save_messages(
        self,
        role: str,
        session_id: str,
        messages: list[ModelMessage],
        *,
        scope: str | None = None,
        channel: str | None = None,
        scope_key: str | None = None,
    ) -> None:
        """全量重写一段会话的消息历史 + 更新指针 / 渠道元数据。

        实现采用 ``DELETE + INSERT`` —— 与"每轮把整段 history 写盘"的旧语义
        逐字节对齐,且 SQLite 端 ``BEGIN IMMEDIATE`` 防止与 ``compress_session``
        竞写。渠道列首写定终身(``INSERT OR IGNORE``),后续调用不覆盖。
        """
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        safe_scope = _normalize_scope(scope)
        ch_effective, ch_raw = self._resolve_channel(channel)
        safe_scope_key = validate_latest_scope(scope_key) if scope_key else None
        now = _now_iso()

        # pydantic_ai 不提供单条 ModelMessage 的 TypeAdapter,所以这里走一次
        # "整段 dump_json → json.loads 拆 array → 每条 json.dumps 入库"。
        # 同 turn 内调用 1 次,KB 量级的开销,可接受。
        arr_bytes = ModelMessagesTypeAdapter.dump_json(messages)
        arr = json.loads(arr_bytes) if arr_bytes else []
        if not isinstance(arr, list):
            raise ValueError("ModelMessagesTypeAdapter.dump_json 返回非数组,无法存储")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 渠道首写定终身:既有行不动 channel/scope_key/channel_raw。
            if channel is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sessions
                        (role, session_id, channel, scope_key, channel_raw, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (safe_role, safe_sid, ch_effective, safe_scope_key, ch_raw, now, now),
                )
            # 没传 channel 也要保证 sessions 行存在(后续 session_messages FK 依赖它)。
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions
                    (role, session_id, channel, scope_key, channel_raw, created_at, updated_at)
                VALUES (?, ?, ?, NULL, NULL, ?, ?)
                """,
                (safe_role, safe_sid, DEFAULT_CHANNEL, now, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE role=? AND session_id=?",
                (now, safe_role, safe_sid),
            )
            conn.execute(
                "DELETE FROM session_messages WHERE role=? AND session_id=?",
                (safe_role, safe_sid),
            )
            if arr:
                conn.executemany(
                    "INSERT INTO session_messages "
                    "(role, session_id, turn_index, kind, message_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            safe_role,
                            safe_sid,
                            idx,
                            str(msg_obj.get("kind", "")) if isinstance(msg_obj, dict) else "",
                            json.dumps(msg_obj, ensure_ascii=False),
                            now,
                        )
                        for idx, msg_obj in enumerate(arr)
                    ],
                )
            conn.execute(
                """
                INSERT INTO session_pointers (role, scope, session_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(role, scope) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (safe_role, safe_scope, safe_sid, now),
            )

        logger.debug(
            "message history saved role=%s session_id=%s messages=%d scope=%s channel=%s",
            safe_role, safe_sid, len(arr), safe_scope or None, channel,
        )

    # ── 列表 ─────────────────────────────────────────────────────────────

    def count_sessions(self, role: str, *, channel: str | None = None) -> int:
        """统计某 role(可选 channel)下的会话总数,供前端分页判定 ``hasMore``。

        与 ``list_sessions`` 走同一组索引(``idx_sessions_role_updated`` /
        ``idx_sessions_role_channel``),只多走一条 COUNT(*) 子查询。
        """
        safe_role = validate_role_name(role)
        filter_channel = channel if channel is not None and is_known_channel(channel) else None
        query = "SELECT COUNT(*) FROM sessions WHERE role = ?"
        params: list[Any] = [safe_role]
        if filter_channel is not None:
            query += " AND channel = ?"
            params.append(filter_channel)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0] or 0)

    def list_sessions(
        self,
        role: str,
        *,
        channel: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """返回 ``[{session_id, mtime, size_bytes, channel}, ...]`` 按 mtime 倒序。

        分页规则:``limit=None`` 时不分页(老调用方兜底);``limit`` 给定时跳过
        ``offset`` 条返回 ``limit`` 条。``offset`` 必须 >=0。

        SQL 分两步执行,避免在大表上做完 ``LEFT JOIN + GROUP BY`` 才 LIMIT:
        1. 先在 ``sessions`` 单表上拿这页的 session_id —— 命中
           ``idx_sessions_role_updated`` / ``idx_sessions_role_channel``,O(limit)。
        2. 再用 ``WHERE session_id IN (...)`` 把这些 sid 的
           ``SUM(LENGTH(message_json))`` 拼上 —— 仅扫本页那些 session 的消息行。

        ``mtime`` 从 ``sessions.updated_at`` 转 POSIX timestamp;``size_bytes``
        从 ``SUM(LENGTH(message_json))`` 估算(比旧文件 ``st_size`` 少几十字节
        的 JSON 数组括号/逗号开销,前端无阈值,可忽略)。
        """
        safe_role = validate_role_name(role)
        filter_channel = channel if channel is not None and is_known_channel(channel) else None

        # Step 1:在 sessions 表上拿本页 session_id + channel + updated_at
        page_query = (
            "SELECT session_id, channel, updated_at FROM sessions WHERE role = ?"
        )
        page_params: list[Any] = [safe_role]
        if filter_channel is not None:
            page_query += " AND channel = ?"
            page_params.append(filter_channel)
        page_query += " ORDER BY updated_at DESC"
        if limit is not None:
            page_query += " LIMIT ? OFFSET ?"
            page_params.extend([int(limit), max(int(offset), 0)])

        with self._connect() as conn:
            page_rows = conn.execute(page_query, page_params).fetchall()
            if not page_rows:
                return []
            sids = [row["session_id"] for row in page_rows]
            # Step 2:仅对本页 sid 算 size_bytes(?,?,...)
            placeholders = ",".join("?" for _ in sids)
            size_rows = conn.execute(
                f"SELECT session_id, COALESCE(SUM(LENGTH(message_json)), 0) AS size_bytes "
                f"FROM session_messages "
                f"WHERE role = ? AND session_id IN ({placeholders}) "
                f"GROUP BY session_id",
                [safe_role, *sids],
            ).fetchall()

        size_map = {row["session_id"]: int(row["size_bytes"] or 0) for row in size_rows}
        result: list[dict] = []
        for row in page_rows:
            sid = row["session_id"]
            result.append({
                "session_id": sid,
                "mtime": _iso_to_epoch(row["updated_at"]),
                "size_bytes": size_map.get(sid, 0),
                "channel": row["channel"] if is_known_channel(row["channel"]) else DEFAULT_CHANNEL,
            })
        return result

    # ── TODO state(原 .memory/todos/{sid}.json) ─────────────────────────

    def load_todo_state(self, session_id: str) -> "TodoState":
        """读取 TODO state;不存在或 JSON 损坏均返回空 ``TodoState()``(并 log)。

        这里把"坏数据当无数据"的兜底语义从 ``tools/planning.py:_load_state`` 平移
        过来 —— 见 plan Risks #5。
        """
        # 延迟 import 避免 storage <-> tools 的循环。
        from openhachimi_agent.tools.planning import TodoState, TodoTask

        try:
            safe_sid = validate_session_id(session_id, allow_legacy=False)
        except ValueError:
            return TodoState()

        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM session_todos WHERE session_id=?",
                (safe_sid,),
            ).fetchone()
        if row is None:
            return TodoState()
        try:
            data = json.loads(row["state_json"])
        except json.JSONDecodeError as exc:
            logger.warning(
                "TODO state corrupt session_id=%s error=%s", safe_sid, exc
            )
            return TodoState()
        if not isinstance(data, dict):
            return TodoState()

        state = TodoState(
            goal=str(data.get("goal", "")),
            invariants=[str(item) for item in data.get("invariants", []) if item],
            tool_calls_since_update=int(data.get("tool_calls_since_update", 0) or 0),
            is_active=bool(data.get("is_active", False)),
            created_turn_seq=int(data.get("created_turn_seq", 0) or 0),
        )

        raw_tasks = data.get("tasks", {})
        if not isinstance(raw_tasks, dict):
            return state

        for k, v in raw_tasks.items():
            try:
                task_id = int(k)
                if not isinstance(v, dict):
                    continue
                status = v.get("status", "pending")
                if status not in {"pending", "in-progress", "done", "blocked"}:
                    status = "pending"
                parent_id = v.get("parent_id")
                if parent_id is not None:
                    parent_id = int(parent_id)
                depends_on_raw = v.get("depends_on", [])
                depends_on = (
                    [int(d) for d in depends_on_raw]
                    if isinstance(depends_on_raw, list) else []
                )
                # v.get("allowed_tools", ...) 被有意忽略 ——
                # 老库可能仍带这个字段,但 TodoTask 已不再有它,丢弃即可保持向后兼容。
                risk_level = v.get("risk_level", "low")
                if risk_level not in {"low", "medium", "high"}:
                    risk_level = "low"
                state.tasks[task_id] = TodoTask(
                    id=task_id,
                    description=str(v.get("description", "Unnamed Task")),
                    status=status,
                    notes=str(v.get("notes", "")),
                    parent_id=parent_id,
                    depends_on=depends_on,
                    success_criteria=str(v.get("success_criteria", "")),
                    verification=str(v.get("verification", "")),
                    risk_level=risk_level,
                    evidence=str(v.get("evidence", "")),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "skipped corrupted TODO task k=%r session_id=%s error=%s",
                    k, safe_sid, exc,
                )
        return state

    def save_todo_state(self, session_id: str, state: "TodoState") -> None:
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        try:
            payload = json.dumps(asdict(state), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to serialize TODO state session_id=%s error=%s", safe_sid, exc)
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_todos (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (safe_sid, payload, _now_iso()),
            )

    # ── 内部 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_channel(channel: str | None) -> tuple[str, str | None]:
        """把传入 channel 归一化为 ``(channel_effective, channel_raw)``。

        - ``None`` -> ``(DEFAULT_CHANNEL, None)`` (调用方不应在 channel=None 时
          指望这条记录有渠道,这里只是给"必须落一个值"的列兜底)
        - 已知渠道 -> ``(channel, None)``
        - 未知渠道 -> ``(DEFAULT_CHANNEL, channel)``,与旧 ``save_meta`` 一致
        """
        if channel is None:
            return DEFAULT_CHANNEL, None
        if is_known_channel(channel):
            return channel, None
        logger.warning("unknown channel code, fallback to default channel=%s", channel)
        return DEFAULT_CHANNEL, channel


__all__ = [
    "CHANNEL_CODES",
    "DEFAULT_CHANNEL",
    "is_known_channel",
    "SessionStore",
]
