"""Mapping from service-layer exceptions to FastAPI HTTP responses."""

from typing import NoReturn

from fastapi import HTTPException

from app.services.role_service import UserNotFound
from app.services.ticket_service import (
    PermissionDenied,
    TicketAlreadyTaken,
    TicketChanged,
)


def raise_http_for_service_error(exc: Exception) -> NoReturn:
    """Translate a known service error into an ``HTTPException``.

    ``RoleChangeBlocked`` is deliberately NOT handled here: its response body
    carries an extra ``active_ticket_ids`` field, so callers construct a
    ``JSONResponse`` for it instead. Any unknown exception is re-raised and
    surfaces as a 500 through FastAPI's default handler.
    """
    if isinstance(exc, PermissionDenied):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, (TicketChanged, TicketAlreadyTaken)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, UserNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise exc
