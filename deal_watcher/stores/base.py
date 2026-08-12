"""Store adapter contract and registry.

An adapter knows two things and nothing else:

* the URL that lists candidates for a search term
* how to turn that page into :class:`~deal_watcher.models.ProductOffer` objects

It does not know what product the user wants, what a good price is, or that
Telegram exists. Adding a store means adding one file here and one line of
config -- no changes to the monitor, the matcher, or the notifiers.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from urllib.parse import quote

from ..fetchers import FetcherFactory, FetchError
from ..models import ProductOffer, StoreResult

log = logging.getLogger(__name__)


class ParseError(RuntimeError):
    """A page was fetched but could not be understood, usually a layout change."""


class StoreAdapter(ABC):
    """Base class for every store."""

    #: Stable identifier used in config and logs, e.g. ``kabum``.
    slug: str
    #: Human-readable name used in notifications, e.g. ``KaBuM!``.
    display_name: str
    #: Documented for operators: what this store needs to be readable.
    notes: str = ""

    @abstractmethod
    def search_url(self, query: str) -> str:
        """URL of the search/listing page for ``query``."""

    @abstractmethod
    def parse(self, page: str) -> tuple[ProductOffer, ...]:
        """Turn a fetched page into offers. Raises :class:`ParseError` on garbage."""

    def search(
        self,
        query: str,
        fetchers: FetcherFactory,
        kind: str = "http",
        max_results: int = 60,
    ) -> StoreResult:
        """Fetch and parse, converting any failure into a failed :class:`StoreResult`.

        This is the error-isolation boundary: one broken store never raises into
        the monitor, it just reports itself as broken and the cycle continues.

        ``kind`` is the transport this store is configured to use. Adapters
        needing more than one request (a listing plus product pages, say) may
        override this method and pull extra fetchers from ``fetchers``.
        """
        try:
            offers = self.collect(query, fetchers, kind, max_results)
        except FetchError as exc:
            return StoreResult(store=self.display_name, error=str(exc))
        except ParseError as exc:
            return StoreResult(store=self.display_name, error=str(exc))
        except Exception as exc:
            log.debug("store %s failed", self.slug, exc_info=True)
            return StoreResult(
                store=self.display_name, error=f"unexpected {type(exc).__name__}: {exc}"
            )

        return StoreResult(store=self.display_name, offers=offers[:max_results])

    def collect(
        self,
        query: str,
        fetchers: FetcherFactory,
        kind: str,
        max_results: int,
    ) -> tuple[ProductOffer, ...]:
        """Default flow: fetch one listing page and parse it.

        Override for stores that need a different request pattern.
        """
        page = fetchers.get(kind).fetch(self.search_url(query))
        return self.parse(page)

    @staticmethod
    def encode(query: str) -> str:
        return quote(query, safe="")

    @staticmethod
    def slugify(query: str) -> str:
        """URL path form of a query: ``rtx 5060 ti`` -> ``rtx-5060-ti``."""
        cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in query.lower())
        return "-".join(cleaned.split())


_REGISTRY: dict[str, type[StoreAdapter]] = {}


def register(adapter_cls: type[StoreAdapter]) -> type[StoreAdapter]:
    """Class decorator adding an adapter to the registry."""
    _REGISTRY[adapter_cls.slug] = adapter_cls
    return adapter_cls


def get_adapter(slug: str) -> StoreAdapter:
    """Instantiate the adapter registered under ``slug``."""
    try:
        return _REGISTRY[slug]()
    except KeyError:
        raise KeyError(
            f"unknown store {slug!r}; available: {', '.join(sorted(_REGISTRY))}"
        ) from None


def available_stores() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
