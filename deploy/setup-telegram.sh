#!/usr/bin/env bash
#
# Interactively write /etc/deal-watcher/deal-watcher.env.
#
#   sudo ./deploy/setup-telegram.sh
#
# The token is typed into a hidden prompt and written straight to a 0640 file
# owned by root:dealwatcher. It is never echoed, never passed as a command-line
# argument (which would show up in `ps`), and never written to shell history.
#
# The chat id is discovered by asking Telegram which chats have messaged the
# bot, so you do not have to dig through a JSON blob yourself.
set -euo pipefail

ENV_FILE=/etc/deal-watcher/deal-watcher.env
APP_USER=dealwatcher

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

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

install -o root -g "$APP_USER" -m 640 /dev/null "$ENV_FILE"
umask 077
cat >"$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=${TOKEN}
TELEGRAM_CHAT_ID=${CHAT_ID}
EOF
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"
unset TOKEN

echo "==> wrote $ENV_FILE (root:${APP_USER}, 0640)"
echo
echo "==> sending a test message"
if sudo -u "$APP_USER" env $(grep -v '^#' "$ENV_FILE" | xargs) \
     /opt/deal-watcher/.venv/bin/deal-watcher \
     --config /etc/deal-watcher/config.yaml test-notification; then
  echo
  echo "Check Telegram -- you should have a message from @${BOT_NAME}."
else
  echo "Test failed. See the output above." >&2
  exit 1
fi
