# OneCard — Reseller Operations Platform (v9)

## Integration API v1 (v9) — see `API_GUIDE.md`

The machine-to-machine layer the technical team connects everything through:

- **Reseller API** (`/api/v1/*`, `X-API-Key` per reseller, generated from My Resellers → 🔌 API):
  ping, paginated/filterable priced catalogue, wallet, **order creation with idempotency keys**
  (safe retries, no double billing), order list/detail, and unified code retrieval.
- **Fulfillment Adapter pattern**: `register_fulfillment_adapter(merchant, fn)` — at checkout
  the adapter fetches real codes from the provider; lines are `delivered` or stay `external`
  for the async worker (`deliver_external_codes()`). A reference adapter ships for the demo
  merchant "Nexon EU Store"; Issuing-Hub products always deliver from our own voucher stock.
- **Outbound webhooks** per reseller (`order.placed`, logged in `webhook_deliveries`).
- **Supplier price feed** (`POST /api/supplier-prices`, per-supplier key) — unchanged.
- Structured error model: `{"error": {"code", "message"}}` with proper HTTP statuses.
- Coverage: `tests/e2e_api.py` (26 checks incl. a live local webhook receiver).

A **6-role web platform** that digitizes OneCard's full reseller lifecycle — from first contact
to contract, wallet funding, ordering, monthly commitment tracking, catalogue operations and
sales-team governance. Built with Flask + SQLite as a **product model / prototype**: the
technical team connects it to the live company systems via the integration points documented
in `models.py`.

## Roles & Responsibilities

| Role | What they do |
|------|--------------|
| **Admin / BD Manager** | Tier rules, master catalogue, all resellers, user accounts (all roles), compliance check, team performance, full visibility over every approval queue. |
| **Sales Manager** | Registers resellers (with business profile: client type + operating countries), previews their portals, receives purchase forecasts, signs contracts, requests special merchant discounts from the CCO, tracks their own scorecard. |
| **CCO** | Approves/rejects special discount requests with full profit before/after economics (auto-applied), monitors **Team Performance** and sets monthly sales targets. |
| **Finance** | Verifies bank-transfer receipts and credits reseller wallets. |
| **Operations** | Owns the catalogue: adds products, updates supplier prices (manual or bulk file), activates/deactivates products, manages the supplier directory. Every change is audited in the Price Change Log with automatic low-margin alerts to BD/CCO. |
| **Reseller** | Browses the catalogue with tier pricing, submits purchase plans (forecasts), tops up a wallet, places orders, and tracks their own purchase analysis. |

## v8 — Role Restructure, Issuing Hub & UX

**Role model (v8):**
- **CCO is the platform owner** — full admin powers everywhere (dashboard, users, tiers, every
  approval queue). The `admin` account remains as the technical owner.
- **BD (Business Development)** is a dedicated role: negotiates merchant deals, better rates,
  new suppliers and gift-card issuing leads — but never enters data. They submit deals through
  the **Deal Pipeline** (`/deals`); Operations execute and mark them done; both sides get notified.
  BD also has read access to Sourcing Intelligence.

**Issuing Partner Portal** (v8.1, role `partner`): the business we issue cards FOR (a perfume shop,
a restaurant chain — any brand with no digital gift cards) gets its own login, created by Ops from
the Issuing Hub. The partner sees ONLY their world: a dashboard (cards sold / redeemed / stock /
earnings), a **Redeem Station** for their cashiers (code + PIN, ownership-checked, no double
redemption), and a monthly **settlement statement** (gross sales × their share). They can never see
resellers, margins or the catalogue.

**Issuing Hub** (Ops → `/ops/issuing`): OneCard issues and manages digital gift cards for partner
businesses that have none, and sells them through all our channels:
- Issuing partners with a revenue-share % (partner share vs OneCard share)
- Card programs are real catalogue products (merchant = partner name) — tiers, wallets, orders
  and analytics work unchanged
- Voucher code batches (unique code + PIN) with available/sold/redeemed tracking
- Orders can never exceed code stock; buyers receive their actual codes inside Order History
- Code Checker for support (check status / mark redeemed) and low-stock alerts to Ops
- Partner economics report: units sold, revenue (SAR), partner payout, OneCard profit

**UX upgrades:** multi-select filters (merchant/category/country/region/currency) across all
catalogue views; multi-select client types at registration (a client can be Bank + Fintech —
recommendations combine all affinities); ops product form fixed (full merchant/category/country
lists incl. eSIM markets); **Sourcing Intelligence rebuilt in plain language** — a "Do This Now"
action list with estimated SAR/month savings, renamed jargon, tooltips and a glossary.

