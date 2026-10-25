from dataclasses import dataclass

from support_bot.domain.enums import Priority
from support_bot.domain.errors import ValidationFailedError

DESCRIPTION_MAX_LEN = 3000
REQUEST_ID_MAX_LEN = 64
TENANT_ID_MAX_LEN = 64


def clean_text(value: str | None, label: str, max_len: int) -> str:
    text = (value or "").strip()
    if not text:
        raise ValidationFailedError(f"{label} не может быть пустым.")
    if len(text) > max_len:
        raise ValidationFailedError(f"{label}: не больше {max_len} символов (сейчас {len(text)}).")
    return text


def clean_description(value: str | None) -> str:
    return clean_text(value, "Описание", DESCRIPTION_MAX_LEN)


def clean_request_id(value: str | None) -> str:
    return clean_text(value, "Id заявки", REQUEST_ID_MAX_LEN)


def clean_tenant_id(value: str | None) -> str:
    return clean_text(value, "Id арендатора", TENANT_ID_MAX_LEN)


@dataclass(frozen=True)
class TicketData:
    description: str
    priority: Priority
    request_id: str
    tenant_id: str | None

    @classmethod
    def validated(
        cls,
        description: str | None,
        priority: Priority | None,
        request_id: str | None,
        tenant_id: str | None,
    ) -> TicketData:
        if priority is None:
            raise ValidationFailedError("Не выбран приоритет.")
        return cls(
            description=clean_description(description),
            priority=priority,
            request_id=clean_request_id(request_id),
            tenant_id=clean_tenant_id(tenant_id) if tenant_id is not None else None,
        )
