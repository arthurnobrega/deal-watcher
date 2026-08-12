#!/bin/sh
# Installed as /usr/local/bin/deal-watcher.
#
# Makes a hand-run command behave exactly like the systemd unit. Without this,
# troubleshooting by hand silently differs from production: the unit exports a
# browser path and the credentials file, an interactive shell does not, so
# Chromium "disappears" and Telegram looks unconfigured.
set -e

export PLAYWRIGHT_BROWSERS_PATH=/opt/deal-watcher/browsers
export HOME="${HOME:-/var/lib/deal-watcher}"

# Credentials are only readable by root and the service user; anyone else just
# gets a command that cannot notify, which is the correct outcome.
if [ -r /etc/deal-watcher/deal-watcher.env ]; then
    . /etc/deal-watcher/deal-watcher.env
    export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
fi

exec /opt/deal-watcher/.venv/bin/deal-watcher --config /etc/deal-watcher/config.yaml "$@"
