#!/bin/sh
# Installed by deploy/install-user.sh as ~/.local/bin/deal-watcher.
#
# Makes a hand-run command behave exactly like the user timer: same config,
# same browser path, same credentials. Without it, running the venv binary
# directly looks unconfigured for Telegram and cannot find Chromium.
set -e

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"

if [ -r "$HOME/.config/deal-watcher/deal-watcher.env" ]; then
    . "$HOME/.config/deal-watcher/deal-watcher.env"
    export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
fi

exec @APP_DIR@/.venv/bin/deal-watcher --config "$HOME/.config/deal-watcher/config.yaml" "$@"
