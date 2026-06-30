"""Agent 构建包。"""

__all__ = [
    "build_main_agent",
    "build_subagent_agent",
]


def __getattr__(name: str):
    if name in __all__:
        from openhachimi_agent.agent import factory
        return getattr(factory, name)
    raise AttributeError(name)
