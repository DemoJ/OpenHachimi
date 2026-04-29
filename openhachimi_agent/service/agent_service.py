"""Agent 后台服务层。"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from openhachimi_agent.agent.factory import build_agent
from openhachimi_agent.content.roles import list_role_names
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.storage.memory import load_message_history, save_message_history, start_new_session
from openhachimi_agent.transport.api_models import AgentState, ChatResponse, CommandResponse, RolesResponse


logger = logging.getLogger(__name__)
_STREAM_DONE = object()


def _text_from_stream_event(event: object) -> str:
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    return ""


def _summarize_tool_args(args: object, max_chars: int = 160) -> str:
    if args in (None, "", {}):
        return ""
    if isinstance(args, str):
        text = args
    else:
        try:
            text = json.dumps(args, ensure_ascii=False)
        except TypeError:
            text = str(args)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _status_from_stream_event(event: object) -> str:
    if isinstance(event, FunctionToolCallEvent):
        tool_name = event.part.tool_name
        args = _summarize_tool_args(event.part.args_as_dict())
        if args:
            return f"\n[工具] 正在调用 {tool_name}：{args}\n"
        return f"\n[工具] 正在调用 {tool_name}\n"

    if isinstance(event, FunctionToolResultEvent):
        result = event.result
        outcome = getattr(result, "outcome", "success")
        if outcome == "success":
            return f"[工具] {result.tool_name} 完成\n"
        return f"[工具] {result.tool_name} 结束：{outcome}\n"

    return ""


class AgentService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.current_role = config.default_role_name
        self.agent = build_agent(config, self.current_role)
        self.current_session_id, self.history = load_message_history(config.memory_dir, self.current_role)
        logger.info(
            "service initialized role=%s session_id=%s history_messages=%d model=%s",
            self.current_role,
            self.current_session_id,
            len(self.history),
            self.config.model_name,
        )

    def state(self) -> AgentState:
        return AgentState(
            role=self.current_role,
            session_id=self.current_session_id,
            has_history=bool(self.history),
            model=self.config.model_name,
            base_url=self.config.openai_base_url or None,
        )

    def list_roles(self) -> RolesResponse:
        logger.debug("listing roles roles_dir=%s", self.config.roles_dir)
        return RolesResponse(
            roles=list_role_names(self.config.roles_dir),
            current_role=self.current_role,
        )

    def new_session(self) -> CommandResponse:
        previous_session_id = self.current_session_id
        self.current_session_id = start_new_session(self.config.memory_dir, self.current_role)
        self.history: list[ModelMessage] = []
        logger.info(
            "new session role=%s previous_session_id=%s session_id=%s",
            self.current_role,
            previous_session_id,
            self.current_session_id,
        )
        return CommandResponse(
            message="已保存上一段对话，并新建对话。",
            role=self.current_role,
            session_id=self.current_session_id,
        )

    def switch_role(self, role_name: str) -> CommandResponse:
        previous_role = self.current_role
        self.agent = build_agent(self.config, role_name)
        self.current_role = role_name
        self.current_session_id = start_new_session(self.config.memory_dir, self.current_role)
        self.history = []
        logger.info(
            "switched role previous_role=%s role=%s session_id=%s",
            previous_role,
            self.current_role,
            self.current_session_id,
        )
        return CommandResponse(
            message=f"已切换到角色：{role_name}，并新建对话。",
            role=self.current_role,
            session_id=self.current_session_id,
        )

    def send_message(self, message: str) -> ChatResponse:
        start_time = time.perf_counter()
        logger.info(
            "chat started role=%s session_id=%s message_chars=%d history_messages=%d stream=false",
            self.current_role,
            self.current_session_id,
            len(message),
            len(self.history),
        )
        try:
            result = self.agent.run_sync(message, message_history=self.history, deps=self.config)
        except Exception:
            logger.exception(
                "chat failed role=%s session_id=%s stream=false",
                self.current_role,
                self.current_session_id,
            )
            raise
        self.history = list(result.all_messages())
        save_message_history(
            self.config.memory_dir,
            self.current_role,
            self.current_session_id,
            result.all_messages_json(),
        )
        logger.info(
            "chat finished role=%s session_id=%s output_chars=%d history_messages=%d duration_ms=%.0f stream=false",
            self.current_role,
            self.current_session_id,
            len(str(result.output)),
            len(self.history),
            (time.perf_counter() - start_time) * 1000,
        )
        return ChatResponse(
            output=result.output,
            role=self.current_role,
            session_id=self.current_session_id,
        )

    async def stream_message(self, message: str) -> AsyncIterator[str]:
        start_time = time.perf_counter()
        output_chars = 0
        chunk_count = 0
        first_chunk_ms: float | None = None
        stream_queue: asyncio.Queue[str | object] = asyncio.Queue()
        result_holder: dict[str, object] = {}
        logger.info(
            "chat started role=%s session_id=%s message_chars=%d history_messages=%d stream=true",
            self.current_role,
            self.current_session_id,
            len(message),
            len(self.history),
        )

        async def handle_stream_events(_ctx: object, stream: object) -> None:
            async for event in stream:  # type: ignore[attr-defined]
                if chunk := _text_from_stream_event(event):
                    await stream_queue.put(chunk)
                if status := _status_from_stream_event(event):
                    await stream_queue.put(status)

        async def run_agent() -> None:
            try:
                result_holder["result"] = await self.agent.run(
                    message,
                    message_history=self.history,
                    deps=self.config,
                    event_stream_handler=handle_stream_events,
                )
            except Exception as exc:
                result_holder["error"] = exc
                logger.exception(
                    "chat failed role=%s session_id=%s stream=true",
                    self.current_role,
                    self.current_session_id,
                )
            finally:
                await stream_queue.put(_STREAM_DONE)

        task = asyncio.create_task(run_agent())

        while True:
            item = await stream_queue.get()
            if item is _STREAM_DONE:
                break

            chunk = str(item)
            if first_chunk_ms is None:
                first_chunk_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "chat first chunk role=%s session_id=%s first_chunk_ms=%.0f chunk_chars=%d",
                    self.current_role,
                    self.current_session_id,
                    first_chunk_ms,
                    len(chunk),
                )
            chunk_count += 1
            output_chars += len(chunk)
            yield chunk

        await task
        if error := result_holder.get("error"):
            raise error

        result = result_holder["result"]
        self.history = list(result.all_messages())  # type: ignore[attr-defined]
        save_message_history(
            self.config.memory_dir,
            self.current_role,
            self.current_session_id,
            result.all_messages_json(),  # type: ignore[attr-defined]
        )

        if not chunk_count:
            output = str(result.output)  # type: ignore[attr-defined]
            if output:
                output_chars = len(output)
                chunk_count = 1
                logger.info(
                    "chat produced non-streamed output role=%s session_id=%s output_chars=%d",
                    self.current_role,
                    self.current_session_id,
                    output_chars,
                )
                yield output

        logger.info(
            "chat finished role=%s session_id=%s output_chars=%d chunks=%d first_chunk_ms=%s history_messages=%d duration_ms=%.0f stream=true",
            self.current_role,
            self.current_session_id,
            output_chars,
            chunk_count,
            f"{first_chunk_ms:.0f}" if first_chunk_ms is not None else None,
            len(self.history),
            (time.perf_counter() - start_time) * 1000,
        )
