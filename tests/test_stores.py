"""Store adapters, exercised against HTML captured from the real sites.

These tests never touch the network, so a store being offline, slow, or serving
a bot challenge cannot make CI fail. When a store changes its layout, refresh
the fixture in ``tests/fixtures/`` and the tests will tell you what broke.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from deal_watcher.fetchers import FetchError
from deal_watcher.matching import match_offer
from deal_watcher.stores import ParseError, available_stores, get_adapter
from deal_watcher.stores.kabum import KabumAdapter
from deal_watcher.stores.pichau import PichauAdapter
from deal_watcher.stores.terabyte import TerabyteAdapter

from .conftest import FakeFetcher, FakeFetcherFactory, fixture


class TestRegistry:
    def test_every_store_is_registered(self) -> None:
        assert set(available_stores()) == {"kabum", "pichau", "terabyte"}

    def test_unknown_store_names_itself_clearly(self) -> None:
        with pytest.raises(KeyError, match="unknown store"):
            get_adapter("magalu")

    @pytest.mark.parametrize("slug", ["kabum", "pichau", "terabyte"])
    def test_every_adapter_declares_its_identity(self, slug: str) -> None:
        adapter = get_adapter(slug)
        assert adapter.slug == slug
        assert adapter.display_name
        assert adapter.notes


class TestKabum:
    def test_search_url_respects_robots(self) -> None:
        # robots.txt disallows /busca/*? -- the path form carries no query.
        url = KabumAdapter().search_url("rtx 5060 ti")
        assert url == "https://www.kabum.com.br/busca/rtx-5060-ti"
        assert "?" not in url

    def test_parses_the_embedded_catalogue(self) -> None:
        offers = KabumAdapter().parse(fixture("kabum_search.html"))
        assert len(offers) == 10
        assert all(offer.store == "KaBuM!" for offer in offers)
        assert all(offer.price > 0 for offer in offers)
        assert all(offer.url.startswith("https://www.kabum.com.br/produto/") for offer in offers)

    def test_matching_narrows_a_real_page_to_the_right_cards(self, rtx_match_rules) -> None:
        offers = KabumAdapter().parse(fixture("kabum_search.html"))
        matched = [offer for offer in offers if match_offer(offer, rtx_match_rules)]
        assert matched, "fixture should contain at least one real 16GB card"
        for offer in matched:
            assert "5060" in offer.name.lower()
            assert "8gb" not in offer.name.lower().replace(" ", "")

    def test_a_page_without_the_payload_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match="__NEXT_DATA__"):
            KabumAdapter().parse("<html><body>Just a moment...</body></html>")

    def test_broken_json_is_a_parse_error(self) -> None:
        page = '<script id="__NEXT_DATA__" type="application/json">{not json</script>'
        with pytest.raises(ParseError, match="valid JSON"):
            KabumAdapter().parse(page)

    def test_unexpected_shape_is_a_parse_error(self) -> None:
        page = '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'
        with pytest.raises(ParseError, match="unexpected"):
            KabumAdapter().parse(page)


class TestTerabyte:
    """Read via sitemap + product pages: its listing route is disallowed and truncated."""

    def test_it_uses_the_robots_allowed_sitemap(self) -> None:
        adapter = TerabyteAdapter()
        assert adapter.sitemap_url == "https://www.terabyteshop.com.br/sitemap-manus.xml"
        # robots.txt says `Disallow: /busca`, so search must never be used.
        assert "busca" not in adapter.sitemap_url

    def test_only_product_urls_are_considered(self) -> None:
        sitemap = (
            "<urlset>"
            "<url><loc>https://www.terabyteshop.com.br/produto/1/placa-de-video-rtx-5060-ti-16gb</loc></url>"
            "<url><loc>https://www.terabyteshop.com.br/hardware/placas-de-video</loc></url>"
            "<url><loc>https://www.terabyteshop.com.br/blog/rtx-5060-ti-16gb-review</loc></url>"
            "</urlset>"
        )
        fetcher = FakeFetcher({"sitemap-manus": sitemap})
        urls = TerabyteAdapter().sitemap_urls(FakeFetcherFactory(fetcher))
        assert len(urls) == 1
        assert "/produto/" in urls[0]

    def test_reads_price_and_stock_from_json_ld(self) -> None:
        offer = TerabyteAdapter().parse_product(fixture("terabyte_product.html"))
        assert offer is not None
        assert offer.store == "TerabyteShop"
        assert offer.price == Decimal("3899.99")
        assert offer.currency == "BRL"
        assert offer.available is True
        assert offer.url.startswith("https://www.terabyteshop.com.br/produto/")

    def test_the_store_suffix_is_stripped_from_the_title(self) -> None:
        # Titles arrive as "GPU Palit RTX 5060 Ti Infinity 3 16GB | Terabyte".
        offer = TerabyteAdapter().parse_product(fixture("terabyte_product.html"))
        assert offer is not None
        assert "|" not in offer.name
        assert not offer.name.endswith("Terabyte")
        assert "5060 Ti" in offer.name

    def test_product_id_is_the_numeric_id_not_the_brand(self) -> None:
        # Terabyte puts the manufacturer in `sku`, which would collapse every
        # Palit card onto one history key.
        offer = TerabyteAdapter().parse_product(fixture("terabyte_product.html"))
        assert offer is not None
        assert offer.raw_id is not None and offer.raw_id.isdigit()

    def test_the_real_fixture_matches_the_shipped_rules(self, rtx_match_rules) -> None:
        offer = TerabyteAdapter().parse_product(fixture("terabyte_product.html"))
        assert offer is not None
        assert match_offer(offer, rtx_match_rules)

    def test_out_of_stock_is_read_correctly(self) -> None:
        page = fixture("terabyte_product.html").replace("InStock", "OutOfStock")
        offer = TerabyteAdapter().parse_product(page)
        assert offer is not None and offer.available is False

    def test_a_challenge_page_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match=r"schema\.org/Product"):
            TerabyteAdapter().parse_product("<html><title>Just a moment...</title></html>")


class TestPichau:
    def test_reads_price_and_availability_from_json_ld(self) -> None:
        offer = PichauAdapter().parse_product(fixture("pichau_product.html"))
        assert offer is not None
        assert offer.store == "Pichau"
        assert offer.price == Decimal("5294.11")
        assert offer.currency == "BRL"
        assert offer.available is False  # the captured page said OutOfStock
        assert offer.raw_id == "PRIME-RTX5060TI-16G"

    def test_in_stock_is_read_correctly(self) -> None:
        page = fixture("pichau_product.html").replace("OutOfStock", "InStock")
        offer = PichauAdapter().parse_product(page)
        assert offer is not None and offer.available is True

    def test_used_stock_is_never_offered(self) -> None:
        page = fixture("pichau_product.html").replace("NewCondition", "UsedCondition")
        assert PichauAdapter().parse_product(page) is None

    def test_a_page_without_product_data_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match=r"schema\.org/Product"):
            PichauAdapter().parse_product("<html><title>Just a moment...</title></html>")

    def test_candidates_come_from_sitemap_slugs(self) -> None:
        urls = (
            "https://www.pichau.com.br/placa-de-video-asus-rtx-5060-ti-prime-16gb-gddr7",
            "https://www.pichau.com.br/placa-de-video-asus-rtx-5060-ti-prime-8gb-gddr7",
            "https://www.pichau.com.br/placa-de-video-asus-rtx-5070-ti-16gb-gddr7",
            "https://www.pichau.com.br/teclado-mecanico",
        )
        candidates = PichauAdapter.candidate_urls(urls, "rtx-5060-ti-16gb", limit=10)
        assert candidates == (urls[0],)

    def test_candidate_limit_is_respected(self) -> None:
        urls = tuple(f"https://www.pichau.com.br/rtx-5060-ti-16gb-{i}" for i in range(50))
        assert len(PichauAdapter.candidate_urls(urls, "rtx-5060-ti", limit=12)) == 12

    def test_end_to_end_over_fake_transports(self) -> None:
        sitemap = (
            "<urlset>"
            "<url><loc>https://www.pichau.com.br/placa-de-video-asus-rtx-5060-ti-16gb</loc></url>"
            "<url><loc>https://www.pichau.com.br/teclado</loc></url>"
            "</urlset>"
        )
        fetcher = FakeFetcher(
            {
                "sitemap.xml": sitemap,
                "placa-de-video-asus-rtx-5060-ti-16gb": fixture("pichau_product.html"),
            }
        )
        result = PichauAdapter().search(
            "rtx-5060-ti-16gb", FakeFetcherFactory(fetcher), kind="browser", max_results=12
        )
        assert result.ok
        assert len(result.offers) == 1
        assert result.offers[0].price == Decimal("5294.11")
        # One sitemap read plus exactly one product page: no scattergun fetching.
        assert len(fetcher.requested) == 2

    def test_sitemap_is_cached_between_cycles(self) -> None:
        sitemap = (
            "<urlset><url><loc>https://www.pichau.com.br/rtx-5060-ti-16gb</loc></url></urlset>"
        )
        fetcher = FakeFetcher(
            {"sitemap.xml": sitemap, "rtx-5060-ti-16gb": fixture("pichau_product.html")}
        )
        adapter = PichauAdapter()
        factory = FakeFetcherFactory(fetcher)
        for _ in range(3):
            adapter.search("rtx-5060-ti-16gb", factory, kind="browser", max_results=12)
        assert sum(1 for url in fetcher.requested if "sitemap" in url) == 1


class TestErrorIsolation:
    """A failing store must come back as data, never as an exception."""

    def test_fetch_failure_becomes_a_failed_result(self) -> None:
        factory = FakeFetcherFactory(FakeFetcher({"kabum": FetchError("HTTP 403")}))
        result = KabumAdapter().search("rtx 5060 ti", factory, kind="http")
        assert not result.ok
        assert "403" in (result.error or "")
        assert result.offers == ()

    def test_parse_failure_becomes_a_failed_result(self) -> None:
        factory = FakeFetcherFactory(FakeFetcher({"kabum": "<html>nope</html>"}))
        result = KabumAdapter().search("rtx 5060 ti", factory, kind="http")
        assert not result.ok
        assert "__NEXT_DATA__" in (result.error or "")

    def test_an_unexpected_exception_is_still_contained(self) -> None:
        factory = FakeFetcherFactory(FakeFetcher({"kabum": RuntimeError("kaboom")}))
        result = KabumAdapter().search("rtx 5060 ti", factory, kind="http")
        assert not result.ok
        assert "kaboom" in (result.error or "")

    def test_a_partially_loaded_page_is_a_failure_not_an_empty_result(self) -> None:
        # The quiet failure this guards: a lazy listing that renders a fraction
        # of its cards, finds no deal in them, and reports "no deals" instead of
        # "did not finish loading".
        factory = FakeFetcherFactory(FakeFetcher({"kabum": fixture("kabum_search.html")}))
        result = KabumAdapter().search("rtx 5060 ti", factory, kind="http", min_results=40)
        assert not result.ok
        assert "did not finish loading" in (result.error or "")
        assert result.offers == ()

    def test_a_full_page_passes_the_same_guard(self) -> None:
        factory = FakeFetcherFactory(FakeFetcher({"kabum": fixture("kabum_search.html")}))
        result = KabumAdapter().search("rtx 5060 ti", factory, kind="http", min_results=10)
        assert result.ok
        assert len(result.offers) == 10

    def test_the_guard_is_off_by_default(self) -> None:
        factory = FakeFetcherFactory(FakeFetcher({"kabum": fixture("kabum_search.html")}))
        assert KabumAdapter().search("rtx 5060 ti", factory, kind="http").ok

    def test_max_results_caps_a_runaway_page(self) -> None:
        factory = FakeFetcherFactory(FakeFetcher({"kabum": fixture("kabum_search.html")}))
        result = KabumAdapter().search("rtx 5060 ti", factory, kind="http", max_results=3)
        assert len(result.offers) == 3
