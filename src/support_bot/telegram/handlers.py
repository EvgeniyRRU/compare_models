"""Хендлеры PTB. Тонкий слой: разбор апдейта → вызов сервиса → ответ пользователю."""

from typing import Any

import structlog
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    WebAppInfo,
)
from telegram import User as TgUser
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from support_bot.callbacks import (
    CallbackDataError,
    DraftAction,
    parse_draft_callback,
    parse_ticket_callback,
)
from support_bot.container import Container
from support_bot.domain.enums import ROLE_LABELS, TEXT_STEPS
from support_bot.domain.errors import AccessDeniedError, DomainError, NotFoundError
from support_bot.domain.state_machine import can_create
from support_bot.monitoring import metrics
from support_bot.monitoring.sentry import capture_exception
from support_bot.services.drafts import DraftNotFoundError, DraftStaleError, TextOutcome
from support_bot.services.users import TelegramProfile
from support_bot.services.views import UserInfo
from support_bot.telegram.form_ui import ask, resume_keyboard

log = structlog.get_logger(__name__)

type Ctx = ContextTypes.DEFAULT_TYPE

HELP_TEXT = (
    "Бот учёта обращений к дежурному разработчику.\n\n"
    "/new — создать обращение (support, admin)\n"
    "/cancel — отменить создание обращения\n"
    "/help — справка\n\n"
    "Обращения публикуются в чате разработчиков, статусы меняются кнопками под постом. "
    "Списки обращений и карточки — в Mini App (кнопка меню)."
)
NO_ROLE_TEXT = (
    "Вы зарегистрированы, но доступа пока нет. "
    "Обратитесь к администратору, чтобы он назначил вам роль."
)


def profile(user: TgUser) -> TelegramProfile:
    return TelegramProfile(user.id, user.username, user.first_name, user.last_name)


