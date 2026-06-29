"""命令行交互逻辑。"""

import asyncio
import codecs
import contextlib
import json
import logging
import os
import threading
from functools import lru_cache
from typing import AsyncIterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.core.redaction import redact_text
from openhachimi_agent.interface.presenter import ToolProgressPresenter
from openhachimi_agent.scheduler.delivery import (
    CliDeliverySender,
    DeliverySenderRegistry,
    InboxDeliverySender,
    deliver_scheduled_run,
)
from openhachimi_agent.scheduler.store import ScheduledTaskStore
from openhachimi_agent.scheduler.scheduler import TaskScheduler
from openhachimi_agent.scheduler.models import ScheduledRun, ScheduledTask
from openhachimi_agent.service.agent_runtime.command_registry import CommandOutcome
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.memory.recall import get_memory_store
from openhachimi_agent.memory.scheduler import MemoryScheduler

logger = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"


def get_server_url() -> str:
    return os.getenv("OPENHACHIMI_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")


@lru_cache(maxsize=1)
def _configured_http_api_token() -> str | None:
    try:
        return load_config().http_api_token
    except Exception as exc:
        logger.debug("failed to load HTTP API token from config: %s", exc)
        return None


def get_http_api_token() -> str | None:
    return _configured_http_api_token()


def _request_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    token = get_http_api_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(server_url: str, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    logger.debug("http request method=%s path=%s server_url=%s", method, path, server_url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{server_url}{path}",
        data=data,
        method=method,
        headers=_request_headers(),
    )
    with urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def request_stream(server_url: str, path: str, payload: dict[str, object]):
    logger.debug("http stream request path=%s server_url=%s", path, server_url)
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{server_url}{path}",
        data=data,
        method="POST",
        headers=_request_headers({"Accept": "text/event-stream"}),
    )
    with urlopen(request) as response:
        for line in response:
            line_str = line.decode("utf-8").strip()
            if line_str.startswith("data: "):
                data_str = line_str[6:]
                try:
                    data_json = json.loads(data_str)
                    if "error" in data_json:
                        raise URLError(redact_text(data_json["error"]))
                    event_type = data_json.get("type")
                    if event_type in {"text", "tool", "system"}:
                        yield StreamEventItem(
                            type=event_type,
                            text=data_json.get("text", ""),
                            tool_name=data_json.get("tool_name"),
                            tool_icon=data_json.get("tool_icon"),
                            temporary=bool(data_json.get("temporary", False)),
                        )
                    elif "text" in data_json:
                        yield data_json["text"]
                except json.JSONDecodeError:
                    pass


