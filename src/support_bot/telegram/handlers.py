"""Thin Telegram adapters; all mutable form and ticket state lives in PostgreSQL."""

from typing import TYPE_CHECKING
from uuid import UUID

from structlog.contextvars import bind_contextvars, bound_contextvars
from telegram import MenuButtonWebApp, Update, WebAppInfo
from telegram.ext import CallbackContext

from support_bot.config import Settings
from support_bot.db.models import Ticket, TicketDraft, User
from support_bot.domain.errors import BusinessError, Conflict
from support_bot.telegram.rendering import draft_keyboard, draft_prompt

if TYPE_CHECKING:
    from support_bot.services.drafts import DraftService
    from support_bot.services.posts import PostService
    from support_bot.services.tickets import TicketService
    from support_bot.services.users import UserService


class BotContext(CallbackContext):
    # CallbackContext is created per update. Roles are never stored in user_data.
    profile: User | None = None


class BotHandlers:
    def __init__(
        self,
        settings: Settings,
        users: UserService,
        tickets: TicketService,
        drafts: DraftService,
        posts: PostService,
    ) -> None:
        self.settings, self.users, self.tickets = settings, users, tickets
        self.drafts, self.posts = drafts, posts

    async def profile(self, update: Update, context: BotContext) -> None:
        person = update.effective_user
        if person is None:
            return
        message = update.effective_message
        words = (message.text or "").split(maxsplit=1) if message else []
        command = words[0] if words else ""
        bootstrap = bool(
            update.effective_chat
            and update.effective_chat.type == "private"
            and command.split("@", 1)[0] == "/start"
            and update.callback_query is None
        )
        context.profile = await self.users.touch(
            telegram_id=person.id,
            first_name=person.first_name,
            username=person.username,
            last_name=person.last_name,
            bootstrap=bootstrap,
        )

    async def _allowed(
        self,
        update: Update,
        context: BotContext,
        *,
        create: bool = False,
    ) -> bool:
        if not update.effective_chat or update.effective_chat.type != "private":
            return False
        user = context.profile
        roles = {"support", "admin"} if create else {"support", "developer", "admin"}
        if user is not None and user.role in roles:
            return True
        if update.effective_message:
            await update.effective_message.reply_text(
                "Недостаточно прав. Обратитесь к администратору."
            )
        return False

    async def start(self, update: Update, context: BotContext) -> None:
        if not await self._allowed(update, context):
            return
        user = context.profile
        if self.settings.mini_app_url and update.effective_chat:
            await context.bot.set_chat_menu_button(
                chat_id=update.effective_chat.id,
                menu_button=MenuButtonWebApp(
                    text="Обращения", web_app=WebAppInfo(self.settings.mini_app_url)
                ),
            )
        if update.effective_message:
            message = "Обращения доступны в меню «Обращения». /help — справка."
            if user and user.role in {"support", "admin"}:
                message += "\n/new — создать обращение; /cancel — отменить форму."
            await update.effective_message.reply_text(message)

    async def help(self, update: Update, context: BotContext) -> None:
        if not await self._allowed(update, context):
            return
        if update.effective_message:
            await update.effective_message.reply_text(
                "Бот учитывает обращения к разработчикам. Общение по обращению идёт вне бота.\n"
                "Первая линия и администратор: /new — создать, /cancel — отменить форму.\n"
                "Разработчик: нажмите «Взять» в чате, затем меняйте статус кнопками поста.\n"
                "Свои и все обращения — в меню «Обращения». Доступ назначает администратор."
            )

    async def new(self, update: Update, context: BotContext) -> None:
        if not await self._allowed(update, context, create=True):
            return
        assert context.profile is not None
        draft = await self.drafts.get(context.profile.id)
        if draft is not None:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "У вас есть незавершённое обращение.",
                    reply_markup=draft_keyboard(draft, ["continue", "restart"]),
                )
            return
        await self.show_draft(update, await self.drafts.begin(context.profile.id))

    async def cancel(self, update: Update, context: BotContext) -> None:
        if not await self._allowed(update, context, create=True):
            return
        assert context.profile is not None
        await self.drafts.cancel(context.profile.id)
        if update.effective_message:
            await update.effective_message.reply_text("Создание обращения отменено.")

    async def show_draft(self, update: Update, draft: TicketDraft) -> None:
        if not update.effective_message:
            return
        draft_id, version, step = draft.id, draft.version, draft.step
        text, markup = draft_prompt(draft)
        sent = await update.effective_message.reply_text(text, reply_markup=markup)
        saved = await self.drafts.save_prompt(draft_id, version, step, sent.message_id)
        if step == "tenant_id" and saved:
            await update.effective_message.reply_text(
                "ID арендатора необязателен.", reply_markup=draft_keyboard(draft, ["skip"])
            )

    async def _redisplay(self, update: Update, user_id: int) -> None:
        draft = await self.drafts.get(user_id)
        if draft is not None:
            await self.show_draft(update, draft)
        elif update.effective_message:
            await update.effective_message.reply_text("Активной формы нет. Начните с /new.")

    async def text(self, update: Update, context: BotContext) -> None:
        if not await self._allowed(update, context, create=True):
            return
        assert context.profile is not None
        message = update.effective_message
        if message is None or message.text is None:
            return
        reply_id = message.reply_to_message.message_id if message.reply_to_message else None
        try:
            draft = await self.drafts.text(context.profile.id, reply_id, message.text)
        except BusinessError as exc:
            await message.reply_text(exc.message)
            await self._redisplay(update, context.profile.id)
            return
        await self.show_draft(update, draft)

    async def _sync(self, ticket_id: int) -> None:
        with bound_contextvars(ticket_id=ticket_id):
            try:
                await self.posts.sync(ticket_id)
            except Exception as exc:
                # Publication is recoverable by the reconciliation worker after commit.
                from support_bot.observability.sentry import capture_error

                capture_error(exc, component="telegram_post_sync")

    async def callback(self, update: Update, context: BotContext) -> None:
        query = update.callback_query
        if query is None:
            return
        user = context.profile
        if user is None or user.role not in {"support", "developer", "admin"}:
            await query.answer("Недостаточно прав. Обратитесь к администратору.", show_alert=True)
            return
        data = query.data if isinstance(query.data, str) else ""
        parts = data.split(":")
        if len(parts) != 4 or len(data.encode()) > 64:
            await query.answer("Недействительная кнопка", show_alert=True)
            return
        try:
            if parts[0] == "d":
                if not update.effective_chat or update.effective_chat.type != "private":
                    await query.answer("Форма доступна в личном чате с ботом", show_alert=True)
                    return
                result = await self.drafts.action(
                    user.id, UUID(hex=parts[1]), int(parts[2]), parts[3]
                )
                await query.answer()
                if isinstance(result, Ticket):
                    await self._sync(result.id)
                    if update.effective_message:
                        await update.effective_message.reply_text(
                            f"Обращение #{result.id} создано."
                        )
                elif isinstance(result, TicketDraft):
                    await self.show_draft(update, result)
                elif update.effective_message:
                    await update.effective_message.reply_text("Создание обращения отменено.")
            elif parts[0] == "t":
                await self._ticket_callback(update, user, parts)
            else:
                await query.answer("Недействительная кнопка", show_alert=True)
        except ValueError, OverflowError:
            await query.answer("Недействительная кнопка", show_alert=True)
        except BusinessError as exc:
            await query.answer(exc.message, show_alert=True)
            if parts[0] == "d" and user.role in {"support", "admin"}:
                await self._redisplay(update, user.id)

    async def _ticket_callback(self, update: Update, user: User, parts: list[str]) -> None:
        query = update.callback_query
        assert query is not None
        action, ticket_id, version = parts[1], int(parts[2]), int(parts[3])
        if (
            action not in {"take", "wait", "work", "resolve", "reject", "duplicate", "refuse"}
            or not 0 < ticket_id < 2**63
            or not 0 < version < 2**31
            or not update.effective_chat
            or update.effective_chat.id != self.settings.dev_chat_id
        ):
            await query.answer("Недействительная кнопка", show_alert=True)
            return
        bind_contextvars(ticket_id=ticket_id)
        ticket = await self.tickets.get(ticket_id)
        try:
            await self.tickets.change(
                actor_id=user.id,
                ticket_id=ticket_id,
                expected_version=version,
                expected_status=ticket.status,
                action=action,
            )
        except Conflict as exc:
            await query.answer(exc.message, show_alert=True)
            await self._sync(ticket_id)
            return
        await query.answer("Готово")
        await self._sync(ticket_id)

    async def error(self, update: object, context: BotContext) -> None:
        if context.error is not None:
            from support_bot.observability.sentry import capture_error

            capture_error(context.error, component="telegram")
