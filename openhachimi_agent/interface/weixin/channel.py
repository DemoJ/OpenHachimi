"""微信 iLink 协议的原生渠道接入。"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.interface.weixin.ilink_client import TYPING_STATUS_CANCEL, TYPING_STATUS_START, WeixinClient
from openhachimi_agent.service.agent_service import AgentService

logger = logging.getLogger(__name__)

# 微信账号凭证文件的相对路径名（相对于 config.base_dir）
_ACCOUNT_REL_PATH = Path(".memory") / "weixin_account.json"


def _account_file(config: AppConfig) -> Path:
    """基于项目根目录返回微信账号凭证文件的绝对路径。"""
    return config.base_dir / _ACCOUNT_REL_PATH


class WeixinChannel:
    def __init__(self, service: AgentService, config: AppConfig):
        self.service = service
        self.config = config
        self.client = WeixinClient()
        self.sync_buf = ""
        self.account_id = ""

    @property
    def account_path(self) -> Path:
        return _account_file(self.config)

    async def _load_account(self) -> bool:
        path = self.account_path
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                self.client.token = data.get("token")
                self.account_id = data.get("account_id", "")
                if self.client.token:
                    logger.info("已加载微信凭证：%s", path)
                    return True
                else:
                    logger.warning("微信凭证文件存在但缺少 token：%s", path)
            except Exception as e:
                logger.warning("加载微信账号失败：%s", e)
        else:
            logger.warning("微信凭证文件不存在：%s", path)
        return False

    async def _handle_message(self, msg: Dict[str, Any]):
        try:
            # 只处理入站消息（message_type == 1），避免处理自己发出的消息
            message_type = msg.get("message_type")
            if message_type != 1:
                logger.debug("跳过非入站消息 message_type=%s", message_type)
                return

            from_user = msg.get("from_user_id", "")
            if not from_user:
                logger.warning("消息缺少 from_user_id")
                return

            # 提取文本内容
            items = msg.get("item_list", [])
            text_content = ""
            for item in items:
                if item.get("type") == 1:  # ITEM_TEXT
                    text_content += (item.get("text_item") or {}).get("text", "")

            if not text_content.strip():
                logger.debug("跳过空消息")
                return

            # 会话键：优先使用 group_id（群聊），否则使用 from_user_id（私聊）
            group_id = msg.get("group_id", "")
            session_id = msg.get("session_id", "")
            session_key = group_id if group_id else from_user

            # 构造合法的 session_scope_key（只允许字母数字点下划线短横线冒号）
            # 将特殊字符替换为下划线
            safe_session_key = session_key.replace("@", "_at_").replace("-", "_")
            scope_key = f"wx_{safe_session_key}"

            context_token = msg.get("context_token", "")

            channel_context = {
                "type": "weixin",
                "platform": "weixin",
                "session_scope_key": scope_key,
            }

            logger.info("收到微信消息 来自 %s: %s", from_user, text_content[:50])

            # 回复目标：如果有 group_id 则回复到群，否则回复给个人
            to_user = group_id if group_id else from_user

            # 获取 typing_ticket 并启动"正在输入"指示器
            typing_ticket = await self.client.get_typing_ticket(to_user)
            typing_task = None
            if typing_ticket:
                try:
                    await self.client.send_typing(to_user, typing_ticket, TYPING_STATUS_START)
                    # 启动后台任务每 5 秒刷新 typing 状态
                    async def _keep_typing():
                        while True:
                            await asyncio.sleep(5)
                            try:
                                await self.client.send_typing(to_user, typing_ticket, TYPING_STATUS_START)
                            except Exception as e:
                                logger.debug("刷新 typing 状态失败: %s", e)
                                break
                    typing_task = asyncio.create_task(_keep_typing())
                    logger.debug("已启动 typing 指示器 for %s", to_user)
                except Exception as e:
                    logger.debug("启动 typing 指示器失败: %s", e)

            try:
                response = await self.service.send_message(
                    message=text_content,
                    role=self.config.default_role_name,
                    session_id=None,
                    channel_context=channel_context,
                    channel="weixin",
                )

                # 取消 typing 指示器
                if typing_task:
                    typing_task.cancel()
                    try:
                        await typing_task
                    except asyncio.CancelledError:
                        pass
                if typing_ticket:
                    try:
                        await self.client.send_typing(to_user, typing_ticket, TYPING_STATUS_CANCEL)
                        logger.debug("已取消 typing 指示器 for %s", to_user)
                    except Exception as e:
                        logger.debug("取消 typing 指示器失败: %s", e)

                # 发送回复消息
                client_id = f"openhachimi-{uuid.uuid4().hex[:8]}"
                await self.client.send_message(
                    to_user_id=to_user,
                    text=response.output,
                    context_token=context_token,
                    client_id=client_id
                )
                logger.info("已回复微信消息给 %s", to_user)
            except Exception:
                # 确保即使出错也取消 typing 指示器
                if typing_task:
                    typing_task.cancel()
                    try:
                        await typing_task
                    except asyncio.CancelledError:
                        pass
                if typing_ticket:
                    try:
                        await self.client.send_typing(to_user, typing_ticket, TYPING_STATUS_CANCEL)
                    except Exception:
                        pass
                raise
        except Exception as e:
            logger.exception("处理微信消息时出错：%s", msg)

    async def run_loop(self):
        if not await self._load_account():
            logger.warning("微信 token 缺失，请运行 `hachimi weixin` 登录。微信渠道将保持未激活状态。")
            return

        if not self.client.token:
            return

        logger.info("微信渠道轮询循环已启动，正在监听消息...")
        error_count = 0

        while True:
            try:
                updates = await self.client.get_updates(self.sync_buf)
                ret = updates.get("ret")
                errcode = updates.get("errcode")

                # 会话过期：部分 iLink 响应会带 ret/errmsg，正常 get_updates 也可能不带 ret。
                if ret in (-14, -2) and (updates.get("errmsg", "").lower() == "unknown error" or ret == -14):
                    logger.warning("微信会话已过期，请运行 `hachimi weixin` 重新登录。")
                    path = self.account_path
                    if path.exists():
                        path.unlink()
                    self.client.token = None
                    break

                # 成功条件：ret 为 0 或 None，且 errcode 为 0 或 None
                if ret not in (0, None) or errcode not in (0, None):
                    logger.error("微信 get_updates 错误：%s", updates)
                    error_count += 1
                    await asyncio.sleep(min(30, error_count * 2))
                    continue

                error_count = 0
                # 只使用 get_updates_buf 作为游标
                if updates.get("get_updates_buf"):
                    self.sync_buf = updates["get_updates_buf"]

                msgs = updates.get("msgs", [])
                for m in msgs:
                    # 不阻塞主轮询
                    asyncio.create_task(self._handle_message(m))

            except Exception as e:
                logger.error("微信轮询异常：%s", e)
                error_count += 1
                await asyncio.sleep(min(30, error_count * 2))


@asynccontextmanager
async def weixin_lifespan(app):
    config: AppConfig = app.state.config
    service: AgentService = app.state.service

    account_path = _account_file(config)
    channel_task = None

    if account_path.exists():
        logger.info("检测到微信账号文件 (%s)，正在启动微信渠道...", account_path)
        channel = WeixinChannel(service, config)
        channel_task = asyncio.create_task(channel.run_loop())
    else:
        logger.info(
            "微信账号文件不存在 (%s)，微信渠道未启动。如需使用微信渠道，请运行 `hachimi weixin` 登录。",
            account_path,
        )

    yield

    if channel_task:
        channel_task.cancel()
        try:
            await channel_task
        except asyncio.CancelledError:
            pass
