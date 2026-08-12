#!/usr/bin/env bash
#
# One-shot probe: does a *user-context* token unlock Mercado Livre's catalogue
# search, when a client_credentials token does not?
#
#   ./deploy/mercadolivre-authcode.sh
#
# Background, measured rather than assumed:
#
#   /sites/MLB/search   403 with both a client_credentials and a user token
#                       -- app-level authorisation, not a token problem
#   /products/search    200 with a user token   <- the way in
#   /users/me           200 with a user token
#
# So deal-watcher reads the *catalogue* endpoint, not site search. This script
# runs the authorization_code flow, saves the refresh token (access tokens
# last ~6h, refresh tokens ~6 months, so the adapter renews itself), and dumps
# a sample response so the adapter can be written against real data.
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

SAMPLE="$HOME/.cache/deal-watcher-ml-probe.json"
mkdir -p "$(dirname "$SAMPLE")"

echo "==> probing the catalogue endpoint"
STATUS=$(curl -sS -o "$SAMPLE" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
  "${API}/products/search?status=active&site_id=MLB&q=rtx%205070")
echo "    $STATUS /products/search"

if [[ "$STATUS" != "200" ]]; then
  echo "    catalogue search is closed to this app too. Nothing saved." >&2
  grep -oP '"(message|error)"\s*:\s*"\K[^"]{0,90}' "$SAMPLE" >&2 || true
  rm -f "$SAMPLE"
  exit 1
fi

# A catalogue product is not an offer: the price lives on the product detail,
# in its buy-box winner. Capture one so the adapter is written against fact.
PRODUCT_ID=$(grep -oP '"id"\s*:\s*"\KMLB\d+' "$SAMPLE" | head -1 || true)
if [[ -n "$PRODUCT_ID" ]]; then
  echo "==> fetching product detail for $PRODUCT_ID"
  curl -sS -o "${SAMPLE%.json}-detail.json" -H "Authorization: Bearer $TOKEN" \
    "${API}/products/${PRODUCT_ID}"
  echo "    saved ${SAMPLE%.json}-detail.json"
fi
chmod 600 "$SAMPLE" "${SAMPLE%.json}-detail.json" 2>/dev/null || true
echo "==> sample responses saved for adapter work (no credentials in them)"

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

echo "==> saved credentials to $ENV_FILE (0600)"
echo
echo "Catalogue search works. Tell me and I will point the adapter at"
echo "/products/search and wire up the refresh-token flow."
