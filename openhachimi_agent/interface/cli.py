"""命令行交互逻辑。"""

import codecs
import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)


EXIT_COMMANDS = {"/exit", "/quit", "退出", "q"}
NEW_SESSION_COMMANDS = {"/new", "新对话"}
HELP_COMMANDS = {"/help", "帮助"}
ROLE_LIST_COMMANDS = {"/roles", "/list-roles"}
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
        headers={"Content-Type": "application/json"},
    )
    decoder = codecs.getincrementaldecoder("utf-8")()
    with urlopen(request) as response:
        while chunk := response.read(1):
            text = decoder.decode(chunk)
            if text:
                yield text
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail


def error_detail(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except json.JSONDecodeError:
        return str(exc)
    return str(payload.get("detail", exc))


def print_welcome(state: dict[str, object], server_url: str) -> None:
    from openhachimi_agent.core.version import get_version

    print(f"OpenHachimi CLI Agent  v{get_version()}")
    print(f"服务地址：{server_url}")
    print(f"当前模型：{state['model']}")
    if state.get("base_url"):
        print(f"模型服务：{state['base_url']}")
    print(f"当前角色：{state['role']}")
    print(f"当前会话：{state['session_id']}")
    print("输入内容后回车即可对话。")
    print("可用命令：/help 查看帮助，/roles 查看角色，/role <名称> 切换角色，/new 新建对话，/exit 退出程序。")
    print()


def state_payload(state: object) -> dict[str, object]:
    return {
        "role": state.role,
        "session_id": state.session_id,
        "has_history": state.has_history,
        "model": state.model,
        "base_url": state.base_url,
    }


async def run_embedded_cli() -> None:
    config = load_config()
    configure_logging(config)
    logger.info("starting embedded cli")
    service = AgentService(config)
    server_url = "embedded"
    print_welcome(state_payload(service.state()), server_url)

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出对话。")
            return

        if not user_input:
            continue

        if user_input in EXIT_COMMANDS:
            print("已退出对话。")
            return

        if user_input in NEW_SESSION_COMMANDS:
            print(service.new_session().message)
            continue

        if user_input in HELP_COMMANDS:
            print_help()
            continue

        if user_input in ROLE_LIST_COMMANDS:
            roles = service.list_roles()
            print("可用角色：")
            for role_name in roles.roles:
                marker = "（当前）" if role_name == roles.current_role else ""
                print(f"  - {role_name}{marker}")
            print()
            continue

        if user_input == "/role" or user_input.startswith("/role "):
            role_name = user_input[6:].strip()
            if not role_name:
                print("请在 /role 后面填写角色名称，例如：/role default")
                print()
                continue
            try:
                print(service.switch_role(role_name).message)
            except (FileNotFoundError, ValueError) as exc:
                print(f"助手 > 切换角色失败：{exc}")
            print()
            continue

        try:
            print("助手 > ", end="", flush=True)
            async for chunk in service.stream_message(user_input):
                print(chunk, end="", flush=True)
        except Exception as exc:
            print(f"\n助手 > 调用模型时出错：{exc}")
            continue

        print("\n")


def print_help() -> None:
    print("命令说明：")
    print("  /help   查看帮助信息")
    print("  /roles  查看可用角色列表")
    print("  /role   切换角色，例如 /role default")
    print("  /new    保存当前对话并新建一段对话")
    print("  /exit   退出程序")
    print()


def print_roles(server_url: str) -> None:
    payload = request_json(server_url, "GET", "/roles")

    print("可用角色：")
    for role_name in payload["roles"]:
        marker = "（当前）" if role_name == payload["current_role"] else ""
        print(f"  - {role_name}{marker}")
    print()


def switch_role(server_url: str, user_input: str) -> None:
    if user_input == "/role":
        print("请在 /role 后面填写角色名称，例如：/role default")
        print()
        return

    role_name = user_input[6:].strip()
    if not role_name:
        print("请在 /role 后面填写角色名称，例如：/role default")
        print()
        return

    try:
        payload = request_json(server_url, "POST", "/role", {"role": role_name})
    except HTTPError as exc:
        print(f"助手 > 切换角色失败：{error_detail(exc)}")
        print()
        return

    print(payload["message"])
    print()


def run_cli() -> None:
    try:
        configure_logging(load_config())
    except Exception:
        logging.basicConfig(level=logging.INFO)
        logger.exception("failed to configure logging from local config")
    server_url = get_server_url()
    logger.info("starting cli client server_url=%s", server_url)
    try:
        state = request_json(server_url, "GET", "/state")
    except URLError as exc:
        raise SystemExit(f"无法连接 OpenHachimi 后台服务：{server_url}，请先运行 python main.py serve") from exc

    print_welcome(state, server_url)

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出对话。")
            return

        if not user_input:
            continue

        if user_input in EXIT_COMMANDS:
            print("已退出对话。")
            return

        if user_input in NEW_SESSION_COMMANDS:
            payload = request_json(server_url, "POST", "/new")
            print(payload["message"])
            continue

        if user_input in HELP_COMMANDS:
            print_help()
            continue

        if user_input in ROLE_LIST_COMMANDS:
            print_roles(server_url)
            continue

        if user_input == "/role" or user_input.startswith("/role "):
            switch_role(server_url, user_input)
            continue

        try:
            print("助手 > ", end="", flush=True)
            for chunk in request_stream(server_url, "/chat/stream", {"message": user_input}):
                print(chunk, end="", flush=True)
        except HTTPError as exc:
            print(f"\n助手 > 调用模型时出错：{error_detail(exc)}")
            continue
        except URLError as exc:
            print(f"\n助手 > 调用模型时出错：{exc}")
            continue

        print("\n")
