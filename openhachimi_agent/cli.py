"""命令行交互逻辑。"""

from pydantic_ai.messages import ModelMessage

from openhachimi_agent.agent import build_agent
from openhachimi_agent.config import AppConfig, load_config
from openhachimi_agent.memory import load_message_history, save_message_history, start_new_session
from openhachimi_agent.roles import list_role_names


EXIT_COMMANDS = {"/exit", "/quit", "退出", "q"}
NEW_SESSION_COMMANDS = {"/new", "新对话"}
HELP_COMMANDS = {"/help", "帮助"}
ROLE_LIST_COMMANDS = {"/roles", "/list-roles"}


def print_welcome(config: AppConfig, role_name: str) -> None:
    """打印命令行欢迎信息。"""
    print("OpenHachimi CLI Agent")
    print(f"当前模型：{config.model_name}")
    print(f"当前 Base URL：{config.openai_base_url or '官方默认地址'}")
    print(f"当前角色：{role_name}")
    print("输入内容后回车即可对话。")
    print("可用命令：/help 查看帮助，/roles 查看角色，/role <名称> 切换角色，/new 新建对话，/exit 退出程序。")
    print()


def print_help() -> None:
    """打印命令帮助。"""
    print("命令说明：")
    print("  /help   查看帮助信息")
    print("  /roles  查看可用角色列表")
    print("  /role   切换角色，例如 /role default")
    print("  /new    保存当前对话并新建一段对话")
    print("  /exit   退出程序")
    print()


def print_roles(config: AppConfig, current_role: str) -> None:
    """打印角色列表，并标记当前使用中的角色。"""
    role_names = list_role_names(config.roles_dir)
    if not role_names:
        print("当前没有可用的角色配置，请先在 roles 目录下创建 .md 文件。")
        print()
        return

    print("可用角色：")
    for role_name in role_names:
        marker = "（当前）" if role_name == current_role else ""
        print(f"  - {role_name}{marker}")
    print()


def ensure_api_key(config: AppConfig) -> None:
    """确认运行前已经配置 API Key。"""
    if not config.openai_api_key:
        raise SystemExit("请先在环境变量或 .env 文件中设置 OPENAI_API_KEY。")


def handle_role_switch(
    config: AppConfig,
    user_input: str,
    current_role: str,
) -> tuple[str, str, list[ModelMessage], bool] | None:
    """处理角色切换命令。"""
    if user_input == "/role":
        print("请在 /role 后面填写角色名称，例如：/role default")
        print()
        return current_role, "", [], False

    if not user_input.startswith("/role "):
        return None

    next_role = user_input[6:].strip()
    if not next_role:
        print("请在 /role 后面填写角色名称，例如：/role default")
        print()
        return current_role, "", [], False

    build_agent(config, next_role)
    session_id = start_new_session(config.memory_dir, next_role)
    print(f"已切换到角色：{next_role}，并新建对话。")
    print()
    return next_role, session_id, [], True


def run_cli(config: AppConfig | None = None) -> None:
    """启动一个带上下文记忆的命令行对话循环。"""
    config = config or load_config()
    ensure_api_key(config)

    current_role = config.default_role_name
    agent = build_agent(config, current_role)
    current_session_id, history = load_message_history(config.memory_dir, current_role)

    print_welcome(config, current_role)
    if history:
        print(f"已恢复角色 {current_role} 的历史会话。")
        print()

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
            current_session_id = start_new_session(config.memory_dir, current_role)
            history = []
            print("已保存上一段对话，并新建对话。")
            continue

        if user_input in HELP_COMMANDS:
            print_help()
            continue

        if user_input in ROLE_LIST_COMMANDS:
            print_roles(config, current_role)
            continue

        try:
            role_switch_result = handle_role_switch(config, user_input, current_role)
        except (FileNotFoundError, ValueError) as exc:
            print(f"助手 > 切换角色失败：{exc}")
            print()
            continue

        if role_switch_result is not None:
            next_role, next_session_id, next_history, switched = role_switch_result
            if switched:
                current_role = next_role
                current_session_id = next_session_id
                agent = build_agent(config, current_role)
                history = next_history
            continue

        try:
            result = agent.run_sync(user_input, message_history=history, deps=config)
        except Exception as exc:
            print(f"助手 > 调用模型时出错：{exc}")
            continue

        history = list(result.all_messages())
        save_message_history(config.memory_dir, current_role, current_session_id, result.all_messages_json())
        print(f"助手 > {result.output}")
        print()
