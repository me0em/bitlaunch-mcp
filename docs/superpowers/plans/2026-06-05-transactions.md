# Transaction Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `create_transaction`, `list_transactions`, `get_transaction` MCP tools so an agent can create a crypto top-up invoice and report its payment status.

**Architecture:** Thin mirror of the BitLaunch transactions API (spec: `docs/superpowers/specs/2026-06-05-transactions-design.md`). Three async methods on `BitLaunchClient` with a shared `_transaction_dict()` normalizer, one new config guardrail (`BITLAUNCH_MAX_TOPUP_USD`), three `@mcp.tool` wrappers in `server.py`.

**Tech Stack:** Python 3.12, httpx, FastMCP, pytest + respx (HTTP mocking), fake-client injection via `server._state`.

**Verified API facts** (from gobitlaunch `transaction.go` + developers.bitlaunch.io):
- `POST /transactions` body: `{"amountUsd": <int, plain USD — NOT mUSD>, "cryptoSymbol": "BTC|LTC|ETH", "lightningNetwork": false}` → returns a transaction object.
- `GET /transactions?page=<n>&items=<n>` → `{"history": [tx...], "total": <int>}`.
- `GET /transactions/{id}` → single transaction object.
- Transaction JSON fields: `id`, `date` (docs examples sometimes show `created`), `address`, `cryptoSymbol`, `amountUsd`, `amountCrypto`, `status` (`Pending`/`Confirming`/...), `statusUrl`, `qrCodeUrl`; older list objects may instead carry `paymentPath` (`"/invoice/<id>"`) + `processorid` (`"bl"` → `https://pay.bitlaunch.io`).

---

### Task 1: Client transaction methods

**Files:**
- Modify: `src/bitlaunch_mcp/client.py` (methods after `create_ssh_key`, ~line 167)
- Test: `tests/test_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_client.py`:

```python
TX_JSON = {
    "id": "tx1",
    "userid": "u1",
    "date": "2026-06-05T12:00:00.000Z",
    "address": "2N8PXuPYFUNf987Tj22GFj4Yyk6zNTMAaSs",
    "cryptoSymbol": "BTC",
    "amountUsd": 20,
    "amountCrypto": "0.00274643",
    "status": "Pending",
    "statusUrl": "https://pay.bitlaunch.io/invoice/inv1",
    "qrCodeUrl": "https://pay.bitlaunch.io/qr/inv1",
}

TX_NORMALIZED = {
    "id": "tx1",
    "created": "2026-06-05T12:00:00.000Z",
    "crypto_symbol": "BTC",
    "amount_usd": 20,
    "amount_crypto": "0.00274643",
    "address": "2N8PXuPYFUNf987Tj22GFj4Yyk6zNTMAaSs",
    "status": "Pending",
    "invoice_url": "https://pay.bitlaunch.io/invoice/inv1",
    "qr_code_url": "https://pay.bitlaunch.io/qr/inv1",
}


@respx.mock
async def test_create_transaction_payload_and_normalization():
    route = respx.post(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json=TX_JSON)
    )
    tx = await BitLaunchClient("tok").create_transaction(20, "BTC")
    sent = json.loads(route.calls.last.request.content)
    # amountUsd is plain USD (gobitlaunch CreateTransactionOptions), not mUSD
    assert sent == {
        "amountUsd": 20, "cryptoSymbol": "BTC", "lightningNetwork": False,
    }
    assert tx == TX_NORMALIZED


@respx.mock
async def test_list_transactions_pagination_and_envelope():
    route = respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json={"history": [TX_JSON], "total": 1})
    )
    res = await BitLaunchClient("tok").list_transactions(page=2, items=10)
    assert route.calls.last.request.url.params["page"] == "2"
    assert route.calls.last.request.url.params["items"] == "10"
    assert res == {"transactions": [TX_NORMALIZED], "total": 1}


@respx.mock
async def test_list_transactions_empty_history():
    respx.get(f"{BASE_URL}/transactions").mock(
        return_value=httpx.Response(200, json={"history": None, "total": 0})
    )
    res = await BitLaunchClient("tok").list_transactions()
    assert res == {"transactions": [], "total": 0}


@respx.mock
async def test_get_transaction_invoice_url_fallback():
    """Docs' transaction object variant: paymentPath + processorid, no statusUrl."""
    legacy = {
        "id": "tx2", "created": "2026-06-05T08:00:00.000Z",
        "address": "addr", "cryptoSymbol": "LTC", "amountUsd": 5,
        "amountCrypto": "0.07", "status": "Confirming",
        "paymentPath": "/invoice/inv2", "processorid": "bl",
    }
    respx.get(f"{BASE_URL}/transactions/tx2").mock(
        return_value=httpx.Response(200, json=legacy)
    )
    tx = await BitLaunchClient("tok").get_transaction("tx2")
    assert tx["invoice_url"] == "https://pay.bitlaunch.io/invoice/inv2"
    assert tx["created"] == "2026-06-05T08:00:00.000Z"  # falls back to "created"
    assert tx["status"] == "Confirming"
    assert tx["qr_code_url"] == ""


@respx.mock
async def test_get_transaction_unknown_processor_empty_invoice_url():
    respx.get(f"{BASE_URL}/transactions/tx3").mock(
        return_value=httpx.Response(200, json={
            "id": "tx3", "date": "2026-06-05T08:00:00.000Z", "address": "a",
            "cryptoSymbol": "ETH", "amountUsd": 5, "amountCrypto": "0.002",
            "status": "Pending", "paymentPath": "/invoice/x",
            "processorid": "other",
        })
    )
    tx = await BitLaunchClient("tok").get_transaction("tx3")
    assert tx["invoice_url"] == ""
```

