"""工具目录摘要缓存与静态上下文池。

`AgentService` 持有 `_tool_catalog_cache` / `_context_static_pool`,这里提供接收
`service` 整体作参数的纯函数,负责:按 role + 签名缓存工具目录摘要;把静态
system prompt 段写入/读出进程内池(de facto 去重),池未命中时按 role 重建当前
版本。`AgentService` 内对应方法退化为薄壳。
"""

from __future__ import annotations

import logging

from openhachimi_agent.service.agent_runtime.context_snapshot import (
    _build_executor_static_context,
    _compute_static_hash,
    _extract_tool_catalog,
)

logger = logging.getLogger(__name__)


def get_cached_tool_catalog(service, role: str, executor_agent: object) -> str:
    """按 role + 当前签名返回缓存的工具目录摘要,未命中或过期则重建。

    ``context_snapshot`` 在 ``_build_executor_static_context`` 中调用此方法获取
    工具清单,不再每轮遍历所有 toolset。
    """
    key = f"{role}:executor"
    sig = (service._mcp_config_signature, service._agent_dependency_mtime_cache)
    cached = service._tool_catalog_cache.get(key)
    if cached is not None and cached[1] == sig:
        return cached[0]
    try:
        text = _extract_tool_catalog(executor_agent)
    except Exception:
        logger.debug("tool catalog extraction failed for role=%s", role, exc_info=True)
        text = ""
    service._tool_catalog_cache[key] = (text, sig)
    return text


def ensure_context_static(service, hash_key: str, text: str) -> None:
    """将静态 system prompt 段写入进程内池(de facto 去重)。"""
    if hash_key and text and not service._context_static_pool.get(hash_key):
        service._context_static_pool[hash_key] = text


def resolve_static_context(service, role: str | None, hash_key: str) -> str:
    """从进程内池按 hash 取出静态段;池中不存在时尝试按 role 重建当前版本。

    重建仅在 hash 与当前版本一致时写入池(避免攒入旧 hash),不一致时静默降级。
    """
    if not hash_key:
        return ""
    text = service._context_static_pool.get(hash_key)
    if text is not None:
        return text
    # 池中不存在:尝试重建当前版本的静态段,若哈希相同则写入池
    if role:
        try:
            executor_agent = service._get_agent(role, "executor")
        except Exception:
            return ""
        try:
            rebuilt = _build_executor_static_context(service.config, role, executor_agent, service=service)
            rebuilt_hash = _compute_static_hash(rebuilt)
            if rebuilt_hash == hash_key:
                service._context_static_pool[hash_key] = rebuilt
                return rebuilt
            # 哈希不匹配 → 配置或依赖已变,旧 hash 的静态段已无意义,不写入
        except Exception:
            logger.debug("failed to rebuild static context for hash=%s role=%s", hash_key, role, exc_info=True)
    return ""
