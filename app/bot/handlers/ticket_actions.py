"""Ticket action buttons: t:<action>:<ticket_id>:<version>.

The version baked into the button is the optimistic-concurrency token: a press from
a stale post gets VersionConflict («Обращение изменилось» / «Уже взято») as an alert,
never a silent double action. Every successful action triggers a post sync; failures
leave the row untouched — the sweeper will re-publish.
"""

from app.bot.handlers.common import Deps, toast
from app.domain.errors import DomainError, Forbidden, NotFound
from app.domain.state_machine import Action
from app.logging_setup import get_logger
from app.services import tickets as tickets_svc
from app.services import users as users_svc
from app.services.post_sync import sync_ticket

log = get_logger("bot.ticket_actions")


def parse_ticket_callback(data: str) -> tuple[Action, int, int]:
    """'t:<act>:<id>:<version>' -> parts; ValueError on garbage."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "t":
        raise ValueError(f"bad ticket callback: {data!r}")
    return Action(parts[1]), int(parts[2]), int(parts[3])


async def handle_ticket_callback(
    deps: Deps,
    *,
    user_id: int,
    callback_id: str,
    ticket_id: int,
    action: Action,
    expected_version: int,
) -> None:
    try:
        async with deps.session_factory() as session, session.begin():
            role = await users_svc.get_role(session, user_id)
            if role is None:
                raise Forbidden("Доступ к боту выдаёт администратор")
            if action is Action.take:
                await tickets_svc.take(
                    session,
                    deps.settings,
                    ticket_id=ticket_id,
                    actor_id=user_id,
                    expected_version=expected_version,
                )
            else:
                await tickets_svc.apply_action(
                    session,
                    deps.settings,
                    ticket_id=ticket_id,
                    actor_id=user_id,
                    action=action,
                    expected_version=expected_version,
                )
    except NotFound:
        await toast(deps.gateway, callback_id, "Обращение не найдено", alert=True)
        return
    except DomainError as exc:
        # Includes VersionConflict («Обращение изменилось») and AlreadyTaken («Уже взято»).
        await toast(deps.gateway, callback_id, exc.message, alert=True)
        return

    await toast(deps.gateway, callback_id, "Готово")
    try:
        # A press on a ticket whose post is missing/stale just brings the post up to date.
        await sync_ticket(deps.gateway, deps.session_factory, deps.settings, ticket_id)
    except Exception:
        log.exception(
            "post sync after action failed", component="ticket_actions", ticket_id=ticket_id
        )
