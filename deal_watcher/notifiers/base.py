"""Notifier contract and shared message rendering.

Adding Discord or email means adding a class here that implements
:meth:`Notifier.send`. Nothing in the monitor changes: it holds a list of
notifiers and hands each one an :class:`~deal_watcher.models.Alert`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..models import Alert
from ..prices import format_brl

log = logging.getLogger(__name__)


class NotifierError(RuntimeError):
    """Delivery failed. Never carries credentials in its message."""


class Notifier(ABC):
    """Something that can deliver an alert to the user."""

    #: Identifier used in config and logs.
    name: str

    @abstractmethod
    def send(self, alert: Alert) -> None:
        """Deliver one alert. Raises :class:`NotifierError` on failure."""

    @abstractmethod
    def send_text(self, text: str, monospace: bool = False) -> None:
        """Deliver a plain message.

        ``monospace`` asks the channel to preserve alignment, which a table
        needs and a sentence does not. Channels that cannot honour it should
        send the text unchanged rather than fail.
        """

    def close(self) -> None:  # noqa: B027 - optional hook, not every channel holds resources
        """Release any transport resources."""


def render_alert(alert: Alert) -> str:
    """Render an alert as plain text.

    Kept out of the notifier classes so every channel shows the user the same
    numbers, and so the wording can be tested without a network.
    """
    offer = alert.offer
    lines = [
        f"{alert.level.emoji} {alert.product_name} {alert.level.label}!",
        "",
        offer.name,
        f"Store: {offer.store}",
        "",
        f"Price: {format_brl(offer.price)}",
    ]

    if offer.coupon_code:
        lines.append(f"Coupon: {offer.coupon_code} (-{format_brl(offer.coupon_discount)})")
    if offer.cashback:
        lines.append(f"Cashback: -{format_brl(offer.cashback)}")
    if offer.effective_price != offer.price:
        lines.append(f"Effective: {format_brl(offer.effective_price)}")

    lines.append(f"Target: {format_brl(alert.max_price)}")
    lines.append("")

    savings = alert.savings
    if savings > 0:
        lines.append(f"📉 {format_brl(savings)} below target")
    else:
        lines.append("📉 exactly at target")

    lines += ["", "🛒 Buy:", offer.url]
    return "\n".join(lines)
