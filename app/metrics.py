"""Prometheus metrics — a clean leaf module with no app imports except config.

Every metric is a module-level object registered with the default
``prometheus_client.REGISTRY``.  The metrics endpoint (served on a separate
port) refreshes the open-tickets gauge on each scrape.
"""

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# HTTP metrics  (labels: method, route)
# ---------------------------------------------------------------------------
http_requests = Counter(
    "rent_support_http_requests_total",
    "Total HTTP requests handled by the application",
    ["method", "route", "status"],
)
http_latency = Histogram(
    "rent_support_http_latency_seconds",
    "HTTP request latency in seconds",
    ["method", "route"],
)

# ---------------------------------------------------------------------------
# Business metrics
# ---------------------------------------------------------------------------
tickets_created = Counter(
    "rent_support_tickets_created_total",
    "Tickets created, partitioned by priority",
    ["priority"],
)
status_transitions = Counter(
    "rent_support_status_transitions_total",
    "Ticket status transitions",
    ["from", "to"],
)
updates_processed = Counter(
    "rent_support_updates_processed_total",
    "Telegram updates successfully processed",
)
updates_deduplicated = Counter(
    "rent_support_updates_deduplicated_total",
    "Duplicate Telegram updates skipped",
)
reminders_sent = Counter(
    "rent_support_reminders_sent_total",
    "Reminder messages successfully sent",
)
errors_total = Counter(
    "rent_support_errors_total",
    "Application errors, partitioned by component",
    ["component"],
)

# Histogram: time from ``new`` to ``in_progress`` (take).  Buckets span a
# realistic range from 1 minute through 24 hours.
ticket_time_to_take = Histogram(
    "rent_support_ticket_time_to_take_seconds",
    "Time from status=new to status=in_progress (take)",
    buckets=[60, 300, 600, 1800, 3600, 7200, 14400, 43200, 86400],
)

# Gauge: open tickets partitioned by status, refreshed on each /metrics scrape.
open_tickets_by_status = Gauge(
    "rent_support_open_tickets_total",
    "Open tickets grouped by status",
    ["status"],
)

# ---------------------------------------------------------------------------
# Refresh helper
# ---------------------------------------------------------------------------

_OPEN_STATUSES = {"new", "in_progress", "waiting_info"}


async def refresh_open_tickets_gauge(session: Any) -> None:
    """Query the database for open-ticket counts and update the gauge labels.

    ``session`` must be an active SQLAlchemy ``AsyncSession``.
    """
    # Late import so this module stays leaf-friendly (no SQLAlchemy at import
    # time).
    from sqlalchemy import text  # noqa: PLC0415

    result = await session.execute(
        text(
            "SELECT status, COUNT(*) FROM tickets WHERE status = ANY(:statuses) GROUP BY status",
        ),
        {"statuses": list(_OPEN_STATUSES)},
    )
    current: dict[str, float] = {s: 0.0 for s in _OPEN_STATUSES}
    for row in result.fetchall():
        status_str: str = row[0]
        count_val: int = row[1]
        current[status_str] = float(count_val)

    # Clear any stale labels first, then set the current values.
    open_tickets_by_status.clear()
    for status, count in current.items():
        open_tickets_by_status.labels(status=status).set(count)
