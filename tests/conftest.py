"""Shared test fixtures.

Nothing here touches the network. Store adapters are exercised against static
HTML captured from the real sites, so a store being down, slow, or angry at us
can never turn the test suite red.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from deal_watcher.config import Config, MatchRules, ProductConfig
from deal_watcher.fetchers import FetcherFactory, FetchError
from deal_watcher.models import Alert, AlertLevel, ProductOffer
from deal_watcher.notifiers.base import Notifier, NotifierError

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetcher:
    """Returns canned pages, or raises for URLs mapped to an exception."""

    def __init__(self, pages: dict[str, str | Exception]) -> None:
        self.pages = pages
        self.requested: list[str] = []
        self.hints: list[object | None] = []

    def fetch(self, url: str, hints: object | None = None) -> str:
        self.requested.append(url)
        self.hints.append(hints)
        for pattern, result in self.pages.items():
            if pattern in url:
                if isinstance(result, Exception):
                    raise result
                return result
        raise FetchError(f"no canned page for {url}")

    def close(self) -> None:
        pass


class FakeFetcherFactory(FetcherFactory):
    """A FetcherFactory that hands out one FakeFetcher for every transport."""

    def __init__(self, fetcher: FakeFetcher) -> None:
        self._fetcher = fetcher

    def get(self, kind: str) -> FakeFetcher:  # type: ignore[override]
        return self._fetcher

    def close(self) -> None:
        pass


class RecordingNotifier(Notifier):
    """Captures alerts instead of sending them. Can be told to fail."""

    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Alert] = []
        self.texts: list[str] = []
        self.fail = fail
        self.closed = False

    def send(self, alert: Alert) -> None:
        if self.fail:
            raise NotifierError("boom")
        self.sent.append(alert)

    def send_text(self, text: str) -> None:
        if self.fail:
            raise NotifierError("boom")
        self.texts.append(text)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def rtx_match_rules() -> MatchRules:
    """The real RTX 5060 Ti 16GB rules, mirroring shipped config.yaml."""
    return MatchRules(
        require_available=True,
        required_memory_gb=16,
        forbidden_memory_gb=(8,),
        require_all=(r"rtx\s*5060\s*ti",),
        reject_any=(
            r"\brtx\s*(5050|5070|5080|5090|4060|4070|5060)\b(?!\s*ti)",
            r"usado|semi ?novo|open ?box|recondicionado|refurbished|vitrine",
            r"pc gamer|computador|desktop|workstation|notebook|kit upgrade",
            r"suporte|cabo|adaptador|riser|water ?cooler|bracket|espelho",
        ),
    )


@pytest.fixture
def rtx_product(rtx_match_rules: MatchRules) -> ProductConfig:
    return ProductConfig.model_validate(
        {
            "name": "RTX 5060 Ti 16GB",
            "max_price": Decimal("3300"),
            "alert_levels": {"good": 3300, "excellent": 3200, "buy_now": 3000},
            "match": rtx_match_rules.model_dump(),
            "queries": {"kabum": "rtx 5060 ti"},
        }
    )


@pytest.fixture
def config(rtx_product: ProductConfig, tmp_path: Path) -> Config:
    return Config(
        products=(rtx_product,),
        stores={"kabum": {"enabled": True, "fetcher": "http"}},  # type: ignore[dict-item]
        database=tmp_path / "test.db",
    )


def make_offer(
    name: str = "Placa de Video ASUS RTX 5060 Ti 16GB GDDR7",
    price: str = "3199.99",
    store: str = "KaBuM!",
    available: bool = True,
    url: str = "https://example.test/produto/1",
    raw_id: str | None = "1",
) -> ProductOffer:
    return ProductOffer(
        store=store,
        name=name,
        price=Decimal(price),
        url=url,
        available=available,
        raw_id=raw_id,
    )


def make_alert(
    price: str = "3199.99",
    level: AlertLevel = AlertLevel.EXCELLENT,
    max_price: str = "3300",
) -> Alert:
    return Alert(
        offer=make_offer(price=price),
        product_name="RTX 5060 Ti 16GB",
        level=level,
        max_price=Decimal(max_price),
    )
