from collections.abc import Callable, Iterator

from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

# Counters and histograms are summed across pods with sum().
# The open-tickets gauge is identical on every replica, so aggregate it with max().

TICKETS_CREATED = Counter("tickets_created_total", "Tickets created", ["priority"])
TICKET_TRANSITIONS = Counter(
    "ticket_transitions_total", "Business transitions", ["action", "to_status"]
)
TIME_TO_TAKE = Histogram(
    "ticket_time_to_take_seconds",
    "Seconds from the last entry into new until the ticket is taken",
    buckets=(60, 300, 600, 1800, 3600, 7200, 14400, 28800, 86400),
)
UPDATES_PROCESSED = Counter("updates_processed_total", "Telegram updates processed")
UPDATES_DUPLICATE = Counter("updates_duplicate_total", "Telegram updates dropped as duplicates")
REMINDERS_SENT = Counter("reminders_sent_total", "Reminders sent")
ERRORS = Counter("errors_total", "Errors by component", ["component"])
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"]
)


class OpenTicketsCollector(Collector):
    """Open tickets per status, read from the database at every scrape."""

    def __init__(self, fetch: Callable[[], dict[str, int]]) -> None:
        self._fetch = fetch

    def describe(self) -> list[GaugeMetricFamily]:
        # Declared without collecting: registering must not run the database query.
        return [GaugeMetricFamily("tickets_open", "Open tickets by status", labels=["status"])]

    def collect(self) -> Iterator[GaugeMetricFamily]:
        gauge = GaugeMetricFamily("tickets_open", "Open tickets by status", labels=["status"])
        for status, count in sorted(self._fetch().items()):
            gauge.add_metric([status], count)
        yield gauge
