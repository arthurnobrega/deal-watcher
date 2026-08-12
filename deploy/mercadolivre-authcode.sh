#!/usr/bin/env bash
#
# One-shot probe: does a *user-context* token unlock Mercado Livre's catalogue
# search, when a client_credentials token does not?
#
#   ./deploy/mercadolivre-authcode.sh
#
# Background: with a client_credentials token, /sites/MLB/search returns 403
# "forbidden" -- authenticated, but the app is not authorised for catalogue
# search. This runs the authorization_code flow instead, which carries the
# logged-in user's context, and probes the same endpoint.
#
# It saves nothing unless the probe succeeds, so a failed experiment leaves no
# half-configured store behind.
set -euo pipefail

ENV_FILE="$HOME/.config/deal-watcher/deal-watcher.env"
CONFIG="$HOME/.config/deal-watcher/config.yaml"
AUTH_HOST="https://auth.mercadolivre.com.br"
API="https://api.mercadolibre.com"

read -rp  "Client ID: " ML_ID
read -rsp "Client Secret (input hidden): " ML_SECRET
echo
read -rp  "Redirect URI exactly as registered: " ML_REDIRECT
[[ -n "$ML_ID" && -n "$ML_SECRET" && -n "$ML_REDIRECT" ]] || { echo "all three are required" >&2; exit 1; }

ENCODED=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$ML_REDIRECT")

cat <<EOF

==> Open this in your browser and approve:

${AUTH_HOST}/authorization?response_type=code&client_id=${ML_ID}&redirect_uri=${ENCODED}

You will land on your redirect URI with ?code=TG-xxxxx in the address bar.
The page itself does not matter -- only the code in the URL.

EOF

read -rp "Paste the code (the TG-... value): " ML_CODE
[[ -n "$ML_CODE" ]] || { echo "no code given" >&2; exit 1; }

echo "==> exchanging the code for a user token"
RESP=$(curl -sS -X POST "$API/oauth/token" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "client_id=$ML_ID" \
  --data-urlencode "client_secret=$ML_SECRET" \
  --data-urlencode "code=$ML_CODE" \
  --data-urlencode "redirect_uri=$ML_REDIRECT")

TOKEN=$(printf '%s' "$RESP" | grep -oP '"access_token"\s*:\s*"\K[^"]+' || true)
REFRESH=$(printf '%s' "$RESP" | grep -oP '"refresh_token"\s*:\s*"\K[^"]+' || true)
if [[ -z "$TOKEN" ]]; then
  echo "exchange failed:" >&2
  printf '%s' "$RESP" | grep -oP '"(error|message|error_description)"\s*:\s*"\K[^"]+' >&2 || true
  echo "(codes are single-use and expire in ~10 minutes -- re-run to get a fresh one)" >&2
  exit 1
fi
echo "    got a user token${REFRESH:+ and a refresh token}"

echo "==> probing the endpoints that matter"
FAIL=0
for path in \
  "/sites/MLB/search?q=rtx%205070&limit=1" \
  "/products/search?status=active&site_id=MLB&q=rtx%205070" \
  "/users/me"
do
  STATUS=$(curl -sS -o /tmp/ml-auth.$$ -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "${API}${path}")
  DETAIL=$(grep -oP '"(message|error)"\s*:\s*"\K[^"]{0,70}' /tmp/ml-auth.$$ | head -1 || true)
  printf '    %-3s %s %s\n' "$STATUS" "${path%%\?*}" "${DETAIL:+-- $DETAIL}"
  [[ "$path" == "/sites/MLB/search"* && "$STATUS" != "200" ]] && FAIL=1
  rm -f /tmp/ml-auth.$$
done

if [[ $FAIL -eq 1 ]]; then
  cat >&2 <<'EOF'

==> Catalogue search is closed to this app even with a user token.
    That is an authorisation Mercado Livre grants per application, not
    something another OAuth flow gets around. Nothing was saved.
EOF
  exit 1
fi

umask 077
touch "$ENV_FILE"
grep -v -E '^MERCADOLIVRE_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN)=' "$ENV_FILE" >"$ENV_FILE.tmp" || true
{
  cat "$ENV_FILE.tmp"
  printf 'MERCADOLIVRE_CLIENT_ID=%s\n' "$ML_ID"
  printf 'MERCADOLIVRE_CLIENT_SECRET=%s\n' "$ML_SECRET"
  [[ -n "$REFRESH" ]] && printf 'MERCADOLIVRE_REFRESH_TOKEN=%s\n' "$REFRESH"
} >"$ENV_FILE"
rm -f "$ENV_FILE.tmp"
chmod 600 "$ENV_FILE"
unset ML_ID ML_SECRET ML_CODE TOKEN REFRESH RESP

echo "==> search works. Saved credentials to $ENV_FILE"
echo "    Tell me, and I will switch the adapter to the refresh-token flow."
