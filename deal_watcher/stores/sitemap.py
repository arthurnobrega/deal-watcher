"""Reading a store through its sitemap and product pages.

Two of the three stores here ended up needing the same shape, for the same
reason: their listing routes are closed to us and their product pages are not.

* Pichau serves its "Site em Manutenção" page for ``/search?q=...`` and for
  category pages alike, while product pages render normally.
* TerabyteShop disallows ``/busca`` in ``robots.txt`` and now serves its
  category listing as 25 cards with a "load more" button that does nothing
  under automation -- but it publishes ``sitemap-manus.xml``, explicitly
  ``Allow``-ed, refreshed every six hours, alongside an ``llms.txt`` describing
  its catalogue for exactly this kind of client.

So both stores are read the sanctioned way: discover product URLs from the
sitemap the store advertises, then read prices from the ``schema.org/Product``
block each product page carries. That block is also the most stable thing on
either site -- far more stable than their CSS.

The cost is one request per candidate, so keep the per-store ``max_results``
low and the query specific. ``candidate_urls`` is a cheap narrowing step, not
product matching: the real accept/reject decision still belongs to
:mod:`deal_watcher.matching`, which sees the full name from the page.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..fetchers import FetcherFactory
from ..models import ProductOffer
from ..prices import PriceParseError, to_decimal
from .base import ParseError, StoreAdapter

log = logging.getLogger(__name__)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class SitemapProductAdapter(StoreAdapter):
    """Base for stores read via sitemap discovery plus product pages."""

    #: Sitemap advertised by the store, ideally in its robots.txt.
    sitemap_url: str
    #: Sitemaps are large and change slowly; refetching one every cycle would
    #: be rude and pointless.
    sitemap_ttl_seconds: int = 6 * 3600
    #: Only these URL prefixes are considered products.
    product_url_marker: str = "/"
    #: Transport for the sitemap itself. It is plain XML behind no interstitial
    #: for some stores and behind one for others.
    sitemap_fetcher: str = "http"

    def __init__(self) -> None:
        self._sitemap_cache: tuple[float, tuple[str, ...]] | None = None

    def search_url(self, query: str) -> str:
        # Not used by `collect`, but part of the contract and useful in logs.
        return self.sitemap_url

    # --- discovery -----------------------------------------------------

    def sitemap_urls(self, fetchers: FetcherFactory) -> tuple[str, ...]:
        now = time.monotonic()
        if self._sitemap_cache and now - self._sitemap_cache[0] < self.sitemap_ttl_seconds:
            return self._sitemap_cache[1]
        xml = fetchers.get(self.sitemap_fetcher).fetch(self.sitemap_url)
        urls = tuple(url for url in _LOC_RE.findall(xml) if self.product_url_marker in url)
        if not urls:
            raise ParseError("sitemap contained no product URLs")
        self._sitemap_cache = (now, urls)
        log.debug("%s: sitemap has %d product URLs", self.slug, len(urls))
        return urls

    @staticmethod
    def candidate_urls(urls: tuple[str, ...], query: str, limit: int) -> tuple[str, ...]:
        """URLs whose slug contains every token of the query.

        Coarse store-side narrowing that only decides which pages are worth a
        request. Put distinguishing words in the query -- ``placa-de-video``
        keeps prebuilt PCs out of a GPU search, and each one skipped is a page
        load saved.
        """
        tokens = _TOKEN_RE.findall(query.casefold())
        if not tokens:
            return ()
        matches = [url for url in urls if all(token in url.casefold() for token in tokens)]
        return tuple(matches[:limit])

    # --- collection ----------------------------------------------------

    def collect(
        self,
        query: str,
        fetchers: FetcherFactory,
        kind: str,
        max_results: int,
    ) -> tuple[ProductOffer, ...]:
        urls = self.sitemap_urls(fetchers)
        candidates = self.candidate_urls(urls, query, max_results)
        if not candidates:
            log.info("%s: no sitemap entries matched %r", self.slug, query)
            return ()

        log.info("%s: %d sitemap candidates for %r", self.slug, len(candidates), query)
        fetcher = fetchers.get(kind)
        offers: list[ProductOffer] = []
        failures = 0
        for url in candidates:
            try:
                offer = self.parse_product(fetcher.fetch(url, self.browser_hints))
            except Exception as exc:  # one bad page must not sink the store
                failures += 1
                log.warning("%s: skipping %s: %s", self.slug, url, exc)
                continue
            if offer is not None:
                offers.append(offer)
        if not offers and failures:
            raise ParseError(f"none of {len(candidates)} candidate pages could be read")
        return tuple(offers)

    # --- parsing -------------------------------------------------------

    def parse(self, page: str) -> tuple[ProductOffer, ...]:
        """Parse a single product page. Present to satisfy the adapter contract."""
        offer = self.parse_product(page)
        return (offer,) if offer else ()

    def parse_product(self, page: str) -> ProductOffer | None:
        """Read the ``schema.org/Product`` block from a product page."""
        product = self.product_ld_json(page)
        if product is None:
            raise ParseError("no schema.org/Product block found (interstitial or layout change?)")

        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            raise ParseError("product block has no offer")

        name = self.clean_name(str(product.get("name") or ""))
        url = str(offers.get("url") or product.get("url") or "").strip()
        if not name or not url:
            return None

        # Refurbished or used stock is not comparable to a new card.
        condition = str(offers.get("itemCondition") or "").casefold()
        if condition and "newcondition" not in condition:
            return None

        try:
            price = to_decimal(offers.get("price"))
        except (PriceParseError, TypeError):
            return None

        availability = str(offers.get("availability") or "").casefold()
        available = "instock" in availability or "limitedavailability" in availability

        return ProductOffer(
            store=self.display_name,
            name=name,
            price=price,
            url=url,
            available=available,
            currency=str(offers.get("priceCurrency") or "BRL"),
            raw_id=self.product_id(url, product),
        )

    def clean_name(self, name: str) -> str:
        """Normalise a product title. Overridden where a store decorates it."""
        return name.strip()

    def product_id(self, url: str, product: dict[str, Any]) -> str | None:
        """Stable per-product identifier, used for price history and dedup."""
        sku = str(product.get("sku") or "").strip()
        return sku or None

    @staticmethod
    def product_ld_json(page: str) -> dict[str, Any] | None:
        for match in _LD_JSON_RE.finditer(page):
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            for entry in data if isinstance(data, list) else [data]:
                if isinstance(entry, dict) and entry.get("@type") == "Product":
                    return entry
        return None
