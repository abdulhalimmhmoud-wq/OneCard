# OneCard Platform — Integration API Guide (v1)

This is the machine-to-machine contract for connecting external systems to the platform:
**reseller systems** (ordering programmatically), **suppliers** (price feeds), and the
**company's own provisioning stack** (fulfillment adapters + webhooks).

Base URL (prototype): `http://<host>:8000`
All endpoints under `/api/` are **CSRF-exempt** and JSON-only.

---

## 1. Authentication

### 1.1 Reseller API (all `/api/v1/*` endpoints)
Every reseller can be given an API key. A Sales Manager (or CCO/Admin) generates or rotates
it from **Sales Portal → My Resellers → 🔌 API**. The key is displayed **once**.

Send it on every request, either way:

```
X-API-Key: rk_9f2c4e...            # preferred
Authorization: Bearer rk_9f2c4e...  # also accepted
```

Missing/invalid key → `401 {"error":{"code":"unauthorized", ...}}`.
The key identifies the reseller: all prices, orders, wallet and codes are scoped to them.

### 1.2 Supplier price feed
Per-supplier key generated from **Operations → Suppliers → 🔑**. The key travels in the JSON
body (legacy shape, kept stable):

```
POST /api/supplier-prices
{"api_key": "oc_...", "items": [{"product_id": "6017", "cost": 95.5}, ...]}
```

Response: `{"ok": true, "supplier": "...", "updated": n, "unmatched": n}`.
Every accepted change is written to `supplier_price_history` and instantly reflected in the
Sourcing Matrix / best-source engine.

---

## 2. Reseller Endpoints

### 2.1 `GET /api/v1/ping`
Health + identity check.

```json
{"ok": true, "company": "Al Noor Digital Trading", "contract_status": "contracted"}
```

### 2.2 `GET /api/v1/catalogue`
The reseller-priced catalogue (their tier + any CCO-approved merchant overrides applied).

Query parameters (all optional):

| Param | Example | Notes |
|---|---|---|
| `page` / `page_size` | `1` / `100` | page_size max 500 |
| `merchant` | `Razer Gold` | exact match |
| `category` | `Gaming` | exact match |
| `country` / `region` / `currency` | `Saudi Arabia` / `GCC` / `SAR` | exact match |
| `search` | `pubg` | substring on name/merchant |

```json
{"ok": true, "page": 1, "page_size": 100, "total": 2942,
 "items": [{"id": 812, "sku": "6017", "name": "FRiENDi Aqua Voucher SR 100",
            "merchant": "FRiENDi mobile", "category": "Telecom & Recharge",
            "country": "Global", "region": "Global", "currency": "SAR",
            "face_value": 100, "your_price": 97, "your_discount": 2,
            "margin_pct": 3.0, "is_new": false, "special_rate": false}]}
```

> Prices are whole numbers in the product's own currency. The wallet and all order
> totals are **SAR**; conversion happens at checkout using the stored FX rate.

### 2.3 `GET /api/v1/wallet`
```json
{"ok": true, "currency": "SAR", "balance": 84350.0,
 "recent_transactions": [{"type": "order", "amount": -1197.56, "status": "approved",
                          "at": "2026-07-18 21:14:03"}]}
```

### 2.4 `GET /api/v1/account` — account model & spending headroom
How much you can still spend right now, in SAR (the internal base). This is the
API mirror of the platform's ordering gate — key for **consignment** clients (e.g.
a bank pulling card-by-card) who draw against a limit and settle later.

```json
{"ok": true, "currency": "SAR", "account_type": "consignment",
 "contract_status": "contracted", "available_to_spend": 320000.0,
 "credit_limit": 500000.0, "credit_outstanding": 180000.0, "unbilled": 180000.0,
 "frozen": false, "settlement_terms_days": 30, "billing_cycle": "monthly"}
```

- `account_type`: `prepaid` (returns `wallet_balance` instead of the credit block),
  `credit`, or `consignment`.
- `available_to_spend`: the ceiling for the next order. `credit` staged lines are
  metered to a tranche; a `frozen` (overdue) line returns `0`.
- `unbilled`: drawn amount not yet on a statement.

### 2.5 `GET /api/v1/statements` — invoices (credit / consignment)
```json
{"ok": true, "currency": "SAR", "unbilled": 180000.0,
 "statements": [
   {"statement_id": 12, "amount": 210000.0, "status": "issued",
    "period_start": "2026-06-01", "period_end": "2026-06-30",
    "due_at": "2026-07-30", "paid_at": null}
 ]}
```
Statement `status`: `issued` → `paid` (once Finance verifies your settlement), or
`overdue` (past due — the account freezes until settled).

### 2.6 `POST /api/v1/orders` — create an order
Requires a **signed contract** (`403 contract_required` otherwise). Prepaid orders
draw down your wallet; **credit/consignment** orders draw against your limit (no
prepaid balance needed) and are billed on a statement — check `GET /account` for
your live headroom before ordering.

Request (identify products by internal `id` or company `sku`; both accepted):

```json
{
  "idempotency_key": "your-unique-uuid",
  "items": [
    {"sku": "6017", "quantity": 10},
    {"id": 5875, "quantity": 3}
  ]
}
```

- `idempotency_key` (body, or `Idempotency-Key` header): replaying the same key returns the
  **original order** with `"idempotent_replay": true` — safe network retries, no double billing.
