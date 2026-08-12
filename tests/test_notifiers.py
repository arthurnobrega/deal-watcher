"""Notification rendering and Telegram delivery.

The Telegram tests mock the HTTP layer: no token, no network, no chance of
sending a real message from CI.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest
import respx

from deal_watcher.config import HttpConfig, TelegramConfig
from deal_watcher.logging_setup import RedactionFilter, redact, setup_logging
from deal_watcher.models import Alert, AlertLevel, ProductOffer
from deal_watcher.notifiers.base import NotifierError, render_alert
from deal_watcher.notifiers.telegram import TelegramNotifier

from .conftest import make_alert

FAKE_TOKEN = "123456789:AAFfakefakefakefakefakefakefakefake0"
FAKE_CHAT_ID = "987654321"


@pytest.fixture
def telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)


class TestRendering:
    def test_message_has_everything_needed_to_act(self) -> None:
        offer = ProductOffer(
            store="TerabyteShop",
            name="MSI RTX 5060 Ti 16GB Ventus 2X OC",
            price=Decimal("3199.99"),
            url="https://www.terabyteshop.com.br/produto/1/msi-rtx-5060-ti",
            available=True,
        )
        alert = Alert(
            offer=offer,
            product_name="RTX 5060 Ti 16GB",
            level=AlertLevel.EXCELLENT,
            max_price=Decimal("3300"),
        )
        text = render_alert(alert)
        assert "🔥 RTX 5060 Ti 16GB EXCELLENT!" in text
        assert "MSI RTX 5060 Ti 16GB Ventus 2X OC" in text
        assert "Store: TerabyteShop" in text
        assert "Price: R$ 3.199,99" in text
        assert "Target: R$ 3.300,00" in text
        assert "📉 R$ 100,01 below target" in text
        assert offer.url in text

    def test_levels_carry_their_own_badge(self) -> None:
        assert "🟢" in render_alert(make_alert(level=AlertLevel.GOOD))
        assert "🔥" in render_alert(make_alert(level=AlertLevel.EXCELLENT))
        assert "🚨" in render_alert(make_alert(level=AlertLevel.BUY_NOW))

    def test_exactly_at_target_reads_sensibly(self) -> None:
        assert "exactly at target" in render_alert(make_alert(price="3300", level=AlertLevel.GOOD))

    def test_no_coupon_line_when_no_coupon_is_verified(self) -> None:
        # deal-watcher never invents a discount, so an ordinary offer says nothing
        # about coupons at all.
        text = render_alert(make_alert())
        assert "Coupon" not in text
        assert "Cashback" not in text
        assert "Effective" not in text

    def test_verified_coupon_is_shown_broken_down(self) -> None:
        offer = ProductOffer(
            store="KaBuM!",
            name="RTX 5060 Ti 16GB",
            price=Decimal("3400"),
            url="https://example.test/1",
            available=True,
            coupon_code="VERIFIED10",
            coupon_discount=Decimal("200"),
            cashback=Decimal("50"),
        )
        text = render_alert(
            Alert(
                offer=offer,
                product_name="RTX 5060 Ti 16GB",
                level=AlertLevel.GOOD,
                max_price=Decimal("3300"),
            )
        )
        assert "Coupon: VERIFIED10 (-R$ 200,00)" in text
        assert "Cashback: -R$ 50,00" in text
        assert "Effective: R$ 3.150,00" in text


class TestTelegram:
    def test_refuses_to_start_without_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        with pytest.raises(NotifierError, match="TELEGRAM_BOT_TOKEN"):
            TelegramNotifier(TelegramConfig(), HttpConfig())

    @respx.mock
    def test_sends_the_rendered_alert(self, telegram_env: None) -> None:
        route = respx.post(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        notifier = TelegramNotifier(TelegramConfig(), HttpConfig())
        notifier.send(make_alert())
        notifier.close()

        assert route.called
        body = route.calls[0].request.content.decode()
        assert FAKE_CHAT_ID in body
        assert "RTX 5060 Ti 16GB" in body

    @respx.mock
    def test_api_errors_surface_without_leaking_the_token(self, telegram_env: None) -> None:
        respx.post(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(400, json={"description": "chat not found"})
        )
        notifier = TelegramNotifier(TelegramConfig(), HttpConfig())
        with pytest.raises(NotifierError) as exc_info:
            notifier.send_text("hello")
        notifier.close()

        message = str(exc_info.value)
        assert "400" in message and "chat not found" in message
        assert FAKE_TOKEN not in message

    @respx.mock
    def test_transport_errors_never_carry_the_url(self, telegram_env: None) -> None:
        # httpx puts the request URL -- and therefore the token -- into its own
        # exception messages. Ours must not repeat it.
        respx.post(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage").mock(
            side_effect=httpx.ConnectError("failed to connect")
        )
        notifier = TelegramNotifier(TelegramConfig(), HttpConfig())
        with pytest.raises(NotifierError) as exc_info:
            notifier.send_text("hello")
        notifier.close()
        assert FAKE_TOKEN not in str(exc_info.value)


class TestSecretsNeverReachTheLogs:
    def test_redacts_configured_env_values(self, telegram_env: None) -> None:
        assert FAKE_TOKEN not in redact(f"calling https://api.telegram.org/bot{FAKE_TOKEN}/x")
        assert FAKE_CHAT_ID not in redact(f"chat {FAKE_CHAT_ID}")

    def test_redacts_token_shaped_strings_even_if_not_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        other = "555555555:BBGsomeoneelsestokenvaluegoeshere123"
        assert other not in redact(f"leaked {other}")

    def test_filter_scrubs_log_records(
        self, telegram_env: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("test.redaction")
        logger.addFilter(RedactionFilter())
        with caplog.at_level(logging.INFO):
            logger.info("posting to bot%s/sendMessage", FAKE_TOKEN)
        assert FAKE_TOKEN not in caplog.text
        assert "REDACTED" in caplog.text

    def test_setup_logging_is_idempotent(self) -> None:
        setup_logging("INFO", "text")
        setup_logging("DEBUG", "json")
        assert len(logging.getLogger().handlers) == 1
