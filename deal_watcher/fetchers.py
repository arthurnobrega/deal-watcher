"""How pages are fetched, kept separate from how they are parsed.

Store adapters ask a :class:`Fetcher` for HTML and never touch the network
themselves. That split is what lets tests feed adapters static fixtures, and
what lets a store that sits behind a JavaScript interstitial switch transport
without its parser changing a line.

Two transports ship:

``HttpFetcher``
    Plain HTTP via httpx. Cheap, and the default.

``BrowserFetcher``
    A real headless Chromium via Playwright, for stores whose pages are only
    assembled client-side. Opt-in, off by default, and heavier: it is only
    worth its cost when plain HTTP genuinely cannot see the page.

Both honour a configured delay between requests. deal-watcher polls a handful of
search pages every 15 minutes; keep it that way.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from .config import BrowserConfig, HttpConfig

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """A page could not be retrieved. Carries no response body, only a reason."""


#: Optional per-store instructions for the browser transport. Store-specific
#: knowledge stays in the adapter; the mechanics stay here.
BrowserHints = Mapping[str, Any]


class Fetcher(Protocol):
    """Anything that can turn a URL into page text."""

    def fetch(self, url: str, hints: BrowserHints | None = None) -> str: ...

    def close(self) -> None: ...


class HttpFetcher:
    """Plain HTTP with a bounded number of retries and a polite delay."""

    def __init__(self, config: HttpConfig) -> None:
        self._config = config
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            },
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._config.delay_between_requests - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def fetch(self, url: str, hints: BrowserHints | None = None) -> str:
        del hints  # plain HTTP cannot click anything
        last_error: Exception | None = None
        for attempt in range(self._config.retries + 1):
            if attempt:
                time.sleep(self._config.backoff_seconds * attempt)
            self._throttle()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
                log.debug("fetch attempt %d failed for %s: %s", attempt + 1, url, exc)
                continue

            if response.status_code == 200:
                return response.text
            # 4xx other than 429 will not fix themselves on retry.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise FetchError(f"HTTP {response.status_code} for {url}")
            last_error = FetchError(f"HTTP {response.status_code} for {url}")

        raise FetchError(f"giving up on {url}: {last_error}")

    def close(self) -> None:
        self._client.close()


class BrowserFetcher:
    """Headless Chromium via Playwright, for client-rendered pages.

    Playwright is an optional dependency (``pip install 'deal-watcher[browser]'``
    plus ``playwright install chromium``). If it is missing, every fetch raises
    :class:`FetchError`, which the monitor isolates to that one store.
    """

    def __init__(self, config: BrowserConfig, http: HttpConfig) -> None:
        self._config = config
        self._http = http
        # Typed as Any because Playwright is an optional dependency: annotating
        # these concretely would make the module unimportable without it.
        self._playwright: Any = None
        self._browser: Any = None

    def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise FetchError(
                "playwright is not installed; install the 'browser' extra or set "
                "this store's fetcher to 'http'"
            ) from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            raise FetchError(f"could not start headless browser: {exc}") from exc
        return self._browser

    def fetch(self, url: str, hints: BrowserHints | None = None) -> str:
        browser = self._ensure_browser()
        timeout_ms = int(self._config.timeout_seconds * 1000)
        context = None
        try:
            context = browser.new_context(
                user_agent=self._http.user_agent,
                locale="pt-BR",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if self._config.wait_after_load_seconds:
                page.wait_for_timeout(int(self._config.wait_after_load_seconds * 1000))
            self._scroll_to_bottom(page)
            if hints:
                self._click_until_exhausted(page, hints)
            return str(page.content())
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"browser fetch failed for {url}: {exc}") from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # pragma: no cover - best-effort cleanup
                    log.debug("browser context close failed", exc_info=True)

    def _scroll_to_bottom(self, page: Any) -> None:
        """Scroll until the page stops growing, so lazy listings finish loading.

        Store listings commonly render a first screenful and fetch the rest as
        you scroll. On a slow box, taking the first screenful at face value
        silently drops most of the catalogue -- and a missing offer looks
        exactly like an offer that is not on sale.
        """
        if not self._config.scroll_passes:
            return
        pause_ms = int(self._config.scroll_pause_seconds * 1000)
        previous_height = 0
        stable = 0
        for _ in range(self._config.scroll_passes):
            try:
                height = int(page.evaluate("document.body.scrollHeight") or 0)
                if height and height == previous_height:
                    # Patience: the height also stops changing in the gap before
                    # the next batch arrives. Bailing on the first unchanged
                    # measurement is how a 150-card listing returns 25 cards.
                    stable += 1
                    if stable >= self._config.scroll_stable_passes:
                        return
                else:
                    stable = 0
                previous_height = height
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(pause_ms)
            except Exception:  # scrolling is best-effort
                log.debug("scrolling stopped early", exc_info=True)
                return

    def _click_until_exhausted(self, page: Any, hints: BrowserHints) -> None:
        """Press a "load more" control until it stops producing new items.

        Some listings replaced infinite scroll with a button, which scrolling
        alone never triggers: the page stops at its first batch and looks like
        a small catalogue.

        Success is measured in *items*, not page height. Height changes a beat
        after the click, so timing it that way reads "nothing happened" while
        the batch is still in flight -- which is how this quietly returned 25
        of ~150 cards.
        """
        selector = str(hints.get("click_selector") or "")
        if not selector:
            return
        item_selector = str(hints.get("item_selector") or "")
        max_clicks = int(hints.get("max_clicks", 10))
        wait_ms = int(hints.get("click_wait_seconds", 10) * 1000)

        def item_count() -> int:
            if not item_selector:
                return int(page.evaluate("document.body.scrollHeight") or 0)
            return int(page.eval_on_selector_all(item_selector, "els => els.length") or 0)

        for _ in range(max_clicks):
            try:
                button = next(
                    (b for b in page.query_selector_all(selector) if b.is_visible()), None
                )
                if button is None:
                    return  # no control left: everything is on the page
                before = item_count()
                # Dispatch the click directly rather than going through
                # Playwright's actionability checks: these listings animate
                # (countdown badges, lazily-sized images), so the button is
                # never "stable" and a normal click times out forever.
                button.evaluate("node => node.click()")

                # Poll for the batch instead of guessing how long it takes.
                deadline, grown = 0, False
                while deadline < wait_ms:
                    page.wait_for_timeout(500)
                    deadline += 500
                    if item_count() > before:
                        grown = True
                        break
                if not grown:
                    return  # clicking changed nothing; we have it all
                self._scroll_to_bottom(page)
            except Exception:  # clicking is best-effort
                log.debug("load-more click stopped early", exc_info=True)
                return

    def close(self) -> None:
        for resource, method in ((self._browser, "close"), (self._playwright, "stop")):
            if resource is None:
                continue
            try:
                getattr(resource, method)()
            except Exception:  # pragma: no cover - best-effort cleanup
                log.debug("browser cleanup failed", exc_info=True)
        self._browser = None
        self._playwright = None


class FetcherFactory:
    """Creates fetchers lazily and shares one instance per kind per cycle."""

    def __init__(self, http: HttpConfig, browser: BrowserConfig) -> None:
        self._http_config = http
        self._browser_config = browser
        self._cache: dict[str, Fetcher] = {}

    def get(self, kind: str) -> Fetcher:
        if kind not in self._cache:
            if kind == "browser":
                if not self._browser_config.enabled:
                    raise FetchError(
                        "this store requires the browser fetcher, but browser.enabled is false"
                    )
                self._cache[kind] = BrowserFetcher(self._browser_config, self._http_config)
            else:
                self._cache[kind] = HttpFetcher(self._http_config)
        return self._cache[kind]

    def close(self) -> None:
        for fetcher in self._cache.values():
            fetcher.close()
        self._cache.clear()
