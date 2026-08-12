"""The monitoring cycle: fetch, match, decide, notify, record.

This is the only module that knows the whole story, and it knows it in one
readable pass. Everything it calls is independently testable: adapters produce
offers, :mod:`deal_watcher.matching` accepts or rejects them,
:mod:`deal_watcher.alerts` scores and deduplicates, notifiers deliver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .alerts import build_alert, should_notify
from .config import Config, ProductConfig
from .fetchers import FetcherFactory
from .matching import filter_offers
from .models import Alert, ProductOffer, StoreResult
from .notifiers.base import Notifier, NotifierError
from .prices import format_brl
from .storage import RunStats, Storage
from .stores import get_adapter

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """What one monitoring cycle did. Returned so the CLI can print it."""

    started_at: datetime
    finished_at: datetime | None = None
    store_results: list[StoreResult] = field(default_factory=list)
    matches: list[ProductOffer] = field(default_factory=list)
    alerts_sent: list[Alert] = field(default_factory=list)
    best_price: ProductOffer | None = None

    @property
    def stores_ok(self) -> int:
        return sum(1 for result in self.store_results if result.ok)

    @property
    def stores_failed(self) -> int:
        return sum(1 for result in self.store_results if not result.ok)

    @property
    def offers_found(self) -> int:
        return sum(len(result.offers) for result in self.store_results)

    def to_stats(self) -> RunStats:
        return RunStats(
            started_at=self.started_at,
            finished_at=self.finished_at or datetime.now(UTC),
            stores_ok=self.stores_ok,
            stores_failed=self.stores_failed,
            offers_found=self.offers_found,
            matches_found=len(self.matches),
            alerts_sent=len(self.alerts_sent),
        )


class Monitor:
    """Runs monitoring cycles against the configured stores and products."""

    def __init__(
        self,
        config: Config,
        storage: Storage,
        notifiers: list[Notifier],
        fetchers: FetcherFactory | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.notifiers = notifiers
        self.fetchers = fetchers or FetcherFactory(config.http, config.browser)

    def run_cycle(self, dry_run: bool = False) -> CycleReport:
        """One full pass over every product and store."""
        report = CycleReport(started_at=datetime.now(UTC))
        log.info("Starting price check")

        for product in self.config.products:
            self._check_product(product, report, dry_run=dry_run)

        report.finished_at = datetime.now(UTC)
        self.storage.record_run(report.to_stats())

        if report.best_price is not None:
            log.info(
                "Best price: %s (%s)",
                format_brl(report.best_price.effective_price),
                report.best_price.store,
            )
        log.info(
            "Cycle done in %.1fs: %d stores ok, %d failed, %d offers, %d matches, %d alerts",
            (report.finished_at - report.started_at).total_seconds(),
            report.stores_ok,
            report.stores_failed,
            report.offers_found,
            len(report.matches),
            len(report.alerts_sent),
        )
        return report

    def _check_product(self, product: ProductConfig, report: CycleReport, dry_run: bool) -> None:
        for slug in self.config.enabled_stores():
            if not product.targets_store(slug):
                continue
            result = self._query_store(slug, product)
            report.store_results.append(result)

            if not result.ok:
                log.warning("%s: ❌ %s", result.store, result.error)
                continue

            matched, rejected = filter_offers(result.offers, product)
            log.info(
                "%s: %d offers found, %d match %r",
                result.store,
                len(result.offers),
                len(matched),
                product.name,
            )
            for offer, reason in rejected:
                log.debug("%s: rejected %r -- %s", result.store, offer.name, reason.reason)

            for offer in matched:
                self.storage.record_offer(product.name, offer)
                self._track_best(offer, report)
                report.matches.append(offer)
                self._maybe_alert(offer, product, report, dry_run=dry_run)

    def _query_store(self, slug: str, product: ProductConfig) -> StoreResult:
        store_config = self.config.store_config(slug)
        try:
            adapter = get_adapter(slug)
        except KeyError as exc:
            return StoreResult(store=slug, error=str(exc))
        return adapter.search(
            query=product.query_for(slug),
            fetchers=self.fetchers,
            kind=store_config.fetcher,
            max_results=store_config.max_results,
        )

    @staticmethod
    def _track_best(offer: ProductOffer, report: CycleReport) -> None:
        if not offer.available:
            return
        if report.best_price is None or offer.effective_price < report.best_price.effective_price:
            report.best_price = offer

    def _maybe_alert(
        self,
        offer: ProductOffer,
        product: ProductConfig,
        report: CycleReport,
        dry_run: bool,
    ) -> None:
        alert = build_alert(offer, product)
        if alert is None:
            # Above target: arm the next alert if we had notified before.
            self.storage.mark_recovered(offer.identity)
            return

        previous = self.storage.notification_state(offer.identity)
        send, reason = should_notify(alert, previous)
        if not send:
            log.info(
                "%s: %s at %s -- not notifying (%s)",
                offer.store,
                product.name,
                format_brl(offer.effective_price),
                reason,
            )
            return

        if dry_run:
            log.info(
                "%s: would alert %s at %s (%s)",
                offer.store,
                product.name,
                format_brl(offer.effective_price),
                reason,
            )
            report.alerts_sent.append(alert)
            return

        if self._deliver(alert):
            self.storage.record_notification(alert)
            report.alerts_sent.append(alert)
            log.info(
                "Alert sent: %s at %s from %s (%s)",
                product.name,
                format_brl(offer.effective_price),
                offer.store,
                reason,
            )

    def _deliver(self, alert: Alert) -> bool:
        """Send to every notifier. Returns True if at least one succeeded.

        A dead notifier must not cost us the alert on a working one, and must not
        mark the alert as delivered when nothing got through.
        """
        delivered = False
        for notifier in self.notifiers:
            try:
                notifier.send(alert)
                delivered = True
            except NotifierError as exc:
                log.error("notifier %s failed: %s", notifier.name, exc)
            except Exception as exc:
                log.error("notifier %s failed unexpectedly: %s", notifier.name, exc)
        if not delivered:
            log.error("alert not delivered by any notifier; will retry next cycle")
        return delivered

    def close(self) -> None:
        self.fetchers.close()
        for notifier in self.notifiers:
            notifier.close()