class BotHandlers:
    def __init__(self, container: Container) -> None:
        self.c = container

    def register(self, app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        private = filters.ChatType.PRIVATE
        app.add_handler(CommandHandler("start", self.cmd_start, filters=private))
        app.add_handler(CommandHandler("help", self.cmd_help, filters=private))
        app.add_handler(CommandHandler("new", self.cmd_new, filters=private))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel, filters=private))
        app.add_handler(CallbackQueryHandler(self.on_ticket_button, pattern=r"^t:"))
        app.add_handler(CallbackQueryHandler(self.on_draft_button, pattern=r"^d:"))
        app.add_handler(MessageHandler(private & filters.COMMAND, self.cmd_help))
        app.add_handler(MessageHandler(private & ~filters.COMMAND, self.on_private_message))
        app.add_error_handler(self.on_error)

    # --- helpers ---------------------------------------------------------------------------

    def _webapp_markup(self) -> InlineKeyboardMarkup | None:
        url = self.c.settings.webapp_url
        if not url:
            return None
        # web_app-кнопки работают только в личных чатах.
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("📋 Открыть обращения", web_app=WebAppInfo(url))]]
        )

    async def _touch(self, update: Update) -> UserInfo | None:
        tg_user = update.effective_user
        if tg_user is None:
            return None
        user = await self.c.users.touch(profile(tg_user))
        structlog.contextvars.bind_contextvars(telegram_user_id=tg_user.id)
        return user

    @staticmethod
    async def _clear_buttons(query: CallbackQuery) -> None:
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            log.debug("clear_buttons_failed", exc_info=True)

    # --- команды ---------------------------------------------------------------------------

    async def cmd_start(self, update: Update, context: Ctx) -> None:
        message, tg_user = update.effective_message, update.effective_user
        if message is None or tg_user is None:
            return
        user = await self.c.users.register_start(profile(tg_user))
        if user.role is None:
            await message.reply_text(NO_ROLE_TEXT)
            return
        text = f"Здравствуйте, {user.display_name}! Ваша роль: {ROLE_LABELS[user.role]}.\n\n"
        await message.reply_text(text + HELP_TEXT, reply_markup=self._webapp_markup())

    async def cmd_help(self, update: Update, context: Ctx) -> None:
        if update.effective_message is None:
            return
        user = await self._touch(update)
        if user is None or user.role is None:
            await update.effective_message.reply_text(NO_ROLE_TEXT)
            return
        await update.effective_message.reply_text(HELP_TEXT, reply_markup=self._webapp_markup())

    async def cmd_new(self, update: Update, context: Ctx) -> None:
        message = update.effective_message
        user = await self._touch(update)
        if message is None or user is None:
            return
        if user.role is None:
            await message.reply_text(NO_ROLE_TEXT)
            return
        if not can_create(user.role):
            await message.reply_text("Создавать обращения могут только support и admin.")
            return
        try:
            result = await self.c.drafts.start(user.id)
        except DomainError as exc:
            await message.reply_text(exc.message)
            return
        if result.existing:
            await message.reply_text(
                "У вас есть незавершённый черновик обращения. Продолжить или начать заново?",
                reply_markup=resume_keyboard(result.draft),
            )
            return
        await ask(context.bot, message.chat_id, self.c.drafts, result.draft)

    async def cmd_cancel(self, update: Update, context: Ctx) -> None:
        message = update.effective_message
        user = await self._touch(update)
        if message is None or user is None:
            return
        deleted = await self.c.drafts.cancel(user.id)
        await message.reply_text(
            "Создание обращения отменено, черновик удалён." if deleted else "Нечего отменять."
        )

    # --- форма -----------------------------------------------------------------------------

    async def on_private_message(self, update: Update, context: Ctx) -> None:
        message: Message | None = update.effective_message
        user = await self._touch(update)
        if message is None or user is None:
            return
        if user.role is None:
            await message.reply_text(NO_ROLE_TEXT)
            return
        reply_to = message.reply_to_message.message_id if message.reply_to_message else None
        try:
            result = await self.c.drafts.submit_text(user.id, reply_to, message.text)
        except DraftNotFoundError:
            await message.reply_text("Чтобы создать обращение, отправьте /new. Справка — /help.")
            return
        except DomainError as exc:
            await message.reply_text(exc.message)
            return

        notice: str | None = None
        match result.outcome:
            case TextOutcome.INVALID:
                notice = f"⚠️ {result.error}"
            case TextOutcome.UNBOUND:
                notice = (
                    "Ответьте, пожалуйста, на вопрос формы (через «Ответить»), "
                    "чтобы ответ попал в нужное поле."
                    if result.draft.step in TEXT_STEPS
                    else "Выберите вариант кнопкой ниже."
                )
            case TextOutcome.ACCEPTED:
                pass
        await ask(context.bot, message.chat_id, self.c.drafts, result.draft, notice)

    async def on_draft_button(self, update: Update, context: Ctx) -> None:
        query = update.callback_query
        user = await self._touch(update)
        if query is None or user is None or query.data is None:
            return
        try:
            cb = parse_draft_callback(query.data)
        except CallbackDataError:
            await query.answer("Неизвестная кнопка", show_alert=True)
            return
        chat_id = query.message.chat.id if query.message else user.telegram_id
        d = self.c.drafts
        try:
            match cb.action:
                case DraftAction.PRIORITY:
                    assert cb.priority is not None
                    draft = await d.choose_priority(user.id, cb.draft_id, cb.version, cb.priority)
                case DraftAction.SKIP_TENANT:
                    draft = await d.skip_tenant(user.id, cb.draft_id, cb.version)
                case DraftAction.CONTINUE:
                    draft = await d.resume(user.id, cb.draft_id, cb.version)
                case DraftAction.RESTART:
                    draft = await d.restart(user.id, cb.draft_id, cb.version)
                case DraftAction.CANCEL:
                    await d.cancel_by_button(user.id, cb.draft_id, cb.version)
                    await query.answer("Черновик удалён")
                    await self._safe_edit_text(query, "Создание обращения отменено.")
                    return
                case DraftAction.SUBMIT:
                    ticket_id = await d.submit(user.id, cb.draft_id, cb.version)
                    structlog.contextvars.bind_contextvars(ticket_id=ticket_id)
                    log.info("ticket_created")
                    await query.answer(f"Обращение #{ticket_id} создано")
                    await self._safe_edit_text(
                        query, f"✅ Обращение #{ticket_id} создано и передано разработчикам."
                    )
                    # Публикация после коммита; при сбое пост опубликует фоновая задача.
                    await self.c.post_sync.sync_safely(ticket_id)
                    return
        except DraftStaleError as exc:
            await query.answer(exc.message, show_alert=True)
            if exc.current is not None:
                await ask(context.bot, chat_id, d, exc.current, "Продолжим с текущего шага.")
            return
        except DomainError as exc:
            await query.answer(exc.message, show_alert=True)
            return
        await query.answer()
        await self._clear_buttons(query)
        await ask(context.bot, chat_id, d, draft)

    @staticmethod
    async def _safe_edit_text(query: CallbackQuery, text: str) -> None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        except TelegramError:
            log.debug("edit_message_failed", exc_info=True)

    # --- кнопки поста ----------------------------------------------------------------------

    async def on_ticket_button(self, update: Update, context: Ctx) -> None:
        query = update.callback_query
        user = await self._touch(update)
        if query is None or user is None or query.data is None:
            return
        try:
            cb = parse_ticket_callback(query.data)
        except CallbackDataError:
            await query.answer("Неизвестная кнопка", show_alert=True)
            return
        with structlog.contextvars.bound_contextvars(ticket_id=cb.ticket_id):
            try:
                result = await self.c.tickets.apply_action(
                    user.id, cb.ticket_id, cb.action, cb.version
                )
            except AccessDeniedError as exc:
                await query.answer(exc.message, show_alert=True)
                return
            except NotFoundError as exc:
                await query.answer(exc.message, show_alert=True)
                return
            except DomainError as exc:
                # «Уже взято», «Обращение изменилось», нет прав, недопустимый переход.
                await query.answer(exc.message, show_alert=True)
            else:
                log.info("ticket_action", action=cb.action.value, version=result.version)
                await query.answer("Готово")
            await self.c.post_sync.sync_safely(cb.ticket_id)

    # --- ошибки ----------------------------------------------------------------------------

    async def on_error(self, update: object, context: Ctx) -> None:
        error = context.error
        update_id = getattr(update, "update_id", None)
        log.error("handler_error", update_id=update_id, exc_info=error)
        metrics.ERRORS.labels(component="bot").inc()
        if error is not None:
            user = getattr(update, "effective_user", None)
            capture_exception(
                error,
                component="bot",
                update_id=update_id,
                telegram_user_id=getattr(user, "id", None),
            )
