class DomainError(Exception):
    """Base class for business errors that are shown to the user."""


class Forbidden(DomainError):
    """The actor's role or relation to the ticket does not allow the action."""


class InvalidTransition(DomainError):
    """The action is not defined for the current status."""


class StaleTicket(DomainError):
    """Expected status or version does not match: the ticket changed."""


class AlreadyTaken(DomainError):
    """Someone else already took the ticket."""


class NotFound(DomainError):
    """Requested object does not exist."""


class ValidationFailed(DomainError):
    """User input does not satisfy the rules."""


class RoleChangeBlocked(DomainError):
    """The user still has tickets that require an assignee role."""

    def __init__(self, ticket_ids: list[int]) -> None:
        super().__init__(f"Есть активные обращения: {ticket_ids}")
        self.ticket_ids = ticket_ids
