from __future__ import annotations

import structlog

from support_bot.db.session import SessionFactory
from support_bot.domain.reminders import ReminderPolicy
from support_bot.observability.metrics import observe_error, observe_reminder_sent
from support_bot.observability.sentry import capture_exception
from support_bot.services.post_sync import PostPublisher
from support_bot.services.reminders import claim_due_reminders, render_reminder_text


async def reminders_tick(
    session_factory: SessionFactory,
    publisher: PostPublisher,
    policy: ReminderPolicy,
    *,
    chat_id: int,
    batch_size: int,
) -> int:
    logger = structlog.get_logger(__name__)
    reminders = await claim_due_reminders(session_factory, policy, batch_size=batch_size)
    sent = 0
    for reminder in reminders:
        try:
            await publisher.send_reminder(
                chat_id,
                render_reminder_text(reminder.ticket_id, reminder.priority),
                reminder.message_id,
            )
            observe_reminder_sent(reminder.priority)
            sent += 1
        except Exception as exc:
            logger.exception(
                "reminder delivery failed",
                component="reminders",
                ticket_id=reminder.ticket_id,
            )
            observe_error("reminders")
            capture_exception(exc, component="reminders", ticket_id=reminder.ticket_id)
    return sent
