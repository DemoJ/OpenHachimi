"""Agent 构建包。"""

__all__ = [
    "build_router_agent",
    "build_planner_agent",
    "build_executor_agent",
    "build_scheduled_executor_agent",
]


def __getattr__(name: str):
    if name in __all__:
        from openhachimi_agent.agent import factory
        return getattr(factory, name)
    raise AttributeError(name)


def __getattr__(name: str):
    if name in __all__:
        from openhachimi_agent.agent import factory
        return getattr(factory, name)
    raise AttributeError(name)
