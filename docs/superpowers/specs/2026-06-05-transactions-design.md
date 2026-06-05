# Transactions support — design

Date: 2026-06-05
Status: approved approach A (thin API mirror)

## Goal

Expose BitLaunch's crypto top-up transactions in the existing bitlaunch-mcp
server so an agent can create a top-up invoice, hand the payment link to the
user (e.g. via a Telegram skill, out of scope here), and check whether the
payment confirmed.

## API surface (BitLaunch)

- `POST /transactions` — body `{amountUsd, cryptoSymbol}` where
  `cryptoSymbol` ∈ {BTC, LTC, ETH}. Returns a transaction with a payment
  `address`, `amountCrypto`, `status` and `statusUrl` invoice link.
  NOTE: `amountUsd` appears to be plain USD (20 = $20), unlike the rest of
  the API which uses mUSD. Verify against gobitlaunch during implementation.
- `GET /transactions?page=&items=` — paginated; returns
  `{history: [tx...], total}`.
- `GET /transactions/{id}` — single transaction.
- Transaction object fields: `id`, `userid`, `created`/`date`, `address`,
  `cryptoSymbol`, `amountUsd`, `amountCrypto`, `status`
  (Pending/Confirming/...), `statusUrl` or `paymentPath` + `processorid`
  (`"bl"` → `https://pay.bitlaunch.io`).

## Changes

### client.py — three methods on `BitLaunchClient`

- `create_transaction(amount_usd: float, crypto_symbol: str) -> dict`
  → `POST /transactions`.
- `list_transactions(page: int = 1, items: int = 25) -> dict`
  → `GET /transactions`; returns `{"transactions": [...], "total": n}`.
- `get_transaction(transaction_id: str) -> dict`
  → `GET /transactions/{transaction_id}`.
- Shared `_transaction_dict()` normalizer returning:
  `id`, `created`, `crypto_symbol`, `amount_usd`, `amount_crypto`,
  `address`, `status`, `invoice_url`. `invoice_url` comes from `statusUrl`
  when present, otherwise built from `processorid` + `paymentPath`
  (known processor: `bl` → `https://pay.bitlaunch.io`; unknown processor →
  empty string).

### config.py

- `max_topup_usd: float` from `BITLAUNCH_MAX_TOPUP_USD`, default `50`.

### server.py — three tools

- `create_transaction(amount_usd, crypto_symbol)` — raises `ToolError` when
  `amount_usd > cfg.max_topup_usd`, message points at
  `BITLAUNCH_MAX_TOPUP_USD` (same style as the cost-per-hour guard).
  Docstring tells the agent: creating a transaction charges nothing by
  itself; the returned `invoice_url`/`address` is what the user pays
  manually; `crypto_symbol` must be BTC, LTC or ETH.
- `list_transactions(page=1, items=25)` — history with statuses.
- `get_transaction(transaction_id)` — poll a single transaction's status
  (e.g. Pending → Confirming → complete) after the user pays.

## Error handling

Same as the rest of the server: client raises `BitLaunchError` on non-200,
tools let it propagate / wrap user-facing guards in `ToolError`. No retry
logic.

## Testing

Unit tests following the existing fake-client pattern in `tests/`:
- create: payload shape, guardrail rejection above `max_topup_usd`,
  normalized response.
- list: pagination params passed through, `history` → `transactions`
  mapping, empty history.
- get: normalization incl. `invoice_url` fallback from
  `processorid`/`paymentPath`.
The live smoke test is not extended — no real transactions in CI.

## Out of scope

- Telegram delivery (user will build a separate skill for that).
- Any auto-top-up / balance-sizing logic — the agent composes that.
