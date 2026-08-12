"""Mercado Livre adapter, via the official API.

Unlike every other store here, this one is not scraped: Mercado Livre publishes
a documented API, so deal-watcher asks it for JSON instead of guessing at
markup. That makes this the sturdiest adapter in the project -- no interstitial,
no layout changes, no browser -- at the cost of needing credentials.

STATUS: blocked, and shipped disabled. Measured on 2026-08-12 with a real
registered application:

* ``/sites/MLB/search`` returns 403 "forbidden" for both a client_credentials
  token and a user-context token. Site search is an app-level authorisation
  Mercado Livre grants selectively; no OAuth flow works around it.
* ``/products/search`` returns 200 with a user token, but catalogue entries
  carry no price -- that needs a second request per product to read the
  ``buy_box_winner`` on the product detail.
* The authorization_code exchange returns no refresh token, even when the
  request asks for ``scope=offline_access``. Access tokens last about six
  hours, so without one this store would go dead twice a day until someone
  re-authorised it by hand.

The code below is kept because it is correct for an application that *has*
those grants, and the first two findings may change if Mercado Livre approves
an app. Until then, leave the store disabled.

Two environment variables are required, from a free application registered at
https://developers.mercadolivre.com.br/:

    MERCADOLIVRE_CLIENT_ID
    MERCADOLIVRE_CLIENT_SECRET

They are exchanged for a short-lived token via the ``client_credentials``
grant, cached in memory, and refreshed shortly before expiry. As everywhere
else in this project, the values are read from the environment and never
logged, stored, or included in an error message.

A word of caution about matching. Mercado Livre is a marketplace: titles are
written by sellers, not manufacturers, and the same search returns new, used,
imported and grey-market listings side by side. This adapter drops anything not
listed as ``new`` and anything with no stock, but the honest defence is still
:mod:`deal_watcher.matching` -- keep the reject patterns strict for products
watched here.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..fetchers import FetcherFactory, FetchError
from ..models import ProductOffer
from ..prices import PriceParseError, to_decimal
from .base import ParseError, StoreAdapter, register

log = logging.getLogger(__name__)

#: Refresh a little before expiry so a cycle never runs with a stale token.
_TOKEN_LEEWAY_SECONDS = 120


@register
class MercadoLivreAdapter(StoreAdapter):
    slug = "mercadolivre"
    display_name = "Mercado Livre"
    notes = (
        "BLOCKED, ships disabled: site search returns 403 for unapproved apps, and no refresh "
        "token is issued, so tokens die every ~6h. Kept for if Mercado Livre ever approves an "
        "app. See the module docstring for the measurements."
    )

    api_base = "https://api.mercadolibre.com"
    site_id = "MLB"  # Brazil
    client_id_env = "MERCADOLIVRE_CLIENT_ID"
    client_secret_env = "MERCADOLIVRE_CLIENT_SECRET"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=20.0)
        self._token: str | None = None
        self._token_expires_at = 0.0

    # --- authentication -------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        client_id = os.environ.get(self.client_id_env, "").strip()
        client_secret = os.environ.get(self.client_secret_env, "").strip()
        missing = [
            name
            for name, value in (
                (self.client_id_env, client_id),
                (self.client_secret_env, client_secret),
            )
            if not value
        ]
        if missing:
            raise FetchError(
                f"missing environment variables: {', '.join(missing)} "
                "(register a free app at developers.mercadolivre.com.br)"
            )
        return client_id, client_secret

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        client_id, client_secret = self._credentials()
        try:
            response = self._client.post(
                f"{self.api_base}/oauth/token",
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        except httpx.HTTPError as exc:
            # Never echo the exception text: it can carry the request body.
            raise FetchError(f"token request failed: {type(exc).__name__}") from None

        if response.status_code != 200:
            raise FetchError(
                f"token request returned HTTP {response.status_code} "
                "(check the client id and secret)"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise FetchError("token response contained no access_token")

        self._token = str(token)
        expires_in = float(payload.get("expires_in", 0) or 0)
        self._token_expires_at = time.monotonic() + max(0.0, expires_in - _TOKEN_LEEWAY_SECONDS)
        log.debug("mercadolivre: obtained an access token valid for %.0fs", expires_in)
        return self._token

    # --- collection -----------------------------------------------------

    def search_url(self, query: str) -> str:
        return f"{self.api_base}/sites/{self.site_id}/search?q={self.encode(query)}"

    def collect(
        self,
        query: str,
        fetchers: FetcherFactory,
        kind: str,
        max_results: int,
    ) -> tuple[ProductOffer, ...]:
        # This store talks to an API, so it does not use the page fetchers.
        del fetchers, kind
        token = self._access_token()
        try:
            response = self._client.get(
                f"{self.api_base}/sites/{self.site_id}/search",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"q": query, "limit": min(max_results, 50)},
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"search request failed: {type(exc).__name__}") from None

        if response.status_code == 401:
            raise FetchError("search returned HTTP 401 (token rejected)")
        if response.status_code != 200:
            raise FetchError(f"search returned HTTP {response.status_code}")

        return self.parse(response.text)

    # --- parsing --------------------------------------------------------

    def parse(self, page: str) -> tuple[ProductOffer, ...]:
        """Turn a search response body into offers."""
        try:
            payload: Any = httpx.Response(200, text=page).json()
        except ValueError as exc:
            raise ParseError(f"search response is not valid JSON: {exc}") from exc

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise ParseError("search response has no results list")

        offers = []
        for item in results:
            if not isinstance(item, dict):
                continue
            offer = self._to_offer(item)
            if offer is not None:
                offers.append(offer)
        return tuple(offers)

    def _to_offer(self, item: dict[str, Any]) -> ProductOffer | None:
        title = str(item.get("title") or "").strip()
        url = str(item.get("permalink") or "").strip()
        if not title or not url:
            return None

        # A marketplace sells used and refurbished stock next to new stock.
        if str(item.get("condition") or "").casefold() != "new":
            return None

        try:
            price = to_decimal(item.get("price"))
        except (PriceParseError, TypeError):
            return None

        available = int(item.get("available_quantity") or 0) > 0
        status = str(item.get("status") or "active").casefold()
        if status not in ("active", ""):
            available = False

        return ProductOffer(
            store=self.display_name,
            name=title,
            price=price,
            url=url,
            available=available,
            currency=str(item.get("currency_id") or "BRL"),
            raw_id=str(item.get("id") or "") or None,
        )

    def close(self) -> None:
        self._client.close()
