"""Configuration loading, the shipped config.yaml, and the CLI surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from deal_watcher.cli import main
from deal_watcher.config import Config, ProductConfig, load_config
from deal_watcher.matching import match_offer, memory_sizes_gb
from deal_watcher.storage import RunStats, Storage
from deal_watcher.stores.kabum import KabumAdapter

from .conftest import fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "config.yaml"


class TestShippedConfig:
    """The config that actually runs in production has to be right."""

    def test_it_loads(self) -> None:
        config = load_config(SHIPPED_CONFIG)
        assert config.products
        assert set(config.stores) == {"kabum", "terabyte", "pichau", "mercadolivre"}

    def test_the_rtx_target_is_configured_not_hardcoded(self) -> None:
        product = load_config(SHIPPED_CONFIG).products[0]
        assert product.name == "RTX 5060 Ti 16GB"
        assert product.max_price > 0

    def test_shipped_alert_levels_are_coherent(self) -> None:
        """Levels must get stricter, and none may sit above the target.

        Deliberately asserts the *shape*, not the numbers: a test that repeats
        the prices it guards breaks every time you tune one, which teaches you
        to ignore it. The failure this catches is real -- a `good` ceiling
        below `max_price` leaves a band where an offer beats the target and
        still alerts nobody.
        """
        product = load_config(SHIPPED_CONFIG).products[0]
        levels = product.alert_levels
        assert levels.good is not None
        assert levels.good == product.max_price, "target and good ceiling must agree"
        assert levels.excellent is not None and levels.excellent < levels.good
        assert levels.buy_now is not None and levels.buy_now < levels.excellent

    def test_its_match_rules_work_on_a_real_store_page(self) -> None:
        product = load_config(SHIPPED_CONFIG).products[0]
        offers = KabumAdapter().parse(fixture("kabum_search.html"))
        matched = [offer for offer in offers if match_offer(offer, product.match)]
        rejected = [offer for offer in offers if not match_offer(offer, product.match)]

        assert matched, "shipped rules should find the 16GB cards on a real page"
        assert rejected, "shipped rules should reject the 8GB cards on the same page"
        for offer in matched:
            assert 16 in memory_sizes_gb(offer.name)
            assert 8 not in memory_sizes_gb(offer.name)

    def test_it_contains_no_secrets(self) -> None:
        text = SHIPPED_CONFIG.read_text(encoding="utf-8")
        # Only env var *names* may appear here.
        assert "TELEGRAM_BOT_TOKEN" in text
        assert ":AA" not in text  # the shape of a real bot token
        for word in ("password", "api_key", "authorization"):
            assert word not in text.casefold()


class TestConfigValidation:
    def test_a_typo_is_rejected_rather_than_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("products:\n  - name: X\n    max_price: 100\n    maxx_price: 90\n")
        with pytest.raises(ValueError):
            load_config(path)

    def test_at_least_one_product_is_required(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("products: []\n")
        with pytest.raises(ValueError, match="at least one product"):
            load_config(path)

    def test_a_product_cannot_reference_an_undeclared_store(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            "stores:\n  kabum: {}\n"
            "products:\n  - name: X\n    max_price: 100\n    stores: [pichau]\n"
        )
        with pytest.raises(ValueError, match="undeclared stores"):
            load_config(path)

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_env_var_selects_the_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "custom.yaml"
        path.write_text("products:\n  - name: X\n    max_price: 100\n")
        monkeypatch.setenv("DEAL_WATCHER_CONFIG", str(path))
        assert load_config().products[0].name == "X"

    def test_products_can_be_limited_to_certain_stores(self) -> None:
        config = Config(
            products=(
                ProductConfig.model_validate({"name": "X", "max_price": 100, "stores": ["kabum"]}),
            ),
            stores={"kabum": {}, "pichau": {}},  # type: ignore[dict-item]
        )
        product = config.products[0]
        assert product.targets_store("kabum")
        assert not product.targets_store("pichau")

    def test_query_falls_back_to_the_product_name(self) -> None:
        product = ProductConfig.model_validate({"name": "RTX 5060 Ti 16GB", "max_price": 100})
        assert product.query_for("kabum") == "RTX 5060 Ti 16GB"


class TestCli:
    def test_stores_command_lists_the_adapters(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["stores"]) == 0
        out = capsys.readouterr().out
        assert "kabum" in out and "pichau" in out and "terabyte" in out

    def test_health_is_unhealthy_before_the_first_cycle(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text(
            f"database: {tmp_path / 'h.db'}\nproducts:\n  - name: X\n    max_price: 100\n"
        )
        assert main(["--config", str(config), "health"]) == 2
        assert "UNHEALTHY" in capsys.readouterr().out

    def test_health_is_ok_after_a_recent_cycle(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from datetime import UTC, datetime

        db = tmp_path / "h.db"
        config = tmp_path / "config.yaml"
        config.write_text(f"database: {db}\nproducts:\n  - name: X\n    max_price: 100\n")
        now = datetime.now(UTC)
        with Storage(db) as storage:
            storage.record_run(
                RunStats(
                    started_at=now,
                    finished_at=now,
                    stores_ok=3,
                    stores_failed=0,
                    offers_found=42,
                    matches_found=2,
                    alerts_sent=0,
                )
            )
        assert main(["--config", str(config), "health"]) == 0
        assert "OK: last cycle" in capsys.readouterr().out

    def test_history_on_an_empty_database(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text(
            f"database: {tmp_path / 'e.db'}\nproducts:\n  - name: X\n    max_price: 100\n"
        )
        assert main(["--config", str(config), "history"]) == 0
        assert "no price history" in capsys.readouterr().out

    def test_a_bad_config_fails_without_a_stack_trace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("products: []\n")
        assert main(["--config", str(config), "health"]) == 1
        assert "configuration error" in capsys.readouterr().err
