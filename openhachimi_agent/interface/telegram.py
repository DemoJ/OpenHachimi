"""Telegram Bot 接入渠道。

Bot 作为后台任务嵌入 FastAPI 的 lifespan，随 HTTP 服务一同启停。
只要 config.app.telegram_bot_token 有值，Bot 就会在 `hachimi serve` 时自动上线。

每个 Telegram 用户拥有独立的角色和会话上下文，互不干扰。
消息回复采用「分段编辑」方式实现打字流式效果，完成后转换为 HTML 格式渲染。
"""

import html
import logging
import re
import time
import asyncio
import contextlib
import shutil
from collections.abc import AsyncIterator, Callable, Awaitable
from contextlib import asynccontextmanager

from telegram import Update, constants
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.redaction import redact_exception
from openhachimi_agent.interface.presenter import ToolProgressPresenter
from openhachimi_agent.service.agent_runtime.command_registry import (
    CommandOutcome,
    iter_for_tg_menu,
)
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.storage.attachments import AttachmentError, AttachmentStorage
from openhachimi_agent.tools.utils import resolve_workspace_path
from openhachimi_agent.transport.api_models import ArtifactRef, AttachmentRef


logger = logging.getLogger(__name__)

# 流式编辑的最小间隔（秒），避免触发 Telegram API 限流（每分钟约 20 次编辑）
_EDIT_INTERVAL = 1.5
# 单条 Telegram 消息最大字符数（官方限制 4096，留余量）
_MAX_MSG_LEN = 4000
# Telegram typing 状态持续时间较短，需要周期性刷新
_TYPING_INTERVAL = 4.0


def _exception_text(exc: BaseException) -> str:
    return redact_exception(exc)


