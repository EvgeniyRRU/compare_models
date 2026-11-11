from collections.abc import Iterator

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

from support_bot.metrics import OpenTicketsCollector


def test_open_tickets_gauge_is_computed_at_scrape_time() -> None:
    counts = {"new": 2, "in_progress": 1}
    calls = 0

    def fetch() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return counts

    registry = CollectorRegistry()
    registry.register(OpenTicketsCollector(fetch))
    counts["new"] = 5

    text = generate_latest(registry).decode()

    assert calls == 1
    assert 'tickets_open{status="new"} 5.0' in text
    assert 'tickets_open{status="in_progress"} 1.0' in text


def test_collector_yields_gauge_family_named_tickets_open() -> None:
    collector = OpenTicketsCollector(lambda: {})
    families: Iterator[GaugeMetricFamily] = iter(collector.collect())  # type: ignore[arg-type]

    family = next(families)

    assert family.name == "tickets_open"


def test_registering_does_not_query_the_database() -> None:
    calls = 0

    def fetch() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {}

    # The global REGISTRY uses auto_describe=True, which is how production registers it.
    CollectorRegistry(auto_describe=True).register(OpenTicketsCollector(fetch))

    assert calls == 0
