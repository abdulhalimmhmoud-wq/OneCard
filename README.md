# OneCard — Reseller Operations Platform (v6)

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
| Admin | `admin@onecard.com` | `OneCard2025!` |
| Sales | `sales@onecard.com` | `Sales2025!` |
| CCO | `cco@onecard.com` | `Cco2025!` |
| Finance | `finance@onecard.com` | `Finance2025!` |
| Operations | `ops@onecard.com` | `Ops2025!` |

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

---

*Built for OneCard Digital Distribution — v5, July 2026*