def _is_message_not_modified(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


def _split_long_text(text: str) -> list[str]:
    """将超长文本切分为多段，每段不超过 _MAX_MSG_LEN 字符。"""
    parts: list[str] = []
    while len(text) > _MAX_MSG_LEN:
        # 尽量在换行处切分
        cut = text.rfind("\n", 0, _MAX_MSG_LEN)
        if cut <= 0:
            cut = _MAX_MSG_LEN
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def _md_to_tg_html(text: str) -> str:
    """将 Markdown 文本转换为 Telegram 支持的 HTML 格式。

    处理顺序：
    1. 提取代码块和行内代码（保护内容不被其他规则误处理）
    2. 对普通文本进行 HTML 字符转义（& < > → &amp; &lt; &gt;）
    3. 应用加粗、斜体、标题、删除线、链接等格式规则
    4. 还原代码块
    """
    saved: list[str] = []

    def save(fragment: str) -> str:
        idx = len(saved)
        saved.append(fragment)
        return f"\x00SAVED{idx}\x00"

    # ① 多行代码块（```lang\n...\n```）
    def replace_code_block(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = html.escape(m.group(2))
        if lang:
            tag = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            tag = f"<pre>{code}</pre>"
        return save(tag)

    text = re.sub(r"```(\w*)\n?(.*?)```", replace_code_block, text, flags=re.DOTALL)

    # ② 行内代码（`...`）
    def replace_inline_code(m: re.Match) -> str:
        return save(f"<code>{html.escape(m.group(1))}</code>")

    text = re.sub(r"`([^`\n]+)`", replace_inline_code, text)

    # ③ 对剩余普通文本进行 HTML 字符转义
    text = html.escape(text)

    # ④ 标题（# ## ### → 加粗）
    text = re.sub(r"^#{1,3} (.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # ⑤ 加粗（**text** 或 __text__）
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)

    # ⑥ 斜体（*text* 或 _text_，避免误匹配 ** 和 __）
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", text)

    # ⑦ 删除线（~~text~~）
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # ⑧ 链接（[text](url)）
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)

    # ⑨ 还原代码块占位符
    for i, fragment in enumerate(saved):
        text = text.replace(f"\x00SAVED{i}\x00", fragment)

    return text


def _live_text(text: str) -> str:
    if len(text) <= _MAX_MSG_LEN - 2:
        return text
    return "…\n" + text[-(_MAX_MSG_LEN - 4):]


async def _keep_typing(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    while True:
        await ctx.bot.send_chat_action(chat_id=chat.id, action=constants.ChatAction.TYPING)
        await asyncio.sleep(_TYPING_INTERVAL)


class TelegramBot:
    """Telegram Bot 主体，封装会话状态与消息处理逻辑。"""

    def __init__(self, config: AppConfig, service: AgentService) -> None:
        self.config = config
        self.service = service
        # 按 chat/thread/user scope 存储各自的 {role, session_id, scope}
        self._sessions: dict[str, dict[str, str]] = {}
        # 按 scope 存储队列锁，实现每个会话范围独立的消息排队机制
        self._user_locks: dict[str, asyncio.Lock] = {}
        self.attachment_storage = AttachmentStorage(
            config.attachments_dir,
            config.max_attachment_size_bytes,
            config.allowed_attachment_mime_types,
            config.base_dir,
        )
        logger.info("telegram bot handler initialized")

    def _session_key(self, update: Update) -> str:
        chat_id = update.effective_chat.id if update.effective_chat else getattr(update.message, "chat_id", 0)
        user_id = update.effective_user.id if update.effective_user else 0
        thread_id = getattr(update.message, "message_thread_id", None) if update.message else None
        return f"telegram:{chat_id}:{thread_id or 0}:{user_id}"

    def _get_session(self, update: Update) -> dict[str, str]:
        """获取当前 Telegram scope 的 session，若不存在则恢复该 scope 上次会话。"""
        key = self._session_key(update)
        user_id = update.effective_user.id if update.effective_user else 0
        if key not in self._sessions:
            if len(self._sessions) >= 1000:
                oldest_key = next(iter(self._sessions))
                self._sessions.pop(oldest_key, None)
            role = self.config.default_role_name
            resp = self.service.latest_session(role, latest_scope=key)
            self._sessions[key] = {"role": role, "session_id": resp.session_id, "scope": key}
            logger.info(
                "telegram restored session user_id=%d scope=%s role=%s session_id=%s",
                user_id, key, role, resp.session_id,
            )
        else:
            session = self._sessions.pop(key)
            self._sessions[key] = session
        return self._sessions[key]

    def _set_session(self, update: Update, role: str, session_id: str) -> None:
        key = self._session_key(update)
        self._sessions[key] = {"role": role, "session_id": session_id, "scope": key}

    def _get_user_lock(self, update: Update) -> asyncio.Lock:
        key = self._session_key(update)
        if key not in self._user_locks:
            if len(self._user_locks) >= 1000:
                oldest_key = next(iter(self._user_locks))
                self._user_locks.pop(oldest_key, None)
            self._user_locks[key] = asyncio.Lock()
        else:
            lock = self._user_locks.pop(key)
            self._user_locks[key] = lock
        return self._user_locks[key]

    async def _dispatch_via_registry(
        self,
        update: Update,
        message_text: str,
    ) -> CommandOutcome | None:
        """统一命令分派:命中即把 outcome 渲染回 Telegram 并同步本地会话。"""
        if not message_text:
            return None
        session = self._get_session(update)
        scope_key = self._session_key(update)
        channel_context = {
            "type": "telegram",
            "platform": "telegram",
            "channel_code": "telegram",
            "chat_id": update.effective_chat.id if update.effective_chat else update.message.chat_id,
            "user_id": update.effective_user.id if update.effective_user else 0,
            "thread_id": getattr(update.message, "message_thread_id", None),
            "session_scope_key": scope_key,
            "session_id": session["session_id"],
            "role": session["role"],
        }
        try:
            outcome = await self.service.dispatch_command(
                message_text,
                role=session["role"],
                session_id=session["session_id"],
                channel_context=channel_context,
                channel="telegram",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("telegram dispatch_command failed user_id=%s", channel_context["user_id"])
            await update.message.reply_text(f"❌ 命令执行失败：{_exception_text(exc)}")
            return None
        if outcome is None:
            return None
        if outcome.role or outcome.session_id:
            new_role = outcome.role or session["role"]
            new_session_id = outcome.session_id or session["session_id"]
            self._set_session(update, new_role, new_session_id)
        await self._reply_outcome(update, outcome)
        return outcome

    async def _reply_outcome(self, update: Update, outcome: CommandOutcome) -> None:
        """按 outcome.kind 选合适的 emoji 前缀并发送。"""
        if not outcome.message:
            return
        prefix_map = {
            "stop": "🛑 ",
            "new_session": "✅ ",
            "switch_role": "✅ ",
            "compress": "🗜️ ",
            "info": "",
            "help": "📖 ",
            "start": "",
            "exit": "",
        }
        prefix = prefix_map.get(outcome.kind, "")
        await update.message.reply_text(f"{prefix}{outcome.message}")

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/start 命令：欢迎语 + 新建会话(通过 registry 分派以保持单一来源)。"""
        user = update.effective_user
        if user is None:
            return
        outcome = await self._dispatch_via_registry(update, "/start")
        if outcome is None:
            return
        # 在 outcome 之上再附一段 Telegram 专属欢迎语;命令清单由 registry 动态生成
        commands_summary = "\n".join(
            f"  <code>{html.escape(spec.aliases[0])}</code>"
            f"{(' <code>' + html.escape(spec.args_hint) + '</code>') if spec.args_hint else ''}"
            f" — {html.escape(spec.summary)}"
            for spec in iter_for_tg_menu()
        )
        role_text = outcome.role or self.config.default_role_name
        welcome = (
            f"\n👋 你好,<b>{html.escape(user.first_name)}</b>!\n\n"
            f"我是 OpenHachimi Agent,当前角色:<b>{html.escape(role_text)}</b>\n\n"
            "直接发送消息即可开始对话。\n"
            f"常用命令:\n{commands_summary}"
        )
        await update.message.reply_text(welcome, parse_mode=constants.ParseMode.HTML)

    async def cmd_dispatch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """所有 `/xxx`(除 /start)的统一入口:把原始命令文本交给 registry。"""
        if update.message is None:
            return
        message_text = update.message.text or ""
        await self._dispatch_via_registry(update, message_text)

    async def _download_attachments(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[AttachmentRef]:
        message = update.message
        if message is None:
            return []

        attachments: list[AttachmentRef] = []
        message_id = getattr(message, "message_id", int(time.time()))
        namespace = f"{user_id}_{message_id}"

        if message.photo:
            photo = message.photo[-1]
            size_bytes = getattr(photo, "file_size", None)
            content_type = "image/jpeg"
            self.attachment_storage.validate_metadata(filename=None, content_type=content_type, size_bytes=size_bytes)
            target = self.attachment_storage.build_path(
                source="telegram",
                namespace=namespace,
                filename=f"photo_{message_id}.jpg",
                content_type=content_type,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            telegram_file = await ctx.bot.get_file(photo.file_id)
            await telegram_file.download_to_drive(custom_path=target)
            attachments.append(
                self.attachment_storage.to_ref(
                    path=target,
                    source="telegram",
                    filename=target.name,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    metadata={"telegram_file_id": photo.file_id},
                )
            )

        if message.document:
            document = message.document
            content_type = document.mime_type or "application/octet-stream"
            filename = document.file_name or f"document_{message_id}"
            size_bytes = document.file_size
            self.attachment_storage.validate_metadata(filename=filename, content_type=content_type, size_bytes=size_bytes)
            target = self.attachment_storage.build_path(
                source="telegram",
                namespace=namespace,
                filename=filename,
                content_type=content_type,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            telegram_file = await ctx.bot.get_file(document.file_id)
            await telegram_file.download_to_drive(custom_path=target)
            attachments.append(
                self.attachment_storage.to_ref(
                    path=target,
                    source="telegram",
                    filename=filename,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    metadata={"telegram_file_id": document.file_id},
                )
            )

        logger.info("telegram attachments user_id=%d count=%d", user_id, len(attachments))
        return attachments

    async def _send_artifact(self, update: Update, artifact: ArtifactRef) -> None:
        target = resolve_workspace_path(self.config.base_dir, artifact.local_path)
        if not target.exists() or not target.is_file():
            await update.message.reply_text(f"⚠️ 生成文件不存在：{artifact.filename}")
            return
        size = target.stat().st_size
        if size > self.config.max_attachment_size_bytes:
            await update.message.reply_text(f"⚠️ 文件过大，无法通过 Telegram 发送：{artifact.filename}")
            return
        caption = f"已生成文件：{artifact.filename}"
        if artifact.description:
            caption = f"{caption}\n{artifact.description}"
        with target.open("rb") as file:
            await update.message.reply_document(
                document=file,
                filename=artifact.filename,
                caption=caption[:1024],
            )
        artifacts_root = self.config.attachments_dir.parent / "artifacts"
        with contextlib.suppress(ValueError, OSError):
            target.relative_to(artifacts_root.resolve())
            shutil.rmtree(target.parent)

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """普通文本消息处理器：流式调用 Agent 并以「编辑消息」实现打字效果。

        流式过程中使用纯文本（避免不完整的 Markdown 导致 HTML 解析出错）。
        流结束后将完整内容转换为 Telegram HTML 格式重新渲染。
        """
        user_id = update.effective_user.id
        user_text = ((update.message.text or update.message.caption or "") if update.message else "").strip()
        try:
            attachments = await self._download_attachments(update, ctx, user_id)
        except AttachmentError as exc:
            await update.message.reply_text(f"❌ 附件无法处理：{exc}")
            return
        except Exception as exc:
            logger.exception("telegram attachment download failed user_id=%d", user_id)
            await update.message.reply_text(f"❌ 附件下载失败：{_exception_text(exc)}")
            return
        if not user_text and attachments:
            user_text = "用户发送了附件，请根据附件内容协助处理。"
        if not user_text:
            return

        session = self._get_session(update)
        role = session["role"]
        session_id = session["session_id"]

        lock = self._get_user_lock(update)

        async with lock:
            # 先尝试命令分派(覆盖中文别名 `/压缩` `停止` `新对话` 等);命中则不走 LLM
            if user_text and not attachments:
                outcome = await self._dispatch_via_registry(update, user_text)
                if outcome is not None:
                    return

            logger.info(
                "telegram message user_id=%d role=%s session_id=%s chars=%d attachment_count=%d",
                user_id, role, session_id, len(user_text), len(attachments),
            )

            # 先发出占位消息
            placeholder = await update.message.reply_text("⏳ 思考中……")

            presenter = ToolProgressPresenter(mode="conversation")
            unused_placeholder = True
            current_tool_text = ""
            tool_messages: list = []
            answer_text = ""
            system_text = ""
            answer_messages: list = []
            last_answer_edit_time = time.monotonic()
            typing_task = asyncio.create_task(_keep_typing(update, ctx))

            async def edit_or_send(messages: list, index: int, text: str, parse_mode=None):
                if index < len(messages):
                    try:
                        await messages[index].edit_text(text, parse_mode=parse_mode)
                        return messages[index]
                    except TelegramError as exc:
                        if _is_message_not_modified(exc):
                            return messages[index]
                        logger.warning(
                            "telegram edit_text failed user_id=%d part=%d: %s",
                            user_id,
                            index,
                            _exception_text(exc),
                            exc_info=True,
                        )
                return await update.message.reply_text(text, parse_mode=parse_mode)

            async def ensure_tool_messages() -> None:
                nonlocal unused_placeholder, tool_messages
                if tool_messages:
                    return
                if unused_placeholder:
                    tool_messages = [placeholder]
                    unused_placeholder = False
                else:
                    tool_messages = [await update.message.reply_text("⏳ 工具调用中……")]

            async def ensure_answer_messages() -> None:
                nonlocal unused_placeholder, answer_messages
                if answer_messages:
                    return
                if unused_placeholder:
                    answer_messages = [placeholder]
                    unused_placeholder = False
                else:
                    answer_messages = [await update.message.reply_text("⏳ 回复中……")]

            async def render_tool_messages() -> None:
                if not current_tool_text:
                    return
                await ensure_tool_messages()
                parts = _split_long_text(current_tool_text)
                for i, part in enumerate(parts):
                    msg = await edit_or_send(tool_messages, i, part)
                    if i >= len(tool_messages):
                        tool_messages.append(msg)

            async def render_answer_messages(final: bool = False) -> None:
                nonlocal last_answer_edit_time
                content = "\n\n".join(part.strip() for part in [answer_text, system_text] if part.strip())
                if not content:
                    return
                await ensure_answer_messages()
                parts = _split_long_text(content)
                for i, part in enumerate(parts):
                    is_last_part = i == len(parts) - 1
                    parse_mode = None
                    display = part
                    if final:
                        try:
                            display = _md_to_tg_html(part)
                            parse_mode = constants.ParseMode.HTML
                        except Exception as exc:
                            logger.warning(
                                "markdown to html conversion failed, fallback to plain text: %s",
                                _exception_text(exc),
                                exc_info=True,
                            )
                    elif is_last_part:
                        display = part + " ▌"
                    msg = await edit_or_send(answer_messages, i, display, parse_mode=parse_mode)
                    if i >= len(answer_messages):
                        answer_messages.append(msg)
                last_answer_edit_time = time.monotonic()

            async def finalize_answer_segment() -> None:
                nonlocal answer_text, system_text, answer_messages
                await render_answer_messages(final=True)
                answer_text = ""
                system_text = ""
                answer_messages = []

            async def finalize_tool_segment() -> None:
                nonlocal current_tool_text, tool_messages
                await render_tool_messages()
                current_tool_text = ""
                tool_messages = []
                presenter.reset_tools()

            try:
                channel_context = {
                    "type": "telegram",
                    "platform": "telegram",
                    "channel_code": "telegram",
                    "chat_id": update.effective_chat.id if update.effective_chat else update.message.chat_id,
                    "user_id": user_id,
                    "thread_id": getattr(update.message, "message_thread_id", None),
                    "session_scope_key": session.get("scope"),
                    "session_id": session_id,
                    "role": role,
                }
                async for event in self.service.stream_events(
                    user_text,
                    role,
                    session_id,
                    attachments=attachments,
                    channel_context=channel_context,
                ):
                    for action in presenter.handle_event(event):
                        if action.type == "tool":
                            if answer_text.strip() or system_text.strip():
                                await finalize_answer_segment()
                            current_tool_text = action.text
                            await render_tool_messages()
                        elif action.type == "text":
                            if current_tool_text:
                                await finalize_tool_segment()
                            answer_text += action.text
                            if time.monotonic() - last_answer_edit_time >= _EDIT_INTERVAL:
                                await render_answer_messages(final=False)
                        elif action.type == "system":
                            if current_tool_text:
                                await finalize_tool_segment()
                            system_text += action.text
                            if time.monotonic() - last_answer_edit_time >= _EDIT_INTERVAL:
                                await render_answer_messages(final=False)
                        elif action.type == "artifact" and action.artifact:
                            if current_tool_text:
                                await finalize_tool_segment()
                            if answer_text.strip() or system_text.strip():
                                await finalize_answer_segment()
                            await self._send_artifact(update, action.artifact)

                for action in presenter.finalize():
                    if action.type == "tool":
                        current_tool_text = action.text
                        await render_tool_messages()
                        current_tool_text = ""
                if current_tool_text:
                    await finalize_tool_segment()
                if answer_text.strip() or system_text.strip():
                    await finalize_answer_segment()
                if unused_placeholder:
                    await placeholder.edit_text("任务已完成，但没有生成文本回复。")

            except Exception as exc:
                logger.exception("telegram stream error user_id=%d", user_id)
                err_text = f"⚠️ 调用 Agent 时出错：{_exception_text(exc)}"
                try:
                    if answer_messages:
                        await answer_messages[0].edit_text(err_text)
                    elif tool_messages:
                        await tool_messages[0].edit_text(err_text)
                    elif unused_placeholder:
                        await placeholder.edit_text(err_text)
                    else:
                        await update.message.reply_text(err_text)
                except Exception as edit_exc:
                    logger.warning(
                        "telegram failed to send error message user_id=%d: %s",
                        user_id,
                        _exception_text(edit_exc),
                        exc_info=True,
                    )
            finally:
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typing_task


@asynccontextmanager
async def telegram_lifespan(config: AppConfig, service: AgentService) -> AsyncIterator[Callable[[int, str, int | None], Awaitable[None]] | None]:
    """Telegram Bot 生命周期管理器，供 FastAPI lifespan 调用。

    若未配置 token，则跳过，不影响 HTTP 服务正常运行。
    """
    token = config.telegram_bot_token
    if not token:
        logger.info("telegram bot token not configured, skipping")
        yield None
        return

    bot = TelegramBot(config, service)

    # 根据配置决定是否使用代理
    proxy_url = config.telegram_proxy_url
    if proxy_url:
        logger.info("telegram bot using proxy: %s", proxy_url)
        request = HTTPXRequest(proxy=proxy_url)
        get_updates_request = HTTPXRequest(proxy=proxy_url, read_timeout=30.0)
        app = (
            Application.builder()
            .token(token)
            .request(request)
            .get_updates_request(get_updates_request)
            .build()
        )
    else:
        # 使用默认配置构建 Application（含内置 Updater），由 asyncio 事件循环统一调度
        app = Application.builder().token(token).build()

    # 注册命令处理器:/start 保留独立 handler 以匹配 Telegram 习惯,
    # 其它 `/xxx` 统一由 cmd_dispatch 接管,真正的执行委托给 registry。
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(MessageHandler(filters.COMMAND & ~filters.Regex(r"^/start(\s|$)"), bot.cmd_dispatch))
    # 普通消息处理器(非命令),支持文本、图片与文档,设置为 block=False 以免阻塞后续的 /stop 等命令
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, bot.handle_message, block=False))

    # 启动 Bot(连接失败时不阻断 HTTP 服务,仅记录错误)
    try:
        await app.initialize()
        await app.start()

        # 向 Telegram 服务器注册命令菜单:从 registry 自动构建
        from telegram import BotCommand
        menu_specs = iter_for_tg_menu()
        bot_commands = [
            BotCommand(
                spec.aliases[0].lstrip("/"),
                (spec.tg_menu_label or spec.summary)[:256],
            )
            for spec in menu_specs
            if spec.aliases and spec.aliases[0].startswith("/")
        ]
        await app.bot.set_my_commands(bot_commands)
        logger.info("telegram bot commands menu registered count=%d", len(bot_commands))

        # 在同一 asyncio 事件循环中启动 Polling，不阻塞
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("telegram bot polling started")
    except Exception:
        logger.exception(
            "telegram bot 启动失败（可能是代理不通或 token 无效），"
            "HTTP 服务将继续运行，Telegram 功能不可用"
        )
        yield None  # HTTP 服务照常运行
        return

    async def send_scheduled_message(chat_id: int, text: str, thread_id: int | None = None) -> None:
        for part in _split_long_text(text):
            kwargs = {"chat_id": chat_id, "text": part}
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            await app.bot.send_message(**kwargs)

    yield send_scheduled_message  # FastAPI 服务运行期间 Bot 持续工作

    # 优雅关闭
    logger.info("telegram bot shutting down")
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        logger.exception("telegram bot 关闭时出错，忽略")
    logger.info("telegram bot stopped")
