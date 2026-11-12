from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"]
)
TICKETS_CREATED = Counter("tickets_created_total", "Created tickets", ["priority"])
STATUS_TRANSITIONS = Counter(
    "ticket_status_transitions_total", "Ticket status transitions", ["from_status", "to_status"]
)
TIME_TO_TAKE = Histogram("ticket_time_to_take_seconds", "Time from entering new to take")
UPDATES_PROCESSED = Counter("telegram_updates_processed_total", "Processed Telegram updates")
UPDATES_DUPLICATED = Counter("telegram_updates_duplicated_total", "Deduplicated Telegram updates")
REMINDERS_SENT = Counter("ticket_reminders_sent_total", "Reminder send attempts")
APP_ERRORS = Counter("application_errors_total", "Application errors", ["component"])
OPEN_TICKETS = Gauge("open_tickets", "Open tickets by status", ["status"])
