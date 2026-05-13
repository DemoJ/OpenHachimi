"""命令行交互逻辑。"""

import asyncio
import codecs
import json
import logging
import os
import threading
from typing import AsyncIterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.service.agent_service import AgentService

logger = logging.getLogger(__name__)

EXIT_COMMANDS = {"/exit", "/quit", "退出", "q"}
NEW_SESSION_COMMANDS = {"/new", "新对话"}
HELP_COMMANDS = {"/help", "帮助"}
ROLE_LIST_COMMANDS = {"/roles", "/list-roles"}
STOP_COMMANDS = {"/stop", "停止"}
DEFAULT_SERVER_URL = "http://127.0.0.1:8765"


def get_server_url() -> str:
    return os.getenv("OPENHACHIMI_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")


def request_json(server_url: str, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    logger.debug("http request method=%s path=%s server_url=%s", method, path, server_url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{server_url}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
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
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    with urlopen(request) as response:
        for line in response:
            line_str = line.decode("utf-8").strip()
            if line_str.startswith("data: "):
                data_str = line_str[6:]
                try:
                    data_json = json.loads(data_str)
                    if "error" in data_json:
                        raise URLError(data_json["error"])
                    if "text" in data_json:
                        yield data_json["text"]
                except json.JSONDecodeError:
                    pass


def error_detail(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except json.JSONDecodeError:
        return str(exc)
    return str(payload.get("detail", exc))


def print_welcome(state: dict[str, object], server_url: str, current_role: str, current_session_id: str) -> None:
    from openhachimi_agent.core.version import get_version

    print(f"OpenHachimi CLI Agent  v{get_version()}")
    print(f"服务地址：{server_url}")
    print(f"当前模型：{state['model']}")
    if state.get("base_url"):
        print(f"模型服务：{state['base_url']}")
    print(f"当前角色：{current_role}")
    print(f"当前会话：{current_session_id}")
    print("输入内容后回车即可对话。")
    print("可用命令：/help 查看帮助，/roles 查看角色，/role <名称> 切换角色，/new 新建对话，/exit 退出程序。")
    print()


def print_help() -> None:
    print("命令说明：")
    print("  /help   查看帮助信息")
    print("  /roles  查看可用角色列表")
    print("  /role   切换角色，例如 /role default")
    print("  /new    保存当前对话并新建一段对话")
    print("  /stop   中断当前正在执行的任务")
    print("  /exit   退出程序")
    print()


class CliBackend(Protocol):
    async def get_state(self) -> dict[str, object]: ...
    async def list_roles(self) -> list[str]: ...
    async def latest_session(self, role: str) -> tuple[str, str]: ...
    async def new_session(self, role: str) -> tuple[str, str, str]: ...
    async def stop_session(self, session_id: str) -> str: ...
    async def switch_role(self, role: str) -> tuple[str, str, str]: ...
    async def stream_message(self, message: str, role: str, session_id: str) -> AsyncIterator[str]: ...


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
        resp = self.service.latest_session(role)
        return resp.role, resp.session_id

    async def new_session(self, role: str) -> tuple[str, str, str]:
        resp = self.service.new_session(role)
        return resp.role, resp.session_id, resp.message

    async def stop_session(self, session_id: str) -> str:
        resp = await self.service.stop_session(session_id)
        return resp.message

    async def switch_role(self, role: str) -> tuple[str, str, str]:
        resp = self.service.switch_role(role)
        return resp.role, resp.session_id, resp.message

    async def stream_message(self, message: str, role: str, session_id: str) -> AsyncIterator[str]:
        async for chunk in self.service.stream_message(message, role, session_id):
            yield chunk


class HttpBackend:
    def __init__(self, server_url: str):
        self.server_url = server_url

    async def get_state(self) -> dict[str, object]:
        return await asyncio.to_thread(request_json, self.server_url, "GET", "/state")

    async def list_roles(self) -> list[str]:
        resp = await asyncio.to_thread(request_json, self.server_url, "GET", "/roles")
        return resp.get("roles", [])

    async def latest_session(self, role: str) -> tuple[str, str]:
        qs = urlencode({'role': role})
        resp = await asyncio.to_thread(request_json, self.server_url, "GET", f"/session/latest?{qs}")
        return resp["role"], resp["session_id"]

    async def new_session(self, role: str) -> tuple[str, str, str]:
        qs = urlencode({'role': role})
        resp = await asyncio.to_thread(request_json, self.server_url, "POST", f"/new?{qs}")
        return resp["role"], resp["session_id"], resp["message"]

    async def stop_session(self, session_id: str) -> str:
        try:
            resp = await asyncio.to_thread(request_json, self.server_url, "POST", "/stop", {"session_id": session_id})
            return resp.get("message", "")
        except HTTPError as exc:
            raise RuntimeError(error_detail(exc)) from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    async def switch_role(self, role: str) -> tuple[str, str, str]:
        try:
            resp = await asyncio.to_thread(request_json, self.server_url, "POST", "/role", {"role": role})
            return resp["role"], resp["session_id"], resp["message"]
        except HTTPError as exc:
            raise RuntimeError(error_detail(exc)) from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    async def stream_message(self, message: str, role: str, session_id: str) -> AsyncIterator[str]:
        q: asyncio.Queue[str | Exception | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run():
            try:
                for chunk in request_stream(self.server_url, "/chat/stream", {"message": message, "role": role, "session_id": session_id}):
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


async def run_interactive_loop(backend: CliBackend, server_url: str, current_role: str) -> None:
    try:
        current_role, current_session_id = await backend.latest_session(current_role)
        state = await backend.get_state()
    except Exception as exc:
        print(f"初始化失败：{exc}")
        return

    print_welcome(state, server_url, current_role, current_session_id)

    while True:
        try:
            user_input = await asyncio.to_thread(input, "你 > ")
            user_input = user_input.strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出对话。")
            return

        if not user_input:
            continue

        if user_input in EXIT_COMMANDS:
            print("已退出对话。")
            return

        if user_input in NEW_SESSION_COMMANDS:
            try:
                await backend.stop_session(current_session_id)
            except Exception:
                pass
            try:
                current_role, current_session_id, msg = await backend.new_session(current_role)
                print(msg)
            except Exception as exc:
                print(f"哈基米 > 新建会话失败：{exc}")
            continue

        if user_input in STOP_COMMANDS:
            try:
                msg = await backend.stop_session(current_session_id)
                print(msg)
            except Exception as exc:
                print(f"哈基米 > 停止任务失败：{exc}")
            continue

        if user_input in HELP_COMMANDS:
            print_help()
            continue

        if user_input in ROLE_LIST_COMMANDS:
            try:
                roles = await backend.list_roles()
                print("可用角色：")
                for r in roles:
                    marker = "（当前）" if r == current_role else ""
                    print(f"  - {r}{marker}")
                print()
            except Exception as exc:
                print(f"哈基米 > 获取角色列表失败：{exc}")
            continue

        if user_input == "/role" or user_input.startswith("/role "):
            role_name = user_input[6:].strip()
            if not role_name:
                print("请在 /role 后面填写角色名称，例如：/role default\n")
                continue
            try:
                await backend.stop_session(current_session_id)
            except Exception:
                pass
            try:
                current_role, current_session_id, msg = await backend.switch_role(role_name)
                print(msg)
            except Exception as exc:
                print(f"哈基米 > 切换角色失败：{exc}")
            print()
            continue

        try:
            print("哈基米 > ", end="", flush=True)
            async for chunk in backend.stream_message(user_input, current_role, current_session_id):
                print(chunk, end="", flush=True)
        except Exception as exc:
            print(f"\n哈基米 > 调用模型时出错：{exc}")
        
        print("\n")


async def run_embedded_cli() -> None:
    config = load_config()
    configure_logging(config)
    logger.info("starting embedded cli")
    service = AgentService(config)
    backend = EmbeddedBackend(service)
    await run_interactive_loop(backend, "embedded", config.default_role_name)


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
        raise SystemExit(f"无法连接 OpenHachimi 后台服务：{server_url}，请先运行 python main.py serve") from exc

    backend = HttpBackend(server_url)
    try:
        asyncio.run(run_interactive_loop(backend, server_url, current_role))
    except KeyboardInterrupt:
        print("\n已退出对话。")
