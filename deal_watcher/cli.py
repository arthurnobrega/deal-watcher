"""Command line interface.

    deal-watcher check              run one monitoring cycle
    deal-watcher run                loop forever, one cycle per interval
    deal-watcher test-notification  prove the Telegram wiring works
    deal-watcher report             cheapest price per product, as a table
    deal-watcher history            recent prices from the local database
    deal-watcher health             is the service alive and doing its job?
    deal-watcher stores             which adapters exist and what they need

`health` exits non-zero when the last cycle is too old, so systemd, a cron
watchdog, or a person over SSH all get the same answer without exposing a port.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .logging_setup import setup_logging
from .monitor import Monitor
from .notifiers.base import Notifier, NotifierError
from .notifiers.telegram import TelegramNotifier
from .prices import format_brl
from .storage import Storage
from .stores import available_stores, get_adapter

log = logging.getLogger("deal_watcher")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNHEALTHY = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deal-watcher",
        description="Monitor hardware prices in Brazilian stores.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument("--log-level", default=None, help="override the configured log level")
    parser.add_argument("--version", action="version", version=f"deal-watcher {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="run a single monitoring cycle")
    check.add_argument(
        "--dry-run",
        action="store_true",
        help="find deals and report them, but send nothing and record no notification",
    )

    sub.add_parser("run", help="run cycles forever, one per configured interval")
    sub.add_parser("test-notification", help="send a test message through every notifier")

    history = sub.add_parser("history", help="show recent observed prices")
    history.add_argument("--product", default=None, help="filter by product name")
    history.add_argument("--limit", type=int, default=20)

    report = sub.add_parser("report", help="cheapest current price per product, as a table")
    report.add_argument(
        "--send",
        action="store_true",
        help="also send the table through every configured notifier",
    )
    report.add_argument(
        "--all-stores",
        action="store_true",
        help="show every store's price, not just the cheapest",
    )

    sub.add_parser("health", help="report whether the monitor is running and recent")
    sub.add_parser("stores", help="list available store adapters")
    return parser


def _load(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    setup_logging(args.log_level or config.log_level, config.log_format)
    return config


def _build_notifiers(config: Config, required: bool) -> list[Notifier]:
    """Instantiate configured notifiers.

    A misconfigured notifier is fatal for `test-notification` (the user asked to
    test it) but only a warning for `check` (a store price is still worth
    recording even if Telegram is broken).
    """
    notifiers: list[Notifier] = []
    telegram = config.notifiers.telegram
    if telegram.enabled:
        try:
            notifiers.append(TelegramNotifier(telegram, config.http))
        except NotifierError as exc:
            if required:
                raise
            log.warning("telegram notifier disabled: %s", exc)
    return notifiers


def cmd_check(args: argparse.Namespace) -> int:
    config = _load(args)
    with Storage(config.database) as storage:
        monitor = Monitor(config, storage, _build_notifiers(config, required=False))
        try:
            report = monitor.run_cycle(dry_run=args.dry_run)
        finally:
            monitor.close()
    return EXIT_OK if report.stores_ok else EXIT_ERROR


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    log.info("Starting deal-watcher %s, every %ds", __version__, config.interval_seconds)
    with Storage(config.database) as storage:
        monitor = Monitor(config, storage, _build_notifiers(config, required=False))
        try:
            while True:
                started = time.monotonic()
                try:
                    monitor.run_cycle()
                except Exception:
                    log.exception("cycle failed; continuing")
                elapsed = time.monotonic() - started
                time.sleep(max(1.0, config.interval_seconds - elapsed))
        except KeyboardInterrupt:
            log.info("stopping on interrupt")
        finally:
            monitor.close()
    return EXIT_OK


def cmd_test_notification(args: argparse.Namespace) -> int:
    config = _load(args)
    try:
        notifiers = _build_notifiers(config, required=True)
    except NotifierError as exc:
        log.error("cannot build notifier: %s", exc)
        return EXIT_ERROR
    if not notifiers:
        log.error("no notifiers are enabled in config")
        return EXIT_ERROR

    text = (
        "✅ deal-watcher test notification\n\n"
        f"Sent at {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC.\n"
        "If you can read this, notifications are configured correctly."
    )
    failures = 0
    for notifier in notifiers:
        try:
            notifier.send_text(text)
            log.info("test message sent via %s", notifier.name)
        except NotifierError as exc:
            failures += 1
            log.error("%s failed: %s", notifier.name, exc)
        finally:
            notifier.close()
    return EXIT_ERROR if failures else EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    config = _load(args)
    with Storage(config.database) as storage:
        entries = storage.history(product=args.product, limit=args.limit)
        if not entries:
            print("no price history recorded yet")
            return EXIT_OK
        for entry in entries:
            stock = "in stock" if entry.available else "out of stock"
            print(
                f"{entry.seen_at:%Y-%m-%d %H:%M}  {entry.store:<14} "
                f"{format_brl(entry.effective_price):>14}  {stock:<12} {entry.name[:60]}"
            )
        for product in {entry.product for entry in entries}:
            lowest = storage.lowest_price(product)
            if lowest is not None:
                print(f"\nlowest ever for {product}: {format_brl(lowest)}")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """Cheapest in-stock price per product, with the build total.

    Reads recorded history rather than hitting the stores, so it is instant and
    works offline. Run `deal-watcher check` first if you want it fresher.
    """
    config = _load(args)

    # A phone renders a narrower table than a terminal does, so the sent
    # version drops the store column and truncates long names.
    width = 24 if args.send else 34
    header = f"{'PRODUCT':<{width}}  {'TARGET':>12}  {'BEST NOW':>12}  {'VS TARGET':>12}"
    if not args.send:
        header += "  STORE"
    lines = [header, "-" * len(header)]

    total_target = Decimal("0")
    total_best = Decimal("0")
    missing = 0

    with Storage(config.database) as storage:
        for product in config.products:
            best = storage.best_offer(product.name)
            label = product.name
            if len(label) > width:
                label = label[: width - 1] + "…"

            if best is None:
                missing += 1
                line = f"{label:<{width}}  {format_brl(product.max_price):>12}  {'-':>12}  {'':>12}"
                if not args.send:
                    line += "  no data"
            else:
                difference = best.effective_price - product.max_price
                delta = ("+" if difference > 0 else "") + format_brl(difference)
                total_target += product.max_price
                total_best += best.effective_price
                line = (
                    f"{label:<{width}}  {format_brl(product.max_price):>12}  "
                    f"{format_brl(best.effective_price):>12}  {delta:>12}"
                )
                if not args.send:
                    hours = (datetime.now(UTC) - best.seen_at).total_seconds() / 3600
                    age = f"{hours:.0f}h ago" if hours >= 1 else "just now"
                    line += f"  {best.store} ({age})"
            lines.append(line)

            if args.all_stores and best is not None:
                for entry in storage.latest_offers(product.name):
                    if not entry.available:
                        continue
                    name = f"  {entry.name}"[:width]
                    lines.append(
                        f"{name:<{width}}  {'':>12}  "
                        f"{format_brl(entry.effective_price):>12}  {'':>12}"
                        + ("" if args.send else f"  {entry.store}")
                    )

    lines.append("-" * len(header))
    difference = total_best - total_target
    delta_total = ("+" if difference > 0 else "") + format_brl(difference)
    lines.append(
        f"{'TOTAL':<{width}}  {format_brl(total_target):>12}  "
        f"{format_brl(total_best):>12}  {delta_total:>12}"
    )
    if missing:
        lines += [
            "",
            f"{missing} of {len(config.products)} products have no in-stock price yet;",
            "both totals exclude them so the comparison stays honest.",
        ]

    table = "\n".join(lines)
    print(table)

    if args.send:
        heading = f"📊 deal-watcher — {datetime.now().strftime('%d/%m/%Y')}"
        failures = 0
        for notifier in _build_notifiers(config, required=True):
            try:
                notifier.send_text(f"{heading}\n\n{table}", monospace=True)
                log.info("daily report sent via %s", notifier.name)
            except NotifierError as exc:
                failures += 1
                log.error("%s failed: %s", notifier.name, exc)
            finally:
                notifier.close()
        if failures:
            return EXIT_ERROR
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    config = _load(args)
    with Storage(config.database) as storage:
        last = storage.last_run()
    if last is None:
        print("UNHEALTHY: no monitoring cycle has completed yet")
        return EXIT_UNHEALTHY

    age = (datetime.now(UTC) - last.finished_at).total_seconds()
    status = "OK" if age <= config.health_max_age_seconds else "UNHEALTHY"
    print(
        f"{status}: last cycle {int(age)}s ago "
        f"({last.stores_ok} stores ok, {last.stores_failed} failed, "
        f"{last.offers_found} offers, {last.matches_found} matches, "
        f"{last.alerts_sent} alerts)"
    )
    return EXIT_OK if status == "OK" else EXIT_UNHEALTHY


def cmd_stores(args: argparse.Namespace) -> int:
    for slug in available_stores():
        adapter = get_adapter(slug)
        print(f"{slug:<10} {adapter.display_name}")
        if adapter.notes:
            print(f"{'':<10} {adapter.notes}")
    return EXIT_OK


_COMMANDS = {
    "check": cmd_check,
    "run": cmd_run,
    "test-notification": cmd_test_notification,
    "history": cmd_history,
    "report": cmd_report,
    "health": cmd_health,
    "stores": cmd_stores,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except ValueError as exc:
        # Configuration problems: a stack trace helps nobody here.
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
