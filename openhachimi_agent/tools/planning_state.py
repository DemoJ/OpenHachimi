"""TODO 计划的数据模型、状态取值与会话内持久化。

从 ``planning.py`` 原样拆出,行为不变。``planning.py`` 仍然重新导出这里的名字,
旧的 ``from openhachimi_agent.tools.planning import TodoState`` 等写法继续可用。
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai import RunContext
from typing_extensions import TypedDict

from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)


class TodoTaskInput(TypedDict, total=False):
    id: int
    description: str

@dataclass
class TodoTask:
    id: int
    description: str
    status: Literal["pending", "in-progress", "done", "blocked"] = "pending"
    notes: str = ""
    # 依赖图已取消,不再校验也不再展示。字段保留只为读取旧会话存下的计划时不出错。
    depends_on: list[int] = field(default_factory=list)
    evidence: str = ""

@dataclass
class TodoState:
    goal: str = ""
    invariants: list[str] = field(default_factory=list)
    tasks: dict[int, TodoTask] = field(default_factory=dict)
    tool_calls_since_update: int = 0
    is_active: bool = False

def _load_state(ctx: RunContext[AgentDeps]) -> TodoState:
    """从 SessionStore 加载本会话 TODO 状态。

    旧实现读 ``{memory_dir}/todos/{session_id}.json``,现在改走
    ``ctx.deps.session_store.load_todo_state``。Store 内部已经把"文件不存在 /
    JSON 解析失败 / 字段类型异常"统一兜底为空 ``TodoState()`` + log warning,
    与原本 ``_load_state`` 的健壮性语义一致。
    """
    store = getattr(ctx.deps, "session_store", None)
    if store is None:
        # 离线/构造期 deps 无 session_store:返回空,与旧路径"文件不存在"等价
        return TodoState()
    return store.load_todo_state(ctx.deps.session_id)

def _save_state(ctx: RunContext[AgentDeps], state: TodoState):
    """把当前 TODO 状态写回 SessionStore。"""
    store = getattr(ctx.deps, "session_store", None)
    if store is None:
        logger.warning("session_store missing on deps, TODO state will not be persisted")
        return
    try:
        store.save_todo_state(ctx.deps.session_id, state)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to save TODO state session_id=%s: %s", ctx.deps.session_id, e)

def _get_state(ctx: RunContext[AgentDeps]) -> TodoState:
    session_state = ctx.deps.session_state
    if "todo_state" not in session_state:
        session_state["todo_state"] = _load_state(ctx)
    return session_state["todo_state"]


# 还没结束的状态。done 是完成,blocked 是模型如实声明走不通,两者都算结束。
OPEN_STATUSES = frozenset({"pending", "in-progress"})


def has_open_tasks(tasks: object) -> bool:
    """任务字典里是否还有 pending / in-progress 项。全仓库判断"计划是否还在进行"都用它。"""
    if not isinstance(tasks, dict):
        return False
    return any(getattr(task, "status", None) in OPEN_STATUSES for task in tasks.values())


def _refresh_active_flag(state: TodoState) -> None:
    # 只剩 done / blocked 时关闭计划;否则一个 blocked 项会让计划永远挂着,
    # 之后每轮都被注入"先执行 TODO"的接力规则,新计划也建不了。
    if state.tasks and not has_open_tasks(state.tasks):
        state.is_active = False


# status 归一化:模型常写 "in_progress"/"completed"/"finished"/"cancelled" 等。
# 用别名表统一映射到 schema 接受的 4 个值,避免每次 LLM 拼写差异都触发 retry。
_STATUS_ALIASES = {
    "pending": "pending",
    "todo": "pending",
    "open": "pending",
    "waiting": "pending",
    "in-progress": "in-progress",
    "in_progress": "in-progress",
    "in progress": "in-progress",
    "inprogress": "in-progress",
    "running": "in-progress",
    "doing": "in-progress",
    "wip": "in-progress",
    "started": "in-progress",
    "done": "done",
    "completed": "done",
    "complete": "done",
    "finished": "done",
    "success": "done",
    "ok": "done",
    "resolved": "done",
    "closed": "done",
    "blocked": "blocked",
    "block": "blocked",
    "stuck": "blocked",
    "failed": "blocked",
    "fail": "blocked",
    "cancelled": "blocked",
    "canceled": "blocked",
    "skipped": "blocked",
    "abandoned": "blocked",
}


def normalize_status(status: object) -> str | None:
    """把模型给的 status 归一到 pending / in-progress / done / blocked;认不出返回 None。"""
    return _STATUS_ALIASES.get(str(status).strip().lower().replace("　", " "))
