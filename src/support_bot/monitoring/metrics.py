"""Метрики Prometheus (стандартный registry, один процесс на pod).

Счётчики и гистограммы суммируются по подам через sum(). Gauge открытых обращений
считается запросом к БД при скрейпе (см. collector.py) и агрегируется через max().
"""

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "support_bot_http_requests_total",
    "HTTP-запросы",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "support_bot_http_request_duration_seconds",
    "Длительность HTTP-запросов",
    ["method", "route"],
)
TICKETS_CREATED = Counter("support_bot_tickets_created_total", "Созданные обращения", ["priority"])
TICKET_TRANSITIONS = Counter(
    "support_bot_ticket_transitions_total",
    "Переходы статусов обращений",
    ["action", "from_status", "to_status"],
)
TIME_TO_TAKE = Histogram(
    "support_bot_ticket_time_to_take_seconds",
    "Время от входа в new до взятия",
    ["priority"],
    buckets=(30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 14400, 28800, 86400, float("inf")),
)
UPDATES = Counter(
    "support_bot_telegram_updates_total",
    "Апдейты Telegram: processed / duplicate / failed / timeout",
    ["result"],
)
REMINDERS = Counter("support_bot_reminders_total", "Напоминания: sent / failed", ["result"])
POST_SYNCS = Counter(
    "support_bot_post_syncs_total",
    "Синхронизации постов: sent / edited / skipped / failed",
    ["result"],
)
ERRORS = Counter("support_bot_errors_total", "Ошибки по компонентам", ["component"])
