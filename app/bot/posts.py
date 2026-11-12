import asyncio
import html

import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload
from structlog.contextvars import bind_contextvars
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from app.bot.drafts import get_or_register_user
from app.core.config import get_settings
from app.db.models import Ticket, TicketStatus
from app.db.session import SessionLocal, engine
from app.services.tickets import TicketError, transition

log = structlog.get_logger()
LOCK_SEED = 872190


def render_ticket(ticket) -> str:
    author = getattr(ticket, "author", None)
    assignee = getattr(ticket, "assignee", None)
    prefix = (
        f"<b>Обращение #{ticket.id}</b>\n"
        f"Статус: {html.escape(status_label(ticket.status))}\n"
        f"Приоритет: {html.escape(priority_label(ticket.priority))}\n"
        f"Заявка: {html.escape(ticket.request_id)}\n"
        f"Арендатор: {html.escape(ticket.tenant_id or '—')}\n"
        f"Автор: {html.escape(author.full_name if author else '—')}\n"
        f"Исполнитель: {html.escape(assignee.full_name if assignee else '—')}\n\n"
    )
    available = max(0, 4096 - len(prefix) - 1)
    escaped = html.escape(ticket.description)
    if len(escaped) <= available:
        return prefix + escaped
    clipped: list[str] = []
    clipped_length = 1  # reserve room for the ellipsis
    for char in ticket.description:
        escaped_char = html.escape(char)
        if clipped_length + len(escaped_char) > available:
            break
        clipped.append(escaped_char)
        clipped_length += len(escaped_char)
    return prefix + "".join(clipped) + "…"


def status_label(status: TicketStatus) -> str:
    return {
        TicketStatus.NEW: "Новый",
        TicketStatus.IN_PROGRESS: "В работе",
        TicketStatus.WAITING_INFO: "Ожидание информации",
        TicketStatus.RESOLVED: "Решено",
        TicketStatus.REJECTED: "Отклонено",
        TicketStatus.DUPLICATE: "Дубль",
    }[status]


def priority_label(priority) -> str:
    return {
        "critical": "Критический",
        "high": "Высокий",
        "medium": "Средний",
        "low": "Низкий",
    }[str(priority)]


def ticket_keyboard(ticket: Ticket) -> InlineKeyboardMarkup | None:
    ver, ident = ticket.version, ticket.id
    buttons: list[list[InlineKeyboardButton]] = []
    if ticket.status == TicketStatus.NEW:
        buttons.append([InlineKeyboardButton("Взять", callback_data=f"t:take:{ident}:{ver}")])
    elif ticket.status in (TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO):
        if ticket.status == TicketStatus.IN_PROGRESS:
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Ожидание информации", callback_data=f"t:waiting:{ident}:{ver}"
                    )
                ]
            )
        else:
            buttons.append(
                [InlineKeyboardButton("В работу", callback_data=f"t:resume:{ident}:{ver}")]
            )
        buttons.append(
            [
                InlineKeyboardButton("Решено", callback_data=f"t:resolve:{ident}:{ver}"),
                InlineKeyboardButton("Отклонено", callback_data=f"t:reject:{ident}:{ver}"),
                InlineKeyboardButton("Дубль", callback_data=f"t:duplicate:{ident}:{ver}"),
            ]
        )
        buttons.append(
            [InlineKeyboardButton("Отказаться", callback_data=f"t:release:{ident}:{ver}")]
        )
    return InlineKeyboardMarkup(buttons) if buttons else None


