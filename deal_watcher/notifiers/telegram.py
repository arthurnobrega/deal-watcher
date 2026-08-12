"""Telegram delivery.

The bot token and chat id are read from environment variables named in config;
their *values* never appear in config, in logs, or in an exception message. The
API URL contains the token, so error reporting here is deliberately built from
the status code and never from the request URL.
"""

from __future__ import annotations

import logging
import os

import httpx

from ..config import HttpConfig, TelegramConfig
from ..models import Alert
from .base import Notifier, NotifierError, render_alert

log = logging.getLogger(__name__)


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, config: TelegramConfig, http: HttpConfig) -> None:
        self._config = config
        self._token = os.environ.get(config.token_env, "").strip()
        self._chat_id = os.environ.get(config.chat_id_env, "").strip()
        missing = [
            env_var
            for env_var, value in (
                (config.token_env, self._token),
                (config.chat_id_env, self._chat_id),
            )
            if not value
        ]
        if missing:
            raise NotifierError(f"missing environment variables: {', '.join(missing)}")
        self._client = httpx.Client(timeout=http.timeout_seconds)

    def send(self, alert: Alert) -> None:
        self.send_text(render_alert(alert))

    def send_text(self, text: str, monospace: bool = False) -> None:
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "disable_web_page_preview": False,
        }
        if monospace:
            # <pre> keeps column alignment on a phone, where a proportional
            # font would turn a table into noise.
            escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            payload["text"] = f"<pre>{escaped}</pre>"
            payload["parse_mode"] = "HTML"
        else:
            payload["text"] = text

        try:
            response = self._client.post(
                f"{self._config.api_base}/bot{self._token}/sendMessage",
                json=payload,
            )
        except httpx.HTTPError as exc:
            # str(exc) can include the request URL, which carries the token.
            raise NotifierError(f"telegram request failed: {type(exc).__name__}") from None

        if response.status_code != 200:
            description = ""
            try:
                payload = response.json()
                description = str(payload.get("description", ""))
            except ValueError:
                description = ""
            raise NotifierError(
                f"telegram API returned HTTP {response.status_code}"
                + (f": {description}" if description else "")
            )

    def close(self) -> None:
        self._client.close()
