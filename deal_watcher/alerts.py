"""Turning matched offers into alert decisions.

Two questions live here, and nowhere else:

1. *How good is this price?* -> :func:`alert_level_for`
2. *Should we say it again?* -> :func:`should_notify`
"""

from __future__ import annotations

from decimal import Decimal

from .config import ProductConfig
from .models import Alert, AlertLevel, ProductOffer
from .storage import NotificationState

#: Checked best-first so the strongest level that applies wins.
_LEVEL_ORDER: tuple[AlertLevel, ...] = (
    AlertLevel.BUY_NOW,
    AlertLevel.EXCELLENT,
    AlertLevel.GOOD,
)


def alert_level_for(price: Decimal, product: ProductConfig) -> AlertLevel | None:
    """The best alert level this price reaches, or ``None`` if it reaches none."""
    thresholds = product.alert_levels
    ceilings = {
        AlertLevel.BUY_NOW: thresholds.buy_now,
        AlertLevel.EXCELLENT: thresholds.excellent,
        AlertLevel.GOOD: thresholds.good,
    }
    for level in _LEVEL_ORDER:
        ceiling = ceilings[level]
        if ceiling is not None and price <= ceiling:
            return level
    return None


def build_alert(offer: ProductOffer, product: ProductConfig) -> Alert | None:
    """Build an :class:`Alert` if the offer's effective price is low enough."""
    level = alert_level_for(offer.effective_price, product)
    if level is None:
        return None
    return Alert(
        offer=offer,
        product_name=product.name,
        level=level,
        max_price=product.max_price,
    )


def should_notify(alert: Alert, previous: NotificationState | None) -> tuple[bool, str]:
    """Decide whether to send ``alert``, given what was last sent for this offer.

    Returns the decision and a short reason, which the monitor logs so a missing
    notification is always explainable.

    An alert is sent when:

    * nothing was ever sent for this offer, or
    * the price recovered above the target since the last alert and has now
      dropped back below it, or
    * the price improved to a strictly better alert level, or
    * the price dropped meaningfully further within the same level.

    A price sitting still under the target produces exactly one notification.
    """
    if previous is None:
        return True, "first time below target"

    if previous.recovered:
        return True, "price went back above target and dropped again"

    if alert.level.rank > previous.level.rank:
        return True, f"improved from {previous.level.value} to {alert.level.value}"

    price = alert.offer.effective_price
    if price < previous.price:
        drop = previous.price - price
        # Re-alerting on every R$ 0,01 wiggle is spam; 1% is a real move.
        if drop >= previous.price * Decimal("0.01"):
            return True, f"price dropped a further {drop}"
        return False, "price drop too small to re-notify"

    return False, "already notified at this level"