- Validation errors: `422 unknown_product`, `400 bad_request`.
- Business rejections (HTTP 409): `insufficient_balance` (prepaid wallet),
  `credit_limit_reached` (credit/consignment headroom exhausted),
  `account_on_hold` (frozen by an overdue statement — settle it first),
  `insufficient_stock` (gift-card codes cannot oversell), `order_rejected`.

Success → `201` with the full order payload:

```json
{"ok": true, "order_id": 19, "status": "placed", "total_sar": 1264.0,
 "items": [
   {"line_id": 40, "product_id": 5875, "name": "Chef Burger Gift Card SAR 100",
    "merchant": "Chef Burger KSA", "quantity": 3, "unit_price": 92,
    "currency": "SAR", "line_total_sar": 276.0,
    "fulfillment_status": "delivered",
    "codes": [{"code": "A1B2-C3D4-E5F6-A7B8", "pin": "123456"}, ...]},
   {"line_id": 41, "product_id": 812, "name": "...", "quantity": 10,
    "fulfillment_status": "external", "codes": []}
 ]}
```

`fulfillment_status` per line:

| Value | Meaning |
|---|---|
| `delivered` | Codes are attached (Issuing-Hub stock, or a provider adapter answered at checkout) |
| `external` | Awaiting provisioning — your worker fulfills it and calls `deliver_external_codes()` (see §4) |

### 2.7 `GET /api/v1/orders` — list (last 50)
### 2.8 `GET /api/v1/orders/<id>` — full detail (same payload as create)
### 2.9 `GET /api/v1/orders/<id>/codes` — codes only

```json
{"ok": true, "order_id": 19,
 "codes": [{"line_id": 40, "code": "A1B2-...", "pin": "123456"}]}
```

---

## 3. Webhooks (outbound)

Set the reseller's `webhook_url` from **My Resellers → 🔌 API**. The platform POSTs:

```
POST <webhook_url>
X-OneCard-Event: order.placed
{"event": "order.placed", "data": {"order_id": 19, "total_sar": 1264.0}}
```

Current events:
- `order.placed` — `{order_id, total_sar}`
- `statement.issued` — `{statement_id, amount_sar, due_at}` (a new invoice for a credit/consignment line)
- `statement.overdue` — `{statement_id, amount_sar}` (past due; the account is now frozen until settled)

Delivery is best-effort with a 3s timeout and is logged in
`webhook_deliveries` (reseller, event, HTTP status).
**Production hardening (your side):** move sending to a queue with retries + HMAC signature
header; the call site is one function — `models.send_webhook()`.

---

## 4. Fulfillment Adapters — the provider integration pattern

This is how the platform gets **real codes** for normal catalogue products (PUBG,
PlayStation, …) from your suppliers/aggregators.

```python
# models.py
def razer_adapter(item):
    """item = {'id', 'product_rowid', 'product_name', 'merchant', 'quantity', 'currency'}
    Return list[{'code': str, 'pin': str|None}] — raise on failure (line stays 'external')."""
    resp = requests.post(RAZER_URL, json={'sku': item['product_name'],
                                          'qty': item['quantity']},
                         headers={'Authorization': RAZER_KEY}, timeout=10)
    resp.raise_for_status()
    return resp.json()['codes']

register_fulfillment_adapter('Razer Gold', razer_adapter)
```

Behavior at checkout (`create_order`):
1. Wallet is charged in SAR; the order and its lines are committed.
2. Issued-Hub lines get codes from our own voucher stock (stock-guarded, can never oversell).
3. For every other line, if an adapter is registered for its merchant it is called
   **synchronously**; returned codes are stored in `external_codes` and the line becomes
   `delivered`. On any exception the line stays `external` — your async worker retries and
   finishes with `models.deliver_external_codes(order_item_id, codes, provider)`.

A reference adapter is registered for the demo merchant **“Nexon EU Store”** so the whole
flow is testable end-to-end (codes are prefixed `EXT-`).

> Production notes: run adapters in a worker (not in-request), add per-provider retry
> policies, and encrypt codes/PINs at rest (they are currently plaintext in the prototype).

---

## 5. Error model

Every non-2xx response:

```json
{"error": {"code": "insufficient_balance", "message": "Human-readable explanation."}}
```

| HTTP | Codes |
|---|---|
| 400 | `bad_request` |
| 401 | `unauthorized` |
| 403 | `contract_required` |
| 404 | `not_found` |
| 409 | `insufficient_balance`, `credit_limit_reached`, `account_on_hold`, `insufficient_stock`, `order_rejected` |
| 422 | `unknown_product` |

---

## 6. Quick start (curl)

```bash
KEY="rk_..."   # from My Resellers -> API -> Generate

curl -H "X-API-Key: $KEY" http://localhost:8000/api/v1/ping
curl -H "X-API-Key: $KEY" "http://localhost:8000/api/v1/catalogue?merchant=Chef%20Burger%20KSA"
curl -H "X-API-Key: $KEY" http://localhost:8000/api/v1/account      # spending headroom
curl -H "X-API-Key: $KEY" http://localhost:8000/api/v1/statements   # credit/consignment invoices

curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"idempotency_key":"demo-001","items":[{"id":5875,"quantity":2}]}' \
     http://localhost:8000/api/v1/orders

curl -H "X-API-Key: $KEY" http://localhost:8000/api/v1/orders/19/codes
```

---

*OneCard Platform — Integration API v1 (account models + statements added v15).
Automated coverage: `tests/e2e_api.py`, `tests/e2e_v15.py`.*
