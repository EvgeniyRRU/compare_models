"""Prometheus-метрики.

Счётчики и гистограммы суммируются по подам через sum().
Gauge tickets_open_by_status вычисляется запросом к БД при скрейпе: каждая
реплика отдаёт одинаковое значение — агрегировать через max(), не sum() (README).
"""

from prometheus_client import Counter, Gauge, Histogram

http_requests_total = Counter(
    "http_requests_total", "Число HTTP-запросов", ["method", "route", "status"]
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "Латентность HTTP-запросов", ["method", "route"]
)

tickets_created_total = Counter("tickets_created_total", "Созданные обращения", ["priority"])
ticket_transitions_total = Counter(
    "ticket_transitions_total", "Переходы статусов", ["from_status", "to_status"]
)
ticket_take_duration_seconds = Histogram(
    "ticket_take_duration_seconds",
    "Время от входа в new до взятия",
    buckets=(60, 300, 900, 3600, 14400, 86400, 259200),
)
telegram_updates_total = Counter("telegram_updates_total", "Апдейты Telegram", ["result"])
reminders_sent_total = Counter("reminders_sent_total", "Отправленные напоминания", ["result"])
errors_total = Counter("errors_total", "Ошибки", ["component"])

open_tickets_by_status = Gauge(
    "tickets_open_by_status", "Открытые обращения по статусу (запрос к БД)", ["status"]
)
