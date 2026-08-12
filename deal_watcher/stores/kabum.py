"""KaBuM! adapter.

KaBuM is a Next.js app that embeds its full catalogue payload in the page as
``__NEXT_DATA__``. Reading that JSON is both more robust and gentler than
scraping the rendered markup: one request, no browser, and field names that
change far less often than CSS classes.

robots.txt allows ``/busca/<term>`` but disallows ``/busca/*?``, so the adapter
uses the query-less path form.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import ProductOffer
from ..prices import PriceParseError, to_decimal
from .base import ParseError, StoreAdapter, register

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


@register
class KabumAdapter(StoreAdapter):
    slug = "kabum"
    display_name = "KaBuM!"
    notes = "Readable over plain HTTP; catalogue comes from the embedded __NEXT_DATA__ JSON."

    base_url = "https://www.kabum.com.br"

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/busca/{self.slugify(query)}"

    def parse(self, page: str) -> tuple[ProductOffer, ...]:
        match = _NEXT_DATA_RE.search(page)
        if match is None:
            raise ParseError("no __NEXT_DATA__ payload found (layout change or bot challenge?)")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ParseError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc

        items = self._catalogue(payload)
        offers = []
        for item in items:
            offer = self._to_offer(item)
            if offer is not None:
                offers.append(offer)
        return tuple(offers)

    @staticmethod
    def _catalogue(payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            data = payload["props"]["pageProps"]["data"]["catalogServer"]["data"]
        except (KeyError, TypeError) as exc:
            raise ParseError(f"unexpected __NEXT_DATA__ shape: missing {exc}") from exc
        if not isinstance(data, list):
            raise ParseError("catalogServer.data is not a list")
        return [item for item in data if isinstance(item, dict)]

    def _to_offer(self, item: dict[str, Any]) -> ProductOffer | None:
        name = item.get("name")
        code = item.get("code")
        if not name or not code:
            return None

        # priceWithDiscount is the price actually charged; price is the list price.
        raw_price = item.get("priceWithDiscount") or item.get("price")
        try:
            price = to_decimal(raw_price)
        except (PriceParseError, TypeError):
            return None

        flags = item.get("flags") or {}
        # Open-box units are refurbished/returned stock: never a like-for-like deal.
        if flags.get("isOpenbox"):
            return None

        available = bool(item.get("available")) and int(item.get("quantity") or 0) > 0
        friendly = item.get("friendlyName") or self.slugify(str(name))

        return ProductOffer(
            store=self.display_name,
            name=str(name),
            price=price,
            url=f"{self.base_url}/produto/{code}/{friendly}",
            available=available,
            raw_id=str(code),
        )
