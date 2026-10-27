class BusinessError(Exception):
    def __init__(self, message: str, *, code: str = "invalid", ticket_ids: list[int] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.ticket_ids = ticket_ids or []


class Forbidden(BusinessError):
    def __init__(self, message: str = "Недостаточно прав"):
        super().__init__(message, code="forbidden")


class Conflict(BusinessError):
    def __init__(self, message: str = "Обращение изменилось", **kwargs: object):
        super().__init__(message, code="conflict")


class NotFound(BusinessError):
    def __init__(self, message: str = "Обращение не найдено"):
        super().__init__(message, code="not_found")
