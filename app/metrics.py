"""Prometheus-метрики.

Счётчики и гистограммы суммируются по подам через sum().
Gauge tickets_open_by_status вычисляется запросом к БД при скрейпе: каждая
реплика отдаёт одинаковое значение — агрегировать через max(), не sum() (README).
"""

from time import perf_counter
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from prometheus_client import Counter, Gauge, Histogram

if TYPE_CHECKING:
    from app.db import DB

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


def add_http_metrics(app: FastAPI) -> None:
    """Middleware: число запросов, latency, коды. В label — ШАБЛОН пути (кардинальность)."""

    @app.middleware("http")
    async def http_metrics(request: Request, call_next: Any) -> Any:
        start = perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path_format = getattr(route, "path_format", None)
        template = path_format if path_format else "unmatched"
        http_requests_total.labels(
            method=request.method, route=template, status=str(response.status_code)
        ).inc()
        http_request_duration_seconds.labels(method=request.method, route=template).observe(
            perf_counter() - start
        )
        return response


def make_metrics_app(db: DB) -> FastAPI:
    """Отдельное ASGI-приложение для /metrics (свой порт, не через ingress)."""
    from fastapi.responses import Response
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
    from sqlalchemy import func, select

    from app.models import Ticket
    from app.states import STATUSES

    app = FastAPI()

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        # Gauge «открытые обращения по статусу» — запрос к БД при скрейпе.
        # Каждая реплика отдаёт одинаковое значение → агрегировать max(), не sum().
        async with db.session_factory() as s:
            rows = (
                await s.execute(
                    select(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status)
                )
            ).all()
        counts = {st: 0 for st in STATUSES}
        for st, n in rows:
            counts[st] = n
        for st in STATUSES:
            open_tickets_by_status.labels(status=st).set(counts[st])
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app


def start_metrics_server(port: int, db: DB) -> tuple[Any, Any]:
    """Второй uvicorn-сервер в том же процессе (один uvicorn-процесс на pod, два порта)."""
    import asyncio

    import uvicorn

    config = uvicorn.Config(make_metrics_app(db), host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="metrics-server")
    return server, task
