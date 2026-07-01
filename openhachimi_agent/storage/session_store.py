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
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
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

                -- 压缩元数据:原始消息永不删(session_messages append-only),压缩只记边界+摘要,
                -- AI 运行时上下文(load_context)与前端展示按此表组装/折叠出视图。
                -- 不变式:同一 session 内 compression_id 单调递增,load_context 只取最大一代。
                CREATE TABLE IF NOT EXISTS session_compressions (
                    role            TEXT NOT NULL,
                    session_id      TEXT NOT NULL,
                    compression_id  INTEGER NOT NULL,   -- 该会话内第几代压缩,从 1 递增
                    created_at      TEXT NOT NULL,
                    head_end_turn   INTEGER NOT NULL,   -- head 末尾(含)对应的 turn_index
                    tail_start_turn INTEGER NOT NULL,   -- tail 起始(含)对应的 turn_index
                    summary_turn    INTEGER NOT NULL,   -- summary 占位条插入位置的 turn_index
                    summary_text    TEXT NOT NULL,      -- 结构化摘要全文
                    PRIMARY KEY (role, session_id, compression_id),
                    FOREIGN KEY (role, session_id)
                        REFERENCES sessions(role, session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_compressions_session
                    ON session_compressions(role, session_id, compression_id DESC);
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

    def _load_message_rows(
        self, role: str, session_id: str, limit: int | None = None, before_turn: int | None = None,
    ) -> tuple[list[tuple[int, ModelMessage]], bool]:
        """按 turn_index 升序返回 ``([(turn_index, ModelMessage), ...], has_more)``。

        供展示层折叠渲染用 —— 需要逐条的 turn_index 来判定是否落入某次压缩的折叠区间,
        ``load_messages`` 丢弃了 turn_index,故单列此方法。每条独立 validate(展示路径,
        条数通常有限,可接受)。
        """
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        query = (
            "SELECT turn_index, message_json FROM session_messages "
            "WHERE role=? AND session_id=?"
        )
        params: list[Any] = [safe_role, safe_sid]
        
        if before_turn is not None:
            query += " AND turn_index < ?"
            params.append(before_turn)
            
        query += " ORDER BY turn_index DESC"
        
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit + 1)
            
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            
        has_more = False
        if limit is not None and len(rows) > limit:
            has_more = True
            rows = rows[:limit]
            
        rows.reverse()
        
        result: list[tuple[int, ModelMessage]] = []
        for r in rows:
            msg = ModelMessagesTypeAdapter.validate_json(
                b"[" + r["message_json"].encode("utf-8") + b"]"
            )
            if msg:
                result.append((int(r["turn_index"]), msg[0]))
        return result, has_more

    def count_messages(self, role: str, session_id: str) -> int:
        """返回某会话的消息总数。"""
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM session_messages WHERE role=? AND session_id=?",
                (safe_role, safe_sid),
            ).fetchone()
        return int(row[0] or 0)


    def save_messages(
        self,
        role: str,
        session_id: str,
        messages: list[ModelMessage],
        *,
        scope: str | None = None,
        channel: str | None = None,
        scope_key: str | None = None,
        append: bool = True,
    ) -> int:
        """追加写入会话消息历史 + 更新指针 / 渠道元数据,返回本次写入的起始 turn_index。

        实现采用 ``append-only`` —— 原始消息一旦写入永不删除。turn_index 从
        ``MAX(turn_index)+1`` 起续编(空表从 0 起),用 ``INSERT OR IGNORE`` 按主键
        ``(role, session_id, turn_index)`` 去重,保证幂等可重入。``append=False``
        兼容旧的「全量覆盖」语义(DELETE+从 0 重编),仅压缩链重建等极端路径用,
        正常 turn/压缩均走 append。

        与 ``compress_session`` 竞写由应用层 per-session asyncio.Lock 保证;SQLite 端
        ``BEGIN IMMEDIATE`` 兜底。渠道列首写定终身(``INSERT OR IGNORE``),后续不覆盖。
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
            if not append:
                # 兼容旧「全量覆盖」语义:先清空,从 0 重编。仅极端路径用。
                conn.execute(
                    "DELETE FROM session_messages WHERE role=? AND session_id=?",
                    (safe_role, safe_sid),
                )
                base_turn = 0
            else:
                # append-only:续编。MAX(turn_index) 为空(NULL)时取 -1,首条从 0 起。
                row = conn.execute(
                    "SELECT COALESCE(MAX(turn_index), -1) FROM session_messages "
                    "WHERE role=? AND session_id=?",
                    (safe_role, safe_sid),
                ).fetchone()
                base_turn = int(row[0]) + 1
            if arr:
                conn.executemany(
                    "INSERT OR IGNORE INTO session_messages "
                    "(role, session_id, turn_index, kind, message_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            safe_role,
                            safe_sid,
                            base_turn + idx,
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
            "message history saved role=%s session_id=%s messages=%d start_turn=%d append=%s scope=%s channel=%s",
            safe_role, safe_sid, len(arr), base_turn, append, safe_scope or None, channel,
        )
        return base_turn

    def current_turn_count(self, role: str, session_id: str) -> int:
        """返回当前已落库消息条数(``MAX(turn_index)+1``),供调用方推导「本轮新增起点」。

        append-only 语义下,这是判定「下一轮 save 从哪个 turn_index 起编」、以及
        ``_stamp_turn_metadata`` 的 prev_len 的权威来源 —— 避免误用压缩视图长度
        (视图因折叠中间窗口而短于落库行数)。
        """
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_index), -1) FROM session_messages "
                "WHERE role=? AND session_id=?",
                (safe_role, safe_sid),
            ).fetchone()
        return int(row[0]) + 1

    # ── 压缩元数据 ────────────────────────────────────────────────────────
    # 原始消息永不删(session_messages append-only),压缩只在此表记边界+摘要。
    # load_context 据此组装 head+summary+tail 视图喂模型;get_session_messages
    # 据此在前端折叠出占位条。详见 plans/fancy-watching-taco.md 方案 A。

    def _latest_compression(
        self, conn: sqlite3.Connection, role: str, session_id: str
    ) -> sqlite3.Row | None:
        """取该会话最新一代压缩元数据(compression_id 最大)。无则 None。"""
        return conn.execute(
            "SELECT * FROM session_compressions "
            "WHERE role=? AND session_id=? "
            "ORDER BY compression_id DESC LIMIT 1",
            (role, session_id),
        ).fetchone()

    def list_compressions(
        self, role: str, session_id: str
    ) -> list[dict]:
        """列出某会话的全部压缩元数据(按 compression_id 升序),供前端折叠渲染。"""
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT compression_id, created_at, head_end_turn, tail_start_turn, "
                "       summary_turn, summary_text "
                "FROM session_compressions WHERE role=? AND session_id=? "
                "ORDER BY compression_id",
                (safe_role, safe_sid),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_compression(
        self,
        role: str,
        session_id: str,
        head_end_turn: int,
        tail_start_turn: int,
        summary: str,
        *,
        total_len: int,
    ) -> int:
        """记录一次压缩的边界与摘要,返回本次 compression_id。

        ``head_end_turn`` / ``tail_start_turn`` 是 ``session_messages`` 的全局
        turn_index(含),由调用方保证:compress 作用于「按 turn_index 升序的完整原始
        序列」时,内存下标即 turn_index。``summary_turn = head_end_turn + 1``。
        ``total_len`` 是该完整序列长度,用于 sanity 校验 tail_start_turn 不越界。
        """
        if not summary:
            raise ValueError("record_compression 要求非空 summary")
        if not (0 <= head_end_turn < tail_start_turn <= total_len):
            raise ValueError(
                f"压缩边界非法 head_end_turn={head_end_turn} tail_start_turn={tail_start_turn} "
                f"total_len={total_len}"
            )
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(compression_id), 0) FROM session_compressions "
                "WHERE role=? AND session_id=?",
                (safe_role, safe_sid),
            ).fetchone()
            next_id = int(row[0]) + 1
            conn.execute(
                "INSERT INTO session_compressions "
                "(role, session_id, compression_id, created_at, "
                " head_end_turn, tail_start_turn, summary_turn, summary_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    safe_role, safe_sid, next_id, now,
                    head_end_turn, tail_start_turn, head_end_turn + 1, summary,
                ),
            )
        logger.info(
            "compression recorded role=%s session_id=%s compression_id=%d head_end=%d tail_start=%d",
            safe_role, safe_sid, next_id, head_end_turn, tail_start_turn,
        )
        return next_id

    def get_folded_messages(
        self, role: str, session_id: str, compression_id: int
    ) -> list[ModelMessage]:
        """取某次压缩被折叠的原始消息(head_end_turn+1 .. tail_start_turn-1),供前端展开。"""
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        with self._connect() as conn:
            comp = conn.execute(
                "SELECT head_end_turn, tail_start_turn FROM session_compressions "
                "WHERE role=? AND session_id=? AND compression_id=?",
                (safe_role, safe_sid, compression_id),
            ).fetchone()
            if comp is None:
                return []
            rows = conn.execute(
                "SELECT message_json FROM session_messages "
                "WHERE role=? AND session_id=? "
                "  AND turn_index > ? AND turn_index < ? "
                "ORDER BY turn_index",
                (safe_role, safe_sid, comp["head_end_turn"], comp["tail_start_turn"]),
            ).fetchall()
        if not rows:
            return []
        arr_json = b"[" + b",".join(
            r["message_json"].encode("utf-8") for r in rows
        ) + b"]"
        return list(ModelMessagesTypeAdapter.validate_json(arr_json))

    def load_context(
        self,
        role: str,
        session_id: str | None = None,
        scope: str | None = None,
    ) -> tuple[str, list[ModelMessage]]:
        """加载 AI 运行时上下文视图:head + summary + tail。

        若无压缩元数据,等价于 ``load_messages``。有则按最新一代的边界取 head/tail 两段,
        中间用 summary 占位(运行时组装,不落库)。具体组装逻辑在
        ``context.context_view.assemble_runtime_context``。

        **append-only 不变式**:返回的视图长度 ≤ 完整原始序列长度(中间折叠区间不进视图)。
        调用方(如 ``turn.py`` 的元数据盖章)若需"本轮新增消息起点",应直接从
        ``session_messages.MAX(turn_index)+1`` 推导(见 ``current_turn_count``),
        而非用 ``len(view)``,否则多代压缩下视图长度会小于落库行数导致差量误判。
        """
        from openhachimi_agent.context.context_view import assemble_runtime_context

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
            comp = self._latest_compression(conn, safe_role, resolved_sid) if rows else None

        if not rows:
            logger.info(
                "message history empty role=%s session_id=%s", safe_role, resolved_sid
            )
            return resolved_sid, []
        if comp is None:
            arr_json = b"[" + b",".join(
                r["message_json"].encode("utf-8") for r in rows
            ) + b"]"
            messages = list(ModelMessagesTypeAdapter.validate_json(arr_json))
            logger.info(
                "context loaded(no compression) role=%s session_id=%s messages=%d",
                safe_role, resolved_sid, len(messages),
            )
            return resolved_sid, messages

        # 有压缩:取 head[0..head_end_turn] + tail[tail_start_turn..],中间折叠。
        head_rows = rows[: comp["head_end_turn"] + 1]
        tail_rows = rows[comp["tail_start_turn"]:]
        head_json = b"[" + b",".join(
            r["message_json"].encode("utf-8") for r in head_rows
        ) + b"]"
        tail_json = b"[" + b",".join(
            r["message_json"].encode("utf-8") for r in tail_rows
        ) + b"]"
        head = list(ModelMessagesTypeAdapter.validate_json(head_json)) if head_rows else []
        tail = list(ModelMessagesTypeAdapter.validate_json(tail_json)) if tail_rows else []
        context = assemble_runtime_context(head, tail, comp["summary_text"])
        logger.info(
            "context loaded(compressed id=%d) role=%s session_id=%s head=%d tail=%d view=%d",
            comp["compression_id"], safe_role, resolved_sid, len(head), len(tail), len(context),
        )
        return resolved_sid, context



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

    def delete_session(self, role: str, session_id: str) -> None:
        """删除会话及其全部关联数据 + 清理悬空的 latest 指针。

        - ``session_messages`` 靠 FK ``ON DELETE CASCADE`` 随 ``sessions`` 行自动清除
          (``_connect`` 已开 ``PRAGMA foreign_keys=ON``)。
        - ``session_todos`` 主键只含 ``session_id``,无 FK,需显式删。
        - ``session_pointers`` 若仍指向该 sid 会变成悬空指针 —— 下次不带 ``session_id``
          发消息会"恢复"到已删除的会话,故一并清掉该 role 下所有指向它的指针。
        """
        safe_role = validate_role_name(role)
        safe_sid = validate_session_id(session_id, allow_legacy=False)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM session_todos WHERE session_id = ?", (safe_sid,)
            )
            conn.execute(
                "DELETE FROM session_pointers WHERE role = ? AND session_id = ?",
                (safe_role, safe_sid),
            )
            conn.execute(
                "DELETE FROM sessions WHERE role = ? AND session_id = ?",
                (safe_role, safe_sid),
            )  # CASCADE 自动删 session_messages

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
