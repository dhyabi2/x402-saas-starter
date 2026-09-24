# x402 SaaS Starter

**Analytics middleware + wallet attribution for x402 builders shipping on Base.**

Pay-per-download via x402 micropayments on Base. No Stripe, no Lemon Squeezy, no signup — buyers pay USDC and receive a 24h signed download URL.

This repo is the MIT core. Three paid tiers ship the production glue on top.

> **Note:** "No signup / no platform account required" means no SmartFlow account is needed to buy — it does **not** mean anonymity or avoidance of accounting, tax, sanctions or compliance obligations. SmartFlow records its own orders and payments for its own software/data sales, and does not operate payment processing, custody, exchange, wallet, facilitator or settlement services for customers.

---

## Buy a tier

| tier | price | what you get |
|---|---|---|
| **[Intro](https://api.smartflowproai.com/k)** | $49 USDC | analytics middleware + wallet attribution + 14-day email support |
| **[Standard](https://api.smartflowproai.com/k)** | $99 USDC | Intro + multi-endpoint router + reconciliation log + EVM cross-chain (Base/OP/Arb) + 30-day support |
| **[Pro](https://api.smartflowproai.com/k)** | $199 USDC | Standard + 3 MCP server templates (agent-facing analytics) + private Discord + 90-day premium support |

→ **Buy at [api.smartflowproai.com/k](https://api.smartflowproai.com/k)** — pay USDC on Base, get the kit by signed URL.

48h no-questions refund. USDC returned to your buying wallet.

---

## What you get (concretely)

**Intro tier ($49 USDC)** ships a 13KB zip with:
- `src/analytics_middleware.py` — FastAPI middleware that logs every x402 payment event to SQLite (190+ LOC, working code, not vapor)
- `src/wallet_attribution.py` — Parse `X-PAYMENT` header → buyer wallet → per-wallet history
- `examples/dashboard_view.sql` — 7 pre-built SQL queries (top-payer leaderboard, conversion funnel, per-tier revenue, repeat-buyer signals, etc.)
- `examples/basic_usage.py` — 3 Python helpers you drop into a one-shot script or dashboard
- `setup.md` — 15-minute walkthrough with troubleshooting
- 14-day email support contract

**Standard tier ($99 USDC)** adds (20KB zip):
- `src/multi_endpoint_router.py` — Per-endpoint config + cross-chain wallet identity registry
- `src/reconciliation_log.py` — Append-only payment log with cryptographic hash chain (tamper-evident; verification CLI included)
- `src/evm_cross_chain.py` — Base + Optimism + Arbitrum USDC contract registry + decoding
- 12 additional SQL queries (cohort retention, MRR proxy, churn windows, refund audit, tier upgrade paths)
- 30-day email support

**Pro tier ($199 USDC)** adds (24KB zip):
- 3 working MCP server templates (~250 LOC each, TypeScript, built on official MCP SDK):
  - `facilitator_analytics_server.ts` — wallet history, endpoint revenue, conversion funnel, revenue velocity
  - `wallet_clustering_server.ts` — cross-chain identity, LTV ranking, suspicious patterns, fingerprint matching
  - `reconciliation_dashboard_server.ts` — chain integrity verification, proof-of-payment, compliance reports
- Claude Desktop registration walkthrough
- Private Discord access (≤100 members)
- Monthly office hours
- 90-day premium support with free integration review

---

## Why x402 (not Stripe)

This kit ships natively on x402 rather than behind a fiat gateway, because it's built for teams already working with x402.

- USDC on Base, full stop
- On-chain receipts (Basescan visible)
- Refunds return to the buying wallet
- No signup, no platform account, no platform fees, no chargeback risk
- 48h no-questions refund window

If you're already shipping on x402 (Bankr, Dexter, Mogami) and you want visibility into your payment flows, this kit gets you there in one evening.

**Scope.** This is self-hosted developer software: you install and run it in your own environment and configure your own recipient wallet. SmartFlow sells the kit as its own product and does not operate payment processing, custody, exchange, wallet, facilitator or settlement services for customers.

---

## Quick start (run the MIT core locally)

```bash
git clone https://github.com/smartflowproai-lang/x402-saas-starter.git
cd x402-saas-starter

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — set DOWNLOAD_SECRET to a long random string,
# RECIPIENT_WALLET to your EVM address on Base,
# optionally CDP_API_KEY if using Coinbase Developer Platform facilitator

mkdir -p assets
# drop your paid artifacts as assets/intro.zip, assets/std.zip, assets/pro.zip

uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Then `curl -i http://localhost:8000/buy/intro` returns a real x402 v0.5 challenge.

---

## Architecture

```
[buyer] ──GET /buy/intro──→ [server.py] ──402 challenge w/ x402 envelope──→ [buyer]
[buyer] ──POST /download/intro w/ X-PAYMENT header──→ [server.py]
[server.py] ──verify(payment)──→ [CDP facilitator OR self-hosted facilitator]
[facilitator] ──valid──→ [server.py] ──signed 24h URL──→ [buyer]
[buyer] ──GET /download/file/intro/<token>──→ [server.py] ──FileResponse(intro.zip)──→ [buyer]
```

Stateless beyond the assets directory + (optional) analytics.db. SQLite gives you full audit trail without operational ceremony.

---

## Environment

| var | required | notes |
|---|---|---|
| `DOWNLOAD_SECRET` | yes | HMAC-SHA256 secret for signed URLs. Must NOT be `changeme`. |
| `CDP_API_KEY` | for CDP mode | Bearer token for Coinbase Developer Platform facilitator. Leave unset for self-hosted facilitator. |
| `RECIPIENT_WALLET` | yes | EVM address that receives USDC. 42 chars, `0x`-prefixed. |
| `FACILITATOR_URL` | no | Defaults to `https://api.cdp.coinbase.com`. |
| `USDC_ASSET_ADDRESS` | no | Defaults to Base USDC (`0x8335…2913`). |
| `NETWORK` | no | Defaults to `base`. |
| `ASSETS_DIR` | no | Defaults to `./assets`. Expects `<tier>.zip` per tier. |
| `PUBLIC_BASE_URL` | no | Used in `resource` field of x402 challenge + returned download URL. |

---

## Docker

```bash
docker build -t x402-saas-starter .
docker run --rm -p 8000:8000 \
  -e DOWNLOAD_SECRET="$(openssl rand -hex 32)" \
  -e CDP_API_KEY="your-cdp-key" \
  -e RECIPIENT_WALLET=0xYourWallet \
  -e PUBLIC_BASE_URL="https://your.domain" \
  -v "$PWD/assets:/app/assets:ro" \
  x402-saas-starter
```

---

## Manual smoke test

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/
# 200 (landing)

curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/buy/intro
# 402 (x402 challenge)
```

For full smoke including post-payment download, see `tests/test_smoke.py` (20+ test cases).

---

## Nano (XNO) feeless settlement rail

A receive-only Nano (XNO) verification module (`nano_xno.py`) now sits beside the USDC
x402 path, giving buyers a **feeless, instant settlement option** — no facilitator, no gas,
no chargeback risk, and no issuer that can freeze funds. The server never holds a private key.

**How it works**
1. The operator publishes a `nano_` pay-to address (no env var change — configurable via
   `NANO_PAY_TO` / `NANO_RPC_URL` once integrated).
2. The buyer sends XNO on the Nano network (~1s soft finality, $0 fee).
3. `verify_nano_payment(pay_to, required_xno)` checks the settled block over a public
   keyless RPC — fail-closed, never trusts the client's amount.

**Status:** `nano_xno.py` + 10 tests (9 offline, 1 online) gate the `isValid` result.
To enable in the `/download` endpoint, call `verify_nano_payment` alongside the existing
`require_x402_payment(path)`; a Nano-settled download is served the same signed-URL way.

| property | USDC (x402) | Nano (nano:mainnet) |
|---|---|---|
| Settlement cost | ~$0.02–0.20 facilitator fee | $0 |
| Finality | ~2–12 s (Base L1) | ~0.3 s soft ack |
| Issuer | USDC Centre (stablecoin issuer) | None (no keys to freeze) |
| Server key needed | No (CDP API key OK) | No (keyless RPC) |

---

## Tests

```bash
pip install -r requirements.txt
pytest tests/
```

---

## License

MIT for the core (this repo). Paid tier bundles ship under MIT for code + restricted resale for docs/queries (see each tier's LICENSE).

---

## Built by

**Tom Smart** — x402 observability operator
- [@tomsmart_ai](https://twitter.com/TomSmart_ai) on X
- [smartflowproai.substack.com](https://smartflowproai.substack.com) — weekly x402 observability writing
- [@tomsmart-ai/mapper-mcp](https://www.npmjs.com/package/@tomsmart-ai/mapper-mcp) — sibling project, x402 endpoint catalog as MCP server

If you are running an x402 catalog and want a paid audit of schema-drift and method-coverage against your data, intake is open at [verify.smartflowproai.com/audit](https://verify.smartflowproai.com/audit/).

---

## Build log

| version | date | changes |
|---|---|---|
| 1.0.0 | 2026-05-28 | Initial release. Three tiers shipped. K main launch 14:00 CEST. |
