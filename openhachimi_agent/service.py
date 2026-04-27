"""Agent 后台服务层。"""

from pydantic_ai.messages import ModelMessage

from openhachimi_agent.agent import build_agent
from openhachimi_agent.api_models import AgentState, ChatResponse, CommandResponse, RolesResponse
from openhachimi_agent.config import AppConfig
from openhachimi_agent.memory import load_message_history, save_message_history, start_new_session
from openhachimi_agent.roles import list_role_names


class AgentService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.current_role = config.default_role_name
        self.agent = build_agent(config, self.current_role)
        self.current_session_id, self.history = load_message_history(config.memory_dir, self.current_role)

    def state(self) -> AgentState:
        return AgentState(
            role=self.current_role,
            session_id=self.current_session_id,
            has_history=bool(self.history),
        )

    def list_roles(self) -> RolesResponse:
        return RolesResponse(
            roles=list_role_names(self.config.roles_dir),
            current_role=self.current_role,
        )

    def new_session(self) -> CommandResponse:
        self.current_session_id = start_new_session(self.config.memory_dir, self.current_role)
        self.history: list[ModelMessage] = []
        return CommandResponse(
            message="已保存上一段对话，并新建对话。",
            role=self.current_role,
            session_id=self.current_session_id,
        )

    def switch_role(self, role_name: str) -> CommandResponse:
        self.agent = build_agent(self.config, role_name)
        self.current_role = role_name
        self.current_session_id = start_new_session(self.config.memory_dir, self.current_role)
        self.history = []
        return CommandResponse(
            message=f"已切换到角色：{role_name}，并新建对话。",
            role=self.current_role,
            session_id=self.current_session_id,
        )

    def send_message(self, message: str) -> ChatResponse:
        result = self.agent.run_sync(message, message_history=self.history, deps=self.config)
        self.history = list(result.all_messages())
        save_message_history(
            self.config.memory_dir,
            self.current_role,
            self.current_session_id,
            result.all_messages_json(),
        )
        return ChatResponse(
            output=result.output,
            role=self.current_role,
            session_id=self.current_session_id,
        )
