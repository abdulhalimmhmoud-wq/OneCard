# OneCard — Reseller Operations Platform (v17)

## Customer CRM: dedup, contact data & CCO tracking board (v17)

Registration and tracking now behave like a lightweight CRM:

- **More contact data** — the sales manager must enter the contact person's **phone**
  at registration (`contact_phone`).
- **Duplicate-customer prevention** — a customer may reach two sales managers who don't
  know about each other; a second registration is now **blocked** when it collides on
  any strong identifier — **email, company name, or phone** (`find_duplicate_reseller`,
  with normalised company/phone matching so `+966 50…` and `050…` are the same). The
  message names the existing customer + managing manager so the team can coordinate.
- **Commercial registration number** — the customer's primary identifier, captured at
  **contract time** (option on the contract-upload form), copied onto the profile and
  also deduplicated (no two customers share a CR number).
- **CCO Customers board** (`/cco/customers`) — every registered customer across all
  sales managers, with a **dashboard** (totals, new-this-month, contracted, with-CR,
  wallet+outstanding exposure, breakdowns by stage & account model) and **filters** by
  sales manager, stage, tier, account model, registration date range, and free-text
  search (company / email / phone / CR) — built to stay usable at 100s of customers.

Changes touch registration (route + form), the contract flow, `create_reseller` /
`update_reseller_profile` / `create_contract`, and the sales My Resellers view (phone +
CR shown). Coverage: `tests/e2e_v17.py` (14 checks); the existing suites were updated to
supply the now-required phone. Full regression green — 15 suites, 375 checks.

## Webhook Hardening & Credit Reporting — Phase 4 (v16)

Production hardening for outbound webhooks plus Finance/CCO reporting — the final
phase of the account-models programme.

- **Durable, retried, signed webhooks** — `send_webhook` now enqueues into
  `webhook_deliveries` (a real queue) and a background worker drains it. Failed
  deliveries **retry with exponential backoff** (up to 6 attempts: 30s → 6h) then
  mark `failed`; every attempt records status/attempts/HTTP code/last error. Each
  POST is **HMAC-SHA256 signed** with a per-reseller secret (`whsec_…`, minted when a
  webhook URL is set, shown to Sales) via `X-OneCard-Signature` +
  `X-OneCard-Timestamp`, with `X-OneCard-Delivery` for idempotent de-duplication.
  `statement.issued` / `statement.overdue` / `order.placed` all flow through it.
- **Receivables aging** — the Finance **Credit & Settlements** hub shows open
  statements bucketed by overdue age (not-due, 1-30, 31-60, 61-90, 90+ days).
- **Portfolio CSV export** — `/finance/credit/export.csv` downloads the full
  credit/consignment book (limit, outstanding, unbilled, overdue, oldest-overdue
  days, frozen, terms) for offline reporting.

Signature verification + delivery semantics are documented in
[API_GUIDE.md](API_GUIDE.md). Coverage: `tests/e2e_v16.py` (15 checks, incl. a live
signed-delivery + retry test). **The account-models programme (v13–v16) is complete.**

## Consignment via API & Account Endpoints — Phase 3 (v15)

Both **credit and consignment** clients can be API-connected and pull
**product-by-product** in real time (e.g. a bank whose customers each buy a single
card on demand) — the account model and the integration channel are independent.
Either can now draw through the Integration API with no prepaid balance required:

- **Card-by-card over the API** — `POST /api/v1/orders` runs through the same
  `available_to_spend` gate, so a credit/consignment client draws against their limit
  and settles later on a statement. Order rejections now return account-type-aware
  codes: `credit_limit_reached`, `account_on_hold` (frozen by an overdue statement),
  alongside the existing `insufficient_balance` / `insufficient_stock`.
- **`GET /api/v1/account`** — live spending headroom + account model (type, limit,
  outstanding, unbilled, frozen, terms).
- **`GET /api/v1/statements`** — the client's invoices + current unbilled amount.
- **Webhooks** — `statement.issued` and `statement.overdue` now fire to the client's
  webhook alongside `order.placed`.