async def sync_ticket(bot, ticket_id: int, *, force_new: bool = False) -> bool:
    async with engine.connect() as connection:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:seed, :ticket_id)"),
            {"seed": LOCK_SEED, "ticket_id": ticket_id},
        )
        if not acquired:
            return False
        try:
            async with SessionLocal() as session:
                ticket = await session.scalar(
                    select(Ticket)
                    .options(selectinload(Ticket.author), selectinload(Ticket.assignee))
                    .where(Ticket.id == ticket_id)
                )
                if ticket is None:
                    return False
                rendered_version = ticket.version
                text_body = render_ticket(ticket)
                markup = ticket_keyboard(ticket)
                message_id = None if force_new else ticket.message_id
                chat_id = get_settings().developer_chat_id
                if not chat_id:
                    raise RuntimeError("DEVELOPER_CHAT_ID is not configured")
                for attempt in range(4):
                    try:
                        if message_id is None:
                            sent = await bot.send_message(
                                chat_id=chat_id,
                                text=text_body,
                                parse_mode="HTML",
                                reply_markup=markup,
                            )
                            message_id = sent.message_id
                        else:
                            await bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=text_body,
                                parse_mode="HTML",
                                reply_markup=markup,
                            )
                        break
                    except RetryAfter as error:
                        if attempt == 3:
                            raise
                        delay = error.retry_after
                        seconds = (
                            delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                        )
                        await asyncio.sleep(float(seconds))
                    except NetworkError:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(0.5 * (2**attempt))
                    except BadRequest as error:
                        detail = str(error).lower()
                        if "message is not modified" in detail:
                            break
                        if message_id is not None and (
                            "message to edit not found" in detail
                            or "message_id_invalid" in detail
                            or "message can't be edited" in detail
                        ):
                            message_id = None
                            continue
                        raise
                    except TelegramError:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(0.5 * (2**attempt))
                async with SessionLocal.begin() as write_session:
                    await write_session.execute(
                        text("""UPDATE tickets SET message_id=:message_id,
                 post_synced_version=:version, updated_at=now() WHERE id=:ticket_id"""),
                        {
                            "message_id": message_id,
                            "version": rendered_version,
                            "ticket_id": ticket_id,
                        },
                    )
                return True
        finally:
            try:
                released = await connection.scalar(
                    text("SELECT pg_advisory_unlock(:seed, :ticket_id)"),
                    {"seed": LOCK_SEED, "ticket_id": ticket_id},
                )
                if not released:
                    await connection.invalidate()
            except BaseException:
                await connection.invalidate()
                raise


async def handle_ticket_callback(update, context) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    try:
        prefix, action, ticket_id, version = query.data.split(":")
        if prefix != "t":
            raise ValueError
        ticket_id, expected_version = int(ticket_id), int(version)
    except ValueError:
        await query.answer("Действие устарело", show_alert=True)
        return
    bind_contextvars(ticket_id=ticket_id, component="telegram_ticket")
    async with SessionLocal.begin() as session:
        actor = await get_or_register_user(session, update.effective_user)
        ticket = await session.get(Ticket, ticket_id)
        if actor is None or ticket is None:
            error = TicketError("Нет доступа")
        else:
            try:
                await transition(
                    session,
                    actor=actor,
                    ticket_id=ticket_id,
                    expected_version=expected_version,
                    expected_status=ticket.status,
                    action=action,
                )
                error = None
            except TicketError as exc:
                error = exc
    if error:
        message = "Обращение изменилось" if error.conflict else str(error)
        if action == "take" and error.conflict:
            message = "Уже взято"
        await query.answer(message, show_alert=True)
        await sync_ticket(context.bot, ticket_id)
        return
    await query.answer()
    await sync_ticket(context.bot, ticket_id)


async def sync_outdated(bot) -> None:
    async with SessionLocal() as session:
        ids = list(
            (
                await session.scalars(
                    select(Ticket.id)
                    .where(
                        (Ticket.message_id.is_(None))
                        | (Ticket.post_synced_version < Ticket.version)
                    )
                    .order_by(Ticket.id)
                    .limit(100)
                )
            ).all()
        )
    for ticket_id in ids:
        try:
            await sync_ticket(bot, ticket_id)
        except TelegramError:
            log.exception("ticket_post_sync_failed", ticket_id=ticket_id)
