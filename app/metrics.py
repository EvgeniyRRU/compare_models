"""Prometheus business metrics. Registry is per-process; pods sum via sum() except
the open-tickets gauge, which must be aggregated with max() (every replica reports
the same DB-derived value)."""

from prometheus_client import Counter, Histogram

TICKETS_CREATED = Counter("tickets_created_total", "Created tickets", ["priority"])
TICKET_TRANSITIONS = Counter(
    "ticket_transitions_total", "Ticket status transitions", ["from_status", "to_status"]
)
TICKET_TIME_TO_TAKE = Histogram(
    "ticket_time_to_take_seconds",
    "Time from entering status new to being taken",
    ["priority"],
    buckets=(60, 300, 600, 1800, 3600, 7200, 28800, 86400),
)
UPDATES_PROCESSED = Counter("updates_processed_total", "Telegram updates processed")
UPDATES_DEDUPLICATED = Counter(
    "updates_deduplicated_total", "Telegram updates skipped as duplicates"
)
REMINDERS_SENT = Counter("reminders_sent_total", "Reminder messages sent", ["priority"])
POST_SYNC_TOTAL = Counter("post_sync_total", "Post sync outcomes", ["result"])
APP_ERRORS = Counter("app_errors_total", "Handled errors", ["component"])
