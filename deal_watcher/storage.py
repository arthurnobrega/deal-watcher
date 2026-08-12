"""SQLite persistence: price history, notification state, and run health.

Prices are stored as TEXT and read back as :class:`~decimal.Decimal`. SQLite's
REAL type is binary floating point, which would quietly turn 3199.99 into
3199.9899999999998 and make history comparisons lie.

Timestamps are ISO-8601 UTC strings, which sort correctly as text.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType

from .models import Alert, AlertLevel, ProductOffer

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key     TEXT NOT NULL,
    product       TEXT NOT NULL,
    store         TEXT NOT NULL,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    price         TEXT NOT NULL,
    effective_price TEXT NOT NULL,
    currency      TEXT NOT NULL,
    available     INTEGER NOT NULL,
    seen_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_offer ON price_history (offer_key, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_product ON price_history (product, seen_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    offer_key   TEXT PRIMARY KEY,
    product     TEXT NOT NULL,
    store       TEXT NOT NULL,
    level       TEXT NOT NULL,
    price       TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    recovered   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    stores_ok     INTEGER NOT NULL,
    stores_failed INTEGER NOT NULL,
    offers_found  INTEGER NOT NULL,
    matches_found INTEGER NOT NULL,
    alerts_sent   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_finished ON runs (finished_at DESC);
"""


@dataclass(frozen=True, slots=True)
class NotificationState:
    """What was last notified for one offer."""

    offer_key: str
    level: AlertLevel
    price: Decimal
    notified_at: datetime
    recovered: bool


@dataclass(frozen=True, slots=True)
class RunStats:
    """Summary of one monitoring cycle, persisted for `deal-watcher health`."""

    started_at: datetime
    finished_at: datetime
    stores_ok: int
    stores_failed: int
    offers_found: int
    matches_found: int
    alerts_sent: int


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    product: str
    store: str
    name: str
    url: str
    price: Decimal
    effective_price: Decimal
    available: bool
    seen_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Storage:
    """Thin, synchronous wrapper over a SQLite file.

    Use as a context manager; the connection is closed on exit.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def __enter__(self) -> Storage:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # --- price history -------------------------------------------------

    def record_offer(self, product: str, offer: ProductOffer) -> None:
        """Append one observation of one offer."""
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO price_history (
                    offer_key, product, store, name, url,
                    price, effective_price, currency, available, seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    offer.identity,
                    product,
                    offer.store,
                    offer.name,
                    offer.url,
                    str(offer.price),
                    str(offer.effective_price),
                    offer.currency,
                    int(offer.available),
                    _iso(offer.fetched_at),
                ),
            )

    def history(self, product: str | None = None, limit: int = 20) -> tuple[HistoryEntry, ...]:
        """Most recent observations, newest first."""
        sql = "SELECT * FROM price_history"
        params: tuple[object, ...] = ()
        if product:
            sql += " WHERE product = ?"
            params = (product,)
        sql += " ORDER BY seen_at DESC, id DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return tuple(
            HistoryEntry(
                product=row["product"],
                store=row["store"],
                name=row["name"],
                url=row["url"],
                price=Decimal(row["price"]),
                effective_price=Decimal(row["effective_price"]),
                available=bool(row["available"]),
                seen_at=_parse_iso(row["seen_at"]),
            )
            for row in rows
        )

    def latest_offers(self, product: str) -> tuple[HistoryEntry, ...]:
        """The most recent observation of each distinct offer for a product.

        Price history is append-only, so "what does this cost now" means the
        newest row per offer, not the newest row overall.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY offer_key ORDER BY seen_at DESC, id DESC
                ) AS rank
                FROM price_history WHERE product = ?
            ) WHERE rank = 1
            ORDER BY CAST(effective_price AS REAL) ASC
            """,
            (product,),
        ).fetchall()
        return tuple(
            HistoryEntry(
                product=row["product"],
                store=row["store"],
                name=row["name"],
                url=row["url"],
                price=Decimal(row["price"]),
                effective_price=Decimal(row["effective_price"]),
                available=bool(row["available"]),
                seen_at=_parse_iso(row["seen_at"]),
            )
            for row in rows
        )

    def best_offer(self, product: str) -> HistoryEntry | None:
        """Cheapest in-stock offer from the latest observation of each offer."""
        in_stock = [entry for entry in self.latest_offers(product) if entry.available]
        return min(in_stock, key=lambda entry: entry.effective_price, default=None)

    def lowest_price(self, product: str) -> Decimal | None:
        """Cheapest price ever recorded for a product, across stores."""
        rows = self._conn.execute(
            "SELECT effective_price FROM price_history WHERE product = ? AND available = 1",
            (product,),
        ).fetchall()
        prices = [Decimal(row["effective_price"]) for row in rows]
        return min(prices) if prices else None

    # --- notification state --------------------------------------------

    def notification_state(self, offer_key: str) -> NotificationState | None:
        row = self._conn.execute(
            "SELECT * FROM notifications WHERE offer_key = ?", (offer_key,)
        ).fetchone()
        if row is None:
            return None
        return NotificationState(
            offer_key=row["offer_key"],
            level=AlertLevel(row["level"]),
            price=Decimal(row["price"]),
            notified_at=_parse_iso(row["notified_at"]),
            recovered=bool(row["recovered"]),
        )

    def record_notification(self, alert: Alert, sent_at: datetime | None = None) -> None:
        """Remember that we alerted, clearing any pending `recovered` flag."""
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO notifications (offer_key, product, store, level, price, notified_at,
                                           recovered)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(offer_key) DO UPDATE SET
                    level = excluded.level,
                    price = excluded.price,
                    notified_at = excluded.notified_at,
                    recovered = 0
                """,
                (
                    alert.offer.identity,
                    alert.product_name,
                    alert.offer.store,
                    alert.level.value,
                    str(alert.offer.effective_price),
                    _iso(sent_at or _now()),
                ),
            )

    def mark_recovered(self, offer_key: str) -> None:
        """Flag that this offer is no longer a deal, arming the next alert."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE notifications SET recovered = 1 WHERE offer_key = ? AND recovered = 0",
                (offer_key,),
            )

    # --- run health -----------------------------------------------------

    def record_run(self, stats: RunStats) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO runs (started_at, finished_at, stores_ok, stores_failed,
                                  offers_found, matches_found, alerts_sent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(stats.started_at),
                    _iso(stats.finished_at),
                    stats.stores_ok,
                    stats.stores_failed,
                    stats.offers_found,
                    stats.matches_found,
                    stats.alerts_sent,
                ),
            )

    def last_run(self) -> RunStats | None:
        row = self._conn.execute(
            "SELECT * FROM runs ORDER BY finished_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return RunStats(
            started_at=_parse_iso(row["started_at"]),
            finished_at=_parse_iso(row["finished_at"]),
            stores_ok=row["stores_ok"],
            stores_failed=row["stores_failed"],
            offers_found=row["offers_found"],
            matches_found=row["matches_found"],
            alerts_sent=row["alerts_sent"],
        )

    def prune(self, keep_days: int) -> int:
        """Drop price history older than ``keep_days``. Returns rows deleted."""
        cutoff = _now().timestamp() - keep_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, UTC).isoformat()
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM price_history WHERE seen_at < ?", (cutoff_iso,))
        return cursor.rowcount
