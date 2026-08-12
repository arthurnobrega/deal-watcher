"""TerabyteShop adapter.

Read through ``sitemap-manus.xml`` and product pages. The listing route is not
an option, for two independent reasons:

* ``robots.txt`` says ``Disallow: /busca``.
* The category page (``/hardware/placas-de-video``, which *is* allowed) now
  serves 25 cards and hides the rest behind a "CLIQUE PARA VER MAIS PRODUTOS"
  button. Scrolling does not trigger it, its click handler does nothing when
  dispatched programmatically, and every pagination parameter tried returns
  the same first 25 cards. Those 25 contained no RTX 5060 Ti at all.

The store's own ``robots.txt`` points the way out: it ``Allow``s ``/produto/``
and ``/sitemap-manus.xml`` -- a 4,400-URL sitemap regenerated every six hours --
and publishes an ``llms.txt`` describing the catalogue for automated clients.
So candidates come from the sitemap and prices from each product page's
``schema.org/Product`` block.

Price note: Terabyte's headline price is the *Pix / à vista* price, which is
what its structured data reports and what this adapter records. A credit-card
total will be higher.
"""

from __future__ import annotations

from typing import Any

from .base import register
from .sitemap import SitemapProductAdapter


@register
class TerabyteAdapter(SitemapProductAdapter):
    slug = "terabyte"
    display_name = "TerabyteShop"
    notes = (
        "Read via the robots.txt-advertised sitemap plus product pages; the listing route is "
        "disallowed and, as of 2026-08, truncated. Needs the browser fetcher. Prices are Pix."
    )

    base_url = "https://www.terabyteshop.com.br"
    sitemap_url = f"{base_url}/sitemap-manus.xml"
    product_url_marker = "/produto/"
    # Even the sitemap sits behind the interstitial on this store.
    sitemap_fetcher = "browser"

    def clean_name(self, name: str) -> str:
        # Titles arrive as "GPU Palit RTX 5060 Ti Infinity 3 16GB | Terabyte".
        return name.split("|")[0].strip()

    def product_id(self, url: str, product: dict[str, Any]) -> str | None:
        # `sku` here is the manufacturer ("Palit"), not a product id, so the
        # numeric id from /produto/<id>/<slug> is used instead.
        parts = [part for part in url.split("/") if part]
        if "produto" in parts:
            index = parts.index("produto")
            if index + 1 < len(parts):
                return parts[index + 1]
        return None
