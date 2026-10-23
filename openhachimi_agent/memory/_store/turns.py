from __future__ import annotations

from openhachimi_agent.memory.models import MemoryTurn


class TurnStoreMixin:
    def list_recent_user_turns(self, session_id: str, *, limit: int = 2, max_chars: int = 500) -> list[dict[str, str]]:
        """同一会话最近几轮用户对话(旧在前),给记忆抽取当上下文。文本截断到 max_chars。"""
        if not session_id or limit <= 0:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT user_message, assistant_output FROM memory_turns
                WHERE session_id = ? AND source = 'user'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            {"user_message": row["user_message"][:max_chars], "assistant_output": row["assistant_output"][:max_chars]}
            for row in reversed(rows)
        ]

    def add_turn(self, turn: MemoryTurn) -> str:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_turns(
                    id, turn_id, tenant_id, user_id, role_name, session_id, channel,
                    user_message, assistant_output, tool_calls_summary_json, task_frame_json,
                    memory_context_ids_json, status, source, error_summary, started_at, finished_at,
                    duration_ms, raw_messages_json_ref, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    turn.id,
                    turn.turn_id,
                    turn.tenant_id,
                    turn.user_id,
                    turn.role_name,
                    turn.session_id,
                    turn.channel,
                    turn.user_message,
                    turn.assistant_output,
                    turn.tool_calls_summary_json,
                    turn.task_frame_json,
                    turn.memory_context_ids_json,
                    turn.status,
                    turn.source,
                    turn.error_summary,
                    turn.started_at,
                    turn.finished_at,
                    turn.duration_ms,
                    turn.raw_messages_json_ref,
                    turn.created_at,
                ),
            )
        return turn.id
