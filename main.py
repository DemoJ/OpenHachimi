"""OpenHachimi 的命令行对话入口。"""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
ROLES_DIR = BASE_DIR / "roles"
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-5.2")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
DEFAULT_ROLE_NAME = os.getenv("OPENHACHIMI_ROLE", "default")
EXIT_COMMANDS = {"/exit", "/quit", "退出", "q"}
CLEAR_COMMANDS = {"/clear", "/reset", "清空"}
HELP_COMMANDS = {"/help", "帮助"}
ROLE_LIST_COMMANDS = {"/roles", "/list-roles"}


def list_role_names() -> list[str]:
    """列出当前 `roles` 目录下所有可用角色名称。"""
    if not ROLES_DIR.exists():
        return []

    return sorted(file.stem for file in ROLES_DIR.glob("*.md") if file.is_file())


def load_role_content(role_name: str) -> str:
    """从 Markdown 文件中加载角色配置内容。"""
    role_path = ROLES_DIR / f"{role_name}.md"
    if not role_path.exists():
        available_roles = "、".join(list_role_names()) or "无"
        raise FileNotFoundError(
            f"未找到角色配置：{role_name}。当前可用角色：{available_roles}"
        )

    content = role_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"角色配置文件为空：{role_path.name}")

    return content


def build_agent(role_name: str) -> Agent:
    """根据指定角色创建 Agent。"""
    role_content = load_role_content(role_name)
    provider = OpenAIProvider(
        base_url=OPENAI_BASE_URL or None,
        api_key=os.getenv("OPENAI_API_KEY"),
    )

    return Agent(
        OpenAIModel(MODEL_NAME, provider=provider),
        instructions=role_content,
        defer_model_check=True,
    )


def print_welcome(role_name: str) -> None:
    """打印命令行欢迎信息。"""
    print("OpenHachimi CLI Agent")
    print(f"当前模型：{MODEL_NAME}")
    print(f"当前 Base URL：{OPENAI_BASE_URL or '官方默认地址'}")
    print(f"当前角色：{role_name}")
    print("输入内容后回车即可对话。")
    print("可用命令：/help 查看帮助，/roles 查看角色，/role <名称> 切换角色，/clear 清空上下文，/exit 退出程序。")
    print()


def print_help() -> None:
    """打印命令帮助。"""
    print("命令说明：")
    print("  /help   查看帮助信息")
    print("  /roles  查看可用角色列表")
    print("  /role   切换角色，例如 /role default")
    print("  /clear  清空当前会话上下文")
    print("  /exit   退出程序")
    print()


def print_roles(current_role: str) -> None:
    """打印角色列表，并标记当前使用中的角色。"""
    role_names = list_role_names()
    if not role_names:
        print("当前没有可用的角色配置，请先在 roles 目录下创建 .md 文件。")
        print()
        return

    print("可用角色：")
    for role_name in role_names:
        marker = "（当前）" if role_name == current_role else ""
        print(f"  - {role_name}{marker}")
    print()


def ensure_api_key() -> None:
    """确认运行前已经配置 API Key。"""
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("请先在环境变量或 .env 文件中设置 OPENAI_API_KEY。")


def chat_loop() -> None:
    """启动一个带上下文记忆的命令行对话循环。"""
    ensure_api_key()
    current_role = DEFAULT_ROLE_NAME
    agent = build_agent(current_role)
    history: list[ModelMessage] = []

    print_welcome(current_role)

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

        if user_input in CLEAR_COMMANDS:
            history = []
            print("已清空当前会话上下文。")
            continue

        if user_input in HELP_COMMANDS:
            print_help()
            continue

        if user_input in ROLE_LIST_COMMANDS:
            print_roles(current_role)
            continue

        if user_input == "/role":
            print("请在 /role 后面填写角色名称，例如：/role default")
            print()
            continue

        if user_input.startswith("/role "):
            next_role = user_input[6:].strip()
            if not next_role:
                print("请在 /role 后面填写角色名称，例如：/role default")
                print()
                continue

            try:
                agent = build_agent(next_role)
            except (FileNotFoundError, ValueError) as exc:
                print(f"助手 > 切换角色失败：{exc}")
                print()
                continue

            current_role = next_role
            history = []
            print(f"已切换到角色：{current_role}，并清空了当前会话上下文。")
            print()
            continue

        try:
            result = agent.run_sync(user_input, message_history=history)
        except Exception as exc:
            print(f"助手 > 调用模型时出错：{exc}")
            continue

        history = list(result.all_messages())
        print(f"助手 > {result.output}")
        print()


if __name__ == "__main__":
    chat_loop()
