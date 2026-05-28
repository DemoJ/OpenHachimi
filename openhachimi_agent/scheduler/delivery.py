"""定时任务结果投递系统。

支持多渠道投递（Telegram、CLI、Inbox），包含：
- DeliveryMessage：投递消息结构
- DeliveryResult：单次投递结果
- DeliverySender：发送者协议
- DeliverySenderRegistry：发送者注册表
- DeliveryResolver：目标解析器
- 具体 Sender 实现：InboxSender、TelegramSender、CliSender
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from openhachimi_agent.scheduler.models import ScheduledRun, ScheduledTask
from openhachimi_agent.scheduler.store import ScheduledTaskStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryMessage:
    """投递消息内容。"""
    task_id: str
    task_name: str
    run_id: str
    status: str
    output: str | None
    error: str | None
    duration_ms: int | None

    def format_text(self) -> str:
        """格式化为可读文本。"""
        if self.status == "succeeded" and self.output:
            return f"[定时任务：{self.task_name}]\n\n{self.output}"
        error_text = self.error or self.status
        return f"[定时任务：{self.task_name}] 执行失败：{error_text}"


@dataclass(frozen=True)
class DeliveryResult:
    """单次投递结果。"""
    target: dict[str, Any]
    canonical_key: str
    status: str  # delivered | failed | skipped
    delivered_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DeliverySender(Protocol):
    """投递发送者协议。"""

    @property
    def type(self) -> str:
        """发送者类型标识，如 'telegram'、'cli'、'inbox'。"""
        ...

    def canonical_key(self, target: dict[str, Any]) -> str:
        """返回目标的唯一标识，用于去重。"""
        ...

    async def send(self, target: dict[str, Any], message: DeliveryMessage) -> DeliveryResult:
        """发送消息到指定目标。"""
        ...


class InboxDeliverySender:
    """Inbox 投递：不外发，只记录结果，使 run 可被 inbox 查询。"""

    @property
    def type(self) -> str:
        return "inbox"

    def canonical_key(self, target: dict[str, Any]) -> str:
        box = target.get("box", "default")
        return f"inbox:{box}"

    async def send(self, target: dict[str, Any], message: DeliveryMessage) -> DeliveryResult:
        return DeliveryResult(
            target=target,
            canonical_key=self.canonical_key(target),
            status="delivered",
            delivered_at=datetime.now(timezone.utc),
        )


class TelegramDeliverySender:
    """Telegram 投递：通过 bot send_message 发送。"""

    def __init__(self, sender: Any) -> None:
        self._sender = sender

    @property
    def type(self) -> str:
        return "telegram"

    def canonical_key(self, target: dict[str, Any]) -> str:
        chat_id = target.get("chat_id")
        thread_id = target.get("thread_id")
        return f"telegram:{chat_id}:{thread_id}"

    async def send(self, target: dict[str, Any], message: DeliveryMessage) -> DeliveryResult:
        chat_id = target.get("chat_id")
        if chat_id is None:
            return DeliveryResult(
                target=target,
                canonical_key=self.canonical_key(target),
                status="failed",
                error="chat_id is required",
            )
        thread_id = target.get("thread_id")
        text = message.format_text()
        try:
            await self._sender(int(chat_id), text, _optional_int(thread_id))
            return DeliveryResult(
                target=target,
                canonical_key=self.canonical_key(target),
                status="delivered",
                delivered_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.exception("telegram delivery failed chat_id=%s", chat_id)
            return DeliveryResult(
                target=target,
                canonical_key=self.canonical_key(target),
                status="failed",
                error=str(exc),
            )


class CliDeliverySender:
    """CLI 投递：embedded CLI 在线时打印，否则失败让 fallback 接管。"""

    def __init__(self, printer: Any | None = None) -> None:
        self._printer = printer

    @property
    def type(self) -> str:
        return "cli"

    def canonical_key(self, target: dict[str, Any]) -> str:
        return "cli:embedded"

    async def send(self, target: dict[str, Any], message: DeliveryMessage) -> DeliveryResult:
        if self._printer is None:
            return DeliveryResult(
                target=target,
                canonical_key=self.canonical_key(target),
                status="failed",
                error="CLI printer not available",
            )
        text = message.format_text()
        try:
            self._printer(f"\n\n哈基米 > {text}\n")
            return DeliveryResult(
                target=target,
                canonical_key=self.canonical_key(target),
                status="delivered",
                delivered_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.exception("cli delivery failed")
            return DeliveryResult(
                target=target,
                canonical_key=self.canonical_key(target),
                status="failed",
                error=str(exc),
            )


class DeliverySenderRegistry:
    """发送者注册表。"""

    def __init__(self) -> None:
        self._senders: dict[str, DeliverySender] = {}

    def register(self, sender: DeliverySender) -> None:
        """注册发送者。"""
        self._senders[sender.type] = sender

    def get(self, target_type: str) -> DeliverySender | None:
        """获取指定类型的发送者。"""
        return self._senders.get(target_type)

    async def send(self, target: dict[str, Any], message: DeliveryMessage) -> DeliveryResult:
        """发送消息到指定目标。"""
        target_type = str(target.get("type") or "inbox")
        sender = self._senders.get(target_type)
        if sender is None:
            return DeliveryResult(
                target=target,
                canonical_key=f"{target_type}:unknown",
                status="failed",
                error=f"no sender registered for type: {target_type}",
            )
        return await sender.send(target, message)


class DeliveryResolver:
    """投递目标解析器。"""

    @staticmethod
    def resolve(
        task: ScheduledTask,
        run: ScheduledRun,
        config: Any,
    ) -> list[dict[str, Any]]:
        """根据 delivery_mode 解析实际投递目标列表。"""
        mode = task.delivery_mode or "origin"
        targets: list[dict[str, Any]] = []

        if mode == "none":
            return []

        if mode == "origin":
            origin = task.origin or {}
            if origin:
                origin_target = dict(origin)
                origin_type = origin.get("type") or origin.get("platform")
                if origin_type:
                    origin_target["type"] = str(origin_type)
                    targets.append(origin_target)
            if not targets:
                targets.append({"type": "inbox", "box": "default"})

        elif mode == "inbox":
            targets.append({"type": "inbox", "box": "default"})

        elif mode == "explicit":
            targets = list(task.delivery_targets or [])

        elif mode == "platform_home":
            home_targets = getattr(config.scheduler.delivery, "home_targets", [])
            targets = list(home_targets)

        elif mode == "all":
            seen_keys: set[str] = set()
            origin = task.origin or {}
            if origin:
                origin_target = dict(origin)
                origin_type = origin.get("type") or origin.get("platform")
                if origin_type:
                    origin_target["type"] = str(origin_type)
                    key = f"{origin_target.get('type')}:{origin_target.get('chat_id', '')}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        targets.append(origin_target)
            for explicit in task.delivery_targets or []:
                key = f"{explicit.get('type')}:{explicit.get('chat_id', '')}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    targets.append(explicit)
            home_targets = getattr(config.scheduler.delivery, "home_targets", [])
            for home in home_targets:
                key = f"{home.get('type')}:{home.get('chat_id', '')}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    targets.append(home)
            targets.append({"type": "inbox", "box": "default"})

        return targets


async def deliver_scheduled_run(
    task: ScheduledTask,
    run: ScheduledRun,
    *,
    store: ScheduledTaskStore,
    registry: DeliverySenderRegistry,
    config: Any,
) -> None:
    """执行投递流水线。

    1. 解析目标列表
    2. 去重（按 canonical_key）
    3. 逐个发送
    4. 处理 fallback
    5. 写回 run/task delivery 字段
    """
    message = DeliveryMessage(
        task_id=task.id,
        task_name=task.name,
        run_id=run.id,
        status=run.status,
        output=run.output,
        error=run.error,
        duration_ms=run.duration_ms,
    )

    resolver = DeliveryResolver()
    targets = resolver.resolve(task, run, config)

    if not targets:
        store.update_run_delivery(
            run.id,
            delivery_status="not_required",
            delivery_targets=[],
            delivery_results=[],
        )
        return

    seen_keys: set[str] = set()
    unique_targets: list[dict[str, Any]] = []
    for target in targets:
        target_type = str(target.get("type") or "inbox")
        sender = registry.get(target_type)
        if sender is None:
            key = f"{target_type}:unknown"
        else:
            key = sender.canonical_key(target)
        if key not in seen_keys:
            seen_keys.add(key)
            unique_targets.append(target)

    results: list[dict[str, Any]] = []
    delivered_count = 0
    failed_count = 0
    delivery_error: str | None = None

    for target in unique_targets:
        result = await registry.send(target, message)
        results.append({
            "target": result.target,
            "canonical_key": result.canonical_key,
            "status": result.status,
            "delivered_at": result.delivered_at.isoformat() if result.delivered_at else None,
            "error": result.error,
        })
        if result.status == "delivered":
            delivered_count += 1
        elif result.status == "failed":
            failed_count += 1
            delivery_error = result.error

    if delivered_count == 0 and failed_count > 0:
        delivery_status = "failed"
        fallback = task.delivery_fallback or {}
        if fallback.get("enabled", True):
            fallback_targets = fallback.get("targets", [{"type": "inbox", "box": "default"}])
            fallback_results: list[dict[str, Any]] = []
            fallback_delivered = 0
            for fb_target in fallback_targets:
                fb_result = await registry.send(fb_target, message)
                fallback_results.append({
                    "target": fb_result.target,
                    "canonical_key": fb_result.canonical_key,
                    "status": fb_result.status,
                    "delivered_at": fb_result.delivered_at.isoformat() if fb_result.delivered_at else None,
                    "error": fb_result.error,
                })
                if fb_result.status == "delivered":
                    fallback_delivered += 1
            if fallback_delivered > 0:
                delivery_status = "fallback_delivered"
                results.extend(fallback_results)
                delivered_count += fallback_delivered
            else:
                delivery_status = "fallback_failed"
    elif delivered_count > 0 and failed_count > 0:
        delivery_status = "partial"
    elif delivered_count > 0:
        delivery_status = "delivered"
    else:
        delivery_status = "resolved_empty"

    delivered_at = datetime.now(timezone.utc) if delivered_count > 0 else None

    store.update_run_delivery(
        run.id,
        delivery_status=delivery_status,
        delivery_targets=unique_targets,
        delivery_results=results,
        delivery_error=delivery_error,
        delivered_at=delivered_at,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
