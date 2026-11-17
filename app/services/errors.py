"""Ошибки сервисного слоя. Тонкие слои (бот/API) маппят их на alerts/HTTP-коды."""


class ServiceError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(ServiceError):
    pass


class PermissionDenied(ServiceError):
    pass


class StaleTicketError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Обращение изменилось. Повторите действие с актуальной версией.")


class AlreadyTakenError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Обращение уже взято.")


class ActiveTicketsError(ServiceError):
    def __init__(self, ticket_ids: list[int]):
        super().__init__("У пользователя есть обращения в работе.")
        self.ticket_ids = ticket_ids


class NoDraftError(ServiceError):
    def __init__(self) -> None:
        super().__init__("У вас нет активной формы.")


class StaleDraftError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Форма изменилась. Продолжите с текущего шага.")


class StaleAnswerError(ServiceError):
    def __init__(self) -> None:
        super().__init__("Ответ устарел. Ответьте на текущий вопрос формы.")


class DraftValidationError(ServiceError):
    def __init__(self, field: str):
        super().__init__(f"Некорректное значение поля {field}.")
        self.field = field
