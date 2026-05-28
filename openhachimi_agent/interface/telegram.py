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
from openhachimi_agent.interface.presenter import ToolProgressPresenter
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
    text = str(exc).strip()
    if text:
        return f"{exc.__class__.__name__}: {text}"
    return exc.__class__.__name__


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
        # 按 user_id 存储各自的 {role, session_id}
        self._sessions: dict[int, dict[str, str]] = {}
        # 按 user_id 存储队列锁，实现每个用户独立的消息排队机制
        self._user_locks: dict[int, asyncio.Lock] = {}
        self.attachment_storage = AttachmentStorage(
            config.attachments_dir,
            config.max_attachment_size_bytes,
            config.allowed_attachment_mime_types,
            config.base_dir,
        )
        logger.info("telegram bot handler initialized")

    def _get_session(self, user_id: int) -> dict[str, str]:
        """获取用户 session，若不存在则尝试恢复上次会话。"""
        if user_id not in self._sessions:
            if len(self._sessions) >= 1000:
                oldest_user = next(iter(self._sessions))
                self._sessions.pop(oldest_user, None)
            role = self.config.default_role_name
            resp = self.service.latest_session(role)
            self._sessions[user_id] = {"role": role, "session_id": resp.session_id}
            logger.info(
                "telegram restored session user_id=%d role=%s session_id=%s",
                user_id, role, resp.session_id,
            )
        else:
            session = self._sessions.pop(user_id)
            self._sessions[user_id] = session
        return self._sessions[user_id]

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._user_locks:
            if len(self._user_locks) >= 1000:
                oldest_user = next(iter(self._user_locks))
                self._user_locks.pop(oldest_user, None)
            self._user_locks[user_id] = asyncio.Lock()
        else:
            lock = self._user_locks.pop(user_id)
            self._user_locks[user_id] = lock
        return self._user_locks[user_id]

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/start 命令：欢迎语 + 新建会话。"""
        user = update.effective_user
        if user is None:
            return
        role = self.config.default_role_name
        resp = self.service.new_session(role)
        self._sessions[user.id] = {"role": role, "session_id": resp.session_id}
        logger.info("cmd /start user_id=%d role=%s session_id=%s", user.id, role, resp.session_id)

        welcome = (
            f"👋 你好，<b>{html.escape(user.first_name)}</b>！\n\n"
            f"我是 OpenHachimi Agent，当前角色：<b>{html.escape(role)}</b>\n\n"
            "直接发送消息即可开始对话。\n"
            "可用命令：\n"
            "  /new — 新建对话\n"
            "  /roles — 查看角色列表\n"
            "  /role &lt;名称&gt; — 切换角色\n"
            "  /stop — 中断当前正在执行的任务\n"
            "  /help — 查看帮助"
        )
        await update.message.reply_text(welcome, parse_mode=constants.ParseMode.HTML)

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/help 命令：帮助说明。"""
        text = (
            "📖 <b>帮助</b>\n\n"
            "<code>/start</code> — 重新开始，新建对话\n"
            "<code>/new</code> — 保存当前对话，新建一段对话\n"
            "<code>/roles</code> — 列出全部可用角色\n"
            "<code>/role &lt;名称&gt;</code> — 切换到指定角色\n"
            "<code>/stop</code> — 中断当前正在执行的任务\n"
            "<code>/help</code> — 查看本帮助\n\n"
            "直接发送文字消息即可与 Agent 对话。"
        )
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/new 命令：新建对话。"""
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        role = session["role"]
        old_session_id = session["session_id"]
        await self.service.stop_session(old_session_id)
        resp = self.service.new_session(role)
        self._sessions[user_id] = {"role": role, "session_id": resp.session_id}
        logger.info("cmd /new user_id=%d role=%s session_id=%s", user_id, role, resp.session_id)
        await update.message.reply_text(f"✅ {resp.message}")

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/stop 命令：停止当前正在执行的任务。"""
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        resp = await self.service.stop_session(session["session_id"])
        logger.info("cmd /stop user_id=%d role=%s session_id=%s", user_id, session["role"], session["session_id"])
        await update.message.reply_text(f"🛑 {resp.message}")

    async def cmd_roles(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/roles 命令：列出可用角色。"""
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        roles_resp = self.service.list_roles()
        lines = ["🎭 <b>可用角色列表：</b>\n"]
        for r in roles_resp.roles:
            marker = " ✅（当前）" if r == session["role"] else ""
            lines.append(f"• <code>{html.escape(r)}</code>{marker}")
        await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

    async def cmd_role(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/role <名称> 命令：切换角色。"""
        user_id = update.effective_user.id
        args = ctx.args
        if not args:
            await update.message.reply_text(
                "❌ 请在命令后面跟上角色名，例如：<code>/role default</code>",
                parse_mode=constants.ParseMode.HTML,
            )
            return
        role_name = args[0].strip()
        old_session_id = self._get_session(user_id)["session_id"]
        await self.service.stop_session(old_session_id)
        try:
            resp = self.service.switch_role(role_name)
            self._sessions[user_id] = {"role": resp.role, "session_id": resp.session_id}
            logger.info("cmd /role user_id=%d role=%s session_id=%s", user_id, resp.role, resp.session_id)
            await update.message.reply_text(f"✅ {resp.message}")
        except (FileNotFoundError, ValueError) as exc:
            await update.message.reply_text(f"❌ 切换角色失败：{exc}")

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

        session = self._get_session(user_id)
        role = session["role"]
        session_id = session["session_id"]

        lock = self._get_user_lock(user_id)

        async with lock:
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
                    "chat_id": update.effective_chat.id if update.effective_chat else update.message.chat_id,
                    "user_id": user_id,
                    "thread_id": getattr(update.message, "message_thread_id", None),
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

    # 注册命令处理器
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("new", bot.cmd_new))
    app.add_handler(CommandHandler("stop", bot.cmd_stop))
    app.add_handler(CommandHandler("roles", bot.cmd_roles))
    app.add_handler(CommandHandler("role", bot.cmd_role))
    # 普通消息处理器（非命令），支持文本、图片与文档，设置为 block=False 以免阻塞后续的 /stop 等命令
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, bot.handle_message, block=False))

    # 启动 Bot（连接失败时不阻断 HTTP 服务，仅记录错误）
    try:
        await app.initialize()
        await app.start()

        # 向 Telegram 服务器注册命令菜单（用户输入 / 时显示的命令列表）
        # 注意：此调用会持久化到 Telegram 服务器，重启服务无需重复设置，但保持同步是最佳实践
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("new",   "💾 保存当前对话，新建一段对话"),
            BotCommand("roles", "🎭 查看可用角色列表"),
            BotCommand("role",  "🔄 切换角色（如：/role default）"),
            BotCommand("stop",  "🛑 中断当前正在执行的任务"),
            BotCommand("help",  "📖 查看帮助"),
        ])
        logger.info("telegram bot commands menu registered")

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
