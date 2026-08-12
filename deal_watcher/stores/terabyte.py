"""TerabyteShop adapter.

Terabyte renders product cards server-side, so parsing is plain HTML. The site
sits behind a JavaScript interstitial that plain HTTP cannot clear, so this
store is configured with ``fetcher: browser``.

robots.txt allows ``/hardware/`` and ``/produto/`` but not the search endpoint,
so the adapter reads a category listing page and lets
:mod:`deal_watcher.matching` narrow it down.

Price note: the headline price on a Terabyte card is the *Pix / à vista* price,
which is what the store charges for an immediate payment. That is the number
this adapter reports.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser, Node

from ..models import ProductOffer
from ..prices import PriceParseError, parse_brl
from .base import ParseError, StoreAdapter, register

#: Category listing pages, keyed by the kind of product being searched for.
#: A query that matches none of these falls back to the GPU listing.
_CATEGORIES = {
    "gpu": "hardware/placas-de-video",
    "cpu": "hardware/processadores",
    "ram": "hardware/memorias",
    "ssd": "hardware/discos-e-armazenamento/ssd",
}

_CATEGORY_HINTS = (
    ("cpu", ("ryzen", "core i", "processador", "threadripper")),
    ("ram", ("ddr4", "ddr5", "memoria", "memória")),
    ("ssd", ("ssd", "nvme", "m.2")),
)


@register
class TerabyteAdapter(StoreAdapter):
    slug = "terabyte"
    display_name = "TerabyteShop"
    notes = "Behind a JS interstitial: needs the browser fetcher. Prices shown are Pix/à vista."

    base_url = "https://www.terabyteshop.com.br"

    def category_for(self, query: str) -> str:
        lowered = query.casefold()
        for category, hints in _CATEGORY_HINTS:
            if any(hint in lowered for hint in hints):
                return _CATEGORIES[category]
        return _CATEGORIES["gpu"]

    def search_url(self, query: str) -> str:
        return f"{self.base_url}/{self.category_for(query)}"

    def parse(self, page: str) -> tuple[ProductOffer, ...]:
        tree = HTMLParser(page)
        cards = tree.css("div.product-item")
        if not cards:
            raise ParseError("no product cards found (layout change or bot challenge?)")

        offers = []
        for card in cards:
            offer = self._to_offer(card)
            if offer is not None:
                offers.append(offer)
        if not offers:
            raise ParseError(f"found {len(cards)} cards but could not read any of them")
        return tuple(offers)

    def _to_offer(self, card: Node) -> ProductOffer | None:
        link = card.css_first("a.product-item__name")
        if link is None:
            return None
        url = link.attributes.get("href") or ""
        name = (link.attributes.get("title") or link.text()).strip()
        if not name or not url:
            return None

        # "Todos vendidos" replaces the price box when a card is out of stock.
        sold_out = card.css_first(".tbt_esgotado") is not None
        price_node = card.css_first(".product-item__new-price span")
        if price_node is None:
            # Out-of-stock cards carry no price. Recording a zero would poison
            # the price history, and matching would reject them anyway, so the
            # card is skipped entirely.
            return None

        try:
            price = parse_brl(price_node.text())
        except PriceParseError:
            return None

        return ProductOffer(
            store=self.display_name,
            name=name,
            price=price,
            url=url,
            available=not sold_out,
            raw_id=self._product_id(url),
        )

    @staticmethod
    def _product_id(url: str) -> str | None:
        parts = [part for part in url.split("/") if part]
        # .../produto/<id>/<slug>
        if "produto" in parts:
            index = parts.index("produto")
            if index + 1 < len(parts):
                return parts[index + 1]
        return None
