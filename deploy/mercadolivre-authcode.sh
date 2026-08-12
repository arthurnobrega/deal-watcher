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

${AUTH_HOST}/authorization?response_type=code&client_id=${ML_ID}&redirect_uri=${ENCODED}&scope=offline_access+read

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
if [[ -z "$REFRESH" ]]; then
  cat >&2 <<'EOF'
    Got an access token but NO refresh token, so it would expire in ~6 hours
    and leave the store dead until you re-authorised by hand.

    Mercado Livre only issues one when the authorization request asks for
    offline_access -- which this script now does. If you are seeing this, the
    app itself is not permitted to use it: check the application settings for
    an offline access option and re-run. Nothing was saved.
EOF
  exit 1
fi
echo "    got a user token and a refresh token"

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
# in its buy-box winner. The first search hit is often a seller-invented
# "COMMUNITY" entry with no offers at all, so capture several real ones.
echo "==> capturing product details for adapter work"
python3 - "$SAMPLE" "$TOKEN" "$API" <<'PY'
import json, sys, urllib.request

sample_path, token, api = sys.argv[1:4]
results = json.load(open(sample_path)).get("results", [])
out = []
for item in results[:6]:
    url = f"{api}/products/{item['id']}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            out.append(json.load(response))
    except Exception as exc:  # noqa: BLE001 - a probe, not production
        out.append({"id": item["id"], "error": str(exc)})
path = sample_path.replace(".json", "-detail.json")
json.dump(out, open(path, "w"), ensure_ascii=False)
priced = sum(1 for d in out if isinstance(d.get("buy_box_winner"), dict))
print(f"    {len(out)} products captured, {priced} with a buy-box price")
PY
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