Note: `tests/test_client.py` already has `import json` at line 150 — these tests rely on it; keep the new code below that line.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_client.py -k transaction -v`
Expected: 5 FAILED with `AttributeError: 'BitLaunchClient' object has no attribute 'create_transaction'` (and similar).

- [ ] **Step 3: Implement the client methods**

In `src/bitlaunch_mcp/client.py`, append to `BitLaunchClient` (after `create_ssh_key`):

```python
    # Invoice URL prefix by payment processor (Transaction Object docs).
    _PROCESSOR_URLS = {"bl": "https://pay.bitlaunch.io"}

    @classmethod
    def _transaction_dict(cls, t: dict) -> dict:
        invoice_url = t.get("statusUrl", "")
        if not invoice_url and t.get("paymentPath"):
            prefix = cls._PROCESSOR_URLS.get(t.get("processorid", ""))
            invoice_url = f"{prefix}{t['paymentPath']}" if prefix else ""
        return {
            "id": t["id"],
            "created": t.get("date") or t.get("created", ""),
            "crypto_symbol": t.get("cryptoSymbol", ""),
            "amount_usd": t.get("amountUsd", 0),
            "amount_crypto": t.get("amountCrypto", ""),
            "address": t.get("address", ""),
            "status": t.get("status", ""),
            "invoice_url": invoice_url,
            "qr_code_url": t.get("qrCodeUrl", ""),
        }

    async def create_transaction(self, amount_usd: int, crypto_symbol: str) -> dict:
        # Unlike the rest of the API, amountUsd here is plain USD, not mUSD.
        d = await self._request("POST", "/transactions", json={
            "amountUsd": amount_usd,
            "cryptoSymbol": crypto_symbol,
            "lightningNetwork": False,
        })
        return self._transaction_dict(d)

    async def list_transactions(self, page: int = 1, items: int = 25) -> dict:
        d = await self._request("GET", f"/transactions?page={page}&items={items}")
        return {
            "transactions": [
                self._transaction_dict(t) for t in (d or {}).get("history") or []
            ],
            "total": (d or {}).get("total", 0),
        }

    async def get_transaction(self, transaction_id: str) -> dict:
        d = await self._request("GET", f"/transactions/{transaction_id}")
        return self._transaction_dict(d)
