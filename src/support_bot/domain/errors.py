"""Ошибки бизнес-слоя. Тексты показываются пользователю как есть."""


class DomainError(Exception):
    default_message = "Ошибка"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(DomainError):
    default_message = "Не найдено"


class AccessDeniedError(DomainError):
    """У пользователя нет роли: доступа к боту нет вообще."""

    default_message = "Нет доступа. Обратитесь к администратору, чтобы вам назначили роль."


class PermissionDeniedError(DomainError):
    default_message = "Недостаточно прав для этого действия"


class InvalidTransitionError(DomainError):
    default_message = "Действие недоступно в текущем статусе обращения"


class StaleVersionError(DomainError):
    default_message = "Обращение изменилось"


class AlreadyTakenError(StaleVersionError):
    default_message = "Уже взято"


class InvalidAssigneeError(DomainError):
    default_message = "Исполнителем может быть только пользователь с ролью developer или admin"


class RoleChangeBlockedError(DomainError):
    default_message = (
        "У пользователя есть обращения в работе. Сначала переназначьте их на другого исполнителя."
    )

    def __init__(self, ticket_ids: list[int], message: str | None = None) -> None:
        self.ticket_ids = ticket_ids
        super().__init__(message)


class ValidationFailedError(DomainError):
    default_message = "Некорректное значение"