- **Operations view** — a new **Consignment Activity** page shows every credit/
  consignment account's live 30-day draw-down, outstanding, unbilled, available and
  channel (API vs portal), so Ops can anticipate restock for real-time pullers.

Full contract in [API_GUIDE.md](API_GUIDE.md). Coverage: `tests/e2e_v15.py`
(11 checks). *Remaining (Phase 4): reporting polish + production webhook hardening.*

## Credit Settlement Engine & Credit Requests — Phase 2 (v14)

Credit and consignment accounts now have a full settlement lifecycle on top of
the v13 draw mechanics:

- **Statements** — drawn-but-unbilled amount is billed as a `statements` row with
  a due date (issue date + `settlement_terms_days`). Finance can issue one on
  demand, and a daily sweep (`run_statement_cycle`) auto-issues per billing cycle
  (monthly/weekly). `unbilled = outstanding − open statements`.
- **Settlement** — the reseller uploads a bank-transfer receipt against a statement
  (new **Statements** panel on the Billing page); Finance verifies it on the new
  **Credit & Settlements** hub, which reduces `credit_outstanding`, marks the
  statement paid and restores available credit.
- **Overdue → freeze** — a statement past its due date flips to `overdue`, freezes
  the line (`available_to_spend → 0`) and escalates to reseller + Finance + CCO.
  Settling it unfreezes automatically. (Real card auto-charge stays a documented
  hook — no PSP.)
- **Additional-credit requests** — Sales requests a limit increase (**permanent** or
  **temporary** with an expiry) from the reseller's row; it needs **both CCO and
  Finance** to sign off before it applies. A temporary bump auto-reverts to the
  permanent base when it expires. Team is alerted on request and on limit-reached.
- **Exposure** — Finance and CCO get portfolio KPIs (approved limits, outstanding,
  overdue, frozen accounts).

Coverage: `tests/e2e_v14.py` (28 checks). *Next: Phase 3 — consignment period
statements + API card-by-card without prepaid; Phase 4 — dashboards, `/api/v1/account`
+ `/statement`, webhooks.*

## Account Models & Contract Signing — Phase 1 (v13)

Resellers are no longer implicitly prepaid. Each has a first-class **`account_type`**
and ordering runs through one gate, `available_to_spend(profile)`:

| Type | Available to spend | An order… | Settlement |
|------|--------------------|-----------|------------|
| **`prepaid`** (cash) | wallet balance | debits the wallet | transfer + receipt → Finance credits (today's flow) |
| **`credit`** | full: `limit − outstanding` · staged: `min(tranche, limit − outstanding)` | raises `credit_outstanding` | settle per billing cycle |
| **`consignment`** | `limit − outstanding` | accrues to the open statement | pay the period statement (usually API-driven, card-by-card) |

Staged credit meters exposure: only a **tranche** is available at once and it
replenishes continuously as the client draws, capped at the cycle limit — exactly
`min(tranche, limit − outstanding)`. A frozen (overdue) line can spend nothing.
Every draw is written to the `wallet_transactions` ledger (`credit_draw` /
`consignment_draw`). `SAR` stays the internal base.

**Contract signing workflow** (`contracts` + append-only `contract_events` audit):

1. **Sales** uploads a draft contract and proposes the terms (account type, credit
   limit, tranche, net-days, cycle) → the reseller is notified.
2. **Reseller** downloads it from their new **My Contract** page, signs offline, and
   uploads the signed copy.
3. **Activation** applies the terms and unlocks ordering. **Governance:** the sales
   owner can activate prepaid or credit/consignment lines **≤ `AUTO_APPROVE_CAP`**
   (100,000 SAR); anything above needs **CCO** (Contract Approvals queue), and Finance
   is notified on every new credit line. The team is alerted when a client hits their
   limit.

Contract files are stored privately (`uploads/contracts/`) and served only to the
owning reseller, their sales manager, CCO, Finance and admin. Full design and the
phase roadmap live in [docs/ACCOUNT_MODELS.md](docs/ACCOUNT_MODELS.md). Coverage:
`tests/e2e_v13.py` (31 checks). *Next phases: statements & settlement, additional-credit
requests, consignment period statements, API `/account` + `/statement`, dashboards.*

## Forecast Visibility for Operations + Prospect Auto-Suspension (v12)

**Forecasts now reach Operations for stock planning.** When a reseller submits a
purchase forecast it still goes to their sales manager, and in addition:

- Operations is notified and gets a dedicated **Demand Forecasts** page
  (`/ops/forecasts`) that aggregates upcoming demand across the whole book —
  headline totals, **demand by merchant**, and **top forecasted products** (units +
  estimated value), plus the list of individual forecasts. All figures are in SAR
  (the internal base), so Ops can plan stock and sourcing ahead.
- `get_forecast_demand_summary(days=90)` and `get_all_forecasts()` back the view.

**New resellers are auto-suspended if they don't convert in 15 days.** A reseller who
neither signs a contract nor places an order within `PROSPECT_SUSPEND_DAYS` (15) of
registration is automatically suspended:

- `auto_suspend_at` is stamped at registration (= created + 15 days). A daily
  housekeeping sweep (`run_prospect_suspension`, piggybacked on the once-a-day gate)
  suspends any prospect past the deadline with no contract and no orders.
- **Suspended = cannot log in** — the login route rejects them and a `before_request`
  guard signs out anyone suspended mid-session, with a "contact your account manager"
  message.
- **Still visible to Sales** — they show a `Suspended` lifecycle chip on *My Resellers*
  with a **Reactivate** button; reactivating clears the suspension and grants a fresh
  15-day window (`set_reseller_suspended`).
- Sales + admin/CCO are notified on each auto-suspension. Contracted/active resellers
  and anyone who has purchased are exempt. Sales preview is unaffected (the session
  user there is the sales manager, not the previewed reseller).
- Existing resellers were backfilled with a fresh window from the upgrade date, so the
  migration never mass-suspends old prospects on the first sweep.
- Coverage: `tests/e2e_v12.py` (28 checks across both features).

## Single Display Currency per Reseller (v11)

Resellers no longer see a mixed-currency catalogue. Each reseller has ONE **display currency**
and sees the entire catalogue, wallet and orders in it:

- **Derived from their market at registration** — Saudi Arabia → `SAR`, any other market → `USD`
  — and the sales manager can override it (registration form + a quick-switch on My Resellers).
- **All prices converted** from each product's own currency into the display currency using the
  FX rates Finance maintains (`convert_amount()` in `enrich_products_for_reseller`), rounded to
  whole numbers and floored at cost. `SAR` remains the internal base for every report.
- **Wallet** is stored in SAR but shown in the display currency; a top-up is entered in the
  reseller's currency, converted to SAR on submit, and Finance sees both (`orig_amount` +
  the SAR credited).
- **Orders** carry a single currency, so the old cross-currency mixing is gone by construction;
  the SAR total deducted always equals the converted price the reseller saw.
- The now-redundant **currency filter was removed** from the reseller catalogue/merchant views
  (kept on staff catalogues, which still show original currencies).
- **Dropdown fix**: the multi-select filter panel is now a body-level `position:fixed` portal, so
  it renders above the results table instead of being clipped behind a card's backdrop-filter
  stacking context.
- Coverage: `tests/e2e_currency.py` (19 checks incl. a full USD-reseller journey).

## Security & Correctness Hardening (v10)

A second hardening pass, prompted by a full technical review of v9:

- **Gift-card codes/PINs encrypted at rest** (Fernet, key in `ONECARD_ENCRYPTION_KEY` or a
  persisted git-ignored `instance_encryption.key`). Lookups use a SHA-256 `code_hash` column
  instead of the plaintext — the database no longer holds a readable copy of any code or PIN.
  Existing rows are migrated automatically and idempotently at startup.