```

Also update the module docstring quirk list (line 5: `- All money amounts are integers in mUSD (1/1000 USD).`) to:

```python
- All money amounts are integers in mUSD (1/1000 USD) — except transaction
  amountUsd, which is plain USD.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_client.py -v`
Expected: all PASS (new 5 + existing).

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/client.py tests/test_client.py
git commit -m "feat(client): transaction methods (create/list/get) with normalization"
```

---

### Task 2: Config guardrail `max_topup_usd`

**Files:**
- Modify: `src/bitlaunch_mcp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` (it already tests `load_config`; ensure `from bitlaunch_mcp.config import load_config` is imported — add it if not):

```python
def test_max_topup_default_and_override():
    cfg = load_config({"BITLAUNCH_API_KEY": "tok"})
    assert cfg.max_topup_usd == 50.0
    cfg = load_config({
        "BITLAUNCH_API_KEY": "tok", "BITLAUNCH_MAX_TOPUP_USD": "200",
    })
    assert cfg.max_topup_usd == 200.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_max_topup_default_and_override -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'max_topup_usd'`.

- [ ] **Step 3: Implement**

In `src/bitlaunch_mcp/config.py`:

Add a field to `Config` AFTER `min_balance_hours` (it has a default, so it must stay behind the non-default fields):

```python
    min_balance_hours: float = 24.0  # require balance >= this many hours of the plan
    max_topup_usd: float = 50.0      # create_transaction refuses larger invoices
```

Add to the `Config(...)` call in `load_config`:

```python
        max_topup_usd=float(env.get("BITLAUNCH_MAX_TOPUP_USD", "50")),
```

Keeping the default in the dataclass means the existing `Config(...)` construction in `tests/test_server.py:73` keeps working unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/config.py tests/test_config.py
git commit -m "feat(config): BITLAUNCH_MAX_TOPUP_USD guardrail (default \$50)"
```

---

### Task 3: MCP tools

**Files:**
- Modify: `src/bitlaunch_mcp/server.py` (tools after `get_account`, ~line 65)
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_server.py`, first extend `FakeBitLaunch`:

In `__init__`, add:

```python
        self.transactions = []
        self.created_transactions = []
```

Add methods (after `create_ssh_key`):

```python
    async def create_transaction(self, amount_usd, crypto_symbol):
        self.created_transactions.append((amount_usd, crypto_symbol))
        tx = {
            "id": f"tx{len(self.transactions) + 1}",
            "created": "2026-06-05T12:00:00.000Z",
            "crypto_symbol": crypto_symbol, "amount_usd": amount_usd,
            "amount_crypto": "0.001", "address": "addr1",
            "status": "Pending",
            "invoice_url": "https://pay.bitlaunch.io/invoice/inv1",
            "qr_code_url": "https://pay.bitlaunch.io/qr/inv1",
        }
        self.transactions.append(tx)
        return tx

    async def list_transactions(self, page=1, items=25):
        return {"transactions": self.transactions,
                "total": len(self.transactions)}

    async def get_transaction(self, transaction_id):
        for t in self.transactions:
            if t["id"] == transaction_id:
                return t
        raise AssertionError(f"unknown transaction {transaction_id}")
```

Then append the tests:

```python
async def test_create_transaction_happy_path(fake):
    async with Client(server.mcp) as c:
        res = await c.call_tool("create_transaction", {
            "amount_usd": 20, "crypto_symbol": "btc",
        })
    assert res.data["status"] == "Pending"
    assert res.data["invoice_url"] == "https://pay.bitlaunch.io/invoice/inv1"
    # symbol is upper-cased before hitting the API
    assert fake.created_transactions == [(20, "BTC")]


async def test_create_transaction_rejects_over_cap(fake):
    # fixture Config uses the default max_topup_usd=50.0
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="MAX_TOPUP_USD"):
            await c.call_tool("create_transaction", {
                "amount_usd": 51, "crypto_symbol": "BTC",
            })
    assert fake.created_transactions == []


async def test_create_transaction_rejects_bad_symbol(fake):
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="BTC, LTC, ETH"):
            await c.call_tool("create_transaction", {
                "amount_usd": 20, "crypto_symbol": "DOGE",
            })
    assert fake.created_transactions == []


async def test_create_transaction_rejects_non_positive(fake):
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="positive"):
            await c.call_tool("create_transaction", {
                "amount_usd": 0, "crypto_symbol": "BTC",
            })


async def test_list_and_get_transaction_tools(fake):
    async with Client(server.mcp) as c:
        await c.call_tool("create_transaction", {
            "amount_usd": 20, "crypto_symbol": "BTC",
        })
        listed = await c.call_tool("list_transactions", {})
        got = await c.call_tool("get_transaction", {"transaction_id": "tx1"})
    assert listed.data["total"] == 1
    assert listed.data["transactions"][0]["id"] == "tx1"
    assert got.data["id"] == "tx1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -k transaction -v`
