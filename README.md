# deal-watcher

Watch hardware prices in Brazilian stores and get a Telegram message when a real
deal shows up — not when a lookalike product happens to be cheap.

It was built to answer one question ("is an RTX 5060 Ti **16GB** under R$ 3.300
anywhere?") without becoming a throwaway script for that one card. Products,
prices, alert levels, stores and matching rules are all configuration.

```text
🔥 RTX 5060 Ti 16GB EXCELLENT!

MSI RTX 5060 Ti 16GB Ventus 2X OC
Store: TerabyteShop

Price: R$ 3.199,99
Target: R$ 3.300,00

📉 R$ 100,01 below target

🛒 Buy:
https://www.terabyteshop.com.br/produto/...
```

[![CI](https://github.com/arthurnobrega/deal-watcher/actions/workflows/ci.yml/badge.svg)](https://github.com/arthurnobrega/deal-watcher/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## Contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Telegram setup](#telegram-setup)
- [CLI](#cli)
- [How product matching works](#how-product-matching-works)
- [Alert levels and anti-spam](#alert-levels-and-anti-spam)
- [Coupons and cashback](#coupons-and-cashback)
- [Adding a store](#adding-a-store)
- [Adding a product](#adding-a-product)
- [Tests](#tests)
- [Deploying to your own VPS](#deploying-to-your-own-vps)
- [Environment variables](#environment-variables)
- [Observability](#observability)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Being a good citizen](#being-a-good-citizen)
- [License](#license)

---

## Architecture

One process, one SQLite file, no daemon to babysit. A cycle is a straight line:

```text
                 ┌──────────────┐
   config.yaml ─▶│   Monitor    │◀── SQLite (history + notification state)
                 └──────┬───────┘
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   StoreAdapter    matching.py      Notifier
   ├─ KaBuM!       accept/reject    └─ Telegram
   ├─ TerabyteShop      │              (Discord, email: same interface)
   └─ Pichau            ▼
        │          alerts.py
        ▼          level + dedup
   Fetcher              │
   ├─ HttpFetcher       ▼
   └─ BrowserFetcher   send
```

Each box has exactly one job:

| Module | Responsibility |
| --- | --- |
| `models.py` | The vocabulary: `ProductOffer`, `Alert`, `AlertLevel`, `StoreResult` |
| `config.py` | Load and validate `config.yaml`. Unknown keys are errors, not typos you never notice |
| `fetchers.py` | How pages are retrieved (plain HTTP, or headless Chromium when a site truly needs it) |
| `stores/` | One adapter per store. Turns a page into offers. Knows nothing about prices or targets |
| `matching.py` | Is this offer actually the product I want? Fails closed |
| `alerts.py` | How good is this price, and have I already said so? |
| `notifiers/` | Delivery. One class per channel |
| `storage.py` | Price history, notification state, run health |
| `monitor.py` | The cycle that ties it together |
| `cli.py` | The operator's interface |

Two rules keep it that way:

1. **Store logic never touches business logic.** An adapter cannot know what a
   "good price" is; the monitor cannot know what a `<div>` is.
2. **A store failure is data, not an exception.** Adapters return a
   `StoreResult` with an `error` field. One store returning HTTP 403 leaves the
   others working.

---

## Quick start

Requires Python 3.12+.

```bash
git clone https://github.com/arthurnobrega/deal-watcher.git
cd deal-watcher

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,browser]'

# Chromium, needed only by stores configured with `fetcher: browser`
playwright install chromium

cp .env.example .env      # then fill it in; .env is git-ignored
set -a && source .env && set +a

deal-watcher check --dry-run   # find deals, send nothing
```

A dry run prints what it found and what it *would* have sent:

```text
2026-08-12 13:57:24 INFO  deal_watcher.monitor: Starting price check
2026-08-12 13:57:25 INFO  deal_watcher.monitor: KaBuM!: 60 offers found, 6 match 'RTX 5060 Ti 16GB'
2026-08-12 13:57:33 INFO  deal_watcher.monitor: TerabyteShop: 88 offers found, 5 match 'RTX 5060 Ti 16GB'
2026-08-12 13:58:49 INFO  deal_watcher.monitor: Pichau: 10 offers found, 1 match 'RTX 5060 Ti 16GB'
2026-08-12 13:58:49 INFO  deal_watcher.monitor: Best price: R$ 3.799,99 (TerabyteShop)
2026-08-12 13:58:49 INFO  deal_watcher.monitor: Cycle done in 85.4s: 3 stores ok, 0 failed, 158 offers, 12 matches, 0 alerts
```

---

## Configuration

Everything lives in `config.yaml`. **No secrets go in it** — it names the
environment variables holding your Telegram credentials, never the values.

```yaml
interval_seconds: 900          # 15 minutes
database: data/deal-watcher.db
log_level: INFO
log_format: text               # text | json

http:
  timeout_seconds: 20
  retries: 2
  delay_between_requests: 1.5

browser:
  enabled: true                # needed by stores with `fetcher: browser`

stores:
  kabum:
    enabled: true
    fetcher: http
  terabyte:
    enabled: true
    fetcher: browser
  pichau:
    enabled: true
    fetcher: browser
    max_results: 12            # each candidate costs one page load

notifiers:
  telegram:
    enabled: true
    token_env: TELEGRAM_BOT_TOKEN
    chat_id_env: TELEGRAM_CHAT_ID

products:
  - name: "RTX 5060 Ti 16GB"
    max_price: 3300
    alert_levels:
      good: 3300
      excellent: 3200
      buy_now: 3000
    queries:
      kabum: "rtx 5060 ti"
      terabyte: "rtx 5060 ti"
      pichau: "rtx-5060-ti-16gb"
    match:
      require_available: true
      required_memory_gb: 16
      forbidden_memory_gb: [8]
      require_all:
        - "rtx\\s*5060\\s*ti"
      reject_any:
        - "usado|semi ?novo|open ?box|recondicionado"
        - "pc gamer|computador|notebook"
```

The config path is resolved in this order: `--config`, `$DEAL_WATCHER_CONFIG`,
`./config.yaml`, `/etc/deal-watcher/config.yaml`.

---

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the
   prompts. You get a token like `123456789:AA...`.
2. Send your new bot any message (a bot cannot start a conversation with you).
3. Fetch your chat id:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
   ```
4. Put both in `.env` (local) or `/etc/deal-watcher/deal-watcher.env` (server):
   ```bash
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
5. Prove it works:
   ```bash
   deal-watcher test-notification
   ```

Neither value is ever written to config, to logs, or to an exception message —
see [Environment variables](#environment-variables).

---

## CLI

```bash
deal-watcher check                 # one cycle, send alerts
deal-watcher check --dry-run       # one cycle, send nothing, remember nothing
deal-watcher run                   # loop forever (use only without a timer)
deal-watcher test-notification     # prove the notification wiring
deal-watcher history --limit 20    # recent prices, plus the lowest ever seen
deal-watcher health                # exit 0 healthy, 2 stale, 1 misconfigured
deal-watcher stores                # which adapters exist and what they need
```

`health` is a command, not an HTTP endpoint, on purpose: nothing has to listen
on a port, so there is nothing to expose or firewall.

---

## How product matching works

This is the part that stops you buying the wrong card. It is deliberately
pessimistic: **an ambiguous offer is rejected.** A missed deal costs nothing; a
wrong alert costs an afternoon and a restocking fee.

An offer must clear every gate:

1. **In stock** — `require_available`.
2. **No rejection pattern matches** — used, open box, complete PCs, notebooks,
   accessories, adjacent GPU models.
3. **Every `require_all` pattern matches** — e.g. `rtx\s*5060\s*ti`, which also
   catches `RTX5060Ti` written without spaces.
4. **Memory is right** — `required_memory_gb: 16` must appear, and every size in
   `forbidden_memory_gb` must not.

Names are lowercased and stripped of accents first, so `Placa de Vídeo` and
`Placa de Video` are the same string to a pattern.

The memory check is its own piece of logic because the 8GB and 16GB cards share
a name. It collects *every* gigabyte figure in the title —
`16GB`, `16 GB`, `O16G` (ASUS model codes), `8G` — and requires 16 present and 8
absent. Numbers that only look like memory (`2602MHz`, `128 bits`, `PCIe 5.0 x8`)
are not counted.

Worked examples, all from real listings:

| Listing | Verdict |
| --- | --- |
| `ASUS DUAL RTX 5060 TI O16G, 16GB GDDR7, 2602MHz, 128 bits` | ✅ match |
| `Palit GeForce RTX5060Ti INFINITY 3, 16GB GDDR7` | ✅ match |
| `RTX 5060 Ti GAMING OC 8G Gigabyte, 8GB GDDR7` | ❌ 8GB variant |
| `MSI GeForce RTX 5060 Shadow 2X OC, 8GB` | ❌ plain 5060 |
| `Galax RTX 5060 1-Click OC, 16GB` | ❌ plain 5060, right memory |
| `Colorful iGame RTX 5060 Ti Ultra W DUO OC` | ❌ memory not stated → not confident |
| `PC Gamer Ryzen 7 com RTX 5060 Ti 16GB` | ❌ complete PC |
| `Suporte Anti Sag para Placa de Vídeo RTX 5060 Ti 16GB` | ❌ accessory |
| `ASUS RTX 5060 Ti 16GB - USADO` | ❌ used |

Every rejection is logged with its reason at `DEBUG`, so a missed deal is always
explainable:

```text
DEBUG KaBuM!: rejected 'Placa de Vídeo RTX 5060 Ti GAMING OC 8G...' -- mentions forbidden 8GB memory
```

A live cycle over 158 offers from three stores produced 12 matches and zero
false positives.

---

## Alert levels and anti-spam

Three configurable ceilings, best match wins:

| Level | Default | Badge |
| --- | --- | --- |
| `good` | ≤ R$ 3.300 (defaults to `max_price`) | 🟢 GOOD DEAL |
| `excellent` | ≤ R$ 3.200 | 🔥 EXCELLENT |
| `buy_now` | ≤ R$ 3.000 | 🚨 BUY NOW |

An offer sitting at R$ 3.199 for six hours produces **one** message, not 24. A
new message is sent only when:

- it is the first time this offer went under target, **or**
- the price left the target range and came back, **or**
- it improved to a strictly better level (`excellent` → `buy_now`), **or**
- it dropped at least another 1% within the same level.

State is keyed on store + the store's own product id, so a store renaming a
listing does not reset its history and re-alert.

If delivery fails, nothing is marked as sent — the alert is retried next cycle
rather than silently lost.

---

## Coupons and cashback

**deal-watcher never invents a coupon.** There is no coupon scraping, no
"try this code", no guessed cashback.

The offer model already distinguishes listed price, coupon discount, cashback
and effective price, and alerts are evaluated against the effective price. Today
nothing populates those fields, so effective price always equals listed price
and no notification mentions a discount. If a verified source is added later,
the message breaks it down explicitly:

```text
Price: R$ 3.400,00
Coupon: VERIFIED10 (-R$ 200,00)
Cashback: -R$ 50,00
Effective: R$ 3.150,00
```

---

## Adding a store

One file, one import, one config block.

```python
# deal_watcher/stores/mystore.py
from ..models import ProductOffer
from ..prices import parse_brl
from .base import ParseError, StoreAdapter, register


@register
class MyStoreAdapter(StoreAdapter):
    slug = "mystore"
    display_name = "My Store"
    notes = "Readable over plain HTTP."

    def search_url(self, query: str) -> str:
        return f"https://mystore.com.br/busca/{self.slugify(query)}"

    def parse(self, page: str) -> tuple[ProductOffer, ...]:
        ...  # raise ParseError if the page cannot be understood
        return (
            ProductOffer(
                store=self.display_name,
                name=name,
                price=parse_brl(price_text),
                url=url,
                available=True,
                raw_id=product_id,
            ),
        )
```

Then import it in `deal_watcher/stores/__init__.py` and add it to `config.yaml`:

```yaml
stores:
  mystore:
    enabled: true
    fetcher: http     # or `browser`
```

Rules of the road for a new adapter:

- Read the store's `robots.txt` first and stay inside it.
- Return the price a buyer actually pays, and say so in `notes` if it is a
  payment-method price (Pix, boleto).
- Never filter by product name — that is `matching.py`'s job. Return what the
  page offered.
- Raise `ParseError` when a page makes no sense. Never return a half-parsed
  offer with a zero price.
- Capture a fixture into `tests/fixtures/` and add tests. Adapters must be
  testable with no network.

Stores that need more than one request (a listing plus product pages) can
override `collect()` — see `stores/pichau.py`.

---

## Adding a product

Append to `products:` in `config.yaml`. Nothing else changes.

```yaml
  - name: "Ryzen 7 9800X3D"
    max_price: 2800
    alert_levels:
      excellent: 2600
      buy_now: 2400
    queries:
      kabum: "ryzen 7 9800x3d"
      terabyte: "ryzen 7 9800x3d"
      pichau: "ryzen-7-9800x3d"
    match:
      require_all:
        - "ryzen\\s*7"
        - "9800\\s*x3d"
      reject_any:
        - "usado|open ?box"
        - "pc gamer|computador|notebook|kit upgrade"
```

`required_memory_gb` / `forbidden_memory_gb` are GPU-and-RAM specific; leave them
out for a CPU. Add `stores: [kabum, terabyte]` to limit a product to some stores.

Test your rules before trusting them:

```bash
deal-watcher check --dry-run --log-level DEBUG
```

---

## Tests

```bash
pytest                                        # 165 tests, ~0.3s
pytest --cov=deal_watcher --cov-report=term-missing
ruff check . && ruff format --check .
mypy
```

Store adapters run against HTML captured from the real sites and committed to
`tests/fixtures/`. **A store being offline, slow, or serving a bot challenge can
never fail the test suite**, and CI never makes a network request — Telegram is
mocked with `respx`.

Covered: price parsing (including the Brazilian `R$ 3.199,99` trap), name
normalisation, 16GB detection, 8GB rejection, accessory/used/PC rejection,
threshold boundaries, alert levels, deduplication across simulated cycles,
SQLite persistence and exact decimal round-tripping, store parsers, error
isolation when a store fails, notifier rendering, and that no secret can reach a
log line.

---

## Deploying to your own VPS

A small VPS is plenty — one vCPU and 1 GB RAM runs this comfortably. The
installer uses systemd, which is simpler than Docker here: no daemon, no image
registry, and the timer already handles scheduling and reboots.

```bash
git clone https://github.com/arthurnobrega/deal-watcher.git
cd deal-watcher
sudo ./deploy/install.sh
```

The script is idempotent and, by design, touches nothing else on the box. It:

- creates the unprivileged `dealwatcher` user (no shell, no home login)
- installs the app into `/opt/deal-watcher` in its own virtualenv
- puts config in `/etc/deal-watcher`, data in `/var/lib/deal-watcher`
- installs headless Chromium only if some store is configured for `browser`
- installs and enables `deal-watcher.timer` (every 15 minutes, `Persistent=true`
  so a missed run after downtime is caught up)

It does **not** modify SSH, the firewall, or any port. The monitor only makes
outbound connections — to the stores and to Telegram.

Then:

```bash
sudoedit /etc/deal-watcher/deal-watcher.env      # add your two Telegram values
sudo -u dealwatcher /opt/deal-watcher/.venv/bin/deal-watcher \
     --config /etc/deal-watcher/config.yaml test-notification
sudo systemctl start deal-watcher.service        # run one cycle now
journalctl -u deal-watcher -f                    # watch it
systemctl list-timers deal-watcher.timer         # confirm the schedule
```

The service unit is hardened: `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`, a restricted syscall and address-family set, and
exactly one writable path (`/var/lib/deal-watcher`).

To update:

```bash
git pull && sudo ./deploy/install.sh    # config and secrets are left untouched
```

To remove:

```bash
sudo systemctl disable --now deal-watcher.timer
sudo rm /etc/systemd/system/deal-watcher.{service,timer}
sudo systemctl daemon-reload
sudo rm -rf /opt/deal-watcher /etc/deal-watcher /var/lib/deal-watcher
sudo userdel dealwatcher
```

---

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes, if Telegram is enabled | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | yes, if Telegram is enabled | Where to send messages |
| `DEAL_WATCHER_CONFIG` | no | Config path, if not `./config.yaml` |
| `PLAYWRIGHT_BROWSERS_PATH` | no | Where Chromium lives (set by the installer) |

How secrets are kept out of everything:

- `.gitignore` covers `.env`, `*.pem`, `*.key`, and SSH key names.
- `.env.example` holds empty placeholders only.
- Config files name env *variables*, never values.
- CI has a job that fails the build if anything token-shaped is committed.
- A logging filter redacts known secret values **and** anything token-shaped, so
  even an unexpected exception string cannot leak one.
- The Telegram API URL contains the token, so transport errors are reported by
  exception type and status code, never by echoing httpx's message.
- On the server, `/etc/deal-watcher/deal-watcher.env` is `root:dealwatcher 0640`.

---

## Observability

Consistent, greppable logs on stdout, captured by journald in production:

```text
2026-08-12 13:57:24 INFO  deal_watcher.monitor: Starting price check
2026-08-12 13:57:25 INFO  deal_watcher.monitor: KaBuM!: 60 offers found, 6 match 'RTX 5060 Ti 16GB'
2026-08-12 13:57:33 INFO  deal_watcher.monitor: TerabyteShop: 88 offers found, 5 match 'RTX 5060 Ti 16GB'
2026-08-12 13:57:34 WARNING deal_watcher.monitor: Pichau: ❌ HTTP 403
2026-08-12 13:58:49 INFO  deal_watcher.monitor: Best price: R$ 3.799,99 (TerabyteShop)
2026-08-12 13:58:49 INFO  deal_watcher.monitor: Alert sent: RTX 5060 Ti 16GB at R$ 3.199,99 from KaBuM! (first time below target)
```

Set `log_format: json` for one JSON object per line if you ship logs somewhere.

Every cycle writes a row to the `runs` table — stores ok/failed, offers, matches,
alerts — which is what `deal-watcher health` reads.

---

## Troubleshooting

**`deal-watcher health` says UNHEALTHY**
No cycle finished within `health_max_age_seconds`. Check
`systemctl list-timers deal-watcher.timer` and `journalctl -u deal-watcher -n 50`.

**A store reports `HTTP 403`**
Expected occasionally: Pichau and TerabyteShop sit behind a JavaScript
interstitial. Confirm `browser.enabled: true` and that Chromium is installed
(`playwright install chromium`). One failing store never stops the others.

**A store reports `no product cards found` or `no __NEXT_DATA__ payload`**
The page was fetched but not understood — usually a layout change, sometimes a
challenge page. Refresh the fixture in `tests/fixtures/` and fix the parser; the
tests will show you exactly what moved.

**No alerts, but prices look right**
Check the target. `deal-watcher history` shows the lowest price ever recorded;
if that is above `max_price`, the monitor is working and the market simply has
not reached your number.

**A deal was found but no message arrived**
`deal-watcher test-notification` first. If that works, the alert was probably
deduplicated — the logs say so explicitly (`not notifying (already notified at
this level)`).

**`missing environment variables: TELEGRAM_BOT_TOKEN`**
The service could not read the env file. Check
`/etc/deal-watcher/deal-watcher.env` exists, is `0640`, and is owned by
`root:dealwatcher`.

**Chromium fails to start on a small VPS**
It needs a few hundred MB while a cycle runs. Add swap, or set the heavier
stores to `enabled: false` and keep KaBuM, which needs no browser.

---

## Known limitations

- **Two of three stores need a headless browser.** Pichau and TerabyteShop serve
  a JavaScript interstitial to plain HTTP clients. Chromium costs RAM and makes a
  cycle take ~90s instead of ~2s. KaBuM works over plain HTTP.
- **Pichau is disabled by default, because it blocks datacenter IPs.** Its
  Cloudflare rules never clear the interstitial for a VPS or a CI runner: the
  same cycle that returns an offer from a residential connection returns
  nothing from either, while spending ~75s on 12 browser page loads. Enable it
  in `config.yaml` if you run from an ordinary home connection. Measured on the
  same commit, minutes apart:

  | Store | Residential | VPS (Hostinger) | GitHub Actions |
  | --- | --- | --- | --- |
  | KaBuM! | 6 matches | 6 matches | 6 matches |
  | TerabyteShop | 5 matches | 5 matches | 0 (page renders partially) |
  | Pichau | 1 match | blocked | blocked |

- **Pichau is priced one product page at a time.** Its search endpoint is
  disallowed by `robots.txt` and its category pages render lazily, so candidates
  come from the sitemap and each is fetched individually (capped by
  `max_results`, default 12). Use a specific `queries.pichau` value to keep that
  number small.
- **TerabyteShop prices are the Pix / à vista price**, which is what the card
  shows. A credit-card total will be higher.
- **Scrapers are brittle by nature.** A layout change breaks a parser; the
  affected store reports an error and the rest keep working, but someone has to
  fix the parser.
- **No coupon or cashback source is wired up.** The model supports it; nothing
  populates it. See [Coupons and cashback](#coupons-and-cashback).
- **Marketplace sellers are not distinguished** on stores that have them, beyond
  rejecting open-box items.
- **Product matching is regex-based.** It is auditable and predictable, but a
  genuinely novel naming convention could produce a false negative. That is the
  intended direction to fail in.
- **Single-node, single-file state.** SQLite on one box. No clustering, and no
  intention of adding any.

---

## Being a good citizen

- `robots.txt` was read for every store, and the adapters stay inside it —
  which is why Pichau uses its sitemap and KaBuM uses the query-less
  `/busca/<term>` path.
- One cycle per 15 minutes, a delay between requests, bounded retries, and a
  randomised timer offset.
- No CAPTCHA solving, no proxy rotation, no fingerprint spoofing, no
  credentialed access. The browser fetcher is an ordinary headless Chromium
  loading a public page.
- If you fork this, keep the request rate low. The stores are not your
  infrastructure.

---

## License

MIT — see [LICENSE](LICENSE).
