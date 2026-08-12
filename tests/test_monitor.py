"""End-to-end monitoring cycles with fake transports and fake notifiers.

These are the tests that would catch a regression nobody notices until a real
deal is missed, or worse, until a wrong alert is sent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from deal_watcher.config import Config, ProductConfig
from deal_watcher.fetchers import FetchError
from deal_watcher.models import AlertLevel
from deal_watcher.monitor import Monitor
from deal_watcher.storage import Storage

from .conftest import FakeFetcher, FakeFetcherFactory, RecordingNotifier, fixture


def kabum_page(items: list[dict[str, object]]) -> str:
    payload = {"props": {"pageProps": {"data": {"catalogServer": {"data": items}}}}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script>"
    )


def item(
    code: int = 1,
    name: str = "Placa de Video ASUS RTX 5060 Ti 16GB GDDR7 128 bits",
    price: float = 3199.99,
    available: bool = True,
    quantity: int = 10,
) -> dict[str, object]:
    return {
        "code": code,
        "name": name,
        "friendlyName": f"produto-{code}",
        "price": price,
        "priceWithDiscount": price,
        "available": available,
        "quantity": quantity,
        "flags": {"isOpenbox": False},
    }


@pytest.fixture
def build(tmp_path: Path, rtx_product: ProductConfig):
    """Returns a factory for (monitor, storage, notifier) over canned pages."""

    def _build(
        pages: dict[str, str | Exception], stores: dict[str, dict[str, object]] | None = None
    ):
        config = Config(
            products=(rtx_product,),
            stores=stores or {"kabum": {"enabled": True, "fetcher": "http"}},  # type: ignore[arg-type]
            database=tmp_path / "monitor.db",
        )
        storage = Storage(config.database)
        notifier = RecordingNotifier()
        monitor = Monitor(
            config, storage, [notifier], fetchers=FakeFetcherFactory(FakeFetcher(pages))
        )
        return monitor, storage, notifier

    return _build


class TestHappyPath:
    def test_a_deal_under_target_produces_one_alert(self, build) -> None:
        monitor, storage, notifier = build({"kabum": kabum_page([item(price=3199.99)])})
        report = monitor.run_cycle()

        assert len(notifier.sent) == 1
        alert = notifier.sent[0]
        assert alert.level is AlertLevel.EXCELLENT
        assert alert.offer.price == Decimal("3199.99")
        assert alert.savings == Decimal("100.01")
        assert report.stores_ok == 1 and report.stores_failed == 0
        storage.close()

    def test_a_price_above_target_alerts_nobody(self, build) -> None:
        monitor, storage, notifier = build({"kabum": kabum_page([item(price=3499.00)])})
        monitor.run_cycle()
        assert notifier.sent == []
        # But it is still recorded, so history and trends survive.
        assert len(storage.history()) == 1
        storage.close()

    def test_the_wrong_card_is_never_alerted_even_when_it_is_cheap(self, build) -> None:
        monitor, storage, notifier = build(
            {
                "kabum": kabum_page(
                    [
                        item(
                            code=1,
                            name="Placa de Video RTX 5060 Ti GAMING OC 8G, 8GB",
                            price=2599.0,
                        ),
                        item(code=2, name="Placa de Video RTX 5060, 16GB GDDR7", price=2499.0),
                        item(code=3, name="PC Gamer com RTX 5060 Ti 16GB", price=2999.0),
                    ]
                )
            }
        )
        monitor.run_cycle()
        assert notifier.sent == []
        assert storage.history() == ()  # nothing matched, nothing recorded
        storage.close()

    def test_out_of_stock_bargains_are_ignored(self, build) -> None:
        monitor, storage, notifier = build(
            {"kabum": kabum_page([item(price=2500.0, available=False, quantity=0)])}
        )
        monitor.run_cycle()
        assert notifier.sent == []
        storage.close()

    def test_alert_levels_follow_the_price(self, build) -> None:
        for price, expected in (
            (3299.0, AlertLevel.GOOD),
            (3150.0, AlertLevel.EXCELLENT),
            (2950.0, AlertLevel.BUY_NOW),
        ):
            monitor, storage, notifier = build({"kabum": kabum_page([item(price=price)])})
            monitor.run_cycle()
            assert notifier.sent[0].level is expected
            storage.close()


class TestNoSpam:
    def test_the_same_deal_is_announced_once(self, build) -> None:
        page = kabum_page([item(price=3199.0)])
        monitor, storage, notifier = build({"kabum": page})
        for _ in range(8):  # two hours of 15-minute cycles
            monitor.run_cycle()
        assert len(notifier.sent) == 1
        assert len(storage.history()) == 8  # history still records every sighting
        storage.close()

    def test_a_better_level_breaks_the_silence(self, build) -> None:
        fetcher = FakeFetcher({"kabum": kabum_page([item(price=3250.0)])})
        monitor, storage, notifier = build({"kabum": ""})
        monitor.fetchers = FakeFetcherFactory(fetcher)

        monitor.run_cycle()
        fetcher.pages["kabum"] = kabum_page([item(price=2990.0)])
        monitor.run_cycle()

        assert [alert.level for alert in notifier.sent] == [
            AlertLevel.GOOD,
            AlertLevel.BUY_NOW,
        ]
        storage.close()

    def test_rising_above_target_then_dropping_again_re_alerts(self, build) -> None:
        fetcher = FakeFetcher({"kabum": kabum_page([item(price=3199.0)])})
        monitor, storage, notifier = build({"kabum": ""})
        monitor.fetchers = FakeFetcherFactory(fetcher)

        monitor.run_cycle()  # deal -> alert
        fetcher.pages["kabum"] = kabum_page([item(price=3500.0)])
        monitor.run_cycle()  # gone -> armed
        fetcher.pages["kabum"] = kabum_page([item(price=3199.0)])
        monitor.run_cycle()  # back -> alert

        assert len(notifier.sent) == 2
        storage.close()

    def test_a_failed_delivery_is_retried_next_cycle(self, tmp_path: Path, rtx_product) -> None:
        config = Config(
            products=(rtx_product,),
            stores={"kabum": {"enabled": True, "fetcher": "http"}},  # type: ignore[arg-type]
            database=tmp_path / "retry.db",
        )
        storage = Storage(config.database)
        notifier = RecordingNotifier(fail=True)
        fetchers = FakeFetcherFactory(FakeFetcher({"kabum": kabum_page([item(price=3199.0)])}))
        monitor = Monitor(config, storage, [notifier], fetchers=fetchers)

        monitor.run_cycle()
        # Nothing got through, so nothing may be marked as notified.
        assert storage.notification_state("KaBuM!:1") is None

        notifier.fail = False
        monitor.run_cycle()
        assert len(notifier.sent) == 1
        storage.close()


class TestResilience:
    def test_one_broken_store_does_not_stop_the_others(self, tmp_path: Path, rtx_product) -> None:
        config = Config(
            products=(
                rtx_product.model_copy(
                    update={
                        "queries": {"terabyte": "placa-de-video rtx 5060 ti 16gb"},
                        "stores": (),
                    }
                ),
            ),
            stores={  # type: ignore[arg-type]
                "kabum": {"enabled": True, "fetcher": "http"},
                "terabyte": {"enabled": True, "fetcher": "http"},
                "pichau": {"enabled": True, "fetcher": "http"},
            },
            database=tmp_path / "resilient.db",
        )
        storage = Storage(config.database)
        notifier = RecordingNotifier()
        fetcher = FakeFetcher(
            {
                "kabum.com.br": kabum_page([item(price=3199.0)]),
                "sitemap-manus": (
                    "<urlset><url><loc>https://www.terabyteshop.com.br/produto/1/"
                    "placa-de-video-rtx-5060-ti-16gb</loc></url></urlset>"
                ),
                "terabyteshop.com.br/produto": fixture("terabyte_product.html"),
                "pichau": FetchError("HTTP 403"),  # store is angry today
            }
        )
        monitor = Monitor(config, storage, [notifier], fetchers=FakeFetcherFactory(fetcher))

        report = monitor.run_cycle()

        assert report.stores_failed == 1
        assert report.stores_ok == 2
        assert len(notifier.sent) == 1  # the KaBuM deal still went out
        errors = [result.error for result in report.store_results if not result.ok]
        assert errors and "403" in errors[0]
        storage.close()

    def test_a_cycle_is_recorded_even_when_every_store_fails(self, build) -> None:
        monitor, storage, notifier = build({"kabum": FetchError("HTTP 503")})
        report = monitor.run_cycle()
        assert report.stores_failed == 1
        assert notifier.sent == []
        last = storage.last_run()
        assert last is not None and last.stores_failed == 1
        storage.close()

    def test_a_broken_notifier_does_not_kill_the_cycle(self, tmp_path: Path, rtx_product) -> None:
        config = Config(
            products=(rtx_product,),
            stores={"kabum": {"enabled": True, "fetcher": "http"}},  # type: ignore[arg-type]
            database=tmp_path / "notifier.db",
        )
        storage = Storage(config.database)
        working, broken = RecordingNotifier(), RecordingNotifier(fail=True)
        monitor = Monitor(
            config,
            storage,
            [broken, working],
            fetchers=FakeFetcherFactory(FakeFetcher({"kabum": kabum_page([item(price=3199.0)])})),
        )
        monitor.run_cycle()
        assert len(working.sent) == 1
        storage.close()


class TestDryRun:
    def test_reports_without_sending_or_remembering(self, build) -> None:
        monitor, storage, notifier = build({"kabum": kabum_page([item(price=3199.0)])})
        report = monitor.run_cycle(dry_run=True)
        assert len(report.alerts_sent) == 1
        assert notifier.sent == []
        assert storage.notification_state("KaBuM!:1") is None
        storage.close()


class TestRunHealthRecording:
    def test_every_cycle_updates_health(self, build) -> None:
        monitor, storage, _ = build({"kabum": kabum_page([item(price=3199.0)])})
        monitor.run_cycle()
        last = storage.last_run()
        assert last is not None
        assert last.stores_ok == 1
        assert last.matches_found == 1
        assert last.alerts_sent == 1
        assert last.finished_at >= last.started_at
        storage.close()
