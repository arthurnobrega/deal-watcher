#!/usr/bin/env bash
#
# Install deal-watcher into your own user session -- no root, no system files.
#
#   ./deploy/install-user.sh
#
# This exists for one specific reason: some stores (Pichau today) refuse
# datacenter IPs, so they can only be read from an ordinary home connection.
# The intended split is that this machine watches *only* the stores your server
# cannot reach. Disjoint store sets mean no offer is watched in two places, so
# the two installs can never alert you twice for the same deal.
#
# Everything lands under your home directory:
#   ~/.config/deal-watcher/config.yaml         what to watch
#   ~/.config/deal-watcher/deal-watcher.env    credentials, 0600
#   ~/.local/share/deal-watcher/               the database
#   ~/.config/systemd/user/                    the timer
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$HOME/.config/deal-watcher"
DATA_DIR="$HOME/.local/share/deal-watcher"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_FILE="$CONFIG_DIR/deal-watcher.env"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[[ $EUID -ne 0 ]] || { echo "run me as your normal user, not root" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd required" >&2; exit 1; }

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$UNIT_DIR"

log "building virtualenv in $REPO_DIR/.venv"
python3 -m venv "$REPO_DIR/.venv" 2>/dev/null || true
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR[browser]"

log "installing headless chromium"
"$REPO_DIR/.venv/bin/playwright" install chromium >/dev/null

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  log "writing $CONFIG_DIR/config.yaml (stores your server cannot reach)"
  # Start from the repo config, then flip it to the home-connection role:
  # Pichau on, the two stores a server handles fine off.
  python3 - "$REPO_DIR/config.yaml" "$CONFIG_DIR/config.yaml" "$DATA_DIR" <<'PY'
import re
import sys

source, target, data_dir = sys.argv[1:4]
text = open(source, encoding="utf-8").read()

def set_enabled(text: str, store: str, value: str) -> str:
    pattern = re.compile(rf"(^  {store}:\n(?:.*\n)*?)(^    enabled: )(true|false)$", re.M)
    return pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{value}", text, count=1)

text = set_enabled(text, "pichau", "true")
text = set_enabled(text, "kabum", "false")
text = set_enabled(text, "terabyte", "false")
text = re.sub(r"^database:.*$", f"database: {data_dir}/deal-watcher.db", text, count=1, flags=re.M)
text = (
    "# Home-connection half of a split install: this machine watches only the\n"
    "# stores a datacenter IP cannot reach. Keep the store sets disjoint from\n"
    "# the server's config, or one deal will alert you twice.\n" + text
)
open(target, "w", encoding="utf-8").write(text)
PY
else
  echo "    $CONFIG_DIR/config.yaml already exists, leaving it alone"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  log "creating an empty $ENV_FILE"
  install -m 600 /dev/null "$ENV_FILE"
  printf 'TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n' >"$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

log "installing user units"
sed "s#@APP_DIR@#$REPO_DIR#g" "$REPO_DIR/deploy/user/deal-watcher.service" \
  >"$UNIT_DIR/deal-watcher.service"
install -m 644 "$REPO_DIR/deploy/user/deal-watcher.timer" "$UNIT_DIR/deal-watcher.timer"

systemctl --user daemon-reload
systemctl --user enable --now deal-watcher.timer

# Without lingering, user timers stop when you log out.
if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
  log "enabling lingering so the timer survives logout (needs sudo once)"
  sudo loginctl enable-linger "$USER" || \
    echo "    could not enable lingering; the timer will only run while you are logged in"
fi

log "done"
cat <<EOF

Next:
  1. ./deploy/setup-telegram.sh --user      # credentials, hidden prompt
  2. systemctl --user start deal-watcher.service
  3. journalctl --user -u deal-watcher -f
  4. systemctl --user list-timers deal-watcher.timer

Check the store split before trusting it -- this machine and your server must
not both watch the same store:

  grep -A2 -E '^  (kabum|terabyte|pichau):' $CONFIG_DIR/config.yaml
EOF
