"""Pichau adapter.

Read through the sitemap advertised in ``robots.txt`` plus product pages,
because every listing route is closed to us. Measured round-robin, five loads
each, from a residential connection with a real browser window:

* ``/search?q=...`` returned Pichau's "Site em Manutenção" page 5/5
* ``/hardware/placa-de-video`` returned it 5/5
* a product page rendered correctly 5/5

Search is additionally disallowed by ``robots.txt`` (``Disallow: /*?*``), but
that is moot -- it does not serve a catalogue to an automated client at all,
headless or headed. So this is not a preference for sitemaps over search:
product pages are the only route Pichau renders, and the sitemap is the only
sanctioned way to learn which product pages exist.

Pichau also blocks datacenter IPs outright, which is why it ships disabled.
See the README for the measurements.
"""

from __future__ import annotations

from .base import register
from .sitemap import SitemapProductAdapter


@register
class PichauAdapter(SitemapProductAdapter):
    slug = "pichau"
    display_name = "Pichau"
    notes = (
        "Product pages sit behind a JS interstitial and need the browser fetcher. Candidates "
        "come from the robots.txt-advertised sitemap; every listing route is closed. Blocked "
        "entirely from datacenter IPs, so it is disabled by default."
    )

    base_url = "https://www.pichau.com.br"
    sitemap_url = f"{base_url}/media/sitemap.xml"
    product_url_marker = "pichau.com.br/"
