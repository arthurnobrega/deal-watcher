"""Pichau adapter.

Pichau is the awkward one, so the reasoning is written down rather than hidden
in the code:

* Its search endpoint takes a query string, and ``robots.txt`` says
  ``Disallow: /*?*``. So search is off the table.
* Its category pages render client-side and lazily: a fetch of
  ``/hardware/placa-de-video`` reports "1-36 of 2144 results" but only a handful
  of cards are in the DOM, and the wanted model is rarely among them.
* Its ``sitemap.xml`` is advertised in ``robots.txt``, served over plain HTTP
  without any interstitial, and lists every product URL with the model in the
  slug.
* Its product pages carry a complete ``schema.org/Product`` block with price,
  currency, availability and condition -- the most stable thing on the site.

So: discover candidate URLs from the sitemap (cheap, cached), then read prices
from the product pages of the few candidates that match the query. The sitemap
fetch uses plain HTTP even when the store is configured for the browser
transport, because it needs no browser.
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
from .base import ParseError, StoreAdapter, register

log = logging.getLogger(__name__)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: The sitemap is ~6 MB and changes slowly; refetching it every cycle would be
#: rude and pointless.
_SITEMAP_TTL_SECONDS = 6 * 3600


@register
class PichauAdapter(StoreAdapter):
    slug = "pichau"
    display_name = "Pichau"
    notes = (
        "Product pages sit behind a JS interstitial and need the browser fetcher. "
        "Candidates are discovered from the robots.txt-advertised sitemap."
    )

    base_url = "https://www.pichau.com.br"
    sitemap_url = f"{base_url}/media/sitemap.xml"

    def __init__(self) -> None:
        self._sitemap_cache: tuple[float, tuple[str, ...]] | None = None

    def search_url(self, query: str) -> str:
        # Not used by `collect`, but part of the contract and useful in logs.
        return self.sitemap_url

    # --- discovery -----------------------------------------------------

    def _sitemap_urls(self, fetchers: FetcherFactory) -> tuple[str, ...]:
        now = time.monotonic()
        if self._sitemap_cache and now - self._sitemap_cache[0] < _SITEMAP_TTL_SECONDS:
            return self._sitemap_cache[1]
        xml = fetchers.get("http").fetch(self.sitemap_url)
        urls = tuple(_LOC_RE.findall(xml))
        if not urls:
            raise ParseError("sitemap contained no <loc> entries")
        self._sitemap_cache = (now, urls)
        return urls

    @staticmethod
    def candidate_urls(urls: tuple[str, ...], query: str, limit: int) -> tuple[str, ...]:
        """URLs whose slug contains every token of the query.

        This is coarse store-side narrowing, not product matching: it only
        decides which pages are worth spending a request on. The real accept or
        reject decision still happens in :mod:`deal_watcher.matching`, against
        the full product name from the page.
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
        urls = self._sitemap_urls(fetchers)
        candidates = self.candidate_urls(urls, query, max_results)
        if not candidates:
            log.info("pichau: no sitemap entries matched %r", query)
            return ()

        log.info("pichau: %d sitemap candidates for %r", len(candidates), query)
        fetcher = fetchers.get(kind)
        offers: list[ProductOffer] = []
        for url in candidates:
            try:
                page = fetcher.fetch(url)
                offer = self.parse_product(page)
            except Exception as exc:
                log.warning("pichau: skipping %s: %s", url, exc)
                continue
            if offer is not None:
                offers.append(offer)
        if not offers and candidates:
            raise ParseError(f"none of {len(candidates)} candidate pages could be read")
        return tuple(offers)

    # --- parsing -------------------------------------------------------

    def parse(self, page: str) -> tuple[ProductOffer, ...]:
        """Parse a single product page. Present to satisfy the adapter contract."""
        offer = self.parse_product(page)
        return (offer,) if offer else ()

    def parse_product(self, page: str) -> ProductOffer | None:
        """Read the ``schema.org/Product`` block from a product page."""
        product = self._product_ld_json(page)
        if product is None:
            raise ParseError("no schema.org/Product block found (interstitial or layout change?)")

        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            raise ParseError("product block has no offer")

        name = str(product.get("name") or "").strip()
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
            raw_id=str(product.get("sku") or "") or None,
        )

    @staticmethod
    def _product_ld_json(page: str) -> dict[str, Any] | None:
        for match in _LD_JSON_RE.finditer(page):
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            for entry in data if isinstance(data, list) else [data]:
                if isinstance(entry, dict) and entry.get("@type") == "Product":
                    return entry
        return None
