from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from support_bot.config import Settings
from support_bot.db.enums import TicketPriority


@dataclass(frozen=True, slots=True)
class ReminderPolicy:
    critical: timedelta
    high: timedelta
    medium: timedelta
    low: timedelta

    def interval_for(self, priority: TicketPriority) -> timedelta:
        return {
            TicketPriority.CRITICAL: self.critical,
            TicketPriority.HIGH: self.high,
            TicketPriority.MEDIUM: self.medium,
            TicketPriority.LOW: self.low,
        }[priority]

    @classmethod
    def from_settings(cls, settings: Settings) -> ReminderPolicy:
        return cls(
            critical=timedelta(minutes=settings.reminder_critical_minutes),
            high=timedelta(minutes=settings.reminder_high_minutes),
            medium=timedelta(minutes=settings.reminder_medium_minutes),
            low=timedelta(minutes=settings.reminder_low_minutes),
        )