- **Wallet-overdraft and gift-card-oversell races closed**: `create_order()` and
  `review_topup()` now run their balance/stock check *and* the deduction inside one
  `BEGIN IMMEDIATE` transaction, so two concurrent orders (or two Finance approvals of the same
  top-up) can no longer both pass a stale check — the second cleanly fails instead of the wallet
  going negative or a customer being under-delivered gift-card codes. Verified with real
  concurrent threads in `tests/e2e_hardening2.py`, not just sequential calls.
- **Redemption brute-force guard**: repeated wrong-PIN attempts on the same gift-card code are
  rate-limited (8 / 15 min), reusing the same in-memory limiter as login.
- **Session cookie policy**: `HttpOnly`, `SameSite=Lax`, a 12-hour lifetime (down from Flask's
  31-day default), and `SESSION_COOKIE_SECURE` toggled via `ONECARD_COOKIE_SECURE=1` once
  deployed behind HTTPS.
- **Receipt uploads validated by content, not filename** — magic-byte sniffing accepts real
  PNG/JPEG/PDF/WEBP bytes only; a renamed non-image file is rejected regardless of its extension.
- **Automatic daily database backups** (SQLite backup API, 14-day rotation) plus an admin
  "Backup Database Now" button; the daily compliance check that used to open a DB connection on
  *every* request is now throttled in-process first.
- Coverage: `tests/e2e_hardening2.py` (27 checks) — all 8 suites green, 222 checks total.

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
pip install -r requirements.txt   # flask, pandas, openpyxl, bcrypt, cryptography
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

**What is already production-grade**: role model & permissions, all business workflows, audit
trails, encryption-at-rest for gift-card codes/PINs, race-condition-safe wallet and stock
operations, CSRF/session/rate-limit/content-sniffing hardening, automated daily backups,
FX-consistent money math, a documented Integration API v1 with idempotency + webhooks, automated
migrations, and the e2e test suite in `tests/` (**222 checks across 8 files** — run the app, then
`python tests/e2e_*.py`).

**Deliberately left for the integration phase** (by design, not omission):

| Area | Current state | Recommended move |
|------|---------------|------------------|
| Database | SQLite, serialized with `BEGIN IMMEDIATE` on money/stock writes | PostgreSQL + `DECIMAL` money columns + Alembic migrations; `BEGIN IMMEDIATE` was the SQLite-appropriate fix, `SELECT … FOR UPDATE` is the Postgres equivalent |
| Catalogue payloads | Full catalogue rendered per page (~1.5 MB) | Server-side pagination/search (the `/api/v1/catalogue` endpoint already paginates — reuse that pattern for the HTML views) |
| Background jobs | Daily checks piggyback on requests, now throttled in-process first (`daily_compliance_check`) | Move to cron/Celery; the functions are already self-contained |
| APIs | ✅ Done in v9 — `/api/v1/*` (catalogue, wallet, orders, codes), idempotency keys, outbound webhooks, fulfillment adapters | Add auth scopes/rotation policy + rate limiting at the API layer for production traffic |
| Sales data | Platform orders stand in for real sales | Point `get_month_total_orders()` at the company sales feed |
| Payments | Manual receipt verification (now content-sniffed on upload) | Bank API/webhooks writing into the same `wallet_transactions` ledger |
| Deployment | `python app.py` (Waitress/Gunicorn ready) | WSGI server behind a reverse proxy + `ONECARD_SECRET_KEY`/`ONECARD_ENCRYPTION_KEY`/`ONECARD_COOKIE_SECURE` env + Docker |
| Order lifecycle | `placed` + per-line `fulfillment_status` (delivered/external) | Add cancel/refund and full delivery states when connected to provisioning |
| Backups | Local daily SQLite snapshot, 14-day rotation | Ship backups off-box (S3/blob) — a local copy next to the live DB doesn't survive a disk failure |
| Not yet built | Invoicing/VAT, refunds, partner-settlement execution (marking a payout as *paid*), multi-user partner accounts (one login per branch/cashier), email/SMS notifications | Product-scope decisions for the next iteration, not technical debt |

---

*Built for OneCard Digital Distribution — v7, July 2026*
