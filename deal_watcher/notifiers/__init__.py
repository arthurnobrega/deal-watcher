"""Notification channels.

Telegram ships today. Discord and email are a class each, implementing
:class:`~deal_watcher.notifiers.base.Notifier`, with no change to the monitor.
"""

from .base import Notifier, NotifierError, render_alert
from .telegram import TelegramNotifier

__all__ = ["Notifier", "NotifierError", "TelegramNotifier", "render_alert"]