Expected: 5 FAILED with `ToolError: Unknown tool: create_transaction` (or FastMCP's equivalent "tool not found" error).

- [ ] **Step 3: Implement the tools**

In `src/bitlaunch_mcp/server.py`, add after the `get_account` tool (line 64):

```python
VALID_CRYPTO = ("BTC", "LTC", "ETH")


@mcp.tool
async def create_transaction(amount_usd: int, crypto_symbol: str) -> dict:
    """Create a crypto top-up invoice for the account balance. Nothing is
    charged automatically: the user must manually pay the returned
    invoice_url (or send amount_crypto to address; qr_code_url renders a
    scannable code). crypto_symbol: BTC | LTC | ETH. Status starts as
    Pending — track it with get_transaction. Balance updates only after
    the payment confirms."""
    cfg = get_config()
    symbol = crypto_symbol.upper()
    if symbol not in VALID_CRYPTO:
        raise ToolError(
            f"crypto_symbol must be one of: {', '.join(VALID_CRYPTO)}."
        )
    if amount_usd <= 0:
        raise ToolError("amount_usd must be positive.")
    if amount_usd > cfg.max_topup_usd:
        raise ToolError(
            f"Top-up of ${amount_usd} exceeds the limit of "
            f"${cfg.max_topup_usd:g} (BITLAUNCH_MAX_TOPUP_USD). Raise it "
            f"to allow larger invoices."
        )
    return await get_client().create_transaction(amount_usd, symbol)


@mcp.tool
async def list_transactions(page: int = 1, items: int = 25) -> dict:
    """Paginated top-up transaction history (newest first) with statuses
    and invoice links. Returns {transactions: [...], total}."""
    return await get_client().list_transactions(page=page, items=items)


@mcp.tool
async def get_transaction(transaction_id: str) -> dict:
    """Status of one top-up transaction (Pending -> Confirming -> Complete).
    Use after create_transaction to check whether the payment confirmed."""
    return await get_client().get_transaction(transaction_id)
```

No new imports needed — `ToolError`, `get_config`, `get_client` are already in scope.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest tests/ -v` (note: `test_live.py` is opt-in and skips without credentials — that is expected)
Expected: all PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/server.py tests/test_server.py
git commit -m "feat(server): create/list/get transaction tools with top-up cap"
```

---

### Task 4: README tool docs

**Files:**
- Modify: `README.md` (tool table/list and env var table — locate the sections documenting existing tools and `BITLAUNCH_*` vars)

- [ ] **Step 1: Document the three tools and the env var**

Add to the tools documentation, matching the existing format:

- `create_transaction(amount_usd, crypto_symbol)` — create a crypto top-up invoice (BTC/LTC/ETH); returns payment address + invoice link. Capped by `BITLAUNCH_MAX_TOPUP_USD`. Nothing is charged until the user pays the invoice.
- `list_transactions(page, items)` — paginated top-up history with statuses.
- `get_transaction(transaction_id)` — check whether a payment confirmed.

Add to the env var documentation: `BITLAUNCH_MAX_TOPUP_USD` — max USD per top-up invoice, default `50`.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: transaction tools and BITLAUNCH_MAX_TOPUP_USD"
```
