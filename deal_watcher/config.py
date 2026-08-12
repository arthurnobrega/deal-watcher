"""Configuration loading and validation.

All tunables live in a YAML file (``config.yaml`` by default). Secrets never do:
they are read from the environment by the notifier that needs them.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

FetcherKind = Literal["http", "browser"]

DEFAULT_CONFIG_PATHS = (
    Path("config.yaml"),
    Path("/etc/deal-watcher/config.yaml"),
)


class StrictModel(BaseModel):
    """Reject unknown keys so a typo in config.yaml fails loudly at startup."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpConfig(StrictModel):
    timeout_seconds: float = 20.0
    retries: int = Field(default=2, ge=0, le=5)
    backoff_seconds: float = Field(default=2.0, ge=0)
    delay_between_requests: float = Field(default=1.5, ge=0)
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )


class AlertLevels(StrictModel):
    """Price ceilings per alert level, in the product's currency.

    ``good`` defaults to the product's ``max_price``; the stricter levels are
    optional. A level with no threshold is simply never reached.
    """

    good: Decimal | None = None
    excellent: Decimal | None = None
    buy_now: Decimal | None = None


class MatchRules(StrictModel):
    """Regex rules deciding whether a scraped offer *is* the wanted product.

    Patterns are matched against an accent-stripped, lowercased product name.
    See :mod:`deal_watcher.matching`.
    """

    require_all: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()
    reject_any: tuple[str, ...] = ()
    require_available: bool = True
    # VRAM guard: the strongest defence against alerting on an 8GB card.
    required_memory_gb: int | None = None
    forbidden_memory_gb: tuple[int, ...] = ()


class ProductConfig(StrictModel):
    name: str
    max_price: Decimal
    match: MatchRules = MatchRules()
    alert_levels: AlertLevels = AlertLevels()
    # Search term per store slug; a store missing here falls back to `name`.
    queries: dict[str, str] = Field(default_factory=dict)
    # Restrict this product to a subset of stores. Empty means "all enabled".
    stores: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _default_good_level(self) -> ProductConfig:
        if self.alert_levels.good is None:
            object.__setattr__(
                self, "alert_levels", self.alert_levels.model_copy(update={"good": self.max_price})
            )
        return self

    def query_for(self, store_slug: str) -> str:
        return self.queries.get(store_slug, self.name)

    def targets_store(self, store_slug: str) -> bool:
        return not self.stores or store_slug in self.stores


class StoreConfig(StrictModel):
    enabled: bool = True
    fetcher: FetcherKind = "http"
    #: Max offers parsed from one search page. Guards against a layout change
    #: turning one query into thousands of bogus candidates.
    max_results: int = Field(default=60, ge=1, le=500)
    #: Fewer offers than this means the page did not finish loading. Treated as
    #: a store failure, because a half-loaded listing that finds nothing is
    #: indistinguishable from a listing that genuinely has nothing.
    min_results: int = Field(default=0, ge=0)


class TelegramConfig(StrictModel):
    enabled: bool = True
    #: Env var *names* only -- never the values.
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    api_base: str = "https://api.telegram.org"


class NotifiersConfig(StrictModel):
    telegram: TelegramConfig = TelegramConfig()


class BrowserConfig(StrictModel):
    """Playwright settings, used only by stores with ``fetcher: browser``."""

    enabled: bool = False
    timeout_seconds: float = 45.0
    #: Extra settle time after load, for stores behind a JS interstitial.
    wait_after_load_seconds: float = 6.0
    #: Listings that load more cards as you scroll need scrolling. Without it a
    #: slow box silently returns a fraction of the catalogue, which reads as
    #: "no deals" rather than "did not finish loading".
    scroll_passes: int = Field(default=25, ge=0, le=100)
    scroll_pause_seconds: float = Field(default=1.0, ge=0)
    #: How many consecutive unchanged measurements mean "finished loading".
    #: One is not enough: the page also sits still between batches.
    scroll_stable_passes: int = Field(default=3, ge=1, le=10)


class Config(StrictModel):
    products: tuple[ProductConfig, ...]
    stores: dict[str, StoreConfig] = Field(default_factory=dict)
    notifiers: NotifiersConfig = NotifiersConfig()
    http: HttpConfig = HttpConfig()
    browser: BrowserConfig = BrowserConfig()
    database: Path = Path("data/deal-watcher.db")
    #: Monitor loop interval, used by `deal-watcher run`.
    interval_seconds: int = Field(default=900, ge=60)
    #: `deal-watcher health` fails if the last successful cycle is older than this.
    health_max_age_seconds: int = Field(default=3600, ge=60)
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @model_validator(mode="after")
    def _validate_products(self) -> Config:
        if not self.products:
            raise ValueError("config must define at least one product")
        unknown = {
            slug
            for product in self.products
            for slug in (*product.stores, *product.queries)
            if slug not in self.stores
        }
        if unknown:
            raise ValueError(f"products reference undeclared stores: {sorted(unknown)}")
        return self

    def store_config(self, slug: str) -> StoreConfig:
        return self.stores.get(slug, StoreConfig())

    def enabled_stores(self) -> tuple[str, ...]:
        return tuple(slug for slug, cfg in self.stores.items() if cfg.enabled)


def find_config_path(explicit: Path | None = None) -> Path:
    """Resolve the config path from CLI arg, ``DEAL_WATCHER_CONFIG``, then defaults."""
    if explicit is not None:
        return explicit
    if env_path := os.environ.get("DEAL_WATCHER_CONFIG"):
        return Path(env_path)
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    return DEFAULT_CONFIG_PATHS[0]


def load_config(path: Path | None = None) -> Config:
    """Load and validate configuration, raising ``ValueError`` on bad input."""
    config_path = find_config_path(path)
    if not config_path.is_file():
        raise ValueError(f"config file not found: {config_path}")
    raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file must contain a YAML mapping: {config_path}")
    return Config.model_validate(raw)
