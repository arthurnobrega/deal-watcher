"""Core domain model.

Everything that crosses a module boundary in deal-watcher is one of these types.
Store adapters produce :class:`ProductOffer`; the monitor turns offers into
:class:`Alert`; notifiers consume :class:`Alert`. No store-specific or
notifier-specific data leaks into these structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class AlertLevel(StrEnum):
    """How good a deal is, worst to best.

    Ordering matters: :meth:`rank` is used to compare levels, and the monitor
    re-notifies when an offer improves to a strictly better level.
    """

    GOOD = "good"
    EXCELLENT = "excellent"
    BUY_NOW = "buy_now"

    @property
    def rank(self) -> int:
        return _LEVEL_RANK[self]

    @property
    def emoji(self) -> str:
        return _LEVEL_EMOJI[self]

    @property
    def label(self) -> str:
        """Human-readable badge text, e.g. "BUY NOW"."""
        return _LEVEL_LABEL[self]


_LEVEL_RANK: dict[AlertLevel, int] = {
    AlertLevel.GOOD: 1,
    AlertLevel.EXCELLENT: 2,
    AlertLevel.BUY_NOW: 3,
}

_LEVEL_EMOJI: dict[AlertLevel, str] = {
    AlertLevel.GOOD: "🟢",
    AlertLevel.EXCELLENT: "🔥",
    AlertLevel.BUY_NOW: "🚨",
}

_LEVEL_LABEL: dict[AlertLevel, str] = {
    AlertLevel.GOOD: "GOOD DEAL",
    AlertLevel.EXCELLENT: "EXCELLENT",
    AlertLevel.BUY_NOW: "BUY NOW",
}


@dataclass(frozen=True, slots=True)
class ProductOffer:
    """A single offer for a single product at a single store.

    ``price`` is the listed price as shown by the store, in ``currency``.
    Discount fields are all optional and default to "none known": deal-watcher
    never invents a coupon, so an offer with no verified coupon simply has
    ``coupon_code is None`` and ``effective_price == price``.
    """

    store: str
    name: str
    price: Decimal
    url: str
    available: bool
    currency: str = "BRL"

    # --- Verified discounts only. See docs/ and README "Coupons and cashback". ---
    coupon_code: str | None = None
    coupon_discount: Decimal = Decimal("0")
    cashback: Decimal = Decimal("0")

    # Free-form, store-specific breadcrumbs useful for debugging a bad match.
    raw_id: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def effective_price(self) -> Decimal:
        """Price after verified coupon and cashback, floored at zero.

        With no verified discounts this equals :attr:`price`, which is the only
        case the first release produces.
        """
        net = self.price - self.coupon_discount - self.cashback
        return net if net > 0 else Decimal("0")

    @property
    def identity(self) -> str:
        """Stable key for deduplication and price history.

        Uses the store plus the store's own product id when available, falling
        back to the URL. Deliberately excludes the product *name*, so a store
        renaming a listing does not reset its alert history.
        """
        return f"{self.store}:{self.raw_id or self.url}"


@dataclass(frozen=True, slots=True)
class Alert:
    """A deal worth telling the user about."""

    offer: ProductOffer
    product_name: str
    level: AlertLevel
    max_price: Decimal

    @property
    def savings(self) -> Decimal:
        """How far under the configured target the effective price landed."""
        return self.max_price - self.offer.effective_price


@dataclass(frozen=True, slots=True)
class StoreResult:
    """Outcome of querying one store for one product.

    A store that fails produces a result with ``error`` set and no offers, so a
    broken store is data rather than an exception escaping into the monitor.
    """

    store: str
    offers: tuple[ProductOffer, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
