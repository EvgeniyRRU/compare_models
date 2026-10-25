from typing import Annotated

import structlog
from fastapi import Depends, Header, HTTPException, Request, status

from support_bot.api.auth import InitDataError, validate_init_data
from support_bot.container import Container
from support_bot.domain.errors import AccessDeniedError
from support_bot.services.users import TelegramProfile
from support_bot.services.views import UserInfo


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def current_user(
    container: ContainerDep,
    authorization: Annotated[str | None, Header()] = None,
) -> UserInfo:
    """Пользователь Mini App по initData (заголовок `Authorization: tma <initData>`)."""
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется initData Telegram")
    s = container.settings
    try:
        tg = validate_init_data(
            authorization.removeprefix("tma "),
            s.bot_token.get_secret_value(),
            s.init_data_max_age_seconds,
        )
    except InitDataError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Некорректные initData") from exc
    structlog.contextvars.bind_contextvars(telegram_user_id=tg.id)
    user = await container.users.touch(
        TelegramProfile(tg.id, tg.username, tg.first_name, tg.last_name)
    )
    if user.role is None:
        raise AccessDeniedError()
    return user


UserDep = Annotated[UserInfo, Depends(current_user)]
