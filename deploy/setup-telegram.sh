#!/usr/bin/env bash
#
# Interactively write the credentials file for a system or user install.
#
#   sudo ./deploy/setup-telegram.sh          # system install (server)
#   ./deploy/setup-telegram.sh --user        # user install (your own machine)
#
# The token is typed into a hidden prompt and written straight to a file only
# its owner can read (root:dealwatcher 0640 for a system install, 0600 for a
# user one). It is never echoed, never passed as a command-line argument or in
# the environment of a command (both are readable through `ps`), and never
# written to shell history. The test message reads it back from that file.
#
# The chat id is discovered by asking Telegram which chats have messaged the
# bot, so you do not have to dig through a JSON blob yourself.
set -euo pipefail

MODE=system
[[ "${1:-}" == "--user" ]] && MODE=user

if [[ $MODE == user ]]; then
  ENV_FILE="$HOME/.config/deal-watcher/deal-watcher.env"
  BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv/bin/deal-watcher"
  CONFIG="$HOME/.config/deal-watcher/config.yaml"
  [[ $EUID -ne 0 ]] || { echo "--user means your own account, not root" >&2; exit 1; }
  mkdir -p "$(dirname "$ENV_FILE")"
else
  ENV_FILE=/etc/deal-watcher/deal-watcher.env
  APP_USER=dealwatcher
  BIN=/opt/deal-watcher/.venv/bin/deal-watcher
  CONFIG=/etc/deal-watcher/config.yaml
  [[ $EUID -eq 0 ]] || { echo "run me with sudo (or pass --user)" >&2; exit 1; }
fi

# Keep the token out of the terminal scrollback and out of `ps`.
read -rsp "Telegram bot token (input hidden): " TOKEN
echo
[[ -n "$TOKEN" ]] || { echo "no token given" >&2; exit 1; }

echo "==> checking the token with Telegram"
BOT_NAME=$(curl -sS "https://api.telegram.org/bot${TOKEN}/getMe" \
  | grep -oP '"username":"\K[^"]+' | head -1 || true)
if [[ -z "$BOT_NAME" ]]; then
  echo "Telegram rejected that token. Check it with @BotFather and try again." >&2
  exit 1
fi
echo "    bot is @${BOT_NAME}"

echo
echo "==> now send @${BOT_NAME} any message from Telegram (a bot cannot start"
echo "    the conversation), then press Enter here."
read -r _

CHAT_ID=$(curl -sS "https://api.telegram.org/bot${TOKEN}/getUpdates" \
  | grep -oP '"chat":\{"id":\K-?[0-9]+' | tail -1 || true)

if [[ -z "$CHAT_ID" ]]; then
  echo "No messages found. Send the bot a message, then re-run this script." >&2
  echo "(If the bot is in a group, send the message there.)" >&2
  exit 1
fi
echo "    chat id: ${CHAT_ID}"

umask 077
install -m 600 /dev/null "$ENV_FILE"
cat >"$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=${TOKEN}
TELEGRAM_CHAT_ID=${CHAT_ID}
EOF

if [[ $MODE == system ]]; then
  chown root:"$APP_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo "==> wrote $ENV_FILE (root:${APP_USER}, 0640)"
else
  chmod 600 "$ENV_FILE"
  echo "==> wrote $ENV_FILE ($USER, 0600)"
fi
unset TOKEN CHAT_ID

echo
echo "==> sending a test message"
# Read the credentials back from the file rather than handing them to a
# command: anything in argv or in a command's environment is readable by other
# users through `ps` and /proc.
if [[ $MODE == system ]]; then
  # The installed wrapper sources the env file itself.
  TEST_CMD=(sudo -u "$APP_USER" /usr/local/bin/deal-watcher test-notification)
else
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  TEST_CMD=("$BIN" --config "$CONFIG" test-notification)
fi

if "${TEST_CMD[@]}"; then
  echo
  echo "Check Telegram -- you should have a message from @${BOT_NAME}."
else
  echo "Test failed. See the output above." >&2
  exit 1
fi
