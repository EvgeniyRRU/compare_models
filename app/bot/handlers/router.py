"""Update routing: parsed-plain-data -> handler cores. No PTB types here."""

from app.bot.handlers import create_form, ticket_actions
from app.bot.handlers.common import Deps, parse_command, toast


async def route_message(
    deps: Deps,
    *,
    user_id: int,
    chat_id: int,
    username: str | None,
    full_name: str | None,
    text: str,
    reply_to_message_id: int | None,
) -> None:
    if text.startswith("/"):
        cmd = parse_command(text)
        if cmd == "start":
            await create_form.handle_start(deps, user_id, chat_id, username, full_name)
        elif cmd == "help":
            await create_form.handle_help(deps, chat_id)
        elif cmd == "cancel":
            await create_form.handle_cancel(deps, user_id, chat_id)
        elif cmd == "new":
            await create_form.handle_new(deps, user_id, chat_id, username, full_name)
        return
    await create_form.handle_text(
        deps,
        user_id=user_id,
        chat_id=chat_id,
        text=text,
        reply_to_message_id=reply_to_message_id,
    )


async def route_callback(
    deps: Deps, *, user_id: int, callback_id: str, chat_id: int, data: str
) -> None:
    if data.startswith("d:"):
        try:
            draft_id, act, version = create_form.parse_draft_callback(data)
        except ValueError:
            await toast(deps.gateway, callback_id, "Кнопка не распознана", alert=True)
            return
        await create_form.handle_draft_callback(
            deps,
            user_id=user_id,
            callback_id=callback_id,
            chat_id=chat_id,
            draft_id=draft_id,
            act=act,
            expected_version=version,
        )
    elif data.startswith("t:"):
        try:
            action, ticket_id, version = ticket_actions.parse_ticket_callback(data)
        except ValueError:
            await toast(deps.gateway, callback_id, "Кнопка не распознана", alert=True)
            return
        await ticket_actions.handle_ticket_callback(
            deps,
            user_id=user_id,
            callback_id=callback_id,
            ticket_id=ticket_id,
            action=action,
            expected_version=version,
        )