## Production Hardening (v7)

- **Single reporting currency (SAR)**: product prices stay in their own currency; at checkout every
  order line converts at the stored FX rate (`currency_rates`, editable by Finance at `/finance/rates`),
  the SAR total is deducted from the wallet, and each line stores the rate used — historical reports
  never drift when rates change. All analytics (analysis, sourcing intelligence, team performance)
  aggregate in SAR. Historical orders were backfilled automatically.
- **CSRF protection** on every state-changing request (session token, auto-injected into all forms by
  `static/app.js`; the supplier API stays key-authenticated). State-changing GET links converted to POST.
- **Stable secret key**: `ONECARD_SECRET_KEY` env var, else auto-persisted `instance_secret.key`
  (git-ignored) — sessions survive restarts and multi-worker setups work.
- **Debug off by default** (`ONECARD_DEBUG=1` to enable), **8 MB upload cap**, friendly 403/404/413/500
  pages, and **login rate-limiting** (5 tries / 15 min per email+IP).
- **UTC everywhere** for month bucketing — compliance and reporting match the DB clock.
- **XSS guard** on all inline JSON payloads; **hot-path DB indexes** added; requirements pinned.

## Multi-Supplier Sourcing & Batch Governance (v6)

- **Supplier price lists**: one product can have many suppliers, each at their own cost — updated manually, by per-supplier Excel import, or through the **supplier price API** (`POST /api/supplier-prices` with a per-supplier key). Every change is kept in `supplier_price_history`.
- **Sourcing Matrix** (Ops): every offer per product with the cheapest source highlighted, per-merchant saving opportunities, inline price edit and availability toggles.
- **Purchase Batches** (Ops): every stock purchase is a lot with quantity, unit cost and invoice ref. At buy time the system snapshots the best available price — buying above it computes a **sourcing variance** and **alerts BD & CCO automatically** (reason required).
- **FIFO allocation**: every reseller order consumes batches oldest-first, so each sold unit knows exactly **which supplier's batch it came from** → true COGS and realized profit per sale, per supplier, per month.
- **Batch Reconciliation** (Finance): every batch enters a reconciliation queue and is matched against the supplier invoice (reconcile / dispute), with SLA reminders after 3 days.
- **Sourcing Intelligence** (`/sourcing-intel`, BD + CCO): who we're actually selling from (revenue/profit share per supplier), top saving opportunities, overpaid-batch governance list, supplier scorecards, and **margin improvement timeline** — "profit improved since batch #X from supplier Y on date Z".

## Governance Layer (v5)

- **Team Performance** (`/team`, CCO + Admin): per-sales-manager funnel — registered → contracted → activated — with conversion rates, monthly order value, commitment attainment, discount-request stats and forecast response time.
- **Monthly targets**: CCO/Admin set targets (new resellers + sales value) per sales manager; attainment shown with progress bars.
- **My Scorecard** (`/sales/scorecard`): each sales manager sees their own numbers exactly as management does (feedback loop).
- **Reseller lifecycle stages** everywhere: `Prospect → Contracted → Active → At-Risk`.
- **SLA nudges** (daily): forecast unreviewed 3+ days → remind sales manager; top-up pending 24h+ → remind finance.
- **New Arrival flag auto-expires** 30 days after a product is added.

## The Customer Journey (end to end)

1. **Registration** — Sales manager creates the account: expected monthly sales → auto-assigns the pricing plan; client type + countries → power personalized recommendations.
2. **Discovery** — Reseller browses catalogue (filters: merchant, category, country, region, currency), profit calculator supports a **multi-item basket** (products + merchants combined), and a **Recommended page** tailored to their business type and markets.
3. **Forecast** — Reseller submits a purchase plan (merchant totals and/or product quantities) → notifies the sales manager. No commitment yet.
4. **Contract** — Sales manager signs the contract → ordering unlocks.
5. **Wallet** — Reseller transfers to OneCard's bank, uploads the receipt → Finance verifies → balance credited.
6. **Orders** — Reseller orders products with quantities, paid from wallet, with a live **forecast vs actual** comparison (ordering above plan is allowed).
7. **Special discounts** — Sales manager requests a higher margin share on a specific merchant (with current + projected sales) → CCO sees OneCard profit before/after → approval applies the override automatically.
8. **Compliance** — Monthly automatic check: below commitment → warning + one grace month (everyone notified) → still below → **automatic downgrade one tier**.
9. **Analysis** — Reseller sees spend, gains, merchant/category mix, monthly trend and growth insights.

