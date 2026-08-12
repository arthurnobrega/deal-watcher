"""SQLite persistence: history, notification state, and run health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from deal_watcher.models import AlertLevel
from deal_watcher.storage import RunStats, Storage

from .conftest import make_alert, make_offer


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    with Storage(tmp_path / "nested" / "test.db") as store:
        yield store


def test_creates_the_database_and_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "deal.db"
    with Storage(path):
        pass
    assert path.exists()


class TestPriceHistory:
    def test_records_and_reads_back(self, storage: Storage) -> None:
        storage.record_offer("RTX 5060 Ti 16GB", make_offer(price="3199.99"))
        entries = storage.history()
        assert len(entries) == 1
        assert entries[0].price == Decimal("3199.99")
        assert entries[0].store == "KaBuM!"

    def test_prices_survive_the_round_trip_exactly(self, storage: Storage) -> None:
        # Stored as TEXT precisely so this does not come back as 3199.9899999.
        storage.record_offer("p", make_offer(price="3199.99"))
        assert storage.history()[0].price == Decimal("3199.99")

    def test_history_is_newest_first(self, storage: Storage) -> None:
        for price in ("3400", "3300", "3200"):
            storage.record_offer("p", make_offer(price=price, raw_id=price))
        assert [e.price for e in storage.history()] == [
            Decimal("3200"),
            Decimal("3300"),
            Decimal("3400"),
        ]

    def test_filters_by_product_and_limit(self, storage: Storage) -> None:
        storage.record_offer("gpu", make_offer(raw_id="a"))
        storage.record_offer("cpu", make_offer(raw_id="b"))
        assert len(storage.history(product="gpu")) == 1
        assert len(storage.history(limit=1)) == 1

    def test_lowest_price_ignores_out_of_stock(self, storage: Storage) -> None:
        storage.record_offer("p", make_offer(price="3300", raw_id="a"))
        storage.record_offer("p", make_offer(price="2500", raw_id="b", available=False))
        assert storage.lowest_price("p") == Decimal("3300")

    def test_lowest_price_is_none_without_data(self, storage: Storage) -> None:
        assert storage.lowest_price("nothing") is None

    def test_prune_drops_only_old_rows(self, storage: Storage) -> None:
        old = make_offer(raw_id="old").__class__(
            store="KaBuM!",
            name="old",
            price=Decimal("3000"),
            url="u",
            available=True,
            raw_id="old",
            fetched_at=datetime.now(UTC) - timedelta(days=90),
        )
        storage.record_offer("p", old)
        storage.record_offer("p", make_offer(raw_id="new"))
        assert storage.prune(keep_days=30) == 1
        assert len(storage.history()) == 1


class TestNotificationState:
    def test_unknown_offer_has_no_state(self, storage: Storage) -> None:
        assert storage.notification_state("nope") is None

    def test_records_what_was_sent(self, storage: Storage) -> None:
        alert = make_alert(price="3199.99")
        storage.record_notification(alert)
        state = storage.notification_state(alert.offer.identity)
        assert state is not None
        assert state.price == Decimal("3199.99")
        assert state.level is AlertLevel.EXCELLENT
        assert state.recovered is False

    def test_second_notification_overwrites_the_first(self, storage: Storage) -> None:
        storage.record_notification(make_alert(price="3199.99"))
        storage.record_notification(make_alert(price="2999.00", level=AlertLevel.BUY_NOW))
        state = storage.notification_state("KaBuM!:1")
        assert state is not None
        assert state.price == Decimal("2999.00")
        assert state.level is AlertLevel.BUY_NOW

    def test_recovery_arms_the_next_alert(self, storage: Storage) -> None:
        alert = make_alert()
        storage.record_notification(alert)
        storage.mark_recovered(alert.offer.identity)
        state = storage.notification_state(alert.offer.identity)
        assert state is not None and state.recovered is True

    def test_notifying_again_clears_recovery(self, storage: Storage) -> None:
        alert = make_alert()
        storage.record_notification(alert)
        storage.mark_recovered(alert.offer.identity)
        storage.record_notification(alert)
        state = storage.notification_state(alert.offer.identity)
        assert state is not None and state.recovered is False

    def test_offer_identity_survives_a_renamed_listing(self) -> None:
        first = make_offer(name="RTX 5060 Ti 16GB", raw_id="42")
        renamed = make_offer(name="RTX 5060 Ti 16GB (nova embalagem)", raw_id="42")
        assert first.identity == renamed.identity


class TestRunHealth:
    def test_no_runs_yet(self, storage: Storage) -> None:
        assert storage.last_run() is None

    def test_records_and_returns_the_latest(self, storage: Storage) -> None:
        now = datetime.now(UTC)
        for index in range(3):
            storage.record_run(
                RunStats(
                    started_at=now + timedelta(minutes=index),
                    finished_at=now + timedelta(minutes=index, seconds=30),
                    stores_ok=index,
                    stores_failed=0,
                    offers_found=10,
                    matches_found=2,
                    alerts_sent=1,
                )
            )
        last = storage.last_run()
        assert last is not None and last.stores_ok == 2
