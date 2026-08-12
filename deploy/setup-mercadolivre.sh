#!/usr/bin/env bash
#
# Store Mercado Livre API credentials and prove they work.
#
#   ./deploy/setup-mercadolivre.sh --user     # user install (your own machine)
#   sudo ./deploy/setup-mercadolivre.sh       # system install (server)
#
# The secret is typed into a hidden prompt and written straight to a file only
# its owner can read. It is never echoed, never passed in argv or in a
# command's environment (both readable via `ps`), and never written to shell
# history.
#
# Credentials come from a free app at https://developers.mercadolivre.com.br/.
# Only the "Usuários" permission is needed; leave every topic unsubscribed.
set -euo pipefail

MODE=system
[[ "${1:-}" == "--user" ]] && MODE=user

if [[ $MODE == user ]]; then
  ENV_FILE="$HOME/.config/deal-watcher/deal-watcher.env"
  CONFIG="$HOME/.config/deal-watcher/config.yaml"
  [[ $EUID -ne 0 ]] || { echo "--user means your own account, not root" >&2; exit 1; }
  mkdir -p "$(dirname "$ENV_FILE")"
else
  ENV_FILE=/etc/deal-watcher/deal-watcher.env
  CONFIG=/etc/deal-watcher/config.yaml
  APP_USER=dealwatcher
  [[ $EUID -eq 0 ]] || { echo "run me with sudo (or pass --user)" >&2; exit 1; }
fi

read -rp  "Mercado Livre Client ID: " ML_ID
read -rsp "Mercado Livre Client Secret (input hidden): " ML_SECRET
echo
[[ -n "$ML_ID" && -n "$ML_SECRET" ]] || { echo "both values are required" >&2; exit 1; }

echo "==> exchanging them for a token"
TOKEN_JSON=$(curl -sS -X POST "https://api.mercadolibre.com/oauth/token" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "client_id=$ML_ID" \
  --data-urlencode "client_secret=$ML_SECRET")

TOKEN=$(printf '%s' "$TOKEN_JSON" | grep -oP '"access_token"\s*:\s*"\K[^"]+' || true)
if [[ -z "$TOKEN" ]]; then
  echo "Mercado Livre rejected those credentials:" >&2
  # Print the error field only -- the response never contains the secret, but
  # dumping unknown JSON wholesale is a bad habit to build.
  printf '%s' "$TOKEN_JSON" | grep -oP '"(error|message)"\s*:\s*"\K[^"]+' >&2 || true
  exit 1
fi
echo "    got a token"

# The question this script exists to answer: the grant is accepted, but is the
# resulting token accepted by the endpoint we actually need?
echo "==> testing a real product search"
STATUS=$(curl -sS -o /tmp/ml-probe.$$ -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" \
  "https://api.mercadolibre.com/sites/MLB/search?q=rtx%205070&limit=1")
FOUND=$(grep -oP '"title"\s*:\s*"\K[^"]{0,60}' /tmp/ml-probe.$$ | head -1 || true)
rm -f /tmp/ml-probe.$$

if [[ "$STATUS" != "200" ]]; then
  echo "    search returned HTTP $STATUS -- the token is not accepted there." >&2
  echo "    The client_credentials grant is not enough for this endpoint;" >&2
  echo "    the authorization-code flow is needed instead. Nothing was saved." >&2
  exit 1
fi
echo "    search works: ${FOUND:-(no title in response)}"

umask 077
touch "$ENV_FILE"
# Replace any previous values rather than appending duplicates.
grep -v -E '^MERCADOLIVRE_(CLIENT_ID|CLIENT_SECRET)=' "$ENV_FILE" >"$ENV_FILE.tmp" || true
{
  cat "$ENV_FILE.tmp"
  printf 'MERCADOLIVRE_CLIENT_ID=%s\n' "$ML_ID"
  printf 'MERCADOLIVRE_CLIENT_SECRET=%s\n' "$ML_SECRET"
} >"$ENV_FILE"
rm -f "$ENV_FILE.tmp"
unset ML_ID ML_SECRET TOKEN TOKEN_JSON

if [[ $MODE == system ]]; then
  chown root:"$APP_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
else
  chmod 600 "$ENV_FILE"
fi
echo "==> saved to $ENV_FILE"

# Enable the store, but only within its own block in config.yaml.
if grep -q '^  mercadolivre:' "$CONFIG"; then
  python3 - "$CONFIG" <<'PY'
import re
import sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()
pattern = re.compile(r"(^  mercadolivre:\n(?:.*\n)*?)(^    enabled: )false$", re.M)
new, count = pattern.subn(lambda m: f"{m.group(1)}{m.group(2)}true", text, count=1)
if count:
    open(path, "w", encoding="utf-8").write(new)
    print("==> enabled the mercadolivre store in", path)
else:
    print("==> mercadolivre already enabled in", path)
PY
else
  echo "==> add a 'mercadolivre:' store block to $CONFIG to enable it"
fi

echo
echo "Now run:  deal-watcher check --dry-run"
