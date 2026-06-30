from __future__ import annotations

from openhachimi_agent.memory.models import MemoryTurn


class TurnStoreMixin:
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
