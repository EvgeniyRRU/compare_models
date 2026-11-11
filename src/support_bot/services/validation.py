from support_bot.domain.errors import ValidationFailed

DESCRIPTION_MAX = 3000
ID_MAX = 64


def required_text(value: str, *, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValidationFailed(f"Поле «{field}» не может быть пустым")
    if len(cleaned) > max_length:
        raise ValidationFailed(f"Поле «{field}» длиннее {max_length} символов")
    return cleaned


def optional_text(value: str | None, *, field: str, max_length: int) -> str | None:
    if value is None or not value.strip():
        return None
    return required_text(value, field=field, max_length=max_length)
