#!/usr/bin/env bash
#
# Install or update deal-watcher on a systemd host. Idempotent: safe to re-run.
#
#   sudo ./deploy/install.sh
#
# What it does:
#   * creates a dedicated unprivileged user (no shell, no home login)
#   * installs the app into /opt/deal-watcher inside its own virtualenv
#   * puts config in /etc/deal-watcher and data in /var/lib/deal-watcher
#   * installs a systemd service + timer that runs one check every 15 minutes
#
# What it deliberately does NOT do:
#   * touch SSH, the firewall, or any port (the monitor only dials out)
#   * write any secret. You create /etc/deal-watcher/deal-watcher.env yourself.
set -euo pipefail

APP_USER=dealwatcher
APP_DIR=/opt/deal-watcher
CONFIG_DIR=/etc/deal-watcher
DATA_DIR=/var/lib/deal-watcher
ENV_FILE="$CONFIG_DIR/deal-watcher.env"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd required" >&2; exit 1; }

log "creating service user $APP_USER"
id -u "$APP_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

log "creating directories"
install -d -o root -g root -m 755 "$CONFIG_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$DATA_DIR"
install -d -o root -g root -m 755 "$APP_DIR"

log "syncing application code to $APP_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data' \
  --exclude '__pycache__' --exclude '.env' \
  "$REPO_DIR"/ "$APP_DIR"/

log "building virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"

# Playwright is only needed by stores configured with `fetcher: browser`.
if grep -qE '^\s*fetcher:\s*browser' "$CONFIG_DIR/config.yaml" 2>/dev/null || \
   grep -qE '^\s*fetcher:\s*browser' "$APP_DIR/config.yaml"; then
  log "installing headless chromium for the browser fetcher"
  "$APP_DIR/.venv/bin/pip" install --quiet playwright
  PLAYWRIGHT_BROWSERS_PATH=/opt/deal-watcher/browsers \
    "$APP_DIR/.venv/bin/playwright" install --with-deps chromium
  chmod -R a+rX /opt/deal-watcher/browsers
fi

log "installing config"
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  install -o root -g "$APP_USER" -m 640 "$APP_DIR/config.yaml" "$CONFIG_DIR/config.yaml"
  # The repo default is a relative path for local runs; production needs the
  # state directory the service unit is allowed to write to.
  sed -i "s#^database:.*#database: $DATA_DIR/deal-watcher.db#" "$CONFIG_DIR/config.yaml"
else
  echo "    $CONFIG_DIR/config.yaml already exists, leaving it alone"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  log "creating an empty $ENV_FILE -- fill it in before starting"
  install -o root -g "$APP_USER" -m 640 /dev/null "$ENV_FILE"
  cat >"$ENV_FILE" <<'EOF'
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
  chmod 640 "$ENV_FILE"
  chown root:"$APP_USER" "$ENV_FILE"
fi

log "installing systemd units"
install -m 644 "$APP_DIR/deploy/deal-watcher.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/deal-watcher.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now deal-watcher.timer

# The database is created on first run; make sure ownership is right either way.
chown -R "$APP_USER":"$APP_USER" "$DATA_DIR"

log "done"
cat <<EOF

Next:
  1. put your credentials in $ENV_FILE  (chmod 640, root:$APP_USER)
  2. sudo -u $APP_USER $APP_DIR/.venv/bin/deal-watcher --config $CONFIG_DIR/config.yaml test-notification
  3. systemctl start deal-watcher.service     # run one cycle now
  4. journalctl -u deal-watcher -f            # watch it work
  5. systemctl list-timers deal-watcher.timer # confirm the schedule
EOF
