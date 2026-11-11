from enum import StrEnum


class Role(StrEnum):
    SUPPORT = "support"
    DEVELOPER = "developer"
    ADMIN = "admin"

    @property
    def can_create(self) -> bool:
        return self in (Role.SUPPORT, Role.ADMIN)

    @property
    def can_take(self) -> bool:
        return self in (Role.DEVELOPER, Role.ADMIN)

    @property
    def can_be_assignee(self) -> bool:
        return self in (Role.DEVELOPER, Role.ADMIN)
