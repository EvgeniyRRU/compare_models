"""HTTP adapters for domain errors: one mapping table for every /api router."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domain.errors import (
    DomainError,
    Forbidden,
    InvalidTransition,
    NotFound,
    RoleChangeBlocked,
    ValidationError,
    VersionConflict,
)


class AuthError(Exception):
    """Bad / expired initData signature -> 401."""

    def __init__(self, message: str = "Сессия недействительна") -> None:
        super().__init__(message)
        self.message = message


def _status_for(exc: DomainError) -> int:
    # Subclass order matters: AlreadyTaken is a VersionConflict (409), etc.
    if isinstance(exc, RoleChangeBlocked):
        return 409
    if isinstance(exc, VersionConflict):
        return 409
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, Forbidden):
        return 403
    if isinstance(exc, (ValidationError, InvalidTransition)):
        return 422
    return 400


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": exc.message})

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        body: dict[str, object] = {"error": exc.message}
        if isinstance(exc, RoleChangeBlocked):
            body["active_ticket_ids"] = exc.active_ticket_ids
        return JSONResponse(status_code=_status_for(exc), content=body)