## Business Rules

- **Margin-sharing pricing**: `reseller_price = max(default_price − (oc_margin × margin_share%), cost)` — per-merchant CCO overrides take precedence over the tier share.
- **Whole numbers only**: all money values display without decimals.
- **No exports** for resellers and sales managers (data confidentiality). Admin retains export.
- **Preview mode is read-only** — staff previewing a reseller portal cannot act on their behalf.
- **Receipts are private** — only Finance/Admin and the owning reseller can view them.

## Default Accounts (seeded)

| Role | Email | Password |
|------|-------|----------|
| CCO (full control) | `cco@onecard.com` | `Cco2025!` |
| Admin (technical) | `admin@onecard.com` | `OneCard2025!` |
| Sales | `sales@onecard.com` | `Sales2025!` |
| Business Development | `bd@onecard.com` | `Bd2025!` |
| Operations | `ops@onecard.com` | `Ops2025!` |
| Finance | `finance@onecard.com` | `Finance2025!` |
| Issuing Partner (demo: Chef Burger) | `portal@chefburger.sa` | `Partner2025!` |

> Change these before any non-local deployment.

## Quick Start

```bash
pip install -r requirements.txt   # flask, pandas, openpyxl, bcrypt
python seed_products.py           # first run — import Full Catalogue.xls
python app.py                     # http://localhost:8000
```

An existing v3 database upgrades **in place automatically** (migrations run at startup).

## Project Files

| File | Description |
|------|-------------|
| `app.py` | Flask routes for all 5 roles + workflows |
| `models.py` | SQLite schema v4, CRUD, pricing engine, compliance engine — **all integration points documented here** |
| `auth.py` | bcrypt login + per-role access decorators |
| `seed_products.py` | Catalogue import (column mapping = the integration contract with the company system) |
| `templates/` | Jinja2 UI — `partials/` (role navs), `admin/`, `sales/`, `reseller/`, `cco/`, `finance/` |
| `uploads/receipts/` | Bank-transfer receipts (private, served with access control) |
| `onecard.db` | SQLite database (generated locally; not tracked in git) |

## Integration Notes (for the technical team)

All reads/writes go through `models.py`. To connect production systems, swap the bodies of:

- **Catalogue feed** → `seed_products.py` (`PREFERRED` column map) + `products` table; ongoing price updates use the same column contract via **Ops → Bulk Price Update**
- **Actual reseller sales** → `get_month_total_orders()` (compliance currently uses platform orders)
- **Payments** → `wallet_transactions` (manual finance verification; replace with bank API/webhooks)
- **Supplier procurement** → `suppliers` table is a directory in v5; extend with PO/inventory when required

## Handover Notes for the Technical Team

**What is already production-grade**: role model & permissions, all business workflows, audit trails,
CSRF/session/upload hardening, FX-consistent money math, automated migrations, and the e2e test
suite in `tests/` (103 checks across three files — run the app, then `python tests/e2e_*.py`).

**Deliberately left for the integration phase** (by design, not omission):

| Area | Current state | Recommended move |
|------|---------------|------------------|
| Database | SQLite (single-writer) | PostgreSQL + `DECIMAL` money columns + Alembic migrations; use `SELECT … FOR UPDATE` on wallet ops |
| Catalogue payloads | Full catalogue rendered per page (~1.4 MB) | Server-side pagination/search API + cache (rates change rarely) |
| Background jobs | Daily checks piggyback on requests (`run_tier_compliance`) | Move to cron/Celery; the functions are already self-contained |
| APIs | Supplier price sync only (`POST /api/supplier-prices`) | Full REST layer for mobile/external systems; add idempotency keys + outbound webhooks |
| Sales data | Platform orders stand in for real sales | Point `get_month_total_orders()` at the company sales feed |
| Payments | Manual receipt verification | Bank API/webhooks writing into the same `wallet_transactions` ledger |
| Deployment | `python app.py` (Waitress/Gunicorn ready) | WSGI server behind a reverse proxy + `ONECARD_SECRET_KEY` env + Docker |
| Order lifecycle | `placed` only | Add fulfillment/delivery states when connected to provisioning |

---

*Built for OneCard Digital Distribution — v7, July 2026*
