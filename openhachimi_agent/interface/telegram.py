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
from collections.abc import AsyncIterator
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
from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)

# 流式编辑的最小间隔（秒），避免触发 Telegram API 限流（每分钟约 20 次编辑）
_EDIT_INTERVAL = 1.5
# 单条 Telegram 消息最大字符数（官方限制 4096，留余量）
_MAX_MSG_LEN = 4000


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


class TelegramBot:
    """Telegram Bot 主体，封装会话状态与消息处理逻辑。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.service = AgentService(config)
        # 按 user_id 存储各自的 {role, session_id}
        self._sessions: dict[int, dict[str, str]] = {}
        logger.info("telegram bot handler initialized")

    def _get_session(self, user_id: int) -> dict[str, str]:
        """获取用户 session，若不存在则尝试恢复上次会话。"""
        if user_id not in self._sessions:
            role = self.config.default_role_name
            resp = self.service.latest_session(role)
            self._sessions[user_id] = {"role": role, "session_id": resp.session_id}
            logger.info(
                "telegram restored session user_id=%d role=%s session_id=%s",
                user_id, role, resp.session_id,
            )
        return self._sessions[user_id]

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
            "<code>/help</code> — 查看本帮助\n\n"
            "直接发送文字消息即可与 Agent 对话。"
        )
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/new 命令：新建对话。"""
        user_id = update.effective_user.id
        session = self._get_session(user_id)
        role = session["role"]
        resp = self.service.new_session(role)
        self._sessions[user_id] = {"role": role, "session_id": resp.session_id}
        logger.info("cmd /new user_id=%d role=%s session_id=%s", user_id, role, resp.session_id)
        await update.message.reply_text(f"✅ {resp.message}")

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
        try:
            resp = self.service.switch_role(role_name)
            self._sessions[user_id] = {"role": resp.role, "session_id": resp.session_id}
            logger.info("cmd /role user_id=%d role=%s session_id=%s", user_id, resp.role, resp.session_id)
            await update.message.reply_text(f"✅ {resp.message}")
        except (FileNotFoundError, ValueError) as exc:
            await update.message.reply_text(f"❌ 切换角色失败：{exc}")

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """普通文本消息处理器：流式调用 Agent 并以「编辑消息」实现打字效果。

        流式过程中使用纯文本（避免不完整的 Markdown 导致 HTML 解析出错）。
        流结束后将完整内容转换为 Telegram HTML 格式重新渲染。
        """
        user_id = update.effective_user.id
        user_text = (update.message.text or "").strip()
        if not user_text:
            return

        session = self._get_session(user_id)
        role = session["role"]
        session_id = session["session_id"]

        logger.info(
            "telegram message user_id=%d role=%s session_id=%s chars=%d",
            user_id, role, session_id, len(user_text),
        )

        # 先发出占位消息
        placeholder = await update.message.reply_text("⏳ 思考中……")

        accumulated = ""
        last_edit_time = time.monotonic()
        sent_parts: list = [placeholder]

        async def flush(final: bool = False) -> None:
            """将累积内容同步到 Telegram 消息。

            final=False：以纯文本发出（流式过程中，Markdown 可能不完整）
            final=True：转换为 HTML 渲染（完整内容，格式正确）
            """
            nonlocal last_edit_time, sent_parts
            if not accumulated:
                return

            parts = _split_long_text(accumulated)

            for i, part in enumerate(parts):
                is_last_part = (i == len(parts) - 1)

                if final:
                    # 最终渲染：转换为 HTML，若失败则回退到纯文本
                    try:
                        display = _md_to_tg_html(part)
                        parse_mode = constants.ParseMode.HTML
                    except Exception as exc:
                        logger.warning(
                            "markdown to html conversion failed, fallback to plain text: %s",
                            _exception_text(exc),
                            exc_info=True,
                        )
                        display = part
                        parse_mode = None
                else:
                    # 流式过程：纯文本 + 光标符号
                    display = part + (" ▌" if is_last_part else "")
                    parse_mode = None

                if i < len(sent_parts):
                    try:
                        await sent_parts[i].edit_text(display, parse_mode=parse_mode)
                    except TelegramError as exc:
                        if _is_message_not_modified(exc):
                            continue
                        logger.warning(
                            "telegram edit_text failed user_id=%d part=%d final=%s: %s",
                            user_id,
                            i,
                            final,
                            _exception_text(exc),
                            exc_info=True,
                        )
                        try:
                            new_msg = await update.message.reply_text(display, parse_mode=parse_mode)
                            sent_parts[i] = new_msg
                        except Exception as fallback_exc:
                            raise RuntimeError(
                                "Telegram 更新回复失败，且发送备用消息也失败："
                                f"edit={_exception_text(exc)}; "
                                f"reply={_exception_text(fallback_exc)}"
                            ) from fallback_exc
                else:
                    try:
                        new_msg = await update.message.reply_text(display, parse_mode=parse_mode)
                        sent_parts.append(new_msg)
                    except Exception as exc:
                        raise RuntimeError(f"Telegram 发送分段回复失败：{_exception_text(exc)}") from exc

            last_edit_time = time.monotonic()

        try:
            async for chunk in self.service.stream_message(user_text, role, session_id):
                accumulated += chunk
                if time.monotonic() - last_edit_time >= _EDIT_INTERVAL:
                    await flush(final=False)

            # 流结束，转换为 HTML 完整渲染
            await flush(final=True)

        except Exception as exc:
            logger.exception("telegram stream error user_id=%d", user_id)
            err_text = f"⚠️ 调用 Agent 时出错：{_exception_text(exc)}"
            try:
                await sent_parts[0].edit_text(err_text)
            except Exception as edit_exc:
                logger.warning(
                    "telegram failed to edit error message user_id=%d: %s",
                    user_id,
                    _exception_text(edit_exc),
                    exc_info=True,
                )
                try:
                    await update.message.reply_text(err_text)
                except Exception:
                    logger.exception("telegram failed to send error message user_id=%d", user_id)


@asynccontextmanager
async def telegram_lifespan(config: AppConfig) -> AsyncIterator[None]:
    """Telegram Bot 生命周期管理器，供 FastAPI lifespan 调用。

    若未配置 token，则跳过，不影响 HTTP 服务正常运行。
    """
    token = config.telegram_bot_token
    if not token:
        logger.info("telegram bot token not configured, skipping")
        yield
        return

    bot = TelegramBot(config)

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
    app.add_handler(CommandHandler("roles", bot.cmd_roles))
    app.add_handler(CommandHandler("role", bot.cmd_role))
    # 普通文本消息处理器（非命令）
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

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
        yield  # HTTP 服务照常运行
        return

    yield  # FastAPI 服务运行期间 Bot 持续工作

    # 优雅关闭
    logger.info("telegram bot shutting down")
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        logger.exception("telegram bot 关闭时出错，忽略")
    logger.info("telegram bot stopped")
