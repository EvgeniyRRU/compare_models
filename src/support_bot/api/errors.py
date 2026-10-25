from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from support_bot.domain.errors import (
    AccessDeniedError,
    AlreadyTakenError,
    DomainError,
    InvalidAssigneeError,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
    RoleChangeBlockedError,
    StaleVersionError,
    ValidationFailedError,
)

# (HTTP-статус, код ошибки) — проверяется по порядку, подклассы раньше базовых.
_MAPPING: list[tuple[type[DomainError], int, str]] = [
    (NotFoundError, 404, "not_found"),
    (AccessDeniedError, 403, "no_access"),
    (PermissionDeniedError, 403, "forbidden"),
    (AlreadyTakenError, 409, "already_taken"),
    (StaleVersionError, 409, "stale_version"),
    (InvalidTransitionError, 409, "invalid_transition"),
    (RoleChangeBlockedError, 409, "role_change_blocked"),
    (InvalidAssigneeError, 422, "invalid_assignee"),
    (ValidationFailedError, 422, "validation_failed"),
]


def error_response(exc: DomainError) -> JSONResponse:
    status_code, code = 400, "domain_error"
    for cls, sc, c in _MAPPING:
        if isinstance(exc, cls):
            status_code, code = sc, c
            break
    body: dict[str, object] = {"error": code, "detail": exc.message}
    if isinstance(exc, RoleChangeBlockedError):
        body["tickets"] = exc.ticket_ids
    return JSONResponse(body, status_code=status_code)


def install_error_handlers(app: FastAPI) -> None:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return error_response(exc)

    app.add_exception_handler(DomainError, handler)