def error_detail(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except json.JSONDecodeError:
        return redact_text(str(exc))
    return redact_text(str(payload.get("detail", exc)))


def print_welcome(state: dict[str, object], server_url: str, current_role: str, current_session_id: str) -> None:
    from openhachimi_agent.core.version import get_version

    print(f"OpenHachimi CLI Agent  v{get_version()}")
    print(f"服务地址:{server_url}")
    print(f"当前模型:{state['model']}")
    if state.get("base_url"):
        print(f"模型服务:{state['base_url']}")
    print(f"当前角色:{current_role}")
    print(f"当前会话:{current_session_id}")
    print("输入内容后回车即可对话。")
    print("可用命令:/help 查看帮助,/roles 查看角色,/role <名称> 切换角色,/new 新建对话,/exit 退出程序。")
    print()


class CliBackend(Protocol):
    async def get_state(self) -> dict[str, object]: ...
    async def list_roles(self) -> list[str]: ...
    async def latest_session(self, role: str) -> tuple[str, str]: ...
    async def dispatch_command(
        self,
        message: str,
        role: str,
        session_id: str,
    ) -> CommandOutcome | None: ...
    async def stream_message(self, message: str, role: str, session_id: str) -> AsyncIterator[str | StreamEventItem]: ...


class EmbeddedBackend:
    def __init__(self, service: AgentService):
        self.service = service

    async def get_state(self) -> dict[str, object]:
        state = self.service.state()
        return {
            "model": state.model,
            "base_url": state.base_url,
        }

    async def list_roles(self) -> list[str]:
        return self.service.list_roles().roles

    async def latest_session(self, role: str) -> tuple[str, str]:
        # CLI 走自己的 scope=cli,与 WebUI/IM 隔离,不再读写全局 latest。
        resp = self.service.latest_session(role, latest_scope="cli")
        return resp.role, resp.session_id

    async def dispatch_command(
        self,
        message: str,
        role: str,
        session_id: str,
    ) -> CommandOutcome | None:
        channel_context = {
            "type": "cli",
            "platform": "cli",
            "channel_code": "cli",
            "session_scope_key": "cli",
            "session_id": session_id,
            "role": role,
        }
        return await self.service.dispatch_command(
            message,
            role=role,
            session_id=session_id,
            channel_context=channel_context,
            channel="cli",
        )

    async def stream_message(self, message: str, role: str, session_id: str) -> AsyncIterator[str | StreamEventItem]:
        channel_context = {
            "type": "cli",
            "platform": "cli",
            "channel_code": "cli",
            "session_scope_key": "cli",
            "session_id": session_id,
            "role": role,
        }
        async for event in self.service.stream_events(message, role, session_id, channel_context=channel_context):
            yield event


class HttpBackend:
    def __init__(self, server_url: str):
        self.server_url = server_url

    async def get_state(self) -> dict[str, object]:
        return await asyncio.to_thread(request_json, self.server_url, "GET", "/state")

    async def list_roles(self) -> list[str]:
        resp = await asyncio.to_thread(request_json, self.server_url, "GET", "/roles")
        return resp.get("roles", [])

    async def latest_session(self, role: str) -> tuple[str, str]:
        # CLI HTTP 客户端启动时拉一次 latest:scope=cli 走专属 latest_by_scope/,
        # 避免被 WebUI / IM 的 latest 写入污染。/session/latest 当前没有 scope 形参,
        # 暂时仍走全局 latest;后端 latest_session 已读 scope=None,长期方案见 TODO。
        # TODO(channel-isolation): 给 /session/latest 加 ?scope=cli 后切到隔离 scope。
        qs = urlencode({'role': role})
        resp = await asyncio.to_thread(request_json, self.server_url, "GET", f"/session/latest?{qs}")
        return resp["role"], resp["session_id"]

    async def dispatch_command(
        self,
        message: str,
        role: str,
        session_id: str,
    ) -> CommandOutcome | None:
        try:
            resp = await asyncio.to_thread(
                request_json,
                self.server_url,
                "POST",
                "/commands",
                {"message": message, "role": role, "session_id": session_id, "channel": "cli"},
            )
        except HTTPError as exc:
            raise RuntimeError(error_detail(exc)) from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        if not resp.get("handled"):
            return None
        return CommandOutcome(
            message=str(resp.get("message", "")),
            kind=str(resp.get("kind", "info")),  # type: ignore[arg-type]
            role=resp.get("role"),
            session_id=resp.get("session_id"),
        )

    async def stream_message(self, message: str, role: str, session_id: str) -> AsyncIterator[str | StreamEventItem]:
        q: asyncio.Queue[str | Exception | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run():
            try:
                for chunk in request_stream(self.server_url, "/chat/stream", {
                    "message": message,
                    "role": role,
                    "session_id": session_id,
                    "channel": "cli",
                }):
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
                loop.call_soon_threadsafe(q.put_nowait, None)
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, e)

        threading.Thread(target=_run, daemon=True).start()

        while True:
            item = await q.get()
            if item is None:
                break
            if isinstance(item, Exception):
                if isinstance(item, HTTPError):
                    raise RuntimeError(error_detail(item)) from item
                raise RuntimeError(str(item)) from item
            yield item


async def _read_stdin(input_queue: asyncio.Queue[object]) -> None:
    while True:
        try:
            user_input = await asyncio.to_thread(input, "你 > ")
            await input_queue.put(user_input.strip())
        except (EOFError, KeyboardInterrupt) as exc:
            await input_queue.put(exc)
            return


async def _render_stream_message(backend: CliBackend, message: str, role: str, session_id: str) -> None:
    print("哈基米 > ", end="", flush=True)
    presenter = ToolProgressPresenter(mode="cli")
    async for event in backend.stream_message(message, role, session_id):
        if isinstance(event, StreamEventItem):
            for action in presenter.handle_event(event):
                if action.type == "tool":
                    print(f"\n[工具] {action.text}", flush=True)
                elif action.type in {"text", "system"}:
                    print(action.text, end="", flush=True)
        else:
            print(event, end="", flush=True)


async def _cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def run_interactive_loop(backend: CliBackend, server_url: str, current_role: str) -> None:
    try:
        current_role, current_session_id = await backend.latest_session(current_role)
        state = await backend.get_state()
    except Exception as exc:
        print(f"初始化失败:{exc}")
        return

    print_welcome(state, server_url, current_role, current_session_id)
    input_queue: asyncio.Queue[object] = asyncio.Queue()
    stdin_task = asyncio.create_task(_read_stdin(input_queue))

    def _apply_outcome(outcome: CommandOutcome, *, prefix: str = "") -> tuple[str, str]:
        """打印 outcome 文案并更新本地会话状态;返回 (role, session_id)。"""
        nonlocal current_role, current_session_id
        if outcome.role:
            current_role = outcome.role
        if outcome.session_id:
            current_session_id = outcome.session_id
        if outcome.message:
            print(f"{prefix}{outcome.message}")
        return current_role, current_session_id

    try:
        while True:
            item = await input_queue.get()
            if isinstance(item, (EOFError, KeyboardInterrupt)):
                print("\n已退出对话。")
                return
            user_input = str(item).strip()

            if not user_input:
                continue

            # 命令统一交给后端分派
            try:
                outcome = await backend.dispatch_command(user_input, current_role, current_session_id)
            except Exception as exc:
                print(f"哈基米 > 命令执行失败:{exc}")
                continue
            if outcome is not None:
                _apply_outcome(outcome)
                if outcome.kind == "exit":
                    return
                print()
                continue

            stream_task = asyncio.create_task(_render_stream_message(backend, user_input, current_role, current_session_id))
            pending_input_task: asyncio.Task | None = None
            try:
                while not stream_task.done():
                    pending_input_task = asyncio.create_task(input_queue.get())
                    done, pending = await asyncio.wait(
                        {stream_task, pending_input_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stream_task in done:
                        pending_input_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pending_input_task
                        await stream_task
                        break

                    pending_input_task = None
                    interrupt_item = next(iter(done)).result()
                    if isinstance(interrupt_item, (EOFError, KeyboardInterrupt)):
                        try:
                            await backend.dispatch_command("/stop", current_role, current_session_id)
                        except Exception:
                            pass
                        await _cancel_task(stream_task)
                        print("\n已退出对话。")
                        return

                    interrupt_input = str(interrupt_item).strip()
                    if not interrupt_input:
                        continue
                    # 流式执行中,只接受可抢占类命令(stop / new / exit);其它命令延迟到任务结束
                    try:
                        outcome = await backend.dispatch_command(interrupt_input, current_role, current_session_id)
                    except Exception as exc:
                        print(f"\n哈基米 > 命令执行失败:{exc}")
                        continue
                    if outcome is None:
                        print("\n哈基米 > 当前任务执行中,可输入 /stop 或 /new 抢占;普通消息请稍后再发。")
                        continue
                    if outcome.kind in {"stop", "new_session", "exit"}:
                        _apply_outcome(outcome, prefix="\n")
                        await _cancel_task(stream_task)
                        if outcome.kind == "exit":
                            return
                        break
                    # 其它命令(/help、/roles 等)在抢占态下不打断流,仅提示
                    print(f"\n哈基米 > 当前任务执行中,{outcome.message}")
            except Exception as exc:
                print(f"\n哈基米 > 调用模型时出错:{exc}")
                if not stream_task.done():
                    await _cancel_task(stream_task)
            finally:
                if pending_input_task is not None and not pending_input_task.done():
                    pending_input_task.cancel()

            print("\n")
    finally:
        stdin_task.cancel()


async def run_embedded_cli() -> None:
    config = load_config()
    configure_logging(config)
    logger.info("starting embedded cli")
    service = AgentService(config)
    await service.start()
    backend = EmbeddedBackend(service)
    scheduler = None
    # 长期记忆后台调度器:消费 memory_jobs 队列。不启动则 embed/extract/consolidate
    # 任务永远 pending(embeddings_pending 虚高、L1 抽取不跑)。与 http.py lifespan 对齐。
    memory_scheduler: MemoryScheduler | None = None
    if config.memory.enabled and config.memory.scheduler.enabled:
        memory_scheduler = MemoryScheduler(
            get_memory_store(config),
            config=config,
            poll_interval_seconds=config.memory.scheduler.poll_interval_seconds,
            batch_size=config.memory.scheduler.batch_size,
        )
        await memory_scheduler.start()
        logger.info("memory scheduler started")

    delivery_registry = DeliverySenderRegistry()
    delivery_registry.register(InboxDeliverySender())
    delivery_registry.register(CliDeliverySender(lambda text: print(text, end="", flush=True)))

    async def on_scheduled_run_complete(task: ScheduledTask, run: ScheduledRun) -> None:
        await deliver_scheduled_run(task, run, store=schedule_store, registry=delivery_registry, config=config)

    if config.scheduler.enabled and config.scheduler.db_path is not None:
        schedule_store = ScheduledTaskStore(config.scheduler.db_path)
        inbox_runs = schedule_store.list_inbox_runs(unread_only=True, limit=10)
        if inbox_runs:
            print(f"\n哈基米 > 你有 {len(inbox_runs)} 条未读定时任务结果:")
            for task, run in inbox_runs:
                output = run.output or run.error or run.status
                print(f"[定时任务:{task.name}] {str(output)[:500]}")
                schedule_store.mark_run_read(run.id)
            print("")
        scheduler = TaskScheduler(
            schedule_store,
            service,
            poll_interval_seconds=config.scheduler.poll_interval_seconds,
            max_concurrency=config.scheduler.max_concurrency,
            default_timeout_seconds=config.scheduler.default_timeout_seconds,
            claim_lock_seconds=config.scheduler.claim_lock_seconds,
            delivery_registry=delivery_registry,
            config=config,
            on_run_complete=on_scheduled_run_complete,
        )
        await scheduler.start()

    try:
        await run_interactive_loop(backend, "embedded", config.default_role_name)
    finally:
        if scheduler is not None:
            await scheduler.stop()
        if memory_scheduler is not None:
            try:
                await memory_scheduler.stop()
            except Exception as exc:
                logger.debug("memory scheduler stop failed: %s", exc)
        try:
            await service.browser_manager.close()
        except Exception as exc:
            logger.debug("browser cleanup on exit failed: %s", exc)
        try:
            await service.stop()
        except Exception as exc:
            logger.debug("service stop on exit failed: %s", exc)


def run_cli() -> None:
    try:
        configure_logging(load_config())
    except Exception:
        logging.basicConfig(level=logging.INFO)
        logger.exception("failed to configure logging from local config")

    server_url = get_server_url()
    logger.info("starting cli client server_url=%s", server_url)

    try:
        roles_info = request_json(server_url, "GET", "/roles")
        current_role = roles_info["current_role"]
    except URLError as exc:
        raise SystemExit(f"无法连接 OpenHachimi 后台服务:{server_url},请先运行 python main.py serve") from exc

    backend = HttpBackend(server_url)
    try:
        asyncio.run(run_interactive_loop(backend, server_url, current_role))
    except KeyboardInterrupt:
        print("\n已退出对话。")
