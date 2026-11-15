"""Domain errors. HTTP / Telegram adapters map them to 4xx codes and alerts."""


class DomainError(Exception):
    def __init__(self, message: str = "Ошибка") -> None:
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    def __init__(self, message: str = "Обращение не найдено") -> None:
        super().__init__(message)


class Forbidden(DomainError):
    def __init__(self, message: str = "Недостаточно прав") -> None:
        super().__init__(message)


class ValidationError(DomainError):
    def __init__(self, message: str = "Некорректные данные") -> None:
        super().__init__(message)


class InvalidTransition(DomainError):
    def __init__(self, message: str = "Недопустимый переход статуса") -> None:
        super().__init__(message)


class VersionConflict(DomainError):
    """Ticket changed since the action was rendered: stale button / stale Mini App request."""

    def __init__(self, message: str = "Обращение изменилось") -> None:
        super().__init__(message)


class AlreadyTaken(VersionConflict):
    def __init__(self, message: str = "Уже взято") -> None:
        super().__init__(message)


class RoleChangeBlocked(DomainError):
    def __init__(self, active_ticket_ids: list[int]) -> None:
        super().__init__("У пользователя есть активные обращения: сначала переназначьте их")
        self.active_ticket_ids = active_ticket_ids
