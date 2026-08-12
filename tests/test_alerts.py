"""Alert levels and the anti-spam rules."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deal_watcher.alerts import alert_level_for, build_alert, should_notify
from deal_watcher.config import ProductConfig
from deal_watcher.models import AlertLevel
from deal_watcher.storage import NotificationState

from .conftest import make_alert, make_offer


class TestAlertLevels:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("3400", None),
            ("3301", None),
            ("3300", AlertLevel.GOOD),
            ("3250", AlertLevel.GOOD),
            ("3200", AlertLevel.EXCELLENT),
            ("3100", AlertLevel.EXCELLENT),
            ("3000", AlertLevel.BUY_NOW),
            ("2500", AlertLevel.BUY_NOW),
        ],
    )
    def test_thresholds_are_inclusive_and_best_wins(
        self, price: str, expected: AlertLevel | None, rtx_product: ProductConfig
    ) -> None:
        assert alert_level_for(Decimal(price), rtx_product) == expected

    def test_thresholds_come_from_config_not_code(self, rtx_product: ProductConfig) -> None:
        cheaper = rtx_product.model_copy(
            update={
                "max_price": Decimal("2800"),
                "alert_levels": rtx_product.alert_levels.model_copy(
                    update={"good": Decimal("2800"), "excellent": None, "buy_now": None}
                ),
            }
        )
        assert alert_level_for(Decimal("3300"), cheaper) is None
        assert alert_level_for(Decimal("2800"), cheaper) is AlertLevel.GOOD

    def test_good_defaults_to_max_price(self) -> None:
        product = ProductConfig.model_validate({"name": "X", "max_price": 1000})
        assert product.alert_levels.good == Decimal("1000")
        assert alert_level_for(Decimal("1000"), product) is AlertLevel.GOOD
        assert alert_level_for(Decimal("1001"), product) is None


class TestBuildAlert:
    def test_no_alert_above_target(self, rtx_product: ProductConfig) -> None:
        assert build_alert(make_offer(price="3400"), rtx_product) is None

    def test_savings_is_distance_below_target(self, rtx_product: ProductConfig) -> None:
        alert = build_alert(make_offer(price="3199.99"), rtx_product)
        assert alert is not None
        assert alert.savings == Decimal("100.01")
        assert alert.level is AlertLevel.EXCELLENT

    def test_uses_effective_price_when_a_coupon_is_verified(
        self, rtx_product: ProductConfig
    ) -> None:
        offer = make_offer(price="3400").__class__(
            store="KaBuM!",
            name="RTX 5060 Ti 16GB",
            price=Decimal("3400"),
            url="https://example.test/1",
            available=True,
            coupon_code="VERIFIED10",
            coupon_discount=Decimal("200"),
        )
        alert = build_alert(offer, rtx_product)
        assert alert is not None
        assert offer.effective_price == Decimal("3200")
        assert alert.level is AlertLevel.EXCELLENT


def _state(price: str, level: AlertLevel, recovered: bool = False) -> NotificationState:
    return NotificationState(
        offer_key="KaBuM!:1",
        level=level,
        price=Decimal(price),
        notified_at=datetime.now(UTC),
        recovered=recovered,
    )


class TestDeduplication:
    def test_first_sighting_notifies(self) -> None:
        send, reason = should_notify(make_alert(), None)
        assert send
        assert "first" in reason

    def test_same_price_next_cycle_is_silent(self) -> None:
        alert = make_alert(price="3199.99")
        previous = _state("3199.99", AlertLevel.EXCELLENT)
        send, _ = should_notify(alert, previous)
        assert not send

    def test_stable_price_stays_silent_for_many_cycles(self) -> None:
        # The stated requirement: R$ 3.199 for hours means exactly one message.
        alert = make_alert(price="3199.00")
        previous = _state("3199.00", AlertLevel.EXCELLENT)
        assert all(not should_notify(alert, previous)[0] for _ in range(20))

    def test_tiny_drop_is_not_worth_a_message(self) -> None:
        alert = make_alert(price="3198.99")
        previous = _state("3199.99", AlertLevel.EXCELLENT)
        send, reason = should_notify(alert, previous)
        assert not send
        assert "too small" in reason

    def test_meaningful_drop_within_a_level_notifies(self) -> None:
        alert = make_alert(price="3050.00")
        previous = _state("3199.99", AlertLevel.EXCELLENT)
        send, _ = should_notify(alert, previous)
        assert send

    def test_upgrade_to_a_better_level_notifies(self) -> None:
        alert = make_alert(price="2999.00", level=AlertLevel.BUY_NOW)
        previous = _state("3199.99", AlertLevel.EXCELLENT)
        send, reason = should_notify(alert, previous)
        assert send
        assert "excellent" in reason and "buy_now" in reason

    def test_price_rising_then_falling_again_notifies(self) -> None:
        # Went above target (monitor flagged `recovered`), now back below it.
        alert = make_alert(price="3199.99")
        previous = _state("3199.99", AlertLevel.EXCELLENT, recovered=True)
        send, reason = should_notify(alert, previous)
        assert send
        assert "back above target" in reason

    def test_price_going_up_within_the_target_is_silent(self) -> None:
        alert = make_alert(price="3280.00", level=AlertLevel.GOOD)
        previous = _state("3199.99", AlertLevel.EXCELLENT)
        send, _ = should_notify(alert, previous)
        assert not send
