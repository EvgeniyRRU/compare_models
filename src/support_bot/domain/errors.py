from __future__ import annotations


class DomainError(Exception):
    """Base class for expected business errors."""


class TicketNotFound(DomainError):
    pass


class UserNotFound(DomainError):
    pass


class VersionConflict(DomainError):
    """The ticket changed since the user received the button/state."""


class StatusConflict(DomainError):
    """The action is not allowed in the current ticket status."""


class AlreadyTaken(DomainError):
    """Another developer took the ticket first."""


class PermissionDenied(DomainError):
    pass


class ValidationError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RoleChangeConflict(DomainError):
    def __init__(self, ticket_ids: list[int]) -> None:
        super().__init__("user has active tickets")
        self.ticket_ids = ticket_ids


class NoActiveDraft(DomainError):
    pass


class DraftConflict(DomainError):
    """Draft id/version/step does not match the current draft."""


class DraftValidationError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LockUnavailable(DomainError):
    pass
